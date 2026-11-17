"""审计链完整性与四问断言（任务 id 40）。

**为什么要有这一层**：审计表早就 append-only 了 —— 库级触发器
``audit_no_update`` / ``audit_no_delete`` 直接 ``RAISE(ABORT)``。但
append-only **不等于**不可篡改：触发器只拦得住「还握着库连接的人」。
拿到库文件的人先 ``DROP TRIGGER`` 再改一条、或者把中间那条删掉，
触发器一点忙都帮不上。

链式哈希把这三类改动照出来：

* **内容改过** —— 重算的内容哈希与存下来的对不上；
* **链哈希改过** —— 用行内两个哈希重算链哈希对不上；
* **中间少了一条** —— 流水号不连续，且后一条的 ``prev_hash`` 接不上。

并且报出**首个断链位置** —— 复核时先看那里，不用从头翻。

**四问**：按哪条依据 / 通知了谁 / 几分钟内 / 谁确认的 —— 必须从**已落库
的事实**里答得出来；答不上来要说清「没记录」，不拿空值糊弄。
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from critval.ledger.audit import (
    AUDIT_PAYLOAD_FIELDS,
    CHAIN_BREAK_CHAIN,
    CHAIN_BREAK_GAP,
    CHAIN_BREAK_PAYLOAD,
    CHAIN_BREAK_PREV,
    GENESIS_HASH,
    AuditLog,
    audit_chain_hash,
    audit_payload_hash,
    canonical_audit_payload,
    verify_audit_chain,
)
from critval.ledger.store import LedgerStore
from critval.models import Citation, CriticalEvent


PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def store() -> LedgerStore:
    instance = LedgerStore(":memory:")
    try:
        yield instance
    finally:
        instance.close()


def _event(event_id: str = "evt-ac-0001") -> CriticalEvent:
    return CriticalEvent(
        event_id=event_id,
        message_control_id="MC-AC-0001",
        specimen_no="SP-0001",
        item_code="718-7",
        item_name="血红蛋白",
        value=Decimal("26.5"),
        unit="g/dL",
        observed_at=datetime(2026, 1, 2, 8, 20, tzinfo=timezone.utc),
        dept_code="LAB",
    )


def _seed_audit(store: LedgerStore, count: int = 5) -> None:
    for index in range(1, count + 1):
        store.append_audit(
            "event_received",
            event_id=f"evt-ac-{index:04d}",
            specimen_no=f"SP-{index:04d}",
            item_code="718-7",
            detail=f"收到第 {index} 条",
        )


def _tamper(db_path: Path, sql: str, *params: object) -> None:
    """绕过应用层直接改库 —— 模拟「拿到库文件的人」。

    先摘掉拒改 / 拒删触发器：摘得掉，正说明 **append-only 靠触发器
    是不够的**，链哈希才最后的兜底。
    """
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("DROP TRIGGER IF EXISTS audit_no_update")
        conn.execute("DROP TRIGGER IF EXISTS audit_no_delete")
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 1. 链式哈希的基本性质
# ---------------------------------------------------------------------------


def test_每条审计行都带链哈希(store: LedgerStore):
    _seed_audit(store, 3)
    rows = store.audit_entries()
    assert len(rows) == 3
    for row in rows:
        assert row["prev_hash"], "缺 prev_hash：这条没接上链"
        assert row["payload_hash"], "缺 payload_hash：这条内容没被哈希"
        assert row["chain_hash"], "缺 chain_hash：这条没封链"


def test_第一条的prev_hash是创世哈希(store: LedgerStore):
    _seed_audit(store, 3)
    rows = store.audit_entries()
    assert rows[0]["prev_hash"] == GENESIS_HASH


def test_后一条的prev_hash正是前一条的链哈希(store: LedgerStore):
    _seed_audit(store, 4)
    rows = store.audit_entries()
    for previous, current in zip(rows, rows[1:]):
        assert current["prev_hash"] == previous["chain_hash"]


def test_链哈希等于前链与本内容哈希的合流(store: LedgerStore):
    _seed_audit(store, 3)
    for row in store.audit_entries():
        expected = audit_chain_hash(row["prev_hash"], row["payload_hash"])
        assert row["chain_hash"] == expected


def test_内容哈希可脱离上下文独立校验(store: LedgerStore):
    """**独立校验**：单拿一行就能重算它的内容哈希，不必先找到上一行。"""
    _seed_audit(store, 3)
    for row in store.audit_entries():
        assert audit_payload_hash(row) == row["payload_hash"]


def test_规范文本消歧义_字段挪位不能凑出同一个哈希(store: LedgerStore):
    """长度前缀是为了消歧义：``AB``+``C`` 与 ``A``+``BC`` 不能撞成一样。"""
    left = canonical_audit_payload({"action": "AB", "detail": "C"})
    right = canonical_audit_payload({"action": "A", "detail": "BC"})
    assert left != right
    assert audit_payload_hash({"action": "AB", "detail": "C"}) != audit_payload_hash(
        {"action": "A", "detail": "BC"}
    )


def test_内容哈希覆盖全部落库字段(store: LedgerStore):
    """少一个字段就等于给篡改留了后门。"""
    assert set(AUDIT_PAYLOAD_FIELDS) == {
        "seq",
        "at",
        "action",
        "event_id",
        "actor",
        "dept_code",
        "specimen_no",
        "item_code",
        "message_control_id",
        "detail",
    }


# ---------------------------------------------------------------------------
# 2. 全链校验
# ---------------------------------------------------------------------------


def test_完整链校验通过(store: LedgerStore):
    _seed_audit(store, 6)
    report = AuditLog(store).verify_chain()
    assert report.ok is True
    assert report.breaks == ()
    assert report.total == 6
    assert report.verified == 6
    assert report.first_break is None


def test_空库校验通过(store: LedgerStore):
    report = AuditLog(store).verify_chain()
    assert report.ok is True
    assert report.total == 0


def test_篡改一条内容后校验失败并定位到该条(tmp_path: Path):
    db_path = tmp_path / "tamper.db"
    with LedgerStore(db_path) as store:
        _seed_audit(store, 5)

    # 绕过应用层改第 3 条的说明文字 —— 这正是触发器拦不住的那种改动
    _tamper(db_path, "UPDATE audit SET detail = ? WHERE seq = ?", "被改过的说明", 3)

    with LedgerStore(db_path) as store:
        report = AuditLog(store).verify_chain()
    assert report.ok is False
    first = report.first_break
    assert first is not None
    assert first.seq == 3, "必须报出首个断链位置"
    assert first.kind == CHAIN_BREAK_PAYLOAD
    assert first.expected != first.found


def test_篡改链哈希能被发现(tmp_path: Path):
    db_path = tmp_path / "chain.db"
    with LedgerStore(db_path) as store:
        _seed_audit(store, 5)

    _tamper(db_path, "UPDATE audit SET chain_hash = ? WHERE seq = ?", "0" * 64, 4)

    with LedgerStore(db_path) as store:
        report = AuditLog(store).verify_chain()
    assert report.ok is False
    kinds = {item.kind for item in report.breaks}
    assert CHAIN_BREAK_CHAIN in kinds


def test_改掉上一条的链哈希会在下一条暴露(tmp_path: Path):
    """链哈希是**接龙**的：动一条，从下一条起全部对不上。"""
    db_path = tmp_path / "relink.db"
    with LedgerStore(db_path) as store:
        _seed_audit(store, 5)

    _tamper(db_path, "UPDATE audit SET chain_hash = ? WHERE seq = ?", "0" * 64, 2)

    with LedgerStore(db_path) as store:
        report = AuditLog(store).verify_chain()
    assert report.ok is False
    assert any(item.kind == CHAIN_BREAK_PREV for item in report.breaks)
    assert any(item.seq == 3 for item in report.breaks), "第 3 条的 prev_hash 接不上第 2 条"


def test_中间删掉一条能被发现(tmp_path: Path):
    db_path = tmp_path / "deleted.db"
    with LedgerStore(db_path) as store:
        _seed_audit(store, 5)

    _tamper(db_path, "DELETE FROM audit WHERE seq = ?", 2)

    with LedgerStore(db_path) as store:
        report = AuditLog(store).verify_chain()
    assert report.ok is False
    assert any(item.kind == CHAIN_BREAK_GAP for item in report.breaks)


def test_补链之前的旧记录会被补上链并可校验(tmp_path: Path):
    """老库补链：历史行本来没有链哈希，补上后整条链也能独立校验。

    造一个「链式哈希之前」的库：审计表**没有**那三列 —— 迁移时会被
    补上列，历史行随即补链。
    """
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE audit (
                seq         INTEGER PRIMARY KEY AUTOINCREMENT,
                at          TEXT NOT NULL,
                action      TEXT NOT NULL,
                event_id    TEXT NOT NULL DEFAULT '',
                actor       TEXT NOT NULL DEFAULT '',
                dept_code   TEXT NOT NULL DEFAULT '',
                specimen_no TEXT NOT NULL DEFAULT '',
                item_code   TEXT NOT NULL DEFAULT '',
                message_control_id TEXT NOT NULL DEFAULT '',
                detail      TEXT NOT NULL DEFAULT ''
            )
            """
        )
        for index in range(1, 4):
            conn.execute(
                """
                INSERT INTO audit (
                    at, action, event_id, actor, dept_code, specimen_no, item_code,
                    message_control_id, detail
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"2026-01-02T08:0{index}:00+00:00",
                    "event_received",
                    f"evt-old-{index:04d}",
                    "",
                    "LAB",
                    f"SP-{index:04d}",
                    "718-7",
                    f"MC-OLD-{index:04d}",
                    f"老库第 {index} 条",
                ),
            )
        conn.commit()
    finally:
        conn.close()

    with LedgerStore(db_path) as store:
        report = AuditLog(store).verify_chain()
    assert report.ok is True, "老库补完链之后应当能校验通过"
    with LedgerStore(db_path) as store:
        rows = store.audit_entries()
    assert len(rows) == 3
    for row in rows:
        assert row["chain_hash"], "历史行没补上链哈希"
    assert rows[0]["prev_hash"] == GENESIS_HASH
    assert rows[1]["prev_hash"] == rows[0]["chain_hash"]


def test_列已存在时空哈希不会被悄悄重锚(tmp_path: Path):
    """空哈希在「列已存在」的库里 = **有人动过**，不能悄悄补回去。

    悄悄重锚等于把篡改的痕迹抹平，自检命令就永远报不出问题。
    """
    db_path = tmp_path / "unanchored.db"
    with LedgerStore(db_path) as store:
        _seed_audit(store, 2)
    _tamper(db_path, "UPDATE audit SET chain_hash = '' WHERE seq = ?", 2)

    with LedgerStore(db_path) as store:
        report = AuditLog(store).verify_chain()
    assert report.ok is False
    assert report.first_break.seq == 2
    assert report.first_break.kind == "unanchored"


def test_未上链的记录不算通过(tmp_path: Path):
    """挂着没上链的记录就说「通过」，等于给自己开后门。"""
    db_path = tmp_path / "unanchored2.db"
    with LedgerStore(db_path) as store:
        _seed_audit(store, 2)
    _tamper(db_path, "UPDATE audit SET chain_hash = '' WHERE seq = ?", 2)

    with LedgerStore(db_path) as store:
        report = AuditLog(store).verify_chain()
    assert report.ok is False
    assert report.first_break.seq == 2


def test_校验函数可脱离数据库单独使用():
    """纯函数：拿一串行就能校验，不必开库。"""
    rows = [
        {
            "seq": 1,
            "at": "2026-01-02T08:00:00+00:00",
            "action": "event_received",
            "event_id": "e1",
            "actor": "",
            "dept_code": "LAB",
            "specimen_no": "SP-1",
            "item_code": "718-7",
            "message_control_id": "MC-1",
            "detail": "收到",
        }
    ]
    payload_hash = audit_payload_hash(rows[0])
    chain_hash = audit_chain_hash(GENESIS_HASH, payload_hash)
    rows[0].update(
        {"payload_hash": payload_hash, "chain_hash": chain_hash, "prev_hash": GENESIS_HASH}
    )
    report = verify_audit_chain(rows)
    assert report.ok is True
    assert report.total == 1


# ---------------------------------------------------------------------------
# 3. 四问可答（按哪条依据 / 通知了谁 / 几分钟内 / 谁确认的）
# ---------------------------------------------------------------------------


def _confirmed_case(store: LedgerStore) -> str:
    """落一条**已确认**的完整处置链路：事件 → 引用 → 推送 → 确认。"""
    event_id = "evt-ac-0001"
    store.record_event(_event(event_id))
    store.set_status(event_id, "judged")
    store.set_status(event_id, "card_built")
    store.record_citation(
        event_id,
        "card-0001",
        Citation(
            source_name="示例危急值阈值表.xlsx",
            source_sha256="2f87902f0be8f176",
            kind="threshold_row",
            line_no=2,
            file_version="示例 v1.0",
        ),
    )
    store.record_citation(
        event_id,
        "card-0001",
        Citation(
            source_name="示例危急值报告制度.docx",
            source_sha256="ef5be6701ca68a67",
            kind="policy_clause",
            paragraph_no=7,
            section_path=("第三章", "上报时限"),
            line_no=19,
            file_version="示例 v1.0",
        ),
    )
    moment = datetime(2026, 1, 2, 8, 30, tzinfo=timezone.utc)
    store.mark_notified(
        event_id,
        notify_target="检验科值班技师",
        due_minutes=10,
        channel="wecom",
        at=moment,
    )
    store.mark_acknowledged(event_id, actor="检验科值班技师", at=moment)
    return event_id


def test_四问都能答上来(store: LedgerStore):
    event_id = _confirmed_case(store)
    answers = AuditLog(store).trace(event_id)

    # 问一：按哪条依据 —— 文件名 + 行号 + 版本
    assert answers.basis, "没答出依据"
    assert "示例危急值阈值表.xlsx" in answers.basis
    assert "第 2 行" in answers.basis
    assert answers.basis_citations
    assert any("第 2 行" in item for item in answers.basis_citations)
    # 制度条款是另一条引用，落在 citations 表里 —— 依据不止阈值表这一行
    locators = {str(row.get("locator", "")) for row in store.citations_of(event_id)}
    assert any("示例危急值报告制度.docx" in item for item in locators)

    # 问二：通知了谁 —— 科室 + 角色
    assert answers.notified_who == "LAB / 检验科值班技师"

    # 问三：几分钟内 —— 制度里的上报时限
    assert answers.due_minutes == 10

    # 问四：谁确认的 + 确认时刻
    assert answers.acknowledged_by == "检验科值班技师"
    assert answers.acknowledged_at == datetime(2026, 1, 2, 8, 30, tzinfo=timezone.utc).isoformat()

    assert set(answers.answered) == {
        "basis",
        "notified_who",
        "due_minutes",
        "acknowledged_by",
    }
    assert answers.missing == ()


def test_四问的答案有审计流水作证(store: LedgerStore):
    """答案不能是现场拼的 —— 流水里得有对应的那几条。"""
    event_id = _confirmed_case(store)
    answers = AuditLog(store).trace(event_id)
    actions = {str(row.get("action", "")) for row in answers.evidence}
    assert "event_acknowledged" in actions
    assert "status_transition" in actions

    # 谁确认的 —— 取得到流水里那条确认记录（角色 + 时刻）
    ack = [row for row in answers.evidence if row["action"] == "event_acknowledged"]
    assert ack and ack[0]["actor"] == "检验科值班技师"
    assert ack[0]["at"] == answers.acknowledged_at

    # 通知了谁 —— 流水里那条「已推送」的迁移带着科室
    pushed = [row for row in answers.evidence if "已推送" in str(row.get("detail", ""))]
    assert pushed, "流水里没有「已推送」那条迁移"
    assert pushed[0]["dept_code"] == "LAB"


def test_四问答不上来时列进missing而不是假装答了(store: LedgerStore):
    store.record_event(_event("evt-ac-0002"))
    answers = AuditLog(store).trace("evt-ac-0002")
    assert answers.acknowledged_by == ""
    assert "acknowledged_by" in answers.missing
    assert "due_minutes" in answers.missing


def test_四问在断链的库里仍能答但链校验不通过(tmp_path: Path):
    """两件事要分得开：**内容答得上** ≠ **链没被动过**。"""
    db_path = tmp_path / "four.db"
    with LedgerStore(db_path) as store:
        event_id = _confirmed_case(store)
        answers = AuditLog(store).trace(event_id)
    assert set(answers.answered) == {
        "basis",
        "notified_who",
        "due_minutes",
        "acknowledged_by",
    }

    _tamper(db_path, "UPDATE audit SET detail = ? WHERE seq = ?", "被改过", 1)
    with LedgerStore(db_path) as store:
        assert AuditLog(store).verify_chain().ok is False
        assert AuditLog(store).trace(event_id).acknowledged_by == "检验科值班技师"


# ---------------------------------------------------------------------------
# 4. CLI 自检命令
# ---------------------------------------------------------------------------


def _run_cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "critval.cli", *args],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
    )


def test_CLI在正常库上exit0(tmp_path: Path):
    db_path = tmp_path / "ok.db"
    with LedgerStore(db_path) as store:
        _seed_audit(store, 3)
    result = _run_cli("audit-verify", "--db", str(db_path))
    assert result.returncode == 0, result.stderr
    assert "审计链校验 · 结论：通过" in result.stdout
    assert "流水条数   : 3" in result.stdout


def test_CLI在断链库上exit非0并报出首个断链位置(tmp_path: Path):
    db_path = tmp_path / "broken.db"
    with LedgerStore(db_path) as store:
        _seed_audit(store, 5)
    _tamper(db_path, "UPDATE audit SET detail = ? WHERE seq = ?", "被改过", 4)

    result = _run_cli("audit-verify", "--db", str(db_path))
    assert result.returncode != 0, "断链必须以非 0 退出码收场"
    assert "发现断链" in result.stdout
    assert "首个断链   : 流水号 4" in result.stdout
    assert "本条内容与内容哈希不符" in result.stdout


def test_CLI的JSON输出可解析且带断链位置(tmp_path: Path):
    db_path = tmp_path / "json.db"
    with LedgerStore(db_path) as store:
        _seed_audit(store, 3)
    _tamper(db_path, "UPDATE audit SET detail = ? WHERE seq = ?", "被改过", 2)

    result = _run_cli("audit-verify", "--db", str(db_path), "--json")
    assert result.returncode != 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["first_break"]["seq"] == 2
    assert payload["total"] == 3


def test_CLI导出CSV时带上链哈希(tmp_path: Path):
    """导出的 CSV 拿到院外也能重算校验 —— 不然导出这一手就把可验证丢了。"""
    db_path = tmp_path / "export.db"
    csv_path = tmp_path / "audit.csv"
    with LedgerStore(db_path) as store:
        _seed_audit(store, 2)
    result = _run_cli("audit-verify", "--db", str(db_path), "--export", str(csv_path))
    assert result.returncode == 0, result.stderr
    assert csv_path.exists()
    text = csv_path.read_text(encoding="utf-8-sig")
    header = text.splitlines()[0]
    for column in ("prev_hash", "payload_hash", "chain_hash"):
        assert column in header


def test_CLI在台账不存在时报用法错误(tmp_path: Path):
    result = _run_cli("audit-verify", "--db", str(tmp_path / "missing.db"))
    assert result.returncode == 2
    assert "台账不存在" in result.stderr
