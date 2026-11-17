#!/usr/bin/env python3
"""入站吞吐基准：ER7 帧重放 → 规范化 → 判定 → 落台账。

回答的是「院内一天的真实报文量下，守护进程扛不扛得住」：

- **总帧数**、**吞吐（帧/秒）**；
- **单帧耗时分布**（P50 / P90 / P95 / P99 / 最大）—— 决定延迟告警该定在
  什么水位；
- **峰值内存**（``tracemalloc`` 堆峰值 + ``resource`` 进程 RSS 高水位，双测）；
- **幂等重放**（同一批帧再跑一遍）—— 台账事件数不增加，重复帧被幂等键挡住。

三个阶段：

============  ==============================================  ================
阶段          做什么                                        回答什么
============  ==============================================  ================
``parse``     解析 + 规范化 + 判定（不落库）                纯计算开销
``ingest``    解析 + 规范化 + 判定 + 落台账 + 出卡（空库）  端到端开销
``idempotent`` 同上，但台账里已有这批事件                  幂等路径开销
============  ==============================================  ================

用法::

    python3 scripts/bench_ingest.py                       # 跑 parse + ingest
    python3 scripts/bench_ingest.py --mode all            # 再加幂等重放
    python3 scripts/bench_ingest.py --frames data/synth/oru --limit 500
    python3 scripts/bench_ingest.py --json                # 机器可读

**RSS 是进程级高水位**（``ru_maxrss`` 只增不减），所以三个阶段的 RSS 数字
是「到该阶段为止的进程峰值」；堆峰值（``tracemalloc``）按阶段重置，是分阶段的。
"""

from __future__ import annotations

import argparse
import json
import math
import resource
import sys
import time
import tracemalloc
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _samplerefs import read_sample_clause  # noqa: E402
from critval.config import Config  # noqa: E402
from critval.console.inbound import LedgerInboundProcessor  # noqa: E402
from critval.hl7.normalize import normalize_events, parse_oru_r01  # noqa: E402
from critval.hl7.replay import (  # noqa: E402
    BatchReplayer,
    FrameOutcome,
    FrameSource,
    HL7LedgerSink,
    ReplayReport,
    load_sources,
)
from critval.judge.critical import judge_event  # noqa: E402
from critval.kb.loader import load_threshold_table  # noqa: E402
from critval.ledger.store import LedgerStore  # noqa: E402

DEFAULT_FRAMES = PROJECT_ROOT / "data" / "synth" / "oru"
DEFAULT_THRESHOLD = PROJECT_ROOT / "samples" / "threshold" / "示例危急值阈值表.xlsx"
DEFAULT_POLICY = PROJECT_ROOT / "samples" / "policy" / "示例危急值报告制度.docx"
DEFAULT_DB = PROJECT_ROOT / "data" / "bench_ingest.db"
DEFAULT_REPORT = PROJECT_ROOT / "reports" / "ingest_bench.json"


# ---------------------------------------------------------------------------
# 计时
# ---------------------------------------------------------------------------


class TimingSink:
    """包一层 sink，记录每帧耗时（毫秒）。

    计时**包含 sink 内部的一切**：解析、规范化、判定、落库、出卡。
    单帧耗时的分布才是延迟告警该看的东西 —— 只测解析会漏掉 SQLite 写事务。
    """

    def __init__(self, inner: Callable[[Any], FrameOutcome]) -> None:
        self.inner = inner
        self.durations_ms: list[float] = []

    def __call__(self, frame: Any) -> FrameOutcome:
        started = time.perf_counter()
        try:
            return self.inner(frame)
        finally:
            self.durations_ms.append((time.perf_counter() - started) * 1000.0)


