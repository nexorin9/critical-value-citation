"""危急判定的边界用例、口径分歧与修正结果取代链（任务 id 9）。

判定权威永远是院内阈值表。本文件盯住四件事：

* **边界** —— 阈值恰等于边界、危急方向缺失、单位未知、参考区间缺失；
* **口径分歧** —— LIS 的 ``OBX-8`` 与阈值表对不上时，以阈值表为准并留痕；
* **互斥值** —— 同标本同项目同一时刻出现两个互斥结果，不自动判定、不出卡；
* **取代链** —— 修正结果（``OBR-25=C``）取代旧事件，旧事件置终态且不可回退。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from critval.console.inbound import LedgerInboundProcessor
from critval.judge.ambiguity import (
    AMBIGUITY_KIND_DIRECTION,
    AMBIGUITY_KIND_VALUE,
    check_ambiguity,
    detect_ambiguous_observations,
)
from critval.judge.critical import (
    DECISION_AMBIGUOUS_OBSERVATION,
    DECISION_CRITICAL,
    DECISION_NO_THRESHOLD,
    DECISION_NOT_CRITICAL,
    DECISION_UNIT_UNKNOWN,
    judge_event,
    judge_value,
)
from critval.judge.crosscheck import (
    DIRECTION_OVER_CALL,
    DIRECTION_UNDER_CALL,
    classify_flag,
    crosscheck_flag,
    naive_flag_decision,
)
from critval.judge.supersede import SUPERSEDE_STATUS, apply_supersede
from critval.kb.loader import load_threshold_table
from critval.ledger.store import LedgerStore, TerminalStateError
from critval.models import CriticalEvent

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "脱敏样例阈值表.xlsx"

OBSERVED_AT = datetime(2026, 1, 2, 8, 30)


@pytest.fixture(scope="module")
def table():
    return load_threshold_table(FIXTURE)


def make_event(**kwargs) -> CriticalEvent:
    payload = {
        "event_id": "evt-0001",
        "message_control_id": "MSG0001",
        "specimen_no": "SPEC-0001",
        "item_code": "718-7",
        "item_name": "血红蛋白",
        "value": Decimal("25"),
        "unit": "g/L",
        "observed_at": OBSERVED_AT,
        "dept_code": "ICU",
    }
    payload.update(kwargs)
    if isinstance(payload["value"], str):
        payload["value"] = Decimal(payload["value"])
    return CriticalEvent(**payload)


# ---------------------------------------------------------------------------
# 边界用例
# ---------------------------------------------------------------------------


def test_阈值恰等于高危急值不判危急(table):
    """裸数字阈值是**严格不等** —— 恰好 200 不算危急。"""
    judgement = judge_event(make_event(value="200"), table.rows)
    assert judgement.decision == DECISION_NOT_CRITICAL
    assert judgement.threshold_row is not None
    assert judgement.threshold_row.citation.line_no == 2


def test_阈值恰等于低危急值不判危急(table):
    judgement = judge_event(make_event(value="30"), table.rows)
    assert judgement.decision == DECISION_NOT_CRITICAL


def test_源表写含等号时等于边界算危急(table):
    """源表写 ``>=200`` 时，取等号也算危急 —— 语义跟着表走，不跟着习惯走。"""
    row = table.first("718-7")
    assert row is not None
    closed = replace(row, high_inclusive=True, low_inclusive=True)
    judgement = judge_value(closed, Decimal("200"), "g/L")
    assert judgement.decision == DECISION_CRITICAL
    assert "或等于" in judgement.reason


def test_危急方向缺失不判定转人工(table):
    """阈值行没有给出危急阈值 —— 不判定，不能当成「不危急」放过。"""
    judgement = judge_event(make_event(item_code="8888-8"), table.rows)
    assert judgement.decision == DECISION_NO_THRESHOLD
    assert judgement.judged is False


def test_单位未知不判定转人工(table):
    """上报单位查不到换算关系 —— 不判定、转人工，绝不按经验猜系数。"""
    judgement = judge_event(
        make_event(item_code="9999-9", value="1.5", unit="g/L"), table.rows
    )
    assert judgement.decision == DECISION_UNIT_UNKNOWN
    assert judgement.judged is False
    assert "不做判定" in judgement.reason


def test_参考区间缺失不影响判定结论(table):
    """参考区间是补充信息 —— 它缺失不能动摇判定结论（判定权威是阈值表）。"""
    row = table.first("718-7")
    assert row is not None

    empty = replace(row, reference_range_raw="")
    judgement = judge_value(empty, Decimal("25"), "g/L")
    assert judgement.decision == DECISION_CRITICAL
    assert judgement.reference_band is None

    unparsable = replace(row, reference_range_raw="见报告单")
    judgement = judge_value(unparsable, Decimal("25"), "g/L")
    assert judgement.decision == DECISION_CRITICAL
    assert judgement.reference_band is None
    assert judgement.reference_band_confidence == "missing"
    assert "无法解析" in judgement.reference_band_note
    assert "参考区间不可用" in judgement.reason


# ---------------------------------------------------------------------------
# OBX-8 与阈值表的口径分歧
# ---------------------------------------------------------------------------


def test_异常标志分类覆盖常见写法():
    assert classify_flag("N") == "normal"
    assert classify_flag("H") == "above_normal"
    assert classify_flag("L") == "below_normal"
    assert classify_flag("HH") == "panic_high"
    assert classify_flag("LL") == "panic_low"
    assert classify_flag("") == "missing"


def test_朴素基线只看异常标志():
    assert naive_flag_decision("H") == "critical"
    assert naive_flag_decision("LL") == "critical"
    assert naive_flag_decision("N") == "not_critical"
    assert naive_flag_decision("") == "not_critical"


def test_标志与阈值表一致时没有分歧(table):
    judgement = judge_event(make_event(value="25", obx_flag="LL"), table.rows)
    assert judgement.decision == DECISION_CRITICAL
    assert judgement.obx_flag_disagreement is False
    assert judgement.flag_disagreement_note == ""


def test_标志说异常但阈值表说不危急时以阈值表为准并记分歧(table):
    """LIS 标了 ``H``，但阈值表说不危急 —— 按阈值表走，分歧留痕。"""
    judgement = judge_event(make_event(value="160", obx_flag="H"), table.rows)
    assert judgement.decision == DECISION_NOT_CRITICAL
    assert judgement.obx_flag_disagreement is True
    assert "以院内阈值表为准" in judgement.flag_disagreement_note
    assert judgement.flag_disagreement_note in judgement.reason


def test_标志说正常但阈值表说危急时记漏报方向分歧(table):
    """真危急但 LIS 标了 ``N`` —— 分歧方向是「基线漏报」。"""
    judgement = judge_event(make_event(value="25", obx_flag="N"), table.rows)
    assert judgement.decision == DECISION_CRITICAL
    assert judgement.obx_flag_disagreement is True
    found = crosscheck_flag("N", judgement)
    assert found is not None
    assert found.direction == DIRECTION_UNDER_CALL


def test_标志多报方向判定正确(table):
    judgement = judge_event(make_event(value="160", obx_flag="H"), table.rows)
    found = crosscheck_flag("H", judgement)
    assert found is not None
    assert found.direction == DIRECTION_OVER_CALL
    assert found.is_over_call is True


def test_判定没有结论时不比对标志(table):
    """转人工的结论没有可比对象 —— 不硬凑一条分歧记录。"""
    judgement = judge_event(make_event(item_code="8888-8", obx_flag="H"), table.rows)
    assert judgement.judged is False
    assert crosscheck_flag("H", judgement) is None
    assert judgement.obx_flag_disagreement is False


# ---------------------------------------------------------------------------
# 互斥值
# ---------------------------------------------------------------------------


def test_同标本同项目同时刻两个互斥值被检出(table):
    events = [
        make_event(event_id="evt-a", message_control_id="MC1", value="25"),
        make_event(event_id="evt-b", message_control_id="MC2", value="62"),
    ]
    findings = detect_ambiguous_observations(events, table.rows)
    assert len(findings) == 1
    finding = findings[0]
    assert finding.kind == AMBIGUITY_KIND_VALUE
    assert finding.specimen_no == "SPEC-0001"
    assert finding.item_code == "718-7"
    assert finding.values == ("25", "62")
    assert set(finding.event_ids) == {"evt-a", "evt-b"}
    assert "转人工" in finding.reason


def test_两个值分处阈值两侧记为方向冲突(table):
    events = [
        make_event(event_id="evt-a", message_control_id="MC1", value="25"),
        make_event(event_id="evt-b", message_control_id="MC2", value="260"),
    ]
    findings = detect_ambiguous_observations(events, table.rows)
    assert len(findings) == 1
    assert findings[0].kind == AMBIGUITY_KIND_DIRECTION
    assert findings[0].is_direction_conflict is True


def test_修正结果不参与互斥判定(table):
    """修正结果走取代链，不算互斥。"""
    events = [
        make_event(event_id="evt-a", message_control_id="MC1", value="25"),
        make_event(
            event_id="evt-b", message_control_id="MC2", value="62", result_status="C"
        ),
    ]
    assert detect_ambiguous_observations(events, table.rows) == []


def test_同值重复到达不是互斥(table):
    """同一个值再来一次 —— 那是幂等的事，不是互斥。"""
    events = [
        make_event(event_id="evt-a", message_control_id="MC1", value="25"),
        make_event(event_id="evt-b", message_control_id="MC2", value="25"),
    ]
    assert detect_ambiguous_observations(events, table.rows) == []


def test_互斥事件不自动判定也不出卡(table):
    """互斥 → 结论落 ``ambiguous_observation``，转人工，不出卡。"""
    with LedgerStore(":memory:") as store:
        processor = LedgerInboundProcessor(store, rows_provider=lambda: list(table.rows))
        first = processor(make_event(event_id="evt-a", message_control_id="MC1", value="25"))
        assert first.decision == DECISION_CRITICAL
        assert first.card_id

        second = processor(
            make_event(event_id="evt-b", message_control_id="MC2", value="62")
        )
        assert second.decision == DECISION_AMBIGUOUS_OBSERVATION
        assert second.status == "transferred_to_human"
        assert second.card_id == ""

        row = store.get_event("evt-b")
        assert row is not None
        assert row["status"] == "transferred_to_human"
        assert row["judgement_decision"] == DECISION_AMBIGUOUS_OBSERVATION

        actions = [entry["action"] for entry in store.audit_entries("evt-b")]
        assert "ambiguous_observation" in actions


def test_台账里的互斥检测能找到兄弟事件(table):
    with LedgerStore(":memory:") as store:
        store.record_event(make_event(event_id="evt-a", message_control_id="MC1", value="25"))
        store.record_event(make_event(event_id="evt-b", message_control_id="MC2", value="62"))
        finding = check_ambiguity(store, make_event(event_id="evt-b", message_control_id="MC2", value="62"), table.rows)
        assert finding is not None
        assert finding.kind == AMBIGUITY_KIND_VALUE


# ---------------------------------------------------------------------------
# 修正结果取代链
# ---------------------------------------------------------------------------


def test_修正结果取代旧事件并把旧事件置为终态(table):
    with LedgerStore(":memory:") as store:
        old = make_event(event_id="evt-a", message_control_id="MC1", value="25")
        store.record_event(old)
        store.set_status("evt-a", "judged")

        new = make_event(
            event_id="evt-b",
            message_control_id="MC2",
            value="62",
            result_status="C",
        )
        store.record_event(new)
        outcome = apply_supersede(store, new)

        assert outcome.applied is True
        assert outcome.old_event_id == "evt-a"
        assert store.get_status("evt-a") == SUPERSEDE_STATUS


def test_旧事件的判定与依据原样保留不改写历史事实(table):
    with LedgerStore(":memory:") as store:
        old = make_event(event_id="evt-a", message_control_id="MC1", value="25")
        store.record_event(old)
        judgement = judge_event(old, table.rows)
        store.record_judgement("evt-a", judgement)
        store.set_status("evt-a", "judged")
        before = store.get_event("evt-a")

        new = make_event(
            event_id="evt-b",
            message_control_id="MC2",
            value="62",
            result_status="C",
        )
        store.record_event(new)
        apply_supersede(store, new)

        after = store.get_event("evt-a")
        assert after["judgement_decision"] == before["judgement_decision"] == DECISION_CRITICAL
        assert after["threshold_row"] == before["threshold_row"]
        assert after["status"] == SUPERSEDE_STATUS
        assert after["superseded_by"] == "evt-b"


def test_终态不可静默回退(table):
    """被取代的旧事件定格在终态 —— 普通路径不许把它改回去。"""
    with LedgerStore(":memory:") as store:
        store.record_event(make_event(event_id="evt-a", message_control_id="MC1"))
        store.set_status("evt-a", "judged")
        store.link_supersede("evt-b", "evt-a")
        assert store.get_status("evt-a") == SUPERSEDE_STATUS
        with pytest.raises(TerminalStateError):
            store.set_status("evt-a", "notified")


def test_终态回退必须显式声明才允许(table):
    with LedgerStore(":memory:") as store:
        store.record_event(make_event(event_id="evt-a", message_control_id="MC1"))
        store.link_supersede("evt-b", "evt-a")
        store.set_status("evt-a", "archived", allow_terminal=True)
        assert store.get_status("evt-a") == "archived"


def test_已被人工处置的旧事件不被改写(table):
    """旧事件已被值班人确认过 —— 迟到的修正结果无权改写既成事实。"""
    with LedgerStore(":memory:") as store:
        store.record_event(make_event(event_id="evt-a", message_control_id="MC1"))
        # 走完整条主链（任务 id 10 起状态机按表校验，不许跳步）：
        # received → judged → card_built → notified → acknowledged
        for status in ("judged", "card_built", "notified", "acknowledged"):
            store.set_status("evt-a", status)
        new = make_event(
            event_id="evt-b",
            message_control_id="MC2",
            value="62",
            result_status="C",
        )
        store.record_event(new)
        outcome = apply_supersede(store, new)

        assert outcome.applied is False
        assert outcome.conflict is True
        assert store.get_status("evt-a") == "acknowledged"
        actions = [entry["action"] for entry in store.audit_entries("evt-b")]
        assert "supersede_conflict" in actions


def test_非修正结果不进入取代链(table):
    with LedgerStore(":memory:") as store:
        store.record_event(make_event(event_id="evt-a", message_control_id="MC1"))
        new = make_event(event_id="evt-b", message_control_id="MC2", value="62")
        store.record_event(new)
        outcome = apply_supersede(store, new)
        assert outcome.applied is False
        assert outcome.conflict is False
        assert store.get_status("evt-a") == "received"


def test_修正结果经入站处理器走完整取代链(table):
    """走真实入站路径：修正结果重新判定，旧事件置终态，新事件记 ``supersedes``。"""
    with LedgerStore(":memory:") as store:
        processor = LedgerInboundProcessor(store, rows_provider=lambda: list(table.rows))
        first = processor(make_event(event_id="evt-a", message_control_id="MC1", value="25"))
        assert first.decision == DECISION_CRITICAL

        second = processor(
            make_event(
                event_id="evt-b",
                message_control_id="MC2",
                value="62",
                result_status="C",
            )
        )
        assert second.decision == DECISION_NOT_CRITICAL
        assert store.get_status("evt-a") == SUPERSEDE_STATUS
        row = store.get_event("evt-b")
        assert row is not None
        assert row["supersedes"] == "evt-a"
