"""阈值表改版影响面分析 + 历史判定重算（**不改写历史事实**）。

## 现场是什么

检验科改版阈值表，组长要回答两个问题：

1. **哪些项目的判定口径变了？**（阈值 / 方向 / 单位 / 适用人群分档）
2. **历史上按旧版判过的那些结论，现在还成立吗？**

第 2 个问题最容易出事 —— 有人会想「把历史台账按新口径重写一遍」。
本模块**明确不做这件事**：重算只产出**复核清单**，历史台账一行不改。
理由很硬：历史事实是已经发生过的处置记录 —— 谁在什么时候按哪一版依据
判的、值班人确认过没有、登记系统回没回写，这些都不能被一次改版悄悄
改掉。要更正，只能由人拿着这份清单**另开一条处置流程**。

## 与任务 id 26 的分工

``kb/version_diff.py`` 比的是**阈值行**（表格里写了什么变了）；
本模块比的是**编译后的规则**（引擎实际拿什么去判），并在此基础上把
历史事件用新规则**重算**一遍，产出翻转清单。

## 与规则引擎的关系

比对与重算都直接吃 :class:`~critval.judge.ruleset.RuleSet`（任务 id 18 的
编译产物），不另写一套阈值解析 —— 规则集是判定的唯一权威口径，
影响面分析必须跟它**逐字对齐**，否则「影响面」与实际判定会各说各话。
"""

from __future__ import annotations

import csv
import hashlib
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional, Sequence

from critval.judge.critical import (
    DECISION_CRITICAL,
    DECISION_NOT_CRITICAL,
    judge_event,
)
from critval.judge.rules import Rule
from critval.judge.ruleset import RuleSet
from critval.judge.units import normalize_unit
from critval.kb.loader import load_threshold_table
from critval.models import CriticalEvent

__all__ = [
    "Band",
    "FieldChange",
    "FlippedEvent",
    "ImpactDiff",
    "ImpactReport",
    "ItemImpact",
    "RecomputeResult",
    "UndecidableEvent",
    "build_impact_report",
    "compile_ruleset",
    "diff_rulesets",
    "load_ruleset",
    "recompute_history",
]


#: 变更类型。
CHANGE_MODIFIED = "modified"
CHANGE_ADDED = "added"
CHANGE_REMOVED = "removed"

CHANGE_LABELS = {
    CHANGE_MODIFIED: "口径变更",
    CHANGE_ADDED: "新增口径",
    CHANGE_REMOVED: "删除口径",
}

#: 参与比对的判定口径字段（**生效期不参与** —— 生效期迁移由版本层管，
#: 混进来会让「口径没变的项目」全被标成变化，淹掉真正的口径变更）。
FIELD_DIRECTION = "direction"
FIELD_LOW = "low_threshold"
FIELD_HIGH = "high_threshold"
FIELD_UNIT = "unit"
FIELD_SCOPE = "scope"
#: 整条口径的新增 / 删除（不是某一个字段变了，是这条口径本身没了或有了）。
FIELD_CLAUSE = "clause"

FIELD_LABELS = {
    FIELD_DIRECTION: "危急方向",
    FIELD_LOW: "低危急值",
    FIELD_HIGH: "高危急值",
    FIELD_UNIT: "单位",
    FIELD_SCOPE: "适用人群分档",
    FIELD_CLAUSE: "判定口径",
}

#: 方向标签。
DIRECTION_LABELS = {
    "low": "只判低危",
    "high": "只判高危",
    "both": "高低都判",
    "none": "未给出阈值",
}

#: 翻转方向。
FLIP_NEWLY_CRITICAL = "newly_critical"
FLIP_NO_LONGER_CRITICAL = "no_longer_critical"

FLIP_LABELS = {
    FLIP_NEWLY_CRITICAL: "旧不危急 → 新判危急",
    FLIP_NO_LONGER_CRITICAL: "旧判危急 → 新不危急",
}


# ---------------------------------------------------------------------------
# 值域（Band）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Band:
    """一个取值区间（``None`` 表示无穷远端）。"""

    lo: Optional[Decimal] = None
    lo_incl: bool = False
    hi: Optional[Decimal] = None
    hi_incl: bool = False

    def describe(self) -> str:
        if self.lo is None and self.hi is None:
            return "全部取值"
        if self.lo is None:
            left = "[-∞" if self.lo_incl else "(-∞"
        else:
            left = f"[{self.lo}" if self.lo_incl else f"({self.lo}"
        if self.hi is None:
            right = "+∞]" if self.hi_incl else "+∞)"
        else:
            right = f"{self.hi}]" if self.hi_incl else f"{self.hi})"
        return f"{left}, {right}"

    def contains(self, value: Decimal) -> bool:
        if self.lo is not None:
            if value < self.lo or (value == self.lo and not self.lo_incl):
                return False
        if self.hi is not None:
            if value > self.hi or (value == self.hi and not self.hi_incl):
                return False
        return True


@dataclass(frozen=True)
class FlipBand:
    """两版口径之间**结论会翻转**的值域。"""

    available: bool = True
    #: 旧口径不危急、新口径判危急的值域。
    became_critical: tuple[Band, ...] = ()
    #: 旧口径判危急、新口径不危急的值域。
    no_longer_critical: tuple[Band, ...] = ()
    note: str = ""

    @property
    def empty(self) -> bool:
        return not self.became_critical and not self.no_longer_critical

    def describe(self) -> str:
        if not self.available:
            return self.note or "值域不可比"
        if self.empty:
            return "无（两版口径在这些值上结论一致）"
        parts: list[str] = []
        if self.no_longer_critical:
            parts.append(
                "旧危急→新不危急 "
                + "、".join(band.describe() for band in self.no_longer_critical)
            )
        if self.became_critical:
            parts.append(
                "旧不危急→新危急 "
                + "、".join(band.describe() for band in self.became_critical)
            )
        return "；".join(parts)


