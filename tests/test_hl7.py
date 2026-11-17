"""HL7 入站层单测：ER7 语法解析 + 事件规范化 + 方言容错。

覆盖三类关键行为：

* **语法层**：MSH 字段编号的特殊性、组件拆分、时间戳变体、MLLP 组帧符；
* **规范化层**：最小必需字段集缺任一 → 落 ``unparsed``（**不静默丢弃**）；
* **患者标识**：``PID-3`` 只落不透明引用，台账里不留住院号明文。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from critval.hl7.er7 import (
    parse_message,
    parse_numeric,
    parse_timestamp,
    strip_frame,
)
from critval.hl7.normalize import (
    decode_frame,
    normalize_events,
    parse_oru_r01,
    patient_reference,
)
from critval.ledger.idempotency import (
    build_idempotency_key,
    event_id_for_key,
    idempotency_key_of,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
NORMAL_FRAME = FIXTURES / "脱敏样例ER7_正常.hl7"
MISSING_FRAME = FIXTURES / "脱敏样例ER7_缺字段.hl7"
CORRECTED_FRAME = FIXTURES / "脱敏样例ER7_修正结果.hl7"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# ER7 语法层
# ---------------------------------------------------------------------------
def test_MSH字段编号从2开始且MSH1是分隔符():
    message = parse_message(read(NORMAL_FRAME))
    msh = message.first("MSH")
    assert msh is not None
    assert msh.field(1) == "|"
    assert msh.field(2) == "^~\\&"
    assert msh.field(9) == "ORU^R01"
    assert msh.field(10) == "FIXMSG000001"
    assert msh.field(12) == "2.5.1"


def test_普通段字段编号从1开始():
    message = parse_message(read(NORMAL_FRAME))
    obr = message.first("OBR")
    assert obr is not None
    assert obr.field(1) == "1"
    assert obr.field(3) == "FIX-ORD-000001"
    assert obr.field(25) == "F"


def test_一条消息里多个OBX都能拿到():
    raw = "\r".join(
        [
            "MSH|^~\\&|LIS|HOSP|CRITVAL|HOSP|20260102083000||ORU^R01|MSG1|P|2.5.1",
            "OBR|1|R1|O1|718-7^血红蛋白^718-7||20260102083000",
            "OBX|1|NM|718-7^血红蛋白^718-7|1|25|g/L|115-150|H|||F|||20260102083000",
            "OBX|2|NM|2823-3^钾^2823-3|1|7.1|mmol/L|3.5-5.3|H|||F|||20260102083000",
        ]
    )
    message = parse_message(raw)
    assert len(message.find_all("OBX")) == 2
    assert message.find_all("OBX")[1].component(3, 1) == "2823-3"


def test_MLLP组帧符被剥掉():
    raw = "\x0b" + read(NORMAL_FRAME) + "\x1c\r"
    message = parse_message(raw)
    assert message.message_control_id == "FIXMSG000001"
    assert strip_frame(raw).startswith("MSH|")


def test_组件按编码字符拆分():
    message = parse_message(read(NORMAL_FRAME))
    obx = message.find_all("OBX")[0]
    assert obx.components(3) == ("718-7", "血红蛋白", "718-7")
    assert obx.component(3, 2) == "血红蛋白"
    assert obx.component(3, 9) == ""


@pytest.mark.parametrize(
    "text,expected",
    [
        ("20260102083000", datetime(2026, 1, 2, 8, 30, 0)),
        ("20260102", datetime(2026, 1, 2, 0, 0, 0)),
        ("2026010208", datetime(2026, 1, 2, 8, 0, 0)),
        ("20260102083000.500", datetime(2026, 1, 2, 8, 30, 0)),
    ],
)
def test_时间戳多种写法(text, expected):
    assert parse_timestamp(text) == expected


def test_时间戳带时区偏移():
    moment = parse_timestamp("20260102083000+0800")
    assert moment is not None
    assert moment.utcoffset().total_seconds() == 8 * 3600


def test_不认识的时间戳返回空而不是猜():
    assert parse_timestamp("") is None
    assert parse_timestamp("昨天") is None
    assert parse_timestamp("20261302") is None


def test_非纯数值不解析成数字():
    assert parse_numeric("25") == Decimal("25")
    assert parse_numeric("25.5") == Decimal("25.5")
    assert parse_numeric(">250") is None
    assert parse_numeric("阳性") is None
    assert parse_numeric("") is None


# ---------------------------------------------------------------------------
# 规范化层
# ---------------------------------------------------------------------------
def test_正常帧规范化出一条事件():
    message = parse_oru_r01(NORMAL_FRAME.read_bytes())
    events, unparsed = normalize_events(message)
    assert unparsed == []
    assert len(events) == 1
    event = events[0]
    assert event.message_control_id == "FIXMSG000001"
    assert event.specimen_no == "FIX-ORD-000001"
    assert event.item_code == "718-7"
    assert event.item_name == "血红蛋白"
    assert event.value == Decimal("25")
    assert event.unit == "g/L"
    assert event.observed_at == datetime(2026, 1, 2, 8, 30, 0)
    assert event.dept_code == "测试科室"
    assert event.obx_flag == "H"
    assert event.source_channel == "hl7"


def test_缺项目码落unparsed不静默丢弃():
    message = parse_oru_r01(MISSING_FRAME.read_bytes())
    events, unparsed = normalize_events(message)
    assert events == []
    assert len(unparsed) == 1
    assert unparsed[0].reason == "missing_item_code"
    assert "OBX-3" in unparsed[0].detail
    assert unparsed[0].message_control_id == "FIXMSG000002"


def test_缺标本号落unparsed():
    raw = "\r".join(
        [
            "MSH|^~\\&|LIS|HOSP|CRITVAL|HOSP|20260102083000||ORU^R01|MSG9|P|2.5.1",
            "OBR|1|FIX-REQ-9||718-7^血红蛋白^718-7||20260102083000",
            "OBX|1|NM|718-7^血红蛋白^718-7|1|25|g/L|115-150|H|||F|||20260102083000",
        ]
    )
    events, unparsed = normalize_events(parse_oru_r01(raw))
    assert events == []
    assert unparsed[0].reason == "missing_specimen_no"


def test_缺消息控制号整条落unparsed():
    raw = "\r".join(
        [
            "MSH|^~\\&|LIS|HOSP|CRITVAL|HOSP|20260102083000||ORU^R01||P|2.5.1",
            "OBR|1|FIX-REQ-8|FIX-ORD-8|718-7^血红蛋白^718-7||20260102083000",
            "OBX|1|NM|718-7^血红蛋白^718-7|1|25|g/L|115-150|H|||F|||20260102083000",
        ]
    )
    events, unparsed = normalize_events(parse_oru_r01(raw))
    assert events == []
    assert unparsed[0].reason == "missing_message_control_id"


def test_非数值观察值不判定也不丢():
    raw = "\r".join(
        [
            "MSH|^~\\&|LIS|HOSP|CRITVAL|HOSP|20260102083000||ORU^R01|MSG7|P|2.5.1",
            "OBR|1|FIX-REQ-7|FIX-ORD-7|718-7^血红蛋白^718-7||20260102083000",
            "OBX|1|NM|718-7^血红蛋白^718-7|1|阳性|g/L|115-150|H|||F|||20260102083000",
        ]
    )
    events, unparsed = normalize_events(parse_oru_r01(raw))
    assert events == []
    assert unparsed[0].reason == "value_not_numeric"


def test_缺观察时刻回落消息时间并留痕():
    raw = "\r".join(
        [
            "MSH|^~\\&|LIS|HOSP|CRITVAL|HOSP|20260102083000||ORU^R01|MSG6|P|2.5.1",
            "OBR|1|FIX-REQ-6|FIX-ORD-6|718-7^血红蛋白^718-7||20260102083000",
            "OBX|1|NM|718-7^血红蛋白^718-7|1|25|g/L|115-150|H|||F|||",
        ]
    )
    message = parse_oru_r01(raw)
    assert message.observations[0].observed_at == datetime(2026, 1, 2, 8, 30, 0)
    assert "回落消息时间" in message.observations[0].notes[0]
    events, unparsed = normalize_events(message)
    assert len(events) == 1
    assert unparsed == []


def test_PID3只落不透明引用不留住院号明文():
    message = parse_oru_r01(NORMAL_FRAME.read_bytes())
    assert "FIX-PAT-000001" not in message.patient_ref
    assert message.patient_ref.startswith("pt-")
    events, _ = normalize_events(message)
    assert events[0].patient_ref == message.patient_ref


def test_患者引用为空时保持为空():
    assert patient_reference("") == ""


def test_修正结果状态透传到事件():
    message = parse_oru_r01(CORRECTED_FRAME.read_bytes())
    events, unparsed = normalize_events(message)
    assert unparsed == []
    assert events[0].result_status == "C"
    assert events[0].value == Decimal("249.29")


def test_编码回退能读GB18030报文():
    payload = read(NORMAL_FRAME).encode("gb18030")
    text, encoding = decode_frame(payload)
    assert encoding == "gb18030"
    message = parse_oru_r01(payload)
    assert message.message_control_id == "FIXMSG000001"


# ---------------------------------------------------------------------------
# 幂等键
# ---------------------------------------------------------------------------
def test_幂等键由消息号项目码观察时刻三者拼成():
    key_a = build_idempotency_key("MSG1", "718-7", datetime(2026, 1, 2, 8, 30))
    key_b = build_idempotency_key("MSG1", "718-7", datetime(2026, 1, 2, 8, 30))
    key_c = build_idempotency_key("MSG2", "718-7", datetime(2026, 1, 2, 8, 30))
    key_d = build_idempotency_key("MSG1", "2823-3", datetime(2026, 1, 2, 8, 30))
    key_e = build_idempotency_key("MSG1", "718-7", datetime(2026, 1, 2, 9, 30))
    assert key_a == key_b
    assert len({key_a, key_c, key_d, key_e}) == 4


def test_同一条帧规范化两次得到同一事件ID():
    first, _ = normalize_events(parse_oru_r01(NORMAL_FRAME.read_bytes()))
    second, _ = normalize_events(parse_oru_r01(NORMAL_FRAME.read_bytes()))
    assert first[0].event_id == second[0].event_id
    assert first[0].event_id == event_id_for_key(idempotency_key_of(first[0]))
