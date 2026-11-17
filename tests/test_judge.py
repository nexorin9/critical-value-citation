"""危急判定测试：边界口径、单位换算、参考区间分档、缺依据不判定。

判定权威是院内阈值表；「查不到依据」必须表现为**不判定**，
而不是悄悄判成「不危急」。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from critval.judge.critical import (
    DECISION_CRITICAL,
    DECISION_NO_THRESHOLD,
    DECISION_NOT_CRITICAL,
    DECISION_UNIT_UNKNOWN,
    judge_event,
    judge_value,
)
from critval.judge.ranges import parse_reference_ranges, select_band
from critval.judge.units import convert, normalize_unit
from critval.kb.loader import load_threshold_table
from critval.models import CriticalEvent

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "脱敏样例阈值表.xlsx"


@pytest.fixture(scope="module")
def table():
    return load_threshold_table(FIXTURE)


def make_event(item_code: str = "718-7", value: str = "25", unit: str = "g/L", **kwargs) -> CriticalEvent:
    payload = {
        "event_id": "evt-0001",
        "message_control_id": "MSG0001",
        "specimen_no": "SPEC-0001",
        "item_code": item_code,
        "item_name": "血红蛋白",
        "value": Decimal(value),
        "unit": unit,
        "observed_at": datetime(2026, 1, 2, 8, 30),
        "dept_code": "ICU",
    }
    payload.update(kwargs)
    return CriticalEvent(**payload)


# ---- 解析报告：未识别行 / 字段缺失行都要在报告里 -------------------------


def test_未识别行进解析报告不静默跳过(table):
    assert len(table.report.unrecognized_rows) == 1
    missing = table.report.unrecognized_rows[0]
    assert missing.row_no == 7  # 缺项目码的那一行
    assert "项目码" in missing.reason
    assert table.report.ok is False
    assert len(table.report.incomplete_rows) == 1
    assert table.report.incomplete_rows[0]["row_no"] == 6  # 阈值列为空的那一行


def test_行级条目带指纹行号与版本(table):
    row = table.first("718-7")
    assert row is not None
    assert row.row_no == 2
    assert row.citation.source_name == FIXTURE.name
    assert len(row.citation.source_sha256) == 64
    assert row.version == "测试 v1.0"
    assert row.effective_from is not None and row.effective_from.isoformat() == "2026-01-01"


# ---- 判定：边界与方向 ---------------------------------------------------


def test_低于低危急值判危急(table):
    judgement = judge_event(make_event(value="25"), table.rows)
    assert judgement.decision == DECISION_CRITICAL
    assert "低于低危急值 30" in judgement.reason
    assert judgement.threshold_row is not None


def test_恰好等于阈值边界不判危急(table):
    """阈值表写「低于 30」，等于 30 不算危急。"""
    judgement = judge_event(make_event(value="30"), table.rows)
    assert judgement.decision == DECISION_NOT_CRITICAL


def test_高于高危急值判危急(table):
    judgement = judge_event(make_event(value="250"), table.rows)
    assert judgement.decision == DECISION_CRITICAL
    assert "高于高危急值 200" in judgement.reason


def test_阈值表未收录项目不判定(table):
    judgement = judge_event(make_event(item_code="0000-0"), table.rows)
    assert judgement.decision == DECISION_NO_THRESHOLD
    assert judgement.judged is False


def test_阈值列缺失的行不判定(table):
    judgement = judge_event(make_event(item_code="8888-8"), table.rows)
    assert judgement.decision == DECISION_NO_THRESHOLD
    assert judgement.judged is False


# ---- 单位换算：换不到就不判定 -------------------------------------------


def test_单位换算后结论一致(table):
    """25 g/L 与 2500 mg/dL、2.5 g/dL 是同一个结论。"""
    base = judge_event(make_event(value="25"), table.rows)
    mg = judge_event(make_event(value="2500", unit="mg/dL"), table.rows)
    dl = judge_event(make_event(value="2.5", unit="g/dL"), table.rows)
    assert base.decision == mg.decision == dl.decision == DECISION_CRITICAL
    assert mg.converted_value == Decimal("25")
    assert dl.converted_value == Decimal("25")


def test_查不到换算关系就不判定(table):
    judgement = judge_event(make_event(item_code="9999-9", value="5", unit="mmol/L"), table.rows)
    assert judgement.decision == DECISION_UNIT_UNKNOWN
    assert judgement.judged is False
    assert "换算" in judgement.reason


def test_换算表缺项时明确说不判定():
    result = convert(Decimal("5"), "mmol/L", "mg/L", item_code=None)
    assert result.ok is False
    assert result.value is None
    assert "不做判定" in result.error


def test_物质相关换算按项目区分():
    """葡萄糖 mg/dL → mmol/L 依赖摩尔质量，不能对所有项目通用。"""
    glucose = convert(Decimal("500"), "mg/dL", "mmol/L", item_code="2345-7")
    assert glucose.ok is True
    assert glucose.value == Decimal("27.75")
    other = convert(Decimal("500"), "mg/dL", "mmol/L", item_code="718-7")
    assert other.ok is False


def test_单位写法归一():
    assert normalize_unit("10*9/L") == "10^9/L"
    assert normalize_unit("10^9/L") == "10^9/L"
    assert normalize_unit("mg/dl") == "mg/dL"
    assert normalize_unit("μmol/L") == "µmol/L"


def test_缺换算不判定不影响其它项目(table):
    """某个项目换不到换算，不许牵连别的项目。"""
    judgement = judge_event(make_event(item_code="2823-3", value="6.8", unit="mmol/L"), table.rows)
    assert judgement.decision == DECISION_CRITICAL


# ---- 参考区间分档 -------------------------------------------------------


def test_参考区间按性别分档():
    bands = parse_reference_ranges("男 115-150；女 120-150")
    assert len(bands) == 2
    male = select_band(bands, sex="男")
    female = select_band(bands, sex="女")
    assert male is not None and male.low == Decimal("115")
    assert female is not None and female.low == Decimal("120")


def test_参考区间多种文本形态():
    assert parse_reference_ranges("3.5-9.5")[0].high == Decimal("9.5")
    assert parse_reference_ranges("<3.5")[0].high == Decimal("3.5")
    assert parse_reference_ranges(">40")[0].low == Decimal("40")
    assert parse_reference_ranges("<=5")[0].high == Decimal("5")
    assert parse_reference_ranges("阴性")[0].kind == "text"


def test_参考区间反写不当成正常区间():
    band = parse_reference_ranges("9.5-3.5")[0]
    assert band.kind == "invalid"


def test_判定附带参考区间位置(table):
    judgement = judge_event(make_event(value="25"), table.rows)
    assert judgement.reference_band is not None
    assert judgement.reference_band.raw == "115-150"
    assert "超出参考区间" in judgement.reason


def test_阈值行判定函数可直接调用(table):
    row = table.first("718-7")
    judgement = judge_value(row, Decimal("250"), "g/L")
    assert judgement.decision == DECISION_CRITICAL
    assert judgement.rule_id == row.citation.locator
