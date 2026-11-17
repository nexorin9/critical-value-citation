"""单位换算表测试：换算覆盖 / 换算精度 / 外部覆盖表 / **缺换算即不判定**。

红线：**查不到换算关系就不判定**。换算表查不到时结论必须是 ``unit_unknown``
（转人工），不是悄悄判成「不危急」—— 猜错系数就是把 2.8 mmol/L 当 2.8 mg/dL。

另一条红线：**同一个物理值换个单位上报，结论必须一致**。这条靠十进制精确
运算保证，靠换算精度记录 + 压线提示兜底。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from critval.judge.critical import (
    DECISION_CRITICAL,
    DECISION_NOT_CRITICAL,
    DECISION_UNIT_UNKNOWN,
    HUMAN_REVIEW_STATUS,
    judge_event,
    judge_value,
    requires_human_review,
)
from critval.judge.units import (
    UNIT_PRECISION,
    UnitTable,
    convert,
    current_unit_table,
    load_unit_table,
    normalize_unit,
    reset_unit_table,
)
from critval.kb.loader import load_threshold_table
from critval.models import Citation, CriticalEvent, ThresholdRow

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "脱敏样例阈值表.xlsx"

CSV_HEADER = "from_unit,to_unit,factor,offset,precision,label,item_code,note"


@pytest.fixture(scope="module")
def table():
    return load_threshold_table(FIXTURE)


@pytest.fixture(autouse=True)
def _restore_unit_table():
    """每个用例结束后把换算表复位，避免用例之间互相污染。"""
    yield
    reset_unit_table()


def make_row(**kwargs) -> ThresholdRow:
    """构造一条阈值行（用于换算精度等边界用例）。"""
    payload = {
        "item_code": "1751-7",
        "item_name": "白蛋白",
        "unit": "g/L",
        "low_critical": Decimal("30"),
        "high_critical": None,
        "reference_range_raw": "40-50",
        "effective_from": None,
        "version": "测试 v1.0",
        "citation": Citation(
            source_name="示例危急值阈值表.xlsx",
            source_sha256="0" * 64,
            line_no=3,
            file_version="测试 v1.0",
        ),
    }
    payload.update(kwargs)
    return ThresholdRow(**payload)


def make_event(item_code: str = "718-7", value: str = "25", unit: str = "g/L", **kwargs) -> CriticalEvent:
    payload = {
        "event_id": "evt-units-0001",
        "message_control_id": "MSGUNITS01",
        "specimen_no": "SPEC-UNITS-01",
        "item_code": item_code,
        "item_name": "血红蛋白",
        "value": Decimal(value),
        "unit": unit,
        "observed_at": datetime(2026, 1, 2, 8, 30),
        "dept_code": "ICU",
    }
    payload.update(kwargs)
    return CriticalEvent(**payload)


# ---- 换算表来源 ---------------------------------------------------------


def test_默认换算表来自代码内常量():
    table = UnitTable.default()
    assert table.source == "builtin"
    assert table.entry_count > 0
    assert table.warnings == ()


def test_外部覆盖表可覆盖同名换算项(tmp_path):
    csv = tmp_path / "units.csv"
    csv.write_text(
        CSV_HEADER
        + "\nmg/dL,g/L,0.02,0,2,质量浓度换算（院内口径）,,院内口径：1 mg/dL = 0.02 g/L\n",
        encoding="utf-8",
    )
    table = UnitTable.load(csv)
    assert table.source == str(csv)
    result = convert(Decimal("1000"), "mg/dL", "g/L", table=table)
    assert result.ok is True
    assert result.value == Decimal("20")
    assert result.label == "质量浓度换算（院内口径）"


def test_外部覆盖表可追加项目专用换算项(tmp_path):
    csv = tmp_path / "units.csv"
    csv.write_text(
        CSV_HEADER + "\nmmol/L,mg/dL,20,0,2,院内口径（某项目）,TEST-1,测试项目\n",
        encoding="utf-8",
    )
    table = UnitTable.load(csv)
    result = convert(Decimal("3"), "mmol/L", "mg/dL", item_code="TEST-1", table=table)
    assert result.ok is True
    assert result.value == Decimal("60")
    # 默认表里查不到「测试项目」，不带项目码就换不了
    assert convert(Decimal("3"), "mmol/L", "mg/dL", table=table).ok is False


def test_外部覆盖表损坏时回退默认表并告警(tmp_path):
    csv = tmp_path / "units.csv"
    csv.write_text(CSV_HEADER + "\nmg/dL,g/L,不是数值,0,2,坏行,,因子写错了\n", encoding="utf-8")
    table = UnitTable.load(csv)
    assert table.warnings, "损坏的换算表必须留下告警，不能静默使用"
    assert "回退默认换算表" in table.warnings[0]
    # 回退后默认换算仍然可用
    result = convert(Decimal("2500"), "mg/dL", "g/L", table=table)
    assert result.ok is True
    assert result.value == Decimal("25")


def test_外部覆盖表个别行不可用时跳过该行并告警(tmp_path):
    csv = tmp_path / "units.csv"
    csv.write_text(
        CSV_HEADER
        + "\nmg/dL,g/L,不是数值,0,2,坏行,,坏行\n"
        + "mmol/L,mg/dL,7,0,2,可用行,,可用行\n",
        encoding="utf-8",
    )
    table = UnitTable.load(csv)
    assert len(table.warnings) == 1
    assert "第 2 行" in table.warnings[0]
    result = convert(Decimal("2"), "mmol/L", "mg/dL", table=table)
    assert result.ok is True
    assert result.value == Decimal("14")


def test_外部覆盖表不存在时用默认表且不告警(tmp_path):
    table = UnitTable.load(tmp_path / "没有这个文件.csv")
    assert table.source == "builtin"
    assert table.warnings == ()


def test_外部覆盖表可带注释行(tmp_path):
    """模板文件带注释行（``#`` 开头）也能正常装载。"""
    csv = tmp_path / "units.csv"
    csv.write_text(
        "# 注释行\n"
        + CSV_HEADER
        + "\n# 又一条注释\n"
        + "mg/dL,g/L,0.01,0,2,质量浓度换算,,1 mg/dL = 0.01 g/L\n",
        encoding="utf-8",
    )
    table = UnitTable.load(csv)
    assert table.warnings == ()
    assert convert(Decimal("2500"), "mg/dL", "g/L", table=table).value == Decimal("25")