class ParseOnlySink:
    """只算不落库：解析 → 规范化 → 判定。

    用来把「纯计算开销」与「落库开销」分开 —— 两者混在一起就说不清
    「慢是慢在判定还是慢在写库」。
    """

    def __init__(self, rows: list[Any]) -> None:
        self.rows = rows
        self.events = 0
        self.critical = 0
        self.unparsed = 0

    def __call__(self, frame: Any) -> FrameOutcome:
        message = parse_oru_r01(frame.raw)
        events, unparsed = normalize_events(message)
        self.unparsed += len(unparsed)
        for event in events:
            judgement = judge_event(event, self.rows)
            self.events += 1
            if judgement.decision == "critical":
                self.critical += 1
        return FrameOutcome(
            source=frame.source,
            index=frame.index,
            status="processed" if events else "unparsed",
            events=len(events),
            events_new=len(events),
        )


# ---------------------------------------------------------------------------
# 统计
# ---------------------------------------------------------------------------


def percentile(values: list[float], q: float) -> float:
    """最近秩百分位（不做插值 —— 报出来的就是真跑出来的那个值）。"""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, math.ceil(q / 100.0 * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


def rss_peak_mib() -> float:
    """进程 RSS 高水位（MiB）。

    ``ru_maxrss`` 在 Linux 上是 KiB、在 macOS 上是字节 —— 平台差异在这里
    抹平，报出来统一是 MiB。
    """
    raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform == "darwin":
        return raw / (1024.0 * 1024.0)
    return raw / 1024.0


@dataclass
class PhaseResult:
    """一个阶段的测量结果。"""

    name: str
    frames: int = 0
    elapsed_seconds: float = 0.0
    events_new: int = 0
    events_duplicate: int = 0
    unparsed: int = 0
    failed: int = 0
    event_total: int = 0
    latency_ms: dict[str, float] = field(default_factory=dict)
    heap_peak_mib: float = 0.0
    rss_peak_mib: float = 0.0
    note: str = ""

    @property
    def throughput(self) -> float:
        if self.elapsed_seconds <= 0:
            return 0.0
        return self.frames / self.elapsed_seconds

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase": self.name,
            "frames": self.frames,
            "elapsed_seconds": round(self.elapsed_seconds, 4),
            "throughput_fps": round(self.throughput, 2),
            "events_new": self.events_new,
            "events_duplicate": self.events_duplicate,
            "unparsed": self.unparsed,
            "failed": self.failed,
            "event_total": self.event_total,
            "latency_ms": {key: round(value, 3) for key, value in self.latency_ms.items()},
            "heap_peak_mib": round(self.heap_peak_mib, 2),
            "rss_peak_mib": round(self.rss_peak_mib, 2),
            "note": self.note,
        }


def summarize(
    name: str,
    report: ReplayReport,
    durations: list[float],
    heap_peak_mib: float,
    rss: float,
    event_total: int = 0,
    note: str = "",
) -> PhaseResult:
    """把重放报告 + 耗时样本汇总成可复核的阶段结果。"""
    return PhaseResult(
        name=name,
        frames=report.frames_processed,
        elapsed_seconds=report.elapsed_seconds,
        events_new=report.events_new,
        events_duplicate=report.events_duplicate,
        unparsed=report.unparsed,
        failed=report.failed,
        event_total=event_total,
        latency_ms={
            "p50": percentile(durations, 50),
            "p90": percentile(durations, 90),
            "p95": percentile(durations, 95),
            "p99": percentile(durations, 99),
            "max": max(durations) if durations else 0.0,
            "mean": (sum(durations) / len(durations)) if durations else 0.0,
        },
        heap_peak_mib=heap_peak_mib,
        rss_peak_mib=rss,
        note=note,
    )


# ---------------------------------------------------------------------------
# 阶段执行
# ---------------------------------------------------------------------------


def _progress(label: str, every: int) -> Callable[[int, int, ReplayReport], None]:
    def _emit(done: int, total: int, report: ReplayReport) -> None:
        if every <= 0 or done % every != 0:
            return
        elapsed = report.elapsed_seconds or 1e-9
        print(
            f"  [{label}] {done}/{total} 帧 · {done / elapsed:.0f} 帧/秒",
            flush=True,
        )

    return _emit


