"""角色优先级表（任务 id 41 · 通知对象唯一化）。

通知对象必须**唯一** —— 一张处置卡只能有一个「该找谁」。当一个科室同一
时刻有多人在班时，按这张表择一：

====================  ==================================================
层级                   含义
====================  ==================================================
``0`` 主责              本班次的主责人 —— 危急值第一个该找的人
``1`` 备班              主责不在时的第二顺位
``2`` 科主任            主责与备班都缺时的第三顺位
``3`` 一般在班人         名单里没有明确角色的人（值班护士 / 值班技师等）
``4`` 科室值班长         **兜底人选** —— 只在「本班次窗口内无人在班」时启用
====================  ==================================================

**为什么科室值班长排在一般在班人之后**：值班长是「叫不到人时的兜底」，
不是「在场就优先找他」。当班的人在场时先找当班的人；只有在**本班次
窗口内一个在班的人都没有**时，才启用科室值班长兜底。

**为什么科主任排在一般在班人之前**：名单里出现科主任且其在班，说明这个
科室的当班责任人是科主任本人（常见于科主任带班的小科室 / 夜班），按
「谁负责」通知科主任是说得通的。

角色名按**子串**匹配（中英文都认），因此院内叫法不同也不影响：

* 主责：``主责`` / ``责任班`` / ``主值班`` / ``一级值班`` / ``责任岗``；
* 备班：``备班`` / ``备值班`` / ``二级值班`` / ``备份``；
* 科主任：``科主任`` / ``科室主任`` / ``科负责人`` / ``科室负责人``；
* 科室值班长：``值班长`` / ``总值班`` / ``值班领导`` / ``值班主任``。

匹配**从上往下**走，先匹配上的层级生效 —— 所以别名表里同一层级的写法
互不重叠（``科室主任`` 不会误命中 ``科主任``，``总值班`` 不会误命中
``值班长``）。
"""

from __future__ import annotations

__all__ = [
    "ROLE_ALIASES",
    "ROLE_TIER_BACKUP",
    "ROLE_TIER_DIRECTOR",
    "ROLE_TIER_DUTY_LEADER",
    "ROLE_TIER_GENERIC",
    "ROLE_TIER_PRIMARY",
    "is_duty_leader",
    "role_tier",
    "role_tier_text",
]

#: 主责 —— 危急值第一个该找的人。
ROLE_TIER_PRIMARY = 0
#: 备班 —— 主责不在时的第二顺位。
ROLE_TIER_BACKUP = 1
#: 科主任 —— 第三顺位。
ROLE_TIER_DIRECTOR = 2
#: 一般在班人 —— 名单里没有明确角色的人。
ROLE_TIER_GENERIC = 3
#: 科室值班长 —— 兜底人选（只在无人在班时启用）。
ROLE_TIER_DUTY_LEADER = 4

#: 层级 → 人话（卡片与 CLI 用）。
ROLE_TIER_TEXT: dict[int, str] = {
    ROLE_TIER_PRIMARY: "主责",
    ROLE_TIER_BACKUP: "备班",
    ROLE_TIER_DIRECTOR: "科主任",
    ROLE_TIER_GENERIC: "在班人",
    ROLE_TIER_DUTY_LEADER: "科室值班长",
}

#: 层级 → 角色别名（**自上而下匹配，先命中先生效**）。
ROLE_ALIASES: tuple[tuple[int, tuple[str, ...]], ...] = (
    (
        ROLE_TIER_PRIMARY,
        ("主责", "责任班", "责任岗", "主值班", "一级值班", "主班", "primary"),
    ),
    (
        ROLE_TIER_BACKUP,
        ("备班", "备值班", "二级值班", "备份", "备班岗", "backup"),
    ),
    (
        ROLE_TIER_DIRECTOR,
        ("科主任", "科室主任", "科负责人", "科室负责人", "director"),
    ),
    (
        ROLE_TIER_DUTY_LEADER,
        ("值班长", "值班主任", "总值班", "值班领导", "duty_leader", "on-call"),
    ),
)

#: 层级文本的公开入口。
role_tier_text = ROLE_TIER_TEXT


def role_tier(role: str) -> int:
    """一个岗位名落在哪一层（认不出来就是「一般在班人」）。"""
    text = str(role or "").strip()
    if not text:
        return ROLE_TIER_GENERIC
    lowered = text.lower()
    for tier, aliases in ROLE_ALIASES:
        for alias in aliases:
            if alias.lower() in lowered:
                return tier
    return ROLE_TIER_GENERIC


def is_duty_leader(role: str) -> bool:
    """这个岗位是不是科室值班长（兜底人选）。"""
    return role_tier(role) == ROLE_TIER_DUTY_LEADER
