"""崩溃恢复单测：进程被打死之后，按台账状态续跑。

盯住任务书里的三条硬要求：

* **不重复推送** —— 推送成功的记录在案（或渠道能回查）时，续跑只补状态，
  不再推一遍；台账里「首次推送」永远只有一条；
* **不重复回写** —— 回写成功即落 ``registered``；崩溃落在「回写已发出、
  状态未更新」时，先问登记系统有没有这条回执；
* **已确认回执不丢** —— 值班人确认过的 ``acknowledged_at`` /
  ``acknowledged_by`` 在续跑里原样保留，回写失败也不清。

另外两条底线：**续跑不动人已经接手的事**（转人工 / 推送耗尽 / 事实已
定格），以及**推不动就明说推不动**（不静默跳过）。

真崩溃用例走 ``scripts/crash_recovery_demo.py``：那个脚本会
``os._exit`` 硬退出（模拟 ``kill -9``），不是抛异常。
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from critval.card.builder import build_card
from critval.judge.critical import judge_event
from critval.kb.loader import load_threshold_table
from critval.ledger.recovery import RecoveryRunner
from critval.ledger.store import LedgerStore
from critval.models import CriticalEvent

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "脱敏样例阈值表.xlsx"
DEMO = PROJECT_ROOT / "scripts" / "crash_recovery_demo.py"


@pytest.fixture(scope="module")
def table():
    return load_threshold_table(FIXTURE)


def _event(event_id: str = "evt-crash-0001") -> CriticalEvent:
    """造一条事件（消息控制号随事件号走 —— 幂等键 = 消息号 + 项目码 + 观察时刻）。"""
    return CriticalEvent(
        event_id=event_id,
        message_control_id=f"MC-{event_id}",
        specimen_no=f"SP-{event_id}",
        item_code="718-7",
        item_name="血红蛋白",
        value=Decimal("25"),
        unit="g/L",
        observed_at=datetime(2026, 1, 2, 8, 30, tzinfo=timezone.utc),
        dept_code="检验科",
        patient_ref="pt-000000000001",
    )


def _card_built_event(store: LedgerStore, table, event_id: str = "evt-crash-0001"):
    """造一条「卡已装好、还没推出去」的事件（崩溃点的真实状态）。"""
    event = _event(event_id)
    judgement = judge_event(event, table.rows)
    assert judgement.decision == "critical"
    store.record_event(event)
    store.record_judgement(event.event_id, judgement)
    card = build_card(event, judgement, policy_clause=None)
    store.record_card(card)
    assert store.get_status(event.event_id) == "card_built"
    return event, card


def _initial_push_rows(store: LedgerStore, event_id: str) -> list[dict]:
    return [
        row for row in store.notifications_of(event_id) if row["kind"] == "initial"
    ]


# ----------------------------------------------------------------------
# 推送
# ----------------------------------------------------------------------
def test_崩溃在推送之前_续跑补推一次且首次推送只记一条(tmp_path):
    """崩溃点在推送之前：续跑补推一次，台账里首次推送恰好一条。"""
    db_path = tmp_path / "crash.db"
    pushed: list[str] = []
    with LedgerStore(db_path) as store:
        event, _card = _card_built_event(store, load_threshold_table(FIXTURE))

    with LedgerStore(db_path) as store:
        runner = RecoveryRunner(
            store,
            notifier=lambda row: bool(pushed.append(str(row["event_id"])) or True),
            channel="test",
        )
        report = runner.resume()

        assert [outcome.action for outcome in report.outcomes] == ["pushed"]
        assert report.outcomes[0].ok is True
        assert store.get_status(event.event_id) == "notified"
        assert len(_initial_push_rows(store, event.event_id)) == 1
        assert pushed == [event.event_id]


def test_崩溃在推送之后状态更新之前_续跑不重复推送(tmp_path):
    """推送成功了、状态没更新就死：续跑只补状态，不再推一遍。"""
    db_path = tmp_path / "crash_after_push.db"
    pushed: list[str] = []
    with LedgerStore(db_path) as store:
        event, _card = _card_built_event(store, load_threshold_table(FIXTURE))
        # 推送成功的记录在案（通知表 kind='initial'），但状态还停在 card_built
        store.record_notification(
            event.event_id,
            kind="initial",
            attempt=1,
            channel="test",
            detail="推送已发出（模拟崩溃发生在状态更新之前）",
        )
        assert store.get_status(event.event_id) == "card_built"

    with LedgerStore(db_path) as store:
        runner = RecoveryRunner(
            store,
            notifier=lambda row: bool(pushed.append(str(row["event_id"])) or True),
            channel="test",
        )
        report = runner.resume()

        assert [outcome.action for outcome in report.outcomes] == ["push_reconciled"]
        assert pushed == []  # 没有再推
        assert store.get_status(event.event_id) == "notified"
        assert len(_initial_push_rows(store, event.event_id)) == 1  # 仍然只有一条


def test_渠道能回查投递结果时续跑不重复推送(tmp_path):
    """渠道能回答「推过没有」时，续跑据此不重推。"""
    db_path = tmp_path / "crash_pushcheck.db"
    pushed: list[str] = []
    with LedgerStore(db_path) as store:
        event, _card = _card_built_event(store, load_threshold_table(FIXTURE))

    with LedgerStore(db_path) as store:
        runner = RecoveryRunner(
            store,
            notifier=lambda row: bool(pushed.append(str(row["event_id"])) or True),
            push_check=lambda row: True,  # 渠道回查：推过了
            channel="test",
        )
        report = runner.resume()

        assert report.outcomes[0].action == "push_reconciled"
        assert pushed == []
        assert store.get_status(event.event_id) == "notified"


def test_推送出口抛错时续跑留痕且不推进状态(tmp_path):
    """推送失败：留审计、状态不动，事件不丢。"""
    db_path = tmp_path / "crash_pushfail.db"
    with LedgerStore(db_path) as store:
        event, _card = _card_built_event(store, load_threshold_table(FIXTURE))

    with LedgerStore(db_path) as store:
        runner = RecoveryRunner(
            store,
            notifier=lambda row: (_ for _ in ()).throw(RuntimeError("企微通道不可达")),
            channel="test",
        )
        report = runner.resume()

        assert report.outcomes[0].action == "push_failed"
        assert report.outcomes[0].ok is False
        assert store.get_status(event.event_id) == "card_built"
        failures = [
            row for row in store.audit_entries() if row["action"] == "notify_failed"
        ]
        assert len(failures) == 1


# ----------------------------------------------------------------------
# 回写
# ----------------------------------------------------------------------
def _acknowledged_event(store: LedgerStore, table, event_id: str = "evt-crash-0002"):
    """造一条「值班人已确认、还没回写」的事件。"""
    event = _event(event_id)
    judgement = judge_event(event, table.rows)
    store.record_event(event)
    store.record_judgement(event.event_id, judgement)
    card = build_card(event, judgement, policy_clause=None)
    store.record_card(card)
    store.mark_notified(
        event.event_id,
        channel="test",
        due_minutes=10,
        card_id=card.card_id,
        detail="已推送",
    )
    store.mark_acknowledged(event.event_id, actor="检验科值班技师", detail="值班人已确认")
    assert store.get_status(event.event_id) == "acknowledged"
    return event


def test_续跑回写成功后已确认回执不丢(tmp_path):
    """回写成功 → 落 registered；确认人与确认时刻原样保留。"""
    db_path = tmp_path / "crash_writeback.db"
    writes: list[str] = []
    with LedgerStore(db_path) as store:
        event = _acknowledged_event(store, load_threshold_table(FIXTURE))
        before = store.get_event(event.event_id)

    with LedgerStore(db_path) as store:
        runner = RecoveryRunner(
            store,
            writeback=lambda row: bool(writes.append(str(row["event_id"])) or True),
        )
        report = runner.resume()

        assert report.outcomes[0].action == "written_back"
        assert writes == [event.event_id]
        row = store.get_event(event.event_id)
        assert store.get_status(event.event_id) == "registered"
        assert row["acknowledged_by"] == before["acknowledged_by"]
        assert row["acknowledged_at"] == before["acknowledged_at"]


def test_登记系统已有回执时续跑不重复回写(tmp_path):
    """崩溃落在「回写已发出、状态未更新」：回查得到就不重复回写。"""
    db_path = tmp_path / "crash_receipt.db"
    writes: list[str] = []
    with LedgerStore(db_path) as store:
        event = _acknowledged_event(store, load_threshold_table(FIXTURE))

    with LedgerStore(db_path) as store:
        runner = RecoveryRunner(
            store,
            writeback=lambda row: bool(writes.append(str(row["event_id"])) or True),
            receipt_check=lambda row: True,  # 登记系统里已经有回执
        )
        report = runner.resume()

        assert report.outcomes[0].action == "writeback_reconciled"
        assert writes == []  # 没有重复回写
        assert store.get_status(event.event_id) == "registered"


def test_续跑回写失败时已确认回执仍然留着(tmp_path):
    """回写失败 → 落 writeback_failed，确认痕迹不清、不丢。"""
    db_path = tmp_path / "crash_writeback_fail.db"
    with LedgerStore(db_path) as store:
        event = _acknowledged_event(store, load_threshold_table(FIXTURE))
        before = store.get_event(event.event_id)

    with LedgerStore(db_path) as store:
        runner = RecoveryRunner(
            store,
            writeback=lambda row: False,
        )
        report = runner.resume()

        assert report.outcomes[0].action == "writeback_failed"
        assert report.outcomes[0].ok is False
        assert store.get_status(event.event_id) == "writeback_failed"
        row = store.get_event(event.event_id)
        assert row["acknowledged_by"] == before["acknowledged_by"]
        assert row["acknowledged_at"] == before["acknowledged_at"]


def test_回写失败后续跑还能补写成功(tmp_path):
    """补写路径开着：下一次续跑把 writeback_failed 推回 registered。"""
    db_path = tmp_path / "crash_writeback_retry.db"
    with LedgerStore(db_path) as store:
        event = _acknowledged_event(store, load_threshold_table(FIXTURE))

    with LedgerStore(db_path) as store:
        RecoveryRunner(store, writeback=lambda row: False).resume()
        assert store.get_status(event.event_id) == "writeback_failed"

    with LedgerStore(db_path) as store:
        runner = RecoveryRunner(store, writeback=lambda row: True)
        report = runner.resume()
        assert report.outcomes[0].action == "written_back"
        assert store.get_status(event.event_id) == "registered"


# ----------------------------------------------------------------------
# 边界
# ----------------------------------------------------------------------
def test_续跑连跑两次不重复做(tmp_path):
    """续跑是幂等的：第二遍没有事可做（状态都推进到位了）。"""
    db_path = tmp_path / "crash_twice.db"
    pushed: list[str] = []
    with LedgerStore(db_path) as store:
        event, _card = _card_built_event(store, load_threshold_table(FIXTURE))

    with LedgerStore(db_path) as store:
        runner = RecoveryRunner(
            store,
            notifier=lambda row: bool(pushed.append(str(row["event_id"])) or True),
            channel="test",
        )
        first = runner.resume()
        second = runner.resume()

    assert len(first.outcomes) == 1
    assert second.outcomes == []
    assert pushed == [event.event_id]


def test_续跑不动人已接手与已定格的事件(tmp_path):
    """转人工 / 推送耗尽 / 已回写 / 不危急 —— 一律不自动推进。"""
    db_path = tmp_path / "crash_human.db"
    pushed: list[str] = []
    writes: list[str] = []
    with LedgerStore(db_path) as store:
        event = _event("evt-crash-0003")
        store.record_event(event)
        store.set_status(event.event_id, "transferred_to_human", detail="转人工")

        other = _event("evt-crash-0004")
        store.record_event(other)
        store.set_status(other.event_id, "not_critical", detail="不危急")

        done = _event("evt-crash-0005")
        store.record_event(done)
        store.set_status(done.event_id, "judged", detail="已判定")
        store.set_status(done.event_id, "card_built", detail="已出卡")
        store.set_status(done.event_id, "notified", detail="已推送")
        store.set_status(done.event_id, "acknowledged", detail="已确认")
        store.set_status(done.event_id, "registered", detail="已回写")

    with LedgerStore(db_path) as store:
        runner = RecoveryRunner(
            store,
            notifier=lambda row: bool(pushed.append(str(row["event_id"])) or True),
            writeback=lambda row: bool(writes.append(str(row["event_id"])) or True),
        )
        report = runner.resume()

        assert report.outcomes == []
        assert pushed == []
        assert writes == []


def test_没接通道时续跑明说推不动不静默跳过(tmp_path):
    """缺通道 → 结论明写「推不动」，并留审计痕迹。"""
    db_path = tmp_path / "crash_nochannel.db"
    with LedgerStore(db_path) as store:
        event, _card = _card_built_event(store, load_threshold_table(FIXTURE))

    with LedgerStore(db_path) as store:
        report = RecoveryRunner(store).resume()

        assert report.outcomes[0].action == "push_deferred"
        assert report.outcomes[0].ok is False
        assert "不静默跳过" in report.outcomes[0].detail
        deferred = [
            row for row in store.audit_entries() if row["action"] == "recovery_deferred"
        ]
        assert len(deferred) == 1
        assert store.get_status(event.event_id) == "card_built"


def test_到期未回执的按重推口径处理(tmp_path):
    """崩溃期间时限到了：续跑走重推口径（另记一行重推，首次推送不被覆盖）。"""
    db_path = tmp_path / "crash_retry.db"
    pushed: list[str] = []
    with LedgerStore(db_path) as store:
        event = _event("evt-crash-0006")
        store.record_event(event)
        store.set_status(event.event_id, "judged", detail="已判定")
        store.set_status(event.event_id, "card_built", detail="已出卡")
        store.mark_notified(
            event.event_id,
            channel="test",
            due_minutes=10,
            detail="首次推送",
            at=datetime(2026, 1, 2, 8, 30, tzinfo=timezone.utc),
        )
        store.mark_pending_ack(
            event.event_id, detail="等回执", at=datetime(2026, 1, 2, 8, 30, tzinfo=timezone.utc)
        )
        first_notified_at = store.get_event(event.event_id)["notified_at"]

    with LedgerStore(db_path) as store:
        runner = RecoveryRunner(
            store,
            notifier=lambda row: bool(pushed.append(str(row["event_id"])) or True),
            channel="test",
            clock=lambda: datetime(2026, 1, 2, 9, 0, tzinfo=timezone.utc),
        )
        report = runner.resume()

        assert report.outcomes[0].action == "retry_repushed"
        assert pushed == [event.event_id]
        rows = store.notifications_of(event.event_id)
        assert [row["kind"] for row in rows] == ["initial", "retry"]
        # 首次推送时刻不被重推覆盖。
        assert store.get_event(event.event_id)["notified_at"] == first_notified_at


# ----------------------------------------------------------------------
# 真崩溃（另一个进程真的死了）
# ----------------------------------------------------------------------
def run_demo(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(DEMO), *args],
        capture_output=True,
        text=True,
        cwd=str(PROJECT_ROOT),
    )


def test_真进程被打死后重启能续跑且不重复推送(tmp_path):
    """``os._exit`` 硬退出（模拟 kill -9）后重启：续跑补推一次，投递箱一行。"""
    db_path = tmp_path / "real_crash.db"
    outbox = tmp_path / "outbox.jsonl"

    crashed = run_demo(
        [
            "--mode",
            "crash",
            "--db",
            str(db_path),
            "--outbox",
            str(outbox),
            "--crash-at",
            "before_push",
        ]
    )
    assert crashed.returncode == 9, crashed.stderr

    resumed = run_demo(["--mode", "resume", "--db", str(db_path), "--outbox", str(outbox)])
    assert resumed.returncode == 0, resumed.stderr
    rows = [json.loads(line) for line in outbox.read_text(encoding="utf-8").splitlines() if line]
    assert len(rows) == 1

    with LedgerStore(db_path) as store:
        ok, text = store.integrity_check()
        assert ok, text
        assert store.get_status(rows[0]["event_id"]) == "notified"


def test_真进程在推送后崩溃重启不重复推送(tmp_path):
    """崩在「推送已发出、状态未更新」：续跑不重推，投递箱仍一行。"""
    db_path = tmp_path / "real_crash_after_push.db"
    outbox = tmp_path / "outbox.jsonl"

    crashed = run_demo(
        [
            "--mode",
            "crash",
            "--db",
            str(db_path),
            "--outbox",
            str(outbox),
            "--crash-at",
            "after_push",
        ]
    )
    assert crashed.returncode == 9, crashed.stderr
    assert len(outbox.read_text(encoding="utf-8").strip().splitlines()) == 1

    resumed = run_demo(["--mode", "resume", "--db", str(db_path), "--outbox", str(outbox)])
    assert resumed.returncode == 0, resumed.stderr
    assert "push_reconciled" in resumed.stdout
    rows = [json.loads(line) for line in outbox.read_text(encoding="utf-8").splitlines() if line]
    assert len(rows) == 1  # 没有第二次投递
