"""处置卡装配：阈值依据行 / 通知对象 / 上报时限 / 记录去向 四要素。

四要素里**每一项都要带出处**（文件名 + 行号 + 版本），这样值班人才能
回答「这条处置是按哪条制度、哪一行阈值做的」。

处置卡的诚实性是本产品的底线（任务 id 21）：

* **缺项就说缺项** —— 任一要素取不到依据，标 ``missing`` 并写明具体原因
  （未命中阈值表行 / 未召回制度条款 / 排班系统不可用），
  **不用占位文案冒充已解决**；
* **降级要说降级** —— 依据有值但来源是降级来的（当班名单来自缓存、
  阈值行来自历史版本、制度召回走了降级层级），标 ``degraded``，
  并把来源、时间戳、层级号写进卡片。

每个要素都带 ``reason`` 原因码，状态可被机器解析（值班台与台账按它分流）。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Optional, Sequence

from critval.card.provenance import (
    ORIGIN_CACHE,
    REASON_CONFLICT_PENDING,
    REASON_ITEM_CODE_FALLBACK,
    ORIGIN_FALLBACK,
    ORIGIN_HISTORICAL,
    ORIGIN_NO_DUTY,
    ORIGIN_UNAVAILABLE,
    REASON_POLICY_FIELD_EMPTY,
    REASON_POLICY_NOT_RECALLED,
    REASON_RESOLVED,
    REASON_RETRIEVAL_FALLBACK,
    REASON_ROSTER_FALLBACK,
    REASON_ROSTER_FROM_CACHE,
    REASON_ROSTER_NO_DUTY_PERSON,
    REASON_ROSTER_UNAVAILABLE,
    REASON_TEXT,
    REASON_THRESHOLD_HISTORICAL,
    REASON_THRESHOLD_ROW_NOT_MATCHED,
    RETRIEVAL_LEVELS,
    describe_provenance,
)
from critval.config import Config
from critval.kb.consistency import (
    VersionBinding,
    VersionMismatchError,
    check_version_consistency,
)
from critval.models import (
    CardElement,
    CardProvenance,
    Citation,
    ConflictItem,
    CriticalEvent,
    DispositionCard,
    Judgement,
    PolicyClause,
    Provenance,
    conflict_element_keys,
)

__all__ = [
    "ELEMENT_KEYS",
    "build_card",
    "build_policy_elements",
    "build_threshold_element",
    "make_card_id",
]

#: 处置卡四要素的固定顺序（渲染与断言都按这个顺序）。
ELEMENT_KEYS: tuple[str, ...] = (
    "threshold_basis",
    "notify_target",
    "deadline",
    "record_destination",
)

_MISSING_NOTE = "未召回制度条款：时限 / 通知对象 / 记录去向无出处"

#: 原因码的严重度：``missing`` = 这条依据压根没有；``degraded`` = 有，但弱。
_SEVERITY_MISSING = "missing"
_SEVERITY_DEGRADED = "degraded"


@dataclass(frozen=True)
class _Issue:
    """要素上的一条问题（缺失或降级）。"""

    reason: str
    text: str
    severity: str


@dataclass(frozen=True)
class _CardContext:
    """装配时来自上游各环节的事实。"""

    clause: Optional[PolicyClause]
    duty_person: str
    roster: Optional[Provenance]
    retrieval: Optional[Provenance]


def make_card_id(event: CriticalEvent, row_citation: str = "") -> str:
    """处置卡 ID：由事件幂等键 + 依据行派生，同一事件 + 同一依据 → 同一张卡。"""
    basis = f"{event.message_control_id}|{event.item_code}|{row_citation}"
    return "card-" + hashlib.sha256(basis.encode("utf-8")).hexdigest()[:12]


def _finish(
    key: str,
    label: str,
    value: str,
    source: Optional[object],
    issues: list[_Issue],
    base_note: str = "",
) -> CardElement:
    """把「值 + 问题清单」收成一条要素。

    判定口径：

    * 没值 → ``missing``（原因码取第一条缺失类问题）；
    * 有值但存在缺失类问题（要素只取到一部分）→ ``degraded``；
    * 有值且只有降级类问题（来源弱）→ ``degraded``；
    * 有值且无问题 → ``resolved``。
    """
    missing_issues = [i for i in issues if i.severity == _SEVERITY_MISSING]
    degraded_issues = [i for i in issues if i.severity == _SEVERITY_DEGRADED]

    notes: list[str] = []
    if base_note:
        notes.append(base_note)
    notes.extend(i.text for i in issues)
    note = "；".join(notes)

    if not value:
        if missing_issues:
            reason = missing_issues[0].reason
        elif degraded_issues:
            reason = degraded_issues[0].reason
        else:
            reason = REASON_POLICY_NOT_RECALLED
        return CardElement(
            key=key,
            label=label,
            value="",
            status="missing",
            source=source,  # type: ignore[arg-type]
            note=note or REASON_TEXT.get(reason, ""),
            reason=reason,
        )

    if missing_issues:
        status = "degraded"
        reason = missing_issues[0].reason
    elif degraded_issues:
        status = "degraded"
        reason = degraded_issues[0].reason
    else:
        status = "resolved"
        reason = REASON_RESOLVED
    return CardElement(
        key=key,
        label=label,
        value=value,
        status=status,
        source=source,  # type: ignore[arg-type]
        note=note,
        reason=reason,
    )


def _threshold_element(
    judgement: Judgement,
    threshold_prov: Optional[Provenance],
    event: Optional[CriticalEvent] = None,
) -> CardElement:
    row = judgement.threshold_row
    if row is None:
        return CardElement(
            key="threshold_basis",
            label="阈值依据行",
            value="",
            status="missing",
            reason=REASON_THRESHOLD_ROW_NOT_MATCHED,
            note=REASON_TEXT[REASON_THRESHOLD_ROW_NOT_MATCHED],
        )
    parts = [f"{row.item_name}（{row.item_code}）", row.threshold_text()]
    if row.version:
        parts.append(f"版本 {row.version}")
    if row.effective_from:
        parts.append(f"生效 {row.effective_from.isoformat()}")

    issues: list[_Issue] = []
    if event is not None and event.item_code_confidence == "low":
        # 项目码是兜底匹配来的（例如 OBX-3 缺项、由 OBR-4 通用检验标识顶上）：
        # 依据行虽然命中，但这条命中的前提需要人工核对一下。
        source = event.item_code_source or "兜底字段"
        issues.append(
            _Issue(
                REASON_ITEM_CODE_FALLBACK,
                f"项目码来自 {source} 兜底匹配（低置信，需人工核对）",
                _SEVERITY_DEGRADED,
            )
        )
    if threshold_prov is not None and threshold_prov.origin == ORIGIN_HISTORICAL:
        period = f"版本 {row.version}" if row.version else "无版本号"
        if row.effective_from:
            period += f"，生效期 {row.effective_from.isoformat()}"
        if row.effective_to:
            period += f"，失效 {row.effective_to.isoformat()}"
        issues.append(
            _Issue(
                REASON_THRESHOLD_HISTORICAL,
                f"阈值行来自历史版本（{period}）",
                _SEVERITY_DEGRADED,
            )
        )
    return _finish(
        "threshold_basis",
        "阈值依据行",
        " · ".join(parts),
        row.citation,
        issues,
        base_note=row.note,
    )


def _roster_issues(ctx: _CardContext) -> list[_Issue]:
    """当班名单来源带来的问题（缓存 / 兜底 / 不可用）。

    文案统一走 ``describe_provenance``，卡片头部与要素行说的是同一句话。
    """
    roster = ctx.roster
    if roster is None:
        return []
    if roster.origin == ORIGIN_UNAVAILABLE:
        return [
            _Issue(
                REASON_ROSTER_UNAVAILABLE,
                f"{describe_provenance(roster)}，未取到当班人",
                _SEVERITY_MISSING,
            )
        ]
    if roster.origin == ORIGIN_CACHE:
        return [
            _Issue(REASON_ROSTER_FROM_CACHE, describe_provenance(roster), _SEVERITY_DEGRADED)
        ]
    if roster.origin == ORIGIN_FALLBACK:
        return [
            _Issue(REASON_ROSTER_FALLBACK, describe_provenance(roster), _SEVERITY_DEGRADED)
        ]
    if roster.origin == ORIGIN_NO_DUTY:
        return [
            _Issue(
                REASON_ROSTER_NO_DUTY_PERSON,
                describe_provenance(roster),
                _SEVERITY_MISSING,
            )
        ]
    return []


def _retrieval_issues(ctx: _CardContext) -> list[_Issue]:
    """制度召回降级层级带来的问题。"""
    retrieval = ctx.retrieval
    if retrieval is None or retrieval.level <= 0:
        return []
    name = RETRIEVAL_LEVELS.get(retrieval.level, f"第 {retrieval.level} 级降级")
    return [
        _Issue(
            REASON_RETRIEVAL_FALLBACK,
            f"制度召回走了第 {retrieval.level} 级降级（{name}）",
            _SEVERITY_DEGRADED,
        )
    ]


def _policy_element(
    key: str,
    label: str,
    clause_value: str,
    ctx: _CardContext,
    extra: str = "",
    *,
    with_roster: bool = False,
) -> CardElement:
    """制度类要素（通知对象 / 上报时限 / 记录去向）。

    依据缺失时标 ``missing`` 并写明具体原因；只有一部分依据时标
    ``degraded``，不把「取到一半」粉饰成「已解决」。

    **通知对象有个特殊口径**：制度写的那句「通知检验科值班技师与检验科
    组长」是**规则**，不是**可通知的人**。排班系统拿不到人（不可用 /
    缓存过期 / 本班次窗口内无人在班）时，光有规则答不出「找谁」——
    此时值置空、要素标 ``missing``，规则原文挪进备注，不拿规则冒充
    已解决。
    """
    issues: list[_Issue] = []
    base_note = ""
    if with_roster:
        # 当班名单是「通知对象」这一要素的一路来源，放前面 ——
        # 排班系统不可用是更直接的阻塞点。
        roster_issues = _roster_issues(ctx)
        issues.extend(roster_issues)
        if any(issue.severity == _SEVERITY_MISSING for issue in roster_issues):
            if clause_value:
                base_note = f"制度要求的通知对象：{clause_value}"
            clause_value = ""
            extra = ""

    if ctx.clause is None:
        issues.append(_Issue(REASON_POLICY_NOT_RECALLED, _MISSING_NOTE, _SEVERITY_MISSING))
    elif not clause_value and not base_note:
        issues.append(
            _Issue(REASON_POLICY_FIELD_EMPTY, REASON_TEXT[REASON_POLICY_FIELD_EMPTY], _SEVERITY_MISSING)
        )

    issues.extend(_retrieval_issues(ctx))

    text = clause_value
    if extra:
        text = f"{text}（{extra}）" if text else extra
    source = ctx.clause.citation if ctx.clause else None
    return _finish(key, label, text, source, issues, base_note=base_note)


def _apply_conflicts(
    elements: dict[str, CardElement],
    conflicts: Sequence[ConflictItem],
) -> dict[str, CardElement]:
    """把待裁决冲突落到要素上（任务 id 33）。

    冲突未裁决时，相关要素**不得**标成「已解决」——依据齐备不等于口径没
    争议。降级为 ``degraded``，原因码 ``conflict_pending_adjudication``，
    备注里写明差在哪、两方出处各在哪。
    """
    for conflict in conflicts:
        if not conflict.pending:
            continue
        for key in conflict_element_keys(conflict):
            element = elements.get(key)
            if element is None:
                continue
            note = conflict.description
            if element.note:
                note = f"{element.note}；{note}"
            status = element.status
            reason = element.reason
            if status == "resolved":
                status = "degraded"
                reason = REASON_CONFLICT_PENDING
            elements[key] = replace(element, status=status, reason=reason, note=note)
    return elements


def build_threshold_element(
    judgement: Judgement,
    threshold_prov: Optional[Provenance] = None,
    event: Optional[CriticalEvent] = None,
) -> CardElement:
    """公开出口：**阈值依据行**要素。

    合并处置卡（任务 id 34）逐项装配依据时走的是**同一套口径** ——
    缺项、降级、低置信兜底的判定不在第二处再写一遍。
    """
    return _threshold_element(judgement, threshold_prov, event)


def build_policy_elements(
    clause: Optional[PolicyClause],
    duty_person: Optional[str] = None,
    provenance: Optional[CardProvenance] = None,
) -> dict[str, CardElement]:
    """公开出口：通知对象 / 上报时限 / 记录去向 三要素。

    合并处置卡的三项是**卡级共享**的（同一份制度条款、同一个当班人），
    但缺项与降级的判定口径与单张卡完全一致 —— 复用同一套实现。
    """
    prov = provenance or CardProvenance()
    ctx = _CardContext(
        clause=clause,
        duty_person=duty_person or "",
        roster=prov.duty_roster,
        retrieval=prov.policy_retrieval,
    )
    return {
        "notify_target": _policy_element(
            "notify_target",
            "通知对象",
            clause.notify_target if clause else "",
            ctx,
            extra=f"当班人 {duty_person}" if duty_person else "",
            with_roster=True,
        ),
        "deadline": _policy_element(
            "deadline",
            "上报时限",
            clause.deadline_text if clause else "",
            ctx,
            extra=(
                f"{clause.deadline_minutes} 分钟"
                if clause and clause.deadline_minutes
                else ""
            ),
        ),
        "record_destination": _policy_element(
            "record_destination",
            "记录去向",
            clause.record_destination if clause else "",
            ctx,
        ),
    }


def build_card(
    event: CriticalEvent,
    judgement: Judgement,
    policy_clause: Optional[PolicyClause] = None,
    duty_person: Optional[str] = None,
    confirm_url: Optional[str] = None,
    card_id: Optional[str] = None,
    *,
    provenance: Optional[CardProvenance] = None,
    version_binding: Optional[VersionBinding] = None,
    strict_version_pair: bool = False,
    extra_citations: Sequence[Citation] = (),
    conflicts: Sequence[ConflictItem] = (),
) -> DispositionCard:
    """装配处置卡。

    ``policy_clause`` 为 ``None`` 时，时限 / 通知对象 / 记录去向三项标 ``missing``，
    卡片照样出得来 —— 值班人至少能看到「依据没查到，需要人工找制度」。

    ``provenance`` 记录三处来源（当班名单 / 阈值行 / 制度召回）是不是降级来的；
    降级会在卡片上标出来，并把时间戳与层级号一并写明。

    ``conflicts`` 是**待人工裁决的口径冲突**（任务 id 33）：两路召回对同一项目
    的时限 / 通知对象 / 阈值数值说法不一致，或引用了非生效版本。冲突会挂到
    卡片的待人工裁决区块，并让相关要素从「已解决」降为「待裁决」——
    **系统不替值班人选一个**。

    **版本闸门（任务 id 26）**：出卡前先核对这次处置引用的材料版本 ——
    同类材料混版、或与院内声明的版本绑定不符，直接抛
    ``VersionMismatchError`` **拒绝出卡**（依据口径自相矛盾的卡，
    比没有卡更危险）。
    """
    citations: list[Citation] = []
    if judgement.threshold_row is not None:
        citations.append(judgement.threshold_row.citation)
    if policy_clause is not None:
        citations.append(policy_clause.citation)
    citations.extend(extra_citations)

    consistency = check_version_consistency(
        citations,
        binding=version_binding,
        strict_pair=strict_version_pair,
    )
    if not consistency.ok:
        raise VersionMismatchError(consistency)

    prov = provenance or CardProvenance()
    ctx = _CardContext(
        clause=policy_clause,
        duty_person=duty_person or "",
        roster=prov.duty_roster,
        retrieval=prov.policy_retrieval,
    )

    threshold_basis = _threshold_element(judgement, prov.threshold_table, event)

    notify_target = _policy_element(
        "notify_target",
        "通知对象",
        policy_clause.notify_target if policy_clause else "",
        ctx,
        extra=f"当班人 {duty_person}" if duty_person else "",
        with_roster=True,
    )
    deadline = _policy_element(
        "deadline",
        "上报时限",
        policy_clause.deadline_text if policy_clause else "",
        ctx,
        extra=(
            f"{policy_clause.deadline_minutes} 分钟"
            if policy_clause and policy_clause.deadline_minutes
            else ""
        ),
    )
    record_destination = _policy_element(
        "record_destination",
        "记录去向",
        policy_clause.record_destination if policy_clause else "",
        ctx,
    )

    # 待裁决冲突落到要素上：未裁决不算「已解决」。
    elements = _apply_conflicts(
        {
            "threshold_basis": threshold_basis,
            "notify_target": notify_target,
            "deadline": deadline,
            "record_destination": record_destination,
        },
        conflicts,
    )

    return DispositionCard(
        card_id=card_id or make_card_id(
            event, judgement.threshold_row.citation.locator if judgement.threshold_row else ""
        ),
        event=event,
        judgement=judgement,
        threshold_basis=elements["threshold_basis"],
        notify_target=elements["notify_target"],
        deadline=elements["deadline"],
        record_destination=elements["record_destination"],
        created_at=datetime.now(timezone.utc),
        extra_citations=(
            ((policy_clause.citation,) if policy_clause else ()) + tuple(extra_citations)
        ),
        # 卡片随后要推给值班群 —— 装配时生效的配置版本随卡一起走，
        # 事后复核能确定「这张卡是在哪一版配置下装配并推送的」。
        config_version=Config.config_version(),
        provenance=prov,
        conflicts=tuple(conflicts),
    )
