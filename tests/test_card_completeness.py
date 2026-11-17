"""处置卡四要素：缺项标注、降级来源标注、字段白名单过滤。

产品底线是**诚实**：依据没查到就说没查到（缺项），依据来自缓存 / 历史版本 /
降级召回就说清楚（降级），患者可识别明细不许出现在任何渲染产物里。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from critval.card.builder import build_card
from critval.card.provenance import (
    ORIGIN_CACHE,
    ORIGIN_FALLBACK,
    ORIGIN_HISTORICAL,
    ORIGIN_UNAVAILABLE,
    REASON_POLICY_FIELD_EMPTY,
    REASON_POLICY_NOT_RECALLED,
    REASON_RESOLVED,
    REASON_RETRIEVAL_FALLBACK,
    REASON_ROSTER_FROM_CACHE,
    REASON_ROSTER_UNAVAILABLE,
    REASON_THRESHOLD_HISTORICAL,
    REASON_THRESHOLD_ROW_NOT_MATCHED,
    retrieval_provenance,
    roster_provenance,
    threshold_provenance,
)
from critval.card.renderer import (
    CARD_ELEMENT_KEYS,
    CARD_PAYLOAD_WHITELIST,
    EVENT_RENDER_FIELDS,
    PUSH_PAYLOAD_WHITELIST,
    PayloadFieldLeak,
    PatientIdentifierLeak,
    assert_payload_patient_free,
    assert_payload_whitelisted,
    build_push_payload,
    build_render_payload,
    render_markdown,
)
from critval.judge.critical import judge_event
from critval.kb.loader import load_threshold_table
from critval.models import CardProvenance, Citation, CriticalEvent, PolicyClause

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "脱敏样例阈值表.xlsx"

POLICY_CITATION = Citation(
    source_name="示例危急值报告制度.docx",
    source_sha256="a" * 64,
    kind="policy_clause",
    paragraph_no=19,
    section_path=("第三章 报告流程与通知时限",),
    line_no=19,
    file_version="示例 v1.0",
)


@pytest.fixture(scope="module")
def table():
    return load_threshold_table(FIXTURE)


@pytest.fixture()
def clause() -> PolicyClause:
    return PolicyClause(
        section_title="第三章 报告流程与通知时限",
        body="第七条 检验科发现危急值后应当班复核……",
        notify_target="检验科值班技师与检验科组长",
        deadline_text="10 分钟内",
        deadline_minutes=10,
        record_destination="危急值登记系统，并同步记入检验科危急值台账",
        citation=POLICY_CITATION,
        clause_no="第七条",
    )


def make_event(**kwargs) -> CriticalEvent:
    payload = {
        "event_id": "evt-0001",
        "message_control_id": "MSG0001",
        "specimen_no": "SPEC-0001",
        "item_code": "718-7",
        "item_name": "血红蛋白",
        "value": Decimal("25"),
        "unit": "g/L",
        "observed_at": datetime(2026, 1, 2, 8, 30),
        "dept_code": "ICU",
        "patient_ref": "PATREF-9f2c",
    }
    payload.update(kwargs)
    return CriticalEvent(**payload)


# —— 一、四要素齐备 ——


def test_四要素齐备时全部已解决且无降级标注(table, clause):
    event = make_event()
    judgement = judge_event(event, table.rows)
    card = build_card(event, judgement, policy_clause=clause, duty_person="李技师")
    payload = build_render_payload(card)

    assert payload["element_status"] == {key: "resolved" for key in CARD_ELEMENT_KEYS}
    assert all(e["reason"] == REASON_RESOLVED for e in payload["elements"])
    assert payload["missing_elements"] == []
    assert payload["degraded_elements"] == []
    assert card.missing_elements() == ()
    assert card.degraded_elements() == ()

    text = render_markdown(card)
    assert "【依据缺失】" not in text
    assert "【降级依据】" not in text
    # 全部实时来源时，卡片头部不挂「依据来源标注」块
    assert "依据来源标注" not in text


# —— 二、缺一项 ——


def test_制度未写明通知对象时该要素标缺失且写明原因(table, clause):
    broken = PolicyClause(
        section_title=clause.section_title,
        body="",
        notify_target="",
        deadline_text=clause.deadline_text,
        deadline_minutes=clause.deadline_minutes,
        record_destination=clause.record_destination,
        citation=POLICY_CITATION,
    )
    event = make_event()
    judgement = judge_event(event, table.rows)
    card = build_card(event, judgement, policy_clause=broken)
    payload = build_render_payload(card)

    notify = next(e for e in payload["elements"] if e["key"] == "notify_target")
    assert notify["status"] == "missing"
    assert notify["reason"] == REASON_POLICY_FIELD_EMPTY
    assert notify["value"] == ""
    assert notify["note"]
    # 其余三项不受影响
    assert payload["element_status"]["threshold_basis"] == "resolved"
    assert payload["element_status"]["deadline"] == "resolved"
    assert payload["element_status"]["record_destination"] == "resolved"

    text = render_markdown(card)
    assert "【依据缺失】" in text
    assert card.missing_elements() == ("通知对象",)


def test_未召回制度条款时三项标缺失不编造(table):
    event = make_event()
    judgement = judge_event(event, table.rows)
    card = build_card(event, judgement, policy_clause=None)
    payload = build_render_payload(card)

    for key in ("notify_target", "deadline", "record_destination"):
        element = next(e for e in payload["elements"] if e["key"] == key)
        assert element["status"] == "missing"
        assert element["reason"] == REASON_POLICY_NOT_RECALLED
        assert element["value"] == ""
    assert payload["element_status"]["threshold_basis"] == "resolved"

    text = render_markdown(card)
    assert text.count("【依据缺失】") == 3
    # 绝不用占位文案冒充已解决
    assert "通知对象**：检验科" not in text
    assert "上报时限**：10 分钟" not in text


# —— 三、全缺 ——


def test_四要素全缺时逐项写明缺失原因(table):
    event = make_event(item_code="0000-0", item_name="未收录项目")
    judgement = judge_event(event, table.rows)
    card = build_card(
        event,
        judgement,
        policy_clause=None,
        provenance=CardProvenance(
            duty_roster=roster_provenance(ORIGIN_UNAVAILABLE, detail="排班系统连接超时")
        ),
    )
    payload = build_render_payload(card)

    assert payload["element_status"] == {key: "missing" for key in CARD_ELEMENT_KEYS}
    assert set(payload["missing_elements"]) == {"阈值依据行", "通知对象", "上报时限", "记录去向"}
    assert payload["degraded_elements"] == []

    reasons = {e["key"]: e["reason"] for e in payload["elements"]}
    assert reasons["threshold_basis"] == REASON_THRESHOLD_ROW_NOT_MATCHED
    # 通知对象：排班系统不可用是最直接的阻塞点，排在制度未召回之前
    assert reasons["notify_target"] == REASON_ROSTER_UNAVAILABLE
    assert reasons["deadline"] == REASON_POLICY_NOT_RECALLED
    assert reasons["record_destination"] == REASON_POLICY_NOT_RECALLED

    text = render_markdown(card)
    assert text.count("【依据缺失】") == 4
    assert "排班系统不可用" in text


# —— 四、降级来源标注 ——


def test_当班名单来自缓存时头部标注来源与时间戳(table, clause):
    event = make_event()
    judgement = judge_event(event, table.rows)
    card = build_card(
        event,
        judgement,
        policy_clause=clause,
        duty_person="李技师",
        provenance=CardProvenance(
            duty_roster=roster_provenance(ORIGIN_CACHE, as_of="2026-01-02 08:30")
        ),
    )
    payload = build_render_payload(card)

    notify = next(e for e in payload["elements"] if e["key"] == "notify_target")
    assert notify["status"] == "degraded"
    assert notify["reason"] == REASON_ROSTER_FROM_CACHE
    assert "缓存" in notify["note"]
    assert "2026-01-02 08:30" in notify["note"]
    # 值还在 —— 降级不是缺项
    assert "李技师" in notify["value"]

    text = render_markdown(card)
    assert "依据来源标注" in text
    assert "当班名单来源：缓存" in text
    assert "2026-01-02 08:30" in text
    assert "【降级依据】" in text
    # 降级不算缺项
    assert card.missing_elements() == ()
    assert card.degraded_elements() == ("通知对象",)


def test_阈值行来自历史版本时标注版本与生效期(table, clause):
    event = make_event()
    judgement = judge_event(event, table.rows)
    card = build_card(
        event,
        judgement,
        policy_clause=clause,
        provenance=CardProvenance(
            threshold_table=threshold_provenance(ORIGIN_HISTORICAL, detail="按事件观察时刻取到的历史版本")
        ),
    )
    payload = build_render_payload(card)

    basis = next(e for e in payload["elements"] if e["key"] == "threshold_basis")
    assert basis["status"] == "degraded"
    assert basis["reason"] == REASON_THRESHOLD_HISTORICAL
    assert "历史版本" in basis["note"]
    assert "测试 v1.0" in basis["note"]          # 版本
    assert "2026-01-01" in basis["note"]          # 生效期

    text = render_markdown(card)
    assert "阈值行来源：历史版本" in text


def test_制度召回走降级层级时标注层级号(table, clause):
    event = make_event()
    judgement = judge_event(event, table.rows)
    card = build_card(
        event,
        judgement,
        policy_clause=clause,
        provenance=CardProvenance(
            policy_retrieval=retrieval_provenance(level=2, detail="章节级兜底")
        ),
    )
    payload = build_render_payload(card)

    for key in ("notify_target", "deadline", "record_destination"):
        element = next(e for e in payload["elements"] if e["key"] == key)
        assert element["status"] == "degraded"
        assert element["reason"] == REASON_RETRIEVAL_FALLBACK
        assert "第 2 级降级" in element["note"]

    text = render_markdown(card)
    assert "制度召回：第 2 级降级" in text
    assert "章节级兜底" in text
    assert card.degraded_elements() == ("通知对象", "上报时限", "记录去向")


def test_兜底当班人也标降级并说明来源(table, clause):
    event = make_event()
    judgement = judge_event(event, table.rows)
    card = build_card(
        event,
        judgement,
        policy_clause=clause,
        duty_person="科室值班长",
        provenance=CardProvenance(
            duty_roster=roster_provenance(ORIGIN_FALLBACK, detail="科室值班长")
        ),
    )
    notify = card.notify_target
    assert notify.status == "degraded"
    assert "兜底人选" in notify.note
    assert "科室值班长" in notify.note


# —— 五、字段白名单过滤 ——


def test_载荷只含白名单字段(table, clause):
    event = make_event()
    judgement = judge_event(event, table.rows)
    card = build_card(event, judgement, policy_clause=clause)
    payload = build_render_payload(card)

    assert set(payload) <= CARD_PAYLOAD_WHITELIST[""]
    assert set(payload["event"]) == set(EVENT_RENDER_FIELDS)
    assert set(payload["elements"][0]) <= CARD_PAYLOAD_WHITELIST["elements"]
    for item in payload["provenance"]:
        assert set(item) <= CARD_PAYLOAD_WHITELIST["provenance"]
    # 断言不抛错即通过（白名单闸门本身）
    assert_payload_whitelisted(payload)


def test_患者标识层字段不进载荷也不进渲染产物(table, clause):
    event = make_event(patient_ref="PATREF-9f2c")
    judgement = judge_event(event, table.rows)
    card = build_card(event, judgement, policy_clause=clause)
    payload = build_render_payload(card)

    assert "patient_ref" not in payload["event"]
    text = render_markdown(card)
    assert "PATREF-9f2c" not in text
    assert "姓名" not in text
    assert "身份证" not in text
    assert "住院号" not in text


def test_PID3明细不出现在任何渲染产物(table, clause):
    """PID-3 只进事件标识层，不得泄漏到卡片或推送载荷。"""
    event = make_event(patient_ref="PID-3|住院流水号 00012345")
    judgement = judge_event(event, table.rows)
    card = build_card(event, judgement, policy_clause=clause)
    push = build_push_payload(card, confirm_url="http://127.0.0.1:8848/events/evt-0001/ack")

    assert "00012345" not in render_markdown(card)
    assert "00012345" not in push["markdown"]["content"]
    assert_payload_patient_free(push)


def test_白名单外字段被拦下():
    with pytest.raises(PayloadFieldLeak):
        assert_payload_whitelisted({"card_id": "card-1", "secret": "x"})
    with pytest.raises(PatientIdentifierLeak):
        assert_payload_whitelisted({"event": {"item_code": "718-7", "patient_name": "张三"}})
    with pytest.raises(PayloadFieldLeak):
        assert_payload_whitelisted({"elements": [{"key": "k", "raw_pid3": "x"}]})


def test_患者标识层字段名被拦下():
    with pytest.raises(PatientIdentifierLeak):
        assert_payload_whitelisted({"event": {"patient_ref": "PATREF-9f2c"}})
    with pytest.raises(PatientIdentifierLeak):
        assert_payload_patient_free({"event": {"note": "住院号 00012345"}})


def test_推送载荷只含白名单字段(table, clause):
    event = make_event()
    judgement = judge_event(event, table.rows)
    card = build_card(event, judgement, policy_clause=clause)
    push = build_push_payload(card, confirm_url="http://127.0.0.1:8848/events/evt-0001/ack")

    assert set(push) == PUSH_PAYLOAD_WHITELIST[""]
    assert set(push["markdown"]) == PUSH_PAYLOAD_WHITELIST["markdown"]
    assert push["msgtype"] == "markdown"
    assert "危急值处置卡" in push["markdown"]["content"]


# —— 六、状态字段可被机器解析 ——


def test_四要素状态字段可被机器解析(table, clause):
    event = make_event()
    judgement = judge_event(event, table.rows)
    card = build_card(event, judgement, policy_clause=clause)
    payload = build_render_payload(card)

    assert set(payload["element_status"]) == set(CARD_ELEMENT_KEYS)
    for element in payload["elements"]:
        assert element["status"] in ("resolved", "missing", "degraded")
        assert element["reason"]  # 原因码非空
        assert element["label"]