# ---------------------------------------------------------------------------
# 口径比对
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FieldChange:
    """一处判定口径的变化。"""

    field: str
    label: str
    old_value: str
    new_value: str
    detail: str
    flips_possible: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "label": self.label,
            "old_value": self.old_value,
            "new_value": self.new_value,
            "detail": self.detail,
            "flips_possible": self.flips_possible,
        }


@dataclass(frozen=True)
class ItemImpact:
    """一个项目在新旧两版之间的口径差异。"""

    item_code: str
    item_name: str
    change: str
    changes: tuple[FieldChange, ...]
    scope: str = ""
    old_rule_id: str = ""
    new_rule_id: str = ""
    old_line_no: Optional[int] = None
    new_line_no: Optional[int] = None
    old_version: str = ""
    new_version: str = ""
    band: Optional[FlipBand] = None
    notes: tuple[str, ...] = ()

    @property
    def label(self) -> str:
        return CHANGE_LABELS.get(self.change, self.change)

    @property
    def flips_possible(self) -> bool:
        return any(change.flips_possible for change in self.changes)

    @property
    def changed_fields(self) -> str:
        return "、".join(change.label for change in self.changes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "item_code": self.item_code,
            "item_name": self.item_name,
            "change": self.change,
            "label": self.label,
            "scope": self.scope,
            "changes": [change.to_dict() for change in self.changes],
            "changed_fields": self.changed_fields,
            "flips_possible": self.flips_possible,
            "old_rule_id": self.old_rule_id,
            "new_rule_id": self.new_rule_id,
            "old_line_no": self.old_line_no,
            "new_line_no": self.new_line_no,
            "old_version": self.old_version,
            "new_version": self.new_version,
            "band": (
                {
                    "available": self.band.available,
                    "became_critical": [b.describe() for b in self.band.became_critical],
                    "no_longer_critical": [b.describe() for b in self.band.no_longer_critical],
                    "note": self.band.note,
                }
                if self.band
                else None
            ),
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class ImpactDiff:
    """两版规则集的口径差异总览。"""

    items: tuple[ItemImpact, ...]
    unchanged_items: tuple[str, ...] = ()
    pending_items: tuple[str, ...] = ()
    #: 只写了法不同、归一后等价的单位（口径未变，但要提醒一句）。
    notation_notes: tuple[str, ...] = ()
    at: str = ""

    @property
    def changed_items(self) -> tuple[ItemImpact, ...]:
        return self.items

    @property
    def flip_risk_items(self) -> tuple[ItemImpact, ...]:
        return tuple(item for item in self.items if item.flips_possible)

    def summary(self) -> dict[str, Any]:
        counts = {key: 0 for key in CHANGE_LABELS}
        for item in self.items:
            counts[item.change] = counts.get(item.change, 0) + 1
        return {
            "at": self.at,
            "total": len(self.items),
            "counts": counts,
            "changed_item_count": len({item.item_code for item in self.items}),
            "flip_risk_count": len(self.flip_risk_items),
            "unchanged_count": len(self.unchanged_items),
            "pending_count": len(self.pending_items),
            "notation_notes": list(self.notation_notes),
            "changed_items": sorted({item.item_code for item in self.items}),
        }


def _scope_tuple(rule: Rule) -> tuple[str, Optional[int], Optional[int]]:
    return (rule.scope.sex, rule.scope.age_min, rule.scope.age_max)


def _side_text(value: Optional[Decimal], inclusive: bool, side: str) -> str:
    """阈值的一侧文本（``< 30`` / ``≤ 20`` / ``未给出``）。"""
    if value is None:
        return "（未给出）"
    if side == "low":
        return f"≤ {value}" if inclusive else f"< {value}"
    return f"≥ {value}" if inclusive else f"> {value}"


def _is_critical(rule: Rule, value: Decimal) -> bool:
    """某个取值在**这条规则**下是否判危急。

    口径与 ``judge_value`` 逐字一致（严格不等为默认，取等号算危急只在源表
    明写 ``≤`` / ``≥`` 时成立）—— 影响面算出来的值域必须与实际判定同口径。
    """
    low_hit = rule.low_critical is not None and (
        value <= rule.low_critical if rule.low_inclusive else value < rule.low_critical
    )
    high_hit = rule.high_critical is not None and (
        value >= rule.high_critical if rule.high_inclusive else value > rule.high_critical
    )
    return low_hit or high_hit


def _critical_set(rule: Rule) -> tuple[Band, ...]:
    """一条规则的危急值域（至多两段半直线）。"""
    bands: list[Band] = []
    if rule.direction in ("low", "both") and rule.low_critical is not None:
        bands.append(Band(None, False, rule.low_critical, rule.low_inclusive))
    if rule.direction in ("high", "both") and rule.high_critical is not None:
        bands.append(Band(rule.high_critical, rule.high_inclusive, None, False))
    return tuple(bands)


def _representative(band: Band) -> Decimal:
    """区间的一个代表取值（开区间取中点，点区间取该点）。"""
    if band.lo is None and band.hi is None:
        return Decimal(0)
    if band.lo is None:
        assert band.hi is not None
        return band.hi - 1
    if band.hi is None:
        return band.lo + 1
    return (band.lo + band.hi) / 2


def _flip_bands(old_rule: Rule, new_rule: Rule) -> FlipBand:
    """两版口径之间**结论会翻转**的值域。

    做法：把两版所有阈值点排成断点序列 —— 断点之间的开区间里，两版
    的「是否危急」都是常量，于是逐段取代表值比对即可；断点本身单点判
    （闭开区间语义差异只可能出现在端点上）。
    """
    points = sorted(
        {
            point
            for point in (
                old_rule.low_critical,
                old_rule.high_critical,
                new_rule.low_critical,
                new_rule.high_critical,
            )
            if point is not None
        }
    )
    if not points:
        return FlipBand(available=True)

    atoms: list[Band] = [Band(None, False, points[0], False)]
    for index, point in enumerate(points):
        atoms.append(Band(point, True, point, True))
        if index + 1 < len(points):
            atoms.append(Band(point, False, points[index + 1], False))
    atoms.append(Band(points[-1], False, None, False))

    groups: list[tuple[str, list[Band]]] = []
    current_kind = ""
    current: list[Band] = []
    for band in atoms:
        sample = _representative(band)
        old_critical = _is_critical(old_rule, sample)
        new_critical = _is_critical(new_rule, sample)
        if old_critical == new_critical:
            kind = ""
        elif new_critical:
            kind = FLIP_NEWLY_CRITICAL
        else:
            kind = FLIP_NO_LONGER_CRITICAL
        if kind and kind == current_kind:
            current.append(band)
        else:
            if current_kind and current:
                groups.append((current_kind, current))
            current_kind = kind
            current = [band] if kind else []
    if current_kind and current:
        groups.append((current_kind, current))

    became: list[Band] = []
    stopped: list[Band] = []
    for kind, bands in groups:
        merged = Band(bands[0].lo, bands[0].lo_incl, bands[-1].hi, bands[-1].hi_incl)
        if kind == FLIP_NEWLY_CRITICAL:
            became.append(merged)
        else:
            stopped.append(merged)
    return FlipBand(
        available=True,
        became_critical=tuple(became),
        no_longer_critical=tuple(stopped),
    )


def _compare_rules(old_rule: Rule, new_rule: Rule) -> tuple[FieldChange, ...]:
    """逐字段比对两条规则（只比判定口径，不比生效期）。"""
    changes: list[FieldChange] = []

    if old_rule.direction != new_rule.direction:
        changes.append(
            FieldChange(
                FIELD_DIRECTION,
                FIELD_LABELS[FIELD_DIRECTION],
                DIRECTION_LABELS.get(old_rule.direction, old_rule.direction),
                DIRECTION_LABELS.get(new_rule.direction, new_rule.direction),
                "危急方向变了 —— 哪些取值算危急改了",
            )
        )

    if old_rule.low_critical != new_rule.low_critical or (
        old_rule.low_inclusive != new_rule.low_inclusive
    ):
        changes.append(
            FieldChange(
                FIELD_LOW,
                FIELD_LABELS[FIELD_LOW],
                _side_text(old_rule.low_critical, old_rule.low_inclusive, "low"),
                _side_text(new_rule.low_critical, new_rule.low_inclusive, "low"),
                "低危急值门槛变了 —— 落在两个门槛之间的历史结果结论会翻转",
            )
        )

    if old_rule.high_critical != new_rule.high_critical or (
        old_rule.high_inclusive != new_rule.high_inclusive
    ):
        changes.append(
            FieldChange(
                FIELD_HIGH,
                FIELD_LABELS[FIELD_HIGH],
                _side_text(old_rule.high_critical, old_rule.high_inclusive, "high"),
                _side_text(new_rule.high_critical, new_rule.high_inclusive, "high"),
                "高危急值门槛变了 —— 落在两个门槛之间的历史结果结论会翻转",
            )
        )

    old_unit = normalize_unit(old_rule.unit)
    new_unit = normalize_unit(new_rule.unit)
    if old_unit != new_unit:
        changes.append(
            FieldChange(
                FIELD_UNIT,
                FIELD_LABELS[FIELD_UNIT],
                old_unit or "（空）",
                new_unit or "（空）",
                "单位变了 —— 数量级可能随之变化，须核对换算关系",
            )
        )

    if _scope_tuple(old_rule) != _scope_tuple(new_rule):
        changes.append(
            FieldChange(
                FIELD_SCOPE,
                FIELD_LABELS[FIELD_SCOPE],
                old_rule.scope.describe(),
                new_rule.scope.describe(),
                "适用人群分档变了 —— 同一张表按不同人群取不同阈值",
            )
        )

    return tuple(changes)


def _notation_notes(old_rule: Rule, new_rule: Rule) -> tuple[str, ...]:
    """只写了法不同、归一后等价的单位 —— 口径没变，但要提醒一句。"""
    raw_old = (old_rule.unit or "").strip()
    raw_new = (new_rule.unit or "").strip()
    if not raw_old or not raw_new or raw_old == raw_new:
        return ()
    if normalize_unit(raw_old) != normalize_unit(raw_new):
        return ()
    return (
        f"单位写法由「{raw_old}」改为「{raw_new}」，归一后等价（{normalize_unit(raw_new)}），"
        "判定口径不变",
    )


def _select_effective(rules: Sequence[Rule], at: date) -> Optional[Rule]:
    """在给定时刻生效的那条规则（多条时取生效期最新的一条）。"""
    covering = [rule for rule in rules if rule.covers(at)]
    if not covering:
        return None
    covering.sort(
        key=lambda rule: (
            rule.effective_from.toordinal() if rule.effective_from else -1,
            rule.rule_id,
        )
    )
    return covering[-1]


def _index_by_scope(rules: Sequence[Rule]) -> dict[tuple[str, str, Optional[int], Optional[int]], list[Rule]]:
    index: dict[tuple[str, str, Optional[int], Optional[int]], list[Rule]] = {}
    for rule in rules:
        sex, age_min, age_max = _scope_tuple(rule)
        index.setdefault((rule.item_code, sex, age_min, age_max), []).append(rule)
    return index


def _band_for_pair(old_rule: Optional[Rule], new_rule: Optional[Rule]) -> Optional[FlipBand]:
    """两版口径之间的翻转值域；不可比时给出原因而不是瞎算。"""
    if old_rule is None and new_rule is None:
        return None
    if old_rule is None:
        assert new_rule is not None
        return FlipBand(
            available=True,
            became_critical=_critical_set(new_rule),
            note="旧版没有这条口径 —— 新版口径下判危急的值都是新增的",
        )
    if new_rule is None:
        assert old_rule is not None
        return FlipBand(
            available=True,
            no_longer_critical=_critical_set(old_rule),
            note="新版删掉了这条口径 —— 旧版判危急的值在新版下不再判危急",
        )
    old_unit = normalize_unit(old_rule.unit)
    new_unit = normalize_unit(new_rule.unit)
    if old_unit != new_unit:
        return FlipBand(
            available=False,
            note=f"两版单位口径不同（{old_unit or '（空）'} → {new_unit or '（空）'}），未做换算 —— 值域须人工核对",
        )
    if _scope_tuple(old_rule) != _scope_tuple(new_rule):
        return FlipBand(
            available=False,
            note="适用人群分档不同，两版口径不在同一适用人群上 —— 值域须人工核对",
        )
    return _flip_bands(old_rule, new_rule)


def diff_rulesets(
    old: RuleSet,
    new: RuleSet,
    *,
    at: Optional[date] = None,
) -> ImpactDiff:
    """两版规则集逐项目比对，产出判定口径变化清单。

    对齐口径：按「项目码 + 适用人群分档」配对；分档本身改了就先按
    「新增 + 删除」处理，再把同一项目码下的孤儿规则配成一对，报成
    **口径变更（分档变化）** —— 避免「删一条加一条」被读成两件事。
    """
    moment = at or date.today()
    old_index = _index_by_scope(old.rules)
    new_index = _index_by_scope(new.rules)

    items: list[ItemImpact] = []
    unchanged: list[str] = []
    pending: list[str] = []
    notation_notes: list[str] = []

    removed_keys: list[tuple[str, str, Optional[int], Optional[int]]] = []
    added_keys: list[tuple[str, str, Optional[int], Optional[int]]] = []

    for key in sorted(set(old_index) | set(new_index)):
        old_rules = old_index.get(key, [])
        new_rules = new_index.get(key, [])
        old_rule = _select_effective(old_rules, moment)
        new_rule = _select_effective(new_rules, moment)

        if old_rule is None and new_rule is None:
            if old_rules or new_rules:
                pending.append(key[0])
            continue

        if old_rule is None:
            added_keys.append(key)
            continue
        if new_rule is None:
            removed_keys.append(key)
            continue

        changes = _compare_rules(old_rule, new_rule)
        notes = _notation_notes(old_rule, new_rule)
        if not changes:
            unchanged.append(key[0])
            notation_notes.extend(
                f"{key[0]} {old_rule.item_name}：{note}" for note in notes
            )
            continue
        items.append(
            ItemImpact(
                item_code=key[0],
                item_name=new_rule.item_name or old_rule.item_name,
                change=CHANGE_MODIFIED,
                changes=changes,
                scope=new_rule.scope.describe(),
                old_rule_id=old_rule.rule_id,
                new_rule_id=new_rule.rule_id,
                old_line_no=old_rule.citation.line_no,
                new_line_no=new_rule.citation.line_no,
                old_version=old_rule.version,
                new_version=new_rule.version,
                band=_band_for_pair(old_rule, new_rule),
                notes=notes,
            )
        )

    # 分档变化：同一项目码下「旧有新无」的孤儿规则配成一对。
    paired_removed: set[tuple[str, str, Optional[int], Optional[int]]] = set()
    paired_added: set[tuple[str, str, Optional[int], Optional[int]]] = set()
    by_item_removed: dict[str, list[tuple[str, str, Optional[int], Optional[int]]]] = {}
    by_item_added: dict[str, list[tuple[str, str, Optional[int], Optional[int]]]] = {}
    for key in removed_keys:
        by_item_removed.setdefault(key[0], []).append(key)
    for key in added_keys:
        by_item_added.setdefault(key[0], []).append(key)

    for item_code in sorted(set(by_item_removed) & set(by_item_added)):
        olds = sorted(by_item_removed[item_code])
        news = sorted(by_item_added[item_code])
        for old_key, new_key in zip(olds, news):
            old_rule = _select_effective(old_index[old_key], moment)
            new_rule = _select_effective(new_index[new_key], moment)
            if old_rule is None or new_rule is None:
                continue
            changes = _compare_rules(old_rule, new_rule)
            if not changes:
                continue
            paired_removed.add(old_key)
            paired_added.add(new_key)
            items.append(
                ItemImpact(
                    item_code=item_code,
                    item_name=new_rule.item_name or old_rule.item_name,
                    change=CHANGE_MODIFIED,
                    changes=changes,
                    scope=f"{old_rule.scope.describe()} → {new_rule.scope.describe()}",
                    old_rule_id=old_rule.rule_id,
                    new_rule_id=new_rule.rule_id,
                    old_line_no=old_rule.citation.line_no,
                    new_line_no=new_rule.citation.line_no,
                    old_version=old_rule.version,
                    new_version=new_rule.version,
                    band=_band_for_pair(old_rule, new_rule),
                    notes=_notation_notes(old_rule, new_rule),
                )
            )

    for key in removed_keys:
        if key in paired_removed:
            continue
        old_rule = _select_effective(old_index[key], moment)
        if old_rule is None:
            continue
        items.append(
            ItemImpact(
                item_code=key[0],
                item_name=old_rule.item_name,
                change=CHANGE_REMOVED,
                changes=(
                    FieldChange(
                        FIELD_CLAUSE,
                        FIELD_LABELS[FIELD_CLAUSE],
                        DIRECTION_LABELS.get(old_rule.direction, old_rule.direction),
                        "（整条口径删除）",
                        "新版删掉了这条口径 —— 该项目在此之后没有兜底阈值",
                    ),
                ),
                scope=old_rule.scope.describe(),
                old_rule_id=old_rule.rule_id,
                old_line_no=old_rule.citation.line_no,
                old_version=old_rule.version,
                band=_band_for_pair(old_rule, None),
            )
        )

    for key in added_keys:
        if key in paired_added:
            continue
        new_rule = _select_effective(new_index[key], moment)
        if new_rule is None:
            continue
        items.append(
            ItemImpact(
                item_code=key[0],
                item_name=new_rule.item_name,
                change=CHANGE_ADDED,
                changes=(
                    FieldChange(
                        FIELD_CLAUSE,
                        FIELD_LABELS[FIELD_CLAUSE],
                        "（整条口径新增）",
                        DIRECTION_LABELS.get(new_rule.direction, new_rule.direction),
                        "新版新增这条口径 —— 该项目此前没有兜底阈值",
                    ),
                ),
                scope=new_rule.scope.describe(),
                new_rule_id=new_rule.rule_id,
                new_line_no=new_rule.citation.line_no,
                new_version=new_rule.version,
                band=_band_for_pair(None, new_rule),
            )
        )

    items.sort(key=lambda item: (item.item_code, item.change, item.scope))
    return ImpactDiff(
        items=tuple(items),
        unchanged_items=tuple(sorted(set(unchanged))),
        pending_items=tuple(sorted(set(pending))),
        notation_notes=tuple(notation_notes),
        at=moment.isoformat(),
    )


# ---------------------------------------------------------------------------
# 历史判定重算
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FlippedEvent:
    """一条历史判定在新旧口径下结论相反。"""

    event_id: str
    item_code: str
    item_name: str
    value: str
    unit: str
    observed_at: str
    dept_code: str
    specimen_no: str
    old_decision: str
    new_decision: str
    old_reason: str
    new_reason: str
    old_basis: str
    new_basis: str
    flip_kind: str

    @property
    def label(self) -> str:
        return FLIP_LABELS.get(self.flip_kind, self.flip_kind)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "item_code": self.item_code,
            "item_name": self.item_name,
            "value": self.value,
            "unit": self.unit,
            "observed_at": self.observed_at,
            "dept_code": self.dept_code,
            "specimen_no": self.specimen_no,
            "old_decision": self.old_decision,
            "new_decision": self.new_decision,
            "old_reason": self.old_reason,
            "new_reason": self.new_reason,
            "old_basis": self.old_basis,
            "new_basis": self.new_basis,
            "flip_kind": self.flip_kind,
            "label": self.label,
        }


