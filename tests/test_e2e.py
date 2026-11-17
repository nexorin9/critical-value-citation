"""端到端落盘单测：ER7 帧 → 判定 → 处置卡 → SQLite 台账。

盯住三件事：

* **幂等** —— 同一条结果重复到达只落一条，既有事实不被覆盖；
* **出处** —— 每条引用同时带 ``source_sha256`` 与可读行号；
* **不丢、不漏** —— 缺字段的报文落 ``unparsed`` 记录 + 写审计，
  台账里不留患者可识别明细。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from critval.hl7.normalize import normalize_events, parse_oru_r01
from critval.ledger.store import LedgerStore
from critval.ledger.idempotency import idempotency_key_of

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
SAMPLE_FRAMES = PROJECT_ROOT / "samples" / "hl7"


def run_e2e(db_path: Path, frames: Path = SAMPLE_FRAMES, extra: list[str] | None = None) -> subprocess.CompletedProcess:
    """跑一次端到端脚本。"""
    command = [
        sys.executable,
        str(PROJECT_ROOT / "scripts" / "e2e.py"),
        "--db",
        str(db_path),
        "--frames",
        str(frames),
    ]
    command.extend(extra or [])
    return subprocess.run(command, capture_output=True, text=True, cwd=str(PROJECT_ROOT))


def test_建库后表齐():
    with LedgerStore(":memory:") as store:
        names = store.table_names()
    for table in ("events", "threshold_rows", "clauses", "citations", "audit", "idempotency_keys"):
        assert table in names


def test_同一事件重复落只落一条且既有事实不被覆盖():
    message = parse_oru_r01((FIXTURES / "脱敏样例ER7_正常.hl7").read_bytes())
    events, _ = normalize_events(message)
    event = events[0]

    with LedgerStore(":memory:") as store:
        assert store.record_event(event) is True
        assert store.count_events() == 1

        assert store.record_event(event) is False
        assert store.count_events() == 1

        row = store.get_event(event.event_id)
        assert row is not None
        assert row["idempotency_key"] == idempotency_key_of(event)
        assert store.idempotency_hits(idempotency_key_of(event)) == 2


def test_不同消息号的不同事件各落一条():
    first, _ = normalize_events(parse_oru_r01((FIXTURES / "脱敏样例ER7_正常.hl7").read_bytes()))
    second, _ = normalize_events(parse_oru_r01((FIXTURES / "脱敏样例ER7_修正结果.hl7").read_bytes()))
    with LedgerStore(":memory:") as store:
        assert store.record_event(first[0]) is True
        assert store.record_event(second[0]) is True
        assert store.count_events() == 2


def test_引用落库同时带sha256与可读行号(tmp_path):
    db_path = tmp_path / "e2e.db"
    result = run_e2e(db_path)
    assert result.returncode == 0, result.stderr

    with LedgerStore(db_path) as store:
        events = store.list_events()
        assert events
        for event in events:
            citations = store.citations_of(event["event_id"])
            assert citations, "每张处置卡的引用都要落库"
            for citation in citations:
                assert citation["source_sha256"]
                assert citation["line_no"]
                assert citation["locator"]

        threshold_citations = [
            citation
            for event in events
            for citation in store.citations_of(event["event_id"])
            if citation["kind"] == "threshold_row"
        ]
        assert threshold_citations
        # 阈值依据行必须指向真实的阈值表文件与行号
        assert all("阈值表" in c["source_name"] for c in threshold_citations)


def test_缺字段报文落unparsed并写审计(tmp_path):
    db_path = tmp_path / "e2e.db"
    result = run_e2e(db_path)
    assert result.returncode == 0, result.stderr

    with LedgerStore(db_path) as store:
        audits = store.audit_entries()
        unparsed = [a for a in audits if a["action"] == "unparsed_event"]
        assert unparsed, "缺字段的报文不能被静默丢弃"
        assert any("missing_item_code" in a["detail"] for a in unparsed)


def test_连跑两次事件数不增加(tmp_path):
    db_path = tmp_path / "e2e.db"

    first = run_e2e(db_path)
    assert first.returncode == 0, first.stderr
    with LedgerStore(db_path) as store:
        count_after_first = store.count_events()
    assert count_after_first > 0

    second = run_e2e(db_path)
    assert second.returncode == 0, second.stderr
    with LedgerStore(db_path) as store:
        count_after_second = store.count_events()
        duplicates = [a for a in store.audit_entries() if a["action"] == "event_duplicate"]

    assert count_after_second == count_after_first
    assert duplicates, "重复到达要有审计留痕"


def test_端到端输出含出处与幂等说明(tmp_path):
    db_path = tmp_path / "e2e.db"
    first = run_e2e(db_path)
    assert first.returncode == 0, first.stderr
    assert "sha256" in first.stdout
    assert "第 " in first.stdout and " 行" in first.stdout
    assert "幂等跳过 0 条" in first.stdout

    second = run_e2e(db_path)
    assert "幂等跳过" in second.stdout
    assert "新增事件 0 条" in second.stdout


def test_json输出可解析且字段齐(tmp_path):
    db_path = tmp_path / "e2e.db"
    result = run_e2e(db_path, extra=["--json"])
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["event_total"] > 0
    assert payload["cards"]
    card = payload["cards"][0]
    assert card["decision"]
    assert card["citations"]
    assert card["citations"][0]["source_sha256"]


def test_台账里不留患者可识别明细(tmp_path):
    db_path = tmp_path / "e2e.db"
    result = run_e2e(db_path)
    assert result.returncode == 0, result.stderr

    with LedgerStore(db_path) as store:
        for event in store.list_events():
            assert event["patient_ref"].startswith("pt-")
            assert "FIX-PAT" not in event["patient_ref"]

    raw = db_path.read_bytes()
    assert "FIX-PAT-000001".encode("utf-8") not in raw


def test_事件行带齐复核需要的字段(tmp_path):
    db_path = tmp_path / "e2e.db"
    result = run_e2e(db_path)
    assert result.returncode == 0, result.stderr

    with LedgerStore(db_path) as store:
        for event in store.list_events():
            assert event["specimen_no"]
            assert event["item_code"]
            assert event["observed_at"]
            assert event["status"] == "card_built"
            assert event["judgement_decision"]
            assert event["threshold_row"], "判定依据行要落在事件行上"
            assert event["card_id"]


@pytest.mark.parametrize("frame", ["脱敏样例ER7_正常.hl7", "脱敏样例ER7_修正结果.hl7"])
def test_单帧跑得通且出卡(tmp_path, frame):
    db_path = tmp_path / "e2e.db"
    frame_dir = tmp_path / "frames"
    frame_dir.mkdir()
    (frame_dir / frame).write_bytes((FIXTURES / frame).read_bytes())

    result = run_e2e(db_path, frames=frame_dir)
    assert result.returncode == 0, result.stderr
    assert "处置卡" in result.stdout
    assert "出处" in result.stdout
