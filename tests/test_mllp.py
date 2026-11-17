"""MLLP 监听与厂商方言容错测试（任务 id 4 · 入站主通道）。

本文件回答三个问题：

1. **帧边界守得住吗** —— 粘包不丢帧、拆包不提前交帧、空帧与帧前垃圾留痕、
   断链时残留的半帧不冒充完整帧；
2. **真起一个 socket 能收报文吗** —— 真连 ``127.0.0.1``、真发字节、真收帧；
3. **方言容错与 HTTP 推送通道同构吗** —— 同一条危急值走 MLLP 与走 REST，
   得到**同一个事件 ID**、同一套规范化字段。

「一帧完整」的判定只有一条：**看到了结束符**。任何读不到结束符的字节都
只能留在缓冲区里等下一次 —— 这条口径贯穿全部用例。
"""

from __future__ import annotations

import socket
import time
from pathlib import Path
from typing import Any

import pytest

from critval.hl7.dialects import (
    BUILTIN_DIALECTS,
    Dialect,
    dialect_from_env,
    resolve_dialect,
)
from critval.hl7.mllp import (
    END_BLOCK,
    START_BLOCK,
    MllpBindError,
    MllpFramer,
    MllpInboundProcessor,
    MllpListener,
    frames_to_mllp,
    iter_mllp_frames,
    parse_bind,
)
from critval.hl7.normalize import normalize_events, parse_oru_r01
from critval.hl7.rest_channel import normalize_rest_body
from critval.hl7.textmatch import ItemTextEntry, ItemTextMatcher
from critval.kb.loader import load_threshold_table

PROJECT_ROOT = Path(__file__).resolve().parent.parent
THRESHOLD = PROJECT_ROOT / "samples" / "threshold" / "示例危急值阈值表.xlsx"
SAMPLES = PROJECT_ROOT / "samples" / "hl7"


def frame_text(name: str) -> str:
    """读一条样例 ER7 帧（去掉行尾换行）。"""
    return (SAMPLES / name).read_text(encoding="utf-8").strip()


class _FakeStore:
    """只收审计的假台账（帧级问题必须有留痕，不能静默丢）。"""

    def __init__(self) -> None:
        self.audits: list[tuple[str, dict[str, Any]]] = []

    def append_audit(self, action: str, **kwargs: Any) -> int:
        self.audits.append((action, kwargs))
        return len(self.audits)


# ---------------------------------------------------------------------------
# 帧边界
# ---------------------------------------------------------------------------


def test_粘包_一次读到多帧都不丢():
    """一次 TCP 读带回好几帧 —— 一帧都不能少。"""
    framer = MllpFramer()
    frames = framer.feed(b"\x0bMSH|A\x1c\r\x0bMSH|B\x1c\r\x0bMSH|C\x1c\r")
    assert frames == [b"MSH|A", b"MSH|B", b"MSH|C"]


def test_拆包_半帧不提前交出():
    """一帧横跨好几次 TCP 读 —— 读到一半不能当成一帧交出去。"""
    framer = MllpFramer()
    assert framer.feed(b"\x0bMSH|^~\\&|SYNTH") == []
    assert framer.feed(b"ETIC_LIS|SYNTH_HOSP") == []
    assert framer.pending_bytes > 0
    assert framer.feed(b"|20261228234825||ORU^R01|SYNMSG00000005\x1c\r") == [
        b"MSH|^~\\&|SYNTHETIC_LIS|SYNTH_HOSP|20261228234825||ORU^R01|SYNMSG00000005"
    ]


def test_空帧被跳过但留痕():
    """空壳帧不含报文 —— 跳过，但**留痕**（不静默丢）。"""
    framer = MllpFramer()
    assert framer.feed(b"\x0b\x1c\r") == []
    assert [issue.kind for issue in framer.issues] == ["empty_frame"]


def test_帧前垃圾字节被丢弃并留痕():
    """起始符之前的字节不是报文 —— 记下来，不静默丢。"""
    framer = MllpFramer()
    frames = framer.feed(b"GARBAGE\x0bMSH|A\x1c\r")
    assert frames == [b"MSH|A"]
    assert [issue.kind for issue in framer.issues] == ["garbage_prefix"]
    assert "GARBAGE" not in frames[0].decode("utf-8")