@dataclass(frozen=True)
class UndecidableEvent:
    """新口径下判不出结论的历史事件（须人工复核）。"""

    event_id: str
    item_code: str
    item_name: str
    dept_code: str
    observed_at: str
    old_decision: str
    new_decision: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "item_code": self.item_code,
            "item_name": self.item_name,
            "dept_code": self.dept_code,
            "observed_at": self.observed_at,
            "old_decision": self.old_decision,
            "new_decision": self.new_decision,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class DeptStat:
    """一个科室的重算统计。"""

    dept_code: str
    scanned: int
    flipped: int
    undecidable: int
    newly_decided: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "dept_code": self.dept_code,
            "scanned": self.scanned,
            "flipped": self.flipped,
            "undecidable": self.undecidable,
            "newly_decided": self.newly_decided,
        }


@dataclass(frozen=True)
class RecomputeResult:
    """历史重算的结果 —— **台账未被改写**。"""

    scanned: int
    unchanged: int
    flipped: tuple[FlippedEvent, ...]
    undecidable: tuple[UndecidableEvent, ...]
    newly_decided: tuple[FlippedEvent, ...]
    skipped_non_conclusive: int
    dept_stats: tuple[DeptStat, ...]
    window_from: str
    window_to: str
    dept_code: str
    ledger_before: str
    ledger_after: str

    @property
    def ledger_unchanged(self) -> bool:
        return self.ledger_before == self.ledger_after

    def summary(self) -> dict[str, Any]:
        return {
            "scanned": self.scanned,
            "unchanged": self.unchanged,
            "flipped": len(self.flipped),
            "undecidable": len(self.undecidable),
            "newly_decided": len(self.newly_decided),
            "skipped_non_conclusive": self.skipped_non_conclusive,
            "ledger_unchanged": self.ledger_unchanged,
            "ledger_fingerprint_before": self.ledger_before,
            "ledger_fingerprint_after": self.ledger_after,
            "window_from": self.window_from,
            "window_to": self.window_to,
            "dept_code": self.dept_code,
            "dept_stats": [stat.to_dict() for stat in self.dept_stats],
        }


