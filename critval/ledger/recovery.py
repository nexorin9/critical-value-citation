"""崩溃恢复：守护进程重启后**按台账状态续跑**。

守护进程随时可能被 kill（发版、重启机器、OOM）。重启之后要回答一个问题：
**上一次没做完的事，现在该做什么 —— 而且不能做重**。

本模块就是这件事的落点。它**不重写主路径**，只做一件事：读台账里每条
事件当前的状态，按状态决定要不要补一步、补哪一步。判定「补什么」的依据
只有**已落库的事实**（事件行的状态 + 通知记录 + 审计流水），
不靠进程内存里的任何东西 —— 进程内存随崩溃一起没了。

============  ==========================================  ===================
事件当前状态   续跑做什么                                为什么
============  ==========================================  ===================
``card_built``  推送（卡已装好，还没推出去）              崩溃点在出卡之后
``received`` / ``judged``  重新判定并出卡（有判定入口时）   崩溃点在判定中途
``acknowledged`` / ``writeback_failed``  回写登记系统     崩溃点在确认之后
``notified`` / ``pending_ack``  到期复查重推（复用重推口径） 崩溃期间时限到了
``not_critical`` / ``transferred_to_human`` / ``failed``  不动   人已经接手了
``registered`` / ``archived`` / ``superseded``  不动        事实已定格
============  ==========================================  ===================

**三条不变量**（本模块的全部意义都在这里）：

1. **不重复推送** —— 推送成功的记录（通知表 ``kind='initial'``）在案，
   续跑就只补状态、不再推一遍；能不能回查投递结果由渠道说了算
   （``push_check``），查不到就按「至少一次」重推并在结论里写明。
2. **不重复回写** —— 回写成功即落 ``registered``，续跑不再回写；
   崩溃落在「回写调用已发出、状态未更新」时，先问登记系统
   （``receipt_check``）有没有这条回执，有就只补状态。
3. **已确认回执不丢** —— 值班人确认过的 ``acknowledged_at`` /
   ``acknowledged_by`` 不会被续跑覆盖；回写失败落 ``writeback_failed``，
   确认痕迹原样留着等人补写。

**渠道是注入的**：本模块不认识企微、也不认识登记系统的 HTTP 细节 ——
推送与回写都靠调用方传进来的可调用对象（守护进程任务装配它们）。
这样本模块可以被单独测（注入假渠道），也能被真渠道复用。

**为什么不是一个「跑一次就退」的脚本**：续跑是**常驻进程的一部分**，
守护进程每次启动都跑一遍 ``resume()``，之后照常处理新入站事件；
续跑做过的每一步都落进台账与审计，值班人复核时看得见「这条是续跑补的」。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from critval.ledger.retry import (
    DEFAULT_MAX_RETRIES,
    NOTIFY_KIND_INITIAL,
    RetryRunner,
)
from critval.ledger.states import (
    STATUS_ACKNOWLEDGED,
    STATUS_CARD_BUILT,
    STATUS_JUDGED,
    STATUS_RECEIVED,
    STATUS_WRITEBACK_FAILED,
)

__all__ = [
    "RecoveryOutcome",
    "RecoveryReport",
    "RecoveryRunner",
]

logger = logging.getLogger("critval.ledger.recovery")

#: 续跑结论的动作名。
ACTION_PUSHED = "pushed"
ACTION_PUSH_RECONCILED = "push_reconciled"
ACTION_PUSH_FAILED = "push_failed"
ACTION_PUSH_DEFERRED = "push_deferred"
ACTION_WRITEBACK = "written_back"
ACTION_WRITEBACK_RECONCILED = "writeback_reconciled"
ACTION_WRITEBACK_FAILED = "writeback_failed"
ACTION_REJUDGED = "rejudged"
ACTION_REJUDGE_DEFERRED = "rejudge_deferred"
ACTION_RETRY = "retry"


@dataclass(frozen=True)
class RecoveryOutcome:
    """一条事件续跑后的结论。"""

    event_id: str
    action: str
    ok: bool
    status_before: str
    status_after: str
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "action": self.action,
            "ok": self.ok,
            "status_before": self.status_before,
            "status_after": self.status_after,
            "detail": self.detail,
        }


@dataclass
class RecoveryReport:
    """一轮续跑的总结。"""

    ran_at: str
    outcomes: list[RecoveryOutcome] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for outcome in self.outcomes:
            counts[outcome.action] = counts.get(outcome.action, 0) + 1
        return counts

    @property
    def failures(self) -> list[RecoveryOutcome]:
        return [outcome for outcome in self.outcomes if not outcome.ok]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ran_at": self.ran_at,
            "total": len(self.outcomes),
            "counts": self.counts(),
            "failures": len(self.failures),
            "outcomes": [outcome.to_dict() for outcome in self.outcomes],
        }

    def to_text(self) -> str:
        lines = [f"续跑时刻 : {self.ran_at}", f"处理条数 : {len(self.outcomes)} 条"]
        if not self.outcomes:
            lines.append("结论     : 没有需要续跑的事件（台账状态都推进到位了）")
            return "\n".join(lines)
        lines.append("─────────────────────────────")
        for outcome in self.outcomes:
            flag = "✔" if outcome.ok else "✘"
            lines.append(
                f"{flag} {outcome.event_id} · {outcome.action}"
                f" · {outcome.status_before} → {outcome.status_after}"
            )
            if outcome.detail:
                lines.append(f"    {outcome.detail}")
        counts = " / ".join(f"{k} {v}" for k, v in sorted(self.counts().items()))
        lines.append("─────────────────────────────")
        lines.append(f"按动作统计 : {counts}")
        if self.failures:
            lines.append(f"未完成     : {len(self.failures)} 条（已留痕，等人处理）")
        return "\n".join(lines)


class RecoveryRunner:
    """按台账状态续跑（守护进程启动时跑一遍，之后照常处理新入站）。

    参数都是**注入**的，模块本身不碰网络：

    * ``notifier(row) -> bool`` —— 推送处置卡；返回 ``True`` 表示送达。
    * ``push_check(row) -> bool | None`` —— 可选：这条事件**推过没有**。
      能回查的渠道（值班台自己就是权威）用它避免重复推送；查不到返回
      ``None``，按「至少一次」处理并在结论里写明。
    * ``writeback(row) -> bool`` —— 回写登记系统；返回 ``True`` 表示成功。
    * ``receipt_check(row) -> bool | None`` —— 可选：登记系统里**有没有这条
      回执**。回写幂等键是 ``message_control_id``，回查得到就不重复回写。
    * ``rejudge(row) -> bool`` —— 可选：重新判定并出卡（给「崩溃在判定
      中途」的事件用）。没有这个入口时，续跑**明说推不动**，不假装推进。
    """

    def __init__(
        self,
        store: Any,
        *,
        notifier: Optional[Callable[[dict[str, Any]], bool]] = None,
        push_check: Optional[Callable[[dict[str, Any]], Optional[bool]]] = None,
        writeback: Optional[Callable[[dict[str, Any]], bool]] = None,
        receipt_check: Optional[Callable[[dict[str, Any]], Optional[bool]]] = None,
        rejudge: Optional[Callable[[dict[str, Any]], bool]] = None,
        clock: Optional[Callable[[], datetime]] = None,
        channel: str = "wecom",
        max_retries: int = DEFAULT_MAX_RETRIES,
    ) -> None:
        self.store = store
        self.notifier = notifier
        self.push_check = push_check
        self.writeback = writeback
        self.receipt_check = receipt_check
        self.rejudge = rejudge
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.channel = channel or "unknown"
        self.max_retries = max(0, int(max_retries))

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------
    def resume(self, now: Optional[datetime] = None) -> RecoveryReport:
        """跑一轮续跑，返回逐条结论。"""
        moment = now or self._clock()
        report = RecoveryReport(ran_at=moment.isoformat())

        # ① 到期复查重推：口径已经在 ``ledger/retry.py`` 里定死了
        #    （到期才推、重推另记一行、耗尽落 failed），这里直接复用，
        #    不再写第二套。
        report.outcomes.extend(self._resume_overdue(moment))

        # ② 按状态续跑其余事件。
        for row in self.store.list_events(limit=0):
            outcome = self._resume_one(dict(row), moment)
            if outcome is not None:
                report.outcomes.append(outcome)
        return report

    # ------------------------------------------------------------------
    # 分派
    # ------------------------------------------------------------------
    def _resume_one(
        self, row: dict[str, Any], moment: datetime
    ) -> Optional[RecoveryOutcome]:
        status = str(row.get("status", "") or "")
        if status == STATUS_CARD_BUILT:
            return self._push(row, moment)
        if status in (STATUS_RECEIVED, STATUS_JUDGED):
            return self._rejudge(row, moment)
        if status in (STATUS_ACKNOWLEDGED, STATUS_WRITEBACK_FAILED):
            return self._writeback(row, moment)
        # 其余状态不动：等回执的由重推口径管；人已接手的
        # （transferred_to_human / failed）与事实已定格的
        # （registered / archived / superseded / not_critical）不自动推进。
        return None

    # ------------------------------------------------------------------
    # ① 到期复查重推
    # ------------------------------------------------------------------
    def _resume_overdue(self, moment: datetime) -> list[RecoveryOutcome]:
        if self.notifier is None:
            return []
        runner = RetryRunner(
            self.store,
            self.notifier,
            max_retries=self.max_retries,
            channel=self.channel,
            clock=lambda: moment,
        )
        report = runner.run(moment)
        outcomes: list[RecoveryOutcome] = []
        for outcome in report.outcomes:
            row = self.store.get_event(outcome.event_id) or {}
            outcomes.append(
                RecoveryOutcome(
                    event_id=outcome.event_id,
                    action=f"{ACTION_RETRY}_{outcome.action}",
                    ok=outcome.ok,
                    status_before=str(row.get("status", "") or ""),
                    status_after=str(row.get("status", "") or ""),
                    detail=outcome.detail,
                )
            )
        return outcomes

    # ------------------------------------------------------------------
    # ② 推送（卡已装好，还没推出去）
    # ------------------------------------------------------------------
    def _push(
        self, row: dict[str, Any], moment: datetime
    ) -> RecoveryOutcome:
        event_id = str(row.get("event_id", "") or "")
        status_before = str(row.get("status", "") or "")
        card_id = str(row.get("card_id", "") or "")
        due_minutes = row.get("due_minutes")
        due_minutes = int(due_minutes) if due_minutes not in (None, "") else None

        # 推送成功的记录在案 → 崩溃点在「推送成功、状态未更新」，
        # 只补状态，不重推（否则值班群刷屏）。
        if self._has_success_push(event_id):
            self.store.mark_notified(
                event_id,
                channel=self.channel,
                due_minutes=due_minutes,
                card_id=card_id,
                actor="recovery",
                detail="续跑：推送记录已在案，只补状态不重推",
                at=moment,
                record_notification=False,
            )
            self.store.append_audit(
                "recovery_resumed",
                event_id=event_id,
                actor="recovery",
                detail="续跑：推送记录已在案（崩溃发生在推送之后），只补状态不重推",
                at=moment,
            )
            return RecoveryOutcome(
                event_id=event_id,
                action=ACTION_PUSH_RECONCILED,
                ok=True,
                status_before=status_before,
                status_after="notified",
                detail="推送记录已在案，只补状态不重推",
            )

        # 渠道能回查投递结果 → 有回执就不推。
        if self.push_check is not None:
            try:
                already = self.push_check(row)
            except Exception as exc:  # 回查失败不等于推过 —— 按未知处理
                already = None
                logger.warning("推送回查失败，按未知处理：%s", exc)
            if already:
                self.store.mark_notified(
                    event_id,
                    channel=self.channel,
                    due_minutes=due_minutes,
                    card_id=card_id,
                    actor="recovery",
                    detail="续跑：渠道回查已投递，只补状态不重推",
                    at=moment,
                )
                self.store.append_audit(
                    "recovery_resumed",
                    event_id=event_id,
                    actor="recovery",
                    detail="续跑：渠道回查确认已投递，只补状态不重推",
                    at=moment,
                )
                return RecoveryOutcome(
                    event_id=event_id,
                    action=ACTION_PUSH_RECONCILED,
                    ok=True,
                    status_before=status_before,
                    status_after="notified",
                    detail="渠道回查确认已投递，只补状态不重推",
                )

        if self.notifier is None:
            return self._defer(
                event_id,
                status_before,
                ACTION_PUSH_DEFERRED,
                "未接入推送通道，本轮不推进（不静默跳过）",
                moment,
            )

        # 写前留痕：先记「这次推送已经发起」，再真的推。
        # 留痕落在**审计流水**里（通知表只记成功推送，计数才有意义）。
        self.store.append_audit(
            "notify_attempted",
            event_id=event_id,
            actor="recovery",
            dept_code=str(row.get("dept_code", "") or ""),
            specimen_no=str(row.get("specimen_no", "") or ""),
            item_code=str(row.get("item_code", "") or ""),
            message_control_id=str(row.get("message_control_id", "") or ""),
            detail=f"续跑：推送发起前留痕（渠道 {self.channel}）",
            at=moment,
        )
        try:
            ok = bool(self.notifier(row))
        except Exception as exc:
            ok = False
            reason = f"推送出口抛错：{exc}"
        else:
            reason = "" if ok else "推送出口返回失败"

        if not ok:
            self.store.append_audit(
                "notify_failed",
                event_id=event_id,
                actor="recovery",
                dept_code=str(row.get("dept_code", "") or ""),
                specimen_no=str(row.get("specimen_no", "") or ""),
                item_code=str(row.get("item_code", "") or ""),
                message_control_id=str(row.get("message_control_id", "") or ""),
                detail=f"续跑补推失败：{reason}；事件留在台账里，等下一轮",
                at=moment,
            )
            return RecoveryOutcome(
                event_id=event_id,
                action=ACTION_PUSH_FAILED,
                ok=False,
                status_before=status_before,
                status_after=status_before,
                detail=f"续跑补推失败：{reason}",
            )

        self.store.mark_notified(
            event_id,
            channel=self.channel,
            due_minutes=due_minutes,
            card_id=card_id,
            actor="recovery",
            detail="续跑补推成功",
            at=moment,
        )
        self.store.append_audit(
            "notify_sent",
            event_id=event_id,
            actor="recovery",
            dept_code=str(row.get("dept_code", "") or ""),
            specimen_no=str(row.get("specimen_no", "") or ""),
            item_code=str(row.get("item_code", "") or ""),
            message_control_id=str(row.get("message_control_id", "") or ""),
            detail=f"续跑补推成功（渠道 {self.channel}）",
            at=moment,
        )
        return RecoveryOutcome(
            event_id=event_id,
            action=ACTION_PUSHED,
            ok=True,
            status_before=status_before,
            status_after="notified",
            detail="续跑补推成功",
        )

    def _has_success_push(self, event_id: str) -> bool:
        """这条事件有没有**成功**推送过（通知表 ``kind='initial'``）。"""
        for record in self.store.notifications_of(event_id):
            if str(record.get("kind", "") or "") == NOTIFY_KIND_INITIAL:
                return True
        return False

    # ------------------------------------------------------------------
    # ③ 回写登记系统
    # ------------------------------------------------------------------
    def _writeback(
        self, row: dict[str, Any], moment: datetime
    ) -> RecoveryOutcome:
        event_id = str(row.get("event_id", "") or "")
        status_before = str(row.get("status", "") or "")

        # 登记系统里已经有这条回执 → 只补状态，不重复回写。
        if self.receipt_check is not None:
            try:
                has_receipt = self.receipt_check(row)
            except Exception as exc:
                has_receipt = None
                logger.warning("回执回查失败，按未知处理：%s", exc)
            if has_receipt:
                self.store.mark_registered(
                    event_id,
                    actor="recovery",
                    detail="续跑：登记系统已有回执，只补状态不重复回写",
                    at=moment,
                )
                self.store.append_audit(
                    "recovery_resumed",
                    event_id=event_id,
                    actor="recovery",
                    message_control_id=str(row.get("message_control_id", "") or ""),
                    detail="续跑：登记系统已有回执，只补状态不重复回写",
                    at=moment,
                )
                return RecoveryOutcome(
                    event_id=event_id,
                    action=ACTION_WRITEBACK_RECONCILED,
                    ok=True,
                    status_before=status_before,
                    status_after="registered",
                    detail="登记系统已有回执，只补状态不重复回写",
                )

        if self.writeback is None:
            return self._defer(
                event_id,
                status_before,
                ACTION_WRITEBACK_FAILED,
                "未接入回写通道，本轮不推进（已确认回执保留在台账里）",
                moment,
            )

        # 写前留痕：回写调用的意图表。回写幂等键是 message_control_id，
        # 登记系统侧会去重；本地留痕是为了事后能回答「这轮回写过没有」。
        self.store.append_audit(
            "writeback_attempted",
            event_id=event_id,
            actor="recovery",
            dept_code=str(row.get("dept_code", "") or ""),
            specimen_no=str(row.get("specimen_no", "") or ""),
            item_code=str(row.get("item_code", "") or ""),
            message_control_id=str(row.get("message_control_id", "") or ""),
            detail="续跑：回写登记系统（幂等键 message_control_id）",
            at=moment,
        )
        try:
            ok = bool(self.writeback(row))
        except Exception as exc:
            ok = False
            reason = f"回写出口抛错：{exc}"
        else:
            reason = "" if ok else "回写出口返回失败"

        if not ok:
            # 确认痕迹不丢：acknowledged_at / acknowledged_by 原样保留。
            if status_before == STATUS_ACKNOWLEDGED:
                self.store.set_status(
                    event_id,
                    STATUS_WRITEBACK_FAILED,
                    actor="recovery",
                    detail=f"续跑回写失败：{reason}；已确认回执保留，等补写",
                    at=moment,
                )
            self.store.append_audit(
                "writeback_failed",
                event_id=event_id,
                actor="recovery",
                dept_code=str(row.get("dept_code", "") or ""),
                specimen_no=str(row.get("specimen_no", "") or ""),
                item_code=str(row.get("item_code", "") or ""),
                message_control_id=str(row.get("message_control_id", "") or ""),
                detail=f"续跑回写失败：{reason}；已确认回执保留在台账里",
                at=moment,
            )
            return RecoveryOutcome(
                event_id=event_id,
                action=ACTION_WRITEBACK_FAILED,
                ok=False,
                status_before=status_before,
                status_after=STATUS_WRITEBACK_FAILED
                if status_before == STATUS_ACKNOWLEDGED
                else status_before,
                detail=f"续跑回写失败：{reason}",
            )

        self.store.mark_registered(
            event_id,
            actor="recovery",
            detail="续跑回写成功（已生效事实）",
            at=moment,
        )
        return RecoveryOutcome(
            event_id=event_id,
            action=ACTION_WRITEBACK,
            ok=True,
            status_before=status_before,
            status_after="registered",
            detail="续跑回写成功",
        )

    # ------------------------------------------------------------------
    # ④ 重新判定
    # ------------------------------------------------------------------
    def _rejudge(
        self, row: dict[str, Any], moment: datetime
    ) -> RecoveryOutcome:
        event_id = str(row.get("event_id", "") or "")
        status_before = str(row.get("status", "") or "")
        if self.rejudge is None:
            return self._defer(
                event_id,
                status_before,
                ACTION_REJUDGE_DEFERRED,
                "未接入判定入口，本轮不推进（不静默跳过）",
                moment,
            )
        try:
            ok = bool(self.rejudge(row))
        except Exception as exc:
            ok = False
            reason = f"判定入口抛错：{exc}"
        else:
            reason = "" if ok else "判定入口返回失败"
        if not ok:
            return self._defer(
                event_id,
                status_before,
                ACTION_REJUDGE_DEFERRED,
                reason or "重新判定未完成",
                moment,
            )
        self.store.append_audit(
            "recovery_resumed",
            event_id=event_id,
            actor="recovery",
            detail="续跑：重新判定完成",
            at=moment,
        )
        return RecoveryOutcome(
            event_id=event_id,
            action=ACTION_REJUDGED,
            ok=True,
            status_before=status_before,
            status_after=self.store.get_status(event_id),
            detail="续跑重新判定完成",
        )

    # ------------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------------
    def _defer(
        self,
        event_id: str,
        status_before: str,
        action: str,
        detail: str,
        moment: datetime,
    ) -> RecoveryOutcome:
        """推不动就**明说推不动**（留痕，不静默跳过）。"""
        self.store.append_audit(
            "recovery_deferred",
            event_id=event_id,
            actor="recovery",
            detail=detail,
            at=moment,
        )
        logger.warning("续跑未推进 %s：%s", event_id, detail)
        return RecoveryOutcome(
            event_id=event_id,
            action=action,
            ok=False,
            status_before=status_before,
            status_after=status_before,
            detail=detail,
        )
