"""班次窗口解析与在班判定（任务 id 41 · 当班人解析）。

排班系统给的班次起止，形态五花八门：

* ``08:00`` / ``08:00:00`` —— 只有时刻；
* ``2026-10-29 08:00`` —— 带日期。日期**只当噪声**：窗口按「一天中的几点」
  反复生效，夜班跨日（``20:00 → 08:00``）靠跨零点规则处理，不靠日期；
* ``202610290800`` —— 紧凑写法；
* ``24:00`` —— 「到当天结束」的特殊写法（白班 08:00–24:00）。

**跨零点班次**是本模块的核心：``20:00 → 08:00`` 的夜班，观察时刻落在
``23:40`` 或 ``03:10`` 都要算在班；落在 ``12:00`` 就不在班。

**判不出「不在班」时按在班处理**：只有一端时刻（班次起止只写了一半）时，
班长未知 —— 可能是 8 小时班，也可能是跨夜班。危急值宁可多通知一个可能
已经下班的人，也不能通知不到人；但这种情况会在解析结果里标
``incomplete``，让卡片上的人看见「班次时间不完整」。

判定用的是**墙钟时间**（医院所在时区的挂钟），不是 UTC —— 班次表上写的
``08:00`` 就是院里早上八点。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, time
from typing import Optional

__all__ = [
    "ShiftWindow",
    "on_duty_at",
    "parse_shift_time",
    "shift_window_of",
    "wall_clock",
]

#: 班次起止的书写形态（按尝试顺序，先匹配到的先生效）。
_TIME_FORMATS: tuple[str, ...] = (
    "%H:%M",
    "%H:%M:%S",
    "%H%M",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%dT%H:%M:%S",
    "%Y/%m/%d %H:%M",
    "%Y/%m/%d %H:%M:%S",
    "%Y%m%d%H%M",
    "%Y%m%d%H%M%S",
)

#: ``24:00`` / ``24:00:00`` —— 班次到当天结束（源表常用写法）。
_DAY_END_PATTERN = re.compile(r"^24(:00)?(:00)?$")


def wall_clock(moment: datetime) -> datetime:
    """把一个时刻折成**墙钟**（去掉时区）。

    带时区的时刻按本机时区换算成挂钟时间（医院所在时区）；不带时区的
    时刻**原样当墙钟** —— 班次表上写的就是挂钟。
    """
    if moment.tzinfo is None:
        return moment
    return moment.astimezone().replace(tzinfo=None)


def parse_shift_time(value: str) -> Optional[time]:
    """把一个班次时刻解析成「一天中的几点」。

    解析不出来返回 ``None`` —— **不猜**。猜一个时刻比留空更危险：
    猜错就会把不在班的人算成在班，或者反过来。
    """
    text = str(value or "").strip()
    if not text:
        return None
    if _DAY_END_PATTERN.match(text):
        # 24:00 由调用方单独处理（end_day_end），这里不返回 time。
        return time(23, 59, 59)
    for fmt in _TIME_FORMATS:
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        return parsed.time()
    return None


@dataclass(frozen=True)
class ShiftWindow:
    """一条班次的起止窗口。

    ``start`` / ``end`` 是「一天中的几点」；``end_day_end`` 表示源表写的是
    ``24:00``（班次到当天结束）。``raw_start`` / ``raw_end`` 保留原文 ——
    卡片上要能看出「这个班次是源表里怎么写的」。
    """

    start: Optional[time] = None
    end: Optional[time] = None
    raw_start: str = ""
    raw_end: str = ""
    end_day_end: bool = False

    @property
    def start_known(self) -> bool:
        return self.start is not None

    @property
    def end_known(self) -> bool:
        return self.end is not None

    @property
    def complete(self) -> bool:
        """起止两端都解析出来了（才能做精确的窗口判定）。"""
        return self.start is not None and self.end is not None

    @property
    def incomplete(self) -> bool:
        """只写了一端时刻 —— 判不出「不在班」，按在班处理并标出来。"""
        return not self.complete

    @property
    def unknown(self) -> bool:
        """两端都没写 —— 完全没有班次信息。"""
        return self.start is None and self.end is None

    @property
    def crosses_midnight(self) -> bool:
        """是否跨零点（``20:00 → 08:00``）。"""
        if not self.complete or self.end_day_end:
            return False
        return bool(self.start and self.end and self.start > self.end)

    def covers(self, at: datetime) -> bool:
        """当前时点是否在班。

        * 起止齐全 → 精确判定（含跨零点）；
        * 只写了一端 / 都没写 → **判不出「不在班」**，按在班处理
          （``incomplete`` / ``unknown`` 会把这个情况标出来）。
        """
        moment = wall_clock(at).time()
        if not self.complete:
            return True
        if self.start is None or self.end is None:  # pragma: no cover —— complete 已覆盖
            return True
        if self.start == self.end:
            # 起止相同 —— 按全天班处理（源表常见写法）。
            return True
        if self.end_day_end:
            # ``08:00 → 24:00``：当天剩下的时间都在班。
            return moment >= self.start
        if self.start < self.end:
            return self.start <= moment < self.end
        # 跨零点：``20:00 → 08:00``
        return moment >= self.start or moment < self.end

    def describe(self) -> str:
        """人话描述（卡片与日志用）。"""
        if self.unknown:
            return "班次时间未写明"
        if self.incomplete:
            known = self.raw_start if self.start_known else self.raw_end
            return f"班次时间不完整（仅 {known}），按在班处理"
        if self.end_day_end:
            return f"班次 {self.raw_start or self.start.isoformat()}-24:00"
        if self.crosses_midnight:
            return f"班次 {self.raw_start or self.start.isoformat()}-{self.raw_end or self.end.isoformat()}（跨零点）"
        return f"班次 {self.raw_start or self.start.isoformat()}-{self.raw_end or self.end.isoformat()}"

    def to_dict(self) -> dict[str, object]:
        return {
            "start": self.start.isoformat() if self.start else "",
            "end": self.end.isoformat() if self.end else "",
            "raw_start": self.raw_start,
            "raw_end": self.raw_end,
            "end_day_end": self.end_day_end,
            "crosses_midnight": self.crosses_midnight,
            "incomplete": self.incomplete,
        }


def shift_window_of(person: object) -> ShiftWindow:
    """从一条当班记录里读出班次窗口。

    接受任何带 ``shift_start`` / ``shift_end`` 属性的对象（当班人模型）。
    """
    raw_start = str(getattr(person, "shift_start", "") or "").strip()
    raw_end = str(getattr(person, "shift_end", "") or "").strip()
    start = parse_shift_time(raw_start) if raw_start else None
    end = parse_shift_time(raw_end) if raw_end else None
    end_day_end = bool(raw_end) and _DAY_END_PATTERN.match(raw_end) is not None
    return ShiftWindow(
        start=start,
        end=end,
        raw_start=raw_start,
        raw_end=raw_end,
        end_day_end=end_day_end,
    )


def on_duty_at(person: object, at: datetime) -> bool:
    """这条当班记录在 ``at`` 时刻是否在班。"""
    return shift_window_of(person).covers(at)
