"""并发写入、幂等竞争与 SQLite 写竞争的单测。

守护进程不是单线程的：入站推送端点、到期复查、值班台页面各占一条线，
运维 CLI 还会**另开一个进程**读同一个库。这份测试盯住三件事：

* **同一幂等键只落一条** —— 线程内、跨进程都成立；
* **重复到达不灌大审计流水** —— 流水记「发生过」，次数由幂等键命中计数承载；
* **写竞争不损坏库** —— WAL + busy_timeout + 唯一索引三件套真的开着，
  库完整性检查（``PRAGMA integrity_check``）始终通过。

跨进程用例走 ``scripts/ledger_probe.py``（另一个真进程往同一个库投同一帧），
不是线程里的假象。
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from critval.console.inbound import LedgerInboundProcessor
from critval.hl7.normalize import normalize_events, parse_oru_r01
from critval.ledger.idempotency import build_idempotency_key, event_id_for_key
from critval.ledger.store import LedgerStore
from critval.models import CriticalEvent

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
PROBE = PROJECT_ROOT / "scripts" / "ledger_probe.py"

THREADS = 8


def sample_event() -> CriticalEvent:
    """一条脱敏的正常事件（并发用例共用同一条 = 同一幂等键）。"""
    message = parse_oru_r01((FIXTURES / "脱敏样例ER7_正常.hl7").read_bytes())
    events, _unparsed = normalize_events(message)
    return events[0]


def probe_event(index: int) -> CriticalEvent:
    """给跨进程用例造一条事件（按 index 区分幂等键）。"""
    observed_at = datetime(2026, 1, 1, 10, 0, index)
    key = build_idempotency_key("MCID-CONC", "718-7", observed_at)
    return CriticalEvent(
        event_id=event_id_for_key(key),
        message_control_id="MCID-CONC",
        specimen_no="SP-CONC",
        item_code="718-7",
        item_name="血红蛋白",
        value=Decimal("25"),
        unit="g/dL",
        observed_at=observed_at,
        dept_code="检验科",
    )


# ----------------------------------------------------------------------
# 线程内并发
# ----------------------------------------------------------------------
def test_多线程写同一幂等键只落一条():
    """8 条线程同时写同一条事件：台账 1 行，恰好一路拿到 True。"""
    event = sample_event()
    results: list[bool] = []
    lock = threading.Lock()

    with LedgerStore(":memory:") as store:

        def worker() -> None:
            recorded = store.record_event(event)
            with lock:
                results.append(recorded)

        threads = [threading.Thread(target=worker) for _ in range(THREADS)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert store.count_events() == 1
        assert results.count(True) == 1
        assert results.count(False) == THREADS - 1
        # 命中计数记的是「这条结果被投了几遍」，不是审计条数。
        assert store.idempotency_hits(build_idempotency_key(
            event.message_control_id, event.item_code, event.observed_at
        )) == THREADS


def test_多线程重复到达审计只记一次():
    """并发重复到达：台账 1 行，``event_duplicate`` 审计只有 1 条。"""
    event = sample_event()
    outcomes: list[str] = []
    lock = threading.Lock()

    with LedgerStore(":memory:") as store:
        processor = LedgerInboundProcessor(store)

        def worker() -> None:
            outcome = processor(event)
            with lock:
                outcomes.append(outcome.status)

        threads = [threading.Thread(target=worker) for _ in range(THREADS)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert store.count_events() == 1
        assert outcomes.count("duplicate") == THREADS - 1
        duplicate_audits = [
            row for row in store.audit_entries() if row["action"] == "event_duplicate"
        ]
        assert len(duplicate_audits) == 1


def test_多线程写不同事件各落一条():
    """不同幂等键互不干扰：8 条线程各写各的，8 条都在。"""
    with LedgerStore(":memory:") as store:
        errors: list[Exception] = []

        def worker(index: int) -> None:
            try:
                store.record_event(probe_event(index))
            except Exception as exc:  # pragma: no cover - 失败要看得见
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(THREADS)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert not errors
        assert store.count_events() == THREADS
        ok, text = store.integrity_check()
        assert ok, text


# ----------------------------------------------------------------------
# 跨进程并发
# ----------------------------------------------------------------------
def run_probe(db_path: Path, index: int) -> dict:
    """起一个真进程往库里投一条事件（返回它的结论）。"""
    observed_at = datetime(2026, 1, 1, 10, 0, index).isoformat()
    result = subprocess.run(
        [
            sys.executable,
            str(PROBE),
            "--db",
            str(db_path),
            "--mcid",
            "MCID-CONC",
            "--item",
            "718-7",
            "--value",
            "25",
            "--unit",
            "g/dL",
            "--observed-at",
            observed_at,
            "--json",
        ],
        capture_output=True,
        text=True,
        cwd=str(PROJECT_ROOT),
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_跨进程写同一幂等键只落一条(tmp_path):
    """4 个真进程同时投同一帧：台账 1 行，恰好一个进程拿到「新事件」。"""
    db_path = tmp_path / "concurrency.db"
    results = [run_probe(db_path, 0) for _ in range(4)]

    assert sum(1 for item in results if item["recorded"]) == 1
    assert sum(1 for item in results if item["duplicate"]) == 3
    assert {item["event_id"] for item in results} == {results[0]["event_id"]}

    with LedgerStore(db_path) as store:
        assert store.count_events() == 1
        ok, text = store.integrity_check()
        assert ok, text


def test_跨进程并发写不同事件全部落库且库不损坏(tmp_path):
    """4 个真进程各写各的：都在，库没坏。"""
    db_path = tmp_path / "concurrency_many.db"
    results = [run_probe(db_path, index) for index in range(4)]

    assert all(item["recorded"] for item in results)
    with LedgerStore(db_path) as store:
        assert store.count_events() == 4
        ok, text = store.integrity_check()
        assert ok, text


# ----------------------------------------------------------------------
# 并发加固本身
# ----------------------------------------------------------------------
def test_并发加固三件套在位(tmp_path):
    """WAL / busy_timeout / 幂等键唯一索引 —— 少一件并发就架不住。"""
    db_path = tmp_path / "hardened.db"
    with LedgerStore(db_path) as store:
        pragmas = store.pragmas()
        assert pragmas["journal_mode"].lower() == "wal"
        assert pragmas["busy_timeout"] > 0
        assert store.idempotency_index_present() is True
        ok, text = store.integrity_check()
        assert ok, text


def test_幂等键唯一约束在库里真的挡得住重复插入(tmp_path):
    """绕过应用代码直接插两条同键 —— 库自己挡（跨进程去重权威）。"""
    import sqlite3

    db_path = tmp_path / "unique_guard.db"
    with LedgerStore(db_path) as store:
        store.record_event(sample_event())
        row = store.get_event(sample_event().event_id)

    conn = sqlite3.connect(str(db_path))
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO events (event_id, idempotency_key, created_at)
                VALUES (?, ?, ?)
                """,
                ("evt-手工插的第二条", row["idempotency_key"], "2026-01-01T00:00:00"),
            )
    finally:
        conn.close()


def test_嵌套写事务复用外层事务不报错():
    """内层再开事务不再 BEGIN（否则 sqlite3 报「事务里不能开事务」）。"""
    with LedgerStore(":memory:") as store:
        event = sample_event()
        with store._transaction():
            store.record_event(event)
            assert store.count_events() == 1
        assert store.count_events() == 1


def test_写事务中途异常整体回滚():
    """事务里抛错 → 这一步全回滚，不留半截状态。"""
    with LedgerStore(":memory:") as store:
        event = sample_event()
        with pytest.raises(RuntimeError):
            with store._transaction():
                store.record_event(event)
                raise RuntimeError("处理中途炸了")
        assert store.count_events() == 0
