"""事件幂等键：``MSH-10 + OBX-3 + OBX-14``。

同一条危急值结果重复到达（LIS 重发、MLLP 断线重投、批量重放跑第二遍）
必须**只处理一次** —— 处理两次会推两张卡、回写两条回执，值班群直接刷屏。

为什么是这三样：

* 只用 ``MSH-10``（消息控制号）不够 —— 一条消息里可能带多个检验项目；
* 只用项目码更不行 —— 同一个项目每天出结果，跨标本、跨患者全都会撞；
* 加上观察时刻 ``OBX-14`` 之后，键才真正指向「某标本某项目的某一次结果」。

键本身取 sha256 十六进制（不落原文，避免把标识符写进索引）；``event_id``
是它的前 12 位，带 ``evt-`` 前缀。

**回写侧的幂等键是另一回事**（``message_control_id``），由写回通道自己管，
两处不要混用。

**去重窗口（:class:`IdempotencyWindow`）**：窗口是**进程内快查层**，不是
去重权威。真正的权威永远是 ``events.idempotency_key`` 上的 UNIQUE 约束 ——
那个约束跨进程也成立；窗口只负责三件事：

1. 挡住进程内绝大多数重复投递（省掉一次库查询）；
2. 记命中次数（「这条结果今天被投了几遍」）；
3. 按住内存 —— 窗口过期后条目被回收，**但台账里的事实不删**。

窗口绝不做「过期即放行」的判断：过期只影响快查层，库里那条记录照样
把重复挡下来。宁可多查一次库，也不放过一次重复处理。
"""

from __future__ import annotations

import hashlib
import threading
import time
from datetime import datetime
from typing import Any, Callable, Optional

__all__ = [
    "DEFAULT_DEDUP_WINDOW_SECONDS",
    "IdempotencyWindow",
    "build_idempotency_key",
    "event_id_for_key",
    "idempotency_key_of",
]

#: 去重窗口默认长度（秒）。一天足够覆盖「LIS 重发 / 断线重投 / 当天重放」
#: 这三类重复到达。
DEFAULT_DEDUP_WINDOW_SECONDS = 24 * 3600


def _stamp(observed_at: Any) -> str:
    """观察时刻的稳定文本形式。"""
    if isinstance(observed_at, datetime):
        return observed_at.isoformat()
    return str(observed_at or "").strip()


def build_idempotency_key(
    message_control_id: str,
    item_code: str,
    observed_at: Any,
) -> str:
    """拼事件幂等键（sha256 十六进制）。"""
    parts = [
        str(message_control_id or "").strip(),
        str(item_code or "").strip(),
        _stamp(observed_at),
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def event_id_for_key(key: str) -> str:
    """由幂等键派生事件 ID（同一键 → 同一事件 ID，重放不会长出第二条）。"""
    return f"evt-{key[:12]}"


def idempotency_key_of(event: Any) -> str:
    """由规范化事件反推幂等键（落盘前用，避免各处各拼一遍）。"""
    return build_idempotency_key(
        getattr(event, "message_control_id", ""),
        getattr(event, "item_code", ""),
        getattr(event, "observed_at", None),
    )


class IdempotencyWindow:
    """幂等键去重窗口（进程内 · 线程安全）。

    用法::

        window = IdempotencyWindow(window_seconds=3600)
        window.record(key)        # 第一次见到
        window.seen(key)          # True —— 窗口内见过
        window.hits(key)          # 命中次数

    **线程安全**：守护进程是多线程的（入站推送、到期复查、值班台页面各占
    一条线），窗口用一把锁保护 —— 不加锁的话，「检查再写入」之间的竞态
    会让同一把键被当成两条新事件放行。
    """

    def __init__(
        self,
        window_seconds: int = DEFAULT_DEDUP_WINDOW_SECONDS,
        *,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        self.window_seconds = max(1, int(window_seconds))
        self._clock = clock or time.monotonic
        self._lock = threading.Lock()
        # key → (最近一次见到的时刻, 命中次数)
        self._entries: dict[str, tuple[float, int]] = {}

    # ------------------------------------------------------------------
    def _now(self) -> float:
        return float(self._clock())

    def seen(self, key: str) -> bool:
        """这个键在窗口内见过吗（见过即「大概率是重复投递」）。"""
        name = str(key or "")
        if not name:
            return False
        now = self._now()
        with self._lock:
            entry = self._entries.get(name)
            if entry is None:
                return False
            last_seen, _hits = entry
            if now - last_seen > self.window_seconds:
                # 窗口过期：快查层放行，但**不删计数**（事实不因过期而消失）。
                self._entries[name] = (last_seen, entry[1])
                return False
            return True

    def record(self, key: str) -> int:
        """记一次到达，返回该键的累计命中次数。"""
        name = str(key or "")
        if not name:
            return 0
        now = self._now()
        with self._lock:
            entry = self._entries.get(name)
            if entry is None:
                self._entries[name] = (now, 1)
                return 1
            last_seen, hits = entry
            self._entries[name] = (now, hits + 1)
            return hits + 1

    def hits(self, key: str) -> int:
        """该键累计被投递了几次。"""
        name = str(key or "")
        with self._lock:
            entry = self._entries.get(name)
            return int(entry[1]) if entry else 0

    def forget_expired(self) -> int:
        """回收窗口外过期的条目，返回回收条数（按住内存）。"""
        now = self._now()
        with self._lock:
            expired = [
                key
                for key, (last_seen, _hits) in self._entries.items()
                if now - last_seen > self.window_seconds
            ]
            for key in expired:
                self._entries.pop(key, None)
            return len(expired)

    def stats(self) -> dict[str, Any]:
        """窗口统计（复核 / 运维用）。"""
        now = self._now()
        with self._lock:
            entries = dict(self._entries)
        live = sum(
            1
            for last_seen, _hits in entries.values()
            if now - last_seen <= self.window_seconds
        )
        return {
            "window_seconds": self.window_seconds,
            "tracked_keys": len(entries),
            "live_keys": live,
            "hits": sum(hits for _last_seen, hits in entries.values()),
        }

    def clear(self) -> None:
        """清空窗口（仅测试与运维重置用；台账里的事实不受影响）。"""
        with self._lock:
            self._entries.clear()
