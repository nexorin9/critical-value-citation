"""到期重推：``pending_ack`` 事件按 ``due_minutes`` 到期复查重推。

**为什么需要它**：处置卡推出去之后，值班人**可能没看见** —— 群里刷屏、
换了班、手机静音。制度上的上报时限（``due_minutes``）到了还没人确认，
就得**再推一次**，而不是让这条危急值静静躺在台账里。

**三条硬口径**：

1. **重推不重复计数** —— 重推是**同一次通知的后续尝试**，不是一次新通知。
   台账里「今天推送了多少条」按**去重事件数**算，重推不会把数字灌大；
   首次推送时刻 ``notified_at`` **不被重推覆盖**，另用 ``last_notified_at``
   记最近一次。
2. **不覆盖原推送记录** —— 每次推送尝试都往 ``notifications`` 表**追加**
   一行（``kind=initial`` 首次 / ``kind=retry`` 重推），原行原样保留。
3. **重试耗尽落 ``failed`` 并告警，不静默丢弃** —— 重试到上限仍推不出去，
   事件留在台账里、状态转 ``failed``、写一条 ``notify_retry_exhausted``
   审计并打告警日志。**绝不**假装推送成功，也绝不把事件删掉。

**推送出口是注入的**：本模块只管「该不该重推、重推了没有、失败怎么落」，
实际往哪儿推由调用方给一个 ``push(event) -> bool``（任务 id 11 接企微群
机器人；本轮用注入的替身验口径）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Optional

from critval.ledger.states import (
    STATUS_FAILED,
    STATUS_PENDING_ACK,
    describe_status,
    validate_transition,
)

__all__ = [
    "AWAITING_ACK_STATUSES",
    "DEFAULT_MAX_RETRIES",
    "NOTIFY_KIND_INITIAL",
    "NOTIFY_KIND_RETRY",
    "RetryOutcome",
    "RetryPlanner",
    "RetryReport",
    "RetryRunner",
]

logger = logging.getLogger("critval.ledger.retry")

#: 等回执中的状态：已推送但还没人确认。
#:
#: ``notified`` 与 ``pending_ack`` 都算 —— 前者是「刚推完」，后者是
#: 「已进入等待回执窗口」。只要事件还停在这两个状态上、且时限已到，
#: 就要复查重推。
AWAITING_ACK_STATUSES: frozenset[str] = frozenset({"notified", STATUS_PENDING_ACK})

#: 首次推送 / 重推。
NOTIFY_KIND_INITIAL = "initial"
NOTIFY_KIND_RETRY = "retry"

#: 默认最大重推次数（超过即落 ``failed``）。
DEFAULT_MAX_RETRIES = 3


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_moment(text: str) -> Optional[datetime]:
    """把 ISO 时间文本解析成 datetime（解析不了返回 ``None``）。"""
    raw = str(text or "").strip()
    if not raw:
        return None
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


@dataclass(frozen=True)
class RetryOutcome:
    """一次到期复查的结论。

    两个数字别混：

    * ``attempt`` —— **通知尝试序号**（1 = 首次推送，2 = 第一次重推 …）；
    * ``retries`` —— 这是第几次**重推**（1 = 第一次重推）。
    """

    event_id: str
    action: str  # repushed | push_failed | exhausted | skipped
    attempt: int = 0
    retries: int = 0
    due_at: str = ""
    ok: bool = False
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "action": self.action,
            "attempt": self.attempt,
            "retries": self.retries,
            "due_at": self.due_at,
            "ok": self.ok,
            "detail": self.detail,
        }


@dataclass
class RetryReport:
    """一轮复查的总体结论。"""

    outcomes: list[RetryOutcome] = field(default_factory=list)
    alerts: list[str] = field(default_factory=list)
    ran_at: str = ""

    @property
    def repushed(self) -> list[RetryOutcome]:
        return [item for item in self.outcomes if item.action == "repushed"]

    @property
    def exhausted(self) -> list[RetryOutcome]:
        return [item for item in self.outcomes if item.action == "exhausted"]

    @property
    def failed(self) -> list[RetryOutcome]:
        return [item for item in self.outcomes if item.action == "push_failed"]

    @property
    def summary(self) -> dict[str, int]:
        """本轮复查的计数（重推 / 失败 / 耗尽 / 告警）。"""
        return {
            "total": len(self.outcomes),
            "repushed": len(self.repushed),
            "push_failed": len(self.failed),
            "exhausted": len(self.exhausted),
            "alerts": len(self.alerts),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "ran_at": self.ran_at,
            "outcomes": [item.to_dict() for item in self.outcomes],
            "alerts": list(self.alerts),
            "summary": dict(self.summary),
        }


#: 推送出口：给一条事件行，返回「是否推送成功」。
PushCallable = Callable[[Mapping[str, Any]], bool]


class RetryPlanner:
    """挑出到期需要重推的事件（只读，不改数据）。"""

    def __init__(self, store: Any, *, clock: Optional[Callable[[], datetime]] = None) -> None:
        self.store = store
        self._clock = clock or _now

    def now(self) -> datetime:
        return self._clock()

    def due_events(self, now: Optional[datetime] = None) -> list[dict[str, Any]]:
        """到期且仍等回执的事件（按到期时刻升序）。

        条件三条同时成立：状态在 ``AWAITING_ACK_STATUSES`` 里、
        ``due_at`` 有值、``due_at`` 已过。
        """
        moment = now or self.now()
        rows = self.store.list_events(limit=0)
        due: list[dict[str, Any]] = []
        for row in rows:
            status = str(row.get("status", "") or "")
            if status not in AWAITING_ACK_STATUSES:
                continue
            due_at = str(row.get("due_at", "") or "")
            if not due_at:
                continue
            due_moment = _parse_moment(due_at)
            if due_moment is None:
                continue
            if due_moment > moment:
                continue
            due.append(dict(row))
        due.sort(key=lambda item: (str(item.get("due_at", "")), str(item.get("event_id", ""))))
        return due

    def plan(
        self,
        now: Optional[datetime] = None,
        *,
        max_retries: int = DEFAULT_MAX_RETRIES,
    ) -> list[RetryOutcome]:
        """给出本轮的重推计划（不推送、不改状态）。"""
        moment = now or self.now()
        plan: list[RetryOutcome] = []
        for row in self.due_events(moment):
            retry_count = int(row.get("retry_count", 0) or 0)
            due_at = str(row.get("due_at", "") or "")
            if retry_count >= max_retries:
                plan.append(
                    RetryOutcome(
                        event_id=str(row.get("event_id", "")),
                        action="exhausted",
                        attempt=retry_count + 1,
                        retries=retry_count,
                        due_at=due_at,
                        ok=False,
                        detail=(
                            f"已重推 {retry_count} 次仍未确认，超过上限 {max_retries} 次；"
                            f"本轮应落 {STATUS_FAILED} 并告警"
                        ),
                    )
                )
                continue
            plan.append(
                RetryOutcome(
                    event_id=str(row.get("event_id", "")),
                    action="repushed",
                    attempt=retry_count + 2,
                    retries=retry_count + 1,
                    due_at=due_at,
                    ok=True,
                    detail=f"已到上报时限（{due_at}），第 {retry_count + 1} 次复查重推",
                )
            )
        return plan


class RetryRunner:
    """执行到期重推。"""

    def __init__(
        self,
        store: Any,
        push: PushCallable,
        *,
        max_retries: int = DEFAULT_MAX_RETRIES,
        channel: str = "wecom",
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self.store = store
        self.push = push
        self.max_retries = max(0, int(max_retries))
        self.channel = channel or "unknown"
        self.planner = RetryPlanner(store, clock=clock)

    def run(self, now: Optional[datetime] = None) -> RetryReport:
        """跑一轮复查重推。"""
        moment = now or self.planner.now()
        report = RetryReport(ran_at=moment.isoformat())
        for outcome in self.planner.plan(moment, max_retries=self.max_retries):
            if outcome.action == "exhausted":
                final = self._exhaust(outcome)
            else:
                final = self._repush(outcome, moment)
            report.outcomes.append(final)
            if final.action == "exhausted":
                report.alerts.append(final.detail)
                logger.warning("危急值重推耗尽：%s", final.detail)
        return report

    # ------------------------------------------------------------------
    def _repush(self, outcome: RetryOutcome, moment: datetime) -> RetryOutcome:
        event_id = outcome.event_id
        row = self.store.get_event(event_id) or {}
        try:
            ok = bool(self.push(row))
        except Exception as exc:  # 推送出口异常 → 视为失败，不吞
            ok = False
            detail = f"推送出口抛错：{exc}"
        else:
            detail = ""

        attempt = outcome.attempt
        retries = outcome.retries
        if ok:
            self.store.record_notification(
                event_id,
                kind=NOTIFY_KIND_RETRY,
                attempt=attempt,
                channel=self.channel,
                notified_at=moment,
                due_at=outcome.due_at,
                detail=f"第 {retries} 次复查重推（首次推送时刻不被覆盖）",
            )
            self.store.bump_retry(event_id, retries=retries, at=moment)
            self.store.append_audit(
                "notify_retry",
                event_id=event_id,
                actor="retry",
                dept_code=str(row.get("dept_code", "") or ""),
                specimen_no=str(row.get("specimen_no", "") or ""),
                item_code=str(row.get("item_code", "") or ""),
                message_control_id=str(row.get("message_control_id", "") or ""),
                detail=(
                    f"到期复查重推：第 {retries} 次（尝试序号 {attempt}），"
                    f"渠道 {self.channel}，原定时限 {outcome.due_at}；"
                    f"首次推送记录原样保留"
                ),
            )
            return RetryOutcome(
                event_id=event_id,
                action="repushed",
                attempt=attempt,
                retries=retries,
                due_at=outcome.due_at,
                ok=True,
                detail=f"第 {retries} 次复查重推成功（渠道 {self.channel}）",
            )

        # 推送失败：记账 + 视次数决定是否落 failed。
        self.store.bump_retry(event_id, retries=retries, at=moment)
        self.store.append_audit(
            "notify_failed",
            event_id=event_id,
            actor="retry",
            dept_code=str(row.get("dept_code", "") or ""),
            specimen_no=str(row.get("specimen_no", "") or ""),
            item_code=str(row.get("item_code", "") or ""),
            message_control_id=str(row.get("message_control_id", "") or ""),
            detail=(
                f"第 {retries} 次复查重推失败：{detail or '推送出口返回失败'}；"
                f"事件留在台账里，未被丢弃"
            ),
        )
        if retries >= self.max_retries:
            return self._exhaust(
                RetryOutcome(
                    event_id=event_id,
                    action="exhausted",
                    attempt=attempt,
                    retries=retries,
                    due_at=outcome.due_at,
                    ok=False,
                    detail=(
                        f"已重推 {retries} 次仍未送达，超过上限 {self.max_retries} 次；"
                        f"本轮落 {STATUS_FAILED} 并告警，事件保留在台账里"
                    ),
                )
            )
        return RetryOutcome(
            event_id=event_id,
            action="push_failed",
            attempt=attempt,
            retries=retries,
            due_at=outcome.due_at,
            ok=False,
            detail=(
                f"第 {retries} 次复查重推失败：{detail or '推送出口返回失败'}；"
                f"未达上限 {self.max_retries} 次，下一轮继续"
            ),
        )

    def _exhaust(self, outcome: RetryOutcome) -> RetryOutcome:
        """重试耗尽 → 落 ``failed`` + 告警（事件不丢）。"""
        event_id = outcome.event_id
        row = self.store.get_event(event_id) or {}
        current = str(row.get("status", "") or "")
        if current and current != STATUS_FAILED:
            try:
                validate_transition(current, STATUS_FAILED)
            except Exception:
                # 当前状态不允许直接落 failed（例如已被人工确认）——
                # 那就不动状态，只留告警，绝不硬改既成事实。
                return RetryOutcome(
                    event_id=event_id,
                    action="skipped",
                    attempt=outcome.attempt,
                    retries=outcome.retries,
                    due_at=outcome.due_at,
                    ok=False,
                    detail=(
                        f"重推已耗尽，但事件当前状态为 {describe_status(current)}，"
                        f"不允许直接落 {STATUS_FAILED}；事件保持原状并留告警"
                    ),
                )
            self.store.set_status(
                event_id,
                STATUS_FAILED,
                actor="retry",
                detail=(
                    f"复查重推 {outcome.retries} 次仍未送达，落 {STATUS_FAILED}；"
                    f"事件保留在台账里等待人工处理"
                ),
            )
        self.store.append_audit(
            "notify_retry_exhausted",
            event_id=event_id,
            actor="retry",
            dept_code=str(row.get("dept_code", "") or ""),
            specimen_no=str(row.get("specimen_no", "") or ""),
            item_code=str(row.get("item_code", "") or ""),
            message_control_id=str(row.get("message_control_id", "") or ""),
            detail=outcome.detail,
        )
        return RetryOutcome(
            event_id=event_id,
            action="exhausted",
            attempt=outcome.attempt,
            retries=outcome.retries,
            due_at=outcome.due_at,
            ok=False,
            detail=outcome.detail,
        )