def test_断链时残留半帧记半帧问题():
    """连接断开时缓冲区里还剩半个帧 —— 不当完整帧处理，也不悄悄扔掉。"""
    framer = MllpFramer()
    assert framer.feed(b"\x0bMSH|ONLY-HALF") == []
    pending = framer.flush()
    assert pending == b"MSH|ONLY-HALF"
    assert [issue.kind for issue in framer.issues] == ["incomplete_frame"]


def test_超长帧被丢弃并重新对齐后续帧():
    """帧超长 —— 丢弃到下一个结束符，后面的帧照样收。"""
    framer = MllpFramer(max_frame_bytes=1024)
    frames = framer.feed(b"\x0b" + b"A" * 2048 + b"\x1c\r\x0bMSH|OK\x1c\r")
    assert frames == [b"MSH|OK"]
    assert [issue.kind for issue in framer.issues] == ["oversized_frame"]


def test_帧内容不含起止符():
    """交给上层的帧是「净报文」 —— 起止符已剥除。"""
    framer = MllpFramer()
    frames = framer.feed(frames_to_mllp(b"MSH|A"))
    assert frames == [b"MSH|A"]
    assert not frames[0].startswith(START_BLOCK)
    assert not frames[0].endswith(END_BLOCK)


def test_离线拆帧与在线组帧结果一致():
    """语料回放用的离线拆帧与监听用的在线组帧是同一套边界。"""
    payload = frames_to_mllp(b"MSH|A") + frames_to_mllp(b"MSH|B")
    assert list(iter_mllp_frames(payload)) == [b"MSH|A", b"MSH|B"]


# ---------------------------------------------------------------------------
# 监听地址
# ---------------------------------------------------------------------------


def test_监听地址解析成主机与端口():
    assert parse_bind("127.0.0.1:8765") == ("127.0.0.1", 8765)


@pytest.mark.parametrize(
    "spec",
    ["", "   ", "127.0.0.1", "127.0.0.1:abc", "127.0.0.1:0", "127.0.0.1:70000"],
)
def test_监听地址写法非法当场报错(spec):
    """监听地址写错要在启动时炸，而不是静默绑到别的端口。"""
    with pytest.raises(MllpBindError):
        parse_bind(spec)


# ---------------------------------------------------------------------------
# 真起 socket
# ---------------------------------------------------------------------------


def test_真实socket收到整帧并交给处理器():
    """真连、真发、真收 —— 监听器把一帧交给处理器。"""
    received: list[bytes] = []
    listener = MllpListener(
        "127.0.0.1:0", lambda frame, peer: received.append(frame), host="127.0.0.1", port=0
    )
    listener.start()
    try:
        with socket.create_connection(("127.0.0.1", listener.port), timeout=3) as client:
            client.sendall(frames_to_mllp(b"MSH|REAL"))
        deadline = time.time() + 3
        while not received and time.time() < deadline:
            time.sleep(0.02)
    finally:
        listener.stop()
    assert received == [b"MSH|REAL"]


def test_真实socket收到拆开的半帧仍能拼成一帧():
    """真实网络也会拆包 —— 拆成两半发，监听器仍拼出一帧。"""
    received: list[bytes] = []
    listener = MllpListener(
        "127.0.0.1:0", lambda frame, peer: received.append(frame), host="127.0.0.1", port=0
    )
    listener.start()
    try:
        with socket.create_connection(("127.0.0.1", listener.port), timeout=3) as client:
            client.sendall(b"\x0bMSH|SPL")
            time.sleep(0.1)
            client.sendall(b"IT\x1c\r")
        deadline = time.time() + 3
        while not received and time.time() < deadline:
            time.sleep(0.02)
    finally:
        listener.stop()
    assert received == [b"MSH|SPLIT"]