def test_随仓库发布的换算表模板可直接装载():
    """samples/units.csv.example 是院内可用的填写模板。"""
    template = Path(__file__).resolve().parent.parent / "samples" / "units.csv.example"
    table = UnitTable.load(template)
    assert table.warnings == ()
    assert table.entry_count > 0


def test_换算表注册表装载后生效并可复位(tmp_path):
    csv = tmp_path / "units.csv"
    csv.write_text(CSV_HEADER + "\nmg/dL,g/L,0.02,0,2,院内口径,,院内口径\n", encoding="utf-8")
    load_unit_table(csv)
    assert current_unit_table().source == str(csv)
    assert convert(Decimal("1000"), "mg/dL", "g/L").value == Decimal("20")
    reset_unit_table()
    assert current_unit_table().source == "builtin"
    assert convert(Decimal("1000"), "mg/dL", "g/L").value == Decimal("10")


# ---- 常见单位对换算 -----------------------------------------------------


@pytest.mark.parametrize(
    "value,from_unit,to_unit,expected,item_code",
    [
        ("2500", "mg/dL", "g/L", "25", None),
        ("25", "g/L", "mg/dL", "2500", None),
        ("2.5", "g/dL", "g/L", "25", None),
        ("500", "mg/dL", "mmol/L", "27.75", "2345-7"),
        ("2.775", "mmol/L", "mg/dL", "49.9944", "2345-7"),
        ("88.4", "mg/dL", "µmol/L", "7814.56", "2160-0"),
        ("120", "IU/L", "U/L", "120", None),
        ("6.35", "10^9/L", "10^3/uL", "6.35", None),
        ("0.35", "10^12/L", "10^9/L", "0.00035", None),
    ],
)
def test_常见检验单位对换算覆盖(value, from_unit, to_unit, expected, item_code):
    result = convert(Decimal(value), from_unit, to_unit, item_code=item_code)
    assert result.ok is True, result.error
    assert result.value == Decimal(expected)


def test_单位别名归一():
    assert normalize_unit("mg/dl") == "mg/dL"
    assert normalize_unit("10*9/L") == "10^9/L"
    assert normalize_unit("μmol/L") == "µmol/L"
    assert normalize_unit(" K/UL ") == "10^3/uL"


def test_逆向换算可回原值():
    forward = convert(Decimal("2500"), "mg/dL", "g/L")
    back = convert(forward.value, "g/L", "mg/dL")
    assert back.ok is True
    assert back.value == Decimal("2500")


def test_物质相关换算按项目区分():
    """葡萄糖 mmol/L ↔ mg/dL 依赖摩尔质量，不能对所有项目通用。"""
    glucose = convert(Decimal("500"), "mg/dL", "mmol/L", item_code="2345-7")
    assert glucose.ok is True
    assert glucose.value == Decimal("27.75")
    other = convert(Decimal("500"), "mg/dL", "mmol/L", item_code="718-7")
    assert other.ok is False


