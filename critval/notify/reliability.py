"""通知投递可靠性：退避重试、死信与重复投递抑制（任务 id 42）。

值班群推送有两条硬要求：**不能丢**（推不出去也得有落点），**不能刷屏**
（同一件事不要连着推好几遍）。本模块把这两条落成可执行的三段机制：

===========================  ==============================================
机制                        口径
===========================  ==============================================
**退避重试**                指数退避 + 抖动 + 最大次数；**每一次**出站尝试
                            都留一行投递记录（状态码 / 耗时 / 重试序号 /
                            错误摘要）—— 事后能回答「推了几次、为什么重推」
**死信**                    重试耗尽 → 事件落 ``failed`` + 死信表留档
                            （原卡内容 + 最后一次错误）+ 审计告警；
                            **事件绝不静默丢弃**
**重复投递抑制**            抑制键 = 事件 ID | 投递状态 | 尝试轮次；
                            首次推送（``initial:1``）与到期重推
                            （``retry:1`` / ``retry:2``）轮次键天生不同，
                            **互不抑制**
===========================  ==============================================

**为什么轮次键要带「投递状态」**：到期重推与首次推送是**同一条通知的两次
机会**，不是两次通知。若抑制键只有「事件 ID」，首次推送成功后再到期重推
就会被误判成重复投递而**不再推**—— 值班人没看见的那条就永远躺在台账里。
把「投递状态」放进键里，首次与重推各占一把锁，谁也不误伤谁。

**抖动为什么要有**：多个值班机同时对同一个对端退避时，没有抖动会出现
「惊群」—— 同一秒一起重试、一起再失败。抖动把重试时刻摊开，成本是多等
零点几秒，换来的是重试不再同步撞车。

**本机降级出口**（``critval/notify/local.py``）：企微**未配置**或
**重试耗尽**时，处置卡正文落本机通知队列 —— 值班台看得到「这条推不出去、
卡在这儿」。本机队列**不通知任何人**，只保证「事情有落点」。

**本模块不改业务状态**：投递层只负责「推没推出去」；落 ``failed`` 这类
状态迁移由 :meth:`ReliableNotifier.notify_event` 显式发起，并且**只落合法的
迁移**（已被人工确认的事件不会被自动改回失败态 —— 既成事实不能被自动路径
改写）。
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Optional

from critval.ledger.states import STATUS_FAILED, describe_status, validate_transition

__all__ = [
    "BackoffPolicy",
    "DeliveryAttempt",
    "DeliveryOutcome",
    "ReliableNotifier",
    "ROUND_KIND_INITIAL",
    "ROUND_KIND_RETRY",
    "build_round_key",
    "suppression_key",
]

logger = logging.getLogger("critval.notify.reliability")

#: 投递状态：首次推送。
ROUND_KIND_INITIAL = "initial"

#: 投递状态：到期重推（任务 id 10 的复查重推走这一类）。
ROUND_KIND_RETRY = "retry"

#: 默认最多尝试几次（含第一次）。
DEFAULT_MAX_ATTEMPTS = 3

#: 退避基数（秒）：第一次重试等这么久。
DEFAULT_BACKOFF_BASE_SECONDS = 0.5

#: 退避倍数：每重试一次，等待时间乘这个数。
DEFAULT_BACKOFF_FACTOR = 2.0

#: 单次等待上限（秒）—— 再长也不该让危急值干等。
DEFAULT_MAX_BACKOFF_SECONDS = 8.0

#: 抖动比例：0 = 不抖动；0.2 = 等待时长在 ±20% 之间浮动。
DEFAULT_JITTER_RATIO = 0.2

#: 陈旧轮次的抢占窗口（秒）：``in_flight`` 超过这么久就允许重新抢占
#: （进程崩在推送中途的情形）。
DEFAULT_STALE_AFTER_SECONDS = 300


# ---------------------------------------------------------------------------
# 轮次键与抑制键
# ---------------------------------------------------------------------------


def build_round_key(kind: str, number: int) -> str:
    """轮次键：``initial:1`` / ``retry:2``。

    **首次推送与到期重推的轮次键天生不同** —— 这就是「互不抑制」的物理基础。
    """
    return f"{str(kind or '').strip()}:{int(number)}"


def suppression_key(event_id: str, kind: str, round_key: str) -> str:
    """抑制键：``事件 ID|投递状态|尝试轮次``。

    同一把键只允许**一个**投递者推；别人再来就是重复投递，直接抑制。
    """
    return (
        f"{str(event_id or '').strip()}|"
        f"{str(kind or '').strip()}|"
        f"{str(round_key or '').strip()}"
    )


# ---------------------------------------------------------------------------
# 退避策略
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BackoffPolicy:
    """指数退避 + 抖动 + 最大次数。"""

    base_seconds: float = DEFAULT_BACKOFF_BASE_SECONDS
    factor: float = DEFAULT_BACKOFF_FACTOR
    max_seconds: float = DEFAULT_MAX_BACKOFF_SECONDS
    jitter_ratio: float = DEFAULT_JITTER_RATIO
    max_attempts: int = DEFAULT_MAX_ATTEMPTS

    @classmethod
    def from_config(cls) -> "BackoffPolicy":
        """按环境变量装配（``CRITVAL_NOTIFY_*``）。配置非法时用默认值。"""
        from critval.config import Config

        def _number(key: str, fallback: float) -> float:
            raw = Config.get_env(key)
            try:
                return float(raw) if raw not in (None, "") else fallback
            except (TypeError, ValueError):
                return fallback

        return cls(
            base_seconds=_number("CRITVAL_NOTIFY_BACKOFF_BASE_SECONDS", DEFAULT_BACKOFF_BASE_SECONDS),
            factor=_number("CRITVAL_NOTIFY_BACKOFF_FACTOR", DEFAULT_BACKOFF_FACTOR),
            max_seconds=_number("CRITVAL_NOTIFY_MAX_BACKOFF_SECONDS", DEFAULT_MAX_BACKOFF_SECONDS),
            jitter_ratio=_number("CRITVAL_NOTIFY_JITTER_RATIO", DEFAULT_JITTER_RATIO),
            max_attempts=int(_number("CRITVAL_NOTIFY_MAX_ATTEMPTS", DEFAULT_MAX_ATTEMPTS)),
        )

    def delay_for(self, attempt_no: int, rng: Optional[Any] = None) -> float:
        """第 ``attempt_no`` 次失败后该等多久（指数退避 + 抖动 + 封顶）。

        * **指数**：``base × factor^(n-1)``；
        * **封顶**：不超过 ``max_seconds``（危急值等不起）；
        * **抖动**：在 ±``jitter_ratio`` 之间浮动，**但永不超过封顶** ——
          抖动只让等待更短或略长，不会突破「单次等待上限」这条口径。
        """
        raw = self.base_seconds * (self.factor ** max(0, int(attempt_no) - 1))
        capped = min(max(0.0, raw), max(0.0, self.max_seconds))
        if self.jitter_ratio <= 0:
            return capped
        source = rng if rng is not None else random
        try:
            unit = float(source.random())
        except Exception:  # noqa: BLE001 —— 抖动源坏了不该让推送崩掉
            unit = 0.5
        spread = self.jitter_ratio * (2.0 * unit - 1.0)
        return max(0.0, min(capped * (1.0 + spread), self.max_seconds))


# ---------------------------------------------------------------------------
# 投递记录
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DeliveryAttempt:
    """一次出站尝试的留痕。"""

    attempt_no: int
    ok: bool = False
    status_code: int = 0
    elapsed_ms: int = 0
    waited_ms: int = 0
    error: str = ""
    attempted_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempt_no": self.attempt_no,
            "ok": self.ok,
            "status_code": self.status_code,
            "elapsed_ms": self.elapsed_ms,
            "waited_ms": self.waited_ms,
            "error": self.error,
            "attempted_at": self.attempted_at,
        }


@dataclass(frozen=True)
class DeliveryOutcome:
    """一次投递（一个轮次）的结论。"""

    event_id: str
    kind: str = ROUND_KIND_INITIAL
    round_key: str = "1"
    ok: bool = False
    suppressed: bool = False
    dead_lettered: bool = False
    fallback_used: bool = False
    channel: str = ""
    card_id: str = ""
    error: str = ""
    attempts: tuple[DeliveryAttempt, ...] = ()
    local_record: Optional[Any] = None

    @property
    def attempt_count(self) -> int:
        """真实发出去几次（含失败的那些次）。"""
        return len(self.attempts)

    @property
    def last_error(self) -> str:
        """最后一次错误摘要。"""
        for attempt in reversed(self.attempts):
            if attempt.error:
                return attempt.error
        return self.error

    @property
    def total_elapsed_ms(self) -> int:
        return sum(attempt.elapsed_ms for attempt in self.attempts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "kind": self.kind,
            "round_key": self.round_key,
            "ok": self.ok,
            "suppressed": self.suppressed,
            "dead_lettered": self.dead_lettered,
            "fallback_used": self.fallback_used,
            "channel": self.channel,
            "card_id": self.card_id,
            "error": self.error,
            "attempt_count": self.attempt_count,
            "attempts": [attempt.to_dict() for attempt in self.attempts],
        }


# ---------------------------------------------------------------------------
# 可靠性投递器
# ---------------------------------------------------------------------------

#: 推送出口：给一条事件行，返回一次尝试的结论（``.ok`` / ``.status`` /
#: ``.error`` / ``.retryable``）。企微出口见 ``WeComNotifier.send_once``。
PushCallable = Callable[[Mapping[str, Any]], Any]


def _normalize(result: Any) -> tuple[bool, int, str, bool]:
    """把推送出口的返回值归一成 ``(成功, 状态码, 错误摘要, 可重试)``。"""
    ok = bool(getattr(result, "ok", False))
    try:
        status = int(getattr(result, "status", 0) or 0)
    except (TypeError, ValueError):
        status = 0
    error = str(getattr(result, "error", "") or "")
    retryable = bool(getattr(result, "retryable", False))
    return ok, status, error, retryable


class ReliableNotifier:
    """带退避重试 / 死信 / 重复投递抑制的推送器。

    * ``store`` —— 台账（投递记录 / 死信 / 投递轮次都落在这里）；
    * ``policy`` —— 退避策略（次数 / 基数 / 倍数 / 封顶 / 抖动）；
    * ``local`` —— 本机降级出口（可选）：重试耗尽时把原卡落本机队列。
    """

    def __init__(
        self,
        store: Any,
        *,
        channel: str = "wecom",
        policy: Optional[BackoffPolicy] = None,
        sleep: Optional[Callable[[float], None]] = None,
        rng: Optional[Any] = None,
        clock: Optional[Callable[[], datetime]] = None,
        local: Optional[Any] = None,
        stale_after_seconds: int = DEFAULT_STALE_AFTER_SECONDS,
    ) -> None:
        self.store = store
        self.channel = channel or "unknown"
        self.policy = policy or BackoffPolicy()
        self._sleep = sleep if sleep is not None else time.sleep
        self._rng = rng
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.local = local
        self.stale_after_seconds = int(stale_after_seconds)

    # ------------------------------------------------------------------
    # 投递
    # ------------------------------------------------------------------
    def deliver(
        self,
        event_id: str,
        *,
        push: PushCallable,
        content: str = "",
        card_id: str = "",
        kind: str = ROUND_KIND_INITIAL,
        round_key: str = "1",
        channel: str = "",
        notify_target: str = "",
        due_minutes: Optional[int] = None,
    ) -> DeliveryOutcome:
        """投一轮（一个轮次）：抑制判定 → 退避重试 → 逐次留痕 → 死信兜底。

        **不碰业务状态** —— 状态迁移由 :meth:`notify_event` 显式发起。
        """
        target_channel = channel or self.channel
        key = suppression_key(event_id, kind, round_key)
        claimed = self.store.claim_delivery_round(
            key,
            event_id=event_id,
            kind=kind,
            attempt_round=round_key,
            channel=target_channel,
            stale_after_seconds=self.stale_after_seconds,
        )
        if not claimed:
            logger.info(
                "重复投递被抑制：事件 %s 轮次 %s|%s 已投递过，不再重复推送",
                event_id,
                kind,
                round_key,
            )
            return DeliveryOutcome(
                event_id=event_id,
                kind=kind,
                round_key=round_key,
                ok=True,
                suppressed=True,
                channel=target_channel,
                card_id=card_id,
                error="同一轮次已投递过（抑制键命中），本次不重复推送",
            )

        row = self.store.get_event(event_id) or {}
        attempts: list[DeliveryAttempt] = []
        waited_ms = 0
        last_error = ""
        max_attempts = max(1, int(self.policy.max_attempts))

        for attempt_no in range(1, max_attempts + 1):
            started = time.monotonic()
            try:
                result = push(row)
            except Exception as exc:  # noqa: BLE001 —— 出口抛错也要留痕，不吞
                ok, status, error, retryable = False, 0, f"推送出口抛错：{exc}", True
            else:
                ok, status, error, retryable = _normalize(result)
            elapsed_ms = int((time.monotonic() - started) * 1000)
            attempt = DeliveryAttempt(
                attempt_no=attempt_no,
                ok=ok,
                status_code=status,
                elapsed_ms=elapsed_ms,
                waited_ms=waited_ms,
                error=error,
                attempted_at=self._clock().isoformat(),
            )
            attempts.append(attempt)
            self.store.record_delivery_attempt(
                event_id,
                card_id=card_id,
                kind=kind,
                round_key=round_key,
                attempt=attempt_no,
                channel=target_channel,
                ok=ok,
                status_code=status,
                elapsed_ms=elapsed_ms,
                waited_ms=waited_ms,
                error=error,
            )
            if ok:
                break
            last_error = error
            if not retryable:
                # 契约错（凭据不对、地址非法……）重试一万次也是一样的错。
                break
            if attempt_no >= max_attempts:
                break
            delay = self.policy.delay_for(attempt_no, self._rng)
            self._sleep(delay)
            waited_ms = int(round(delay * 1000))

        if attempts and attempts[-1].ok:
            self.store.mark_delivery_round(key, "delivered")
            return DeliveryOutcome(
                event_id=event_id,
                kind=kind,
                round_key=round_key,
                ok=True,
                channel=target_channel,
                card_id=card_id,
                attempts=tuple(attempts),
            )

        # ---- 重试耗尽：落死信 + 告警 + 本机降级出口（事件不丢） ----------
        if not last_error and attempts:
            last_error = attempts[-1].error
        self.store.mark_delivery_round(key, "failed")
        dead_letter_id = self.store.record_dead_letter(
            event_id,
            card_id=card_id,
            kind=kind,
            round_key=round_key,
            channel=target_channel,
            attempts=len(attempts),
            last_error=last_error,
            card_content=content,
        )
        self.store.append_audit(
            "notify_dead_lettered",
            event_id=event_id,
            actor="notify",
            dept_code=str(row.get("dept_code", "") or ""),
            specimen_no=str(row.get("specimen_no", "") or ""),
            item_code=str(row.get("item_code", "") or ""),
            message_control_id=str(row.get("message_control_id", "") or ""),
            detail=(
                f"推送重试 {len(attempts)} 次仍失败，已落死信（死信号 {dead_letter_id}）；"
                f"最后一次错误：{last_error or '未记录'}；事件保留在台账里等待人工处理"
            ),
        )
        logger.warning(
            "危急值推送重试耗尽：事件 %s 轮次 %s|%s 落死信（%s 次）",
            event_id,
            kind,
            round_key,
            len(attempts),
        )
        local_record = None
        if self.local is not None:
            local_record = self.local.send(
                content,
                event_id=event_id,
                card_id=card_id,
                notify_target=notify_target,
                due_minutes=due_minutes,
                kind=kind,
                round_key=round_key,
                reason=f"推送重试 {len(attempts)} 次仍失败：{last_error or '未记录错误'}",
            )
        return DeliveryOutcome(
            event_id=event_id,
            kind=kind,
            round_key=round_key,
            ok=False,
            dead_lettered=True,
            fallback_used=local_record is not None,
            channel=target_channel,
            card_id=card_id,
            error=last_error,
            attempts=tuple(attempts),
            local_record=local_record,
        )

    # ------------------------------------------------------------------
    # 业务副作用
    # ------------------------------------------------------------------
    def notify_event(
        self,
        event_id: str,
        *,
        push: PushCallable,
        content: str = "",
        card_id: str = "",
        kind: str = ROUND_KIND_INITIAL,
        round_key: str = "1",
        notify_target: str = "",
        due_minutes: Optional[int] = None,
        attempt: Optional[int] = None,
        channel: str = "",
    ) -> DeliveryOutcome:
        """投一轮 + 落业务副作用（状态迁移 / 通知记录 / 审计）。

        * 首次推送成功 → 事件落 ``notified``（含上报时限与到期时刻）；
        * 重推成功 → 追加一条 ``kind=retry`` 通知记录（首次推送记录原样保留）；
        * 重试耗尽 → 事件落 ``failed``（**只落合法迁移**）+ 死信 + 告警。
        """
        outcome = self.deliver(
            event_id,
            push=push,
            content=content,
            card_id=card_id,
            kind=kind,
            round_key=round_key,
            channel=channel,
            notify_target=notify_target,
            due_minutes=due_minutes,
        )
        if outcome.suppressed:
            return outcome
        if outcome.ok:
            if kind == ROUND_KIND_INITIAL:
                self.store.mark_notified(
                    event_id,
                    notify_target=notify_target,
                    due_minutes=due_minutes,
                    channel=outcome.channel,
                    card_id=card_id,
                    detail=f"已推送至值班渠道（{outcome.channel}）",
                )
            else:
                seq = int(attempt or 1)
                self.store.record_notification(
                    event_id,
                    kind=kind,
                    attempt=seq,
                    channel=outcome.channel,
                    card_id=card_id,
                    detail=f"第 {seq} 次复查重推（首次推送记录原样保留）",
                )
                self.store.bump_retry(event_id, retries=seq)
                self.store.append_audit(
                    "notify_retry",
                    event_id=event_id,
                    actor="retry",
                    detail=(
                        f"到期复查重推：第 {seq} 次（轮次 {kind}:{round_key}），"
                        f"渠道 {outcome.channel}；首次推送记录原样保留"
                    ),
                )
            return outcome

        self.mark_failed(event_id, outcome)
        return outcome

    def mark_failed(self, event_id: str, outcome: DeliveryOutcome) -> bool:
        """把重试耗尽的事件落 ``failed`` —— **只落合法迁移**。

        已被人工确认（``acknowledged``）的事件**不会被自动路径改回失败态**：
        既成事实不可被自动路径改写。这种情形只留死信与告警，状态保持原样。
        """
        current = self.store.get_status(event_id)
        if not current or current == STATUS_FAILED:
            return False
        try:
            validate_transition(current, STATUS_FAILED)
        except Exception:
            logger.warning(
                "事件 %s 当前状态为 %s，不允许自动落 %s；死信与告警已留，状态保持原样",
                event_id,
                describe_status(current),
                STATUS_FAILED,
            )
            return False
        self.store.set_status(
            event_id,
            STATUS_FAILED,
            actor="notify",
            detail=(
                f"推送重试耗尽（{outcome.attempt_count} 次），落 {STATUS_FAILED}；"
                f"事件保留在台账里等待人工处理"
            ),
        )
        return True
