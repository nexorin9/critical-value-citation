"""当班人解析与通知对象唯一化（任务 id 41）。

排班系统给回来的东西有三个特点，处置卡必须都能扛住：

1. **多班次** —— 一个科室一天分好几班，观察时刻落在哪一班要算清楚
   （夜班 ``20:00 → 08:00`` 跨零点）；
2. **多角色** —— 主责 / 备班 / 科主任 / 值班长 / 普通值班护士都在名单里，
   但通知对象**只能有一个**；
3. **空当班人** —— 有人没排班、有人休假、整个科室今晚没人值班。

本模块把这三件事收成**一个通知对象** + **一条可追问的来源**：

============================  =============================================
情形                          结论
============================  =============================================
窗口内有人在班                按角色优先级择一（主责 > 备班 > 科主任 > 在班人）
窗口内无人、名单里有值班长      科室值班长兜底（来源标为「兜底」）
窗口内无人、名单里也没有值班长  **没有通知对象** → 通知对象标缺项，转人工
名单来自过期缓存               **转人工** —— 不拿过期名单冒充当前当班人
============================  =============================================

**缓存过期就是转人工**，不设例外。危急值宁可让人打电话找人，也不能
悄悄通知一个三小时前已经下班的人。

**兜底人选只在「无人在班」时启用**：值班长是「叫不到人时的兜底」，
不是在场就优先找他。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable, Optional

from critval.card.provenance import (
    ORIGIN_CACHE,
    ORIGIN_FALLBACK,
    ORIGIN_LIVE,
    ORIGIN_NO_DUTY,
    ORIGIN_UNAVAILABLE,
    REASON_RESOLVED,
    REASON_ROSTER_FALLBACK,
    REASON_ROSTER_FROM_CACHE,
    REASON_ROSTER_NO_DUTY_PERSON,
    REASON_ROSTER_UNAVAILABLE,
    roster_provenance,
)
from critval.models import Provenance
from critval.roster.cache import DEFAULT_CACHE_TTL_SECONDS, CachedRoster
from critval.roster.roles import (
    ROLE_TIER_TEXT,
    is_duty_leader,
    role_tier,
)
from critval.roster.shifts import shift_window_of, wall_clock
from critval.roster.types import (
    SELECTION_DUTY_LEADER_FALLBACK,
    SELECTION_NONE,
    SELECTION_ON_DUTY,
    DutyPerson,
    NotifyTarget,
)

__all__ = [
    "resolve_notify_target",
    "target_provenance",
]


def _sort_key(person: DutyPerson) -> tuple[int, str, str, str]:
    """同层级内的稳定排序键。

    排序必须**可复现**：同样的名单、同样的时刻，两次解析给出同一个人，
    不能因为字典顺序或集合顺序抖动。工号优先（同一科室内唯一），没有
    工号才按姓名。
    """
    return (
        role_tier(person.role),
        person.staff_id or "",
        person.name or "",
        person.shift_start or "",
    )


def resolve_notify_target(
    people: Iterable[DutyPerson],
    *,
    dept_code: str = "",
    at: Optional[datetime] = None,
    origin: str = ORIGIN_LIVE,
    as_of: str = "",
    cache_entry: Optional[CachedRoster] = None,
    cache_ttl_seconds: float = DEFAULT_CACHE_TTL_SECONDS,
    now: Optional[datetime] = None,
    note: str = "",
) -> NotifyTarget:
    """把一份当班名单解析成**唯一**的通知对象。

    :param people: 名单（读回来的当班记录）；
    :param dept_code: 科室代码（写进结果，便于审计）；
    :param at: 判定时刻（墙钟）。默认取当前时间；
    :param origin: 名单来源（实时 / 缓存 / 不可用）；
    :param as_of: 名单取数时刻（缓存来源时必带）；
    :param cache_entry: 缓存条目（来源为缓存时传入，用于**复核 TTL**）；
    :param cache_ttl_seconds: 缓存有效期（秒）；
    :param now: 复核 TTL 用的当前时刻（默认取 UTC 当前时间）；
    :param note: 上游给的说明（例如排班系统报了什么错）。
    """
    moment = at or datetime.now()
    stamp = wall_clock(moment).strftime("%Y-%m-%d %H:%M")
    roster = tuple(people)

    # —— 第 0 关：上游根本不可用 ——
    if origin == ORIGIN_UNAVAILABLE:
        return NotifyTarget(
            person=None,
            origin=ORIGIN_UNAVAILABLE,
            selection=SELECTION_NONE,
            reason=REASON_ROSTER_UNAVAILABLE,
            as_of=as_of,
            dept_code=dept_code,
            at=stamp,
            note=note or "排班系统不可用，未取到当班人，转人工",
            requires_human=True,
        )

    # —— 第 1 关：缓存过期就不作数 ——
    # 名单来自缓存时，这里**再复核一次**有效期：不能只信「来源标签写着
    # cache」就拿来用 —— 名单从缓存里取出来之后可能又过了一会儿。
    if origin == ORIGIN_CACHE:
        if cache_entry is None:
            # 没有缓存条目却标了来源是缓存 —— 拿不到取数时刻就无法复核，
            # 按「不可用」处理（宁可转人工，不拿来路不明的名单通知人）。
            return NotifyTarget(
                person=None,
                origin=ORIGIN_UNAVAILABLE,
                selection=SELECTION_NONE,
                reason=REASON_ROSTER_UNAVAILABLE,
                as_of=as_of,
                dept_code=dept_code,
                at=stamp,
                note=(
                    "当班名单来源标为缓存但缺少缓存条目，无法复核有效期，"
                    "转人工"
                ),
                requires_human=True,
            )
        check_at = now or datetime.now(timezone.utc)
        if cache_entry.expired(cache_ttl_seconds, check_at):
            return NotifyTarget(
                person=None,
                origin=ORIGIN_UNAVAILABLE,
                selection=SELECTION_NONE,
                reason=REASON_ROSTER_UNAVAILABLE,
                as_of=cache_entry.as_of_text(),
                dept_code=dept_code,
                at=stamp,
                note=(
                    f"当班名单缓存已过期（取数时刻 {cache_entry.as_of_text()}，"
                    f"有效期 {int(cache_ttl_seconds)} 秒），"
                    "不拿过期名单冒充当前当班人，转人工"
                ),
                requires_human=True,
            )

    # —— 第 2 关：本班次窗口内在班的人 ——
    on_duty: list[DutyPerson] = []
    off_duty: list[DutyPerson] = []
    for person in roster:
        if shift_window_of(person).covers(moment):
            on_duty.append(person)
        else:
            off_duty.append(person)

    if on_duty:
        ordered = tuple(sorted(on_duty, key=_sort_key))
        chosen = ordered[0]
        window = shift_window_of(chosen)
        tier = role_tier(chosen.role)
        tier_text = ROLE_TIER_TEXT.get(tier, ROLE_TIER_TEXT[3])
        note = (
            f"{stamp} 在班人员 {len(ordered)} 人，按角色优先级取「{tier_text}」"
        )
        if len(ordered) > 1:
            others = "、".join(
                f"{p.name or p.staff_id}（{ROLE_TIER_TEXT.get(role_tier(p.role), '在班人')}）"
                for p in ordered[1:]
            )
            note += f"；未选：{others}"
        if window.incomplete:
            note += f"；{window.describe()}"
        reason = REASON_ROSTER_FROM_CACHE if origin == ORIGIN_CACHE else REASON_RESOLVED
        if origin == ORIGIN_CACHE:
            note += f"；名单取自缓存（{as_of}）"
        return NotifyTarget(
            person=chosen,
            origin=origin,
            selection=SELECTION_ON_DUTY,
            reason=reason,
            as_of=as_of,
            dept_code=dept_code,
            at=stamp,
            note=note,
            candidates=ordered,
            window_incomplete=window.incomplete,
        )

    # —— 第 3 关：无人在班 → 科室值班长兜底 ——
    leaders = [p for p in roster if is_duty_leader(p.role)]
    if leaders:
        leader = sorted(leaders, key=_sort_key)[0]
        detail = f"科室值班长 {leader.label()}"
        if origin == ORIGIN_CACHE:
            detail += f"（名单取自缓存 {as_of}）"
        note = (
            f"{stamp} 本班次窗口内无人在班（名单 {len(roster)} 人），"
            f"改由科室值班长兜底：{leader.label()}"
        )
        return NotifyTarget(
            person=leader,
            origin=ORIGIN_FALLBACK,
            selection=SELECTION_DUTY_LEADER_FALLBACK,
            reason=REASON_ROSTER_FALLBACK,
            as_of=as_of,
            dept_code=dept_code,
            at=stamp,
            note=note,
            candidates=(),
            duty_leader=leader,
        )

    # —— 第 4 关：既无人在班，也没有值班长可兜底 ——
    detail = (
        f"{stamp} 本班次窗口内无人在班（名单 {len(roster)} 人），"
        "且名单中没有科室值班长可兜底，转人工"
    )
    if off_duty:
        detail += "；在班窗口外：" + "、".join(
            f"{p.name or p.staff_id}（{shift_window_of(p).describe()}）"
            for p in off_duty
        )
    return NotifyTarget(
        person=None,
        origin=ORIGIN_NO_DUTY,
        selection=SELECTION_NONE,
        reason=REASON_ROSTER_NO_DUTY_PERSON,
        as_of=as_of,
        dept_code=dept_code,
        at=stamp,
        note=detail,
        candidates=(),
        requires_human=True,
    )


def target_provenance(target: NotifyTarget) -> Provenance:
    """把解析结果转成处置卡上的**来源标注**。

    卡片头部那句「当班名单来源：……」由它决定 —— 值班人一眼能看出
    手里这份通知对象是实时取到、缓存凑合、值班长兜底，还是压根没人。
    """
    if target.origin == ORIGIN_CACHE:
        return roster_provenance(ORIGIN_CACHE, detail=target.label(), as_of=target.as_of)
    if target.origin == ORIGIN_FALLBACK:
        detail = f"科室值班长 {target.label()}"
        if target.as_of:
            detail += f"（名单取自缓存 {target.as_of}）"
        return roster_provenance(ORIGIN_FALLBACK, detail=detail, as_of=target.as_of)
    if target.origin == ORIGIN_NO_DUTY:
        return roster_provenance(
            ORIGIN_NO_DUTY,
            detail="本班次窗口内无人在班，且名单中无科室值班长可兜底",
            as_of=target.as_of,
        )
    if target.origin == ORIGIN_UNAVAILABLE:
        return roster_provenance(ORIGIN_UNAVAILABLE, detail=target.note, as_of=target.as_of)
    return roster_provenance(
        ORIGIN_LIVE,
        detail=target.label(),
        as_of=target.as_of,
    )