#: 判定结论 → 是否**已定论**。
CONCLUSIVE = frozenset({DECISION_CRITICAL, DECISION_NOT_CRITICAL})


def _normalize_bound(text: str, *, upper: bool) -> str:
    """时间窗边界归一（只给日期时补到当天首/末秒）。"""
    value = (text or "").strip()
    if not value:
        return ""
    if len(value) == 10 and value.count("-") == 2:
        return f"{value}T23:59:59" if upper else f"{value}T00:00:00"
    return value


def _ledger_fingerprint(rows: Sequence[dict[str, Any]]) -> str:
    """台账指纹（判定相关列的内容哈希）—— 用来证明「历史事实没被动过」。"""
    digest = hashlib.sha256()
    for row in rows:
        digest.update(
            "|".join(
                str(row.get(key, "") or "")
                for key in (
                    "event_id",
                    "status",
                    "judgement_decision",
                    "judgement_reason",
                    "threshold_row",
                    "card_id",
                )
            ).encode("utf-8")
        )
        digest.update(b"\n")
    return digest.hexdigest()[:16]


def _event_from_row(row: dict[str, Any]) -> Optional[CriticalEvent]:
    """从台账行还原成规范化事件（只取事件层字段，不碰患者明细）。"""
    raw_value = str(row.get("value", "") or "").strip()
    try:
        value = Decimal(raw_value)
    except Exception:  # noqa: BLE001 —— 非数值就是不可重算，不抛栈
        return None
    raw_time = str(row.get("observed_at", "") or "").strip()
    try:
        observed_at = datetime.fromisoformat(raw_time)
    except ValueError:
        return None
    return CriticalEvent(
        event_id=str(row.get("event_id", "") or ""),
        message_control_id=str(row.get("message_control_id", "") or ""),
        specimen_no=str(row.get("specimen_no", "") or ""),
        item_code=str(row.get("item_code", "") or ""),
        item_name=str(row.get("item_name", "") or ""),
        value=value,
        unit=str(row.get("unit", "") or ""),
        observed_at=observed_at,
        dept_code=str(row.get("dept_code", "") or ""),
        obx_flag=str(row.get("obx_flag", "") or ""),
        result_status=str(row.get("result_status", "") or ""),
        patient_ref=str(row.get("patient_ref", "") or ""),
        source_channel=str(row.get("source_channel", "") or "hl7"),
    )


