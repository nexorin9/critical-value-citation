"""修正结果（``OBR-25=C``）的取代链（任务 id 9）。

## 现场是什么

检验科发现某条结果报错了，会**重发一条带 ``OBR-25=C`` 的修正结果**。
这条修正结果带着**同一个标本、同一个项目、同一个观察时刻**，只是数值改了。
对台账来说，这就产生了一个问题：**同一次观察出现了两个结论**。

## 本项目的口径

* 新事件（修正结果）**重新判定**，并在自己身上记 ``supersedes = 旧事件 ID``；
* 旧事件被置 ``superseded``（**终态**）—— 旧事件的历史判定与依据**原样保留**，
  不删不改写，只是明确标记「这条已被修正取代」；
* **终态不可静默回退**：``superseded`` / ``archived`` 之后不许再被普通路径
  改回别的状态（``LedgerStore.set_status`` 会拒绝）。

## 两条不许越过的线

1. **不改写历史事实** —— 旧事件的判定结论、命中阈值行、出处都还在库里，
   复核时能看到「当时判的是什么、后来被谁取代了」。
2. **不改写已经被人处置过的事实** —— 旧事件若已经 ``acknowledged`` /
   ``registered`` / ``archived``（值班人已经确认过、甚至已回写登记系统），
   这条迟到的修正结果**不取代**它：新事件照常落台账，但落一条
   ``supersede_conflict`` 审计并转人工，由值班人决定要不要重开一条处置流程。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

__all__ = [
    "SUPERSEDE_STATUS",
    "SUPERSEDEABLE_STATUSES",
    "SupersedeOutcome",
    "apply_supersede",
    "find_superseded_candidate",
    "is_correction_result",
]

#: 旧事件被取代后的状态（**终态**）。
SUPERSEDE_STATUS = "superseded"

#: **还没被人处置过**的旧事件才允许被修正结果取代。
#:
#: 一旦值班人点过确认（``acknowledged``）、已回写登记系统（``registered``）
#: 或已归档（``archived``），那条就是**既成事实**，迟到的修正结果无权改写它。
SUPERSEDEABLE_STATUSES = frozenset(
    {"received", "judged", "card_built", "notified", "pending_ack"}
)

#: 冲突时的审计动作名。
SUPERSEDE_CONFLICT_ACTION = "supersede_conflict"


@dataclass(frozen=True)
class SupersedeOutcome:
    """一次取代处理的结果。"""

    applied: bool
    new_event_id: str = ""
    old_event_id: str = ""
    reason: str = ""
    conflict: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "applied": self.applied,
            "new_event_id": self.new_event_id,
            "old_event_id": self.old_event_id,
            "reason": self.reason,
            "conflict": self.conflict,
        }


def is_correction_result(event: Any) -> bool:
    """该事件是不是**修正结果**（``OBR-25=C``）。"""
    return str(getattr(event, "result_status", "") or "").strip().upper() == "C"


def _observed_text(event: Any) -> str:
    observed_at = getattr(event, "observed_at", None)
    isoformat = getattr(observed_at, "isoformat", None)
    if callable(isoformat):
        return isoformat()
    return str(observed_at or "").strip()


def find_superseded_candidate(store: Any, event: Any) -> Optional[str]:
    """找出这条修正结果要取代的旧事件 ID。

    匹配口径：**同标本 + 同项目**，优先取观察时刻相同的那条；
    没有同刻的旧事件时，退到同标本同项目的**最近一条**（按观察时刻倒序）。

    只考虑**还没被取代**、且自身**不是修正结果**的旧事件 ——
    修正结果之间不互相取代（连续两次修正以最后一条为准，
    由取代链一层层串起来）。
    """
    specimen_no = str(getattr(event, "specimen_no", "") or "").strip()
    item_code = str(getattr(event, "item_code", "") or "").strip()
    if not specimen_no or not item_code:
        return None

    candidates = store.events_for_specimen_item(specimen_no, item_code)
    if not candidates:
        return None

    same_time = _observed_text(event)
    eligible: list[tuple[str, str]] = []
    for row in candidates:
        row_id = str(row.get("event_id", ""))
        if not row_id or row_id == getattr(event, "event_id", ""):
            continue
        if str(row.get("result_status", "") or "").strip().upper() == "C":
            continue
        if str(row.get("status", "") or "") == SUPERSEDE_STATUS:
            continue
        eligible.append((str(row.get("observed_at", "") or ""), row_id))

    if not eligible:
        return None
    same_moment = [row_id for stamp, row_id in eligible if stamp == same_time]
    if same_moment:
        return same_moment[0]
    eligible.sort()
    return eligible[-1][1]


def apply_supersede(store: Any, event: Any) -> SupersedeOutcome:
    """把修正结果与旧事件接上取代链。

    * 不是修正结果 → 不做任何事（``applied=False``）；
    * 找不到可取代的旧事件 → 不做任何事；
    * 旧事件已被人工处置过 → **不取代**，落冲突审计（``conflict=True``）；
    * 正常取代 → 旧事件置 ``superseded``（终态），新事件记 ``supersedes``。
    """
    new_event_id = str(getattr(event, "event_id", "") or "")
    if not is_correction_result(event):
        return SupersedeOutcome(
            applied=False,
            new_event_id=new_event_id,
            reason="该事件不是修正结果（OBR-25 ≠ C），不进入取代链",
        )

    old_event_id = find_superseded_candidate(store, event)
    if not old_event_id:
        return SupersedeOutcome(
            applied=False,
            new_event_id=new_event_id,
            reason="同标本同项目下没有可取代的旧事件，修正结果照常落台账",
        )

    old_status = store.get_status(old_event_id)
    if old_status and old_status not in SUPERSEDEABLE_STATUSES:
        detail = (
            f"修正结果 {new_event_id} 想取代旧事件 {old_event_id}，"
            f"但该事件已处于 {old_status}（值班人已处置或已归档），"
            f"不改写既成事实；本条修正结果照常落台账并转人工裁决"
        )
        store.append_audit(
            SUPERSEDE_CONFLICT_ACTION,
            event_id=new_event_id,
            actor="judge",
            dept_code=str(getattr(event, "dept_code", "") or ""),
            specimen_no=str(getattr(event, "specimen_no", "") or ""),
            item_code=str(getattr(event, "item_code", "") or ""),
            message_control_id=str(getattr(event, "message_control_id", "") or ""),
            detail=detail,
        )
        return SupersedeOutcome(
            applied=False,
            new_event_id=new_event_id,
            old_event_id=old_event_id,
            reason=detail,
            conflict=True,
        )

    store.link_supersede(new_event_id, old_event_id)
    detail = (
        f"修正结果 {new_event_id} 取代旧事件 {old_event_id}"
        f"（旧事件状态 {old_status or 'received'} → {SUPERSEDE_STATUS}）；"
        f"旧事件的判定与依据原样保留，不改写历史事实"
    )
    store.append_audit(
        "event_superseded",
        event_id=new_event_id,
        actor="judge",
        dept_code=str(getattr(event, "dept_code", "") or ""),
        specimen_no=str(getattr(event, "specimen_no", "") or ""),
        item_code=str(getattr(event, "item_code", "") or ""),
        message_control_id=str(getattr(event, "message_control_id", "") or ""),
        detail=detail,
    )
    return SupersedeOutcome(
        applied=True,
        new_event_id=new_event_id,
        old_event_id=old_event_id,
        reason=detail,
    )
