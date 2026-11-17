"""规则集与规则选取。

一个**规则集** = 一次编译的产物（规则 + 缺口 + 快照 ID）。快照 ID 会随
材料指纹变化而变化，事件落库时记录它 —— 事后复核能确定「当时用的是
哪一版阈值规则」。

选取规则的口径（与「宁可转人工，不拿错档硬套」一致）：

1. **项目码**命中；
2. **观察时刻**落在规则的生效期内（落在版本空档 → 转人工，不取就近版本）；
3. **人群分档**匹配（性别 / 年龄段；上下文不足 → 转人工）；
4. 仍有多条时按**生效期 → 分档具体度 → 规则 ID** 稳定择一；
5. 若同档同生效期出现**口径互斥**（阈值不一致）→ 拒绝自动判定，转人工。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Optional, Sequence

from critval.judge.rules import Rule, RuleGap, compile_rules
from critval.models import ThresholdRow

__all__ = [
    "RuleSelection",
    "RuleSet",
    "SELECTION_CONFLICT",
    "SELECTION_NO_MATCH",
    "SELECTION_SELECTED",
    "select_rule",
]

#: 选取状态。
SELECTION_SELECTED = "selected"
SELECTION_NO_MATCH = "no_match"
SELECTION_CONFLICT = "conflict"


@dataclass(frozen=True)
class RuleSet:
    """一次规则编译的产物（不可变）。"""

    rules: tuple[Rule, ...]
    gaps: tuple[RuleGap, ...]
    snapshot_id: str
    source_name: str
    source_sha256: str
    versions: tuple[str, ...]
    compiled_at: str
    item_codes: tuple[str, ...] = ()

    @classmethod
    def compile(
        cls,
        rows: Sequence[ThresholdRow],
        *,
        snapshot_id: str = "rs-0000-000000000000",
        source_name: str = "",
        source_sha256: str = "",
    ) -> "RuleSet":
        rules, gaps = compile_rules(rows)
        versions: list[str] = []
        item_codes: list[str] = []
        for rule in rules:
            if rule.version and rule.version not in versions:
                versions.append(rule.version)
            if rule.item_code not in item_codes:
                item_codes.append(rule.item_code)
        return cls(
            rules=tuple(rules),
            gaps=tuple(gaps),
            snapshot_id=snapshot_id,
            source_name=source_name,
            source_sha256=source_sha256,
            versions=tuple(versions),
            compiled_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            item_codes=tuple(item_codes),
        )

    def by_item(self, item_code: str) -> tuple[Rule, ...]:
        return tuple(rule for rule in self.rules if rule.item_code == item_code)

    def active_versions(self, at: Optional[date] = None) -> tuple[str, ...]:
        """在给定时刻生效的版本号（未给定时用今天）。"""
        moment = at or date.today()
        seen: list[str] = []
        for rule in self.rules:
            if rule.version and rule.version not in seen and rule.covers(moment):
                seen.append(rule.version)
        return tuple(seen)

    def summary(self) -> dict[str, Any]:
        """规则集摘要 —— CLI 与值班台共用同一份口径。"""
        return {
            "snapshot_id": self.snapshot_id,
            "source_name": self.source_name,
            "source_sha256": self.source_sha256,
            "versions": list(self.versions),
            "active_versions": list(self.active_versions()),
            "rule_count": len(self.rules),
            "gap_count": len(self.gaps),
            "item_count": len(self.item_codes),
            "compiled_at": self.compiled_at,
            "gaps": [gap.to_dict() for gap in self.gaps],
        }

    def summary_text(self) -> str:
        """人读摘要：规则条数 / 生效版本 / 缺口条数 / 规则快照 ID。"""
        active = "、".join(self.active_versions()) or "（无生效版本）"
        lines = [
            f"规则集快照 ID : {self.snapshot_id}",
            f"阈值表文件     : {self.source_name or '（内存构造）'}",
            f"文件指纹       : {self.source_sha256 or '（无）'}",
            f"生效版本       : {active}",
            f"全部版本       : {'、'.join(self.versions) or '（无）'}",
            f"规则条数       : {len(self.rules)}",
            f"覆盖项目数     : {len(self.item_codes)}",
            f"缺口条数       : {len(self.gaps)}",
        ]
        if self.gaps:
            lines.append("缺口明细       ：")
            for gap in self.gaps:
                lines.append(
                    f"  · 第 {gap.row_no} 行 [{gap.item_code}] {gap.reason} —— {gap.detail}"
                )
        return "\n".join(lines)


@dataclass(frozen=True)
class RuleSelection:
    """一次规则选取的结果。"""

    status: str  # selected | no_match | conflict
    rule: Optional[Rule]
    reason: str
    candidates: tuple[Rule, ...] = ()
    considered: int = 0

    @property
    def ok(self) -> bool:
        return self.status == SELECTION_SELECTED and self.rule is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "rule_id": self.rule.rule_id if self.rule else "",
            "reason": self.reason,
            "considered": self.considered,
            "candidates": [rule.rule_id for rule in self.candidates],
        }


def _sort_key(rule: Rule) -> tuple[int, int, str]:
    """排序键：**生效期新的优先 → 分档具体的优先 → 规则 ID 稳定兜底**。

    生效期优先，是因为同一项目换了版本时，新版本才是当前口径；
    分档具体度次之，用于同版本内「通用行 vs 性别 / 年龄段行」择一。
    """
    effective = rule.effective_from.toordinal() if rule.effective_from else -1
    return (-effective, -rule.scope.specificity, rule.rule_id)


def select_rule(
    ruleset: RuleSet,
    item_code: str,
    observed_at: date,
    sex: Optional[str] = None,
    age: Optional[int] = None,
) -> RuleSelection:
    """按项目码 + 观察时刻 + 人群上下文选出**唯一**规则。"""
    candidates = ruleset.by_item(item_code)
    if not candidates:
        return RuleSelection(
            status=SELECTION_NO_MATCH,
            rule=None,
            reason=f"阈值表未收录项目 {item_code}，无可用规则（转人工）",
        )

    effective = [rule for rule in candidates if rule.covers(observed_at)]
    if not effective:
        versions = "、".join(sorted({rule.version or "无版本" for rule in candidates}))
        return RuleSelection(
            status=SELECTION_NO_MATCH,
            rule=None,
            reason=(
                f"项目 {item_code} 的观察时刻 {observed_at.isoformat()} 不在任何阈值版本生效期内"
                f"（现有版本：{versions}），落在版本空档，转人工"
            ),
            candidates=candidates,
            considered=len(candidates),
        )

    scoped = [rule for rule in effective if rule.scope.matches(sex=sex, age=age)]
    if not scoped:
        scope_note = effective[0].scope.describe()
        return RuleSelection(
            status=SELECTION_NO_MATCH,
            rule=None,
            reason=(
                f"项目 {item_code} 按人群分档（{scope_note}）后没有适用规则"
                f"（事件上下文：性别={sex or '未知'}、年龄={age if age is not None else '未知'}），转人工"
            ),
            candidates=effective,
            considered=len(effective),
        )

    ordered = sorted(scoped, key=_sort_key)
    top = ordered[0]

    # 同档同生效期出现口径不一致 → 互斥，拒绝自动判定
    same_tier = [
        rule
        for rule in ordered
        if rule.scope.specificity == top.scope.specificity
        and rule.effective_from == top.effective_from
    ]
    if len(same_tier) > 1 and not all(
        rule.same_threshold_as(top) for rule in same_tier
    ):
        detail = "；".join(
            f"{rule.citation.locator} → {rule.threshold_text}" for rule in same_tier
        )
        return RuleSelection(
            status=SELECTION_CONFLICT,
            rule=None,
            reason=(
                f"项目 {item_code} 在同一分档（{top.scope.describe()}）与同一生效期下存在互斥阈值，"
                f"拒绝自动判定，转人工：{detail}"
            ),
            candidates=same_tier,
            considered=len(effective),
        )

    return RuleSelection(
        status=SELECTION_SELECTED,
        rule=top,
        reason=(
            f"命中规则 {top.rule_id}（{top.threshold_text} {top.scope.describe()}，"
            f"依据 {top.citation.locator}）"
        ),
        candidates=tuple(ordered),
        considered=len(effective),
    )