def recompute_history(
    store: Any,
    new_ruleset: RuleSet,
    *,
    observed_from: str = "",
    observed_to: str = "",
    dept_code: str = "",
    limit: int = 0,
) -> RecomputeResult:
    """用新规则集把历史事件**重算**一遍，产出翻转清单。

    **只读**：本函数不写台账、不改状态、不写审计；重算结果只进内存与
    复核报告。台账指纹在重算前后各取一次，一致即证明历史事实未被改写。
    """
    lower = _normalize_bound(observed_from, upper=False)
    upper = _normalize_bound(observed_to, upper=True)

    rows = store.judged_events(
        observed_from=lower,
        observed_to=upper,
        dept_code=dept_code,
        limit=limit,
    )
    fingerprint_before = _ledger_fingerprint(rows)

    flipped: list[FlippedEvent] = []
    newly_decided: list[FlippedEvent] = []
    undecidable: list[UndecidableEvent] = []
    unchanged = 0
    skipped = 0

    stats: dict[str, dict[str, int]] = {}

    def bump(dept: str, key: str) -> None:
        bucket = stats.setdefault(
            dept, {"scanned": 0, "flipped": 0, "undecidable": 0, "newly_decided": 0}
        )
        bucket[key] += 1

    for row in rows:
        old_decision = str(row.get("judgement_decision", "") or "")
        event = _event_from_row(row)
        if event is None:
            skipped += 1
            continue

        judgement = judge_event(event, rows=(), ruleset=new_ruleset)
        new_decision = judgement.decision
        new_basis = (
            judgement.threshold_row.citation.locator
            if judgement.threshold_row is not None
            else ""
        )
        old_basis = str(row.get("threshold_row", "") or "")
        dept = event.dept_code
        bump(dept, "scanned")

        old_conclusive = old_decision in CONCLUSIVE
        new_conclusive = new_decision in CONCLUSIVE

        if old_conclusive and new_conclusive and old_decision != new_decision:
            flip_kind = (
                FLIP_NEWLY_CRITICAL
                if new_decision == DECISION_CRITICAL
                else FLIP_NO_LONGER_CRITICAL
            )
            flipped.append(
                FlippedEvent(
                    event_id=event.event_id,
                    item_code=event.item_code,
                    item_name=event.item_name,
                    value=str(event.value),
                    unit=event.unit,
                    observed_at=event.observed_at.isoformat(),
                    dept_code=dept,
                    specimen_no=event.specimen_no,
                    old_decision=old_decision,
                    new_decision=new_decision,
                    old_reason=str(row.get("judgement_reason", "") or ""),
                    new_reason=judgement.reason,
                    old_basis=old_basis,
                    new_basis=new_basis,
                    flip_kind=flip_kind,
                )
            )
            bump(dept, "flipped")
            continue

        if not new_conclusive:
            undecidable.append(
                UndecidableEvent(
                    event_id=event.event_id,
                    item_code=event.item_code,
                    item_name=event.item_name,
                    dept_code=dept,
                    observed_at=event.observed_at.isoformat(),
                    old_decision=old_decision,
                    new_decision=new_decision,
                    reason=judgement.reason,
                )
            )
            bump(dept, "undecidable")
            continue

        if not old_conclusive:
            newly_decided.append(
                FlippedEvent(
                    event_id=event.event_id,
                    item_code=event.item_code,
                    item_name=event.item_name,
                    value=str(event.value),
                    unit=event.unit,
                    observed_at=event.observed_at.isoformat(),
                    dept_code=dept,
                    specimen_no=event.specimen_no,
                    old_decision=old_decision,
                    new_decision=new_decision,
                    old_reason=str(row.get("judgement_reason", "") or ""),
                    new_reason=judgement.reason,
                    old_basis=old_basis,
                    new_basis=new_basis,
                    flip_kind="newly_decided",
                )
            )
            bump(dept, "newly_decided")
            continue

        unchanged += 1

    after_rows = store.judged_events(
        observed_from=lower,
        observed_to=upper,
        dept_code=dept_code,
        limit=limit,
    )
    fingerprint_after = _ledger_fingerprint(after_rows)

    dept_stats = tuple(
        DeptStat(
            dept_code=dept,
            scanned=bucket["scanned"],
            flipped=bucket["flipped"],
            undecidable=bucket["undecidable"],
            newly_decided=bucket["newly_decided"],
        )
        for dept, bucket in sorted(stats.items())
    )

    return RecomputeResult(
        scanned=len(rows),
        unchanged=unchanged,
        flipped=tuple(flipped),
        undecidable=tuple(undecidable),
        newly_decided=tuple(newly_decided),
        skipped_non_conclusive=skipped,
        dept_stats=dept_stats,
        window_from=observed_from,
        window_to=observed_to,
        dept_code=dept_code,
        ledger_before=fingerprint_before,
        ledger_after=fingerprint_after,
    )


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ImpactReport:
    """改版影响面 + 历史重算的完整报告。"""

    old_ruleset: RuleSet
    new_ruleset: RuleSet
    diff: ImpactDiff
    recompute: Optional[RecomputeResult] = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "old": {
                "source_name": self.old_ruleset.source_name,
                "source_sha256": self.old_ruleset.source_sha256,
                "versions": list(self.old_ruleset.versions),
                "rule_count": len(self.old_ruleset.rules),
                "gap_count": len(self.old_ruleset.gaps),
            },
            "new": {
                "source_name": self.new_ruleset.source_name,
                "source_sha256": self.new_ruleset.source_sha256,
                "versions": list(self.new_ruleset.versions),
                "rule_count": len(self.new_ruleset.rules),
                "gap_count": len(self.new_ruleset.gaps),
            },
            "diff": {
                "summary": self.diff.summary(),
                "items": [item.to_dict() for item in self.diff.items],
            },
        }
        if self.recompute is not None:
            payload["recompute"] = self.recompute.summary()
            payload["recompute"]["flipped"] = [
                event.to_dict() for event in self.recompute.flipped
            ]
            payload["recompute"]["undecidable"] = [
                event.to_dict() for event in self.recompute.undecidable
            ]
            payload["recompute"]["newly_decided"] = [
                event.to_dict() for event in self.recompute.newly_decided
            ]
        return payload

    def render_markdown(self) -> str:
        """人读版报告（写进 ``reports/threshold_impact.md``）。"""
        old = self.old_ruleset
        new = self.new_ruleset
        diff = self.diff
        summary = diff.summary()

        lines: list[str] = [
            "# 阈值表改版影响面与历史判定重算",
            "",
            "> **只读复核报告**：本次分析**未改写历史台账** —— 重算只产出复核清单，"
            "历史上的判定结论、命中依据行与人工确认记录一行未动。"
            "要更正历史，必须由人拿着本清单另开一条处置流程。",
            "",
            f"- 旧版本：{old.source_name or '（未命名）'} · {'、'.join(old.versions) or '（无版本号）'}"
            f" · sha256 {old.source_sha256[:12] or '（无）'} · {len(old.rules)} 条规则"
            f" · 缺口 {len(old.gaps)} 条",
            f"- 新版本：{new.source_name or '（未命名）'} · {'、'.join(new.versions) or '（无版本号）'}"
            f" · sha256 {new.source_sha256[:12] or '（无）'} · {len(new.rules)} 条规则"
            f" · 缺口 {len(new.gaps)} 条",
            f"- 对比基准时刻：{diff.at}（按该时刻生效的那一版口径比对）",
        ]
        if self.recompute is not None:
            window = _window_text(self.recompute)
            lines.append(f"- 历史重算窗口：{window}")
        lines.append("")

        lines.append("## 一、判定口径变化项目清单")
        lines.append("")
        if not diff.items:
            lines.append("两版口径逐项一致 —— 没有项目发生判定口径变化。")
            lines.append("")
        else:
            lines.append(
                f"共 {summary['changed_item_count']} 个项目、{summary['total']} 处口径变化"
                f"（其中 {summary['flip_risk_count']} 个项目的口径变化**可能导致历史结论翻转**）。"
            )
            lines.append("")
            lines.append(
                "| 项目码 | 项目名 | 变更 | 变化字段 | 旧口径 | 新口径 | 可能翻转的值域 | 旧行 → 新行 |"
            )
            lines.append("|------|------|------|------|------|------|------|------|")
            for item in diff.items:
                old_side = "；".join(
                    f"{change.label} {change.old_value}" for change in item.changes
                )
                new_side = "；".join(
                    f"{change.label} {change.new_value}" for change in item.changes
                )
                where = ""
                if item.old_line_no and item.new_line_no:
                    where = f"第 {item.old_line_no} 行 → 第 {item.new_line_no} 行"
                elif item.old_line_no:
                    where = f"第 {item.old_line_no} 行 → （新版无）"
                elif item.new_line_no:
                    where = f"（旧版无） → 第 {item.new_line_no} 行"
                band_text = item.band.describe() if item.band else "—"
                lines.append(
                    f"| {item.item_code} | {item.item_name} | {item.label} | "
                    f"{item.changed_fields} | {old_side} | {new_side} | {band_text} | {where} |"
                )
            lines.append("")
            lines.append(
                "提示：值域按**阈值表单位**给出；单位口径不同的项目未做换算，"
                "已标「值域须人工核对」—— 不要拿两版数字直接比大小。"
            )
            lines.append("")

        notation_notes = list(diff.notation_notes) + [
            f"{item.item_code} {item.item_name}：{note}"
            for item in diff.items
            for note in item.notes
        ]
        if notation_notes:
            lines.append("写法归一提示（口径未变，别误读成变更）：")
            lines.extend(f"- {note}" for note in notation_notes)
            lines.append("")

        if self.recompute is not None:
            result = self.recompute
            lines.append("## 二、历史判定重算（不改写历史事实）")
            lines.append("")
            lines.append(
                f"- 扫描已判定事件 **{result.scanned}** 条"
                f"（其中 {result.skipped_non_conclusive} 条事件值或观察时刻不可解析，未纳入比对）"
            )
            lines.append(
                f"- 结论翻转 **{len(result.flipped)}** 条 · 新口径判不出 **{len(result.undecidable)}** 条 · "
                f"旧口径判不出、新口径判出 **{len(result.newly_decided)}** 条 · 结论一致 **{result.unchanged}** 条"
            )
            lines.append(
                f"- 台账指纹：重算前 `{result.ledger_before}` → 重算后 `{result.ledger_after}`"
                f"（{'一致 —— 历史事实未被改写' if result.ledger_unchanged else '**不一致 —— 须立即排查**'}）"
            )
            lines.append("")

            lines.append("### 2.1 结论翻转事件清单")
            lines.append("")
            if not result.flipped:
                lines.append("（本次改版没有让任何历史判定结论翻转。）")
            else:
                lines.append(
                    "| 事件 ID | 项目码 | 结果 | 观察时刻 | 科室 | 原判定 | 新判定 | 翻转方向 | 旧依据 | 新依据 |"
                )
                lines.append("|------|------|------|------|------|------|------|------|------|------|")
                for event in result.flipped:
                    lines.append(
                        f"| {event.event_id} | {event.item_code} | {event.value} {event.unit} | "
                        f"{event.observed_at} | {event.dept_code or '（未标科室）'} | "
                        f"{event.old_decision} | {event.new_decision} | {event.label} | "
                        f"{event.old_basis or '—'} | {event.new_basis or '—'} |"
                    )
            lines.append("")

            lines.append("### 2.2 新口径下判不出（须人工复核）")
            lines.append("")
            if not result.undecidable:
                lines.append("（无。）")
            else:
                lines.append("| 事件 ID | 项目码 | 科室 | 观察时刻 | 原判定 | 新口径结论 | 说明 |")
                lines.append("|------|------|------|------|------|------|------|")
                for event in result.undecidable:
                    lines.append(
                        f"| {event.event_id} | {event.item_code} | "
                        f"{event.dept_code or '（未标科室）'} | {event.observed_at} | "
                        f"{event.old_decision} | {event.new_decision} | {event.reason} |"
                    )
            lines.append("")

            if result.newly_decided:
                lines.append("### 2.3 旧口径判不出、新口径判出")
                lines.append("")
                lines.append("| 事件 ID | 项目码 | 结果 | 科室 | 旧判定 | 新判定 | 新依据 |")
                lines.append("|------|------|------|------|------|------|------|")
                for event in result.newly_decided:
                    lines.append(
                        f"| {event.event_id} | {event.item_code} | {event.value} {event.unit} | "
                        f"{event.dept_code or '（未标科室）'} | {event.old_decision} | "
                        f"{event.new_decision} | {event.new_basis or '—'} |"
                    )
                lines.append("")

            lines.append("## 三、涉及科室")
            lines.append("")
            if not result.dept_stats:
                lines.append("（窗口内没有已判定事件。）")
            else:
                lines.append("| 科室 | 扫描 | 结论翻转 | 判不出 | 新判出 |")
                lines.append("|------|------|------|------|------|")
                for stat in result.dept_stats:
                    lines.append(
                        f"| {stat.dept_code or '（未标科室）'} | {stat.scanned} | {stat.flipped} | "
                        f"{stat.undecidable} | {stat.newly_decided} |"
                    )
            lines.append("")

        lines.append("## 四、复核提示")
        lines.append("")
        lines.append(
            "1. **翻转不等于错**。口径改了就该改 —— 但历史那次的处置已经发生，"
            "台账记录的是「当时按哪一版判的」，这份清单只提示哪些条目值得回看。"
        )
        lines.append(
            "2. **单位不同不能直接比**。值域一栏标了「须人工核对」的项目，"
            "要先确认换算关系再下结论。"
        )
        lines.append(
            "3. **人群分档项目重算受限**。台账按院内口径不落患者明细，"
            "分档项目缺性别 / 年龄上下文时无法自动重算，会落在「判不出」一栏，"
            "须由检验科人工核对。"
        )
        lines.append(
            "4. **本报告不改写任何历史事实**。要更正历史结论，"
            "请由值班人 / 质控专员人工发起新的处置流程并留痕。"
        )
        lines.append("")
        return "\n".join(lines)