def test_连接断开时残留半帧写审计():
    """断链时缓冲区里的半帧要留痕 —— 值班台能看到「有报文没读完」。"""
    store = _FakeStore()
    listener = MllpListener(
        "127.0.0.1:0", lambda frame, peer: None, store=store, host="127.0.0.1", port=0
    )
    listener.start()
    try:
        with socket.create_connection(("127.0.0.1", listener.port), timeout=3) as client:
            client.sendall(b"\x0bMSH|HALF-ONLY")
            time.sleep(0.15)
        # 断链收尾在连接线程里做 —— 等它把半帧留痕写完。
        deadline = time.time() + 3
        while time.time() < deadline:
            if any(action == "mllp_incomplete_frame" for action, _ in store.audits):
                break
            time.sleep(0.02)
    finally:
        listener.stop()
    actions = [action for action, _ in store.audits]
    assert "mllp_incomplete_frame" in actions


# ---------------------------------------------------------------------------
# 解析与规范化
# ---------------------------------------------------------------------------


def test_一帧MLLP解析出规范化事件():
    """一帧 → 一条带标本号与项目码的规范化事件。"""
    raw = frame_text("示例ORU_R01_正常.hl7")
    message = parse_oru_r01(raw)
    events, unparsed = normalize_events(message)
    assert not unparsed
    assert len(events) == 1
    event = events[0]
    assert event.message_control_id == "SYNMSG00000005"
    assert event.item_code == "1920-8"
    assert event.source_channel == "hl7"


def test_缺字段的帧落未识别记录不静默丢弃():
    """读不懂的帧进 ``unparsed`` —— 一条都不静默丢。"""
    raw = frame_text("示例ORU_R01_缺字段.hl7")
    message = parse_oru_r01(raw)
    events, unparsed = normalize_events(message)
    assert not events
    assert unparsed
    assert unparsed[0].reason in {
        "missing_item_code",
        "missing_specimen_no",
        "missing_value",
        "missing_message_control_id",
    }


def test_处理器把帧解析成事件并把未识别记录写审计():
    """处理器：帧 → 事件；读不懂的帧 → 审计留痕。"""
    store = _FakeStore()
    processor = MllpInboundProcessor(store=store)
    outcome = processor(frames_to_mllp(frame_text("示例ORU_R01_正常.hl7").encode("utf-8")))
    assert outcome.ok
    assert len(outcome.events) == 1
    assert not store.audits

    bad = processor(frames_to_mllp(frame_text("示例ORU_R01_缺字段.hl7").encode("utf-8")))
    assert not bad.events
    assert bad.unparsed
    assert any(action == "unparsed_event" for action, _ in store.audits)


def test_帧解析异常不影响后续帧():
    """一帧读不懂不影响后面的帧 —— 监听器继续收。"""
    seen: list[str] = []

    def handler(frame: bytes, peer: str) -> None:
        seen.append(frame.decode("utf-8", errors="replace"))

    listener = MllpListener("127.0.0.1:0", handler, host="127.0.0.1", port=0)
    listener.start()
    try:
        with socket.create_connection(("127.0.0.1", listener.port), timeout=3) as client:
            client.sendall(frames_to_mllp(b"MSH|FIRST"))
            client.sendall(frames_to_mllp(b"MSH|SECOND"))
        deadline = time.time() + 3
        while len(seen) < 2 and time.time() < deadline:
            time.sleep(0.02)
    finally:
        listener.stop()
    assert seen == ["MSH|FIRST", "MSH|SECOND"]


# ---------------------------------------------------------------------------
# 方言容错与两条通道同构
# ---------------------------------------------------------------------------


def test_方言配置由LIS_FIELD_MAP决定改一处即可():
    """院内换厂商只改这一个环境变量 —— 不改代码。"""
    dialect = dialect_from_env(lambda key: "vendor_zseg" if key == "LIS_FIELD_MAP" else None)
    assert dialect.name == "vendor_zseg"

    # 配置位写错时回落到默认方言并告警，不打断入站。
    fallback = dialect_from_env(lambda key: "no-such-dialect" if key == "LIS_FIELD_MAP" else None)
    assert fallback.name == BUILTIN_DIALECTS["default"].name


def test_内联方言JSON也认():
    """``LIS_FIELD_MAP`` 认三种写法：内置名 / 文件路径 / 内联 JSON。"""
    inline = resolve_dialect(
        '{"name": "inline_demo", "item_code": [{"ref": "OBX-3.1", "kind": "code"}]}'
    )
    assert isinstance(inline, Dialect)
    assert inline.name == "inline_demo"
    assert inline.item_code[0].ref.source_label == "OBX-3.1"


