"""阈值表行 → 可执行规则（规则编译）。

一条规则 = 一条**能直接跑的判定语句**：危急方向 + 双阈值 + 闭开区间语义 +
单位 + 适用人群（性别 / 年龄段）+ 生效期 + 出处（文件名 + 行号 + 版本）。

编译期铁律：**编不出规则的行不进判定集**，而是进「规则缺口清单」
（保留来源行号与原始单元格内容），等人工确认。阈值表少一条兜底，
就是少一条病人安全网 —— 宁可转人工，也不用一条半对的规则去判生死。

判定权威仍是院内阈值表；本模块不新增任何医学口径，只把表里的事实
**编译**成可执行形式，并把编不出来的地方显式暴露出来。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Optional, Sequence

from critval.models import Citation, ThresholdRow

__all__ = [
    "GAP_DIRECTION_MISSING",
    "GAP_EFFECTIVE_REVERSED",
    "GAP_MISSING_COLUMN",
    "GAP_RANGE_REVERSED",
    "GAP_SCOPE_UNPARSED",
    "GAP_THRESHOLD_NOT_NUMERIC",
    "Rule",
    "RuleGap",
    "RuleScope",
    "compile_rule",
    "compile_rules",
]


#: 缺口归因（机器可解析，CLI 与解析报告共用同一套码）。
GAP_DIRECTION_MISSING = "direction_missing"  # 没给任何一侧阈值
GAP_THRESHOLD_NOT_NUMERIC = "threshold_not_numeric"  # 写了但解析不出数值
GAP_RANGE_REVERSED = "range_reversed"  # 低危值 ≥ 高危值
GAP_MISSING_COLUMN = "missing_column"  # 关键列缺失（如单位）
GAP_SCOPE_UNPARSED = "scope_unparsed"  # 适用人群写法看不懂
GAP_EFFECTIVE_REVERSED = "effective_period_reversed"  # 生效期反写


@dataclass(frozen=True)
class RuleScope:
    """规则的适用人群。

    * ``sex``：``""``（不限）/ ``"男"`` / ``"女"``；
    * 年龄段：下限含、上限不含（与参考区间分档口径一致）。

    匹配语义是**保守**的：规则按性别或年龄分档时，事件上下文里必须
    有对应的性别 / 年龄才算命中 —— 上下文缺失宁可判「无适用规则」
    转人工，也不拿通用档硬套。
    """

    sex: str = ""
    age_min: Optional[int] = None
    age_max: Optional[int] = None
    age_label: str = ""

    @classmethod
    def from_row(cls, row: ThresholdRow) -> "RuleScope":
        return cls(
            sex=row.applies_sex,
            age_min=row.applies_age_min,
            age_max=row.applies_age_max,
            age_label=row.applies_age_label,
        )

    @property
    def has_age(self) -> bool:
        return self.age_min is not None or self.age_max is not None

    @property
    def specificity(self) -> int:
        """分档具体度：性别 2 分 + 年龄段 1 分。越大越优先。"""
        return (2 if self.sex else 0) + (1 if self.has_age else 0)

    def matches(self, sex: Optional[str] = None, age: Optional[int] = None) -> bool:
        """事件上下文是否落在本规则分档内。"""
        if self.sex:
            if not sex:
                return False
            if _normalize_sex(sex) != self.sex:
                return False
        if self.has_age:
            if age is None:
                return False
            if self.age_min is not None and age < self.age_min:
                return False
            if self.age_max is not None and age >= self.age_max:
                return False
        return True

    def describe(self) -> str:
        parts: list[str] = []
        if self.sex:
            parts.append(self.sex)
        if self.has_age:
            if self.age_label:
                parts.append(self.age_label)
            else:
                low = "-" if self.age_min is None else str(self.age_min)
                high = "" if self.age_max is None else str(self.age_max)
                parts.append(f"{low}~{high} 岁")
        return " · ".join(parts) if parts else "通用"

    def to_dict(self) -> dict[str, Any]:
        return {
            "sex": self.sex,
            "age_min": self.age_min,
            "age_max": self.age_max,
            "age_label": self.age_label,
            "specificity": self.specificity,
            "describe": self.describe(),
        }


def _normalize_sex(sex: Optional[str]) -> str:
    if not sex:
        return ""
    text = str(sex).strip()
    if text in ("男", "男性", "M", "male", "Male"):
        return "男"
    if text in ("女", "女性", "F", "female", "Female"):
        return "女"
    return text


@dataclass(frozen=True)
class Rule:
    """一条可执行规则。"""

    rule_id: str
    item_code: str
    item_name: str
    direction: str  # low | high | both
    low_critical: Optional[Decimal]
    high_critical: Optional[Decimal]
    low_inclusive: bool
    high_inclusive: bool
    unit: str
    scope: RuleScope
    effective_from: Optional[date]
    effective_to: Optional[date]
    version: str
    citation: Citation
    row: ThresholdRow

    @property
    def threshold_text(self) -> str:
        low_op = "<=" if self.low_inclusive else "<"
        high_op = ">=" if self.high_inclusive else ">"
        if self.direction == "both":
            return f"{low_op} {self.low_critical} 或 {high_op} {self.high_critical} {self.unit}"
        if self.direction == "low":
            return f"{low_op} {self.low_critical} {self.unit}"
        if self.direction == "high":
            return f"{high_op} {self.high_critical} {self.unit}"
        return "未给出阈值"

    def covers(self, at: date) -> bool:
        """观察时刻是否落在本规则的生效期内。"""
        if self.effective_from is not None and at < self.effective_from:
            return False
        if self.effective_to is not None and at > self.effective_to:
            return False
        return True

    def same_threshold_as(self, other: "Rule") -> bool:
        """两条规则的判定口径是否完全一致（用于互斥检测）。"""
        return (
            self.low_critical == other.low_critical
            and self.high_critical == other.high_critical
            and self.low_inclusive == other.low_inclusive
            and self.high_inclusive == other.high_inclusive
            and self.unit == other.unit
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "item_code": self.item_code,
            "item_name": self.item_name,
            "direction": self.direction,
            "low_critical": str(self.low_critical) if self.low_critical is not None else None,
            "high_critical": str(self.high_critical) if self.high_critical is not None else None,
            "low_inclusive": self.low_inclusive,
            "high_inclusive": self.high_inclusive,
            "unit": self.unit,
            "scope": self.scope.to_dict(),
            "effective_from": self.effective_from.isoformat() if self.effective_from else None,
            "effective_to": self.effective_to.isoformat() if self.effective_to else None,
            "version": self.version,
            "row_no": self.citation.line_no,
            "source": self.citation.locator,
            "threshold_text": self.threshold_text,
        }


@dataclass(frozen=True)
class RuleGap:
    """一条编不出规则的阈值行 —— 必须进缺口清单待人工确认。"""

    reason: str
    detail: str
    row_no: Optional[int]
    item_code: str
    item_name: str
    citation: Optional[Citation] = None
    values: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "reason": self.reason,
            "detail": self.detail,
            "row_no": self.row_no,
            "item_code": self.item_code,
            "item_name": self.item_name,
            "source": self.citation.locator if self.citation else "",
            "values": list(self.values),
        }


def _gap(
    row: ThresholdRow,
    reason: str,
    detail: str,
    values: Sequence[Any] = (),
) -> RuleGap:
    return RuleGap(
        reason=reason,
        detail=detail,
        row_no=row.row_no,
        item_code=row.item_code,
        item_name=row.item_name,
        citation=row.citation,
        values=tuple(str(v) if v is not None else "" for v in values),
    )


def _rule_id(row: ThresholdRow) -> str:
    """规则 ID：项目码 + 行号 + 版本 —— 稳定、可回溯、跨版本不撞。"""
    version = row.version or "无版本"
    return f"{row.item_code}#r{row.row_no or 0}@{version}"


def compile_rule(row: ThresholdRow) -> tuple[Optional[Rule], Optional[RuleGap]]:
    """把一行阈值行编译成规则。

    返回 ``(规则, 缺口)`` —— 二者必有且只有一个非空。
    """
    # 1) 关键列缺失：单位是比对的前提
    if not (row.unit or "").strip():
        return None, _gap(
            row,
            GAP_MISSING_COLUMN,
            "未给出单位，无法与上报值比较，该行不进入判定集（转人工）",
            values=(row.unit,),
        )

    # 2) 阈值非数值：写了但解析不出来（与「空着」是两回事）
    if row.raw_low and row.low_critical is None:
        return None, _gap(
            row,
            GAP_THRESHOLD_NOT_NUMERIC,
            f"低危急值写的是「{row.raw_low}」，解析不出数值，该行不进入判定集（转人工）",
            values=(row.raw_low,),
        )
    if row.raw_high and row.high_critical is None:
        return None, _gap(
            row,
            GAP_THRESHOLD_NOT_NUMERIC,
            f"高危急值写的是「{row.raw_high}」，解析不出数值，该行不进入判定集（转人工）",
            values=(row.raw_high,),
        )

    # 3) 方向缺失：两侧都没给阈值，说不清往哪边危急
    if row.low_critical is None and row.high_critical is None:
        return None, _gap(
            row,
            GAP_DIRECTION_MISSING,
            "未给出低危急值与高危急值，危急方向缺失，该行不进入判定集（转人工）",
            values=(row.raw_low, row.raw_high),
        )

    # 4) 区间反写：低危值 ≥ 高危值 —— 这种行算不出任何危急区间
    if (
        row.low_critical is not None
        and row.high_critical is not None
        and row.low_critical >= row.high_critical
    ):
        return None, _gap(
            row,
            GAP_RANGE_REVERSED,
            f"低危急值 {row.low_critical} 不小于高危急值 {row.high_critical}，区间反写，"
            "该行不进入判定集（转人工）",
            values=(row.raw_low, row.raw_high),
        )

    # 5) 生效期反写
    if (
        row.effective_from is not None
        and row.effective_to is not None
        and row.effective_from > row.effective_to
    ):
        return None, _gap(
            row,
            GAP_EFFECTIVE_REVERSED,
            f"生效日期 {row.effective_from} 晚于失效日期 {row.effective_to}，"
            "该行不进入判定集（转人工）",
        )

    # 6) 适用人群写法看不懂
    if row.scope_error:
        return None, _gap(
            row,
            GAP_SCOPE_UNPARSED,
            f"{row.scope_error}，无法确定该行适用人群，该行不进入判定集（转人工）",
            values=(row.applies_sex, row.applies_age_label),
        )

    rule = Rule(
        rule_id=_rule_id(row),
        item_code=row.item_code,
        item_name=row.item_name,
        direction=row.direction,
        low_critical=row.low_critical,
        high_critical=row.high_critical,
        low_inclusive=row.low_inclusive,
        high_inclusive=row.high_inclusive,
        unit=row.unit,
        scope=RuleScope.from_row(row),
        effective_from=row.effective_from,
        effective_to=row.effective_to,
        version=row.version,
        citation=row.citation,
        row=row,
    )
    return rule, None


def compile_rules(
    rows: Sequence[ThresholdRow],
) -> tuple[list[Rule], list[RuleGap]]:
    """编译一批阈值行 → ``(规则列表, 缺口清单)``。

    缺口清单**不吞行**：每一行要么成为一条规则，要么带着行号与原因
    出现在缺口清单里，两边对得上。
    """
    rules: list[Rule] = []
    gaps: list[RuleGap] = []
    for row in rows:
        rule, gap = compile_rule(row)
        if rule is not None:
            rules.append(rule)
        elif gap is not None:
            gaps.append(gap)
    return rules, gaps
