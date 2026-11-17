"""危急值判定引擎（判定权威 = 院内阈值表）。

判定链：取阈值行 → 单位对齐（缺换算即不判定）→ 双阈值比较（危急方向）→
参考区间分档（补充信息）→ 结论。

结论取值：

* ``critical`` / ``not_critical`` —— 确定结论；
* ``unit_unknown`` / ``no_threshold`` / ``ambiguous`` —— **不判定**，
  必须转人工，不得当成「不危急」静默放过。

边界口径：阈值**恰好等于**边界时**不算**危急（``低于 30`` 是严格小于）。
这与院内制度「低于 30 或高于 200 判定为危急值」的写法一致。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal
from typing import Iterable, Optional, Sequence

from critval.config import Config
from critval.judge.crosscheck import crosscheck_flag
from critval.judge.ranges import BandScope, missing_markers, parse_reference_ranges, select_band
from critval.kb.version import build_versions, select_version
from critval.judge.ruleset import (
    SELECTION_CONFLICT,
    RuleSet,
    select_rule,
)
from critval.judge.units import convert, normalize_unit
from critval.models import CriticalEvent, Judgement, ThresholdRow

__all__ = [
    "DECISION_AMBIGUOUS",
    "DECISION_AMBIGUOUS_OBSERVATION",
    "DECISION_CRITICAL",
    "DECISION_NOT_CRITICAL",
    "DECISION_NO_THRESHOLD",
    "DECISION_UNIT_UNKNOWN",
    "DECISION_STATUS",
    "HUMAN_REVIEW_STATUS",
    "judge_event",
    "judge_value",
    "requires_human_review",
    "select_threshold_row",
]

DECISION_CRITICAL = "critical"
DECISION_NOT_CRITICAL = "not_critical"
DECISION_UNIT_UNKNOWN = "unit_unknown"
DECISION_NO_THRESHOLD = "no_threshold"
DECISION_AMBIGUOUS = "ambiguous"

#: 同一标本同一项目同一时刻出现**互斥值**（任务 id 9）——
#: 两个结果不可能同时成立，不自动判定、不出卡，转人工裁决。
#:
#: 与 :data:`DECISION_AMBIGUOUS` 的区别：后者是「**阈值口径**互斥」
#: （同一分档同一生效期出现两套阈值），本者是「**观察值**互斥」。
DECISION_AMBIGUOUS_OBSERVATION = "ambiguous_observation"

#: 转人工出口的台账分支态（任务 id 2 起沿用）。
#: 「查不到依据」与「无法判定」都落到这里 —— 绝不自动当成「不危急」放过。
HUMAN_REVIEW_STATUS = "transferred_to_human"

#: 判定结论 → 台账状态。判定层只给结论，状态机按这张表推进。
DECISION_STATUS: dict[str, str] = {
    DECISION_CRITICAL: "judged",
    DECISION_NOT_CRITICAL: "not_critical",
    DECISION_UNIT_UNKNOWN: HUMAN_REVIEW_STATUS,
    DECISION_NO_THRESHOLD: HUMAN_REVIEW_STATUS,
    DECISION_AMBIGUOUS: HUMAN_REVIEW_STATUS,
    DECISION_AMBIGUOUS_OBSERVATION: HUMAN_REVIEW_STATUS,
}


def requires_human_review(decision: str) -> bool:
    """该结论是否必须转人工（不判定、不得静默放过）。"""
    return DECISION_STATUS.get(decision) == HUMAN_REVIEW_STATUS


def select_threshold_row(
    rows: Sequence[ThresholdRow],
    item_code: str,
    observed_at: Optional[date] = None,
) -> tuple[Optional[ThresholdRow], list[ThresholdRow]]:
    """按项目码选阈值行。

    同项目**多版本共存**时（任务 id 26）按观察时刻选生效的那一版：
    命中唯一生效版本 → 取该版本的行；落在版本空档 / 两版口径互斥 →
    **不取就近版本**，返回 ``(None, matched)``，由调用方转人工。
    同一版本内的多行（人群分档）维持原口径。
    """
    matched = [row for row in rows if row.item_code == item_code]
    if not matched:
        return None, []
    if len(matched) == 1 or observed_at is None:
        return matched[0], matched

    versions = build_versions(matched)
    if len({v.version_no for v in versions}) <= 1:
        # 同一版本的多行（性别 / 年龄段分档）：维持旧口径。
        covering = [
            row
            for row in matched
            if row.effective_from is None or row.effective_from <= observed_at
        ]
        return (covering[-1] if covering else matched[0]), matched

    selection = select_version(versions, item_code, observed_at)
    if selection.version is None:
        # 版本空档 / 口径互斥 —— 不取就近版本。
        return None, matched
    selected = [
        row
        for row in matched
        if (row.version or "") == selection.version.version_no
        and row.effective_from == selection.version.effective_from
    ]
    return (selected[-1] if selected else None), matched


def judge_value(
    row: ThresholdRow,
    value: Decimal,
    unit: str,
    sex: Optional[str] = None,
    age: Optional[int] = None,
    *,
    low_inclusive: Optional[bool] = None,
    high_inclusive: Optional[bool] = None,
    rule_id: Optional[str] = None,
    ruleset_snapshot_id: str = "",
) -> Judgement:
    """对**已选定阈值行**的数值做危急判定。

    ``low_inclusive`` / ``high_inclusive`` 是闭开区间语义：源表写 ``<=`` / ``>=``
    时取等号也算危急；裸数字按院内习惯取严格不等（等于阈值不算危急）。
    """
    item_code = row.item_code
    low_incl = row.low_inclusive if low_inclusive is None else low_inclusive
    high_incl = row.high_inclusive if high_inclusive is None else high_inclusive
    effective_rule_id = rule_id or row.citation.locator

    if row.direction == "none":
        return Judgement(
            decision=DECISION_NO_THRESHOLD,
            reason=f"阈值表 {row.citation.locator} 未给出该项目的危急值阈值，不做判定（转人工）",
            item_code=item_code,
            value=value,
            unit=unit,
            threshold_row=row,
            rule_id=effective_rule_id,
            ruleset_snapshot_id=ruleset_snapshot_id,
        )

    threshold_unit = normalize_unit(row.unit)
    if not threshold_unit:
        return Judgement(
            decision=DECISION_UNIT_UNKNOWN,
            reason=f"阈值表 {row.citation.locator} 未给出单位，无法与上报值比较，不做判定（转人工）",
            item_code=item_code,
            value=value,
            unit=unit,
            threshold_row=row,
            rule_id=effective_rule_id,
            ruleset_snapshot_id=ruleset_snapshot_id,
        )

    conversion = convert(value, unit, row.unit, item_code=item_code)
    if not conversion.ok:
        return Judgement(
            decision=DECISION_UNIT_UNKNOWN,
            reason=f"{conversion.error}（上报 {value} {normalize_unit(unit) or '空'}，阈值单位 {threshold_unit}）",
            item_code=item_code,
            value=value,
            unit=unit,
            threshold_row=row,
            rule_id=effective_rule_id,
            ruleset_snapshot_id=ruleset_snapshot_id,
        )

    compared = conversion.value
    assert compared is not None  # ok=True 时必有值

    low_hit = row.low_critical is not None and (
        compared <= row.low_critical if low_incl else compared < row.low_critical
    )
    high_hit = row.high_critical is not None and (
        compared >= row.high_critical if high_incl else compared > row.high_critical
    )
    is_critical = low_hit or high_hit

    # 压线检测：换算后的值离阈值不足一个精度步长时，结论可能只是换算精度决定的，
    # 标出来提示人工复核（不改判 —— 判定口径仍按阈值表的严格不等 / 含等号）。
    near_boundary = False
    boundary_note = ""
    step = conversion.step
    if step is not None:
        for label, threshold in (("低危急值", row.low_critical), ("高危急值", row.high_critical)):
            if threshold is None:
                continue
            distance = abs(compared - threshold)
            if distance <= step:
                near_boundary = True
                boundary_note = (
                    f"换算值 {compared} {conversion.to_unit} 距{label} {threshold} 仅 {distance}"
                    f"（不超过目标单位精度步长 {step}），属压线值，建议人工复核"
                )
                break

    bands = parse_reference_ranges(row.reference_range_raw)
    band = select_band(bands, sex=sex, age=age, scope=BandScope.from_row(row))
    band_issues = missing_markers(bands)
    band_note = ""
    band_confidence = ""
    band_note_detail = ""
    if band is not None:
        band_confidence = band.confidence
        band_note_detail = band.note
        if band.kind == "numeric":
            if band.contains(compared):
                band_note = f"换算后 {compared} 在参考区间 {band.raw} 内"
            else:
                band_note = f"换算后 {compared} 超出参考区间 {band.raw}"
        elif band.kind == "text":
            band_note = f"参考区间为定性表述（{band.text_value}）"
        if band.confidence == "low" and band.note:
            band_note += f"（参考区间分档置信度低：{band.note}）"
    elif band_issues:
        band_confidence = "missing"
        band_note_detail = "；".join(band_issues)
        band_note = f"参考区间不可用：{band_note_detail}"

    if is_critical:
        if low_hit and high_hit:
            basis = f"同时低于低危急值 {row.low_critical} 且高于高危急值 {row.high_critical}"
        elif low_hit:
            basis = f"低于{'或等于' if low_incl else ''}低危急值 {row.low_critical} {row.unit}"
        else:
            basis = f"高于{'或等于' if high_incl else ''}高危急值 {row.high_critical} {row.unit}"
        reason = f"判定为危急值：{basis}（依据 {row.citation.locator}）"
        if band_note:
            reason += f"；{band_note}"
        decision = DECISION_CRITICAL
    else:
        reason = f"未触发危急阈值 {row.threshold_text()}（依据 {row.citation.locator}）"
        if band_note:
            reason += f"；{band_note}"
        decision = DECISION_NOT_CRITICAL
    if boundary_note:
        reason += f"；{boundary_note}"

    return Judgement(
        decision=decision,
        reason=reason,
        item_code=item_code,
        value=value,
        unit=unit,
        converted_value=compared,
        converted_unit=conversion.to_unit,
        converted_precision=conversion.precision,
        conversion_note=conversion.note,
        near_boundary=near_boundary,
        boundary_note=boundary_note,
        threshold_row=row,
        rule_id=effective_rule_id,
        reference_band=band,
        reference_band_confidence=band_confidence,
        reference_band_note=band_note_detail,
        ruleset_snapshot_id=ruleset_snapshot_id,
    )


def _judge_event(
    event: CriticalEvent,
    rows: Iterable[ThresholdRow],
    sex: Optional[str] = None,
    age: Optional[int] = None,
    *,
    ruleset: Optional[RuleSet] = None,
) -> Judgement:
    """判定主体（不盖配置版本号）。"""
    observed_date = event.observed_at.date() if event.observed_at else None

    if ruleset is not None:
        if observed_date is None:
            return Judgement(
                decision=DECISION_NO_THRESHOLD,
                reason="事件缺少观察时刻，无法按生效期选取规则，不做判定（转人工）",
                item_code=event.item_code,
                value=event.value,
                unit=event.unit,
                ruleset_snapshot_id=ruleset.snapshot_id,
            )
        selection = select_rule(ruleset, event.item_code, observed_date, sex=sex, age=age)
        if selection.rule is None:
            decision = (
                DECISION_AMBIGUOUS if selection.status == SELECTION_CONFLICT else DECISION_NO_THRESHOLD
            )
            return Judgement(
                decision=decision,
                reason=selection.reason,
                item_code=event.item_code,
                value=event.value,
                unit=event.unit,
                ruleset_snapshot_id=ruleset.snapshot_id,
            )
        rule = selection.rule
        judgement = judge_value(
            rule.row,
            event.value,
            event.unit,
            sex=sex,
            age=age,
            low_inclusive=rule.low_inclusive,
            high_inclusive=rule.high_inclusive,
            rule_id=rule.rule_id,
            ruleset_snapshot_id=ruleset.snapshot_id,
        )
        return Judgement(
            decision=judgement.decision,
            reason=f"{judgement.reason}；{selection.reason}",
            item_code=judgement.item_code,
            value=judgement.value,
            unit=judgement.unit,
            converted_value=judgement.converted_value,
            converted_unit=judgement.converted_unit,
            converted_precision=judgement.converted_precision,
            conversion_note=judgement.conversion_note,
            near_boundary=judgement.near_boundary,
            boundary_note=judgement.boundary_note,
            threshold_row=judgement.threshold_row,
            rule_id=judgement.rule_id,
            reference_band=judgement.reference_band,
            reference_band_confidence=judgement.reference_band_confidence,
            reference_band_note=judgement.reference_band_note,
            ruleset_snapshot_id=ruleset.snapshot_id,
        )

    row, matched = select_threshold_row(list(rows), event.item_code, observed_date)
    if row is None:
        if matched:
            # 多版本共存但选不出生效版本（版本空档 / 口径互斥）—— 转人工，
            # **不取就近版本**（任务 id 26）。
            versions = build_versions(matched)
            selection = select_version(versions, event.item_code, observed_date)
            decision = (
                DECISION_AMBIGUOUS
                if selection.status == "conflict"
                else DECISION_NO_THRESHOLD
            )
            return Judgement(
                decision=decision,
                reason=selection.reason,
                item_code=event.item_code,
                value=event.value,
                unit=event.unit,
            )
        return Judgement(
            decision=DECISION_NO_THRESHOLD,
            reason=f"阈值表未收录项目 {event.item_code}，不做判定（转人工）",
            item_code=event.item_code,
            value=event.value,
            unit=event.unit,
        )

    judgement = judge_value(row, event.value, event.unit, sex=sex, age=age)
    if len(matched) > 1:
        # 同项目多版本：提示按哪一行判定（版本共存与空档处理在版本任务里深化）
        versions = "、".join(sorted({m.version or "无版本" for m in matched}))
        judgement = Judgement(
            decision=judgement.decision,
            reason=judgement.reason + f"；该项目在阈值表中命中 {len(matched)} 行（版本 {versions}），本次按 {row.citation.locator} 判定",
            item_code=judgement.item_code,
            value=judgement.value,
            unit=judgement.unit,
            converted_value=judgement.converted_value,
            converted_unit=judgement.converted_unit,
            converted_precision=judgement.converted_precision,
            conversion_note=judgement.conversion_note,
            near_boundary=judgement.near_boundary,
            boundary_note=judgement.boundary_note,
            threshold_row=judgement.threshold_row,
            rule_id=judgement.rule_id,
            reference_band=judgement.reference_band,
            reference_band_confidence=judgement.reference_band_confidence,
            reference_band_note=judgement.reference_band_note,
            ruleset_snapshot_id=judgement.ruleset_snapshot_id,
        )
    return judgement


def judge_event(
    event: CriticalEvent,
    rows: Iterable[ThresholdRow],
    sex: Optional[str] = None,
    age: Optional[int] = None,
    *,
    ruleset: Optional[RuleSet] = None,
) -> Judgement:
    """对一条规范化事件做危急判定。

    给了 ``ruleset`` 就走**规则引擎**口径（生效期 + 人群分档 + 互斥检测），
    并在结论上记录规则集快照 ID；没给则退回按行选取的旧口径。

    结论上还盖上**本次生效的配置版本号**（任务 id 20）：配置会在运行期
    热载入，事后复核要能确定「这条结论是用哪一版配置判出来的」。

    判定完成后还会做一次 **``OBX-8`` 交叉校验**（任务 id 9）：LIS 的异常标志
    与院内阈值表口径对不上时，**以阈值表为准**，分歧落成
    ``obx_flag_disagreement`` + 说明文字，供值班人复核。
    """
    judgement = _judge_event(event, rows, sex, age, ruleset=ruleset)
    judgement = replace(judgement, config_version=Config.config_version())
    return attach_flag_crosscheck(judgement, event.obx_flag)


def attach_flag_crosscheck(judgement: Judgement, obx_flag: str) -> Judgement:
    """把 ``OBX-8`` 与判定结论对一遍，把分歧记到结论上（**不改判**）。"""
    disagreement = crosscheck_flag(obx_flag, judgement)
    if disagreement is None:
        return judgement
    return replace(
        judgement,
        obx_flag_disagreement=True,
        flag_disagreement_note=disagreement.note,
        reason=f"{judgement.reason}；{disagreement.note}",
    )
