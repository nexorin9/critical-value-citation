"""MLLP 组帧与监听（任务 id 4 · 入站主通道）。

MLLP（Minimal Lower Layer Protocol）是 HL7 v2 在 TCP 上的传输封装：**帧以
``\\x0b``（VT）起、以 ``\\x1c\\r``（FS + CR）止**。院内 LIS 的接口引擎常按
这条协议把 ``ORU^R01`` 推过来。

**帧边界语义**（本模块最要紧的一件事）：

* **粘包** —— 一次 TCP 读可能带回好几帧；一帧都不能少。
* **拆包** —— 一帧可能横跨好几次 TCP 读；读到一半**不能**当成一帧交出去。
* **空帧** —— ``\\x0b\\x1c\\r`` 是空壳，不含报文；跳过，但**留痕**（不静默丢）。
* **帧前垃圾** —— 起始符之前的字节不是报文；记下来，不静默丢。
* **半帧断链** —— 连接断开时缓冲区里还剩半个帧：**不能当成完整帧处理**，
  也不能悄悄扔掉 —— 记一条 ``incomplete_frame``，值班台能看到「今天有报文
  没读完」。

「一帧完整」的判定只有一条：**看到了结束符 ``\\x1c\\r``**。这与参考实现里
「按记录分隔符切分、取不到分隔符就继续攒」的帧边界处理是同一套语义 ——
攒够一帧才交出去，交出去的每一帧都带着自己的边界。

**与其它入站通道同构**：MLLP 帧走 ``critval.hl7.normalize`` 的同一套规范
函数，因此同一条危急值走 MLLP 与走 HTTP 推送得到**同一个事件 ID**。
"""

from __future__ import annotations

import logging
import socket
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional

from critval.hl7.normalize import (
    UnparsedRecord,
    normalize_events,
    parse_oru_r01,
)
from critval.hl7.primitives import decode_frame
from critval.models import CriticalEvent

__all__ = [
    "END_BLOCK",
    "FrameIssue",
    "MllpBindError",
    "MllpFramer",
    "MllpInboundProcessor",
    "MllpListener",
    "MllpOutcome",
    "START_BLOCK",
    "parse_bind",
]

LOGGER = logging.getLogger(__name__)

#: 帧起始符（VT，``0x0b``）。
START_BLOCK = b"\x0b"
#: 帧结束符（FS + CR，``0x1c 0x0d``）。
END_BLOCK = b"\x1c\r"

#: 单帧上限（字节）。危急值报文是几 KB；超过 1 MiB 基本是异常流量。
DEFAULT_MAX_FRAME_BYTES = 1 << 20

#: 单次 TCP 读缓冲大小。
READ_CHUNK_BYTES = 8192


class MllpBindError(ValueError):
    """监听地址写法不合法。"""


@dataclass(frozen=True)
class FrameIssue:
    """一条**没有变成事件**的帧级问题。

    帧级问题一律留痕：空帧、帧前垃圾、半帧、超长帧 —— 这些都不是「读不懂
    报文」（那类进 ``unparsed`` 记录），但同样不能悄悄消失。
    """

    kind: str  # empty_frame | garbage_prefix | incomplete_frame | oversized_frame
    detail: str
    raw: bytes = b""
    peer: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "detail": self.detail,
            "bytes": len(self.raw),
            "peer": self.peer,
        }


@dataclass
class MllpOutcome:
    """一帧的处理结论。"""

    frame_bytes: int = 0
    events: list[CriticalEvent] = field(default_factory=list)
    unparsed: list[UnparsedRecord] = field(default_factory=list)
    issues: list[FrameIssue] = field(default_factory=list)
    encoding: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


def parse_bind(spec: str) -> tuple[str, int]:
    """``主机:端口`` → ``(host, port)``。

    写法不合法当场报错 —— 监听地址写错要在启动时炸，而不是静默绑到别的端口。
    """
    text = (spec or "").strip()
    if not text:
        raise MllpBindError("MLLP 监听地址为空")
    host, sep, port_text = text.rpartition(":")
    if not sep or not host:
        raise MllpBindError(f"MLLP 监听地址应为 主机:端口 形式，收到 {spec!r}")
    port_text = port_text.strip()
    if not port_text.isdigit():
        raise MllpBindError(f"MLLP 监听地址端口不是数字：{port_text!r}")
    port = int(port_text)
    if not 1 <= port <= 65535:
        raise MllpBindError(f"MLLP 监听端口 {port} 超出 1-65535")
    return host, port


