"""排班对接层：当班人权威读取、择人与降级标注。

* ``critval.roster.types`` —— 当班人数据模型与厂商无关的响应解析；
* ``critval.roster.shifts`` —— 班次窗口解析与在班判定（含跨零点夜班）；
* ``critval.roster.roles`` —— 角色优先级表（通知对象唯一化用）；
* ``critval.roster.cache`` —— 带时间戳的当班名单缓存（降级依据）；
* ``critval.roster.resolve`` —— 择人：窗口判定 → 角色优先级 → 值班长兜底；
* ``critval.roster.client`` —— 排班系统出站客户端（真实 REST 调用）。
"""

from critval.roster.cache import (
    DEFAULT_CACHE_TTL_SECONDS,
    CachedRoster,
    RosterCache,
)
from critval.roster.client import (
    ROSTER_PATH,
    RosterClient,
    RosterError,
    RosterResult,
)
from critval.roster.resolve import resolve_notify_target, target_provenance
from critval.roster.roles import (
    ROLE_ALIASES,
    ROLE_TIER_BACKUP,
    ROLE_TIER_DIRECTOR,
    ROLE_TIER_DUTY_LEADER,
    ROLE_TIER_GENERIC,
    ROLE_TIER_PRIMARY,
    is_duty_leader,
    role_tier,
)
from critval.roster.shifts import (
    ShiftWindow,
    on_duty_at,
    parse_shift_time,
    shift_window_of,
)
from critval.roster.types import (
    SELECTION_DUTY_LEADER_FALLBACK,
    SELECTION_NONE,
    SELECTION_ON_DUTY,
    DutyPerson,
    NotifyTarget,
    parse_roster_payload,
    pick_duty_person,
)

__all__ = [
    "DEFAULT_CACHE_TTL_SECONDS",
    "ROLE_ALIASES",
    "ROLE_TIER_BACKUP",
    "ROLE_TIER_DIRECTOR",
    "ROLE_TIER_DUTY_LEADER",
    "ROLE_TIER_GENERIC",
    "ROLE_TIER_PRIMARY",
    "ROSTER_PATH",
    "SELECTION_DUTY_LEADER_FALLBACK",
    "SELECTION_NONE",
    "SELECTION_ON_DUTY",
    "CachedRoster",
    "DutyPerson",
    "NotifyTarget",
    "RosterCache",
    "RosterClient",
    "RosterError",
    "RosterResult",
    "ShiftWindow",
    "is_duty_leader",
    "on_duty_at",
    "parse_roster_payload",
    "parse_shift_time",
    "pick_duty_person",
    "resolve_notify_target",
    "role_tier",
    "shift_window_of",
    "target_provenance",
]