def run_parse_phase(
    sources: list[FrameSource],
    rows: list[Any],
    progress_every: int,
) -> PhaseResult:
    """阶段一：只算不落库。"""
    sink = ParseOnlySink(rows)
    timing = TimingSink(sink)
    tracemalloc.start()
    tracemalloc.reset_peak()
    replayer = BatchReplayer(timing, progress=_progress("parse", progress_every))
    report = replayer.run(sources)
    _, heap_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    result = summarize(
        "parse",
        report,
        timing.durations_ms,
        heap_peak / (1024.0 * 1024.0),
        rss_peak_mib(),
        note=f"纯计算（解析+规范化+判定），临界 {sink.critical} 条 / 共 {sink.events} 条结果",
    )
    result.unparsed = sink.unparsed
    return result


def run_ingest_phase(
    sources: list[FrameSource],
    db_path: Path,
    threshold_path: Path,
    policy_path: Path,
    progress_every: int,
    *,
    fresh: bool,
    note: str,
    phase_name: str = "ingest",
) -> PhaseResult:
    """阶段二 / 三：端到端落台账。

    ``fresh=True`` 时先把旧库删掉 —— 空库插入与幂等重放是两条不同的路径，
    混在一起测出来的吞吐没有意义。
    """
    if fresh and db_path.exists():
        db_path.unlink()
        for suffix in ("-wal", "-shm"):
            sidecar = db_path.with_name(db_path.name + suffix)
            if sidecar.exists():
                sidecar.unlink()

    table = load_threshold_table(threshold_path)
    clause = read_sample_clause(policy_path)

    with LedgerStore(db_path) as store:
        processor = LedgerInboundProcessor(
            store,
            rows_provider=lambda: table.rows,
            clause_provider=lambda: clause,
        )
        sink = HL7LedgerSink(processor, store=store)
        timing = TimingSink(sink)
        tracemalloc.start()
        tracemalloc.reset_peak()
        # 基准**不带续跑游标** —— 基准要量的是「全量重放」的开销，
        # 带上游标会把帧跳过去，测出来的数字是假的。
        replayer = BatchReplayer(
            timing,
            progress=_progress("ingest", progress_every),
            checkpoint_every=0,
        )
        report = replayer.run(sources)
        _, heap_peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        event_total = store.count_events()

    return summarize(
        phase_name,
        report,
        timing.durations_ms,
        heap_peak / (1024.0 * 1024.0),
        rss_peak_mib(),
        event_total=event_total,
        note=note,
    )


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------