class MllpFramer:
    """把 TCP 字节流切成 MLLP 帧。

    只负责**切帧**，不认识报文内容 —— 切出来的帧交给上层按方言解析。
    """

    def __init__(self, *, max_frame_bytes: int = DEFAULT_MAX_FRAME_BYTES) -> None:
        self.max_frame_bytes = max(1024, int(max_frame_bytes))
        self._buffer = bytearray()
        self.issues: list[FrameIssue] = []

    # ---- 状态 -----------------------------------------------------------
    @property
    def pending_bytes(self) -> int:
        """缓冲区里还没凑成完整帧的字节数。"""
        return len(self._buffer)

    def flush(self) -> bytes:
        """连接断开时收尾：返回**没凑完整**的残留字节。

        残留字节**不是一帧**（缺结束符），既不能当报文处理，也不能悄悄扔掉
        —— 记一条 ``incomplete_frame`` 后返回，由调用方决定怎么留痕。
        """
        if not self._buffer:
            return b""
        pending = bytes(self._buffer)
        self._buffer.clear()
        # 起始符已经起过帧了 —— 残留的「半帧」只算报文内容，不含起止符。
        if pending.startswith(START_BLOCK):
            pending = pending[1:]
        self.issues.append(
            FrameIssue(
                kind="incomplete_frame",
                detail=(
                    f"连接结束时缓冲区仍有 {len(pending)} 字节未收到结束符 "
                    f"{END_BLOCK!r}，按半帧留痕（不作为报文处理）"
                ),
                raw=pending,
            )
        )
        return pending

    # ---- 组帧 -----------------------------------------------------------
    def feed(self, chunk: bytes) -> list[bytes]:
        """喂入一段字节，返回本次凑齐的**完整帧**列表。"""
        if not chunk:
            return []
        self._buffer.extend(chunk)
        frames: list[bytes] = []
        while True:
            start = self._buffer.find(START_BLOCK)
            if start < 0:
                # 缓冲区里没有起始符 —— 这些字节不是报文。
                garbage = bytes(self._buffer)
                self._buffer.clear()
                if garbage.strip(b"\r\n"):
                    self.issues.append(
                        FrameIssue(
                            kind="garbage_prefix",
                            detail=(
                                f"帧起始符之前收到 {len(garbage)} 字节非报文内容，已丢弃并留痕"
                            ),
                            raw=garbage,
                        )
                    )
                break
            if start > 0:
                garbage = bytes(self._buffer[:start])
                del self._buffer[:start]
                if garbage.strip(b"\r\n"):
                    self.issues.append(
                        FrameIssue(
                            kind="garbage_prefix",
                            detail=(
                                f"帧起始符之前收到 {len(garbage)} 字节非报文内容，已丢弃并留痕"
                            ),
                            raw=garbage,
                        )
                    )
            end = self._buffer.find(END_BLOCK, 1)
            if end < 0:
                if len(self._buffer) > self.max_frame_bytes:
                    self._resync_oversized()
                    continue
                break  # 半帧：等下一次 feed
            frame = bytes(self._buffer[1:end])
            del self._buffer[: end + len(END_BLOCK)]
            if len(frame) > self.max_frame_bytes:
                # 帧超长：丢弃并留痕，但**不吞掉后面的帧**。
                self.issues.append(
                    FrameIssue(
                        kind="oversized_frame",
                        detail=(
                            f"单帧超过上限 {self.max_frame_bytes} 字节"
                            f"（收到 {len(frame)} 字节），已丢弃并留痕"
                        ),
                        raw=frame,
                    )
                )
                continue
            if frame.strip(b"\r\n"):
                frames.append(frame)
            else:
                self.issues.append(
                    FrameIssue(
                        kind="empty_frame",
                        detail="收到空帧（起始符后紧跟结束符），不含报文，已跳过并留痕",
                        raw=frame,
                    )
                )
        return frames

    def _resync_oversized(self) -> None:
        """帧超长：丢弃到下一个结束符，重新对齐（不丢掉后面的帧）。"""
        end = self._buffer.find(END_BLOCK, 1)
        if end < 0:
            dropped = bytes(self._buffer)
            self._buffer.clear()
        else:
            dropped = bytes(self._buffer[: end + len(END_BLOCK)])
            del self._buffer[: end + len(END_BLOCK)]
        self.issues.append(
            FrameIssue(
                kind="oversized_frame",
                detail=(
                    f"单帧超过上限 {self.max_frame_bytes} 字节（收到 {len(dropped)} 字节），"
                    f"已丢弃并重新对齐后续帧"
                ),
                raw=dropped,
            )
        )


