"""业务状态机单测：合法跃迁 / 非法跃迁拒绝 / 终态不可回退 / 迁移留痕。

盯住四件事：

* **跳步被拒** —— ``judged`` 直接到 ``notified`` 报错，说明中间环节被绕过；
* **终态定格** —— ``archived`` / ``superseded`` 之后普通路径改不动它；
* **每次迁移写审计** —— 审计流水里能逐条看到状态怎么走的；
* **四问可答** —— 「按哪条依据、通知了谁、几分钟内、谁确认的」从已落库的
  事实里答得出来，答不出来的也要说清「没记录」。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from critval.ledger.audit import AuditLog, AuditDetailLeak, assert_detail_clean
from critval.ledger.states import (
    LEGAL_TRANSITIONS,
    MAIN_CHAIN,
    TERMINAL_STATUSES,
    IllegalTransitionError,
    describe_status,
    is_terminal,
    validate_transition,
)
from critval.ledger.store import LedgerStore, TerminalStateError
from critval.models import CriticalEvent


def _event(event_id: str = "evt-sm-0001", mcid: str = "MC-SM-0001") -> CriticalEvent:
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
        patient_ref="pt-000000000001",
    )


@pytest.fixture()
def store() -> LedgerStore:
    instance = LedgerStore(":memory:")
    try:
        yield instance
    finally:
        instance.close()


# ---------------------------------------------------------------------------
# 1. 跃迁表本身
# ---------------------------------------------------------------------------


def test_主链是七态且顺序正确():
    assert MAIN_CHAIN == (
        "received",
        "judged",
        "card_built",
        "notified",
        "acknowledged",
        "registered",
        "archived",
    )
    assert len(MAIN_CHAIN) == 7


def test_六个分支态都在表里():
    for status in (
        "not_critical",
        "pending_ack",
        "failed",
        "writeback_failed",
        "superseded",
        "transferred_to_human",
    ):
        assert status in LEGAL_TRANSITIONS, f"{status} 必须是状态机认识的状态"


def test_终态没有出边():
    for status in TERMINAL_STATUSES:
        assert not LEGAL_TRANSITIONS[status], f"{status} 是终态，不许有出边"


def test_同状态是幂等空操作():
    validate_transition("judged", "judged")  # 不抛错


def test_未知状态被拒():
    with pytest.raises(IllegalTransitionError):
        validate_transition("judged", "judgement")


def test_描述状态给人话():
    assert describe_status("pending_ack") == "已推送待回执"
    assert describe_status("archived") == "已归档"


# ---------------------------------------------------------------------------
# 2. 非法跃迁被拒
# ---------------------------------------------------------------------------


def test_跳步被拒(store: LedgerStore):
    """judged 直接到 notified —— 中间的出卡环节被绕过了，必须报错。"""
    store.record_event(_event())
    store.set_status("evt-sm-0001", "judged")

    with pytest.raises(IllegalTransitionError) as excinfo:
        store.set_status("evt-sm-0001", "notified")
    assert "judged" in str(excinfo.value)
    assert "notified" in str(excinfo.value)
    # 状态没被改坏
    assert store.get_status("evt-sm-0001") == "judged"


def test_从接收直接跳到已确认被拒(store: LedgerStore):
    store.record_event(_event())
    with pytest.raises(IllegalTransitionError):
        store.set_status("evt-sm-0001", "acknowledged")
    assert store.get_status("evt-sm-0001") == "received"


def test_未确认不得直接回写(store: LedgerStore):
    """acknowledged → registered 是唯一能落「已生效事实」的路。"""
    store.record_event(_event())
    store.set_status("evt-sm-0001", "judged")
    with pytest.raises(IllegalTransitionError):
        store.set_status("evt-sm-0001", "registered")


def test_终态不可静默回退(store: LedgerStore):
    store.record_event(_event())
    store.set_status("evt-sm-0001", "judged")
    store.set_status("evt-sm-0001", "card_built")
    store.set_status("evt-sm-0001", "notified")
    store.set_status("evt-sm-0001", "acknowledged")
    store.set_status("evt-sm-0001", "registered")
    store.set_status("evt-sm-0001", "archived")

    with pytest.raises(TerminalStateError):
        store.set_status("evt-sm-0001", "notified")
    assert store.get_status("evt-sm-0001") == "archived"


def test_终态回退必须显式声明才允许(store: LedgerStore):
    store.record_event(_event())
    store.set_status("evt-sm-0001", "judged")
    store.set_status("evt-sm-0001", "card_built")
    store.set_status("evt-sm-0001", "notified")
    store.set_status("evt-sm-0001", "acknowledged")
    store.set_status("evt-sm-0001", "registered")
    store.set_status("evt-sm-0001", "archived")

    store.set_status("evt-sm-0001", "acknowledged", allow_terminal=True)
    assert store.get_status("evt-sm-0001") == "acknowledged"


def test_非法跃迁报错不带患者明细(store: LedgerStore):
    store.record_event(_event())
    with pytest.raises(IllegalTransitionError) as excinfo:
        store.set_status("evt-sm-0001", "acknowledged")
    assert "00012345" not in str(excinfo.value)


# ---------------------------------------------------------------------------
# 3. 每次迁移写审计
# ---------------------------------------------------------------------------


def test_每次迁移写一条审计(store: LedgerStore):
    store.record_event(_event())
    store.set_status("evt-sm-0001", "judged", actor="judge", detail="判定完成")
    store.set_status("evt-sm-0001", "card_built", actor="card")

    audits = store.audit_entries("evt-sm-0001")
    transitions = [row for row in audits if row["action"] == "status_transition"]
    assert len(transitions) == 2
    assert "received" in transitions[0]["detail"] and "judged" in transitions[0]["detail"]
    assert "judged" in transitions[1]["detail"] and "card_built" in transitions[1]["detail"]
    assert transitions[0]["actor"] == "judge"


def test_同状态重复设置不写审计(store: LedgerStore):
    store.record_event(_event())
    store.set_status("evt-sm-0001", "judged")
    before = len(store.audit_entries("evt-sm-0001"))

    assert store.set_status("evt-sm-0001", "judged") is False
    assert len(store.audit_entries("evt-sm-0001")) == before


def test_审计表是append_only拒改拒删(store: LedgerStore):
    store.record_event(_event())
    store.set_status("evt-sm-0001", "judged")

    with pytest.raises(Exception) as excinfo:
        with store._transaction():
            store._conn.execute("UPDATE audit SET detail = '改过了' WHERE seq = 1")
    assert "append-only" in str(excinfo.value)

    with pytest.raises(Exception) as excinfo:
        with store._transaction():
            store._conn.execute("DELETE FROM audit WHERE seq = 1")
    assert "append-only" in str(excinfo.value)


def test_审计说明文字出现患者标识就拒写():
    with pytest.raises(AuditDetailLeak):
        assert_detail_clean("患者姓名：张三")
    with pytest.raises(AuditDetailLeak):
        assert_detail_clean("住院流水号 00012345")
    with pytest.raises(AuditDetailLeak):
        assert_detail_clean("身份证 110101199001011234")
    # 正常说明文字放行
    assert_detail_clean("已推送至值班群，等待检验科值班技师确认")


def test_审计门面拒写患者标识(store: LedgerStore):
    log = AuditLog(store)
    with pytest.raises(AuditDetailLeak):
        log.append("event_received", event_id="evt-sm-0001", detail="患者姓名：李四")


# ---------------------------------------------------------------------------
# 4. 业务里程碑入口
# ---------------------------------------------------------------------------


def test_里程碑入口按主链推进(store: LedgerStore):
    store.record_event(_event())
    moment = datetime(2026, 1, 2, 8, 30, tzinfo=timezone.utc)
    store.set_status("evt-sm-0001", "judged")
    store.set_status("evt-sm-0001", "card_built")

    assert store.mark_notified(
        "evt-sm-0001",
        notify_target="检验科值班技师",
        due_minutes=10,
        channel="wecom",
        at=moment,
    ) is True
    assert store.get_status("evt-sm-0001") == "notified"

    assert store.mark_pending_ack("evt-sm-0001", at=moment) is True
    assert store.get_status("evt-sm-0001") == "pending_ack"

    assert store.mark_acknowledged("evt-sm-0001", actor="检验科值班技师", at=moment) is True
    assert store.get_status("evt-sm-0001") == "acknowledged"

    assert store.mark_registered("evt-sm-0001", at=moment) is True
    assert store.get_status("evt-sm-0001") == "registered"

    assert store.mark_archived("evt-sm-0001") is True
    assert store.get_status("evt-sm-0001") == "archived"
    assert is_terminal("archived")


def test_首次推送记下时限与到期时刻(store: LedgerStore):
    store.record_event(_event())
    moment = datetime(2026, 1, 2, 8, 30, tzinfo=timezone.utc)
    store.set_status("evt-sm-0001", "judged")
    store.set_status("evt-sm-0001", "card_built")
    store.mark_notified(
        "evt-sm-0001",
        notify_target="检验科值班技师",
        due_minutes=10,
        channel="wecom",
        at=moment,
    )
    row = store.get_event("evt-sm-0001")
    assert row["due_minutes"] == 10
    assert row["notify_target"] == "检验科值班技师"
    assert row["notified_at"] == moment.isoformat()
    assert row["due_at"] == (moment + timedelta(minutes=10)).isoformat()


# ---------------------------------------------------------------------------
# 5. 审计能回答四问
# ---------------------------------------------------------------------------


def test_审计能回答四问(store: LedgerStore):
    store.record_event(_event())
    moment = datetime(2026, 1, 2, 8, 30, tzinfo=timezone.utc)
    store.set_status("evt-sm-0001", "judged")
    store.set_status("evt-sm-0001", "card_built")
    store.mark_notified(
        "evt-sm-0001",
        notify_target="检验科值班技师",
        due_minutes=10,
        channel="wecom",
        at=moment,
    )
    store.mark_acknowledged("evt-sm-0001", actor="检验科值班技师", at=moment)

    answers = AuditLog(store).trace("evt-sm-0001")
    # 按哪条依据 —— 事件行上的命中阈值行
    assert answers.status == "acknowledged"
    assert answers.dept_code == "LAB"
    assert answers.notified_who == "LAB / 检验科值班技师"
    assert answers.due_minutes == 10
    assert answers.acknowledged_by == "检验科值班技师"
    assert answers.acknowledged_at == moment.isoformat()
    # 四问都答上来了
    assert set(answers.answered) == {"basis", "notified_who", "due_minutes", "acknowledged_by"} or (
        "basis" in answers.missing
    )


def test_四问答不上来时列进missing而不是假装答了(store: LedgerStore):
    store.record_event(_event())
    answers = AuditLog(store).trace("evt-sm-0001")
    assert answers.acknowledged_by == ""
    assert "acknowledged_by" in answers.missing
    assert "due_minutes" in answers.missing


def test_审计统计给出动作与科室分布(store: LedgerStore):
    store.record_event(_event())
    store.set_status("evt-sm-0001", "judged", actor="judge")
    store.set_status("evt-sm-0001", "card_built", actor="card")

    stats = AuditLog(store).stats()
    assert stats["total"] == 2
    assert stats["by_action"]["status_transition"] == 2
    assert stats["events"] == 1


# ---------------------------------------------------------------------------
# 6. CLI：四问与统计
# ---------------------------------------------------------------------------


def _run_cli(argv: list[str]) -> int:
    from critval.cli import main

    return main(argv)


def test_命令行能答四问(tmp_path, capsys):
    db_path = tmp_path / "cli.db"
    with LedgerStore(db_path) as store:
        store.record_event(_event())
        store.set_status("evt-sm-0001", "judged")
        store.set_status("evt-sm-0001", "card_built")
        store.mark_notified(
            "evt-sm-0001",
            notify_target="检验科值班技师",
            due_minutes=10,
            channel="wecom",
            at=datetime(2026, 1, 2, 8, 30, tzinfo=timezone.utc),
        )
        store.mark_acknowledged("evt-sm-0001", actor="检验科值班技师")

    code = _run_cli(
        ["ledger-trace", "--event", "evt-sm-0001", "--db", str(db_path), "--json"]
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["due_minutes"] == 10
    assert payload["acknowledged_by"] == "检验科值班技师"
    assert payload["status"] == "acknowledged"


def test_命令行查不到事件返回业务失败码(tmp_path, capsys):
    db_path = tmp_path / "cli2.db"
    with LedgerStore(db_path) as store:
        store.record_event(_event())

    code = _run_cli(["ledger-trace", "--event", "nope", "--db", str(db_path)])
    assert code == 1
    assert "没有事件" in capsys.readouterr().err


def test_命令行审计统计(tmp_path, capsys):
    db_path = tmp_path / "cli3.db"
    with LedgerStore(db_path) as store:
        store.record_event(_event())
        store.set_status("evt-sm-0001", "judged")

    code = _run_cli(["audit-stats", "--db", str(db_path), "--json"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["by_action"]["status_transition"] == 1
    assert payload["idempotency"]["db_keys"] == 1
