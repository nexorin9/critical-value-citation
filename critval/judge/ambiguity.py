"""同一标本 · 同一项目 · 同一观察时刻的**互斥值**检测（任务 id 9）。

## 现场是什么

同一份标本、同一个检验项目、同一个观察时刻，LIS 送来**两个不一样的数值** ——
常见来源是双仪器并行复查、接口重发带上了旧值、或者上游两套系统各推了一条。
这两个数值不可能同时成立，所以**不能取一个当结论**。

本项目的口径：

* 互斥 → 判定结论落 ``ambiguous_observation``，**不自动判定、不出卡**，
  事件转人工（和「查不到依据」一样，绝不静默放过）；
* 互斥的两个值若分处危急阈值**两侧**（一个低于低危急值、一个高于高危急值），
  那是更强的矛盾（``direction_conflict``）—— 值班人看一眼就知道
  「这两条不可能都对」，比「两个相近的值」更需要立刻人工裁决。

## 修正结果不走这里

``OBR-25=C`` 的**修正结果**同样会带着同标本同项目同观察时刻再来一条，
但它是**合法的新值**，由取代链处理（旧事件置 ``superseded``），
**不算互斥**。所以本模块先把修正结果剔掉。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Iterable, Optional, Sequence

from critval.models import ThresholdRow

__all__ = [
    "AMBIGUITY_KIND_DIRECTION",
    "AMBIGUITY_KIND_VALUE",
    "AmbiguousObservation",
    "EventView",
    "check_ambiguity",
    "detect_ambiguous_observations",
    "event_view_from_row",
    "is_correction_result",
    "observation_group_key",
]

#: 两个不同的数值同时出现。
AMBIGUITY_KIND_VALUE = "value_conflict"

#: 两个数值分处危急阈值两侧（一个过低一个过高）。
AMBIGUITY_KIND_DIRECTION = "direction_conflict"

_SIDE_BELOW = "below_low"
_SIDE_ABOVE = "above_high"
_SIDE_WITHIN = "within"


@dataclass(frozen=True)
class AmbiguousObservation:
    """一条互斥观察记录。

    ``values`` / ``event_ids`` 一一对应（按事件 ID 排序），便于人工逐条核对。
    """

    kind: str
    specimen_no: str
    item_code: str
    item_name: str = ""
    observed_at: str = ""
    values: tuple[str, ...] = ()
    event_ids: tuple[str, ...] = ()
    reason: str = ""

    @property
    def is_direction_conflict(self) -> bool:
        return self.kind == AMBIGUITY_KIND_DIRECTION

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "specimen_no": self.specimen_no,
            "item_code": self.item_code,
            "item_name": self.item_name,
            "observed_at": self.observed_at,
            "values": list(self.values),
            "event_ids": list(self.event_ids),
            "reason": self.reason,
        }


def is_correction_result(event: Any) -> bool:
    """该事件是不是**修正结果**（``OBR-25=C``）。"""
    return str(getattr(event, "result_status", "") or "").strip().upper() == "C"


def observation_group_key(event: Any) -> tuple[str, str, str]:
    """互斥检测的分组键：标本号 + 项目码 + 观察时刻。"""
    observed_at = getattr(event, "observed_at", None)
    if isinstance(observed_at, datetime):
        stamp = observed_at.isoformat()
    else:
        stamp = str(observed_at or "").strip()
    return (
        str(getattr(event, "specimen_no", "") or "").strip(),
        str(getattr(event, "item_code", "") or "").strip(),
        stamp,
    )


def _value_text(value: Any) -> str:
    if isinstance(value, Decimal):
        return format(value, "f")
    return str(value).strip()


def _observed_date(event: Any) -> Optional[date]:
    observed_at = getattr(event, "observed_at", None)
    if isinstance(observed_at, datetime):
        return observed_at.date()
    return None


def _side_of(value: Decimal, row: Optional[ThresholdRow]) -> str:
    """这个数值落在危急阈值的哪一侧（``within`` 表示没触发任何一侧）。"""
    if row is None:
        return _SIDE_WITHIN
    low = row.low_critical
    high = row.high_critical
    if low is not None and value < low:
        return _SIDE_BELOW
    if high is not None and value > high:
        return _SIDE_ABOVE
    return _SIDE_WITHIN


def _threshold_row_for(
    rows: Optional[Sequence[ThresholdRow]], item_code: str, observed_date: Optional[date]
) -> Optional[ThresholdRow]:
    """按项目码取阈值行（多版本时按观察时刻择一，取不到就不判方向）。"""
    if not rows:
        return None
    matched = [row for row in rows if row.item_code == item_code]
    if not matched:
        return None
    versions = {row.version or "" for row in matched}
    if len(versions) <= 1:
        return matched[0]
    if observed_date is None:
        return matched[0]
    from critval.judge.critical import select_threshold_row

    row, _ = select_threshold_row(matched, item_code, observed_date)
    return row


def detect_ambiguous_observations(
    events: Iterable[Any],
    rows: Optional[Sequence[ThresholdRow]] = None,
) -> list[AmbiguousObservation]:
    """在一批事件里找出互斥观察。

    返回按「标本号 + 项目码 + 观察时刻」分组后的互斥组，每组一条记录。
    修正结果（``OBR-25=C``）不参与互斥判定 —— 它们走取代链。
    """
    groups: dict[tuple[str, str, str], list[Any]] = {}
    for event in events:
        if is_correction_result(event):
            continue
        specimen_no, item_code, stamp = observation_group_key(event)
        if not specimen_no or not item_code:
            # 缺标本号 / 项目码就没法确认「是不是同一次观察」，不猜。
            continue
        groups.setdefault((specimen_no, item_code, stamp), []).append(event)

    findings: list[AmbiguousObservation] = []
    for (specimen_no, item_code, stamp), group in sorted(groups.items()):
        if len(group) < 2:
            continue
        distinct: dict[str, list[Any]] = {}
        numeric: dict[str, Decimal] = {}
        for event in group:
            raw = getattr(event, "value", "")
            text = _value_text(raw)
            distinct.setdefault(text, []).append(event)
            try:
                numeric[text] = Decimal(text)
            except Exception:  # noqa: BLE001 - 非数值结果不参与方向判定
                continue
        if len(distinct) < 2:
            # 同一个值重复到达 —— 那是幂等的事，不是互斥。
            continue

        ordered_values = sorted(distinct)
        ordered_events = sorted(
            (e for value in ordered_values for e in distinct[value]),
            key=lambda e: str(getattr(e, "event_id", "")),
        )
        row = _threshold_row_for(rows, item_code, _observed_date(group[0]))
        sides = {
            _side_of(numeric[value], row)
            for value in ordered_values
            if value in numeric
        }
        kind = (
            AMBIGUITY_KIND_DIRECTION
            if _SIDE_BELOW in sides and _SIDE_ABOVE in sides
            else AMBIGUITY_KIND_VALUE
        )
        item_name = str(getattr(group[0], "item_name", "") or "")
        values_text = "、".join(
            f"{value}{(' ' + str(getattr(group[0], 'unit', ''))).strip()}" for value in ordered_values
        )
        if kind == AMBIGUITY_KIND_DIRECTION:
            reason = (
                f"标本 {specimen_no} 的项目 {item_code} 在观察时刻 {stamp} 出现互斥结果，"
                f"且两个数值分处危急阈值两侧（{values_text}），不可能同时成立；"
                f"不自动判定，转人工裁决"
            )
        else:
            reason = (
                f"标本 {specimen_no} 的项目 {item_code} 在观察时刻 {stamp} 出现互斥结果"
                f"（{values_text}），无法判定以哪个为准；不自动判定，转人工裁决"
            )
        findings.append(
            AmbiguousObservation(
                kind=kind,
                specimen_no=specimen_no,
                item_code=item_code,
                item_name=item_name,
                observed_at=stamp,
                values=tuple(ordered_values),
                event_ids=tuple(
                    str(getattr(e, "event_id", "")) for e in ordered_events
                ),
                reason=reason,
            )
        )
    return findings


@dataclass(frozen=True)
class EventView:
    """事件的最小视图 —— 台账行与规范化事件都能投影成它。

    互斥检测只关心「哪个标本、哪个项目、哪个时刻、什么值、是不是修正结果」，
    所以两边投影到同一个形状，检测逻辑就不必关心数据来自内存还是 SQLite。
    """

    event_id: str
    specimen_no: str
    item_code: str
    item_name: str
    unit: str
    observed_at: str
    value: str
    result_status: str


def event_view_from_row(row: Any) -> EventView:
    """把台账行（``dict``）投影成 :class:`EventView`。"""
    if isinstance(row, EventView):
        return row
    if isinstance(row, dict):
        get = row.get
    else:
        get = lambda key, default="": getattr(row, key, default)  # noqa: E731
    observed_at = get("observed_at", "")
    return EventView(
        event_id=str(get("event_id", "") or ""),
        specimen_no=str(get("specimen_no", "") or ""),
        item_code=str(get("item_code", "") or ""),
        item_name=str(get("item_name", "") or ""),
        unit=str(get("unit", "") or ""),
        observed_at=observed_at.isoformat() if hasattr(observed_at, "isoformat") else str(observed_at or ""),
        value=str(get("value", "") or ""),
        result_status=str(get("result_status", "") or ""),
    )


def check_ambiguity(
    store: Any,
    event: Any,
    rows: Optional[Sequence[ThresholdRow]] = None,
) -> Optional[AmbiguousObservation]:
    """在台账里找这条事件的**兄弟事件**，看有没有互斥值。

    兄弟事件的取法：同标本 + 同项目 + **同一观察时刻**，且不是自己。
    修正结果不参与互斥（走取代链）。

    找到互斥就返回一条 :class:`AmbiguousObservation`；没有就返回 ``None``。
    """
    specimen_no = str(getattr(event, "specimen_no", "") or "").strip()
    item_code = str(getattr(event, "item_code", "") or "").strip()
    if not specimen_no or not item_code:
        return None
    observed_at = getattr(event, "observed_at", None)
    stamp = (
        observed_at.isoformat()
        if hasattr(observed_at, "isoformat")
        else str(observed_at or "").strip()
    )
    siblings = store.sibling_events(
        specimen_no, item_code, stamp, exclude_event_id=str(getattr(event, "event_id", "") or "")
    )
    if not siblings:
        return None

    views = [event_view_from_row(event)] + [
        event_view_from_row(row) for row in siblings
    ]
    findings = detect_ambiguous_observations(views, rows)
    if not findings:
        return None
    # 取涉及本事件的那一条（兄弟里若还有第三、第四条同刻互斥，另记一条）。
    own = str(getattr(event, "event_id", "") or "")
    for finding in findings:
        if own in finding.event_ids:
            return finding
    return findings[0]
