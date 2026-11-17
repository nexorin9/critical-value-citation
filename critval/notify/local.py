"""本机通知降级出口（任务 id 11 · 企微不可用时事件不丢 · 任务 id 42 队列化）。

值班群推不出去的时候，**危急值不能跟着丢**。本模块是那条兜底：

* 处置卡正文落到**本机通知队列**（JSONL，一行一条）；
* 每条记录带事件号、卡片号、推送时刻、通知对象、上报时限、**投递轮次**；
* 值班台上能查到「这条推不出去、卡在这儿」。

它**不是**第二个推送通道 —— 它不通知任何人，只保证**事情有落点**。
值班人要知道「这条没推出去」，得靠值班台 / 台账里的痕迹，不靠猜。

**两种落队列的时机**（任务 id 42）：

1. **企微未配置** —— 部署时忘了配 webhook；
2. **重试耗尽** —— 配了，但推了 ``max_attempts`` 次仍推不出去（网络挂了、
   限流了、webhook 失效了）。这时事件已经落了死信与 ``failed`` 状态，
   本机队列让值班台看得到「这条还卡在这儿」。

记录落在**运行期本地文件**（默认 ``data/local_notifications.jsonl``，
已被 ``.gitignore`` 排除）—— 可再生数据，不进仓库。

**关于「改写」**：本队列文件是**运行期队列**，不是审计流水 —— 值班人处理
完一条要能把它标成「已处理」，所以 ``mark_handled`` 会整体重写文件。
审计流水在 SQLite 的 append-only 表里，不受这里影响。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

__all__ = [
    "CHANNEL_LOCAL",
    "DEFAULT_LOCAL_NOTIFICATION_PATH",
    "LocalNotification",
    "LocalNotifier",
    "NotifyOutcome",
    "FallbackNotifier",
]

#: 本机通知记录的通道名（台账与值班台上认这个值）。
CHANNEL_LOCAL = "local"

#: 本机通知记录默认落盘位置（运行期数据，已 gitignore）。
DEFAULT_LOCAL_NOTIFICATION_PATH = Path("data/local_notifications.jsonl")


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class LocalNotification:
    """一条本机通知记录。"""

    event_id: str
    card_id: str = ""
    channel: str = CHANNEL_LOCAL
    content: str = ""
    notify_target: str = ""
    due_minutes: Optional[int] = None
    reason: str = ""
    created_at: datetime = field(default_factory=_now)
    #: 投递状态（``initial`` 首次推送 / ``retry`` 到期重推）—— 任务 id 42。
    kind: str = "initial"
    #: 投递轮次键（``initial:1`` / ``retry:2``）—— 任务 id 42。
    round_key: str = ""
    #: 是否已被人处理（值班人手动接走）。
    handled: bool = False
    handled_by: str = ""
    handled_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "card_id": self.card_id,
            "channel": self.channel,
            "content": self.content,
            "notify_target": self.notify_target,
            "due_minutes": self.due_minutes,
            "reason": self.reason,
            "created_at": self.created_at.isoformat(),
            "kind": self.kind,
            "round_key": self.round_key,
            "handled": self.handled,
            "handled_by": self.handled_by,
            "handled_at": self.handled_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "LocalNotification":
        raw = str(data.get("created_at", "") or "")
        try:
            moment = datetime.fromisoformat(raw)
        except ValueError:
            moment = _now()
        due = data.get("due_minutes")
        minutes: Optional[int] = None
        if due not in (None, ""):
            try:
                minutes = int(due)
            except (TypeError, ValueError):
                minutes = None
        return cls(
            event_id=str(data.get("event_id", "") or ""),
            card_id=str(data.get("card_id", "") or ""),
            channel=str(data.get("channel", CHANNEL_LOCAL) or CHANNEL_LOCAL),
            content=str(data.get("content", "") or ""),
            notify_target=str(data.get("notify_target", "") or ""),
            due_minutes=minutes,
            reason=str(data.get("reason", "") or ""),
            created_at=moment,
            kind=str(data.get("kind", "initial") or "initial"),
            round_key=str(data.get("round_key", "") or ""),
            handled=bool(data.get("handled", False)),
            handled_by=str(data.get("handled_by", "") or ""),
            handled_at=str(data.get("handled_at", "") or ""),
        )


class LocalNotifier:
    """把处置卡正文落到本机通知队列。

    * 落盘：``data/local_notifications.jsonl``（一行一条 JSON）；
    * 追加写：每条一行，不改旧行 —— 「推过几次、每次什么时候」看得见；
    * ``mark_handled``：值班人处理完后把某一条标成「已处理」（队列文件整体
      重写 —— 它是运行期队列，不是审计流水）。
    """

    def __init__(
        self,
        path: Optional[Path | str] = None,
        *,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self.path = Path(path) if path is not None else DEFAULT_LOCAL_NOTIFICATION_PATH
        self._clock = clock or _now

    # ---- 构造 -------------------------------------------------------------
    @classmethod
    def from_config(cls) -> "LocalNotifier":
        """按环境变量装配（``CRITVAL_LOCAL_NOTIFICATION_PATH``）。"""
        from critval.config import Config

        raw = Config.get_env("CRITVAL_LOCAL_NOTIFICATION_PATH") or ""
        return cls(Path(raw) if raw else DEFAULT_LOCAL_NOTIFICATION_PATH)

    # ---- 写 ---------------------------------------------------------------
    def send(
        self,
        content: str,
        *,
        event_id: str = "",
        card_id: str = "",
        notify_target: str = "",
        due_minutes: Optional[int] = None,
        reason: str = "",
        kind: str = "initial",
        round_key: str = "",
    ) -> LocalNotification:
        """落一条本机通知记录，返回落下来的那一条。"""
        record = LocalNotification(
            event_id=str(event_id or ""),
            card_id=str(card_id or ""),
            channel=CHANNEL_LOCAL,
            content=str(content or ""),
            notify_target=str(notify_target or ""),
            due_minutes=due_minutes,
            reason=str(reason or ""),
            created_at=self._clock(),
            kind=str(kind or "initial"),
            round_key=str(round_key or ""),
        )
        self._append(record)
        return record

    def _append(self, record: LocalNotification) -> None:
        line = json.dumps(record.to_dict(), ensure_ascii=False)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(line + "\n")
        except OSError:
            # 落不进去也不能把异常抛给推送主流程 —— 但**不能装作写成功了**，
            # 所以这里返回 False 由调用方在审计里留痕。
            return

    def mark_handled(
        self,
        event_id: str,
        *,
        handler: str = "",
        round_key: str = "",
    ) -> int:
        """把某条队列记录标成「已处理」，返回标记了几条。

        ``round_key`` 非空时只标同一轮次的那一条 —— 同一事件可能有多个轮次
        的队列记录（首次推送一条、重推一条），值班人处理的是哪一条要说得清。
        """
        records = list(self.records())
        moment = self._clock().isoformat()
        changed = 0
        updated: list[LocalNotification] = []
        for record in records:
            if record.event_id != str(event_id or ""):
                updated.append(record)
                continue
            if round_key and record.round_key != round_key:
                updated.append(record)
                continue
            if record.handled:
                updated.append(record)
                continue
            updated.append(
                replace(
                    record,
                    handled=True,
                    handled_by=str(handler or ""),
                    handled_at=moment,
                )
            )
            changed += 1
        if changed:
            self._rewrite(updated)
        return changed

    def _rewrite(self, records: list[LocalNotification]) -> None:
        """整体重写队列文件（原子替换：先写临时文件再替换）。"""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            with tmp.open("w", encoding="utf-8") as stream:
                for record in records:
                    stream.write(json.dumps(record.to_dict(), ensure_ascii=False) + "\n")
            tmp.replace(self.path)
        except OSError:
            return

    # ---- 读 ---------------------------------------------------------------
    def records(self) -> tuple[LocalNotification, ...]:
        """读回全部本机通知记录（按落盘顺序）。"""
        try:
            raw = self.path.read_text(encoding="utf-8")
        except (FileNotFoundError, OSError):
            return ()
        out: list[LocalNotification] = []
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, Mapping):
                out.append(LocalNotification.from_dict(payload))
        return tuple(out)

    def records_of(self, event_id: str) -> tuple[LocalNotification, ...]:
        """某个事件的本机通知记录。"""
        return tuple(r for r in self.records() if r.event_id == str(event_id or ""))

    def pending(self) -> tuple[LocalNotification, ...]:
        """**还没被人处理**的队列记录（值班台首页就看这一份）。"""
        return tuple(r for r in self.records() if not r.handled)


@dataclass(frozen=True)
class NotifyOutcome:
    """一次推送的最终结论（企微优先，失败落本机）。"""

    ok: bool
    channel: str = ""
    fallback_used: bool = False
    error: str = ""
    attempts: int = 0
    errcode: int = 0
    local_record: Optional[LocalNotification] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "channel": self.channel,
            "fallback_used": self.fallback_used,
            "error": self.error,
            "attempts": self.attempts,
            "errcode": self.errcode,
            "local_record": self.local_record.to_dict() if self.local_record else None,
        }


class FallbackNotifier:
    """企微优先；企微未配置或推送失败 → 落本机通知记录，**事件不丢**。

    两种降级触发方式：

    * **没配企微**（``primary is None``）—— 部署时忘了配 webhook；
    * **企微推不出去**（网络挂了、限流了、webhook 失效了）。

    两种都**留痕**：本机通知记录里写清「为什么没推出去」。
    """

    def __init__(
        self,
        primary: Optional[Any] = None,
        fallback: Optional[LocalNotifier] = None,
    ) -> None:
        self.primary = primary
        self.fallback = fallback if fallback is not None else LocalNotifier()

    def send_card(
        self,
        card: Any,
        *,
        confirm_url: str = "",
        event_id: str = "",
        notify_target: str = "",
        due_minutes: Optional[int] = None,
    ) -> NotifyOutcome:
        """推送处置卡；推不出去就落本机记录。"""
        from critval.card.renderer import render_markdown

        content = render_markdown(card, confirm_url)
        if self.primary is None:
            return self._fallback(
                content,
                event_id=event_id or getattr(card.event, "event_id", ""),
                card_id=getattr(card, "card_id", ""),
                notify_target=notify_target,
                due_minutes=due_minutes,
                reason="未配置企微群机器人 webhook",
            )
        result = self.primary.send_card(card, confirm_url=confirm_url)
        if result.ok:
            return NotifyOutcome(
                ok=True,
                channel="wecom",
                attempts=result.attempts,
                errcode=result.errcode,
            )
        return self._fallback(
            content,
            event_id=event_id or getattr(card.event, "event_id", ""),
            card_id=getattr(card, "card_id", ""),
            notify_target=notify_target,
            due_minutes=due_minutes,
            reason=f"企微推送失败：{result.error or result.errmsg}",
            attempts=result.attempts,
            errcode=result.errcode,
        )

    def _fallback(
        self,
        content: str,
        *,
        event_id: str,
        card_id: str,
        notify_target: str,
        due_minutes: Optional[int],
        reason: str,
        attempts: int = 0,
        errcode: int = 0,
    ) -> NotifyOutcome:
        record = self.fallback.send(
            content,
            event_id=event_id,
            card_id=card_id,
            notify_target=notify_target,
            due_minutes=due_minutes,
            reason=reason,
        )
        return NotifyOutcome(
            ok=False,
            channel=CHANNEL_LOCAL,
            fallback_used=True,
            error=reason,
            attempts=attempts,
            errcode=errcode,
            local_record=record,
        )
