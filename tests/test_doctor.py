"""台账体检（``doctor``）单测：输出结构完整、异常项决定退出码。

体检的价值在于**能落到行动上**：结构完整（信息齐），异常项分级清楚，
退出码能被运维脚本直接用（0 = 通过，1 = 有必须处理的异常项，2 = 参数错）。
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from critval.ledger.health import LEVEL_ERROR, LEVEL_WARN, run_doctor
from critval.ledger.store import LedgerStore
from critval.models import CriticalEvent

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _event(event_id: str) -> CriticalEvent:
    return CriticalEvent(
        event_id=event_id,
        message_control_id=f"MC-{event_id}",
        specimen_no="SP-0001",
        item_code="718-7",
        item_name="血红蛋白",
        value=Decimal("25"),
        unit="g/L",
        observed_at=datetime(2026, 1, 2, 8, 30, tzinfo=timezone.utc),
        dept_code="检验科",
    )


def run_cli(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "critval.cli", "doctor", *args],
        capture_output=True,
        text=True,
        cwd=str(PROJECT_ROOT),
    )


def test_doctor输出结构完整(tmp_path):
    """体检报告四块都在：完整性 / 并发加固 / 状态分布 / 待办计数。"""
    db_path = tmp_path / "doctor.db"
    with LedgerStore(db_path) as store:
        store.record_event(_event("evt-doc-0001"))

    result = run_cli(["--db", str(db_path)])
    assert result.returncode == 0, result.stderr
    text = result.stdout
    assert "危急值台账体检" in text
    assert "库完整性检查通过" in text
    assert "journal_mode=wal" in text
    assert "busy_timeout=" in text
    assert "状态分布" in text
    assert "待办计数" in text
    assert "配置版本" in text
    assert "规则快照" in text
    assert "自检结论   : 通过" in text


def test_doctor的JSON输出可被解析且字段齐(tmp_path):
    """机器可读输出：字段固定，运维脚本能直接取。"""
    db_path = tmp_path / "doctor_json.db"
    with LedgerStore(db_path) as store:
        store.record_event(_event("evt-doc-0002"))

    result = run_cli(["--db", str(db_path), "--json"])
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    for key in (
        "db_path",
        "generated_at",
        "ok",
        "event_total",
        "status_counts",
        "pending",
        "pragmas",
        "config_version",
        "ruleset_snapshot_id",
        "findings",
        "counts",
    ):
        assert key in payload
    assert payload["ok"] is True
    assert payload["event_total"] == 1


def test_doctor在台账不存在时退出码为参数错(tmp_path):
    """库都没有就别谈体检 —— 退出码 2（参数错）。"""
    result = run_cli(["--db", str(tmp_path / "missing.db")])
    assert result.returncode == 2
    assert "台账不存在" in result.stderr


def test_doctor发现异常项时退出码非零(tmp_path, monkeypatch, capsys):
    """有必须处理的异常项 → 退出码 1（运维脚本据此报警）。"""
    import critval.ledger.health as health_module
    from critval.cli import main

    db_path = tmp_path / "doctor_bad.db"
    with LedgerStore(db_path) as store:
        store.record_event(_event("evt-doc-0003"))

    original = health_module.run_doctor

    def broken(store, **kwargs):
        report = original(store, **kwargs)
        report.findings.append(
            health_module.DoctorFinding(
                LEVEL_ERROR, "injected", "注入的异常项（测试用）"
            )
        )
        return report

    monkeypatch.setattr(health_module, "run_doctor", broken)
    # CLI 在调用时才 import，走的是模块属性 → 补丁生效。
    code = main(["doctor", "--db", str(db_path)])
    out = capsys.readouterr().out
    assert code == 1
    assert "注入的异常项" in out


def test_缺并发加固时体检判异常(tmp_path):
    """busy_timeout 为 0 / 幂等唯一索引不在 → error 级（必须处理）。"""

    class _BrokenStore:
        def __init__(self, real: LedgerStore) -> None:
            self._real = real

        def integrity_check(self):
            return self._real.integrity_check()

        def pragmas(self):
            data = dict(self._real.pragmas())
            data["busy_timeout"] = 0
            return data

        def idempotency_index_present(self) -> bool:
            return False

        def status_counts(self):
            return self._real.status_counts()

        def list_events(self, limit: int = 100):
            return self._real.list_events(limit=limit)

    with LedgerStore(":memory:") as store:
        store.record_event(_event("evt-doc-0004"))
        report = run_doctor(_BrokenStore(store), db_path=":memory:", config_version=1)

    codes = {item.code for item in report.findings}
    assert "busy_timeout_missing" in codes
    assert "idempotency_index_missing" in codes
    assert report.ok is False
    assert report.errors


def test_体检报出到期未回执的待办(tmp_path):
    """过了时限还没回执 → 待办计数里点出来（提醒级，不判失败）。"""
    db_path = tmp_path / "doctor_overdue.db"
    with LedgerStore(db_path) as store:
        event = _event("evt-doc-0005")
        store.record_event(event)
        store.set_status(event.event_id, "judged", detail="已判定")
        store.set_status(event.event_id, "card_built", detail="已出卡")
        store.mark_notified(
            event.event_id,
            channel="test",
            due_minutes=10,
            detail="首次推送",
            at=datetime.now(timezone.utc) - timedelta(minutes=30),
        )
        store.mark_pending_ack(event.event_id, detail="等回执")

        report = run_doctor(store, db_path=str(db_path), config_version=1)

    assert report.pending.get("overdue_ack", 0) == 1
    codes = {item.code for item in report.findings}
    assert "ack_overdue" in codes
    # 提醒级不判失败。
    assert all(item.level != LEVEL_ERROR for item in report.findings)
    assert report.ok is True


def test_未装载规则集时体检提醒(tmp_path):
    """规则集没装载 → warn（判定引擎没有生效版本）。"""
    with LedgerStore(":memory:") as store:
        store.record_event(_event("evt-doc-0006"))
        report = run_doctor(store, db_path=":memory:", config_version=0)

    levels = {item.code: item.level for item in report.findings}
    assert levels["ruleset_missing"] == LEVEL_WARN
    assert levels["config_version_missing"] == LEVEL_WARN
