"""批量重放：ER7 帧目录 / 语料 → 规范化 → 判定 → 台账（带断点续跑游标）。

守护进程上线前要回答三个问题，本模块就是这三个问题的机制：

1. **扛不扛得住** —— 一天的报文量级下入站处理有多快、单帧耗时分布如何、
   峰值内存多大（``scripts/bench_ingest.py`` 用它出基准）。
2. **断了能不能接着跑** —— 重放中途被打断，从游标继续，不必从头重来
   （:class:`ReplayCursor` + :class:`BatchReplayer`）。
3. **跑两遍会不会写脏台账** —— 幂等键挡住重复落库，重放不覆盖既有事实。

**本模块只管「取帧 → 交给 sink → 记游标」**，判定与出卡不在这里写：
sink 用 ``critval.console.inbound.LedgerInboundProcessor`` —— 与 HTTP 入站
通道**同一条处理路径**。这样「重放出来的结论」和「实时收下来的结论」不会
出现两套口径（那是批量回灌最容易出的错：批量看着对，实时却判得不一样）。

**游标记什么**：每个来源文件的 ``sha256`` + 已处理帧数。文件内容变了
（sha256 不一致）→ 该文件的游标作废、从头重放 —— 宁可多放一遍（幂等键
会挡住重复落库），也不能拿旧偏移去读新内容。

**进程被强杀（kill -9）怎么办**：``finally`` 里的落盘救不了被 SIGKILL 的
进程，那种情况靠**周期性检查点**（``checkpoint_every`` 帧）续跑，损失不超过
一个检查点间隔的帧数。
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from critval.hl7.normalize import normalize_events, parse_oru_r01

__all__ = [
    "BatchReplayer",
    "FrameOutcome",
    "FrameSource",
    "HL7LedgerSink",
    "ReplayCursor",
    "ReplayReport",
    "load_source",
    "load_sources",
    "split_frames",
]

#: MLLP 帧结束符（FS + CR）。批量语料里用它切帧。
FRAME_END = b"\x1c\r"
#: MLLP 帧起始符（VT）。
FRAME_START = b"\x0b"
#: 帧文件后缀（ER7 是纯文本，常见 .hl7 / .txt）。
FRAME_SUFFIXES: tuple[str, ...] = (".hl7", ".txt")


def sha256_bytes(payload: bytes) -> str:
    """字节指纹。"""
    return hashlib.sha256(payload).hexdigest()


def split_frames(payload: bytes) -> list[bytes]:
    """把一个文件里的 ER7 帧拆出来。

    帧之间用 MLLP 结束符 ``\\x1c\\r`` 分隔，帧首的 ``\\x0b`` 起始符剥掉。
    一个文件里只有一帧（常见于样例帧）时原样返回。**空帧丢掉** —— 空帧不是
    报文，读不出任何东西。
    """
    frames: list[bytes] = []
    for chunk in payload.split(FRAME_END):
        if chunk.startswith(FRAME_START):
            chunk = chunk[1:]
        text = chunk.strip(b"\r\n")
        if text:
            frames.append(text)
    return frames


@dataclass(frozen=True)
class ReplayFrame:
    """一帧 ER7 报文。"""

    source: str
    index: int
    raw: bytes

    @property
    def sha256(self) -> str:
        """单帧指纹（用于报告里定位问题帧）。"""
        return sha256_bytes(self.raw)


@dataclass(frozen=True)
class FrameSource:
    """一个帧来源（一个文件）。

    文件内容在构造时读进内存并算好 sha256 —— 重放期间文件不会再变，
    拿到的是**同一份内容的一致视图**，不会出现「前半段读的是旧版、后半段
    读的是新版」的撕裂。
    """

    key: str
    path: str
    sha256: str
    frames: tuple[bytes, ...]

    @property
    def frame_count(self) -> int:
        return len(self.frames)

    def iter_frames(self, start: int = 0) -> Any:
        """从第 ``start`` 帧开始迭代。"""
        for index in range(max(0, start), len(self.frames)):
            yield ReplayFrame(source=self.key, index=index, raw=self.frames[index])


def load_source(path: str | Path) -> FrameSource:
    """读一个帧文件 → :class:`FrameSource`。"""
    file_path = Path(path)
    payload = file_path.read_bytes()
    return FrameSource(
        key=str(file_path),
        path=str(file_path),
        sha256=sha256_bytes(payload),
        frames=tuple(split_frames(payload)),
    )


def load_sources(directory: str | Path) -> list[FrameSource]:
    """列出目录下所有帧文件 → :class:`FrameSource` 列表（按文件名排序，可复现）。"""
    base = Path(directory)
    if not base.is_dir():
        return []
    paths = sorted(
        path
        for path in base.iterdir()
        if path.is_file() and path.suffix.lower() in FRAME_SUFFIXES
    )
    return [load_source(path) for path in paths]


# ---------------------------------------------------------------------------
# 断点续跑游标
# ---------------------------------------------------------------------------


@dataclass
class CursorEntry:
    """一个来源文件的游标。"""

    source: str
    source_sha256: str
    frames_done: int
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "source_sha256": self.source_sha256,
            "frames_done": self.frames_done,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> Optional["CursorEntry"]:
        source = str(payload.get("source") or "")
        if not source:
            return None
        try:
            frames_done = int(payload.get("frames_done") or 0)
        except (TypeError, ValueError):
            frames_done = 0
        return cls(
            source=source,
            source_sha256=str(payload.get("source_sha256") or ""),
            frames_done=max(0, frames_done),
            updated_at=str(payload.get("updated_at") or ""),
        )


class ReplayCursor:
    """断点续跑游标。

    落盘格式是 JSON（人可读，坏了也能手工改）。**原子写**：先写临时文件
    再 ``os.replace`` —— 断电或进程被杀时不会留下半个游标文件。
    """

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else None
        self.entries: dict[str, CursorEntry] = {}

    # ---- 读写 -------------------------------------------------------------
    def load(self) -> "ReplayCursor":
        """读游标文件。**文件缺失或损坏都当作没有游标**（从头重放，幂等兜底）。"""
        if not self.path or not self.path.exists():
            return self
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return self
        if not isinstance(payload, dict):
            return self
        for item in payload.get("sources", []) or []:
            if not isinstance(item, dict):
                continue
            entry = CursorEntry.from_dict(item)
            if entry is not None:
                self.entries[entry.source] = entry
        return self

    def save(self) -> None:
        """原子写游标文件。"""
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "sources": [entry.to_dict() for entry in self.entries.values()],
        }
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        os.replace(tmp, self.path)

    # ---- 游标语义 ---------------------------------------------------------
    def done_for(self, source: str, source_sha256: str) -> int:
        """这个来源已经处理到第几帧。

        **文件内容变了 → 游标作废（返回 0）**：旧偏移对应的是旧内容，
        拿它去读新文件会读错帧。宁可重放一遍（幂等键挡住重复落库）。
        """
        entry = self.entries.get(source)
        if entry is None:
            return 0
        if entry.source_sha256 != source_sha256:
            return 0
        return entry.frames_done

    def mark(self, source: str, source_sha256: str, frames_done: int) -> None:
        """记下这个来源已处理到第几帧。"""
        self.entries[source] = CursorEntry(
            source=source,
            source_sha256=source_sha256,
            frames_done=max(0, int(frames_done)),
            updated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )

    def total_done(self) -> int:
        """所有来源已处理帧数之和。"""
        return sum(entry.frames_done for entry in self.entries.values())

    def forget(self, source: str) -> None:
        """忘掉一个来源的游标（重放该文件从头开始）。"""
        self.entries.pop(source, None)


# ---------------------------------------------------------------------------
# 处理结果
# ---------------------------------------------------------------------------


@dataclass
class FrameOutcome:
    """一帧的处理结论。"""

    source: str
    index: int
    #: ``processed``（有新事件落库）/ ``duplicate``（幂等跳过）/
    #: ``unparsed``（读不懂）/ ``failed``（处理抛异常）
    status: str
    event_id: str = ""
    decision: str = ""
    card_id: str = ""
    detail: str = ""
    #: 这一帧里的结果条数。
    events: int = 0
    #: 其中新落库（幂等键首次出现）的条数。
    events_new: int = 0
    #: 其中被幂等键挡下（既有事实，不覆盖）的条数。
    events_duplicate: int = 0
    error: str = ""


@dataclass
class ReplayReport:
    """一批重放的汇总。"""

    frames_seen: int = 0
    frames_skipped: int = 0
    #: 本轮推进过的帧数（含读不懂与失败的 —— 它们同样消耗了时间）。
    frames_processed: int = 0
    events_new: int = 0
    events_duplicate: int = 0
    unparsed: int = 0
    failed: int = 0
    elapsed_seconds: float = 0.0
    resumed: bool = False
    failures: list[FrameOutcome] = field(default_factory=list)

    @property
    def frames_done(self) -> int:
        """本次跑完的帧数（含续跑跳过的）。"""
        return self.frames_processed + self.frames_skipped

    @property
    def throughput(self) -> float:
        """吞吐（帧/秒）。"""
        if self.elapsed_seconds <= 0:
            return 0.0
        return self.frames_processed / self.elapsed_seconds


# ---------------------------------------------------------------------------
# 批量重放器
# ---------------------------------------------------------------------------


class BatchReplayer:
    """逐帧推进的重放器。

    参数：

    - ``sink`` —— 收一帧返回一条 :class:`FrameOutcome` 的可调用对象；
    - ``cursor`` —— 断点续跑游标（给了才启用续跑）；
    - ``rate_limit`` —— 限速（帧/秒，0 = 不限）。回灌演练时压着跑，别把
      院内 LIS 通道打满；
    - ``progress`` —— 进度回调 ``(done, total, report)``；
    - ``checkpoint_every`` —— 每多少帧落一次游标（进程被强杀时靠它续跑）；
    - ``sleep`` —— 休眠函数（测试里换成假函数，避免真等）。
    """

    def __init__(
        self,
        sink: Callable[[ReplayFrame], FrameOutcome],
        *,
        cursor: Optional[ReplayCursor] = None,
        rate_limit: float = 0.0,
        progress: Optional[Callable[[int, int, ReplayReport], None]] = None,
        checkpoint_every: int = 100,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.sink = sink
        self.cursor = cursor
        self.rate_limit = max(0.0, float(rate_limit))
        self.progress = progress
        self.checkpoint_every = max(0, int(checkpoint_every))
        self.sleep = sleep

    def run(self, sources: Sequence[FrameSource]) -> ReplayReport:
        """跑一批来源，返回汇总。

        **游标在 ``finally`` 里落盘** —— 中途抛异常（中断 / 崩溃）时游标
        仍然落盘，续跑才有意义。
        """
        report = ReplayReport()
        started = time.perf_counter()
        total = sum(source.frame_count for source in sources)
        done = 0

        try:
            for source in sources:
                already = (
                    self.cursor.done_for(source.key, source.sha256)
                    if self.cursor is not None
                    else 0
                )
                if already:
                    report.frames_skipped += already
                    report.resumed = True
                    done += already

                for frame in source.iter_frames(already):
                    outcome = self._process(frame)
                    report.frames_seen += 1
                    self._account(report, outcome)
                    done += 1
                    # 实时更新已用时长 —— 进度回调要在跑的过程中就能算出
                    # 当前吞吐，跑完才算就只剩一个总数了。
                    report.elapsed_seconds = time.perf_counter() - started

                    if self.cursor is not None:
                        # 游标**每帧都更新**（内存里），落盘按检查点间隔。
                        # 这样中途被打断时，finally 里落盘的是准确位置，
                        # 续跑不会重复处理已完成的帧。
                        self.cursor.mark(source.key, source.sha256, frame.index + 1)
                        if self.checkpoint_every and done % self.checkpoint_every == 0:
                            self.cursor.save()

                    if self.progress is not None:
                        self.progress(done, total, report)
                    self._throttle()

                if self.cursor is not None:
                    # 这个来源跑完了 → 游标记到末尾（覆盖检查点间隔留下的偏差）
                    self.cursor.mark(source.key, source.sha256, source.frame_count)
        finally:
            # 无论正常结束还是中途抛异常，游标都要落盘 —— 否则「续跑」名存实亡。
            if self.cursor is not None:
                self.cursor.save()

        report.elapsed_seconds = time.perf_counter() - started
        return report

    # ---- 内部 -------------------------------------------------------------
    def _process(self, frame: ReplayFrame) -> FrameOutcome:
        try:
            return self.sink(frame)
        except Exception as exc:  # noqa: BLE001 —— 单帧失败不该打断整批
            return FrameOutcome(
                source=frame.source,
                index=frame.index,
                status="failed",
                detail=f"处理失败：{type(exc).__name__}: {exc}",
                error=f"{type(exc).__name__}: {exc}",
            )

    def _account(self, report: ReplayReport, outcome: FrameOutcome) -> None:
        if outcome.status in ("processed", "duplicate", "unparsed", "failed"):
            report.frames_processed += 1
        if outcome.status == "unparsed":
            report.unparsed += 1
        elif outcome.status == "failed":
            report.failed += 1
            report.failures.append(outcome)

        report.events_new += outcome.events_new
        report.events_duplicate += outcome.events_duplicate

    def _throttle(self) -> None:
        if self.rate_limit <= 0:
            return
        self.sleep(1.0 / self.rate_limit)


# ---------------------------------------------------------------------------
# 台账 sink
# ---------------------------------------------------------------------------


class HL7LedgerSink:
    """把一帧 ER7 走完整条入站路径：解析 → 规范化 → 落台账 → 判定 → 出卡。

    **复用产品既有函数**，不另写一套判定逻辑：

    - 解析与规范化：``critval.hl7.normalize``；
    - 落台账 / 判定 / 出卡：``critval.console.inbound.LedgerInboundProcessor``
      （与 HTTP 推送通道同一条路径）。

    重复到达的帧（幂等键已存在）由 processor 挡住 —— **不重复判定、
    不重复出卡、不覆盖既有事实**。
    """

    def __init__(
        self,
        processor: Any,
        *,
        store: Any = None,
        dialect: Any = None,
    ) -> None:
        self.processor = processor
        self.store = store
        self.dialect = dialect

    def __call__(self, frame: ReplayFrame) -> FrameOutcome:
        try:
            message = parse_oru_r01(frame.raw, self.dialect)
            events, unparsed = normalize_events(message)
        except Exception as exc:  # noqa: BLE001 —— 读不懂的帧记下来，不中断整批
            return FrameOutcome(
                source=frame.source,
                index=frame.index,
                status="failed",
                detail=f"解析失败：{type(exc).__name__}: {exc}",
                error=f"{type(exc).__name__}: {exc}",
            )

        if unparsed and self.store is not None:
            for record in unparsed:
                self.store.append_audit(
                    "unparsed_event",
                    message_control_id=record.message_control_id,
                    specimen_no=record.specimen_no,
                    detail=f"{record.reason}：{record.detail}",
                )

        if not events:
            reasons = "；".join(sorted({record.reason for record in unparsed})) or "no_observation"
            return FrameOutcome(
                source=frame.source,
                index=frame.index,
                status="unparsed",
                detail=f"未识别：{reasons}",
                events=0,
            )

        new_events = 0
        duplicate_events = 0
        first: Optional[FrameOutcome] = None

        for event in events:
            outcome = self.processor(event)
            if getattr(outcome, "duplicate", False):
                duplicate_events += 1
            else:
                new_events += 1
            if first is None:
                first = FrameOutcome(
                    source=frame.source,
                    index=frame.index,
                    status="processed",
                    event_id=outcome.event_id,
                    decision=outcome.decision,
                    card_id=outcome.card_id,
                )

        assert first is not None
        first.status = "processed" if new_events else "duplicate"
        first.events = len(events)
        first.events_new = new_events
        first.events_duplicate = duplicate_events
        first.detail = (
            f"新增 {new_events} 条 · 幂等跳过 {duplicate_events} 条"
            f" · 未识别 {len(unparsed)} 条"
        )
        return first
