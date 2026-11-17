"""幂等单测：重复到达只处理一次 + 并发写同一幂等键只落一条。

盯住三件事：

* **重复不到达处理两遍** —— 同一条结果重复投递，台账只多一行；
* **并发也只落一条** —— 多条线程同时收同一帧（守护进程是多线程的，
  入站推送、到期复查、值班台页面各占一条线），幂等键上只有一行；
* **去重窗口只管快查，不管事实** —— 窗口过期后快查层放行，但库里的
  记录照样把重复挡下来（窗口过期 ≠ 事实失效）。
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from critval.ledger.idempotency import (
    DEFAULT_DEDUP_WINDOW_SECONDS,
    IdempotencyWindow,
    build_idempotency_key,
    event_id_for_key,
    idempotency_key_of,
)
from critval.ledger.store import LedgerStore
from critval.models import CriticalEvent


def _event(event_id: str = "evt-idem-0001", mcid: str = "MC-IDEM-0001") -> CriticalEvent:
    return CriticalEvent(
        event_id=event_id,
        message_control_id=mcid,
        specimen_no="SP-0001",
        item_code="718-7",
        item_name="血红蛋白",
        value=Decimal("25"),
        unit="g/L",
        observed_at=datetime(2026, 1, 2, 8, 30, tzinfo=timezone.utc),
        dept_code="LAB",
    )


@pytest.fixture()
def store() -> LedgerStore:
    instance = LedgerStore(":memory:")
    try:
        yield instance
    finally:
        instance.close()


# ---------------------------------------------------------------------------
# 1. 键的构造
# ---------------------------------------------------------------------------


def test_键由消息控制号项目码观察时刻三样拼成():
    key = build_idempotency_key("MC-1", "718-7", "2026-01-02T08:30:00")
    same = build_idempotency_key("MC-1", "718-7", "2026-01-02T08:30:00")
    other = build_idempotency_key("MC-1", "718-7", "2026-01-02T09:30:00")

    assert key == same
    assert key != other
    assert event_id_for_key(key) == f"evt-{key[:12]}"


def test_键由事件反推与手拼一致():
    event = _event()
    assert idempotency_key_of(event) == build_idempotency_key(
        event.message_control_id, event.item_code, event.observed_at
    )


def test_键不落原始标识():
    """键是 sha256 —— 索引里不该出现消息控制号原文。"""
    key = build_idempotency_key("MC-SECRET-0001", "718-7", "2026-01-02T08:30:00")
    assert "MC-SECRET-0001" not in key
    assert len(key) == 64


# ---------------------------------------------------------------------------
# 2. 重复到达
# ---------------------------------------------------------------------------


def test_重复到达只落一条且既有事实不被覆盖(store: LedgerStore):
    event = _event()
    assert store.record_event(event) is True
    store.set_status("evt-idem-0001", "judged")

    assert store.record_event(event) is False
    assert store.count_events() == 1
    # 既有事实没被覆盖：判定状态还在
    assert store.get_status("evt-idem-0001") == "judged"


def test_重复到达累加命中次数(store: LedgerStore):
    event = _event()
    store.record_event(event)
    key = idempotency_key_of(event)

    for _ in range(3):
        store.record_event(event)

    assert store.idempotency_hits(key) == 4
    assert store.count_events() == 1


def test_不同观察时刻算两条(store: LedgerStore):
    first = _event()
    second = _event(event_id="evt-idem-0002")
    second = CriticalEvent(
        event_id="evt-idem-0002",
        message_control_id="MC-IDEM-0001",
        specimen_no="SP-0001",
        item_code="718-7",
        item_name="血红蛋白",
        value=Decimal("25"),
        unit="g/L",
        observed_at=datetime(2026, 1, 2, 9, 30, tzinfo=timezone.utc),
        dept_code="LAB",
    )
    assert store.record_event(first) is True
    assert store.record_event(second) is True
    assert store.count_events() == 2


# ---------------------------------------------------------------------------
# 3. 并发
# ---------------------------------------------------------------------------


def test_并发写同一幂等键只落一条(store: LedgerStore):
    """8 条线程同时收同一帧 —— 台账只多一行，只有一路拿到 True。"""
    event = _event()
    results: list[bool] = []
    lock = threading.Lock()
    barrier = threading.Barrier(8)

    def worker() -> None:
        barrier.wait()
        outcome = store.record_event(event)
        with lock:
            results.append(outcome)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert store.count_events() == 1
    assert results.count(True) == 1, f"只允许一路插进去，实际 {results.count(True)} 路"
    assert results.count(False) == 7


def test_并发写不同幂等键各落一条(store: LedgerStore):
    """不同结果各自入账 —— 并发不能把不同的事件挤成一条。"""
    barrier = threading.Barrier(6)
    lock = threading.Lock()
    created: list[bool] = []

    def worker(index: int) -> None:
        event = _event(event_id=f"evt-idem-c{index}", mcid=f"MC-IDEM-C{index}")
        barrier.wait()
        outcome = store.record_event(event)
        with lock:
            created.append(outcome)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert store.count_events() == 6
    assert created.count(True) == 6


# ---------------------------------------------------------------------------
# 4. 去重窗口
# ---------------------------------------------------------------------------


def test_窗口内见过就认得():
    clock = {"now": 1000.0}
    window = IdempotencyWindow(window_seconds=60, clock=lambda: clock["now"])

    assert window.seen("k1") is False
    assert window.record("k1") == 1
    assert window.seen("k1") is True
    assert window.hits("k1") == 1


def test_窗口过期后快查层放行():
    clock = {"now": 1000.0}
    window = IdempotencyWindow(window_seconds=60, clock=lambda: clock["now"])
    window.record("k1")
    clock["now"] += 61

    assert window.seen("k1") is False
    # 命中次数不因过期而清零（事实不因过期而消失）
    assert window.hits("k1") == 1


def test_窗口回收过期条目():
    clock = {"now": 1000.0}
    window = IdempotencyWindow(window_seconds=60, clock=lambda: clock["now"])
    window.record("k1")
    window.record("k2")
    clock["now"] += 61

    assert window.forget_expired() == 2
    assert window.stats()["tracked_keys"] == 0


def test_窗口统计给出键数与命中数():
    window = IdempotencyWindow(window_seconds=60)
    window.record("k1")
    window.record("k1")
    window.record("k2")

    stats = window.stats()
    assert stats["tracked_keys"] == 2
    assert stats["hits"] == 3
    assert stats["window_seconds"] == 60


def test_默认窗口长度是一天():
    assert DEFAULT_DEDUP_WINDOW_SECONDS == 24 * 3600


def test_窗口过期后库里仍然挡重复(store: LedgerStore):
    """窗口过期只影响快查层 —— 库里的事实照样把重复挡下来。"""
    event = _event()
    store.record_event(event)
    key = idempotency_key_of(event)
    store.idempotency_window.record(key)

    # 快查层把这个键忘了（等价于窗口过期回收）
    store.idempotency_window.clear()
    assert store.idempotency_window.seen(key) is False

    assert store.record_event(event) is False
    assert store.count_events() == 1


def test_台账幂等统计(store: LedgerStore):
    event = _event()
    store.record_event(event)
    store.record_event(event)

    stats = store.idempotency_stats()
    assert stats["db_keys"] == 1
    assert stats["db_hits"] == 2
    assert stats["window_seconds"] == DEFAULT_DEDUP_WINDOW_SECONDS
