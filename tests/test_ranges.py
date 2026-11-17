"""参考区间解析与分档测试。

两条硬纪律：

* **解析失败不静默跳过** —— 看不懂的写法落成缺失标记，不参与分档，
  但要在判定记录里看得见；
* **分档拿不准要标低置信** —— 取默认区间时把原因写进判定记录，
  复核的人能看出这条区间是「挑出来的」还是「猜出来的」。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from critval.judge.critical import DECISION_CRITICAL, judge_event, judge_value
from critval.judge.ranges import (
    BandScope,
    missing_markers,
    parse_reference_ranges,
    select_band,
)
from critval.kb.loader import load_threshold_table
from critval.models import Citation, CriticalEvent, ThresholdRow

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "脱敏样例阈值表.xlsx"


@pytest.fixture(scope="module")
def table():
    return load_threshold_table(FIXTURE)


def make_row(**kwargs) -> ThresholdRow:
    payload = {
        "item_code": "718-7",
        "item_name": "血红蛋白",
        "unit": "g/L",
        "low_critical": Decimal("30"),
        "high_critical": Decimal("200"),
        "reference_range_raw": "115-150",
        "effective_from": None,
        "version": "测试 v1.0",
        "citation": Citation(
            source_name="示例危急值阈值表.xlsx",
            source_sha256="0" * 64,
            line_no=2,
            file_version="测试 v1.0",
        ),
    }
    payload.update(kwargs)
    return ThresholdRow(**payload)


def make_event(item_code: str = "718-7", value: str = "25", unit: str = "g/L", **kwargs) -> CriticalEvent:
    payload = {
        "event_id": "evt-range-0001",
        "message_control_id": "MSGRANGE01",
        "specimen_no": "SPEC-RANGE-01",
        "item_code": item_code,
        "item_name": "血红蛋白",
        "value": Decimal(value),
        "unit": unit,
        "observed_at": datetime(2026, 1, 2, 8, 30),
        "dept_code": "ICU",
    }
    payload.update(kwargs)
    return CriticalEvent(**payload)


# ---- 文本形态解析 --------------------------------------------------------


def test_多种区间文本形态解析():
    assert parse_reference_ranges("3.5-9.5")[0].high == Decimal("9.5")
    assert parse_reference_ranges("3.5~9.5")[0].low == Decimal("3.5")
    assert parse_reference_ranges("<3.5")[0].high == Decimal("3.5")
    assert parse_reference_ranges(">40")[0].low == Decimal("40")
    assert parse_reference_ranges("<=5")[0].high == Decimal("5")
    assert parse_reference_ranges(">=2.5")[0].low == Decimal("2.5")
    assert parse_reference_ranges("阴性")[0].kind == "text"
    assert parse_reference_ranges("阴性")[0].text_value == "阴性"
    assert parse_reference_ranges("阳性")[0].kind == "text"


def test_空区间文本解析为空列表():
    assert parse_reference_ranges(None) == []
    assert parse_reference_ranges("") == []


def test_区间反写不当成正常区间():
    band = parse_reference_ranges("9.5-3.5")[0]
    assert band.kind == "invalid"
    assert band.note


# ---- 解析失败：缺失标记 --------------------------------------------------


def test_解析失败落缺失标记不静默跳过():
    bands = parse_reference_ranges("约 3-10")
    assert len(bands) == 1
    assert bands[0].kind == "missing"
    assert bands[0].raw == "约 3-10"
    assert "无法解析" in bands[0].note


def test_缺失标记不参与分档():
    bands = parse_reference_ranges("约 3-10")
    assert select_band(bands, sex="男") is None


def test_缺失标记与可用区间并存时只取可用区间():
    bands = parse_reference_ranges("约 3-10；3.5-9.5")
    assert [b.kind for b in bands] == ["missing", "numeric"]
    band = select_band(bands)
    assert band is not None
    assert band.raw == "3.5-9.5"


def test_缺失说明可被取出():
    bands = parse_reference_ranges("约 3-10；9.5-3.5")
    notes = missing_markers(bands)
    assert len(notes) == 2
    assert any("无法解析" in note for note in notes)
    assert any("反写" in note for note in notes)


# ---- 分档选择 ------------------------------------------------------------


def test_按性别择一():
    bands = parse_reference_ranges("男 115-150；女 120-150")
    assert len(bands) == 2
    male = select_band(bands, sex="男")
    female = select_band(bands, sex="女")
    assert male is not None and male.low == Decimal("115")
    assert female is not None and female.low == Decimal("120")
    assert male.confidence == "high"


def test_按年龄段择一():
    bands = parse_reference_ranges("儿童 3-5；成人 4-6")
    child = select_band(bands, age=8)
    adult = select_band(bands, age=30)
    assert child is not None and child.label == "儿童"
    assert adult is not None and adult.label == "成人"


def test_阈值表人群优先于患者上下文():
    """阈值表把项目拆成「男一行、女一行」时，按表里的人群取区间。"""
    row = make_row(reference_range_raw="男 115-150；女 120-150", applies_sex="男")
    judgement = judge_value(row, Decimal("25"), "g/L", sex="女")
    assert judgement.reference_band is not None
    assert judgement.reference_band.raw == "115-150"
    assert judgement.reference_band.label == "男"
    assert judgement.reference_band_confidence == "high"


def test_缺少分档信息时用默认区间并标低置信():
    bands = parse_reference_ranges("男 115-150；女 120-150")
    band = select_band(bands)
    assert band is not None
    assert band.confidence == "low"
    assert "默认区间" in band.note


def test_性别与区间标签冲突标低置信():
    bands = parse_reference_ranges("女 120-150")
    band = select_band(bands, sex="男")
    assert band is not None
    assert band.confidence == "low"
    assert "不符" in band.note


def test_唯一且无标签的区间置信度为高():
    band = select_band(parse_reference_ranges("115-150"))
    assert band is not None
    assert band.confidence == "high"


def test_定性区间不参与数值比较():
    band = select_band(parse_reference_ranges("阴性"))
    assert band is not None
    assert band.kind == "text"
    assert band.contains(Decimal("1")) is False


def test_分档范围对象可从阈值行读取():
    row = make_row(applies_sex="女", applies_age_min=18, applies_age_max=60)
    scope = BandScope.from_row(row)
    assert scope.has_scope is True
    assert scope.sex == "女"
    assert scope.age_min == 18 and scope.age_max == 60


# ---- 判定记录落低置信 / 缺失 --------------------------------------------


def test_判定记录写入参考区间低置信(table):
    """样例表里葡萄糖的参考区间按性别分档，上报没有性别信息 → 低置信。"""
    judgement = judge_event(make_event(item_code="2345-7", value="2.5", unit="mmol/L"), table.rows)
    assert judgement.decision == DECISION_CRITICAL
    assert judgement.reference_band is not None
    assert judgement.reference_band_confidence == "low"
    assert "默认区间" in judgement.reference_band_note
    assert "置信度低" in judgement.reason


def test_参考区间不可用时判定结论不受影响():
    """区间写坏了不能连累判定结论 —— 判定权威是阈值表，不是参考区间。"""
    row = make_row(reference_range_raw="约 3-10")
    judgement = judge_value(row, Decimal("25"), "g/L")
    assert judgement.decision == DECISION_CRITICAL
    assert judgement.reference_band is None
    assert judgement.reference_band_confidence == "missing"
    assert "参考区间不可用" in judgement.reason


def test_判定附带参考区间位置(table):
    judgement = judge_event(make_event(value="25", unit="g/L"), table.rows)
    assert judgement.reference_band is not None
    assert judgement.reference_band.raw == "115-150"
    assert judgement.reference_band_confidence == "high"
    assert "超出参考区间" in judgement.reason
