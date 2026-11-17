"""到期重推单测：pending_ack 到期复查重推 + 重推不重复计数 + 耗尽落 failed。

盯住四件事：

* **不到期不重推** —— 时限没到不打扰值班人；
* **重推不重复计数** —— 「今天推送了多少条」按去重事件数算，重推不灌大；
  首次推送时刻不被覆盖，原推送记录原样保留；
* **失败不静默** —— 推送出口抛错也记账，事件留在台账里；
* **耗尽落 failed 并告警** —— 重试到上限仍推不出去，事件转 ``failed``、
  写告警审计，**不静默丢弃**。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from critval.ledger.retry import (
    AWAITING_ACK_STATUSES,
    NOTIFY_KIND_INITIAL,
    NOTIFY_KIND_RETRY,
    RetryPlanner,
    RetryRunner,
)
from critval.ledger.store import LedgerStore
from critval.models import CriticalEvent

PUSHED_AT = datetime(2026, 1, 2, 8, 30, tzinfo=timezone.utc)


def _event(event_id: str = "evt-retry-0001") -> CriticalEvent:
    return CriticalEvent(
        event_id=event_id,
        message_control_id=f"MC-{event_id}",
        specimen_no="SP-0001",
        item_code="718-7",
        item_name="血红蛋白",
        value=25,
        unit="g/L",
        observed_at=PUSHED_AT,
        dept_code="LAB",
    )


@pytest.fixture()
def store() -> LedgerStore:
    instance = LedgerStore(":memory:")
    try:
        yield instance
    finally:
        instance.close()


def _pushed(store: LedgerStore, event_id: str = "evt-retry-0001", due_minutes: int = 10):
    """把一条事件推到「已推送待回执」，并记下首次推送。"""
    store.record_event(_event(event_id))
    store.set_status(event_id, "judged")
    store.set_status(event_id, "card_built")
    store.mark_notified(
        event_id,
        notify_target="检验科值班技师",
        due_minutes=due_minutes,
        channel="wecom",
        at=PUSHED_AT,
    )
    store.mark_pending_ack(event_id, at=PUSHED_AT)
    return store.get_event(event_id)


# ---------------------------------------------------------------------------
# 1. 计划
# ---------------------------------------------------------------------------


def test_不到期不重推(store: LedgerStore):
    _pushed(store)
    planner = RetryPlanner(store)

    assert planner.due_events(PUSHED_AT + timedelta(minutes=5)) == []
    assert planner.plan(PUSHED_AT + timedelta(minutes=5)) == []


def test_到期进入计划(store: LedgerStore):
    _pushed(store)
    planner = RetryPlanner(store)

    due = planner.due_events(PUSHED_AT + timedelta(minutes=11))
    assert len(due) == 1
    assert due[0]["event_id"] == "evt-retry-0001"

    plan = planner.plan(PUSHED_AT + timedelta(minutes=11))
    assert plan[0].action == "repushed"
    # 第 1 次重推 = 通知尝试序号 2（序号 1 是首次推送）
    assert plan[0].retries == 1
    assert plan[0].attempt == 2


def test_不在等回执状态的事件不参与复查(store: LedgerStore):
    """只有 notified / pending_ack 两种状态等回执。"""
    assert AWAITING_ACK_STATUSES == {"notified", "pending_ack"}
    store.record_event(_event("evt-retry-0002"))
    store.set_status("evt-retry-0002", "judged")

    planner = RetryPlanner(store)
    assert planner.due_events(PUSHED_AT + timedelta(days=1)) == []


def test_没有时限的事件不参与复查(store: LedgerStore):
    """制度里没写时限（due_minutes 为空）就没有到期时刻，不重推。"""
    store.record_event(_event("evt-retry-0003"))
    store.set_status("evt-retry-0003", "judged")
    store.set_status("evt-retry-0003", "card_built")
    store.mark_notified("evt-retry-0003", channel="wecom", at=PUSHED_AT)
    store.mark_pending_ack("evt-retry-0003", at=PUSHED_AT)

    planner = RetryPlanner(store)
    assert planner.due_events(PUSHED_AT + timedelta(days=1)) == []


# ---------------------------------------------------------------------------
# 2. 重推
# ---------------------------------------------------------------------------


def test_到期重推成功且不覆盖首次推送记录(store: LedgerStore):
    _pushed(store)
    pushed: list[dict] = []

    def push(event):
        pushed.append(dict(event))
        return True

    report = RetryRunner(store, push, clock=lambda: PUSHED_AT + timedelta(minutes=11)).run()
    assert report.summary["repushed"] == 1
    assert len(pushed) == 1

    row = store.get_event("evt-retry-0001")
    # 首次推送时刻不被重推覆盖
    assert row["notified_at"] == PUSHED_AT.isoformat()
    assert row["last_notified_at"] != PUSHED_AT.isoformat()
    assert row["retry_count"] == 1
    # 状态仍在等回执
    assert row["status"] == "pending_ack"

    records = store.notifications_of("evt-retry-0001")
    assert len(records) == 2
    assert records[0]["kind"] == NOTIFY_KIND_INITIAL
    assert records[0]["attempt"] == 1
    assert records[1]["kind"] == NOTIFY_KIND_RETRY
    assert records[1]["attempt"] == 2


def test_重推不重复计数(store: LedgerStore):
    """「今天推送了多少条」按去重事件数算 —— 重推不把数字灌大。"""
    _pushed(store)

    def push(event):
        return True

    runner = RetryRunner(store, push, clock=lambda: PUSHED_AT + timedelta(minutes=11))
    runner.run()
    runner.run()

    stats = store.notify_stats()
    assert stats["notified_events"] == 1, "去重后只算一条通知"
    assert stats["attempts"] == 3, "首次 + 两次重推 = 三次真实尝试"
    assert stats["retries"] == 2


def test_重推写审计留痕(store: LedgerStore):
    _pushed(store)

    def push(event):
        return True

    RetryRunner(store, push, clock=lambda: PUSHED_AT + timedelta(minutes=11)).run()
    actions = [row["action"] for row in store.audit_entries("evt-retry-0001")]
    assert "notify_retry" in actions


def test_推送出口抛错也记账不静默(store: LedgerStore):
    _pushed(store)

    def push(event):
        raise RuntimeError("企微网关不可达")

    report = RetryRunner(store, push, clock=lambda: PUSHED_AT + timedelta(minutes=11)).run()
    assert report.summary["push_failed"] == 1
    assert report.summary["exhausted"] == 0

    row = store.get_event("evt-retry-0001")
    assert row["status"] == "pending_ack", "未到上限前状态不变"
    assert row["retry_count"] == 1
    actions = [entry["action"] for entry in store.audit_entries("evt-retry-0001")]
    assert "notify_failed" in actions


def test_推送失败重试到上限落failed并告警(store: LedgerStore):
    _pushed(store)

    def push(event):
        return False

    clock = {"now": PUSHED_AT + timedelta(minutes=11)}
    runner = RetryRunner(store, push, max_retries=2, clock=lambda: clock["now"])

    first = runner.run()
    assert first.summary["push_failed"] == 1
    assert store.get_event("evt-retry-0001")["status"] == "pending_ack"

    clock["now"] = PUSHED_AT + timedelta(minutes=21)
    second = runner.run()
    assert second.summary["exhausted"] == 1
    assert second.alerts, "耗尽必须告警，不能静默"

    row = store.get_event("evt-retry-0001")
    assert row["status"] == "failed"
    assert row["retry_count"] == 2
    actions = [entry["action"] for entry in store.audit_entries("evt-retry-0001")]
    assert "notify_retry_exhausted" in actions


def test_耗尽后事件仍在台账里不丢(store: LedgerStore):
    _pushed(store)

    def push(event):
        return False

    runner = RetryRunner(
        store,
        push,
        max_retries=1,
        clock=lambda: PUSHED_AT + timedelta(minutes=11),
    )
    runner.run()

    assert store.count_events() == 1
    row = store.get_event("evt-retry-0001")
    assert row is not None
    assert row["status"] == "failed"
    # 人工接手的路还开着
    store.set_status("evt-retry-0001", "transferred_to_human")
    assert store.get_status("evt-retry-0001") == "transferred_to_human"


def test_已确认的事件不会被重推(store: LedgerStore):
    _pushed(store)
    store.mark_acknowledged("evt-retry-0001", actor="检验科值班技师", at=PUSHED_AT)

    pushed: list[dict] = []

    def push(event):
        pushed.append(dict(event))
        return True

    report = RetryRunner(store, push, clock=lambda: PUSHED_AT + timedelta(hours=5)).run()
    assert report.outcomes == []
    assert pushed == []


def test_计划器能算出已耗尽的事件(store: LedgerStore):
    _pushed(store)

    def push(event):
        return False

    runner = RetryRunner(store, push, max_retries=1, clock=lambda: PUSHED_AT + timedelta(minutes=11))
    runner.run()
    # 再跑一轮：事件已落 failed，不再进入计划
    report = RetryRunner(store, push, max_retries=1, clock=lambda: PUSHED_AT + timedelta(minutes=21)).run()
    assert report.outcomes == []
