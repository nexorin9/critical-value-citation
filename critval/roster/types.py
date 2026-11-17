"""当班人数据模型（任务 id 11 · 排班系统读通道）。

排班系统是**当班人的权威来源**。本模块只放**与厂商无关的结构**：

* :class:`DutyPerson` —— 一条当班记录（姓名 / 工号 / 岗位 / 班次起止）；
* :func:`parse_roster_payload` —— 把排班系统返回的 JSON 读成当班人列表。

**为什么不直接吃原始 JSON**：排班系统的响应外壳各家不一样（顶层数组、
``data`` 包装、``duty`` / ``roster`` / ``persons`` 包装都见过），字段名也有
中文的、有英文的。把它归一在这一层，下游（处置卡 / 回写）就只认一份
结构 —— 换一家排班系统只改这一层。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Optional

__all__ = [
    "DutyPerson",
    "FIELD_ALIASES",
    "LIST_KEYS",
    "NotifyTarget",
    "SELECTION_DUTY_LEADER_FALLBACK",
    "SELECTION_NONE",
    "SELECTION_ON_DUTY",
    "parse_roster_payload",
]


#: 人员字段别名 → 规范字段名。中文键优先（国内排班系统常见），英文键兜底。
FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "name": ("姓名", "name", "duty_name", "person_name", "employee_name", "staff_name"),
    "staff_id": ("工号", "staff_id", "staff_no", "employee_no", "emp_no", "job_number"),
    "role": ("岗位", "role", "title", "position", "duty_role", "职务"),
    "shift_start": (
        "shift_start",
        "shift_from",
        "on_duty_from",
        "start",
        "开始时间",
        "班次开始",
    ),
    "shift_end": (
        "shift_end",
        "shift_to",
        "on_duty_to",
        "end",
        "结束时间",
        "班次结束",
    ),
    "dept_code": ("dept_code", "dept", "department", "科室", "科室代码"),
}

#: 响应里「人在哪」的包装键。命中第一个就用它；都没有就按顶层数组读。
LIST_KEYS: tuple[str, ...] = (
    "data",
    "duty",
    "duties",
    "roster",
    "persons",
    "people",
    "list",
    "items",
    "result",
)


def _pick(payload: Mapping[str, Any], field_name: str) -> str:
    """按别名表从一条载荷里取字段值（取不到返回空串）。"""
    for alias in FIELD_ALIASES.get(field_name, ()):
        if alias in payload:
            value = payload.get(alias)
            if value not in (None, ""):
                return str(value).strip()
    return ""


@dataclass(frozen=True)
class DutyPerson:
    """排班系统里的一条当班记录。

    只记**当班人是谁、什么时候在班**。不落患者信息 —— 这一层与患者无关。
    """

    name: str
    staff_id: str = ""
    role: str = ""
    shift_start: str = ""
    shift_end: str = ""
    dept_code: str = ""
    #: 对端多给而本产品不认的字段，原样留着（不静默丢掉）。
    extra: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "DutyPerson":
        """从一条 JSON 对象读出当班人。"""
        data = dict(payload or {})
        known = {alias for aliases in FIELD_ALIASES.values() for alias in aliases}
        extra = {k: v for k, v in data.items() if k not in known}
        return cls(
            name=_pick(data, "name"),
            staff_id=_pick(data, "staff_id"),
            role=_pick(data, "role"),
            shift_start=_pick(data, "shift_start"),
            shift_end=_pick(data, "shift_end"),
            dept_code=_pick(data, "dept_code"),
            extra=extra,
        )

    def label(self) -> str:
        """人话标签（卡片与回写载荷里的 ``duty_person``）。

        姓名 + 岗位 + 工号 + 班次，能拼上什么拼什么 —— 拼不上的不硬凑。
        """
        parts = [self.name] if self.name else []
        if self.role:
            parts.append(self.role)
        if self.staff_id:
            parts.append(f"工号 {self.staff_id}")
        if self.shift_start and self.shift_end:
            parts.append(f"班次 {self.shift_start}-{self.shift_end}")
        elif self.shift_start or self.shift_end:
            parts.append(f"班次 {self.shift_start or self.shift_end}")
        return " · ".join(parts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "staff_id": self.staff_id,
            "role": self.role,
            "shift_start": self.shift_start,
            "shift_end": self.shift_end,
            "dept_code": self.dept_code,
            "extra": dict(self.extra),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "DutyPerson":
        """从缓存文件读回一条当班人。"""
        return cls(
            name=str(data.get("name", "") or ""),
            staff_id=str(data.get("staff_id", "") or ""),
            role=str(data.get("role", "") or ""),
            shift_start=str(data.get("shift_start", "") or ""),
            shift_end=str(data.get("shift_end", "") or ""),
            dept_code=str(data.get("dept_code", "") or ""),
            extra=dict(data.get("extra", {}) or {}),
        )


def _extract_entries(payload: Any) -> list[Mapping[str, Any]]:
    """把响应体里「人在哪」找出来。"""
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, Mapping)]
    if isinstance(payload, Mapping):
        for key in LIST_KEYS:
            inner = payload.get(key)
            if isinstance(inner, list):
                return [item for item in inner if isinstance(item, Mapping)]
        # 顶层就是一条记录（只有一个当班人）。
        if any(alias in payload for aliases in FIELD_ALIASES.values() for alias in aliases):
            return [payload]
    return []


def parse_roster_payload(payload: Any) -> tuple[DutyPerson, ...]:
    """排班系统响应 → 当班人列表。

    **读不出人不是错误**：空列表是合法结论（这个科室这个班次确实没人排班，
    或者对端给的是另一种结构）。调用方据此把「通知对象」标成缺项 ——
    不能拿一个空名单当「有当班人」。
    """
    entries = _extract_entries(payload)
    people: list[DutyPerson] = []
    for entry in entries:
        person = DutyPerson.from_payload(entry)
        if not person.name and not person.staff_id:
            # 一条既没姓名也没工号的记录 —— 对端给的不是当班人，跳过。
            continue
        people.append(person)
    return tuple(people)


def pick_duty_person(
    people: Iterable[DutyPerson],
    *,
    at: Optional[str] = "",
) -> Optional[DutyPerson]:
    """从当班人列表里挑一个通知对象。

    **本任务只保证通道通**（读得到、解析得出、缓存可降级）。择人细则
    （班次窗口判定、唯一化、兜底值班长）由任务 id 41 收口；这里只做
    「取第一条有姓名的」这一保守默认，并在列表为空时返回 ``None`` ——
    让调用方把「通知对象」标成缺项，而不是随便挑一个。
    """
    for person in people:
        if person.name:
            return person
    return None


# —— 通知对象的择人结果（任务 id 41）——

#: 在班窗口内命中，按角色优先级择一。
SELECTION_ON_DUTY = "on_duty"
#: 本班次窗口内无人在班，改由科室值班长兜底。
SELECTION_DUTY_LEADER_FALLBACK = "duty_leader_fallback"
#: 没有任何通知对象（名单里也没有科室值班长可兜底）。
SELECTION_NONE = "none"


@dataclass(frozen=True)
class NotifyTarget:
    """**唯一**的通知对象，以及它是怎么选出来的。

    通知对象必须唯一且可追问来源 —— 所以除了选中的那一个，还留着
    ``candidates``（当时在班、被比较过的人），事后能回答
    「为什么不选他」。

    字段口径：

    * ``person`` —— 选中的当班人（``None`` = 没有通知对象）；
    * ``origin`` —— 这份名单的来源性质（实时 / 缓存 / 兜底 / 无人在班 /
      上游不可用），与卡片上的来源标注一一对应；
    * ``selection`` —— 择人方式（在班择一 / 值班长兜底 / 没有）；
    * ``reason`` —— **可被机器解析**的原因码，值班台与台账按它分流；
    * ``as_of`` —— 名单取数时刻（缓存来源时必带）；
    * ``note`` —— 人话说明（为什么是他 / 为什么是兜底）。
    """

    person: Optional[DutyPerson] = None
    origin: str = ""
    selection: str = SELECTION_NONE
    reason: str = ""
    as_of: str = ""
    dept_code: str = ""
    at: str = ""
    note: str = ""
    candidates: tuple[DutyPerson, ...] = ()
    duty_leader: Optional[DutyPerson] = None
    requires_human: bool = False
    window_incomplete: bool = False

    @property
    def unique(self) -> bool:
        """通知对象唯一（至多一个人）。"""
        return self.person is None or isinstance(self.person, DutyPerson)

    @property
    def resolved(self) -> bool:
        """是否拿到了可通知的人。"""
        return self.person is not None

    def label(self) -> str:
        """通知对象的人话标签（没有人时返回空串，不编）。"""
        return self.person.label() if self.person else ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "person": self.person.to_dict() if self.person else None,
            "label": self.label(),
            "origin": self.origin,
            "selection": self.selection,
            "reason": self.reason,
            "as_of": self.as_of,
            "dept_code": self.dept_code,
            "at": self.at,
            "note": self.note,
            "candidates": [person.to_dict() for person in self.candidates],
            "duty_leader": self.duty_leader.to_dict() if self.duty_leader else None,
            "requires_human": self.requires_human,
            "window_incomplete": self.window_incomplete,
            "unique": self.unique,
        }
