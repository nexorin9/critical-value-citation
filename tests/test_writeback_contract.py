"""回写契约、幂等冲突与补写入口单测（任务 id 43）。

盯住五件事：

* **契约闸门** —— 必填 / 类型 / 枚举 / 长度 / 时刻格式不合规时
  **一个字节都不发**，并给**字段级**原因；
* **幂等冲突** —— 同一 ``message_control_id`` 下内容与既有事实不一致 →
  **报冲突、转人工、既有事实一个字都不改**；
* **失败分类** —— ``4xx`` 契约错**不重试**，``5xx`` / 超时可重试，
  分类结果直接决定重试策略；
* **补写入口** —— 显式补写失败的那条；仍失败就**留在待写队列**并告警；
* **未确认零回写** —— 这条红线不能因为加了闸门而松动。
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
from critval.writeback.client import RegistrationSystemClient, build_receipt_payload
from critval.writeback.contract import (
    diff_payloads,
    identity_fingerprint,
    payload_fingerprint,
    validate_payload,
)
from critval.writeback.failures import (
    FAILURE_CONFLICT,
    FAILURE_CONTRACT,
    FAILURE_SERVER,
    FAILURE_TIMEOUT,
    FAILURE_VALIDATION,
    classify_failure,
    is_retryable,
    retry_decision,
)
from critval.writeback.registry import (
    CONFLICT_ALREADY_WRITTEN,
    CONFLICT_CONTENT_CHANGED,
    CONFLICT_REMOTE_DIFFERS,
    RegistryWriteback,
)

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


def _event(
    event_id: str = "evt-0001",
    mcid: str = MCID,
    value: str = "25",
    observed_at: datetime = datetime(2026, 1, 2, 8, 30),
) -> CriticalEvent:
    return CriticalEvent(
        event_id=event_id,
        message_control_id=mcid,
        specimen_no="SPEC-0001",
        item_code="718-7",
        item_name="血红蛋白",
        value=Decimal(value),
        unit="g/L",
        observed_at=observed_at,
        dept_code=DEPT,
    )


def _judgement(event: CriticalEvent | None = None):
    from critval.judge.critical import judge_event
    from critval.kb.loader import load_threshold_table

    table = load_threshold_table(FIXTURE)
    return judge_event(event or _event(), table.rows)


def _acknowledged(
    store: LedgerStore,
    event_id: str = "evt-0001",
    mcid: str = MCID,
    value: str = "25",
    observed_at: datetime = datetime(2026, 1, 2, 8, 30),
    *,
    judged: bool = True,
) -> None:
    """把一条事件推到「值班人已确认」。

    ``judged=False`` 造的是**判定过但没落阈值依据行**的不存在场景 ——
    用来验证契约闸门会拦下「凭哪一行阈值判的危急」说不清的回写体。
    """
    event = _event(event_id, mcid, value, observed_at)
    store.record_event(event)
    if judged:
        store.record_judgement(event_id, _judgement(event))
    else:
        # 状态照常推进到「已判定」，只是**没有阈值依据行**。
        store.set_status(event_id, "judged")
    store.set_status(event_id, "card_built")
    store.mark_notified(
        event_id, notify_target="李静 · 检验科值班技师", due_minutes=10, channel="wecom"
    )
    store.mark_pending_ack(event_id)
    store.mark_acknowledged(event_id, actor="检验科值班技师")


def _writeback(double: RegistryDouble, store: LedgerStore, **kwargs) -> RegistryWriteback:
    client = RegistrationSystemClient(double.base_url, TOKEN)
    return RegistryWriteback(client, store, **kwargs)


# ---------------------------------------------------------------------------
# 契约校验（纯函数）
# ---------------------------------------------------------------------------


def test_契约校验九个字段齐备时通过():
    payload = {
        "message_control_id": "MSG0001",
        "item_code": "718-7",
        "value": "25",
        "threshold_row": "示例危急值阈值表.xlsx · 第 2 行 · 示例 v1.0",
        "notified_dept": "ICU",
        "duty_person": "李静 · 检验科值班技师",
        "due_minutes": 10,
        "receipt_status": "acknowledged",
        "receipt_at": "2026-01-02T08:31:00+00:00",
    }
    result = validate_payload(payload)
    assert result.ok is True
    assert result.issues == ()
    assert result.summary().startswith("契约校验通过")


def test_契约校验缺必填字段时逐字段给原因():
    result = validate_payload({"message_control_id": "MSG0001"})
    assert result.ok is False
    # 缺的字段一个不落地报出来，带字段名与人话原因。
    assert set(result.fields) >= {
        "item_code",
        "value",
        "threshold_row",
        "notified_dept",
        "duty_person",
        "due_minutes",
        "receipt_status",
    }
    for issue in result.issues:
        assert issue.field
        assert issue.message
        assert issue.code


def test_契约校验报枚举越界():
    result = validate_payload(
        {
            "message_control_id": "MSG0001",
            "item_code": "718-7",
            "value": "25",
            "threshold_row": "第 2 行",
            "notified_dept": "ICU",
            "duty_person": "李静",
            "due_minutes": 10,
            "receipt_status": "acknowledgedd",  # 拼错了
            "receipt_at": "2026-01-02T08:31:00+00:00",
        }
    )
    assert result.ok is False
    assert result.fields == ("receipt_status",)
    assert result.issues[0].code == "not_in_enum"
    assert "acknowledged" in result.issues[0].message


def test_契约校验报字符串超长():
    result = validate_payload(
        {
            "message_control_id": "M" * 200,
            "item_code": "718-7",
            "value": "25",
            "threshold_row": "第 2 行",
            "notified_dept": "ICU",
            "duty_person": "李静",
            "due_minutes": 10,
            "receipt_status": "acknowledged",
        }
    )
    assert result.ok is False
    assert "message_control_id" in result.fields
    assert result.issues[0].code == "too_long"


def test_契约校验报时限越界与非数值():
    payload = {
        "message_control_id": "MSG0001",
        "item_code": "718-7",
        "value": "25",
        "threshold_row": "第 2 行",
        "notified_dept": "ICU",
        "duty_person": "李静",
        "due_minutes": 0,
        "receipt_status": "acknowledged",
    }
    result = validate_payload(payload)
    assert result.ok is False
    assert "due_minutes" in result.fields
    assert result.issues[0].code == "out_of_range"

    payload["due_minutes"] = "十分钟"
    result = validate_payload(payload)
    assert result.ok is False
    assert result.issues[0].code == "wrong_type"


def test_契约校验报时刻格式不对():
    payload = {
        "message_control_id": "MSG0001",
        "item_code": "718-7",
        "value": "25",
        "threshold_row": "第 2 行",
        "notified_dept": "ICU",
        "duty_person": "李静",
        "due_minutes": 10,
        "receipt_status": "acknowledged",
        "receipt_at": "昨天下午三点",
    }
    result = validate_payload(payload)
    assert result.ok is False
    assert result.fields == ("receipt_at",)
    assert result.issues[0].code == "bad_format"


def test_契约校验拦控制字符():
    payload = {
        "message_control_id": "MSG0001",
        "item_code": "718-7",
        "value": "25",
        "threshold_row": "第 2 行",
        "notified_dept": "ICU\nX",
        "duty_person": "李静",
        "due_minutes": 10,
        "receipt_status": "acknowledged",
    }
    result = validate_payload(payload)
    assert result.ok is False
    assert result.fields == ("notified_dept",)
    assert result.issues[0].code == "control_char"


# ---------------------------------------------------------------------------
# 契约闸门（发出前拦截）
# ---------------------------------------------------------------------------


def test_契约不过时一个字节都不发(double: RegistryDouble, store: LedgerStore):
    """没有阈值依据行的回写体 —— 登记系统不知道「凭哪行阈值判的危急」。"""
    _acknowledged(store, "evt-0001", judged=False)

    outcome = _writeback(double, store).write_back("evt-0001")

    assert outcome.ok is False
    assert outcome.skipped is True
    assert outcome.failure_class == FAILURE_VALIDATION
    assert outcome.retryable is False
    assert "threshold_row" in outcome.issues[0].field
    # **一次出站调用都不发** —— 不能拿对端当校验器。
    assert double.calls == []
    assert double.posted == []
    # 落 writeback_failed 等人工改数据。
    assert store.get_status("evt-0001") == "writeback_failed"
    kinds = [entry.get("action") for entry in store.audit_entries("evt-0001")]
    assert "writeback_contract_invalid" in kinds


def test_契约不过时已确认回执不丢(double: RegistryDouble, store: LedgerStore):
    _acknowledged(store, "evt-0001", judged=False)

    _writeback(double, store).write_back("evt-0001")

    row = store.get_event("evt-0001")
    assert row["acknowledged_by"] == "检验科值班技师"
    assert row["acknowledged_at"]
    assert row["notify_target"] == "李静 · 检验科值班技师"


def test_契约不过时进待写队列并说明下一步(
    double: RegistryDouble, store: LedgerStore
):
    _acknowledged(store, "evt-0001", judged=False)
    writeback = _writeback(double, store)
    writeback.write_back("evt-0001")

    detail = writeback.pending_detail()
    assert detail[0]["failure_class"] == FAILURE_VALIDATION
    assert detail[0]["retryable"] is False
    assert "人工" in detail[0]["next_action"]


# ---------------------------------------------------------------------------
# 幂等冲突
# ---------------------------------------------------------------------------


def test_同一消息控制号内容不同时报冲突且不覆盖既有事实(
    double: RegistryDouble, store: LedgerStore
):
    """第一条已写进登记系统；同键再来一条**内容不同**的回执 → 冲突。

    真实形态：一条 HL7 报文里两条危急结果，或一条修正结果 —— 消息控制号
    相同，内容不同。登记系统按键覆盖，所以这里必须挡住。
    """
    _acknowledged(store, "evt-0001", MCID, value="25")
    first = _writeback(double, store).write_back("evt-0001")
    assert first.ok is True
    assert double.get(MCID)["value"] == "25"

    # 第二个事件：同一个消息控制号，**结果值不同**（修正结果）。
    _acknowledged(
        store,
        "evt-0002",
        MCID,
        value="18",
        observed_at=datetime(2026, 1, 2, 9, 15),
    )

    outcome = _writeback(double, store).write_back("evt-0002")

    assert outcome.ok is False
    assert outcome.skipped is True
    assert outcome.failure_class == FAILURE_CONFLICT
    assert outcome.conflict is not None
    assert outcome.conflict.kind == CONFLICT_ALREADY_WRITTEN
    assert "value" in outcome.conflict.fields
    assert outcome.retryable is False
    # **冲突时不发请求** —— 既有事实不许被覆盖。
    assert len(double.posted) == 1
    assert double.get(MCID)["value"] == "25"
    # 本地既有事实逐字段未被改动。
    receipt = store.get_writeback_receipt(MCID)
    assert json.loads(receipt["payload_json"])["value"] == "25"
    assert receipt["payload_hash"] == payload_fingerprint(
        build_receipt_payload(
            store.get_event("evt-0001"), receipt_status="acknowledged"
        )
    )


def test_冲突时事件转人工且冲突留痕(double: RegistryDouble, store: LedgerStore):
    _acknowledged(store, "evt-0001", MCID, value="25")
    _writeback(double, store).write_back("evt-0001")

    _acknowledged(
        store,
        "evt-0002",
        MCID,
        value="18",
        observed_at=datetime(2026, 1, 2, 9, 15),
    )
    outcome = _writeback(double, store).write_back("evt-0002")

    assert outcome.failure_class == FAILURE_CONFLICT
    # 冲突要人裁决 → 转人工。
    assert store.get_status("evt-0002") == "transferred_to_human"
    kinds = [entry.get("action") for entry in store.audit_entries("evt-0002")]
    assert "writeback_conflict" in kinds
    # 冲突进尝试流水，可复算。
    attempts = store.writeback_attempts(message_control_id=MCID)
    assert attempts[0]["outcome"] == "conflict"
    assert attempts[0]["failure_class"] == FAILURE_CONFLICT
    assert attempts[0]["retryable"] == 0


def test_上一次对端不可用之后内容变了也算冲突(
    double: RegistryDouble, store: LedgerStore
):
    """上次是 5xx（可能其实写进去了），此后内容被改 → 不得拿新内容盖上去。"""
    _acknowledged(store, "evt-0001", MCID)
    double.post_status_code = 500
    failed = _writeback(double, store).write_back("evt-0001")
    assert failed.ok is False
    assert failed.failure_class == FAILURE_SERVER
    assert store.get_status("evt-0001") == "writeback_failed"

    # 同一个消息控制号下又来一条**内容不同**的回执（修正结果）。
    _acknowledged(
        store,
        "evt-0002",
        MCID,
        value="18",
        observed_at=datetime(2026, 1, 2, 9, 15),
    )
    double.post_status_code = 0

    outcome = _writeback(double, store).write_back("evt-0002")

    assert outcome.ok is False
    assert outcome.failure_class == FAILURE_CONFLICT
    assert outcome.conflict.kind == CONFLICT_CONTENT_CHANGED
    assert len(double.posted) == 0


def test_上一次契约错之后改了数据允许重发(
    double: RegistryDouble, store: LedgerStore
):
    """契约错是「人正在改数据」—— 改完重发是正当流程，不算冲突。"""
    _acknowledged(store, "evt-0001", MCID)
    writeback = _writeback(double, store)

    rejected = writeback.write_back("evt-0001", receipt_status="acknowledgedd")
    assert rejected.failure_class == FAILURE_VALIDATION
    assert len(double.posted) == 0

    outcome = writeback.replay("evt-0001")
    assert outcome.ok is True
    assert store.get_status("evt-0001") == "registered"
    assert len(double.posted) == 1


def test_远端探针发现内容不同判冲突(double: RegistryDouble, store: LedgerStore):
    """登记系统里那条内容与本次要写的不一致 → 冲突，不覆盖。"""
    _acknowledged(store, "evt-0001", MCID)
    local = build_receipt_payload(
        store.get_event("evt-0001"), receipt_status="acknowledged"
    )
    double.seed(dict(local, value="99"))

    outcome = _writeback(double, store, probe_remote=True).write_back("evt-0001")

    assert outcome.ok is False
    assert outcome.failure_class == FAILURE_CONFLICT
    assert outcome.conflict.kind == CONFLICT_REMOTE_DIFFERS
    assert "value" in outcome.conflict.fields
    assert double.posted == []
    assert double.get(MCID)["value"] == "99"


def test_远端探针内容一致时不误报冲突(double: RegistryDouble, store: LedgerStore):
    """对端把状态词归一成 accepted —— 那是正常归一，不该报冲突。"""
    _acknowledged(store, "evt-0001", MCID)
    local = build_receipt_payload(
        store.get_event("evt-0001"), receipt_status="acknowledged"
    )
    double.seed(
        dict(local, receipt_status="accepted", receipt_at="2026-01-02T09:00:00+00:00")
    )

    outcome = _writeback(double, store, probe_remote=True).write_back("evt-0001")

    assert outcome.ok is True
    assert len(double.posted) == 1


def test_默认不探针时不会多发一次出站调用(
    double: RegistryDouble, store: LedgerStore
):
    """守护进程的自动回写不探针 —— 每次回写都多一次往返不值当。"""
    _acknowledged(store, "evt-0001", MCID)

    outcome = _writeback(double, store).write_back("evt-0001")

    assert outcome.ok is True
    assert [method for method, _path, _body in double.calls] == ["POST"]


# ---------------------------------------------------------------------------
# 失败分类与重试策略
# ---------------------------------------------------------------------------


def test_四xx归为契约错且不可重试():
    assert classify_failure(400) == FAILURE_CONTRACT
    assert classify_failure(422) == FAILURE_CONTRACT
    assert is_retryable(FAILURE_CONTRACT) is False
    decision = retry_decision(FAILURE_CONTRACT, attempts=1)
    assert decision.should_retry is False
    assert "人工" in decision.action


def test_五xx与超时归为可重试():
    assert classify_failure(500) == FAILURE_SERVER
    assert classify_failure(503) == FAILURE_SERVER
    assert classify_failure(0, "登记系统调用超时（5.0 秒）") == FAILURE_TIMEOUT
    assert is_retryable(FAILURE_SERVER) is True
    assert is_retryable(FAILURE_TIMEOUT) is True
    decision = retry_decision(FAILURE_SERVER, attempts=1, max_attempts=3)
    assert decision.should_retry is True


def test_重试超过上限转人工():
    decision = retry_decision(FAILURE_SERVER, attempts=3, max_attempts=3)
    assert decision.should_retry is False
    assert "人工" in decision.action


def test_五xx失败默认只发一次(double: RegistryDouble, store: LedgerStore):
    _acknowledged(store, "evt-0001", MCID)
    double.post_status_code = 503

    outcome = _writeback(double, store, max_attempts=1).write_back("evt-0001")

    assert outcome.ok is False
    assert outcome.failure_class == FAILURE_SERVER
    assert outcome.retryable is True
    assert len(double.calls) == 1  # 只发了一次


def test_允许重试时五xx会退避重试(double: RegistryDouble, store: LedgerStore):
    _acknowledged(store, "evt-0001", MCID)
    double.post_status_code = 503

    slept: list[float] = []
    writeback = RegistryWriteback(
        RegistrationSystemClient(double.base_url, TOKEN),
        store,
        max_attempts=3,
        backoff_seconds=0.01,
        sleep=slept.append,
    )
    outcome = writeback.write_back("evt-0001")

    assert outcome.ok is False
    assert outcome.attempts == 3
    assert len(double.calls) == 3
    assert len(slept) == 2  # 两次退避


def test_契约错不重试即使允许重试(double: RegistryDouble, store: LedgerStore):
    """4xx 是契约错 —— 重试一万次也是一样的错。"""
    _acknowledged(store, "evt-0001", MCID)
    double.post_status_code = 422

    writeback = RegistryWriteback(
        RegistrationSystemClient(double.base_url, TOKEN),
        store,
        max_attempts=3,
        backoff_seconds=0.0,
        sleep=lambda _s: None,
    )
    outcome = writeback.write_back("evt-0001")

    assert outcome.ok is False
    assert outcome.failure_class == FAILURE_CONTRACT
    assert outcome.attempts == 1
    assert len(double.calls) == 1


# ---------------------------------------------------------------------------
# 补写入口
# ---------------------------------------------------------------------------


def test_补写把失败的那条写回去并离开队列(double: RegistryDouble, store: LedgerStore):
    _acknowledged(store, "evt-0001", MCID)
    double.post_status_code = 500
    writeback = _writeback(double, store)
    writeback.write_back("evt-0001")
    assert store.get_status("evt-0001") == "writeback_failed"

    double.post_status_code = 0
    outcome = writeback.replay("evt-0001")

    assert outcome.ok is True
    assert store.get_status("evt-0001") == "registered"
    assert writeback.pending() == []


def test_补写仍失败时留在待写队列并继续告警(
    double: RegistryDouble, store: LedgerStore
):
    _acknowledged(store, "evt-0001", MCID)
    double.post_status_code = 500
    writeback = _writeback(double, store)
    writeback.write_back("evt-0001")

    outcome = writeback.replay("evt-0001")

    assert outcome.ok is False
    # 仍失败 → 留在待写队列里（没被丢弃），且明确告诉人下一步做什么。
    pending = writeback.pending()
    assert [row["event_id"] for row in pending] == ["evt-0001"]
    detail = writeback.pending_detail()
    assert detail[0]["failure_class"] == FAILURE_SERVER
    assert detail[0]["retryable"] is True
    assert detail[0]["next_action"]
    assert detail[0]["attempts"] == 2


def test_待写队列清单给出分类与下一步(double: RegistryDouble, store: LedgerStore):
    _acknowledged(store, "evt-0001", MCID)
    double.post_status_code = 422
    writeback = _writeback(double, store)
    writeback.write_back("evt-0001")

    detail = writeback.pending_detail()
    assert detail[0]["failure_class"] == FAILURE_CONTRACT
    assert detail[0]["retryable"] is False
    assert "人工" in detail[0]["next_action"]


def test_显式补写未确认的事件被拒(double: RegistryDouble, store: LedgerStore):
    store.record_event(_event())
    store.set_status("evt-0001", "judged")

    outcome = _writeback(double, store).replay("evt-0001")

    assert outcome.skipped is True
    assert double.calls == []


def test_未确认事件零回写(double: RegistryDouble, store: LedgerStore):
    """加了闸门也不能松动这条红线。"""
    store.record_event(_event())
    store.set_status("evt-0001", "judged")
    store.set_status("evt-0001", "card_built")
    store.mark_notified("evt-0001", notify_target="李静", due_minutes=10, channel="wecom")
    store.mark_pending_ack("evt-0001")

    outcome = _writeback(double, store).write_back("evt-0001")

    assert outcome.ok is False
    assert outcome.skipped is True
    assert double.calls == []
    assert double.posted == []


# ---------------------------------------------------------------------------
# 指纹
# ---------------------------------------------------------------------------


def test_内容相同指纹相同内容不同指纹不同():
    left = {
        "message_control_id": "M1",
        "item_code": "718-7",
        "value": "25",
        "threshold_row": "第 2 行",
        "notified_dept": "ICU",
        "duty_person": "李静",
        "due_minutes": 10,
        "receipt_status": "acknowledged",
        "receipt_at": "2026-01-02T08:31:00+00:00",
    }
    right = dict(left, value="26")
    assert payload_fingerprint(left) == payload_fingerprint(dict(left))
    assert payload_fingerprint(left) != payload_fingerprint(right)
    assert diff_payloads(left, right) == ("value",)


def test_实质指纹不看状态词与时刻():
    """对端把 acknowledged 归一成 accepted —— 不是「内容变了」。"""
    left = {
        "item_code": "718-7",
        "value": "25",
        "threshold_row": "第 2 行",
        "notified_dept": "ICU",
        "duty_person": "李静",
        "due_minutes": 10,
        "receipt_status": "acknowledged",
        "receipt_at": "2026-01-02T08:31:00+00:00",
    }
    right = dict(left, receipt_status="accepted", receipt_at="2026-01-02T09:00:00+00:00")
    assert identity_fingerprint(left) == identity_fingerprint(right)
    assert set(diff_payloads(left, right)) == {"receipt_at", "receipt_status"}
