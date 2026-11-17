"""本地台账 ↔ 登记系统回执对账（任务 id 38）。

盯住五件事：

1. **四类差异都能检出** —— ``missing_local`` / ``missing_remote`` /
   ``status_mismatch`` / ``field_mismatch``，每类都给得出建议动作；
2. **差异不自动抹平** —— 只读对账**不改任何本地状态、不发任何写请求**；
   要动状态必须显式 ``resolve()``，且每一次处理都留审计；
3. **清单可复算** —— 同一份台账 + 同一个对端状态，两次对账得到**逐字节
   相同**的清单；每行都带处理建议；
4. **凭据与错误分级** —— 缺凭据位在发请求之前就报错；``4xx`` 不可重试、
   ``5xx`` 可重试；对端不支持列表查询时**降级并写明**；
5. **真 socket 端到端** —— 对着本机登记系统替身跑真实 HTTP 调用（替身只
   替掉对端系统本身，协议是真的）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from critval.ledger.store import LedgerStore
from critval.models import Citation, CriticalEvent, Judgement, ThresholdRow
from critval.writeback.client import (
    RECEIPT_FIELDS,
    RegistrationError,
    RegistrationSystemClient,
    build_receipt_payload,
)
from critval.writeback.reconcile import (
    ACTION_MANUAL,
    ACTION_REPLAY,
    ACTION_WRITEBACK,
    DIFF_FIELD_MISMATCH,
    DIFF_MISSING_LOCAL,
    DIFF_MISSING_REMOTE,
    DIFF_STATUS_MISMATCH,
    Reconciler,
    ReconcileError,
    TARGET_LOCAL,
    TARGET_REGISTRY,
)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fixtures.registry_double import RegistryDouble  # noqa: E402

NOW = datetime(2026, 10, 30, 9, 0, tzinfo=timezone.utc)
TOKEN = "test-registry-token-0001"


# ---------------------------------------------------------------------------
# 造数据
# ---------------------------------------------------------------------------
def _threshold_row(item_code: str, item_name: str, line_no: int) -> ThresholdRow:
    return ThresholdRow(
        item_code=item_code,
        item_name=item_name,
        unit="g/L",
        low_critical=Decimal("30"),
        high_critical=Decimal("200"),
        reference_range_raw="115-150",
        effective_from=date(2026, 1, 1),
        version="示例 v1.0",
        citation=Citation(
            source_name="示例危急值阈值表.xlsx",
            source_sha256="sha-threshold-table",
            kind="threshold_row",
            line_no=line_no,
            file_version="示例 v1.0",
        ),
        row_no=line_no,
    )


def _event(event_id: str, *, mcid: str, item_code: str, item_name: str,
           dept_code: str, value: str, line_no: int = 2) -> CriticalEvent:
    return CriticalEvent(
        event_id=event_id,
        message_control_id=mcid,
        specimen_no=f"SP-{event_id}",
        item_code=item_code,
        item_name=item_name,
        value=Decimal(value),
        unit="g/L",
        observed_at=datetime(2026, 10, 29, 8, 30, tzinfo=timezone.utc),
        dept_code=dept_code,
        patient_ref="pt-000000000001",
    )


def _seed_event(store: LedgerStore, event: CriticalEvent, *, status: str,
                notify_target: str = "检验科值班技师", due_minutes: int = 10) -> None:
    """落一条事件并推到指定状态（只走状态机允许的路径）。"""
    store.record_event(event)
    store.record_judgement(
        event.event_id,
        Judgement(
            decision="critical",
            reason="低于低危急值 30",
            item_code=event.item_code,
            value=event.value,
            unit=event.unit,
            threshold_row=_threshold_row(event.item_code, event.item_name, 2),
        ),
    )
    store.set_status(event.event_id, "card_built")
    store.mark_notified(
        event.event_id,
        notify_target=notify_target,
        due_minutes=due_minutes,
        channel="wecom",
        at=NOW - timedelta(minutes=30),
    )
    if status in ("acknowledged", "registered", "writeback_failed"):
        store.mark_acknowledged(event.event_id, actor=notify_target, at=NOW - timedelta(minutes=20))
    if status in ("registered", "archived"):
        store.mark_registered(event.event_id, at=NOW - timedelta(minutes=15))
    if status == "archived":
        store.mark_archived(event.event_id, actor="duty", detail="复核后归档")
    if status == "writeback_failed":
        store.set_status(event.event_id, "writeback_failed", actor="writeback",
                         detail="登记系统不可达")


def _receipt(mcid: str, *, item_code: str = "718-7", value: str = "25",
             dept: str = "LAB", duty: str = "检验科值班技师",
             due_minutes: int = 10, status: str = "accepted",
             threshold_row: str = "示例危急值阈值表.xlsx · 第 2 行 · 示例 v1.0") -> dict[str, Any]:
    """一条登记系统回执（字段按 spec.md 的九项规范）。"""
    return {
        "message_control_id": mcid,
        "item_code": item_code,
        "value": value,
        "threshold_row": threshold_row,
        "notified_dept": dept,
        "duty_person": duty,
        "due_minutes": due_minutes,
        "receipt_status": status,
        "receipt_at": "2026-10-29T08:45:00",
    }


@pytest.fixture()
def store() -> LedgerStore:
    """一份覆盖四类差异的小台账。

    * ``evt-r-01`` 已确认未回写，远端**无**回执 → missing_remote（补写）
    * ``evt-r-02`` 已回写，远端**无**回执 → missing_remote（重放）
    * ``evt-r-03`` 已确认未回写，远端**已受理** → status_mismatch（补写本地）
    * ``evt-r-04`` 已回写，远端字段**不一致** → field_mismatch（重放）
    * ``evt-r-05`` 已回写，远端**拒收** → status_mismatch（标记人工处理）
    * ``evt-r-06`` 已推送待回执，远端**有**回执 → 回执先于人工确认（人工处理）
    * ``evt-r-07`` 已回写，远端**一致** → 无差异
    """
    instance = LedgerStore(":memory:")
    try:
        _seed_event(instance, _event("evt-r-01", mcid="MC-R-01", item_code="718-7",
                                     item_name="血红蛋白", dept_code="LAB", value="25"),
                    status="acknowledged")
        _seed_event(instance, _event("evt-r-02", mcid="MC-R-02", item_code="2823-3",
                                     item_name="钾", dept_code="ICU", value="6.2"),
                    status="registered", notify_target="ICU 值班护士", due_minutes=15)
        _seed_event(instance, _event("evt-r-03", mcid="MC-R-03", item_code="6690-2",
                                     item_name="肌酐", dept_code="LAB", value="320"),
                    status="acknowledged")
        _seed_event(instance, _event("evt-r-04", mcid="MC-R-04", item_code="2345-7",
                                     item_name="钠", dept_code="EMERG", value="160"),
                    status="registered", notify_target="急诊值班护士", due_minutes=10)
        _seed_event(instance, _event("evt-r-05", mcid="MC-R-05", item_code="7777-7",
                                     item_name="肌酸激酶", dept_code="ICU", value="900"),
                    status="registered", notify_target="ICU 值班护士", due_minutes=15)
        _seed_event(instance, _event("evt-r-06", mcid="MC-R-06", item_code="1111-1",
                                     item_name="钾", dept_code="LAB", value="6.5"),
                    status="notified", notify_target="检验科值班技师", due_minutes=10)
        _seed_event(instance, _event("evt-r-07", mcid="MC-R-07", item_code="718-7",
                                     item_name="血红蛋白", dept_code="LAB", value="25"),
                    status="registered")
        yield instance
    finally:
        instance.close()


@pytest.fixture()
def double() -> RegistryDouble:
    """登记系统替身（真 socket）。

    预置：

    * ``MC-R-03`` —— 已受理（本地还停在「已确认」）；
    * ``MC-R-04`` —— 字段不一致（结果值 999 ≠ 本地 160）；
    * ``MC-R-05`` —— 拒收；
    * ``MC-R-06`` —— 已受理，但本地这条还没确认（回执先于人工确认）；
    * ``MC-R-07`` —— 与本地完全一致；
    * ``MC-R-99`` —— 本地查无此消息控制号（本地漏记）。
    """
    server = RegistryDouble(TOKEN)
    server.seed(_receipt("MC-R-03", item_code="6690-2", value="320", dept="LAB"))
    server.seed(_receipt("MC-R-04", item_code="2345-7", value="999", dept="EMERG",
                          duty="急诊值班护士"))
    server.seed(_receipt("MC-R-05", item_code="7777-7", value="900", dept="ICU",
                          duty="ICU 值班护士", due_minutes=15, status="rejected"))
    server.seed(_receipt("MC-R-06", item_code="1111-1", value="6.5", dept="LAB"))
    server.seed(_receipt("MC-R-07", item_code="718-7", value="25", dept="LAB"))
    server.seed(_receipt("MC-R-99", item_code="718-7", value="25", dept="LAB"))
    server.start()
    try:
        yield server
    finally:
        server.stop()


def _client(double: RegistryDouble, **kwargs: Any) -> RegistrationSystemClient:
    return RegistrationSystemClient(double.base_url, TOKEN, **kwargs)


def _rows_by_kind(report: Any) -> dict[str, list[Any]]:
    out: dict[str, list[Any]] = {}
    for row in report.rows:
        out.setdefault(row.diff_kind, []).append(row)
    return out


def _snapshot_events(store: LedgerStore) -> list[tuple[Any, ...]]:
    rows = store.query_rows("SELECT * FROM events ORDER BY event_id", [])
    return [tuple(sorted(row.items())) for row in rows]


# ---------------------------------------------------------------------------
# 一、四类差异都能检出
# ---------------------------------------------------------------------------
def test_missing_remote_本地已确认但登记系统无回执(store, double):
    report = Reconciler(store, _client(double)).reconcile()
    rows = _rows_by_kind(report)[DIFF_MISSING_REMOTE]
    ids = {row.message_control_id for row in rows}
    assert "MC-R-01" in ids, "本地已确认却查无回执的，必须被列为 missing_remote"
    row = next(r for r in rows if r.message_control_id == "MC-R-01")
    assert row.action == ACTION_WRITEBACK
    assert row.target == TARGET_REGISTRY
    assert row.suggestion, "每行差异都要带处理建议"


def test_missing_remote_本地已回写远端没有时建议重放(store, double):
    report = Reconciler(store, _client(double)).reconcile()
    row = next(r for r in report.rows if r.message_control_id == "MC-R-02")
    assert row.diff_kind == DIFF_MISSING_REMOTE
    assert row.action == ACTION_REPLAY, "本地已记为已回写而远端没有 → 用同一幂等键重放"
    assert row.target == TARGET_REGISTRY


def test_missing_local_登记系统有回执本地查无(store, double):
    report = Reconciler(store, _client(double)).reconcile()
    rows = _rows_by_kind(report)[DIFF_MISSING_LOCAL]
    assert [r.message_control_id for r in rows] == ["MC-R-99"]
    assert rows[0].action == ACTION_MANUAL
    assert "漏记" in rows[0].suggestion


def test_status_mismatch_远端已受理本地还停在已确认(store, double):
    report = Reconciler(store, _client(double)).reconcile()
    row = next(
        r for r in _rows_by_kind(report)[DIFF_STATUS_MISMATCH]
        if r.message_control_id == "MC-R-03"
    )
    assert row.local_status == "acknowledged"
    assert row.remote_status == "accepted"
    assert row.action == ACTION_WRITEBACK
    assert row.target == TARGET_LOCAL, "远端已受理 → 补齐的是本地台账这一侧"


def test_status_mismatch_远端拒收而本地说已回写(store, double):
    report = Reconciler(store, _client(double)).reconcile()
    row = next(
        r for r in _rows_by_kind(report)[DIFF_STATUS_MISMATCH]
        if r.message_control_id == "MC-R-05"
    )
    assert row.remote_status == "rejected"
    assert row.action == ACTION_MANUAL, "两边说法冲突不能自动判定 → 转人工"


def test_status_mismatch_回执先于人工确认出现要转人工(store, double):
    report = Reconciler(store, _client(double)).reconcile()
    row = next(
        r for r in _rows_by_kind(report)[DIFF_STATUS_MISMATCH]
        if r.message_control_id == "MC-R-06"
    )
    assert row.local_status == "notified"
    assert row.action == ACTION_MANUAL
    assert "人工确认" in row.suggestion


def test_field_mismatch_字段不一致建议重放(store, double):
    report = Reconciler(store, _client(double)).reconcile()
    rows = _rows_by_kind(report)[DIFF_FIELD_MISMATCH]
    row = next(r for r in rows if r.message_control_id == "MC-R-04")
    assert row.action == ACTION_REPLAY
    assert "结果值" in row.mismatched_fields
    assert row.target == TARGET_REGISTRY


def test_两边一致时不产生差异行(store, double):
    report = Reconciler(store, _client(double)).reconcile()
    assert "MC-R-07" not in {row.message_control_id for row in report.rows}


# ---------------------------------------------------------------------------
# 二、差异不自动抹平
# ---------------------------------------------------------------------------
def test_只读对账不改任何本地状态(store, double):
    before = _snapshot_events(store)
    audit_before = len(store.audit_entries())
    Reconciler(store, _client(double)).reconcile()
    assert _snapshot_events(store) == before, "只读对账不得改写台账任何一行"
    assert len(store.audit_entries()) == audit_before, "只读对账不写审计"


def test_只读对账不发任何写请求(store, double):
    Reconciler(store, _client(double)).reconcile()
    assert not [c for c in double.calls if c[0] == "POST"], "对账阶段不得回写登记系统"
    assert double.posted == []


def test_清单不自动抹平_差异仍在(store, double):
    reconciler = Reconciler(store, _client(double))
    first = reconciler.reconcile()
    second = reconciler.reconcile()
    assert [r.message_control_id for r in first.rows] == [
        r.message_control_id for r in second.rows
    ]


def test_显式补写才动本地状态与远端(store, double):
    reconciler = Reconciler(store, _client(double))
    report = reconciler.reconcile()
    outcomes = reconciler.resolve(report, actor="检验科组长", kinds=(DIFF_MISSING_REMOTE,))
    assert outcomes, "应当有 missing_remote 可处理"
    assert all(o.ok for o in outcomes)
    assert double.posted, "补写必须真的发一次 POST 到登记系统"
    posted = double.posted[0]
    assert posted["message_control_id"] == "MC-R-01", "回写必须带幂等键"
    assert store.get_status("evt-r-01") == "registered"
    actions = [row["action"] for row in store.audit_entries()]
    assert "reconcile_writeback_succeeded" in actions


def test_重放不覆盖原来的回写时刻(store, double):
    """已经记着「已回写」的事件，重放一次不该改掉**第一次**写进去的时刻。"""
    before = store.get_event("evt-r-02")
    original = str(before.get("registered_at") or "")
    assert original, "样本事件应已带回写时刻"
    reconciler = Reconciler(store, _client(double))
    report = reconciler.reconcile()
    reconciler.resolve(report, actor="检验科组长", kinds=(DIFF_MISSING_REMOTE,))
    after = store.get_event("evt-r-02")
    assert str(after.get("registered_at") or "") == original
    assert after["status"] == "registered"


def test_重放以本地台账字段为准(store, double):
    reconciler = Reconciler(store, _client(double))
    report = reconciler.reconcile()
    reconciler.resolve(report, actor="检验科组长", kinds=(DIFF_FIELD_MISMATCH,))
    posted = double.posted[0]
    assert posted["message_control_id"] == "MC-R-04"
    assert posted["value"] == "160", "重放必须用本地的结果值，不是远端那份"
    assert posted["threshold_row"].startswith("示例危急值阈值表.xlsx")


def test_补齐本地状态只动本地不动远端(store, double):
    reconciler = Reconciler(store, _client(double))
    report = reconciler.reconcile()
    reconciler.resolve(report, actor="检验科组长", kinds=(DIFF_STATUS_MISMATCH,))
    assert store.get_status("evt-r-03") == "registered"
    assert not double.posted, "补齐本地状态不需要回写远端"


def test_标记人工处理只写审计不改状态(store, double):
    reconciler = Reconciler(store, _client(double))
    report = reconciler.reconcile()
    before = _snapshot_events(store)
    outcomes = reconciler.resolve(report, actor="检验科组长", kinds=(DIFF_MISSING_LOCAL,))
    assert outcomes and all(o.ok for o in outcomes)
    assert _snapshot_events(store) == before, "转人工处理不得改写任何状态"
    assert not double.posted
    actions = [row["action"] for row in store.audit_entries()]
    assert "reconcile_manual_review" in actions


def test_回写失败时本地状态不变且留审计(store, double):
    # 读得到、写不通：对账阶段正常，补写阶段对端 500。
    double.post_status_code = 500
    reconciler = Reconciler(store, _client(double))
    report = reconciler.reconcile()
    outcomes = reconciler.resolve(report, actor="检验科组长", kinds=(DIFF_MISSING_REMOTE,))
    assert outcomes and not any(o.ok for o in outcomes)
    assert store.get_status("evt-r-01") == "acknowledged", "写失败不能把本地改成已回写"
    actions = [row["action"] for row in store.audit_entries()]
    assert "reconcile_writeback_failed" in actions


# ---------------------------------------------------------------------------
# 三、清单可复算
# ---------------------------------------------------------------------------
def test_两次对账的清单逐字节相同(store, double, tmp_path):
    reconciler = Reconciler(store, _client(double))
    first = reconciler.reconcile()
    second = Reconciler(store, _client(double)).reconcile()
    p1 = first.write_csv(tmp_path / "a.csv")
    p2 = second.write_csv(tmp_path / "b.csv")
    assert p1.read_bytes() == p2.read_bytes(), "同一输入必须得到逐字节相同的清单"


def test_清单每行都带处理建议(store, double, tmp_path):
    report = Reconciler(store, _client(double)).reconcile()
    path = report.write_csv(tmp_path / "diff.csv")
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    assert len(lines) == len(report.rows) + 1, "清单行数 = 差异条数 + 表头"
    for row in report.rows:
        assert row.suggestion.strip(), f"{row.message_control_id} 缺处理建议"
    body = path.read_text(encoding="utf-8-sig")
    assert "处理建议" in body
    for kind in ("missing_local", "missing_remote", "status_mismatch", "field_mismatch"):
        assert kind in body


def test_清单带BOM且能被Excel直开(store, double, tmp_path):
    report = Reconciler(store, _client(double)).reconcile()
    path = report.write_csv(tmp_path / "diff.csv")
    assert path.read_bytes()[:3] == b"\xef\xbb\xbf"


# ---------------------------------------------------------------------------
# 四、凭据与错误分级
# ---------------------------------------------------------------------------
def test_缺凭据位时在发请求之前就报错(monkeypatch):
    from critval.config import Config

    monkeypatch.delenv("CRITVAL_BASE_URL", raising=False)
    monkeypatch.delenv("CRITVAL_TOKEN", raising=False)
    Config.init_env()
    with pytest.raises(RegistrationError) as excinfo:
        RegistrationSystemClient.from_config()
    assert "CRITVAL_BASE_URL" in str(excinfo.value)


def test_缺Token时报错并点名凭据位(monkeypatch):
    from critval.config import Config

    monkeypatch.setenv("CRITVAL_BASE_URL", "http://10.0.0.8:8080")
    monkeypatch.delenv("CRITVAL_TOKEN", raising=False)
    Config.init_env()
    with pytest.raises(RegistrationError) as excinfo:
        RegistrationSystemClient.from_config()
    assert "CRITVAL_TOKEN" in str(excinfo.value)


def test_4xx不可重试而5xx可重试(double):
    double.status_code = 404
    result = _client(double).list_receipts()
    assert not result.ok and not result.retryable
    double.status_code = 503
    result = _client(double).list_receipts()
    assert not result.ok and result.retryable


def test_对端整体不可达时报错而不出差异清单(store, double):
    """查不到 ≠ 没有。

    对端把请求全拒（鉴权不过 / 服务不可用）时，**不能**出一份「本地已确认
    但登记系统查无回执」的清单 —— 那是凭空造的差异，值班人照着补写一遍才
    会发现对面根本连不上。
    """
    before = _snapshot_events(store)
    client = RegistrationSystemClient(double.base_url, "wrong-token-000000")
    with pytest.raises(ReconcileError) as excinfo:
        Reconciler(store, client).reconcile()
    assert "全部失败" in str(excinfo.value)
    assert _snapshot_events(store) == before


def test_单条查不到时不判_missing_remote(store):
    """部分查询失败 → 那条的远端状态**未判定**，不进差异清单。"""
    server = RegistryDouble(TOKEN, supports_list=False)
    server.seed(_receipt("MC-R-03", item_code="6690-2", value="320", dept="LAB"))
    server.fault_keys.add("MC-R-01")
    server.start()
    try:
        report = Reconciler(store, _client(server)).reconcile()
        ids = {row.message_control_id for row in report.rows}
        assert "MC-R-01" not in ids, "查不到的键不得被判成「登记系统没有」"
        assert "MC-R-03" in ids, "查得到的键照常比对"
        assert any("未判定" in note for note in report.notes)
    finally:
        server.stop()


def test_对端不支持列表查询时降级并写明(store):
    server = RegistryDouble(TOKEN, supports_list=False)
    server.seed(_receipt("MC-R-03", item_code="6690-2", value="320", dept="LAB"))
    server.start()
    try:
        report = Reconciler(store, _client(server)).reconcile()
        assert report.listing_supported is False
        assert any("降级" in note for note in report.notes)
        assert any("missing_local" in note or "未检出" in note for note in report.notes)
    finally:
        server.stop()


def test_回写载荷字段按规范且只取白名单(store, double):
    row = store.get_event("evt-r-01")
    payload = build_receipt_payload(row)
    assert set(payload) == set(RECEIPT_FIELDS), "载荷字段必须与规范一致"
    assert payload["message_control_id"] == "MC-R-01"
    assert payload["notified_dept"] == "LAB"
    assert payload["due_minutes"] == 10
    assert "patient_ref" not in payload, "患者标识不进回写载荷"


# ---------------------------------------------------------------------------
# 五、真 socket 端到端（CLI）
# ---------------------------------------------------------------------------
def _run_cli(args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess:
    full_env = dict(os.environ)
    full_env.update(env)
    full_env["PYTHONPATH"] = str(ROOT)
    return subprocess.run(
        [sys.executable, "-m", "critval.cli", *args],
        cwd=str(ROOT),
        env=full_env,
        capture_output=True,
        text=True,
    )


@pytest.fixture()
def db_file(store: LedgerStore, tmp_path: Path) -> Path:
    """把内存台账落成文件（CLI 子进程要能打开它）。"""
    path = tmp_path / "reconcile.db"
    store.close()
    return path


def test_cli_reconcile_只读出清单且不改状态(tmp_path, double):
    db = tmp_path / "cli.db"
    with LedgerStore(db) as instance:
        _seed_event(instance, _event("evt-c-01", mcid="MC-C-01", item_code="718-7",
                                     item_name="血红蛋白", dept_code="LAB", value="25"),
                    status="acknowledged")
    out = tmp_path / "diff.csv"
    result = _run_cli(
        ["reconcile", "--db", str(db), "--out", str(out)],
        {"CRITVAL_BASE_URL": double.base_url, "CRITVAL_TOKEN": TOKEN},
    )
    assert result.returncode == 0, result.stderr
    assert out.exists(), "差异清单要落盘"
    assert "missing_remote" in out.read_text(encoding="utf-8-sig")
    with LedgerStore(db) as instance:
        assert instance.get_status("evt-c-01") == "acknowledged", "只读对账不得改状态"
    assert not [c for c in double.calls if c[0] == "POST"]


def test_cli_reconcile_加resolve才动状态(tmp_path, double):
    db = tmp_path / "cli2.db"
    with LedgerStore(db) as instance:
        _seed_event(instance, _event("evt-c-02", mcid="MC-C-01", item_code="718-7",
                                     item_name="血红蛋白", dept_code="LAB", value="25"),
                    status="acknowledged")
    result = _run_cli(
        [
            "reconcile", "--db", str(db), "--out", str(tmp_path / "d.csv"),
            "--resolve", "--actor", "检验科组长", "--only", "missing_remote",
        ],
        {"CRITVAL_BASE_URL": double.base_url, "CRITVAL_TOKEN": TOKEN},
    )
    assert result.returncode == 0, result.stderr
    assert "显式处理结果" in result.stdout
    with LedgerStore(db) as instance:
        assert instance.get_status("evt-c-02") == "registered"


def test_cli_reconcile_没有actor时报参数错(tmp_path, double):
    db = tmp_path / "cli3.db"
    with LedgerStore(db) as instance:
        _seed_event(instance, _event("evt-c-03", mcid="MC-C-01", item_code="718-7",
                                     item_name="血红蛋白", dept_code="LAB", value="25"),
                    status="acknowledged")
    result = _run_cli(
        ["reconcile", "--db", str(db), "--resolve"],
        {"CRITVAL_BASE_URL": double.base_url, "CRITVAL_TOKEN": TOKEN},
    )
    assert result.returncode == 2, result.stderr
    assert "--actor" in result.stderr


def test_cli_reconcile_json输出可解析(tmp_path, double):
    db = tmp_path / "cli4.db"
    with LedgerStore(db) as instance:
        _seed_event(instance, _event("evt-c-04", mcid="MC-C-01", item_code="718-7",
                                     item_name="血红蛋白", dept_code="LAB", value="25"),
                    status="acknowledged")
    result = _run_cli(
        ["reconcile", "--db", str(db), "--out", str(tmp_path / "d.csv"), "--json"],
        {"CRITVAL_BASE_URL": double.base_url, "CRITVAL_TOKEN": TOKEN},
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["counts"]["missing_remote"] == 1
    assert payload["csv_path"].endswith("d.csv")
    assert payload["rows"][0]["suggestion"]