def render(payload: dict[str, Any]) -> str:
    """人读的基准报告。"""
    lines: list[str] = []
    lines.append("== 入站吞吐基准 ==")
    lines.append(
        f"语料 {payload['corpus']['frames']} 帧 / {payload['corpus']['files']} 个文件"
        f" · 阈值表 {payload['corpus']['threshold_table']}"
    )
    lines.append("")
    header = (
        f"{'阶段':<12}{'帧数':>8}{'耗时(s)':>10}{'吞吐(帧/s)':>12}"
        f"{'P50(ms)':>10}{'P95(ms)':>10}{'P99(ms)':>10}{'堆峰(MiB)':>12}{'RSS(MiB)':>11}"
    )
    lines.append(header)
    lines.append("-" * len(header))
    for phase in payload["phases"]:
        lines.append(
            f"{phase['phase']:<12}{phase['frames']:>8}{phase['elapsed_seconds']:>10.2f}"
            f"{phase['throughput_fps']:>12.0f}{phase['latency_ms']['p50']:>10.3f}"
            f"{phase['latency_ms']['p95']:>10.3f}{phase['latency_ms']['p99']:>10.3f}"
            f"{phase['heap_peak_mib']:>12.2f}{phase['rss_peak_mib']:>11.2f}"
        )
    lines.append("")
    for phase in payload["phases"]:
        lines.append(
            f"[{phase['phase']}] 新增事件 {phase['events_new']} · 幂等跳过 "
            f"{phase['events_duplicate']} · 未识别 {phase['unparsed']} · 失败 "
            f"{phase['failed']} · 台账事件总数 {phase['event_total']}"
        )
        if phase.get("note"):
            lines.append(f"        {phase['note']}")
    if payload.get("idempotency"):
        idem = payload["idempotency"]
        lines.append("")
        lines.append(
            f"[幂等验收] 首轮台账事件 {idem['event_total_first']} 条 → "
            f"重放后 {idem['event_total_second']} 条"
            f"（{'一致' if idem['stable'] else '不一致'}）"
        )
    lines.append("")
    lines.append(
        "口径：单帧耗时含解析、规范化、判定、落库与出卡；RSS 为进程级高水位"
        "（ru_maxrss 只增不减）；堆峰为该阶段 tracemalloc 峰值。"
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="入站吞吐基准（真实规模）")
    parser.add_argument("--frames", default=str(DEFAULT_FRAMES), help="ER7 帧目录")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="基准用的 SQLite 库路径")
    parser.add_argument("--threshold", default=str(DEFAULT_THRESHOLD), help="阈值表 xlsx 路径")
    parser.add_argument("--policy", default=str(DEFAULT_POLICY), help="院内危急值报告制度 docx 路径")
    parser.add_argument("--report", default=str(DEFAULT_REPORT), help="报告 JSON 落盘路径")
    parser.add_argument(
        "--mode",
        choices=("parse", "ingest", "both", "all"),
        default="both",
        help="跑哪些阶段（默认 both = parse + ingest）",
    )
    parser.add_argument("--limit", type=int, default=0, help="最多处理多少帧（0 = 全部）")
    parser.add_argument("--progress-every", type=int, default=500, help="每多少帧打一次进度")
    parser.add_argument("--json", action="store_true", help="只输出 JSON")
    args = parser.parse_args(argv)

    Config.init_env()

    sources = load_sources(args.frames)
    if not sources:
        print(f"没有找到帧文件：{args.frames}", file=sys.stderr)
        return 1
    total_frames = sum(source.frame_count for source in sources)
    if args.limit:
        limited = []
        remaining = args.limit
        for source in sources:
            if remaining <= 0:
                break
            take = min(remaining, source.frame_count)
            limited.append(
                FrameSource(
                    key=source.key,
                    path=source.path,
                    sha256=source.sha256,
                    frames=source.frames[:take],
                )
            )
            remaining -= take
        sources = limited
        total_frames = sum(source.frame_count for source in sources)

    table = load_threshold_table(args.threshold)
    phases: list[PhaseResult] = []

    if args.mode in ("parse", "both", "all"):
        phases.append(run_parse_phase(sources, table.rows, args.progress_every))
    if args.mode in ("ingest", "both", "all"):
        phases.append(
            run_ingest_phase(
                sources,
                Path(args.db),
                Path(args.threshold),
                Path(args.policy),
                args.progress_every,
                fresh=True,
                note="端到端落台账（空库插入）",
                phase_name="ingest",
            )
        )
    if args.mode == "all":
        idempotent = run_ingest_phase(
            sources,
            Path(args.db),
            Path(args.threshold),
            Path(args.policy),
            args.progress_every,
            fresh=False,
            note="同一批帧再跑一遍（幂等路径）",
            phase_name="idempotent",
        )
        phases.append(idempotent)

    first_total = next((phase.event_total for phase in phases if phase.name == "ingest"), 0)
    last_total = phases[-1].event_total if phases else 0
    idempotency = None
    if args.mode == "all":
        idempotency = {
            "event_total_first": first_total,
            "event_total_second": last_total,
            "stable": first_total == last_total,
        }

    payload: dict[str, Any] = {
        "corpus": {
            "frames": total_frames,
            "files": len(sources),
            "threshold_table": Path(args.threshold).name,
            "policy": Path(args.policy).name,
        },
        "phases": [phase.to_dict() for phase in phases],
    }
    if idempotency is not None:
        payload["idempotency"] = idempotency

    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(render(payload))
        print(f"\n报告已落盘：{report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
