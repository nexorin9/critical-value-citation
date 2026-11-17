"""批量重放、吞吐基准与断点续跑（任务 id 25）。

覆盖三件事：

1. **断点续跑** —— 中断后从游标继续，已处理帧不重复处理，续跑前后处理帧数
   相加等于总数；
2. **幂等** —— 同一批帧连续跑两遍，台账事件数不增加；
3. **边界** —— 文件内容变了游标作废、游标文件损坏、限速开关、单帧失败不
   中断整批。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from critval.config import Config
from critval.console.inbound import LedgerInboundProcessor
from critval.hl7.replay import (
    BatchReplayer,
    FrameOutcome,
    HL7LedgerSink,
    ReplayCursor,
    ReplayFrame,
    load_source,
    load_sources,
    split_frames,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SAMPLE_FRAMES = PROJECT_ROOT / "samples" / "hl7"
THRESHOLD = PROJECT_ROOT / "samples" / "threshold" / "示例危急值阈值表.xlsx"
POLICY = PROJECT_ROOT / "samples" / "policy" / "示例危急值报告制度.docx"

sys_path = str(PROJECT_ROOT / "scripts")
if sys_path not in __import__("sys").path:
    __import__("sys").path.insert(0, sys_path)

from _samplerefs import read_sample_clause  # noqa: E402


# ---------------------------------------------------------------------------
# 测试替身
# ---------------------------------------------------------------------------


class CountingSink:
    """只数帧、不落库的 sink（测重放机制本身）。"""

    def __init__(self, *, fail_at: int = -1, error: BaseException = RuntimeError) -> None:
        self.seen: list[tuple[str, int]] = []
        self.attempts = 0
        #: 第几次调用抛错（1 起；-1 = 不抛）
        self.fail_at = fail_at
        self.error = error

    def __call__(self, frame: Any) -> FrameOutcome:
        self.attempts += 1
        if 0 < self.fail_at == self.attempts:
            raise self.error("注入的失败")
        self.seen.append((frame.source, frame.index))
        return FrameOutcome(
            source=frame.source,
            index=frame.index,
            status="processed",
            events=1,
            events_new=1,
        )


def _corpus(tmp_path: Path, count: int = 6) -> list[Any]:
    """造一个小语料：把样例帧复制成多份（每份换消息控制号，避免幂等键撞车）。"""
    source_frames = []
    for path in sorted(SAMPLE_FRAMES.glob("*.hl7")):
        source_frames.extend(split_frames(path.read_bytes()))
    if not source_frames:
        pytest.skip("样例帧缺失，无法构造语料")

    target = tmp_path / "corpus"
    target.mkdir(parents=True, exist_ok=True)
    for index in range(count):
        raw = source_frames[index % len(source_frames)]
        # 换掉 MSH-10 消息控制号：同一批帧里的不同帧必须是不同的幂等键
        lines = raw.decode("utf-8").split("\r")
        for position, line in enumerate(lines):
            if line.startswith("MSH|"):
                fields = line.split("|")
                if len(fields) > 9:
                    fields[9] = f"TEST{index:06d}"
                lines[position] = "|".join(fields)
                break
        (target / f"part-{index:04d}.hl7").write_bytes("\r".join(lines).encode("utf-8"))
    return load_sources(target)


def _ledger_sink(store: Any) -> HL7LedgerSink:
    from critval.kb.loader import load_threshold_table

    table = load_threshold_table(THRESHOLD)
    clause = read_sample_clause(POLICY)
    processor = LedgerInboundProcessor(
        store,
        rows_provider=lambda: table.rows,
        clause_provider=lambda: clause,
    )
    return HL7LedgerSink(processor, store=store)


# ---------------------------------------------------------------------------
# 帧拆分与来源
# ---------------------------------------------------------------------------


def test_按MLLP结束符拆帧且剥掉起始符():
    payload = b"\x0bMSH|a\rOBX|b\r\x1c\r\x0bMSH|c\rOBX|d\r\x1c\r"
    frames = split_frames(payload)
    assert len(frames) == 2
    assert frames[0] == b"MSH|a\rOBX|b"
    assert frames[1] == b"MSH|c\rOBX|d"


def test_单帧文件原样拆出一帧():
    frames = split_frames(b"MSH|a\rOBX|b\r")
    assert len(frames) == 1


def test_空帧被丢掉():
    assert split_frames(b"\x1c\r\x1c\r") == []


def test_来源按文件名排序且指纹稳定(tmp_path: Path):
    sources = _corpus(tmp_path, count=3)
    assert [source.key for source in sources] == sorted(
        source.key for source in sources
    )
    again = load_sources(tmp_path / "corpus")
    assert [source.sha256 for source in again] == [source.sha256 for source in sources]


# ---------------------------------------------------------------------------
# 断点续跑
# ---------------------------------------------------------------------------


def test_续跑从游标继续且不重复处理(tmp_path: Path):
    sources = _corpus(tmp_path, count=5)
    total = sum(source.frame_count for source in sources)
    cursor = ReplayCursor(tmp_path / "cursor.json")

    first_sink = CountingSink()
    first = BatchReplayer(first_sink, cursor=cursor, checkpoint_every=0).run(sources)
    assert first.frames_processed == total
    assert first.frames_skipped == 0
    assert first.resumed is False

    second_sink = CountingSink()
    second = BatchReplayer(second_sink, cursor=cursor, checkpoint_every=0).run(sources)
    assert second.frames_skipped == total
    assert second.frames_processed == 0
    assert second.resumed is True
    # 续跑那一轮一帧都没碰
    assert second_sink.seen == []


def test_中断后续跑帧数相加等于总数(tmp_path: Path):
    """进程在第 3 帧被打断 —— 续跑后两轮处理帧数相加等于总数。"""
    sources = _corpus(tmp_path, count=6)
    total = sum(source.frame_count for source in sources)
    cursor = ReplayCursor(tmp_path / "cursor.json")

    class InterruptSink(CountingSink):
        def __call__(self, frame: Any) -> FrameOutcome:
            if len(self.seen) >= 3:
                # 模拟进程被中断（KeyboardInterrupt 不会被 except Exception 吞掉）
                raise KeyboardInterrupt("模拟中断")
            return super().__call__(frame)

    first_sink = InterruptSink()
    with pytest.raises(KeyboardInterrupt):
        BatchReplayer(first_sink, cursor=cursor, checkpoint_every=0).run(sources)
    assert len(first_sink.seen) == 3

    # 游标已落盘（finally 里保存）
    assert cursor.total_done() == 3

    second_sink = CountingSink()
    second = BatchReplayer(second_sink, cursor=cursor, checkpoint_every=0).run(sources)
    assert second.frames_skipped == 3
    assert second.frames_processed == total - 3
    assert len(first_sink.seen) + len(second_sink.seen) == total


def test_游标文件内容变了则作废从头重放(tmp_path: Path):
    sources = _corpus(tmp_path, count=3)
    total = sum(source.frame_count for source in sources)
    cursor = ReplayCursor(tmp_path / "cursor.json")
    BatchReplayer(CountingSink(), cursor=cursor, checkpoint_every=0).run(sources)
    assert cursor.total_done() == total

    # 同一个来源键，但文件内容变了（sha256 不同）→ 旧游标作废
    stale = ReplayCursor(tmp_path / "cursor.json")
    assert stale.done_for(sources[0].key, "0" * 64) == 0


def test_游标文件损坏时当作没有游标(tmp_path: Path):
    path = tmp_path / "cursor.json"
    path.write_text("{ 这不是 JSON", encoding="utf-8")
    cursor = ReplayCursor(path).load()
    assert cursor.total_done() == 0


def test_游标原子落盘且内容可读(tmp_path: Path):
    path = tmp_path / "cursor.json"
    cursor = ReplayCursor(path)
    cursor.mark("a.hl7", "abc", 7)
    cursor.save()
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["sources"][0]["frames_done"] == 7
    assert payload["sources"][0]["source_sha256"] == "abc"
    assert not (tmp_path / "cursor.json.tmp").exists()


# ---------------------------------------------------------------------------
# 幂等
# ---------------------------------------------------------------------------


def test_同一批帧跑两遍台账事件数不变(tmp_path: Path):
    from critval.ledger.store import LedgerStore

    Config.init_env()
    sources = _corpus(tmp_path, count=6)
    db_path = tmp_path / "ledger.db"

    with LedgerStore(db_path) as store:
        sink = _ledger_sink(store)
        first = BatchReplayer(sink, checkpoint_every=0).run(sources)
        after_first = store.count_events()
        second = BatchReplayer(sink, checkpoint_every=0).run(sources)
        after_second = store.count_events()

    assert first.events_new > 0
    assert after_first == after_second
    assert second.events_new == 0
    assert second.events_duplicate == first.events_new


def test_重放与HTTP入站判定口径一致(tmp_path: Path):
    """重放走的是 HTTP 推送通道同一个 processor —— 结论口径必须一致。"""
    from critval.console.inbound import LedgerInboundProcessor
    from critval.hl7.normalize import normalize_events, parse_oru_r01
    from critval.judge.critical import judge_event
    from critval.ledger.store import LedgerStore

    Config.init_env()
    sources = _corpus(tmp_path, count=3)
    frame = sources[0].frames[0]
    message = parse_oru_r01(frame)
    events, _unparsed = normalize_events(message)
    assert events

    from critval.kb.loader import load_threshold_table

    table = load_threshold_table(THRESHOLD)
    with LedgerStore(tmp_path / "same.db") as store:
        processor = LedgerInboundProcessor(
            store, rows_provider=lambda: table.rows, clause_provider=lambda: None
        )
        replay_outcome = HL7LedgerSink(processor, store=store)(
            ReplayFrame(source="x", index=0, raw=frame)
        )
    direct = judge_event(events[0], table.rows)
    assert replay_outcome.decision == direct.decision


# ---------------------------------------------------------------------------
# 边界
# ---------------------------------------------------------------------------


def test_单帧失败不中断整批(tmp_path: Path):
    sources = _corpus(tmp_path, count=4)
    total = sum(source.frame_count for source in sources)
    sink = CountingSink(fail_at=1)
    report = BatchReplayer(sink, checkpoint_every=0).run(sources)
    assert report.failed == 1
    assert report.frames_processed == total
    assert len(report.failures) == 1
    assert "注入的失败" in report.failures[0].detail


def test_限速开关会按设定节流(tmp_path: Path):
    sources = _corpus(tmp_path, count=3)
    sleeps: list[float] = []
    BatchReplayer(
        CountingSink(),
        rate_limit=2.0,
        sleep=sleeps.append,
        checkpoint_every=0,
    ).run(sources)
    assert len(sleeps) == 3
    assert all(abs(value - 0.5) < 1e-9 for value in sleeps)


def test_不限速时不会休眠(tmp_path: Path):
    sources = _corpus(tmp_path, count=3)
    sleeps: list[float] = []
    BatchReplayer(CountingSink(), sleep=sleeps.append, checkpoint_every=0).run(sources)
    assert sleeps == []


def test_进度回调按帧推进(tmp_path: Path):
    sources = _corpus(tmp_path, count=3)
    marks: list[tuple[int, int]] = []
    BatchReplayer(
        CountingSink(),
        progress=lambda done, total, report: marks.append((done, total)),
        checkpoint_every=0,
    ).run(sources)
    total = sum(source.frame_count for source in sources)
    assert marks
    assert marks[-1] == (total, total)


def test_读取不存在的目录返回空来源(tmp_path: Path):
    assert load_sources(tmp_path / "没有这个目录") == []


def test_读单个来源得到帧列表(tmp_path: Path):
    sources = _corpus(tmp_path, count=1)
    single = load_source(sources[0].key)
    assert single.frame_count == sources[0].frame_count
    assert single.sha256 == sources[0].sha256


def test_游标可以忘掉某个来源():
    cursor = ReplayCursor(None)
    cursor.mark("a.hl7", "abc", 5)
    assert cursor.total_done() == 5
    cursor.forget("a.hl7")
    assert cursor.total_done() == 0


def test_报告吞吐按处理帧数算(tmp_path: Path):
    sources = _corpus(tmp_path, count=2)
    report = BatchReplayer(CountingSink(), checkpoint_every=0).run(sources)
    assert report.frames_done == report.frames_processed
    assert report.throughput > 0