def _window_text(result: RecomputeResult) -> str:
    if result.window_from and result.window_to:
        return f"{result.window_from} ~ {result.window_to}"
    if result.window_from:
        return f"{result.window_from} ~ 至今"
    if result.window_to:
        return f"起始 ~ {result.window_to}"
    return "全部历史事件"


def build_impact_report(
    old_ruleset: RuleSet,
    new_ruleset: RuleSet,
    store: Any = None,
    *,
    at: Optional[date] = None,
    observed_from: str = "",
    observed_to: str = "",
    dept_code: str = "",
    limit: int = 0,
) -> ImpactReport:
    """装配一份完整的改版影响面报告（口径比对 + 历史重算）。"""
    diff = diff_rulesets(old_ruleset, new_ruleset, at=at)
    recompute = None
    if store is not None:
        recompute = recompute_history(
            store,
            new_ruleset,
            observed_from=observed_from,
            observed_to=observed_to,
            dept_code=dept_code,
            limit=limit,
        )
    return ImpactReport(
        old_ruleset=old_ruleset,
        new_ruleset=new_ruleset,
        diff=diff,
        recompute=recompute,
    )


def compile_ruleset(table: Any, *, label: str = "impact") -> RuleSet:
    """把一份阈值表编译成规则集（快照 ID 由**材料指纹**推导，可复算）。"""
    digest = table.source_sha256 or "0000000000000000"
    return RuleSet.compile(
        table.rows,
        snapshot_id=f"rs-{label}-{digest[:12]}",
        source_name=table.source_name,
        source_sha256=table.source_sha256,
    )


