"""多项目合并通知：**一条标本一张卡**（任务 id 34）。

一条标本常同时触发多个危急项目 —— 血钾 6.9、血钠 118 一起越限。
两种做法都不行：

* **一项目一张卡** —— 值班群被刷屏，两条卡还各带各的时限与当班人，
  值班人以为要处理两件事；
* **一标本一张卡但只写一个项目** —— 丢项，另一项的依据没人看得见。

本模块的做法：**同标本、同科室、同一时间窗**内的多个危急项目合并成
**一张**处置卡；卡内**逐项**给出各自的阈值依据行与出处行号，
卡级共享通知对象 / 上报时限 / 记录去向（同一份制度条款、同一个当班人）。

**合并边界**（三条，任一条不满足就不合并）：

1. **标本不同** —— 不是一个人的标本，不该并到一张卡；
2. **科室不同** —— 一张卡只通知一个科室的当班人；
3. **不在同一时间窗** —— 观察时刻相隔超过 ``CRITVAL_MERGE_WINDOW_SECONDS``
   就是两次独立的危急事件，不该并成一条通知。

**缺项语义沿用任务 id 21**：合并后某一项的依据取不到，那一项在卡内标
``missing`` 并写明原因 —— 不用占位文案冒充已解决，其余项照常显示。

**排序可复现**：卡内项目按优先级降序（同分按项目码升序），值班人看到的
"第 1 项"每次都一样。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Mapping, Optional, Sequence

from critval.card.builder import build_policy_elements, build_threshold_element
from critval.card.renderer import STATUS_LABELS, assert_patient_free
from critval.config import Config
from critval.judge.critical import DECISION_CRITICAL
from critval.judge.priority import UrgencyTable, compute_priority, priority_sort_key
from critval.models import (
    CardProvenance,
    CriticalEvent,
    Judgement,
    MergedCardItem,
    MergedDispositionCard,
    PolicyClause,
)

__all__ = [
    "DEFAULT_MERGE_WINDOW_SECONDS",
    "MERGE_WINDOW_CONFIG_KEY",
    "MergeGroup",
    "build_merged_card",
    "group_for_notification",
    "merge_window_seconds",
    "plan_notification_cards",
    "render_merged_markdown",
    "render_merged_text",
]

#: 合并时间窗的默认值（秒）。同标本、间隔在窗口内的危急项目合到一张卡。
DEFAULT_MERGE_WINDOW_SECONDS = 300

#: 合并时间窗的配置键（环境变量）—— 院内可按科室习惯调。
MERGE_WINDOW_CONFIG_KEY = "CRITVAL_MERGE_WINDOW_SECONDS"


def merge_window_seconds(default: int = DEFAULT_MERGE_WINDOW_SECONDS) -> int:
    """当前生效的合并时间窗（秒），取自配置面。"""
    raw = Config.get_env(MERGE_WINDOW_CONFIG_KEY)
    if not raw:
        return default
    try:
        value = int(str(raw).strip())
    except ValueError:
        return default
    return value if value >= 0 else default


@dataclass(frozen=True)
class MergeGroup:
    """一个合并分组（同标本 + 同科室 + 同一时间窗）。"""

    specimen_no: str
    dept_code: str
    window_start: datetime
    window_end: datetime
    events: tuple[CriticalEvent, ...]

    @property
    def size(self) -> int:
        return len(self.events)

    def to_dict(self) -> dict[str, object]:
        return {
            "specimen_no": self.specimen_no,
            "dept_code": self.dept_code,
            "window_start": self.window_start.isoformat() if self.window_start else "",
            "window_end": self.window_end.isoformat() if self.window_end else "",
            "event_count": self.size,
            "event_ids": [event.event_id for event in self.events],
            "item_codes": [event.item_code for event in self.events],
        }


def _observed(event: CriticalEvent) -> Optional[datetime]:
    return getattr(event, "observed_at", None)


def group_for_notification(
    events: Sequence[CriticalEvent],
    window_seconds: int = DEFAULT_MERGE_WINDOW_SECONDS,
) -> tuple[MergeGroup, ...]:
    """把事件按「标本 + 科室 + 时间窗」分组。

    时间窗按**相邻间隔**判定：同标本同科室的一串事件里，相邻两个观察时刻
    相差不超过 ``window_seconds`` 就并进同一组；超过就另起一组。
    （用固定时间桶会在窗口边界把两个相距 1 分钟的结果拆开，不合理。）

    没有观察时刻的事件**各自单独成组** —— 时间窗无从判断时，宁可不合并。
    """
    if not events:
        return ()

    window = max(0, int(window_seconds))
    buckets: dict[tuple[str, str], list[CriticalEvent]] = {}
    for event in events:
        buckets.setdefault((event.specimen_no or "", event.dept_code or ""), []).append(event)

    groups: list[MergeGroup] = []
    for (specimen_no, dept_code), bucket in sorted(buckets.items()):
        timed = [e for e in bucket if _observed(e) is not None]
        untimed = [e for e in bucket if _observed(e) is None]

        timed.sort(key=lambda e: (_observed(e), e.item_code, e.event_id))
        current: list[CriticalEvent] = []
        for event in timed:
            if not current:
                current = [event]
                continue
            previous = _observed(current[-1])
            moment = _observed(event)
            assert previous is not None and moment is not None
            if moment - previous <= timedelta(seconds=window):
                current.append(event)
            else:
                groups.append(_make_group(specimen_no, dept_code, current))
                current = [event]
        if current:
            groups.append(_make_group(specimen_no, dept_code, current))

        for event in untimed:
            groups.append(
                MergeGroup(
                    specimen_no=specimen_no,
                    dept_code=dept_code,
                    window_start=datetime.now(timezone.utc),
                    window_end=datetime.now(timezone.utc),
                    events=(event,),
                )
            )

    groups.sort(key=lambda g: (g.window_start, g.specimen_no, g.dept_code))
    return tuple(groups)


def _make_group(
    specimen_no: str,
    dept_code: str,
    events: Sequence[CriticalEvent],
) -> MergeGroup:
    moments = [m for m in (_observed(e) for e in events) if m is not None]
    start = min(moments) if moments else datetime.now(timezone.utc)
    end = max(moments) if moments else start
    return MergeGroup(
        specimen_no=specimen_no,
        dept_code=dept_code,
        window_start=start,
        window_end=end,
        events=tuple(events),
    )


def _make_merged_card_id(group: MergeGroup, item_codes: Sequence[str]) -> str:
    """合并卡 ID：标本 + 科室 + 窗口起点 + 项目码集合 → 同一个 ID。

    同一组结果重复合并得到同一个 ID（幂等），项目集合变了才是新卡。
    """
    basis = "|".join(
        [
            group.specimen_no,
            group.dept_code,
            group.window_start.isoformat() if group.window_start else "",
            ",".join(sorted(item_codes)),
        ]
    )
    return "card-m" + hashlib.sha256(basis.encode("utf-8")).hexdigest()[:12]


def build_merged_card(
    group: MergeGroup,
    judgements: Mapping[str, Judgement],
    policy_clause: Optional[PolicyClause] = None,
    duty_person: Optional[str] = None,
    *,
    provenance: Optional[CardProvenance] = None,
    urgency_table: Optional[UrgencyTable] = None,
) -> MergedDispositionCard:
    """把一个分组装配成一张合并处置卡。

    卡内项目按优先级降序（同分按项目码升序）排好；每项的阈值依据行与
    出处各归各的，取不到依据的那一项标 ``missing``。
    """
    prov = provenance or CardProvenance()
    items: list[MergedCardItem] = []
    for event in group.events:
        judgement = judgements.get(event.event_id)
        if judgement is None:
            continue
        basis = build_threshold_element(judgement, prov.threshold_table, event)
        priority = compute_priority(judgement, urgency_table)
        items.append(
            MergedCardItem(
                event_id=event.event_id,
                item_code=event.item_code,
                item_name=event.item_name or (
                    judgement.threshold_row.item_name if judgement.threshold_row else ""
                ),
                value=event.value,
                unit=event.unit,
                observed_at=event.observed_at,
                decision=judgement.decision,
                reason=judgement.reason,
                threshold_basis=basis,
                priority=priority,
                judgement=judgement,
            )
        )

    items.sort(key=lambda item: priority_sort_key(item.priority, item.item_code))

    shared = build_policy_elements(policy_clause, duty_person, prov)
    card_id = _make_merged_card_id(group, [item.item_code for item in items])
    return MergedDispositionCard(
        card_id=card_id,
        specimen_no=group.specimen_no,
        dept_code=group.dept_code,
        window_start=group.window_start,
        window_end=group.window_end,
        items=tuple(items),
        notify_target=shared["notify_target"],
        deadline=shared["deadline"],
        record_destination=shared["record_destination"],
        created_at=datetime.now(timezone.utc),
        extra_citations=(policy_clause.citation,) if policy_clause else (),
        config_version=Config.config_version(),
        provenance=prov,
    )


def plan_notification_cards(
    events: Sequence[CriticalEvent],
    judgements: Mapping[str, Judgement],
    policy_clause: Optional[PolicyClause] = None,
    duty_person: Optional[str] = None,
    *,
    provenance: Optional[CardProvenance] = None,
    urgency_table: Optional[UrgencyTable] = None,
    window_seconds: Optional[int] = None,
) -> tuple[MergedDispositionCard, ...]:
    """规划这一批事件要发**几张**通知卡。

    只有判定为危急的项目进通知卡（不危急的只入台账、转人工的不走通知）。
    一条标本的多个危急项目 → 一张卡 —— 这就是「不刷屏也不丢项」。

    返回的每一张卡就是一个**通知单元**：推一次值班群、确认一次、
    回写一次登记系统。
    """
    window = merge_window_seconds() if window_seconds is None else max(0, int(window_seconds))

    critical_events = [
        event
        for event in events
        if (judgements.get(event.event_id) is not None)
        and judgements[event.event_id].decision == DECISION_CRITICAL
    ]
    if not critical_events:
        return ()

    groups = group_for_notification(critical_events, window)
    cards: list[MergedDispositionCard] = []
    for group in groups:
        card = build_merged_card(
            group,
            judgements,
            policy_clause=policy_clause,
            duty_person=duty_person,
            provenance=provenance,
            urgency_table=urgency_table,
        )
        if card.items:
            cards.append(card)
    return tuple(cards)


# ---------------------------------------------------------------------------
# 渲染：合并卡也是一个推送单元，渲染与单张卡同一套口径
# ---------------------------------------------------------------------------


def _item_lines(card: MergedDispositionCard) -> list[str]:
    """卡内逐项（优先级 + 判定 + 依据行 + 出处）。"""
    lines: list[str] = []
    for index, item in enumerate(card.items, start=1):
        band = item.priority.band
        lines.append(
            f"{index}. 【{band}】{item.item_name or '（未命名项目）'}"
            f"（{item.item_code}） {item.value} {item.unit}"
        )
        lines.append(
            f"   - 优先级分值 {item.priority.score}"
            f"（{item.priority.deviation_note}）"
        )
        basis = item.threshold_basis
        if basis.status == "missing":
            lines.append(
                f"   - 阈值依据行：【依据缺失】{basis.note or '原因未记录'}"
            )
        else:
            suffix = "【降级依据】" + basis.note if basis.status == "degraded" else ""
            lines.append(f"   - 阈值依据行：{basis.value}{suffix}")
            if basis.source is not None and basis.source.locator:
                lines.append(f"   - 出处：{basis.source.locator}")
        lines.append(f"   - 判定：{item.reason}")
    return lines


def _shared_lines(card: MergedDispositionCard) -> list[str]:
    lines: list[str] = []
    for element in card.shared_elements:
        if element.status == "missing":
            lines.append(
                f"- {element.label}：【依据缺失】{element.note or '原因未记录'}"
            )
        else:
            suffix = "【降级依据】" + element.note if element.status == "degraded" else ""
            lines.append(f"- {element.label}：{element.value}{suffix}")
    return lines


def render_merged_markdown(card: MergedDispositionCard, confirm_url: str = "") -> str:
    """合并卡的企微 Markdown。

    与单张卡同一套三态口径（依据齐备 / 降级依据 / 依据缺失），逐项给
    自己的阈值依据行与出处行号 —— 合并的是通知单元，不是依据。
    """
    count = len(card.items)
    title = f"**危急值处置卡 · 待确认（{count} 项合并）**" if count > 1 else "**危急值处置卡 · 待确认**"
    lines: list[str] = [title, ""]
    lines.append(f"- 标本号：{card.specimen_no}")
    lines.append(f"- 科室：{card.dept_code or '（消息未带科室）'}")
    lines.append(f"- 观察时间：{card.window_start.isoformat() if card.window_start else ''}")
    lines.append("")
    lines.append("**处置依据（卡级共享）**")
    lines.append("")
    lines.extend(_shared_lines(card))
    lines.append("")
    lines.append(f"**危急项目（按优先级排序，共 {count} 项）**")
    lines.append("")
    lines.extend(_item_lines(card))
    lines.append("")
    if confirm_url:
        lines.append(f"确认回执：{confirm_url}")
    else:
        lines.append("确认回执：请在值班台确认本条处置")

    text = "\n".join(lines)
    assert_patient_free(text)
    return text


def render_merged_text(card: MergedDispositionCard, confirm_url: str = "") -> str:
    """合并卡的离线纯文本版（打印 / 粘工作群）。"""
    count = len(card.items)
    lines: list[str] = [f"危急值处置卡 · 待确认（{count} 项合并）", "═" * 64]
    lines.append(f"标本号   : {card.specimen_no}")
    lines.append(f"科室     : {card.dept_code or '（消息未带科室）'}")
    lines.append(f"观察时间 : {card.window_start.isoformat() if card.window_start else ''}")
    lines.append("─" * 64)
    lines.append("处置依据（卡级共享）")
    for element in card.shared_elements:
        status = STATUS_LABELS.get(element.status, element.status)
        if element.status == "missing":
            lines.append(f"  {element.label} [{status}]")
            lines.append(f"     说明    : 【依据缺失】{element.note or '原因未记录'}")
        else:
            lines.append(f"  {element.label} [{status}]")
            lines.append(f"     值      : {element.value}")
    lines.append("─" * 64)
    lines.append(f"危急项目（按优先级排序，共 {count} 项）")
    for index, item in enumerate(card.items, start=1):
        lines.append(
            f"  {index}. 【{item.priority.band}】"
            f"{item.item_name or item.item_code}（{item.item_code}）"
            f" {item.value} {item.unit}"
        )
        lines.append(f"     优先级  : {item.priority.score}（{item.priority.deviation_note}）")
        basis = item.threshold_basis
        if basis.status == "missing":
            lines.append(f"     依据行  : 【依据缺失】{basis.note or '原因未记录'}")
        else:
            lines.append(f"     依据行  : {basis.value}")
            if basis.source is not None and basis.source.locator:
                lines.append(f"     出处    : {basis.source.locator}")
    lines.append("─" * 64)
    if confirm_url:
        lines.append(f"确认回执 : {confirm_url}")
    else:
        lines.append("确认回执 : 请在值班台确认本条处置")

    text = "\n".join(lines)
    assert_patient_free(text)
    return text


def summarize_cards(cards: Sequence[MergedDispositionCard]) -> dict[str, object]:
    """一批合并卡的摘要（卡数 / 项数 / 分档计数）—— 机器可读输出用。"""
    bands: dict[str, int] = {}
    item_total = 0
    for card in cards:
        for item in card.items:
            item_total += 1
            bands[item.priority.band] = bands.get(item.priority.band, 0) + 1
    return {
        "cards": len(cards),
        "items": item_total,
        "bands": bands,
        "card_ids": [card.card_id for card in cards],
        "specimens": [card.specimen_no for card in cards],
    }


def card_priority_scores(card: MergedDispositionCard) -> list[Decimal]:
    """卡内各项目的优先级分值（供断言与报表）。"""
    return [item.priority.score for item in card.items]