class MllpInboundProcessor:
    """一帧 MLLP → 规范化事件（并交给共享的处理路径落台账）。

    **复用产品既有函数**，不另写一套判定逻辑：

    * 解析与规范化：``critval.hl7.normalize``（与 HTTP 推送通道同一份实现）；
    * 落台账 / 判定 / 出卡：``critval.console.inbound.LedgerInboundProcessor``
      （与 HTTP 推送通道同一条路径）。

    因此「同一条危急值走 MLLP 与走 HTTP」得到的 ``event_id`` 完全一样。
    """

    def __init__(
        self,
        *,
        processor: Optional[Callable[[CriticalEvent], Any]] = None,
        store: Any = None,
        dialect: Any = None,
        item_matcher: Any = None,
    ) -> None:
        self.processor = processor
        self.store = store
        self.dialect = dialect
        self.item_matcher = item_matcher

    def parse(
        self, frame: bytes
    ) -> tuple[list[CriticalEvent], list[UnparsedRecord], str]:
        """帧 → （事件, 未识别记录, 编码名）。"""
        text, encoding = decode_frame(frame)
        message = parse_oru_r01(text, self.dialect)
        events, unparsed = normalize_events(message, item_matcher=self.item_matcher)
        return events, unparsed, encoding

    def __call__(self, frame: bytes, peer: str = "") -> MllpOutcome:
        outcome = MllpOutcome(frame_bytes=len(frame))
        try:
            events, unparsed, encoding = self.parse(frame)
        except Exception as exc:  # noqa: BLE001 —— 一帧读不懂不影响后面的帧
            outcome.error = f"{type(exc).__name__}: {exc}"
            if self.store is not None:
                self.store.append_audit(
                    "mllp_frame_failed",
                    actor="lis",
                    detail=f"MLLP 帧解析失败：{outcome.error}；来源 {peer or '-'}",
                )
            return outcome

        outcome.encoding = encoding
        outcome.unparsed.extend(unparsed)
        for record in unparsed:
            if self.store is not None:
                self.store.append_audit(
                    "unparsed_event",
                    message_control_id=record.message_control_id,
                    specimen_no=record.specimen_no,
                    detail=f"{record.reason}：{record.detail}",
                )

        for event in events:
            outcome.events.append(event)
            if self.processor is not None:
                self.processor(event)
        return outcome