def load_ruleset(path: str | Path, *, label: str = "impact") -> RuleSet:
    """读阈值表 xlsx → 编译成规则集。"""
    table = load_threshold_table(path)
    return compile_ruleset(table, label=label)


#: 翻转事件 CSV 的固定列序。
FLIP_CSV_HEADER = (
    "事件ID",
    "项目码",
    "项目名",
    "结果值",
    "单位",
    "观察时刻",
    "科室",
    "标本号",
    "原判定",
    "新判定",
    "翻转方向",
    "旧依据",
    "新依据",
)


def write_flip_csv(events: Sequence[FlippedEvent], path: str | Path) -> Path:
    """把翻转事件清单写成 CSV（UTF-8 BOM，Excel 直开）。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(FLIP_CSV_HEADER)
        for event in events:
            writer.writerow(
                [
                    event.event_id,
                    event.item_code,
                    event.item_name,
                    event.value,
                    event.unit,
                    event.observed_at,
                    event.dept_code,
                    event.specimen_no,
                    event.old_decision,
                    event.new_decision,
                    event.label,
                    event.old_basis,
                    event.new_basis,
                ]
            )
    return target


def write_report(report: ImpactReport, path: str | Path) -> Path:
    """把影响面报告写成 Markdown。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(report.render_markdown(), encoding="utf-8")
    return target