# ---- 换算精度 ------------------------------------------------------------


def test_换算结果记录精度与步长():
    result = convert(Decimal("2500"), "mg/dL", "g/L")
    assert result.precision == UNIT_PRECISION["g/L"] == 2
    assert result.step == Decimal("0.01")
    assert result.reported_value == Decimal("25.00")
    assert result.label == "质量浓度换算"


def test_十进制精确换算不因浮点误差把临界值判反():
    """浮点会把「恰好等于阈值」的值算到阈值另一侧，危急值就这样被漏掉。

    浮点：29 g/L × 0.1 = 2.9000000000000004 > 2.9 → 判「不危急」（漏报）；
    十进制：29 g/L × 0.1 = 2.90 == 2.9 → 判「危急」。
    """
    assert 29 * 0.1 > 2.9  # 浮点陷阱：结果落在阈值另一侧
    row = make_row(item_code="1751-7", unit="g/dL", low_critical=Decimal("2.9"), low_inclusive=True)
    judgement = judge_value(row, Decimal("29"), "g/L")
    assert judgement.converted_value == Decimal("2.9")
    assert judgement.decision == DECISION_CRITICAL


def test_压线值标出待复核但不改变判定():
    """换算值离阈值不足一个精度步长 → 标压线，判定口径不变。"""
    row = make_row(low_critical=Decimal("30"))
    judgement = judge_value(row, Decimal("2999.9"), "mg/dL")
    assert judgement.converted_value == Decimal("29.999")
    assert judgement.decision == DECISION_CRITICAL
    assert judgement.near_boundary is True
    assert "压线值" in judgement.boundary_note
    assert "压线值" in judgement.reason


def test_远离阈值的值不标压线():
    row = make_row(low_critical=Decimal("30"))
    judgement = judge_value(row, Decimal("25"), "g/L")
    assert judgement.near_boundary is False
    assert judgement.boundary_note == ""


def test_同一数值不同单位判定结论一致(table):
    """同一个物理值换个单位上报，结论必须一致（跨单位一致性是硬要求）。"""
    cases = [
        # (g/L 值, mg/dL 值, 期望结论)
        ("25", "2500", DECISION_CRITICAL),
        ("30", "3000", DECISION_NOT_CRITICAL),
        ("29.999", "2999.9", DECISION_CRITICAL),
        ("29.99", "2999", DECISION_CRITICAL),
        ("200", "20000", DECISION_NOT_CRITICAL),
        ("200.01", "20001", DECISION_CRITICAL),
    ]
    for gl_value, mgdl_value, expected in cases:
        base = judge_event(make_event(value=gl_value, unit="g/L"), table.rows)
        converted = judge_event(make_event(value=mgdl_value, unit="mg/dL"), table.rows)
        assert base.decision == expected, f"{gl_value} g/L 结论不符"
        assert converted.decision == expected, f"{mgdl_value} mg/dL 结论不符"
        assert base.decision == converted.decision


def test_同一数值不同单位换算值一致(table):
    base = judge_event(make_event(value="25", unit="g/L"), table.rows)
    converted = judge_event(make_event(value="2500", unit="mg/dL"), table.rows)
    assert base.converted_value == converted.converted_value == Decimal("25")


# ---- 缺换算即不判定 -----------------------------------------------------


def test_查不到换算关系就不判定():
    result = convert(Decimal("5"), "mmol/L", "mg/L", item_code=None)
    assert result.ok is False
    assert result.value is None
    assert "不做判定" in result.error
    assert "转人工" in result.error


def test_缺换算时结论为unit_unknown且转人工(table):
    judgement = judge_event(make_event(item_code="9999-9", value="5", unit="mmol/L"), table.rows)
    assert judgement.decision == DECISION_UNIT_UNKNOWN
    assert judgement.judged is False
    assert requires_human_review(judgement.decision) is True
    assert "转人工" in judgement.reason


def test_转人工出口映射到台账分支态():
    assert requires_human_review(DECISION_UNIT_UNKNOWN) is True
    assert requires_human_review(DECISION_CRITICAL) is False
    assert requires_human_review(DECISION_NOT_CRITICAL) is False
    from critval.judge.critical import DECISION_STATUS

    assert DECISION_STATUS[DECISION_UNIT_UNKNOWN] == HUMAN_REVIEW_STATUS == "transferred_to_human"


def test_缺换算不判定不影响其它项目(table):
    """某个项目换不到换算，不许牵连别的项目。"""
    judgement = judge_event(make_event(item_code="2823-3", value="6.8", unit="mmol/L"), table.rows)
    assert judgement.decision == DECISION_CRITICAL
