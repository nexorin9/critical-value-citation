"""通知投递可靠性单测（任务 id 42）：退避重试、死信与重复投递抑制。

盯住六件事：

* **退避序列** —— 指数退避 + 抖动 + 封顶；契约错不重试；
* **逐次留痕** —— 每一次出站尝试一行（状态码 / 耗时 / 重试序号 / 错误摘要）；
* **死信不丢事件** —— 重试耗尽 → 事件落 ``failed`` + 死信表留档（原卡内容与
  最后一次错误）+ 审计告警；事件**仍在台账里**；
* **重复投递抑制** —— 同一事件同一轮次只推一次；
* **到期重推不被误抑制** —— 首次推送（``initial:1``）与到期重推
  （``retry:1``）轮次键不同，**互不抑制**；
* **本机降级出口** —— 企微未配置或重试耗尽时落本机队列，值班台可见。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from critval.ledger.retry import RetryRunner
from critval.ledger.store import LedgerStore
from critval.models import CriticalEvent
from critval.notify.local import LocalNotifier
from critval.notify.reliability import (
    ROUND_KIND_INITIAL,
    ROUND_KIND_RETRY,
    BackoffPolicy,
    ReliableNotifier,
    build_round_key,
    suppression_key,
)

PUSHED_AT = datetime(2026, 1, 2, 8, 30, tzinfo=timezone.utc)
CARD_TEXT = "危急值处置卡 · 血红蛋白 25 g/L（低于 30）· 检验科值班技师 · 10 分钟内"


# ---------------------------------------------------------------------------
# 替身
# ---------------------------------------------------------------------------


@dataclass
class FakeResult:
    """一次推送尝试的结论（字段与 ``WeComResult`` 对齐）。"""

    ok: bool
    status: int = 0
    error: str = ""
    retryable: bool = False
    errcode: int = 0
    errmsg: str = ""


class FakePush:
    """按脚本返回结果的推送出口，并记录被调了几次。"""

    def __init__(self, results: list[FakeResult]) -> None:
        self.results = list(results)
        self.calls = 0
        self.rows: list[dict] = []

    def __call__(self, row: dict) -> FakeResult:
        self.calls += 1
        self.rows.append(dict(row))
        index = min(self.calls - 1, len(self.results) - 1)
        return self.results[index]


class FixedRandom:
    """固定随机源（抖动可复现）。"""

    def __init__(self, value: float) -> None:
        self.value = float(value)

    def random(self) -> float:
        return self.value


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


@pytest.fixture()
def store() -> LedgerStore:
    instance = LedgerStore(":memory:")
    try:
        yield instance
    finally:
        instance.close()


def _event(event_id: str = "evt-notify-0001") -> CriticalEvent:
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


def _card_built(store: LedgerStore, event_id: str = "evt-notify-0001") -> None:
    """把事件推到「已出卡」状态（首次推送的合法起点）。"""
    store.record_event(_event(event_id))
    store.set_status(event_id, "judged")
    store.set_status(event_id, "card_built")


def _notifier(
    store: LedgerStore,
    *,
    local: LocalNotifier | None = None,
    policy: BackoffPolicy | None = None,
    waits: list[float] | None = None,
    rng=None,
) -> ReliableNotifier:
    return ReliableNotifier(
        store,
        channel="wecom",
        policy=policy or BackoffPolicy(jitter_ratio=0.0),
        sleep=(waits.append if waits is not None else (lambda _s: None)),
        rng=rng,
        clock=lambda: PUSHED_AT,
        local=local,
    )


# ---------------------------------------------------------------------------
# 1. 退避策略
# ---------------------------------------------------------------------------


def test_退避序列按指数增长():
    policy = BackoffPolicy(base_seconds=0.5, factor=2.0, max_seconds=8.0, jitter_ratio=0.0)
    assert policy.delay_for(1) == pytest.approx(0.5)
    assert policy.delay_for(2) == pytest.approx(1.0)
    assert policy.delay_for(3) == pytest.approx(2.0)


def test_退避封顶不超上限():
    policy = BackoffPolicy(base_seconds=1.0, factor=10.0, max_seconds=8.0, jitter_ratio=0.0)
    assert policy.delay_for(2) == pytest.approx(8.0)
    assert policy.delay_for(9) == pytest.approx(8.0)


def test_抖动让等待浮动但不突破封顶():
    policy = BackoffPolicy(base_seconds=1.0, factor=1.0, max_seconds=8.0, jitter_ratio=0.2)
    low = policy.delay_for(1, FixedRandom(0.0))
    high = policy.delay_for(1, FixedRandom(1.0))
    assert low == pytest.approx(0.8)
    assert high == pytest.approx(1.2)
    assert low < high


def test_抖动下的等待仍不超过封顶():
    policy = BackoffPolicy(base_seconds=10.0, factor=1.0, max_seconds=8.0, jitter_ratio=0.5)
    for unit in (0.0, 0.5, 1.0):
        assert policy.delay_for(1, FixedRandom(unit)) <= 8.0


def test_退避策略可从配置面装配(monkeypatch):
    from critval.config import Config

    Config.init_config(
        CRITVAL_NOTIFY_MAX_ATTEMPTS="5",
        CRITVAL_NOTIFY_BACKOFF_BASE_SECONDS="0.25",
        CRITVAL_NOTIFY_BACKOFF_FACTOR="3",
        CRITVAL_NOTIFY_MAX_BACKOFF_SECONDS="6",
        CRITVAL_NOTIFY_JITTER_RATIO="0",
    )
    policy = BackoffPolicy.from_config()
    assert policy.max_attempts == 5
    assert policy.base_seconds == pytest.approx(0.25)
    assert policy.factor == pytest.approx(3.0)
    assert policy.max_seconds == pytest.approx(6.0)
    assert policy.jitter_ratio == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# 2. 重试序列与逐次留痕
# ---------------------------------------------------------------------------


def test_连续失败时按最大次数重试并逐次留痕(store: LedgerStore):
    _card_built(store)
    waits: list[float] = []
    push = FakePush([FakeResult(ok=False, status=503, error="企微 webhook 返回 503", retryable=True)])
    notifier = _notifier(store, waits=waits)

    outcome = notifier.deliver("evt-notify-0001", push=push, content=CARD_TEXT)

    assert outcome.ok is False
    assert outcome.attempt_count == 3
    assert push.calls == 3
    # 指数退避：0.5 → 1.0
    assert waits == [pytest.approx(0.5), pytest.approx(1.0)]
    # 逐次留痕：每次尝试一行，带状态码 / 耗时 / 重试序号 / 错误摘要
    rows = store.delivery_attempts_of("evt-notify-0001")
    assert len(rows) == 3
    assert [row["attempt"] for row in rows] == [1, 2, 3]
    assert all(row["status_code"] == 503 for row in rows)
    assert all(row["ok"] == 0 for row in rows)
    assert "503" in rows[0]["error"]
    assert rows[1]["waited_ms"] == 500
    assert rows[2]["waited_ms"] == 1000


def test_契约错不重试只试一次(store: LedgerStore):
    _card_built(store)
    push = FakePush([FakeResult(ok=False, status=200, error="企微推送失败：93000 webhook 地址不合法", retryable=False)])
    notifier = _notifier(store)

    outcome = notifier.deliver("evt-notify-0001", push=push, content=CARD_TEXT)

    assert outcome.attempt_count == 1
    assert push.calls == 1


def test_中途成功则停止重试(store: LedgerStore):
    _card_built(store)
    push = FakePush(
        [
            FakeResult(ok=False, status=503, error="企微 webhook 返回 503", retryable=True),
            FakeResult(ok=True, status=200),
        ]
    )
    waits: list[float] = []
    notifier = _notifier(store, waits=waits)

    outcome = notifier.deliver("evt-notify-0001", push=push, content=CARD_TEXT)

    assert outcome.ok is True
    assert outcome.attempt_count == 2
    assert push.calls == 2
    assert waits == [pytest.approx(0.5)]
    assert len(store.delivery_attempts_of("evt-notify-0001")) == 2


def test_推送出口抛错也留痕不吞(store: LedgerStore):
    _card_built(store)

    def boom(_row: dict) -> FakeResult:
        raise RuntimeError("连接被重置")

    notifier = _notifier(store)
    outcome = notifier.deliver("evt-notify-0001", push=boom, content=CARD_TEXT)

    assert outcome.ok is False
    # 抛错按「可重试」处理：照常退避重试到上限，每一次都留痕
    assert outcome.attempt_count == 3
    rows = store.delivery_attempts_of("evt-notify-0001")
    assert "连接被重置" in rows[0]["error"]


# ---------------------------------------------------------------------------
# 3. 死信：事件不丢
# ---------------------------------------------------------------------------


def test_重试耗尽落死信且事件不丢(store: LedgerStore):
    _card_built(store)
    push = FakePush([FakeResult(ok=False, status=503, error="企微 webhook 返回 503", retryable=True)])
    notifier = _notifier(store)

    outcome = notifier.notify_event(
        "evt-notify-0001", push=push, content=CARD_TEXT, notify_target="检验科值班技师", due_minutes=10
    )

    assert outcome.ok is False
    assert outcome.dead_lettered is True
    # 事件仍在台账里，状态落 failed
    row = store.get_event("evt-notify-0001")
    assert row is not None
    assert row["status"] == "failed"
    # 死信表留档：原卡内容 + 最后一次错误
    letters = store.dead_letters_of("evt-notify-0001")
    assert len(letters) == 1
    assert letters[0]["attempts"] == 3
    assert "503" in letters[0]["last_error"]
    assert CARD_TEXT in letters[0]["card_content"]
    # 审计告警
    actions = [entry["action"] for entry in store.audit_entries("evt-notify-0001")]
    assert "notify_dead_lettered" in actions


def test_死信落库后可查全量清单(store: LedgerStore):
    _card_built(store)
    push = FakePush([FakeResult(ok=False, status=0, error="企微 webhook 不可达", retryable=True)])
    _notifier(store).deliver("evt-notify-0001", push=push, content=CARD_TEXT)

    letters = store.dead_letters()
    assert len(letters) == 1
    assert letters[0]["event_id"] == "evt-notify-0001"


def test_已确认的事件不被自动改回失败态(store: LedgerStore):
    _card_built(store)
    store.mark_notified("evt-notify-0001", notify_target="检验科值班技师", due_minutes=10)
    store.mark_acknowledged("evt-notify-0001", actor="检验科值班技师")
    push = FakePush([FakeResult(ok=False, status=503, error="企微 webhook 返回 503", retryable=True)])

    outcome = _notifier(store).notify_event("evt-notify-0001", push=push, content=CARD_TEXT)

    assert outcome.dead_lettered is True
    # 既成事实不被自动路径改写：状态仍是 acknowledged
    assert store.get_status("evt-notify-0001") == "acknowledged"
    # 死信与告警仍然留痕
    assert len(store.dead_letters_of("evt-notify-0001")) == 1


# ---------------------------------------------------------------------------
# 4. 重复投递抑制
# ---------------------------------------------------------------------------


def test_同一轮次重复投递被抑制(store: LedgerStore):
    _card_built(store)
    push = FakePush([FakeResult(ok=True, status=200)])
    notifier = _notifier(store)

    first = notifier.deliver("evt-notify-0001", push=push, content=CARD_TEXT)
    second = notifier.deliver("evt-notify-0001", push=push, content=CARD_TEXT)

    assert first.ok is True and first.suppressed is False
    assert second.suppressed is True
    assert push.calls == 1  # 第二次根本没发出去
    # 抑制不产生新的投递记录
    assert len(store.delivery_attempts_of("evt-notify-0001")) == 1


def test_抑制键含事件状态与尝试轮次():
    assert suppression_key("evt-1", ROUND_KIND_INITIAL, "initial:1") == "evt-1|initial|initial:1"
    assert build_round_key(ROUND_KIND_RETRY, 2) == "retry:2"


def test_首次推送与到期重推互不抑制(store: LedgerStore):
    _card_built(store)
    push = FakePush([FakeResult(ok=True, status=200)])
    notifier = _notifier(store)

    initial = notifier.deliver(
        "evt-notify-0001",
        push=push,
        content=CARD_TEXT,
        kind=ROUND_KIND_INITIAL,
        round_key=build_round_key(ROUND_KIND_INITIAL, 1),
    )
    repush = notifier.deliver(
        "evt-notify-0001",
        push=push,
        content=CARD_TEXT,
        kind=ROUND_KIND_RETRY,
        round_key=build_round_key(ROUND_KIND_RETRY, 1),
    )
    again = notifier.deliver(
        "evt-notify-0001",
        push=push,
        content=CARD_TEXT,
        kind=ROUND_KIND_RETRY,
        round_key=build_round_key(ROUND_KIND_RETRY, 1),
    )

    assert initial.suppressed is False
    assert repush.suppressed is False   # 到期重推不被首次推送误抑制
    assert again.suppressed is True     # 同一重推轮次再推 → 抑制
    assert push.calls == 2


def test_投递失败后同轮次可以重新驱动(store: LedgerStore):
    _card_built(store)
    failing = FakePush([FakeResult(ok=False, status=503, error="企微 webhook 返回 503", retryable=True)])
    notifier = _notifier(store)

    first = notifier.deliver("evt-notify-0001", push=failing, content=CARD_TEXT)
    assert first.dead_lettered is True

    working = FakePush([FakeResult(ok=True, status=200)])
    second = notifier.deliver("evt-notify-0001", push=working, content=CARD_TEXT)

    # 上一轮推失败（已落死信）→ 允许人重新驱动同一条
    assert second.suppressed is False
    assert second.ok is True
    assert working.calls == 1


def test_在途轮次不会被并发抢占(store: LedgerStore):
    _card_built(store)
    key = suppression_key("evt-notify-0001", ROUND_KIND_INITIAL, "initial:1")
    assert store.claim_delivery_round(key, event_id="evt-notify-0001") is True
    # 第二次抢占被拒（在途）
    assert store.claim_delivery_round(key, event_id="evt-notify-0001") is False
    # 标成已送达之后同样被拒
    store.mark_delivery_round(key, "delivered")
    assert store.claim_delivery_round(key, event_id="evt-notify-0001") is False
    assert store.delivery_round_state(key) == "delivered"


def test_陈旧的在途轮次可以被抢占(store: LedgerStore):
    key = suppression_key("evt-notify-0001", ROUND_KIND_INITIAL, "initial:1")
    assert store.claim_delivery_round(key, event_id="evt-notify-0001") is True
    # 把在途时间戳推到很久以前（模拟进程崩在推送中途）
    with store._transaction():  # noqa: SLF001 —— 测试里直接改运行期状态
        store._conn.execute(  # noqa: SLF001
            "UPDATE notify_delivery_rounds SET updated_at = ? WHERE round_key = ?",
            ("2020-01-01T00:00:00+00:00", key),
        )
    assert store.claim_delivery_round(key, event_id="evt-notify-0001", stale_after_seconds=60) is True


# ---------------------------------------------------------------------------
# 5. 到期重推（任务 id 10 的复查路径）与本机降级出口
# ---------------------------------------------------------------------------


def test_到期重推走可靠性层且不被首次推送抑制(store: LedgerStore):
    _card_built(store)
    push = FakePush([FakeResult(ok=True, status=200)])
    notifier = _notifier(store)

    # 首次推送
    notifier.notify_event(
        "evt-notify-0001",
        push=push,
        content=CARD_TEXT,
        kind=ROUND_KIND_INITIAL,
        round_key=build_round_key(ROUND_KIND_INITIAL, 1),
        notify_target="检验科值班技师",
        due_minutes=10,
    )
    assert push.calls == 1
    store.mark_pending_ack("evt-notify-0001", at=PUSHED_AT)
    # 到期时刻按台账实际落下的 due_at 算（首次推送那一刻 + 上报时限）
    due_at = datetime.fromisoformat(store.get_event("evt-notify-0001")["due_at"])

    # 到期复查重推（任务 id 10 的 RetryRunner 走可靠性层的推送出口）
    runner = RetryRunner(
        store,
        lambda row: notifier.deliver(
            str(row["event_id"]),
            push=push,
            content=CARD_TEXT,
            kind=ROUND_KIND_RETRY,
            round_key=build_round_key(ROUND_KIND_RETRY, 1),
        ).ok,
        clock=lambda: due_at + timedelta(minutes=1),
    )
    report = runner.run()

    assert [outcome.action for outcome in report.outcomes] == ["repushed"]
    assert push.calls == 2  # 重推真的发出去了，没有被首次推送误抑制


def test_重试耗尽时落本机通知队列(store: LedgerStore, tmp_path: Path):
    _card_built(store)
    local = LocalNotifier(tmp_path / "local_notifications.jsonl")
    push = FakePush([FakeResult(ok=False, status=503, error="企微 webhook 返回 503", retryable=True)])

    outcome = _notifier(store, local=local).notify_event(
        "evt-notify-0001",
        push=push,
        content=CARD_TEXT,
        notify_target="检验科值班技师",
        due_minutes=10,
    )

    assert outcome.fallback_used is True
    records = local.pending()
    assert len(records) == 1
    assert records[0].event_id == "evt-notify-0001"
    assert records[0].due_minutes == 10
    assert CARD_TEXT in records[0].content
    assert "503" in records[0].reason


def test_本机队列可标已处理(store: LedgerStore, tmp_path: Path):
    _card_built(store)
    local = LocalNotifier(tmp_path / "local_notifications.jsonl")
    push = FakePush([FakeResult(ok=False, status=0, error="企微 webhook 不可达", retryable=True)])
    _notifier(store, local=local).deliver("evt-notify-0001", push=push, content=CARD_TEXT)

    assert len(local.pending()) == 1
    changed = local.mark_handled("evt-notify-0001", handler="检验科组长")
    assert changed == 1
    assert local.pending() == ()
    handled = local.records_of("evt-notify-0001")[0]
    assert handled.handled is True
    assert handled.handled_by == "检验科组长"
    assert handled.handled_at


def test_推送成功不落本机队列(store: LedgerStore, tmp_path: Path):
    _card_built(store)
    local = LocalNotifier(tmp_path / "local_notifications.jsonl")
    push = FakePush([FakeResult(ok=True, status=200)])

    outcome = _notifier(store, local=local).notify_event(
        "evt-notify-0001",
        push=push,
        content=CARD_TEXT,
        notify_target="检验科值班技师",
        due_minutes=10,
    )

    assert outcome.ok is True
    assert outcome.fallback_used is False
    assert local.records() == ()
    # 首次推送成功后事件落 notified，并记下上报时限与到期时刻
    row = store.get_event("evt-notify-0001")
    assert row["status"] == "notified"
    assert row["notify_channel"] == "wecom"
    assert row["due_minutes"] == 10
    assert row["due_at"]


def test_重推成功追加通知记录且不覆盖首次记录(store: LedgerStore):
    _card_built(store)
    push = FakePush([FakeResult(ok=True, status=200)])
    notifier = _notifier(store)

    notifier.notify_event(
        "evt-notify-0001",
        push=push,
        content=CARD_TEXT,
        kind=ROUND_KIND_INITIAL,
        round_key=build_round_key(ROUND_KIND_INITIAL, 1),
    )
    first_push_at = store.get_event("evt-notify-0001")["notified_at"]
    notifier.notify_event(
        "evt-notify-0001",
        push=push,
        content=CARD_TEXT,
        kind=ROUND_KIND_RETRY,
        round_key=build_round_key(ROUND_KIND_RETRY, 1),
        attempt=1,
    )

    records = store.notifications_of("evt-notify-0001")
    assert [row["kind"] for row in records] == ["initial", "retry"]
    # 首次推送时刻不被重推覆盖（最近一次推送另记 last_notified_at）
    row = store.get_event("evt-notify-0001")
    assert row["notified_at"] == first_push_at
    assert row["retry_count"] == 1


# ---------------------------------------------------------------------------
# 6. 值班台可见（本机队列查询端点）
# ---------------------------------------------------------------------------


def _queue_request(path: str, token: str = "") -> object:
    from critval.console.inbound import InboundRequest

    headers = {"authorization": f"Bearer {token}"} if token else {}
    return InboundRequest(method="GET", path=path, headers=headers)


def test_本机队列端点需值班台凭据(store: LedgerStore, tmp_path: Path):
    from critval.console.inbound import LOCAL_NOTIFICATIONS_PATH, InboundService

    local = LocalNotifier(tmp_path / "local_notifications.jsonl")
    local.send(CARD_TEXT, event_id="evt-notify-0001", reason="企微未配置")
    service = InboundService(
        token="lis-token",
        store=store,
        console_token="console-token",
        local_queue=local,
    )

    # 无凭据 → 401
    assert service.handle(_queue_request(LOCAL_NOTIFICATIONS_PATH)).status == 401
    # 凭据不对 → 401
    wrong = service.handle(_queue_request(LOCAL_NOTIFICATIONS_PATH, "wrong-token"))
    assert wrong.status == 401
    # 凭据对 → 200，队列可见
    ok = service.handle(_queue_request(LOCAL_NOTIFICATIONS_PATH, "console-token"))
    assert ok.status == 200
    assert ok.payload["count"] == 1
    assert ok.payload["pending_count"] == 1
    assert ok.payload["items"][0]["event_id"] == "evt-notify-0001"


def test_未配值班台凭据时队列端点一律拒绝(store: LedgerStore, tmp_path: Path):
    from critval.console.inbound import LOCAL_NOTIFICATIONS_PATH, InboundService

    local = LocalNotifier(tmp_path / "local_notifications.jsonl")
    local.send(CARD_TEXT, event_id="evt-notify-0001")
    service = InboundService(token="lis-token", store=store, local_queue=local)

    response = service.handle(_queue_request(LOCAL_NOTIFICATIONS_PATH, "any-token"))
    assert response.status == 401
    assert "CONSOLE_TOKEN" in response.payload["detail"]


def test_队列端点只接受GET(store: LedgerStore, tmp_path: Path):
    from critval.console.inbound import LOCAL_NOTIFICATIONS_PATH, InboundRequest, InboundService

    service = InboundService(
        token="lis-token",
        store=store,
        console_token="console-token",
        local_queue=LocalNotifier(tmp_path / "local_notifications.jsonl"),
    )
    response = service.handle(
        InboundRequest(
            method="POST",
            path=LOCAL_NOTIFICATIONS_PATH,
            headers={"authorization": "Bearer console-token"},
        )
    )
    assert response.status == 405
