"""处置卡测试：四要素齐备、缺项不编造、患者可识别明细不外泄。"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from critval.card.builder import build_card
from critval.card.renderer import (
    CARD_ELEMENT_KEYS,
    EVENT_RENDER_FIELDS,
    PatientIdentifierLeak,
    assert_patient_free,
    build_render_payload,
    render_markdown,
)
from critval.judge.critical import judge_event
from critval.kb.loader import load_threshold_table
from critval.models import Citation, CriticalEvent, PolicyClause

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


def test_四要素齐备时状态都是已解决(table, clause):
    event = make_event()
    judgement = judge_event(event, table.rows)
    card = build_card(event, judgement, policy_clause=clause)
    assert [e.key for e in card.elements] == list(CARD_ELEMENT_KEYS)
    assert all(e.status == "resolved" for e in card.elements)
    assert card.missing_elements() == ()


def test_处置卡带出处_文件名行号版本时限与记录去向(table, clause):
    event = make_event()
    judgement = judge_event(event, table.rows)
    card = build_card(event, judgement, policy_clause=clause)
    text = render_markdown(card)

    assert FIXTURE.name in text          # 阈值表文件名
    assert "第 2 行" in text              # 可读行号
    assert "测试 v1.0" in text            # 版本号
    assert "10 分钟" in text              # 上报时限分钟数
    assert "危急值登记系统" in text        # 记录去向
    assert "示例危急值报告制度.docx" in text


def test_每条依据都能落到出处(table, clause):
    event = make_event()
    judgement = judge_event(event, table.rows)
    card = build_card(event, judgement, policy_clause=clause)
    assert len(card.citations) == 5  # 四要素出处 + 制度条款
    for citation in card.citations:
        assert citation.source_name
        assert len(citation.source_sha256) == 64


def test_缺制度条款时标缺失不编造(table):
    event = make_event()
    judgement = judge_event(event, table.rows)
    card = build_card(event, judgement, policy_clause=None)
    assert card.threshold_basis.status == "resolved"
    for element in (card.notify_target, card.deadline, card.record_destination):
        assert element.status == "missing"
        assert element.value == ""
    text = render_markdown(card)
    assert "【依据缺失】" in text
    # 不许用占位文案冒充已解决
    assert "通知对象**：检验科" not in text


def test_要素缺失时不给出处(table, clause):
    event = make_event()
    judgement = judge_event(event, table.rows)
    broken = PolicyClause(
        section_title="第三章",
        body="",
        notify_target="",
        deadline_text="",
        deadline_minutes=None,
        record_destination="",
        citation=POLICY_CITATION,
    )
    card = build_card(event, judgement, policy_clause=broken)
    assert card.notify_target.status == "missing"
    assert card.deadline.status == "missing"
    assert card.record_destination.status == "missing"


def test_渲染载荷只含白名单字段(table, clause):
    event = make_event()
    judgement = judge_event(event, table.rows)
    card = build_card(event, judgement, policy_clause=clause)
    payload = build_render_payload(card)
    assert set(payload["event"]) == set(EVENT_RENDER_FIELDS)
    assert "patient_ref" not in payload["event"]
    assert [e["key"] for e in payload["elements"]] == list(CARD_ELEMENT_KEYS)


def test_患者可识别明细不出现在渲染产物(table, clause):
    event = make_event(patient_ref="PATREF-9f2c")
    judgement = judge_event(event, table.rows)
    card = build_card(event, judgement, policy_clause=clause)
    text = render_markdown(card)
    assert "PATREF-9f2c" not in text
    assert "姓名" not in text
    assert "住院号" not in text


def test_守卫拦下患者可识别明细():
    with pytest.raises(PatientIdentifierLeak):
        assert_patient_free("患者姓名：张三")
    with pytest.raises(PatientIdentifierLeak):
        assert_patient_free("身份证号 110101199001011234")
    with pytest.raises(PatientIdentifierLeak):
        assert_patient_free("住院号 00012345")
    with pytest.raises(PatientIdentifierLeak):
        assert_patient_free("标本 PATREF-9f2c 已出结果", extra_values=["PATREF-9f2c"])


def test_渲染是确定性的(table, clause):
    event = make_event()
    judgement = judge_event(event, table.rows)
    card = build_card(event, judgement, policy_clause=clause)
    assert render_markdown(card) == render_markdown(card)


def test_卡片按事件与依据派生稳定ID(table, clause):
    event = make_event()
    judgement = judge_event(event, table.rows)
    first = build_card(event, judgement, policy_clause=clause)
    second = build_card(event, judgement, policy_clause=clause)
    assert first.card_id == second.card_id
    assert first.card_id.startswith("card-")