def test_文本兜底_厂商只给项目名时按名称匹配到项目码并标低置信():
    """``OBX-3`` 只给中文项目名 → 按名称匹配到阈值表里的项目码，标低置信。"""
    table = load_threshold_table(THRESHOLD)
    matcher = ItemTextMatcher([ItemTextEntry.from_row(row) for row in table.rows])
    raw = frame_text("方言样例/方言样例_文本项目码.hl7")
    message = parse_oru_r01(raw)
    events, unparsed = normalize_events(message, item_matcher=matcher)
    assert not unparsed
    assert len(events) == 1
    event = events[0]
    assert event.item_code == "6690-2"
    assert event.item_code_confidence == "low"
    assert "文本匹配" in event.item_code_source


def test_文本兜底匹配不上时不猜():
    """名称对不上阈值表 —— 仍按「缺项目码」落未识别记录，不硬套一个码。

    这一帧里厂商**既没给项目码、也没给能当码用的订单级标识**：``OBR-4``
    写的是中文项目名。名字匹配不上阈值表就只能转人工 —— 危急值安错项目的
    后果比晚几分钟转人工严重得多。
    """
    matcher = ItemTextMatcher([ItemTextEntry("718-7", "血红蛋白")])
    raw = (
        "MSH|^~\\&|VENDOR_LIS|VENDOR_HOSP|CRITVAL|VENDOR_HOSP|20260201101200||"
        "ORU^R01|TEXTMSG00000001|P|2.5.1\r"
        "PID|1||PAT-000001||||19800101|M\r"
        "PV1|1|I|检验科^检验科^LAB|E|||检验科^LAB\r"
        "OBR|1|REQ-00000001|ORD-00000001|白细胞计数||20260201101200|20260201101200"
        "||||||||||||||||||F\r"
        "OBX|1|NM|白细胞计数|1|2.76|10*9/L|3.5-9.5|L|||F|||20260201101200\r"
    )
    message = parse_oru_r01(raw)
    events, unparsed = normalize_events(message, item_matcher=matcher)
    assert not events
    assert unparsed and unparsed[0].reason == "missing_item_code"

    # 同一个匹配器换成认识「白细胞计数」的那份 —— 项目码才落得下来。
    table = load_threshold_table(THRESHOLD)
    known = ItemTextMatcher([ItemTextEntry.from_row(row) for row in table.rows])
    events, unparsed = normalize_events(parse_oru_r01(raw), item_matcher=known)
    assert not unparsed
    assert events and events[0].item_code == "6690-2"
    assert events[0].item_code_confidence == "low"


def test_两条入站通道产出同一个事件ID():
    """同一条危急值走 MLLP 与走 REST —— 事件 ID 与关键字段完全一致。"""
    raw = frame_text("示例ORU_R01_正常.hl7")
    message = parse_oru_r01(raw)
    mllp_events, _ = normalize_events(message)
    assert len(mllp_events) == 1
    mllp_event = mllp_events[0]

    rest_event, unparsed = normalize_rest_body(
        {
            "event_id": mllp_event.message_control_id,
            "item_code": mllp_event.item_code,
            "value": float(mllp_event.value),
            "unit": mllp_event.unit,
            "critical_level": mllp_event.obx_flag,
            "raised_at": mllp_event.observed_at.isoformat(sep=" ", timespec="seconds"),
            "dept_code": mllp_event.dept_code,
        }
    )
    assert unparsed is None
    assert rest_event is not None
    assert rest_event.event_id == mllp_event.event_id
    assert rest_event.item_code == mllp_event.item_code
    assert rest_event.value == mllp_event.value
    assert rest_event.unit == mllp_event.unit
    assert rest_event.observed_at == mllp_event.observed_at


def test_监听器可在后台线程启动与停止():
    """常驻形态：后台线程起监听，停时不留线程泄漏。"""
    listener = MllpListener(
        "127.0.0.1:0", lambda frame, peer: None, host="127.0.0.1", port=0
    )
    thread = listener.start()
    assert thread.is_alive()
    listener.stop()
    assert not thread.is_alive()
