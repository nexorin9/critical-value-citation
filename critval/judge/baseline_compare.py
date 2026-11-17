"""判定基线对比：只看 ``OBX-8`` 的朴素口径 vs 院内阈值表判定引擎（任务 id 9 · 深度项 b）。

## 这层回答什么

「检验科值班技师按「LIS 说异常就报危急值」处理，和按院内阈值表判定，到底差多少？」

差在两处，而且**两处都会在业务上出事**：

1. **告警疲劳** —— LIS 的 ``OBX-8`` 标的是「偏离参考区间」，而危急阈值比
   参考区间宽得多。按 ``OBX-8`` 判，会把大量「异常但不到危急值」的结果推进
   值班群，真要命的那一条被淹在里面。
2. **漏报** —— 有些危急值 LIS 自己没标异常（标志字段没填、厂商方言丢了、
   仪器判读口径不同）。按 ``OBX-8`` 判，这些就漏了。

## 两侧必须同口径，否则数字没意义

对比表里的两行跑的是**同一批样例**、**同一份阈值表**、**同一套判定规则**，
只差「看不看阈值表」这一处：

* **样例集一样** —— 由阈值表**确定性生成**（:func:`build_judge_cases`），
  同一份阈值表跑两次得到逐字节相同的样例集，不掺随机数；
* **材料一样** —— 两侧看到同一份阈值表；
* **尺子一样** —— 真值由样例生成时按**场景意图**标注（``expected_critical``），
  不是拿引擎自己的结论倒推的，所以引擎判错会被这张尺子量出来。

## 产物

* ``docs/examples/judge_baseline_compare.md`` —— 人读的对比表（含复算命令）；
* ``reports/judge_baseline_compare.csv`` —— 机器读的两行数据。

两个产物都**不含时间戳**：同一命令跑两次应得到逐字节相同的文件。
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from critval.judge.crosscheck import (
    BASELINE_CRITICAL,
    BASELINE_NOT_CRITICAL,
    crosscheck_flag,
    naive_flag_decision,
)
from critval.judge.critical import judge_event
from critval.judge.ruleset import RuleSet
from critval.judge.units import convert
from critval.models import CriticalEvent, Judgement, ThresholdRow

__all__ = [
    "BASELINE_NAME",
    "CSV_COLUMNS",
    "ENGINE_NAME",
    "JudgeBaselineComparison",
    "JudgeCase",
    "SCENARIO_LABELS",
    "build_judge_cases",
    "compare_judge_baselines",
    "render_comparison_markdown",
    "write_comparison_csv",
]

#: 两个参赛者的名字（对比表两行）。
BASELINE_NAME = "基线（只看 OBX-8）"
ENGINE_NAME = "判定引擎（院内阈值表为准）"

#: 对比表 CSV 的列。
CSV_COLUMNS = (
    "参赛者",
    "样例数",
    "判危急数",
    "判不危急数",
    "不判定数",
    "口径分歧检出数",
    "未检出分歧数",
    "误报数",
    "漏报数",
    "该判未判数",
    "判错合计",
    "依据可追溯数",
)

#: 场景键 → 人读标签。
SCENARIO_LABELS: dict[str, str] = {
    "critical_low": "低于低危急值（真危急）",
    "critical_high": "高于高危急值（真危急）",
    "unit_converted_critical": "换算单位后高于高危急值（真危急）",
    "critical_without_flag": "真危急但 LIS 未给异常标志",
    "critical_flagged_normal": "真危急但 LIS 标了正常",
    "normal_in_range": "参考区间内（不危急）",
    "abnormal_not_critical_high": "高于参考区间但未达高危急值",
    "abnormal_not_critical_low": "低于参考区间但未达低危急值",
    "edge_equal_high": "恰好等于高危急值",
    "edge_equal_low": "恰好等于低危急值",
    "unit_unknown_not_judged": "单位无法换算（引擎不判定，转人工）",
}

#: 用来试「换个单位上报」的候选单位。
_ALTERNATE_UNITS: tuple[str, ...] = (
    "mg/dL",
    "g/L",
    "mmol/L",
    "umol/L",
    "10*9/L",
    "/uL",
    "U/L",
    "IU/L",
    "ng/mL",
    "%",
    "s",
    "mm[Hg]",
    "/min",
)

#: 样例统一的观察时刻（落在示例阈值表 ``2026-01-01`` 起的生效期内）。
_DEFAULT_OBSERVED_AT = datetime(2026, 1, 15, 8, 30)


def _q(value: Decimal, digits: int = 4) -> Decimal:
    """定量取整（避免浮点噪声把临界值判反），并去掉多余的尾随零。"""
    quantized = value.quantize(Decimal(1).scaleb(-digits), rounding=ROUND_HALF_UP)
    text = format(quantized, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return Decimal(text)


@dataclass(frozen=True)
class JudgeCase:
    """一个对比样例。

    ``expected_critical`` 是**场景意图**（真值）：``True`` / ``False`` 表示这条
    确实该判 / 不该判危急值；``None`` 表示「判不了也不该判」（单位无法换算），
    这类样例只进「不判定数」，不进对错统计。
    """

    scenario: str
    event: CriticalEvent
    expected_critical: Optional[bool]
    note: str = ""

    @property
    def scenario_label(self) -> str:
        return SCENARIO_LABELS.get(self.scenario, self.scenario)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario": self.scenario,
            "scenario_label": self.scenario_label,
            "event": self.event.to_dict(),
            "expected_critical": self.expected_critical,
            "note": self.note,
        }


@dataclass(frozen=True)
class JudgeBaselineComparison:
    """一次判定对比的完整结果（两行数据 + 分歧明细 + 场景分布）。"""

    rows: tuple[Mapping[str, Any], ...]
    case_count: int
    disagreements: tuple[Mapping[str, Any], ...]
    scenario_summary: Mapping[str, Mapping[str, int]]
    threshold_source: str = ""
    ruleset_snapshot_id: str = ""
    observed_at: str = ""

    @property
    def baseline(self) -> Mapping[str, Any]:
        return self.rows[0]

    @property
    def engine(self) -> Mapping[str, Any]:
        return self.rows[1]

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_count": self.case_count,
            "threshold_source": self.threshold_source,
            "ruleset_snapshot_id": self.ruleset_snapshot_id,
            "observed_at": self.observed_at,
            "rows": [dict(row) for row in self.rows],
            "disagreements": [dict(item) for item in self.disagreements],
            "scenario_summary": {
                key: dict(value) for key, value in self.scenario_summary.items()
            },
        }


def _numeric_band(row: ThresholdRow):
    """取该行的数值型参考区间（取不到就返回 ``None``）。"""
    from critval.judge.ranges import BandScope, parse_reference_ranges, select_band

    bands = parse_reference_ranges(row.reference_range_raw)
    band = select_band(bands, sex=None, age=None, scope=BandScope.from_row(row))
    if band is None or band.kind != "numeric":
        return None
    return band


def _flag_for(row: ThresholdRow, value: Decimal) -> str:
    """按 **LIS 的真实行为**给异常标志：相对参考区间偏离就是异常。

    低于参考区间 → ``L``（同时低于低危急值 → ``LL``，panic 档）；
    高于参考区间 → ``H``（同时高于高危急值 → ``HH``）；区间内 → ``N``。
    """
    band = _numeric_band(row)
    if band is None:
        return ""
    if band.low is not None and value < band.low:
        if row.low_critical is not None and value < row.low_critical:
            return "LL"
        return "L"
    if band.high is not None and value > band.high:
        if row.high_critical is not None and value > row.high_critical:
            return "HH"
        return "H"
    return "N"


def _make_event(
    index: int,
    row: ThresholdRow,
    value: Decimal,
    unit: str,
    flag: str,
    observed_at: datetime,
    dept_code: str,
) -> CriticalEvent:
    """造一条样例事件（**脱敏合成**，不冒充真实院内数据）。"""
    return CriticalEvent(
        event_id=f"case-{index:04d}",
        message_control_id=f"CASE{index:04d}",
        specimen_no=f"CASE-SPEC-{index:04d}",
        item_code=row.item_code,
        item_name=row.item_name,
        value=value,
        unit=unit,
        observed_at=observed_at,
        dept_code=dept_code,
        obx_flag=flag,
        result_status="",
        source_channel="baseline_eval",
    )


def _alternate_unit_case(
    row: ThresholdRow, target: Decimal
) -> Optional[tuple[Decimal, str]]:
    """找一个能换算到阈值单位的**别的单位**，并给出对应数值。

    找不到能换算的单位就返回 ``None`` —— 宁可不造样例，也不造一条
    引擎换算不出来、判不了基线的样例来凑数。
    """
    for candidate in _ALTERNATE_UNITS:
        if candidate == row.unit:
            continue
        probe = convert(Decimal(1), candidate, row.unit, item_code=row.item_code)
        if not probe.ok or probe.value in (None, Decimal(0), Decimal(1)):
            continue
        raw = _q(target / probe.value)
        back = convert(raw, candidate, row.unit, item_code=row.item_code)
        if not back.ok or back.value is None:
            continue
        if row.high_critical is not None and back.value > row.high_critical:
            return raw, candidate
        if row.low_critical is not None and back.value < row.low_critical:
            return raw, candidate
    return None


def build_judge_cases(
    rows: Sequence[ThresholdRow],
    *,
    observed_at: datetime = _DEFAULT_OBSERVED_AT,
    dept_code: str = "检验科",
) -> list[JudgeCase]:
    """由阈值表**确定性生成**对比样例集。

    生成逻辑全部由阈值表本身推出来（阈值、参考区间、开闭区间语义），
    **不含随机数** —— 同一份阈值表跑两次得到逐字节相同的样例集。

    每个阈值行按下列场景各出一条（不适用就跳过）：

    ==============================  ====================================
    场景                             构造方式
    ==============================  ====================================
    ``critical_low``                低危急值 × 0.8，``OBX-8=LL``
    ``critical_high``               高危急值 × 1.2，``OBX-8=HH``
    ``unit_converted_critical``      换个单位上报同样的危急值
    ``critical_without_flag``       真危急但 ``OBX-8`` 为空
    ``critical_flagged_normal``     真危急但 ``OBX-8=N``
    ``normal_in_range``             参考区间中点，``OBX-8=N``
    ``abnormal_not_critical_high``  参考区间上沿与高危急值之间
    ``abnormal_not_critical_low``   低危急值与参考区间下沿之间
    ``edge_equal_high``             恰好等于高危急值
    ``edge_equal_low``              恰好等于低危急值
    ``unit_unknown_not_judged``     单位无法换算（引擎不判定）
    ==============================  ====================================
    """
    cases: list[JudgeCase] = []
    index = 0

    def add(
        scenario: str,
        row: ThresholdRow,
        value: Decimal,
        unit: str,
        flag: str,
        expected: Optional[bool],
        note: str = "",
    ) -> None:
        nonlocal index
        index += 1
        cases.append(
            JudgeCase(
                scenario=scenario,
                event=_make_event(index, row, value, unit, flag, observed_at, dept_code),
                expected_critical=expected,
                note=note,
            )
        )

    for row in rows:
        band = _numeric_band(row)
        low = row.low_critical
        high = row.high_critical

        # 1. 真危急：低于低危急值
        if low is not None and low > 0:
            value = _q(low * Decimal("0.8"))
            add(
                "critical_low",
                row,
                value,
                row.unit,
                _flag_for(row, value),
                True,
                f"低于低危急值 {low}",
            )

        # 2. 真危急：高于高危急值
        if high is not None and high > 0:
            value = _q(high * Decimal("1.2"))
            add(
                "critical_high",
                row,
                value,
                row.unit,
                _flag_for(row, value),
                True,
                f"高于高危急值 {high}",
            )

        # 3. 真危急：换个单位上报（引擎靠换算才认得出来）
        if high is not None and high > 0:
            alternate = _alternate_unit_case(row, _q(high * Decimal("1.2")))
            if alternate is not None:
                value, unit = alternate
                add(
                    "unit_converted_critical",
                    row,
                    value,
                    unit,
                    _flag_for(row, _q(high * Decimal("1.2"))),
                    True,
                    f"以 {unit} 上报，换算后高于高危急值 {high}",
                )

        # 4. 真危急但 LIS 没给异常标志 → 朴素口径会漏报
        if high is not None and high > 0:
            value = _q(high * Decimal("1.2"))
            add(
                "critical_without_flag",
                row,
                value,
                row.unit,
                "",
                True,
                "LIS 未填异常标志（方言丢字段 / 仪器未判读）",
            )

        # 5. 真危急但 LIS 标了正常 → 朴素口径会漏报
        if low is not None and low > 0:
            value = _q(low * Decimal("0.8"))
            add(
                "critical_flagged_normal",
                row,
                value,
                row.unit,
                "N",
                True,
                "LIS 把危急值标成了正常",
            )

        # 6. 参考区间内 → 不危急
        if band is not None and band.low is not None and band.high is not None and band.low < band.high:
            value = _q((band.low + band.high) / 2)
            if not (
                (low is not None and value <= low)
                or (high is not None and value >= high)
            ):
                add(
                    "normal_in_range",
                    row,
                    value,
                    row.unit,
                    "N",
                    False,
                    f"落在参考区间 {band.raw} 内",
                )

        # 7. 高于参考区间但未达高危急值 → 朴素口径会误报
        if band is not None and band.high is not None and high is not None and band.high < high:
            value = _q((band.high + high) / 2)
            add(
                "abnormal_not_critical_high",
                row,
                value,
                row.unit,
                "H",
                False,
                f"高于参考区间上沿 {band.high} 但未达高危急值 {high}",
            )

        # 8. 低于参考区间但未达低危急值 → 朴素口径会误报
        if band is not None and band.low is not None and low is not None and low < band.low:
            value = _q((low + band.low) / 2)
            add(
                "abnormal_not_critical_low",
                row,
                value,
                row.unit,
                "L",
                False,
                f"低于参考区间下沿 {band.low} 但未达低危急值 {low}",
            )

        # 9. 恰好等于高危急值（按表内开闭区间语义判）
        if high is not None:
            value = high
            add(
                "edge_equal_high",
                row,
                value,
                row.unit,
                _flag_for(row, value),
                bool(row.high_inclusive),
                f"恰好等于高危急值 {high}（表内语义 {row.threshold_text()}）",
            )

        # 10. 恰好等于低危急值
        if low is not None:
            value = low
            add(
                "edge_equal_low",
                row,
                value,
                row.unit,
                _flag_for(row, value),
                bool(row.low_inclusive),
                f"恰好等于低危急值 {low}（表内语义 {row.threshold_text()}）",
            )

        # 11. 单位无法换算 → 引擎不判定（转人工），基线照样敢下结论
        if high is not None and high > 0:
            value = _q(high * Decimal("1.2"))
            add(
                "unit_unknown_not_judged",
                row,
                value,
                "未登记单位",
                "H",
                None,
                "上报单位查不到换算关系，引擎不判定、转人工",
            )

    return cases


def _engine_decision(judgement: Judgement) -> str:
    """引擎结论归成三档：危急 / 不危急 / 不判定。"""
    if judgement.decision == "critical":
        return BASELINE_CRITICAL
    if judgement.decision == "not_critical":
        return BASELINE_NOT_CRITICAL
    return "undecided"


def compare_judge_baselines(
    cases: Sequence[JudgeCase],
    rows: Sequence[ThresholdRow],
    *,
    ruleset: Optional[RuleSet] = None,
) -> JudgeBaselineComparison:
    """跑两侧判定并算指标。

    两侧看到**同一批样例**、**同一份阈值表**；真值取样例自带的情景意图。
    """
    baseline_critical = 0
    baseline_not_critical = 0
    baseline_over = 0
    baseline_under = 0

    engine_critical = 0
    engine_not_critical = 0
    engine_undecided = 0
    engine_over = 0
    engine_under = 0
    engine_should_judge_missed = 0
    engine_traceable = 0

    detected = 0
    disagreements: list[Mapping[str, Any]] = []
    scenario_summary: dict[str, dict[str, int]] = {}

    for case in cases:
        truth = case.expected_critical
        baseline = naive_flag_decision(case.event.obx_flag)
        judgement = judge_event(case.event, rows, ruleset=ruleset)
        engine = _engine_decision(judgement)

        if baseline == BASELINE_CRITICAL:
            baseline_critical += 1
        else:
            baseline_not_critical += 1
        if truth is not None:
            if baseline == BASELINE_CRITICAL and not truth:
                baseline_over += 1
            elif baseline == BASELINE_NOT_CRITICAL and truth:
                baseline_under += 1

        if engine == BASELINE_CRITICAL:
            engine_critical += 1
        elif engine == BASELINE_NOT_CRITICAL:
            engine_not_critical += 1
        else:
            engine_undecided += 1
        if truth is not None:
            if engine == BASELINE_CRITICAL and not truth:
                engine_over += 1
            elif engine == BASELINE_NOT_CRITICAL and truth:
                engine_under += 1
            elif engine == "undecided":
                engine_should_judge_missed += 1
        if judgement.threshold_row is not None:
            engine_traceable += 1

        # 口径分歧：OBX-8 与阈值表对不上（引擎检出，基线无从检出）
        found = crosscheck_flag(case.event.obx_flag, judgement)
        if found is not None:
            detected += 1
            disagreements.append(
                {
                    "event_id": case.event.event_id,
                    "scenario": case.scenario,
                    "scenario_label": case.scenario_label,
                    "item_code": case.event.item_code,
                    "item_name": case.event.item_name,
                    "value": str(case.event.value),
                    "unit": case.event.unit,
                    "obx_flag": case.event.obx_flag,
                    "engine_decision": engine,
                    "baseline_decision": baseline,
                    "direction": found.direction,
                    "note": found.note,
                }
            )

        bucket = scenario_summary.setdefault(
            case.scenario,
            {"样例数": 0, "基线判危急": 0, "引擎判危急": 0, "引擎不判定": 0, "口径分歧": 0},
        )
        bucket["样例数"] += 1
        if baseline == BASELINE_CRITICAL:
            bucket["基线判危急"] += 1
        if engine == BASELINE_CRITICAL:
            bucket["引擎判危急"] += 1
        if engine == "undecided":
            bucket["引擎不判定"] += 1
        if found is not None:
            bucket["口径分歧"] += 1

    total = len(cases)
    baseline_row = {
        "参赛者": BASELINE_NAME,
        "样例数": total,
        "判危急数": baseline_critical,
        "判不危急数": baseline_not_critical,
        "不判定数": 0,
        "口径分歧检出数": 0,
        "未检出分歧数": detected,
        "误报数": baseline_over,
        "漏报数": baseline_under,
        "该判未判数": 0,
        "判错合计": baseline_over + baseline_under,
        "依据可追溯数": 0,
    }
    engine_row = {
        "参赛者": ENGINE_NAME,
        "样例数": total,
        "判危急数": engine_critical,
        "判不危急数": engine_not_critical,
        "不判定数": engine_undecided,
        "口径分歧检出数": detected,
        "未检出分歧数": detected - detected,
        "误报数": engine_over,
        "漏报数": engine_under,
        "该判未判数": engine_should_judge_missed,
        "判错合计": engine_over + engine_under,
        "依据可追溯数": engine_traceable,
    }

    source = ""
    if rows:
        source = rows[0].citation.source_name
    return JudgeBaselineComparison(
        rows=(baseline_row, engine_row),
        case_count=total,
        disagreements=tuple(disagreements),
        scenario_summary=scenario_summary,
        threshold_source=source,
        ruleset_snapshot_id=ruleset.snapshot_id if ruleset is not None else "",
        observed_at=cases[0].event.observed_at.isoformat() if cases else "",
    )


def render_comparison_markdown(comparison: JudgeBaselineComparison) -> str:
    """渲染人读的对比表（**不写时间戳**，同一命令两次结果逐字节相同）。"""
    base_row, engine_row = comparison.rows
    lines: list[str] = []
    lines.append("# 判定基线对比：只看 OBX-8 vs 院内阈值表判定引擎")
    lines.append("")
    lines.append(
        "同一批样例、同一份阈值表、同一把尺子（样例生成时标注的场景意图），"
        "只差「看不看阈值表」这一处：**基线**只看 LIS 的异常标志；"
        "**引擎**按院内阈值表判定，并把两边的口径分歧落成可复核的记录。"
    )
    lines.append("")

    lines.append("## 参数面")
    lines.append("")
    lines.append("| 项 | 取值 |")
    lines.append("|------|------|")
    lines.append("| 样例数 | {} |".format(comparison.case_count))
    lines.append("| 阈值表 | {} |".format(comparison.threshold_source or "（内存构造）"))
    lines.append(
        "| 规则集快照 ID | {} |".format(comparison.ruleset_snapshot_id or "（按行选取口径）")
    )
    lines.append("| 观察时刻 | {} |".format(comparison.observed_at))
    lines.append("")

    lines.append("## 对比表")
    lines.append("")
    lines.append(
        "| 参赛者 | 判危急 | 判不危急 | 不判定（转人工） | 口径分歧检出 | 未检出分歧 | 误报 | 漏报 | 判错合计 | 依据可追溯 |"
    )
    lines.append("|------|--------|----------|------------------|--------------|------------|------|------|----------|------------|")
    for row in comparison.rows:
        lines.append(
            "| {} | {} | {} | {} | {} | {} | {} | {} | {} | {} |".format(
                row["参赛者"],
                row["判危急数"],
                row["判不危急数"],
                row["不判定数"],
                row["口径分歧检出数"],
                row["未检出分歧数"],
                row["误报数"],
                row["漏报数"],
                row["判错合计"],
                row["依据可追溯数"],
            )
        )
    lines.append("")
    lines.append(
        "**差值**：误报 {over} 条（{b_over} → {e_over}），漏报 {under} 条"
        "（{b_under} → {e_under}），口径分歧检出 {detect} 条（{b_detect} → {e_detect}），"
        "依据可追溯 {trace} 条（{b_trace} → {e_trace}）。".format(
            over=base_row["误报数"] - engine_row["误报数"],
            b_over=base_row["误报数"],
            e_over=engine_row["误报数"],
            under=base_row["漏报数"] - engine_row["漏报数"],
            b_under=base_row["漏报数"],
            e_under=engine_row["漏报数"],
            detect=engine_row["口径分歧检出数"] - base_row["口径分歧检出数"],
            b_detect=base_row["口径分歧检出数"],
            e_detect=engine_row["口径分歧检出数"],
            trace=engine_row["依据可追溯数"] - base_row["依据可追溯数"],
            b_trace=base_row["依据可追溯数"],
            e_trace=engine_row["依据可追溯数"],
        )
    )
    lines.append("")

    lines.append("## 分场景（差在哪儿）")
    lines.append("")
    lines.append(
        "| 场景 | 样例数 | 基线判危急 | 引擎判危急 | 引擎不判定 | 口径分歧 |"
    )
    lines.append("|------|--------|------------|------------|------------|----------|")
    for scenario in sorted(comparison.scenario_summary):
        bucket = comparison.scenario_summary[scenario]
        lines.append(
            "| {} | {} | {} | {} | {} | {} |".format(
                SCENARIO_LABELS.get(scenario, scenario),
                bucket["样例数"],
                bucket["基线判危急"],
                bucket["引擎判危急"],
                bucket["引擎不判定"],
                bucket["口径分歧"],
            )
        )
    lines.append("")

    over_call = [d for d in comparison.disagreements if d["direction"] == "baseline_over_call"]
    under_call = [d for d in comparison.disagreements if d["direction"] == "baseline_under_call"]
    lines.append("## 分歧明细（各取前 5 条）")
    lines.append("")
    lines.append("### 基线多报（误报）")
    lines.append("")
    if over_call:
        lines.append("| 项目 | 结果 | LIS 标志 | 引擎结论 | 说明 |")
        lines.append("|------|------|----------|----------|------|")
        for item in over_call[:5]:
            lines.append(
                "| {} {} | {} {} | {} | {} | {} |".format(
                    item["item_name"],
                    item["item_code"],
                    item["value"],
                    item["unit"],
                    item["obx_flag"] or "（空）",
                    "危急" if item["engine_decision"] == "critical" else "不危急",
                    item["scenario_label"],
                )
            )
    else:
        lines.append("（本次样例集内没有基线多报）")
    lines.append("")
    lines.append("### 基线漏报")
    lines.append("")
    if under_call:
        lines.append("| 项目 | 结果 | LIS 标志 | 引擎结论 | 说明 |")
        lines.append("|------|------|----------|----------|------|")
        for item in under_call[:5]:
            lines.append(
                "| {} {} | {} {} | {} | {} | {} |".format(
                    item["item_name"],
                    item["item_code"],
                    item["value"],
                    item["unit"],
                    item["obx_flag"] or "（空）",
                    "危急" if item["engine_decision"] == "critical" else "不危急",
                    item["scenario_label"],
                )
            )
    else:
        lines.append("（本次样例集内没有基线漏报）")
    lines.append("")

    lines.append("## 怎么读这张表")
    lines.append("")
    lines.append(
        "* **误报那几行是告警疲劳的来源** —— LIS 说「异常」，是因为偏离了"
        "参考区间；危急阈值比参考区间宽得多。按异常标志推进值班群，"
        "真要命的那一条会被淹掉。"
    )
    lines.append(
        "* **漏报那几行是「标志不可信」的证据** —— 有些危急值 LIS 自己没标异常"
        "（标志字段没填、厂商方言丢了）。只看标志就漏。"
    )
    lines.append(
        "* **引擎的「不判定」不是漏报** —— 单位查不到换算关系时，引擎拒绝猜，"
        "转人工；基线照样敢下结论。**敢猜**不等于**判得对**。"
    )
    lines.append(
        "* **依据可追溯那一列是产品底线** —— 引擎每条危急结论都带"
        "「阈值表文件 + 行号 + 版本」，值班人能翻回原文复核；"
        "基线给不出依据。"
    )
    lines.append("")

    lines.append("## 复算")
    lines.append("")
    lines.append("```bash")
    lines.append("python3 scripts/compare_judge_baseline.py")
    lines.append("```")
    lines.append("")
    lines.append(
        "同一命令跑两次得到逐字节相同的 `docs/examples/judge_baseline_compare.md` "
        "与 `reports/judge_baseline_compare.csv`（两个产物都不写时间戳）。"
    )
    lines.append("")
    lines.append(
        "口径说明：样例集由阈值表**确定性生成**（不掺随机数）；真值是样例生成时"
        "按**场景意图**标注的，不是拿引擎自己的结论倒推的 —— 所以引擎判错会被这张"
        "尺子量出来。误报 = 判危急但真值不危急；漏报 = 判不危急但真值为危急；"
        "「该判未判」= 真值明确却转人工。"
    )
    return "\n".join(lines) + "\n"


def write_comparison_csv(
    comparison: JudgeBaselineComparison, path: str | Path
) -> Path:
    """写对比表 CSV（UTF-8 BOM，Excel 直接双击能看）。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(CSV_COLUMNS))
        writer.writeheader()
        for row in comparison.rows:
            writer.writerow(row)
    return target
