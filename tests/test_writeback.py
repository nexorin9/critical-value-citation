"""危急值登记系统回写单测（任务 id 11）。

盯住五件事：

* **未确认零回写** —— 状态不是 ``acknowledged`` 时**一次出站调用都不发**
  （这是整条闭环的红线：未确认的回执不许变成登记系统里的已生效事实）；
* **回写体逐字段齐备** —— 九个字段一个不缺，值与台账对得上；
* **幂等键就是 message_control_id** —— 补写不会在登记系统里生成第二条；
* **失败不丢已确认回执** —— 落 ``writeback_failed``，确认痕迹原样保留；
* **能补写** —— ``replay`` 把上一轮失败的补上，状态推进到 ``registered``。
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from critval.ledger.store import LedgerStore
from critval.models import CriticalEvent
from critval.writeback.client import RECEIPT_FIELDS, RegistrationSystemClient
from critval.writeback.registry import RegistryWriteback

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fixtures.registry_double import RegistryDouble  # noqa: E402

TOKEN = "test-registry-token-0001"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "脱敏样例阈值表.xlsx"
DEPT = "ICU"
MCID = "MSG0001"


@pytest.fixture()
def store() -> LedgerStore:
    instance = LedgerStore(":memory:")
    try:
        yield instance
    finally:
        instance.close()


@pytest.fixture()
def double() -> RegistryDouble:
    instance = RegistryDouble(TOKEN)
    instance.start()
    try:
        yield instance
    finally:
        instance.stop()


def _event(event_id: str = "evt-0001", patient_ref: str = "") -> CriticalEvent:
    return CriticalEvent(
        event_id=event_id,
        message_control_id=MCID,
        specimen_no="SPEC-0001",
        item_code="718-7",
        item_name="血红蛋白",
        value=Decimal("25"),
        unit="g/L",
        observed_at=datetime(2026, 1, 2, 8, 30),
        dept_code=DEPT,
        patient_ref=patient_ref,
    )


def _judgement():
    """按样例阈值表判一次（阈值依据行要落到台账行上）。"""
    from critval.judge.critical import judge_event
    from critval.kb.loader import load_threshold_table

    table = load_threshold_table(FIXTURE)
    return judge_event(_event(), table.rows)


def _acknowledged(store: LedgerStore, event_id: str = "evt-0001") -> None:
    """把一条事件推到「值班人已确认」。"""
    store.record_event(_event(event_id))
    store.record_judgement(event_id, _judgement())
    store.set_status(event_id, "card_built")
    store.mark_notified(
        event_id,
        notify_target="李静 · 检验科值班技师",
        due_minutes=10,
        channel="wecom",
    )
    store.mark_pending_ack(event_id)
    store.mark_acknowledged(event_id, actor="检验科值班技师")


def _writeback(double: RegistryDouble, store: LedgerStore) -> RegistryWriteback:
    client = RegistrationSystemClient(double.base_url, TOKEN)
    return RegistryWriteback(client, store)


# ---------------------------------------------------------------------------
# 未确认零回写
# ---------------------------------------------------------------------------


def test_未确认事件零回写(double: RegistryDouble, store: LedgerStore):
    store.record_event(_event())
    store.set_status("evt-0001", "judged")
    store.set_status("evt-0001", "card_built")
    store.mark_notified("evt-0001", notify_target="李静", due_minutes=10, channel="wecom")
    store.mark_pending_ack("evt-0001")

    outcome = _writeback(double, store).write_back("evt-0001")

    assert outcome.ok is False
    assert outcome.skipped is True
    assert "acknowledged" in outcome.reason
    # **一次出站调用都不发** —— 登记系统那边不能出现这条记录。
    assert len(double.posted) == 0
    assert double.calls == []


def test_台账里没这条事件时零回写(double: RegistryDouble, store: LedgerStore):
    outcome = _writeback(double, store).write_back("evt-missing")
    assert outcome.skipped is True
    assert "没有这条事件" in outcome.reason
    assert double.calls == []


def test_已推送待回执状态也不回写(double: RegistryDouble, store: LedgerStore):
    store.record_event(_event())
    store.set_status("evt-0001", "judged")
    store.set_status("evt-0001", "card_built")
    store.mark_notified("evt-0001", notify_target="李静", due_minutes=10, channel="wecom")
    store.mark_pending_ack("evt-0001")

    outcome = _writeback(double, store).write_back("evt-0001")
    assert outcome.skipped is True
    assert len(double.posted) == 0


# ---------------------------------------------------------------------------
# 确认后回写
# ---------------------------------------------------------------------------


def test_确认后回写体逐字段齐备(double: RegistryDouble, store: LedgerStore):
    _acknowledged(store)
    outcome = _writeback(double, store).write_back("evt-0001")

    assert outcome.ok is True
    assert outcome.status == "registered"
    assert len(double.posted) == 1
    posted = double.posted[0]
    # 九个字段一个不缺。
    for name in RECEIPT_FIELDS:
        assert name in posted, f"回写体缺字段 {name}"
    assert posted["message_control_id"] == MCID
    assert posted["item_code"] == "718-7"
    assert posted["value"] == "25"
    assert posted["threshold_row"]
    assert posted["notified_dept"] == DEPT
    assert posted["duty_person"] == "李静 · 检验科值班技师"
    assert posted["due_minutes"] == 10
    assert posted["receipt_status"] == "acknowledged"
    assert posted["receipt_at"]


def test_确认后回写带Bearer头且幂等键正确(double: RegistryDouble, store: LedgerStore):
    _acknowledged(store)
    _writeback(double, store).write_back("evt-0001")

    method, path, body = double.calls[0]
    assert method == "POST"
    assert path == "/api/v1/critical-events"
    assert '"message_control_id": "MSG0001"' in body or '"message_control_id":"MSG0001"' in body
    # 登记系统按幂等键去重。
    assert double.get(MCID) is not None


def test_回写成功后状态推进到已回写(double: RegistryDouble, store: LedgerStore):
    _acknowledged(store)
    _writeback(double, store).write_back("evt-0001")
    assert store.get_status("evt-0001") == "registered"


def test_回写成功会留审计(double: RegistryDouble, store: LedgerStore):
    _acknowledged(store)
    _writeback(double, store).write_back("evt-0001")
    kinds = [entry.get("action") for entry in store.audit_entries("evt-0001")]
    assert "writeback_attempted" in kinds
    assert "writeback_succeeded" in kinds


# ---------------------------------------------------------------------------
# 失败与补写
# ---------------------------------------------------------------------------


def test_回写失败落writeback_failed且确认痕迹不丢(double: RegistryDouble, store: LedgerStore):
    _acknowledged(store)
    double.post_status_code = 500

    outcome = _writeback(double, store).write_back("evt-0001")

    assert outcome.ok is False
    assert outcome.skipped is False
    assert outcome.status == "writeback_failed"
    assert store.get_status("evt-0001") == "writeback_failed"

    row = store.get_event("evt-0001")
    # 已确认的回执不丢 —— 确认人、确认时刻、通知对象、上报时限都还在。
    assert row["acknowledged_by"] == "检验科值班技师"
    assert row["acknowledged_at"]
    assert row["notify_target"] == "李静 · 检验科值班技师"
    assert row["due_minutes"] == 10


def test_回写失败会留审计(double: RegistryDouble, store: LedgerStore):
    _acknowledged(store)
    double.post_status_code = 500
    _writeback(double, store).write_back("evt-0001")
    kinds = [entry.get("action") for entry in store.audit_entries("evt-0001")]
    assert "writeback_failed" in kinds


def test_补写能把失败的那条写回去(double: RegistryDouble, store: LedgerStore):
    _acknowledged(store)
    double.post_status_code = 500
    _writeback(double, store).write_back("evt-0001")
    assert store.get_status("evt-0001") == "writeback_failed"

    double.post_status_code = 0
    outcome = _writeback(double, store).replay("evt-0001")

    assert outcome.ok is True
    assert outcome.status == "registered"
    assert store.get_status("evt-0001") == "registered"
    assert len(double.calls) == 2  # 首发失败 + 补写成功，两次出站调用
    # 两次请求体带的是同一个幂等键 —— 登记系统按它覆盖，不会生成第二条。
    keys = [json.loads(body)["message_control_id"] for _method, _path, body in double.calls]
    assert keys == [MCID, MCID]
    assert len(double.receipts) == 1
    assert len(double.posted) == 1


def test_待补写清单能列出失败事件(double: RegistryDouble, store: LedgerStore):
    _acknowledged(store)
    double.post_status_code = 500
    writeback = _writeback(double, store)
    writeback.write_back("evt-0001")

    pending = writeback.pending()
    assert [row["event_id"] for row in pending] == ["evt-0001"]


def test_未确认事件不能被补写(double: RegistryDouble, store: LedgerStore):
    store.record_event(_event())
    store.set_status("evt-0001", "judged")
    outcome = _writeback(double, store).replay("evt-0001")
    assert outcome.skipped is True
    assert double.calls == []


# ---------------------------------------------------------------------------
# 凭据与载荷
# ---------------------------------------------------------------------------


def test_缺凭据位时在发请求之前就报错(double: RegistryDouble, store: LedgerStore):
    _acknowledged(store)
    client = RegistrationSystemClient(double.base_url, "")
    writeback = RegistryWriteback(client, store)
    with pytest.raises(Exception) as excinfo:
        writeback.write_back("evt-0001")
    assert "CRITVAL_TOKEN" in str(excinfo.value)
    assert double.calls == []


def test_回写体不含患者标识(double: RegistryDouble, store: LedgerStore):
    store.record_event(_event(patient_ref="PATREF-9f2c"))
    store.set_status("evt-0001", "judged")
    # 任务 id 43 起回写体要过**契约闸门**：``threshold_row``（阈值依据行）
    # 是必填 —— 登记系统要知道「凭哪一行阈值判的危急」。真实流程里危急值
    # 一定先判定再通知，所以这里补上判定记录（原来这条用例没判定就通知，
    # 是一份不存在的场景）。
    store.record_judgement("evt-0001", _judgement())
    store.set_status("evt-0001", "card_built")
    store.mark_notified("evt-0001", notify_target="李静", due_minutes=10, channel="wecom")
    store.mark_acknowledged("evt-0001", actor="检验科值班技师")

    _writeback(double, store).write_back("evt-0001")
    posted = double.posted[0]
    assert "PATREF-9f2c" not in str(posted)
    assert "住院流水号" not in str(posted)
