"""当班名单缓存（排班系统不可用时的降级依据 · 任务 id 11）。

排班系统是**当班人的权威来源**，但它也会挂。缓存不是「另一个权威」——
它只是把最近一次成功读到的名单**连同取数时刻**留下来，好让危急值在
排班系统不可用时还能通知到人，同时在卡片上**明说这份名单有多旧**。

三条硬规则：

1. **缓存必须带时间戳** —— 没有取数时刻的缓存不叫降级，叫编造。卡片上
   写「当班名单来源：缓存（2026-10-29 21:20）」就是这条规则的落地。
2. **缓存过期即失效** —— 超过 ``ttl_seconds`` 就不再拿来当通知依据，
   事件转人工（``transferred_to_human``），由人接手找当班人。
3. **过期条目不静默删** —— 留着能被审计看到「什么时候取过、什么时候过期」。

缓存落在**运行期本地文件**（默认 ``data/roster_cache.json``，已被
``.gitignore`` 排除）—— 它是可再生数据，不是材料，不进仓库。
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Mapping, Optional

from critval.roster.types import DutyPerson

__all__ = [
    "DEFAULT_CACHE_PATH",
    "DEFAULT_CACHE_TTL_SECONDS",
    "CachedRoster",
    "RosterCache",
]

#: 缓存默认落盘位置（运行期数据，已 gitignore）。
DEFAULT_CACHE_PATH = Path("data/roster_cache.json")

#: 缓存有效期（秒）。超过这个时长，缓存里的名单不再作为通知依据。
#:
#: 900 秒 = 15 分钟：一个班次（通常 8–12 小时）里缓存要能顶住排班系统
#: 短暂抽风；但顶不过一整个班次 —— 名单变了就宁可转人工。
DEFAULT_CACHE_TTL_SECONDS = 900


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class CachedRoster:
    """一条缓存的当班名单（带取数时刻）。"""

    dept_code: str
    people: tuple[DutyPerson, ...]
    fetched_at: datetime

    def age_seconds(self, now: Optional[datetime] = None) -> float:
        """这份名单有多旧（秒）。"""
        moment = now or _now()
        return max(0.0, (moment - self.fetched_at).total_seconds())

    def expired(self, ttl_seconds: float, now: Optional[datetime] = None) -> bool:
        """是否已过期（超过有效期就不能再当通知依据）。"""
        return self.age_seconds(now) > float(ttl_seconds)

    def as_of_text(self) -> str:
        """卡片上写的取数时刻（``2026-10-29 21:20``）。"""
        return self.fetched_at.strftime("%Y-%m-%d %H:%M")

    def to_dict(self) -> dict[str, object]:
        return {
            "dept_code": self.dept_code,
            "fetched_at": self.fetched_at.isoformat(),
            "people": [person.to_dict() for person in self.people],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "CachedRoster":
        raw_people = data.get("people") or []
        people = tuple(
            DutyPerson.from_dict(item)
            for item in raw_people
            if isinstance(item, Mapping)
        )
        fetched = str(data.get("fetched_at", "") or "")
        try:
            moment = datetime.fromisoformat(fetched)
        except ValueError:
            moment = _now()
        if moment.tzinfo is None:
            # 历史缓存里存的是不带时区的时刻 —— 按本地墙钟理解。
            moment = moment.astimezone()
        return cls(
            dept_code=str(data.get("dept_code", "") or ""),
            people=people,
            fetched_at=moment,
        )


class RosterCache:
    """当班名单缓存（按科室分键）。

    * 落盘：``data/roster_cache.json``（可换成别处，构造时给 ``path``）；
    * 有效期：``ttl_seconds``，默认 900 秒；
    * 写盘：**原子写**（临时文件 + ``os.replace``）—— 进程被 kill 时不会
      留下一份半截的 JSON。
    """

    def __init__(
        self,
        path: Optional[Path | str] = None,
        *,
        ttl_seconds: float = DEFAULT_CACHE_TTL_SECONDS,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self.path = Path(path) if path is not None else DEFAULT_CACHE_PATH
        self.ttl_seconds = float(ttl_seconds)
        self._clock = clock or _now
        self._entries: dict[str, CachedRoster] = {}
        self._loaded = False

    # ---- 取 / 存 -----------------------------------------------------------
    def get(self, dept_code: str) -> Optional[CachedRoster]:
        """取某个科室的缓存（**不判断过期** —— 过期与否由调用方决定）。"""
        self._ensure_loaded()
        return self._entries.get(str(dept_code or "").strip())

    def put(
        self,
        dept_code: str,
        people: Iterable[DutyPerson],
        at: Optional[datetime] = None,
    ) -> CachedRoster:
        """写入一次成功读取的名单（**带取数时刻**）。"""
        self._ensure_loaded()
        key = str(dept_code or "").strip()
        moment = at or self._clock()
        if moment.tzinfo is None:
            # 不带时区的时刻按**本地墙钟**理解（院里的挂钟），换算成带
            # 时区的时刻 —— 直接当 UTC 存会让缓存年龄算错（东八区会把
            # 缓存算成「未来取的」，于是永远不过期）。
            moment = moment.astimezone()
        entry = CachedRoster(dept_code=key, people=tuple(people), fetched_at=moment)
        self._entries[key] = entry
        self._save()
        return entry

    def entries(self) -> tuple[CachedRoster, ...]:
        """全部缓存条目（按科室名排序，便于审计与复现）。"""
        self._ensure_loaded()
        return tuple(self._entries[key] for key in sorted(self._entries))

    def expired_entries(self, now: Optional[datetime] = None) -> tuple[CachedRoster, ...]:
        """已过期但仍留着的条目（审计用）。"""
        moment = now or self._clock()
        return tuple(e for e in self.entries() if e.expired(self.ttl_seconds, moment))

    def clear(self) -> None:
        """清空缓存（发布前清理 / 测试用）。"""
        self._entries = {}
        self._loaded = True
        self._save()

    # ---- 落盘 -------------------------------------------------------------
    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            raw = self.path.read_text(encoding="utf-8")
        except (FileNotFoundError, OSError):
            return
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            # 缓存文件坏了 —— 从空缓存起步，不让一条坏缓存卡住危急值。
            return
        entries = data.get("entries") if isinstance(data, Mapping) else None
        if not isinstance(entries, Mapping):
            return
        for dept_code, payload in entries.items():
            if isinstance(payload, Mapping):
                self._entries[str(dept_code)] = CachedRoster.from_dict(payload)

    def _save(self) -> None:
        payload = {
            "entries": {
                key: entry.to_dict() for key, entry in sorted(self._entries.items())
            }
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle, tmp_name = tempfile.mkstemp(
                dir=str(self.path.parent), prefix=".roster_cache-", suffix=".tmp"
            )
            try:
                with os.fdopen(handle, "w", encoding="utf-8") as stream:
                    json.dump(payload, stream, ensure_ascii=False, indent=2)
                os.replace(tmp_name, self.path)
            except BaseException:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
                raise
        except OSError:
            # 缓存写不进去（只读目录等）—— 不影响主流程，缓存只是降级手段。
            return