class MllpListener:
    """MLLP 监听器：按 ``LIS_MLLP_BIND`` 绑定端口，收帧并交给处理器。

    * 每条连接一个线程（院内 LIS 的连接数是个位数，够用）；
    * 收帧用 ``MllpFramer`` —— 粘包 / 拆包都不丢帧；
    * 连接断开时 ``flush()`` 收尾，半帧留痕；
    * 帧级问题（空帧 / 垃圾字节 / 超长帧）写审计。
    """

    def __init__(
        self,
        bind: str,
        handler: Callable[[bytes, str], Any],
        *,
        store: Any = None,
        host: Optional[str] = None,
        port: Optional[int] = None,
        max_frame_bytes: int = DEFAULT_MAX_FRAME_BYTES,
        backlog: int = 16,
    ) -> None:
        if host is None or port is None:
            host, port = parse_bind(bind)
        self.bind = bind
        self.host = host
        self.port = port
        self.handler = handler
        self.store = store
        self.max_frame_bytes = max_frame_bytes
        self.backlog = backlog
        self._socket: Optional[socket.socket] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._conn_threads: list[threading.Thread] = []

    # ---- 生命周期 --------------------------------------------------------
    def bind_socket(self) -> socket.socket:
        """创建并绑定监听 socket（测试可直接调用）。"""
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((self.host, self.port))
        server.listen(self.backlog)
        server.settimeout(0.5)
        self._socket = server
        # 端口写 0 时由系统分配 —— 回填真实端口，便于测试里连接。
        self.host, self.port = server.getsockname()[:2]
        return server

    def start(self) -> threading.Thread:
        """后台线程起监听，返回线程对象。"""
        if self._socket is None:
            self.bind_socket()
        self._stop.clear()
        self._thread = threading.Thread(
            target=self.serve_forever, name="critval-mllp", daemon=True
        )
        self._thread.start()
        return self._thread

    def stop(self, timeout: float = 3.0) -> None:
        """停监听（关 socket + 置停止位）。"""
        self._stop.set()
        if self._socket is not None:
            try:
                self._socket.close()
            except OSError:  # pragma: no cover —— 关两次不会报错
                pass
            self._socket = None
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    # ---- 主循环 ----------------------------------------------------------
    def serve_forever(self) -> None:
        """接受连接并收帧（常驻）。"""
        if self._socket is None:
            self.bind_socket()
        while not self._stop.is_set():
            try:
                conn, addr = self._socket.accept()  # type: ignore[union-attr]
            except socket.timeout:
                continue
            except OSError:
                break
            peer = f"{addr[0]}:{addr[1]}"
            thread = threading.Thread(
                target=self._serve_connection, args=(conn, peer), daemon=True
            )
            thread.start()
            self._conn_threads.append(thread)
            self._conn_threads = [t for t in self._conn_threads if t.is_alive()]

    def _serve_connection(self, conn: socket.socket, peer: str) -> None:
        """一条连接：读 → 组帧 → 交给处理器。"""
        framer = MllpFramer(max_frame_bytes=self.max_frame_bytes)
        try:
            conn.settimeout(1.0)
            while not self._stop.is_set():
                try:
                    chunk = conn.recv(READ_CHUNK_BYTES)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if not chunk:
                    break
                for frame in framer.feed(chunk):
                    self._dispatch(frame, peer)
            # 断链收尾：半帧留痕，不当成完整帧。
            framer.flush()
            for issue in framer.issues:
                self._audit_issue(issue, peer)
        finally:
            try:
                conn.close()
            except OSError:  # pragma: no cover
                pass

    def _dispatch(self, frame: bytes, peer: str) -> None:
        try:
            self.handler(frame, peer)
        except Exception as exc:  # noqa: BLE001 —— 一帧处理失败不影响连接
            LOGGER.warning("MLLP 帧处理失败：%s: %s", type(exc).__name__, exc)
            if self.store is not None:
                self.store.append_audit(
                    "mllp_frame_failed",
                    actor="lis",
                    detail=f"MLLP 帧处理失败：{type(exc).__name__}: {exc}；来源 {peer}",
                )

    def _audit_issue(self, issue: FrameIssue, peer: str) -> None:
        if self.store is None:
            LOGGER.info("MLLP 帧问题：%s（%s）", issue.kind, issue.detail)
            return
        self.store.append_audit(
            f"mllp_{issue.kind}",
            actor="lis",
            detail=f"{issue.detail}；来源 {peer}",
        )


def frames_to_mllp(frame: bytes) -> bytes:
    """给一帧报文套上 MLLP 边界（测试与联调用）。"""
    return START_BLOCK + frame + END_BLOCK


def iter_mllp_frames(payload: bytes) -> Iterable[bytes]:
    """把一段含多帧的字节流拆成帧（离线联调 / 语料回放用）。"""
    framer = MllpFramer()
    for frame in framer.feed(payload):
        yield frame
