"""SQLite 台账：事件 / 阈值行 / 制度条款 / 引用 / 通知 / 审计 + 事件幂等键。

**为什么是 SQLite**：零外部中间件 —— 检验科值班机上装个 Python 就能跑，
不要求院方先装 MySQL 或 Redis。库文件路径取 ``CRITVAL_DB_PATH``
（默认 ``./data/critval.db``，``data/`` 已在 .gitignore 内，不进仓库）。

**状态机在库这一层守着**（任务 id 10）：跃迁表落在
``critval/ledger/states.py``，``set_status`` 按表校验 —— 非法跃迁抛错、
终态不可静默回退、每次迁移都写一条 ``status_transition`` 审计。绕过
``set_status`` 直接改库状态的口子只有本模块内部那几个（出卡 / 取代链），
它们自己也走同一套校验。

去重的口径是**幂等键**（``MSH-10 + OBX-3 + OBX-14``，见
``critval/ledger/idempotency.py``）：同键重复到达**不覆盖既有事实**，
只记一次命中次数 —— 已推送过的卡片不会被第二次推送冲掉。

**审计表是 append-only**（任务 id 10）：库级触发器直接拒 UPDATE / DELETE，
绕过 Python 改库同样被拦下。链式哈希与自检命令在任务 id 40 补上。
"""

from __future__ import annotations

import contextlib
import json
import re
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Optional

from critval.judge.supersede import SUPERSEDE_STATUS
from critval.ledger.audit import (
    AUDIT_PAYLOAD_FIELDS,
    GENESIS_HASH,
    audit_chain_hash,
    audit_payload_hash,
)
from critval.ledger.idempotency import IdempotencyWindow, idempotency_key_of
from critval.ledger.states import (
    TERMINAL_STATUSES,
    IllegalTransitionError,
    describe_status,
    validate_transition,
)
from critval.models import Citation, CriticalEvent, DispositionCard, Judgement

__all__ = [
    "LedgerStore",
    "SCHEMA_STATEMENTS",
    "TERMINAL_STATUSES",
    "TerminalStateError",
    "unique_citations",
]


class TerminalStateError(IllegalTransitionError):
    """试图把事件从终态迁走 —— 「终态不可静默回退」。

    是 :class:`~critval.ledger.states.IllegalTransitionError` 的子类：
    「从终态回退」本质上是「非法跃迁」的一种，调用方想统一拦就拦
    ``IllegalTransitionError``，想只拦终态这一种就拦本类。
    """

    def __init__(self, event_id: str, current: str, target: str) -> None:
        self.event_id = event_id
        self.current = current
        self.target = target
        self.reason = "终态事实已定格，不许静默回退"
        RuntimeError.__init__(
            self,
            f"事件 {event_id} 已处于终态 {current}，不能静默回退到 {target}",
        )


#: 从处置卡的「上报时限」要素里抠出分钟数（``10 分钟`` → ``10``）。
_DEADLINE_MINUTES_RE = re.compile(r"(\d+)\s*分钟")


def _deadline_minutes(text: str) -> Optional[int]:
    """从「上报时限」要素文本里解析分钟数；解析不出返回 ``None``。

    制度里写「10 分钟内」就取 10；写「立即」这类没有分钟数的表述取不到
    —— **不猜**，留空让到期复查按「无时限」处理（事件不会被误判为超时）。
    """
    match = _DEADLINE_MINUTES_RE.search(str(text or ""))
    if match is None:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def _is_stale(updated_at: str, stale_after_seconds: int) -> bool:
    """一个 ``in_flight`` 投递轮次是不是已经「陈旧」（可以抢占）。

    进程崩在推送中途时，轮次会永远停在 ``in_flight`` —— 不给抢占窗口，
    这条危急值就再也推不出去了。**时间戳读不出来时按陈旧处理**：
    宁可让人重新推一次（推送有抑制键兜底，不会刷屏），也不能让事件卡死。
    """
    window = max(0, int(stale_after_seconds))
    raw = str(updated_at or "").strip()
    if not raw:
        return True
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError:
        return True
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - moment) > timedelta(seconds=window)


def unique_citations(citations: Iterable[Citation]) -> list[Citation]:
    """同一张卡里同一条出处可能挂在多个要素上（阈值行 + 制度条款各要素），
    落库前按「文件 + 段落 + 行号」去重，避免台账里出现四条一样的出处。"""
    seen: set[tuple[str, str, Optional[int], Optional[int]]] = set()
    result: list[Citation] = []
    for citation in citations:
        key = (citation.source_sha256, citation.kind, citation.paragraph_no, citation.line_no)
        if key in seen:
            continue
        seen.add(key)
        result.append(citation)
    return result


#: 审计表的「拒改」触发器。单独提成常量，是为了让**老库补链**时能
#: 临时摘掉它、补完链哈希再原样装回（见 ``_backfill_audit_chain``）。
#: 摘与装都在同一个写事务里 —— 中途失败整个事务回滚，触发器原样还在，
#: 不会出现「触发器被摘掉却没装回」的窗口。
AUDIT_NO_UPDATE_TRIGGER = """
    CREATE TRIGGER IF NOT EXISTS audit_no_update
    BEFORE UPDATE ON audit
    BEGIN
        SELECT RAISE(ABORT, 'audit 表是 append-only：审计流水不允许修改');
    END
"""


#: 库结构。``events.idempotency_key`` 上的 UNIQUE 约束就是去重的物理保证。
SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS events (
        event_id           TEXT PRIMARY KEY,
        idempotency_key    TEXT NOT NULL UNIQUE,
        message_control_id TEXT NOT NULL DEFAULT '',
        specimen_no        TEXT NOT NULL DEFAULT '',
        item_code          TEXT NOT NULL DEFAULT '',
        item_name          TEXT NOT NULL DEFAULT '',
        value              TEXT NOT NULL DEFAULT '',
        unit               TEXT NOT NULL DEFAULT '',
        observed_at        TEXT NOT NULL DEFAULT '',
        dept_code          TEXT NOT NULL DEFAULT '',
        obx_flag           TEXT NOT NULL DEFAULT '',
        result_status      TEXT NOT NULL DEFAULT '',
        patient_ref        TEXT NOT NULL DEFAULT '',
        source_channel     TEXT NOT NULL DEFAULT 'hl7',
        item_code_source   TEXT NOT NULL DEFAULT '',
        item_code_confidence TEXT NOT NULL DEFAULT 'high',
        status             TEXT NOT NULL DEFAULT 'received',
        supersedes         TEXT NOT NULL DEFAULT '',
        superseded_by      TEXT NOT NULL DEFAULT '',
        judgement_decision TEXT NOT NULL DEFAULT '',
        judgement_reason   TEXT NOT NULL DEFAULT '',
        threshold_row      TEXT NOT NULL DEFAULT '',
        card_id            TEXT NOT NULL DEFAULT '',
        config_version     INTEGER NOT NULL DEFAULT 0,
        ruleset_snapshot_id TEXT NOT NULL DEFAULT '',
        received_at        TEXT NOT NULL DEFAULT '',
        created_at         TEXT NOT NULL DEFAULT '',
        notify_target      TEXT NOT NULL DEFAULT '',
        due_minutes        INTEGER,
        due_at             TEXT NOT NULL DEFAULT '',
        notified_at        TEXT NOT NULL DEFAULT '',
        last_notified_at   TEXT NOT NULL DEFAULT '',
        notify_channel     TEXT NOT NULL DEFAULT '',
        retry_count        INTEGER NOT NULL DEFAULT 0,
        acknowledged_by    TEXT NOT NULL DEFAULT '',
        acknowledged_at    TEXT NOT NULL DEFAULT '',
        registered_at      TEXT NOT NULL DEFAULT ''
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_events_mcid ON events (message_control_id)",
    "CREATE INDEX IF NOT EXISTS idx_events_item ON events (item_code)",
    "CREATE INDEX IF NOT EXISTS idx_events_status ON events (status)",
    "CREATE INDEX IF NOT EXISTS idx_events_observed ON events (observed_at)",
    # 幂等键的**唯一**索引：跨进程去重的物理保证。
    # 建表时 ``idempotency_key`` 已带列级 UNIQUE；这里再落一条显式唯一索引，
    # 老库补建时也能拿到同一份保证（``doctor`` 会查它在不在）。
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_events_idempotency ON events (idempotency_key)",
    """
    CREATE TABLE IF NOT EXISTS threshold_rows (
        row_id            INTEGER PRIMARY KEY AUTOINCREMENT,
        item_code         TEXT NOT NULL,
        item_name         TEXT NOT NULL DEFAULT '',
        unit              TEXT NOT NULL DEFAULT '',
        low_critical      TEXT NOT NULL DEFAULT '',
        high_critical     TEXT NOT NULL DEFAULT '',
        reference_range_raw TEXT NOT NULL DEFAULT '',
        effective_from    TEXT NOT NULL DEFAULT '',
        effective_to      TEXT NOT NULL DEFAULT '',
        version           TEXT NOT NULL DEFAULT '',
        source_name       TEXT NOT NULL DEFAULT '',
        source_sha256     TEXT NOT NULL DEFAULT '',
        line_no           INTEGER,
        file_version      TEXT NOT NULL DEFAULT '',
        UNIQUE (source_sha256, item_code, line_no)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS clauses (
        clause_id         INTEGER PRIMARY KEY AUTOINCREMENT,
        source_name       TEXT NOT NULL,
        source_sha256     TEXT NOT NULL,
        section_title     TEXT NOT NULL DEFAULT '',
        body              TEXT NOT NULL DEFAULT '',
        notify_target     TEXT NOT NULL DEFAULT '',
        deadline_text     TEXT NOT NULL DEFAULT '',
        deadline_minutes  INTEGER,
        record_destination TEXT NOT NULL DEFAULT '',
        paragraph_no      INTEGER,
        line_no           INTEGER,
        file_version      TEXT NOT NULL DEFAULT '',
        UNIQUE (source_sha256, paragraph_no)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS citations (
        citation_id   INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id      TEXT NOT NULL DEFAULT '',
        card_id       TEXT NOT NULL DEFAULT '',
        source_name   TEXT NOT NULL,
        source_sha256 TEXT NOT NULL,
        kind          TEXT NOT NULL DEFAULT 'threshold_row',
        paragraph_no  INTEGER,
        section_path  TEXT NOT NULL DEFAULT '',
        line_no       INTEGER,
        file_version  TEXT NOT NULL DEFAULT '',
        locator       TEXT NOT NULL DEFAULT ''
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_citations_event ON citations (event_id)",
    """
    CREATE TABLE IF NOT EXISTS audit (
        seq         INTEGER PRIMARY KEY AUTOINCREMENT,
        at          TEXT NOT NULL,
        action      TEXT NOT NULL,
        event_id    TEXT NOT NULL DEFAULT '',
        actor       TEXT NOT NULL DEFAULT '',
        dept_code   TEXT NOT NULL DEFAULT '',
        specimen_no TEXT NOT NULL DEFAULT '',
        item_code   TEXT NOT NULL DEFAULT '',
        message_control_id TEXT NOT NULL DEFAULT '',
        detail      TEXT NOT NULL DEFAULT '',
        prev_hash   TEXT NOT NULL DEFAULT '',
        payload_hash TEXT NOT NULL DEFAULT '',
        chain_hash  TEXT NOT NULL DEFAULT ''
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_audit_event ON audit (event_id)",
    # 审计表 **append-only**（任务 id 10）：库级触发器直接拒改、拒删。
    # 这不是 Python 层的自律 —— 绕过应用代码直接改库同样会被拦下。
    #
    # 但触发器只挡「还握着库连接的人」（任务 id 40）：先 DROP TRIGGER
    # 再改一条，触发器拦不住。所以每条行另带链式哈希
    # （prev_hash / payload_hash / chain_hash），`audit-verify` 逐条重算。
    AUDIT_NO_UPDATE_TRIGGER,
    """
    CREATE TRIGGER IF NOT EXISTS audit_no_delete
    BEFORE DELETE ON audit
    BEGIN
        SELECT RAISE(ABORT, 'audit 表是 append-only：审计流水不允许删除');
    END
    """,
    """
    CREATE TABLE IF NOT EXISTS idempotency_keys (
        key          TEXT PRIMARY KEY,
        event_id     TEXT NOT NULL,
        first_seen_at TEXT NOT NULL,
        hits         INTEGER NOT NULL DEFAULT 1
    )
    """,
    # 通知记录（任务 id 10）：**每次推送尝试追加一行**，原行不改。
    # ``kind=initial`` 首次推送 / ``kind=retry`` 到期重推；
    # ``attempt`` 是第几次尝试。重推不覆盖首次推送的行，也不改事件行上的
    # ``notified_at``（首次推送时刻）—— 只更新 ``last_notified_at``。
    """
    CREATE TABLE IF NOT EXISTS notifications (
        notification_id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id       TEXT NOT NULL,
        card_id        TEXT NOT NULL DEFAULT '',
        channel        TEXT NOT NULL DEFAULT '',
        kind           TEXT NOT NULL DEFAULT 'initial',
        attempt        INTEGER NOT NULL DEFAULT 1,
        notified_at    TEXT NOT NULL DEFAULT '',
        due_at         TEXT NOT NULL DEFAULT '',
        detail         TEXT NOT NULL DEFAULT ''
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_notifications_event ON notifications (event_id)",
    # 逐次投递记录（任务 id 42）：**每一次出站尝试**留一行。
    # ``notifications`` 记的是「通知轮次」，这张表记的是「这一轮里试了几次、
    # 每次的 HTTP 状态码、耗时、退避等待多久、报了什么错」—— 事后能回答
    # 「这条到底推了几次、为什么重推」。
    """
    CREATE TABLE IF NOT EXISTS notify_deliveries (
        delivery_id  INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id     TEXT NOT NULL,
        card_id      TEXT NOT NULL DEFAULT '',
        kind         TEXT NOT NULL DEFAULT 'initial',
        round_key    TEXT NOT NULL DEFAULT '',
        attempt      INTEGER NOT NULL DEFAULT 1,
        channel      TEXT NOT NULL DEFAULT '',
        ok           INTEGER NOT NULL DEFAULT 0,
        status_code  INTEGER NOT NULL DEFAULT 0,
        elapsed_ms   INTEGER NOT NULL DEFAULT 0,
        waited_ms    INTEGER NOT NULL DEFAULT 0,
        error        TEXT NOT NULL DEFAULT '',
        attempted_at TEXT NOT NULL DEFAULT ''
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_notify_deliveries_event ON notify_deliveries (event_id)",
    # 死信（任务 id 42）：重试耗尽后**原卡内容与最后一次错误**留档。
    # 事件本身不丢（仍在 events 表里，状态落 failed 等人处理），
    # 死信表是「这条推不出去」的**专门落点**，值班台与复核都看它。
    """
    CREATE TABLE IF NOT EXISTS notify_dead_letters (
        dead_letter_id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id       TEXT NOT NULL,
        card_id        TEXT NOT NULL DEFAULT '',
        kind           TEXT NOT NULL DEFAULT 'initial',
        round_key      TEXT NOT NULL DEFAULT '',
        channel        TEXT NOT NULL DEFAULT '',
        attempts       INTEGER NOT NULL DEFAULT 0,
        last_error     TEXT NOT NULL DEFAULT '',
        card_content   TEXT NOT NULL DEFAULT '',
        payload        TEXT NOT NULL DEFAULT '',
        created_at     TEXT NOT NULL DEFAULT ''
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_notify_dead_letters_event ON notify_dead_letters (event_id)",
    # 投递轮次（任务 id 42）：**重复投递抑制**的物理落点。
    # 主键就是抑制键（事件 ID | 投递状态 | 尝试轮次）—— 同一轮次只允许一个人
    # 推；``delivered`` 之后再来同轮次的推送直接被抑制；``failed`` 允许重新
    # 驱动（人修好 webhook 后重推），``in_flight`` 超过陈旧窗口才允许抢占
    # （进程崩了没释放的情形）。
    """
    CREATE TABLE IF NOT EXISTS notify_delivery_rounds (
        round_key     TEXT PRIMARY KEY,
        event_id      TEXT NOT NULL DEFAULT '',
        kind          TEXT NOT NULL DEFAULT '',
        attempt_round TEXT NOT NULL DEFAULT '',
        channel       TEXT NOT NULL DEFAULT '',
        state         TEXT NOT NULL DEFAULT 'in_flight',
        deliveries    INTEGER NOT NULL DEFAULT 1,
        updated_at    TEXT NOT NULL DEFAULT ''
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_notify_delivery_rounds_event ON notify_delivery_rounds (event_id)",
    # 判定记录（任务 id 36）：一条事件一份「为什么这么判」的完整判定链。
    # 结论本身在 events 行上；这张表存的是**可逐步复算的过程**。
    """
    CREATE TABLE IF NOT EXISTS judgement_records (
        event_id            TEXT PRIMARY KEY,
        decision            TEXT NOT NULL DEFAULT '',
        reason              TEXT NOT NULL DEFAULT '',
        item_code           TEXT NOT NULL DEFAULT '',
        value               TEXT NOT NULL DEFAULT '',
        unit                TEXT NOT NULL DEFAULT '',
        converted_value     TEXT NOT NULL DEFAULT '',
        converted_unit      TEXT NOT NULL DEFAULT '',
        rule_id             TEXT NOT NULL DEFAULT '',
        threshold_locator   TEXT NOT NULL DEFAULT '',
        source_name         TEXT NOT NULL DEFAULT '',
        source_sha256       TEXT NOT NULL DEFAULT '',
        source_line         INTEGER,
        file_version        TEXT NOT NULL DEFAULT '',
        threshold_source_sha256 TEXT NOT NULL DEFAULT '',
        ruleset_snapshot_id TEXT NOT NULL DEFAULT '',
        config_version      INTEGER NOT NULL DEFAULT 0,
        payload             TEXT NOT NULL DEFAULT '',
        recorded_at         TEXT NOT NULL DEFAULT ''
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_judgement_records_item ON judgement_records (item_code)",
    # 回写**既有事实**（任务 id 43）：登记系统按 message_control_id 覆盖同
    # 一条，所以这张表是「本地已经写进去过的那条」—— **只写一次，不覆盖**。
    # 幂等冲突检测比对的就是这里的 payload_hash：同一消息控制号下内容不同
    # → 报冲突，既有事实一个字都不改。
    """
    CREATE TABLE IF NOT EXISTS writeback_receipts (
        message_control_id TEXT PRIMARY KEY,
        event_id           TEXT NOT NULL DEFAULT '',
        payload_json       TEXT NOT NULL DEFAULT '',
        payload_hash       TEXT NOT NULL DEFAULT '',
        receipt_status     TEXT NOT NULL DEFAULT '',
        http_status        INTEGER NOT NULL DEFAULT 0,
        written_at         TEXT NOT NULL DEFAULT ''
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_writeback_receipts_event ON writeback_receipts (event_id)",
    # 回写**尝试流水**（追加式）：每次尝试一行 —— 成功、契约错、冲突都留。
    # 这张表回答「这条回执试过几次、每次为什么没成」。
    """
    CREATE TABLE IF NOT EXISTS writeback_attempts (
        attempt_id         INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id           TEXT NOT NULL DEFAULT '',
        message_control_id TEXT NOT NULL DEFAULT '',
        payload_hash       TEXT NOT NULL DEFAULT '',
        outcome            TEXT NOT NULL DEFAULT '',
        failure_class      TEXT NOT NULL DEFAULT '',
        retryable          INTEGER NOT NULL DEFAULT 0,
        http_status        INTEGER NOT NULL DEFAULT 0,
        error              TEXT NOT NULL DEFAULT '',
        detail             TEXT NOT NULL DEFAULT '',
        at                 TEXT NOT NULL DEFAULT ''
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_writeback_attempts_mcid ON writeback_attempts (message_control_id)",
    "CREATE INDEX IF NOT EXISTS idx_writeback_attempts_event ON writeback_attempts (event_id)",
    # 处置链**节点级轨迹**（任务 id 45）：一条事件一行一个节点。
    # 记的是「这一步花了多久、读到了什么、结论是什么、为什么停」——
    # 回答「这条事件在哪一步停下、为什么」靠的就是这张表。
    # 摘要已脱敏（患者标识层字段不进表），只落标量。
    """
    CREATE TABLE IF NOT EXISTS flow_traces (
        trace_id       INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id       TEXT NOT NULL DEFAULT '',
        seq            INTEGER NOT NULL DEFAULT 0,
        node           TEXT NOT NULL DEFAULT '',
        component      TEXT NOT NULL DEFAULT '',
        status         TEXT NOT NULL DEFAULT '',
        conclusion     TEXT NOT NULL DEFAULT '',
        elapsed_ms     REAL NOT NULL DEFAULT 0,
        input_summary  TEXT NOT NULL DEFAULT '',
        output_summary TEXT NOT NULL DEFAULT '',
        error          TEXT NOT NULL DEFAULT '',
        retryable      INTEGER NOT NULL DEFAULT 0,
        at             TEXT NOT NULL DEFAULT ''
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_flow_traces_event ON flow_traces (event_id)",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class LedgerStore:
    """事件台账（SQLite）。

    用法::

        with LedgerStore("./data/critval.db") as store:
            store.record_event(event)      # True = 新事件，False = 重复已跳过
            store.record_judgement(event.event_id, judgement)
            store.record_card(card)
    """

    def __init__(
        self,
        path: str | Path = ":memory:",
        *,
        dedup_window_seconds: int = 24 * 3600,
    ) -> None:
        self.path = str(path)
        if self.path not in (":memory:", "") and not self.path.startswith("file:"):
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        # ``check_same_thread=False``：值班台服务承载是**多线程**的
        # （ThreadingHTTPServer），入站推送端点与页面渲染各占一个线程，
        # 而同一个库连接要同时被它们用上。SQLite 自身不保证跨线程复用连接
        # 的安全性，所以下面配一把进程内锁把所有写事务串起来 ——
        # 「连接可跨线程用」与「访问被串行化」必须一起给，缺一个就是
        # 「守护进程跑到一半崩在 sqlite3 内部」。
        self._conn = sqlite3.connect(
            self.path, timeout=30.0, isolation_level=None, check_same_thread=False
        )
        self._conn.row_factory = sqlite3.Row
        # WAL + busy_timeout：守护进程与 CLI 可能同时开库，别把对方堵死。
        # - WAL：读写不互斥 —— 值班台页面在读的同时，入站线程还能写；
        # - busy_timeout：拿不到写锁时等 30 秒再报「database is locked」，
        #   跨进程并发（守护进程 + CLI doctor）靠它不打架。
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._lock = threading.RLock()
        #: 写事务嵌套深度（0 = 不在事务里）。锁保证同一时刻只有一个线程
        #: 在事务中，所以这个计数器用实例级就够（见 ``_transaction``）。
        self._tx_depth = 0
        #: 幂等键去重窗口（进程内快查层，见 ``critval/ledger/idempotency.py``）。
        #: 真正的去重权威永远是 ``events.idempotency_key`` 上的 UNIQUE 约束 ——
        #: 跨进程也成立；窗口只负责挡住进程内绝大多数重复投递并按住内存。
        self.idempotency_window = IdempotencyWindow(window_seconds=dedup_window_seconds)
        self.initialize()

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def initialize(self) -> None:
        """建表建索引（幂等，可重复调用）。"""
        with self._transaction():
            for statement in SCHEMA_STATEMENTS:
                self._conn.execute(statement)
            legacy = self._migrate_columns()
            self._migrate_flow_trace_columns()
            self._backfill_audit_chain(legacy=legacy)

    def _migrate_flow_trace_columns(self) -> None:
        """给老库补 ``flow_traces.retryable`` 列。

        这一列是后加的（任务 id 45 收口）：老库里已建好的 ``flow_traces``
        表没有它，补一列默认值即可，**不重建表、不丢已有轨迹**。
        """
        columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(flow_traces)")}
        if columns and "retryable" not in columns:
            self._conn.execute(
                "ALTER TABLE flow_traces ADD COLUMN retryable INTEGER NOT NULL DEFAULT 0"
            )

    def _migrate_columns(self) -> bool:
        """给老库补后来新增的列（``ALTER TABLE``，不重建表、不丢数据）。

        返回 ``True`` 表示**本次刚补上链哈希三列** —— 也就是这个库是在
        链式哈希之前建的，历史行还没链上，需要补链（见
        :meth:`_backfill_audit_chain`）。
        """
        columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(audit)")}
        if "message_control_id" not in columns:
            self._conn.execute(
                "ALTER TABLE audit ADD COLUMN message_control_id TEXT NOT NULL DEFAULT ''"
            )
        legacy = False
        for column in ("prev_hash", "payload_hash", "chain_hash"):
            if column not in columns:
                self._conn.execute(
                    f"ALTER TABLE audit ADD COLUMN {column} TEXT NOT NULL DEFAULT ''"
                )
                legacy = True
        return legacy
        event_columns = {
            row["name"] for row in self._conn.execute("PRAGMA table_info(events)")
        }
        if "item_code_source" not in event_columns:
            self._conn.execute(
                "ALTER TABLE events ADD COLUMN item_code_source TEXT NOT NULL DEFAULT ''"
            )
        if "item_code_confidence" not in event_columns:
            self._conn.execute(
                "ALTER TABLE events ADD COLUMN item_code_confidence TEXT NOT NULL DEFAULT 'high'"
            )
        if "supersedes" not in event_columns:
            self._conn.execute(
                "ALTER TABLE events ADD COLUMN supersedes TEXT NOT NULL DEFAULT ''"
            )
        if "superseded_by" not in event_columns:
            self._conn.execute(
                "ALTER TABLE events ADD COLUMN superseded_by TEXT NOT NULL DEFAULT ''"
            )
        # 任务 id 10 新增的状态机相关列（通知 / 回执 / 重推记账）。
        for column, ddl in (
            ("notify_target", "TEXT NOT NULL DEFAULT ''"),
            ("due_minutes", "INTEGER"),
            ("due_at", "TEXT NOT NULL DEFAULT ''"),
            ("notified_at", "TEXT NOT NULL DEFAULT ''"),
            ("last_notified_at", "TEXT NOT NULL DEFAULT ''"),
            ("notify_channel", "TEXT NOT NULL DEFAULT ''"),
            ("retry_count", "INTEGER NOT NULL DEFAULT 0"),
            ("acknowledged_by", "TEXT NOT NULL DEFAULT ''"),
            ("acknowledged_at", "TEXT NOT NULL DEFAULT ''"),
            ("registered_at", "TEXT NOT NULL DEFAULT ''"),
        ):
            if column not in event_columns:
                self._conn.execute(f"ALTER TABLE events ADD COLUMN {column} {ddl}")

    def _backfill_audit_chain(self, *, legacy: bool) -> int:
        """给**补链之前**就写下的历史审计行补上链哈希，返回补了几条。

        为什么要有这一步：本项目在任务 id 40 之前，审计行只有
        append-only 触发器兜着 —— 那套触发器拦得住「改」，但**拦不住
        「先摘触发器再改」**。老库里的历史行没有链哈希，自检命令就没法
        对它们下结论（不能因为「没链」就当「没事」，也不能因为「没链」
        就当「坏了」）。补上链哈希后，整条历史链也能被独立校验。

        **只在老库上补**（``legacy=True``，即本次刚加上的链哈希三列）。
        列已经在的库若出现空哈希，那是**有人动过**或者补链没做完 ——
        这时候悄悄重锚，等于把篡改的痕迹抹平，自检命令就永远报不出问题。
        所以那种情况一律**不补**，交给 ``audit-verify`` 报成断链。

        补链要改写历史行 —— 而审计表的拒改触发器正是不让改。所以这里
        **临时摘掉拒改触发器、补完、原样装回**。摘与装都在同一个写事务
        里：中途任何一步失败，整个事务回滚，触发器跟着回滚回去，
        不会出现「触发器被摘掉却没装回」的窗口。
        """
        if not legacy:
            return 0
        rows = self._conn.execute("SELECT * FROM audit ORDER BY seq").fetchall()
        if not rows:
            return 0

        self._conn.execute("DROP TRIGGER IF EXISTS audit_no_update")
        try:
            prev = GENESIS_HASH
            for row in rows:
                values = {name: row[name] for name in AUDIT_PAYLOAD_FIELDS}
                payload_hash = audit_payload_hash(values)
                chain_hash = audit_chain_hash(prev, payload_hash)
                self._conn.execute(
                    """
                    UPDATE audit
                       SET prev_hash = ?, payload_hash = ?, chain_hash = ?
                     WHERE seq = ?
                    """,
                    (prev, payload_hash, chain_hash, row["seq"]),
                )
                prev = chain_hash
        finally:
            self._conn.execute(AUDIT_NO_UPDATE_TRIGGER)
        return len(rows)

    @contextlib.contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        """显式写事务：``BEGIN IMMEDIATE`` 立刻拿写锁，避免并发写打架。

        进程内再加一把 ``RLock`` —— 连接可跨线程用（见 ``__init__``），
        但同一时刻只允许一个线程在写。

        **可重入**：已经在事务里再开一层事务时，内层直接复用外层事务
        （不再 ``BEGIN``）—— 否则 sqlite3 会抛
        「cannot start a transaction within a transaction」。崩溃恢复
        （``critval/ledger/recovery.py``）要把「写状态 + 写审计 + 写通知
        记录」合成一步时，靠的就是这个。
        """
        with self._lock:
            if self._tx_depth > 0:
                # 内层：复用外层事务，不重复 BEGIN / COMMIT。
                self._tx_depth += 1
                try:
                    yield self._conn
                finally:
                    self._tx_depth -= 1
                return
            self._conn.execute("BEGIN IMMEDIATE")
            self._tx_depth = 1
            try:
                yield self._conn
            except Exception:
                self._tx_depth = 0
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._tx_depth = 0
                self._conn.execute("COMMIT")

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "LedgerStore":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # 事件
    # ------------------------------------------------------------------
    def record_event(
        self,
        event: CriticalEvent,
        received_at: Optional[datetime] = None,
    ) -> bool:
        """落一条事件。

        返回 ``True`` 表示这是**新事件**；返回 ``False`` 表示幂等键已存在
        —— **既有事实不被覆盖**，只把命中次数加一。
        """
        key = idempotency_key_of(event)
        now = (received_at or datetime.now(timezone.utc)).isoformat()
        with self._transaction():
            existing = self._conn.execute(
                "SELECT event_id FROM events WHERE idempotency_key = ?",
                (key,),
            ).fetchone()
            if existing is not None:
                self._conn.execute(
                    "UPDATE idempotency_keys SET hits = hits + 1 WHERE key = ?",
                    (key,),
                )
                self.idempotency_window.record(key)
                return False
            try:
                self._conn.execute(
                    """
                    INSERT INTO events (
                        event_id, idempotency_key, message_control_id, specimen_no,
                        item_code, item_name, value, unit, observed_at, dept_code,
                        obx_flag, result_status, patient_ref, source_channel,
                        item_code_source, item_code_confidence, supersedes,
                        status, received_at, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event.event_id,
                        key,
                        event.message_control_id,
                        event.specimen_no,
                        event.item_code,
                        event.item_name,
                        str(event.value),
                        event.unit,
                        event.observed_at.isoformat() if event.observed_at else "",
                        event.dept_code,
                        event.obx_flag,
                        event.result_status,
                        event.patient_ref,
                        event.source_channel,
                        event.item_code_source,
                        event.item_code_confidence,
                        getattr(event, "supersedes", "") or "",
                        "received",
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError:
                # 并发写同一幂等键：另一个事务先插进去了 —— 既有事实不被
                # 覆盖，本进程按「重复到达」处理，不抛栈。
                self._conn.execute(
                    "UPDATE idempotency_keys SET hits = hits + 1 WHERE key = ?",
                    (key,),
                )
                self.idempotency_window.record(key)
                return False
            self._conn.execute(
                """
                INSERT OR IGNORE INTO idempotency_keys (key, event_id, first_seen_at, hits)
                VALUES (?, ?, ?, 1)
                """,
                (key, event.event_id, now),
            )
            self.idempotency_window.record(key)
            return True

    def record_judgement(self, event_id: str, judgement: Judgement) -> None:
        """把判定结论写到事件行上（结论 + 理由 + 命中阈值行 + 版本号）。

        判定结论落库 = 这条事件**已经判定过** —— 事件行随之从
        ``received`` 推进到 ``judged``（状态机见 ``critval/ledger/states.py``）。
        已经是别的状态就**不动**：判定记录可以被重算覆盖，但状态不倒退。
        """
        threshold_row = ""
        if judgement.threshold_row is not None:
            threshold_row = judgement.threshold_row.citation.locator
        with self._transaction():
            self._conn.execute(
                """
                UPDATE events
                   SET judgement_decision = ?, judgement_reason = ?, threshold_row = ?,
                       config_version = ?, ruleset_snapshot_id = ?
                 WHERE event_id = ?
                """,
                (
                    judgement.decision,
                    judgement.reason,
                    threshold_row,
                    judgement.config_version,
                    judgement.ruleset_snapshot_id,
                    event_id,
                ),
            )
        if self.get_status(event_id) == "received":
            self.set_status(
                event_id,
                "judged",
                actor="judge",
                detail=f"判定完成：{judgement.decision}",
            )

    def record_judgement_record(self, record: Any) -> None:
        """把**判定记录**（判定链）落库（任务 id 36）。

        一条事件一份判定记录，按 ``event_id`` 覆盖写：判定结论被重算时
        （例如修正结果取代旧事件后重新判定），记录跟着更新到最新结论，
        但**历史判定记录不会被另存一份**—— 复核看的是「这条事件当前的
        判定链」，历史事实由审计流水与取代链保留。

        载荷里只有判定过程事实，**不含患者可识别明细**（落盘前由
        ``critval.judge.explain.assert_no_patient_detail`` 兜底）。
        """
        payload = record.to_dict()
        with self._transaction():
            self._conn.execute(
                """
                INSERT INTO judgement_records (
                    event_id, decision, reason, item_code, value, unit,
                    converted_value, converted_unit, rule_id, threshold_locator,
                    source_name, source_sha256, source_line, file_version,
                    threshold_source_sha256, ruleset_snapshot_id, config_version,
                    payload, recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(event_id) DO UPDATE SET
                    decision = excluded.decision,
                    reason = excluded.reason,
                    item_code = excluded.item_code,
                    value = excluded.value,
                    unit = excluded.unit,
                    converted_value = excluded.converted_value,
                    converted_unit = excluded.converted_unit,
                    rule_id = excluded.rule_id,
                    threshold_locator = excluded.threshold_locator,
                    source_name = excluded.source_name,
                    source_sha256 = excluded.source_sha256,
                    source_line = excluded.source_line,
                    file_version = excluded.file_version,
                    threshold_source_sha256 = excluded.threshold_source_sha256,
                    ruleset_snapshot_id = excluded.ruleset_snapshot_id,
                    config_version = excluded.config_version,
                    payload = excluded.payload,
                    recorded_at = excluded.recorded_at
                """,
                (
                    record.event_id,
                    record.decision,
                    record.reason,
                    record.item_code,
                    record.value,
                    record.unit,
                    record.converted_value,
                    record.converted_unit,
                    record.rule_id,
                    record.threshold_locator,
                    record.source_name,
                    record.source_sha256,
                    record.source_line,
                    record.file_version,
                    record.threshold_source_sha256,
                    record.ruleset_snapshot_id,
                    record.config_version,
                    json.dumps(payload, ensure_ascii=False),
                    record.recorded_at,
                ),
            )

    def get_judgement_record(self, event_id: str) -> Optional[dict[str, Any]]:
        """按事件 ID 取回判定记录（取不到返回 ``None``）。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT payload FROM judgement_records WHERE event_id = ?",
                (event_id,),
            ).fetchone()
        if row is None:
            return None
        try:
            payload = json.loads(row["payload"] or "{}")
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict):
            return None
        return payload

    def judgement_record_count(self) -> int:
        """判定记录条数（复核用）。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM judgement_records"
            ).fetchone()
        return int(row["n"] if row else 0)

    def record_card(self, card: DispositionCard) -> None:
        """把处置卡与它的引用落库（事件行挂上 card_id）。

        出卡 = 事件从 ``judged`` 推进到 ``card_built``（走状态机校验）。
        卡上的「通知对象」「上报时限」顺手落到事件行 —— 事后复核时
        「通知了谁、几分钟内」不必再回头翻卡片快照。
        """
        with self._transaction():
            self._conn.execute(
                "UPDATE events SET card_id = ?, notify_target = ?, due_minutes = ? WHERE event_id = ?",
                (
                    card.card_id,
                    card.notify_target.value or "",
                    _deadline_minutes(card.deadline.value),
                    card.event.event_id,
                ),
            )
            for citation in unique_citations(card.citations):
                self._insert_citation(card.event.event_id, card.card_id, citation)
        self.set_status(
            card.event.event_id,
            "card_built",
            actor="card",
            detail=f"处置卡已装配（{card.card_id}）",
        )

    def record_merged_card(self, card: Any) -> None:
        """把**合并处置卡**与逐项引用落库（任务 id 34）。

        卡里每个危急项目仍是一条独立事件 —— 它们挂**同一个** ``card_id``：
        值班群只推一次，但台账里每条事件仍能追到自己的处置卡与依据行。
        """
        with self._transaction():
            shared = list(card.extra_citations)
            for item in card.items:
                self._conn.execute(
                    "UPDATE events SET card_id = ?, notify_target = ?, due_minutes = ? WHERE event_id = ?",
                    (
                        card.card_id,
                        card.notify_target.value or "",
                        _deadline_minutes(card.deadline.value),
                        item.event_id,
                    ),
                )
                citations: list[Citation] = []
                if item.threshold_basis.source is not None:
                    citations.append(item.threshold_basis.source)
                citations.extend(shared)
                for citation in unique_citations(citations):
                    self._insert_citation(item.event_id, card.card_id, citation)
        for item in card.items:
            self.set_status(
                item.event_id,
                "card_built",
                actor="card",
                detail=f"合并处置卡已装配（{card.card_id}）",
            )

    def _insert_citation(self, event_id: str, card_id: str, citation: Citation) -> None:
        self._conn.execute(
            """
            INSERT INTO citations (
                event_id, card_id, source_name, source_sha256, kind,
                paragraph_no, section_path, line_no, file_version, locator
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                card_id,
                citation.source_name,
                citation.source_sha256,
                citation.kind,
                citation.paragraph_no,
                " / ".join(citation.section_path),
                citation.line_no,
                citation.file_version or "",
                citation.locator,
            ),
        )

    def record_citation(self, event_id: str, card_id: str, citation: Citation) -> None:
        """单独落一条引用（召回阶段先于出卡时用）。"""
        with self._transaction():
            self._insert_citation(event_id, card_id, citation)

    def set_status(
        self,
        event_id: str,
        status: str,
        *,
        allow_terminal: bool = False,
        actor: str = "",
        detail: str = "",
        at: Optional[datetime] = None,
    ) -> bool:
        """改事件状态（**状态机在这里守着**）。

        跃迁表见 ``critval/ledger/states.py``：

        * **非法跃迁被拒** —— 跳步（``judged`` 直接到 ``notified``）抛
          :class:`~critval.ledger.states.IllegalTransitionError`，
          报错即留痕，不静默放行；
        * **终态不可静默回退** —— ``superseded`` / ``archived`` 是终态，
          从终态再迁走抛 :class:`TerminalStateError`；
        * **同状态是幂等空操作** —— 值班人点两次确认不该报错，但也不写
          审计（否则审计里全是噪音）。

        ``allow_terminal=True`` 是**显式覆盖**：人工裁决路径上确实需要把
        终态事件改回别的状态时用（例如归档一条已被取代的旧事件），这条
        路会绕过跃迁表校验 —— 所以它必须由人显式写出来，不能是默认值。

        返回 ``True`` 表示状态确实变了，``False`` 表示同状态空操作。
        """
        current = self.get_status(event_id)
        if current == status:
            return False
        if (
            current in TERMINAL_STATUSES
            and status != current
            and not allow_terminal
        ):
            raise TerminalStateError(event_id, current, status)
        if not allow_terminal:
            validate_transition(current, status)
        moment = at or datetime.now(timezone.utc)
        with self._transaction():
            self._conn.execute(
                "UPDATE events SET status = ? WHERE event_id = ?",
                (status, event_id),
            )
        row = self.get_event(event_id) or {}
        self.append_audit(
            "status_transition",
            event_id=event_id,
            actor=actor,
            dept_code=str(row.get("dept_code", "") or ""),
            specimen_no=str(row.get("specimen_no", "") or ""),
            item_code=str(row.get("item_code", "") or ""),
            message_control_id=str(row.get("message_control_id", "") or ""),
            detail=(
                f"{describe_status(current)} → {describe_status(status)}"
                f"（{current} → {status}）"
                + (f"；{detail}" if detail else "")
            ),
            at=moment,
        )
        return True

    # ------------------------------------------------------------------
    # 业务里程碑（状态机上的几个固定入口）
    # ------------------------------------------------------------------
    def mark_notified(
        self,
        event_id: str,
        *,
        notify_target: str = "",
        due_minutes: Optional[int] = None,
        channel: str = "",
        card_id: str = "",
        actor: str = "notify",
        detail: str = "",
        at: Optional[datetime] = None,
        record_notification: bool = True,
    ) -> bool:
        """记一次**首次推送**：状态 → ``notified``，并追加一条通知记录。

        ``due_minutes`` 是制度里的上报时限 —— 据此算出 ``due_at``（到期
        时刻），到期没回执就由 ``critval/ledger/retry.py`` 复查重推。

        ``record_notification=False``：通知记录**已经在案**时不再追加一条
        （崩溃恢复补状态时用 —— 补状态不该把「推送次数」灌成两条）。
        """
        moment = at or datetime.now(timezone.utc)
        row = self.get_event(event_id) or {}
        minutes = due_minutes if due_minutes is not None else row.get("due_minutes")
        minutes = int(minutes) if minutes not in (None, "") else None
        due_at = ""
        if minutes is not None:
            due_at = (moment + timedelta(minutes=minutes)).isoformat()
        with self._transaction():
            self._conn.execute(
                """
                UPDATE events
                   SET notified_at = ?, last_notified_at = ?, due_at = ?,
                       notify_channel = ?, notify_target = COALESCE(NULLIF(?, ''), notify_target),
                       due_minutes = COALESCE(?, due_minutes)
                 WHERE event_id = ?
                """,
                (
                    moment.isoformat(),
                    moment.isoformat(),
                    due_at,
                    channel,
                    notify_target,
                    minutes,
                    event_id,
                ),
            )
        if record_notification:
            self.record_notification(
                event_id,
                kind="initial",
                attempt=1,
                channel=channel,
                card_id=card_id or str(row.get("card_id", "") or ""),
                notified_at=moment,
                due_at=due_at,
                detail=detail or "首次推送",
            )
        return self.set_status(
            event_id,
            "notified",
            actor=actor,
            detail=detail or f"已推送至值班渠道（{channel or '未指定'}）",
            at=moment,
        )

    def mark_pending_ack(
        self,
        event_id: str,
        *,
        actor: str = "notify",
        detail: str = "",
        at: Optional[datetime] = None,
    ) -> bool:
        """进入「已推送待回执」：状态 → ``pending_ack``，到期要复查重推。"""
        return self.set_status(
            event_id,
            "pending_ack",
            actor=actor,
            detail=detail or "等待值班人确认回执",
            at=at,
        )

    def mark_acknowledged(
        self,
        event_id: str,
        *,
        actor: str = "",
        detail: str = "",
        at: Optional[datetime] = None,
    ) -> bool:
        """值班人确认（**人工确认点**）：状态 → ``acknowledged``。

        确认人只记**角色**（``actor``，如「检验科值班技师」），不记姓名
        与患者明细。
        """
        moment = at or datetime.now(timezone.utc)
        with self._transaction():
            self._conn.execute(
                "UPDATE events SET acknowledged_by = ?, acknowledged_at = ? WHERE event_id = ?",
                (actor, moment.isoformat(), event_id),
            )
        changed = self.set_status(
            event_id,
            "acknowledged",
            actor=actor or "duty",
            detail=detail or "值班人已确认处置",
            at=moment,
        )
        if changed:
            self.append_audit(
                "event_acknowledged",
                event_id=event_id,
                actor=actor or "duty",
                detail=detail or f"值班人确认（角色 {actor or '值班人'}）",
                at=moment,
            )
        return changed

    def mark_registered(
        self,
        event_id: str,
        *,
        actor: str = "writeback",
        detail: str = "",
        at: Optional[datetime] = None,
    ) -> bool:
        """回写登记系统成功（**已生效事实**）：状态 → ``registered``。"""
        moment = at or datetime.now(timezone.utc)
        with self._transaction():
            self._conn.execute(
                "UPDATE events SET registered_at = ? WHERE event_id = ?",
                (moment.isoformat(), event_id),
            )
        changed = self.set_status(
            event_id,
            "registered",
            actor=actor,
            detail=detail or "回执已回写登记系统",
            at=moment,
        )
        if changed:
            self.append_audit(
                "writeback_succeeded",
                event_id=event_id,
                actor=actor,
                detail=detail or "回执已回写登记系统（已生效事实）",
                at=moment,
            )
        return changed

    def mark_archived(self, event_id: str, *, actor: str = "", detail: str = "") -> bool:
        """归档（终态）：状态 → ``archived``。"""
        return self.set_status(
            event_id,
            "archived",
            actor=actor,
            detail=detail or "台账归档",
        )

    def get_status(self, event_id: str) -> str:
        """取事件当前状态（没有这条事件时返回空串）。"""
        with self._transaction():
            row = self._conn.execute(
                "SELECT status FROM events WHERE event_id = ?", (event_id,)
            ).fetchone()
        return str(row["status"]) if row is not None else ""

    def events_for_specimen_item(
        self, specimen_no: str, item_code: str
    ) -> list[dict[str, Any]]:
        """同标本 + 同项目的全部事件（取代链找旧事件、互斥值检测找兄弟事件都要用）。"""
        with self._transaction():
            rows = self._conn.execute(
                """
                SELECT * FROM events
                 WHERE specimen_no = ? AND item_code = ?
                 ORDER BY observed_at, created_at, event_id
                """,
                (specimen_no, item_code),
            ).fetchall()
        return [dict(row) for row in rows]

    def sibling_events(
        self,
        specimen_no: str,
        item_code: str,
        observed_at: str,
        *,
        exclude_event_id: str = "",
    ) -> list[dict[str, Any]]:
        """同标本 + 同项目 + **同一观察时刻**的其他事件。

        互斥值检测用：同一时刻出现两个不同数值，就是一对互斥观察。

        **已被取代的事件（``superseded``）不算兄弟** —— 修正结果取代旧事件后，
        旧值已经作废，不再与新值构成互斥。
        """
        with self._transaction():
            rows = self._conn.execute(
                """
                SELECT * FROM events
                 WHERE specimen_no = ? AND item_code = ? AND observed_at = ?
                   AND event_id != ?
                   AND status != ?
                 ORDER BY created_at, event_id
                """,
                (specimen_no, item_code, observed_at, exclude_event_id, SUPERSEDE_STATUS),
            ).fetchall()
        return [dict(row) for row in rows]

    def link_supersede(self, new_event_id: str, old_event_id: str) -> None:
        """接上取代链：新事件记 ``supersedes``，旧事件置 ``superseded`` 并记 ``superseded_by``。

        旧事件的判定结论、命中阈值行与出处**原样保留** —— 取代只改状态与关系，
        不改写历史事实。状态迁移走状态机校验（``superseded`` 是终态，
        迁过去之后普通路径就再也改不动它了）。
        """
        with self._transaction():
            self._conn.execute(
                "UPDATE events SET supersedes = ? WHERE event_id = ?",
                (old_event_id, new_event_id),
            )
        self.set_status(
            old_event_id,
            SUPERSEDE_STATUS,
            actor="judge",
            detail=f"被修正结果 {new_event_id} 取代",
        )
        with self._transaction():
            self._conn.execute(
                "UPDATE events SET superseded_by = ? WHERE event_id = ?",
                (new_event_id, old_event_id),
            )

    # ------------------------------------------------------------------
    # 材料（阈值行 / 制度条款）
    # ------------------------------------------------------------------
    def record_threshold_row(self, row: Any) -> None:
        """落一条阈值行（行级条目 + 指纹 + 版本 + 生效期）。"""
        citation = row.citation
        with self._transaction():
            self._conn.execute(
                """
                INSERT OR REPLACE INTO threshold_rows (
                    item_code, item_name, unit, low_critical, high_critical,
                    reference_range_raw, effective_from, effective_to, version,
                    source_name, source_sha256, line_no, file_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.item_code,
                    row.item_name,
                    row.unit,
                    str(row.low_critical) if row.low_critical is not None else "",
                    str(row.high_critical) if row.high_critical is not None else "",
                    row.reference_range_raw,
                    row.effective_from.isoformat() if row.effective_from else "",
                    row.effective_to.isoformat() if row.effective_to else "",
                    row.version,
                    citation.source_name,
                    citation.source_sha256,
                    citation.line_no,
                    citation.file_version or "",
                ),
            )

    def record_clause(self, clause: Any) -> None:
        """落一条制度条款（条款正文 + 通知对象 / 时限 / 记录去向 + 出处）。"""
        citation = clause.citation
        with self._transaction():
            self._conn.execute(
                """
                INSERT OR REPLACE INTO clauses (
                    source_name, source_sha256, section_title, body, notify_target,
                    deadline_text, deadline_minutes, record_destination,
                    paragraph_no, line_no, file_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    citation.source_name,
                    citation.source_sha256,
                    clause.section_title,
                    clause.body,
                    clause.notify_target,
                    clause.deadline_text,
                    clause.deadline_minutes,
                    clause.record_destination,
                    citation.paragraph_no,
                    citation.line_no,
                    citation.file_version or "",
                ),
            )

    # ------------------------------------------------------------------
    # 处置链节点级轨迹
    # ------------------------------------------------------------------
    def record_flow_trace(self, event_id: str, trace: Any) -> int:
        """落一条**处置链节点轨迹**，返回行号。

        一条事件一个节点一行 —— 记的是「这一步花了多久、读到了什么、
        结论是什么、为什么停」。回答「这条事件在哪一步停下、为什么」
        靠的就是这张表。

        摘要**已脱敏**（患者标识层字段不进表），这里只做最后一道兜底：
        万一调用方塞了带患者标识的键，拦下来不写。
        """
        from critval.flow.trace import redact_text

        def _clean(value: Any) -> str:
            return redact_text("" if value is None else str(value))

        with self._transaction():
            cursor = self._conn.execute(
                """
                INSERT INTO flow_traces (
                    event_id, seq, node, component, status, conclusion,
                    elapsed_ms, input_summary, output_summary, error, retryable, at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    int(getattr(trace, "seq", 0) or 0),
                    _clean(getattr(trace, "node", "")),
                    _clean(getattr(trace, "component", "")),
                    _clean(getattr(trace, "status", "")),
                    _clean(getattr(trace, "conclusion", "")),
                    float(getattr(trace, "elapsed_ms", 0.0) or 0.0),
                    _clean(getattr(trace, "input_summary", "")),
                    _clean(getattr(trace, "output_summary", "")),
                    _clean(getattr(trace, "error", "")),
                    1 if getattr(trace, "retryable", False) else 0,
                    _clean(getattr(trace, "at", "")),
                ),
            )
            return int(cursor.lastrowid or 0)

    def flow_traces_of(self, event_id: str) -> list[dict[str, Any]]:
        """一条事件的节点级轨迹（按步序）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM flow_traces WHERE event_id = ? ORDER BY seq, trace_id",
                (event_id,),
            ).fetchall()
            return [dict(row) for row in rows]

    def flow_trace_count(self, event_id: str = "") -> int:
        """轨迹条数（给了事件 ID 就只数这一条事件的）。"""
        with self._lock:
            if event_id:
                row = self._conn.execute(
                    "SELECT COUNT(*) AS n FROM flow_traces WHERE event_id = ?",
                    (event_id,),
                ).fetchone()
            else:
                row = self._conn.execute("SELECT COUNT(*) AS n FROM flow_traces").fetchone()
            return int(row["n"] if row else 0)

    # ------------------------------------------------------------------
    # 审计
    # ------------------------------------------------------------------
    def append_audit(
        self,
        action: str,
        *,
        event_id: str = "",
        actor: str = "",
        dept_code: str = "",
        specimen_no: str = "",
        item_code: str = "",
        message_control_id: str = "",
        detail: str = "",
        at: Optional[datetime] = None,
    ) -> int:
        """追加一条审计流水，返回流水号。

        审计只落**标本号 / 项目码 / 时间 / 科室 / 角色 / 消息控制号**，不落患者明细。

        落库时同步算**链式哈希**（任务 id 40）：本条内容哈希 + 上一条的
        链哈希。哈希与落行在同一个写事务里完成，``prev_hash`` 取的是当
        前链头 —— 并发写入由 ``BEGIN IMMEDIATE`` 的写锁串行化，不会算出
        两条抢同一个 ``prev_hash`` 的行。
        """
        moment = (at or datetime.now(timezone.utc)).isoformat()
        with self._transaction():
            return self._insert_audit_row(
                action,
                at=moment,
                event_id=event_id,
                actor=actor,
                dept_code=dept_code,
                specimen_no=specimen_no,
                item_code=item_code,
                message_control_id=message_control_id,
                detail=detail,
            )

    def _insert_audit_row(
        self,
        action: str,
        *,
        at: str,
        event_id: str = "",
        actor: str = "",
        dept_code: str = "",
        specimen_no: str = "",
        item_code: str = "",
        message_control_id: str = "",
        detail: str = "",
    ) -> int:
        """在**当前写事务**里插入一条带链哈希的审计行，返回流水号。

        调用方必须已经持有写事务（本方法自己不再开事务），这样
        「取链头 → 算内容哈希 → 算链哈希 → 落行」四步是原子的。
        """
        head = self._conn.execute(
            "SELECT seq, chain_hash FROM audit ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        if head is None:
            next_seq = 1
            prev_hash = GENESIS_HASH
        else:
            next_seq = int(head["seq"]) + 1
            prev_hash = str(head["chain_hash"] or GENESIS_HASH)

        values = {
            "seq": next_seq,
            "at": at,
            "action": action,
            "event_id": event_id,
            "actor": actor,
            "dept_code": dept_code,
            "specimen_no": specimen_no,
            "item_code": item_code,
            "message_control_id": message_control_id,
            "detail": detail,
        }
        payload_hash = audit_payload_hash(values)
        chain_hash = audit_chain_hash(prev_hash, payload_hash)
        cursor = self._conn.execute(
            """
            INSERT INTO audit (
                seq, at, action, event_id, actor, dept_code, specimen_no, item_code,
                message_control_id, detail, prev_hash, payload_hash, chain_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                next_seq,
                at,
                action,
                event_id,
                actor,
                dept_code,
                specimen_no,
                item_code,
                message_control_id,
                detail,
                prev_hash,
                payload_hash,
                chain_hash,
            ),
        )
        return int(cursor.lastrowid or next_seq)

    def audit_entries(self, event_id: str = "") -> list[dict[str, Any]]:
        """查审计流水（可按事件过滤）。"""
        with self._lock:
            if event_id:
                rows = self._conn.execute(
                    "SELECT * FROM audit WHERE event_id = ? ORDER BY seq",
                    (event_id,),
                ).fetchall()
            else:
                rows = self._conn.execute("SELECT * FROM audit ORDER BY seq").fetchall()
            return [dict(row) for row in rows]

    def append_audit_once(
        self,
        action: str,
        *,
        event_id: str = "",
        actor: str = "",
        dept_code: str = "",
        specimen_no: str = "",
        item_code: str = "",
        message_control_id: str = "",
        detail: str = "",
        at: Optional[datetime] = None,
    ) -> bool:
        """同一条动作对同一事件**只记一次**；返回 ``True`` = 本次真的写了。

        为什么要有这个：并发投递时同一帧可能被 8 条线程同时收到，
        8 条 ``event_duplicate`` 审计会把真正的状态变化淹掉。
        口径与「重推不灌大、去重事件数另计」一致 ——
        **流水记「这件事发生过」，重复的次数由幂等键命中计数承载**。

        判重与写入在**同一个写事务**里完成（``BEGIN IMMEDIATE`` 先拿写锁），
        所以并发线程里只有一个能把这条流水写进去；审计表是 append-only，
        事后用 UPDATE 去重做不到，判重只能落在写入这一刻。
        """
        moment = (at or datetime.now(timezone.utc)).isoformat()
        with self._transaction():
            existing = self._conn.execute(
                "SELECT seq FROM audit WHERE action = ? AND event_id = ? LIMIT 1",
                (action, event_id),
            ).fetchone()
            if existing is not None:
                return False
            self._insert_audit_row(
                action,
                at=moment,
                event_id=event_id,
                actor=actor,
                dept_code=dept_code,
                specimen_no=specimen_no,
                item_code=item_code,
                message_control_id=message_control_id,
                detail=detail,
            )
            return True

    # ------------------------------------------------------------------
    # 通知记录（推送尝试 · 追加式）
    # ------------------------------------------------------------------
    def record_notification(
        self,
        event_id: str,
        *,
        kind: str = "initial",
        attempt: int = 1,
        channel: str = "",
        card_id: str = "",
        notified_at: Optional[datetime] = None,
        due_at: str = "",
        detail: str = "",
    ) -> int:
        """追加一条**推送尝试**记录，返回记录号。

        每次推送尝试一行（``kind=initial`` 首次 / ``kind=retry`` 重推），
        **原行不改**：重推不会覆盖首次推送那条记录。
        """
        moment = notified_at or datetime.now(timezone.utc)
        with self._transaction():
            cursor = self._conn.execute(
                """
                INSERT INTO notifications (
                    event_id, card_id, channel, kind, attempt, notified_at, due_at, detail
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    card_id,
                    channel,
                    kind,
                    int(attempt),
                    moment.isoformat(),
                    due_at,
                    detail,
                ),
            )
            return int(cursor.lastrowid or 0)

    def notifications_of(self, event_id: str) -> list[dict[str, Any]]:
        """一条事件的全部推送记录（按时间顺序）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM notifications WHERE event_id = ? ORDER BY notification_id",
                (event_id,),
            ).fetchall()
            return [dict(row) for row in rows]

    def bump_retry(
        self,
        event_id: str,
        *,
        retries: int,
        at: Optional[datetime] = None,
    ) -> int:
        """记一次重推：``retry_count`` 记成「已重推几次」，``last_notified_at`` 更新。

        **首次推送时刻 ``notified_at`` 不被覆盖** —— 重推是同一次通知的
        后续尝试，不是新的一次通知。
        """
        moment = at or datetime.now(timezone.utc)
        with self._transaction():
            self._conn.execute(
                """
                UPDATE events
                   SET retry_count = ?, last_notified_at = ?
                 WHERE event_id = ?
                """,
                (int(retries), moment.isoformat(), event_id),
            )
        return int(retries)

    def notify_stats(self) -> dict[str, Any]:
        """推送统计（复核用）。

        ``notified_events`` 按**去重事件数**算 —— 重推不把「今天推送了
        多少条」灌大；``attempts`` 是真实推送次数（含重推）。
        """
        with self._lock:
            events_row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM events WHERE notified_at != ''"
            ).fetchone()
            attempts_row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM notifications"
            ).fetchone()
            retries_row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM notifications WHERE kind = 'retry'"
            ).fetchone()
            events_row2 = self._conn.execute(
                "SELECT COUNT(DISTINCT event_id) AS n FROM notifications"
            ).fetchone()
        return {
            "notified_events": int(events_row["n"] if events_row else 0),
            "notified_events_with_records": int(events_row2["n"] if events_row2 else 0),
            "attempts": int(attempts_row["n"] if attempts_row else 0),
            "retries": int(retries_row["n"] if retries_row else 0),
        }

    # ------------------------------------------------------------------
    # 投递可靠性（任务 id 42）：逐次投递记录 / 死信 / 重复投递抑制
    # ------------------------------------------------------------------
    def record_delivery_attempt(
        self,
        event_id: str,
        *,
        card_id: str = "",
        kind: str = "initial",
        round_key: str = "",
        attempt: int = 1,
        channel: str = "",
        ok: bool = False,
        status_code: int = 0,
        elapsed_ms: int = 0,
        waited_ms: int = 0,
        error: str = "",
        attempted_at: Optional[datetime] = None,
    ) -> int:
        """追加一条**投递尝试**记录，返回记录号。

        每一次出站调用一行，**原行不改** —— 「推了几次、每次什么状态码、
        退避等了多久」全部看得见。
        """
        moment = (attempted_at or datetime.now(timezone.utc)).isoformat()
        with self._transaction():
            cursor = self._conn.execute(
                """
                INSERT INTO notify_deliveries (
                    event_id, card_id, kind, round_key, attempt, channel, ok,
                    status_code, elapsed_ms, waited_ms, error, attempted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    card_id,
                    kind,
                    round_key,
                    int(attempt),
                    channel,
                    1 if ok else 0,
                    int(status_code),
                    int(elapsed_ms),
                    int(waited_ms),
                    str(error or ""),
                    moment,
                ),
            )
            return int(cursor.lastrowid or 0)

    def delivery_attempts_of(self, event_id: str) -> list[dict[str, Any]]:
        """一条事件的全部投递尝试（按时间顺序）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM notify_deliveries WHERE event_id = ? ORDER BY delivery_id",
                (event_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def delivery_attempts(self, limit: int = 0) -> list[dict[str, Any]]:
        """全部投递尝试（``limit<=0`` 表示不限量）。"""
        sql = "SELECT * FROM notify_deliveries ORDER BY delivery_id"
        params: list[Any] = []
        if limit and limit > 0:
            sql += " LIMIT ?"
            params.append(int(limit))
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def record_dead_letter(
        self,
        event_id: str,
        *,
        card_id: str = "",
        kind: str = "initial",
        round_key: str = "",
        channel: str = "",
        attempts: int = 0,
        last_error: str = "",
        card_content: str = "",
        payload: str = "",
        created_at: Optional[datetime] = None,
    ) -> int:
        """落一条**死信**：重试耗尽后原卡内容与最后一次错误留档。

        事件本身不丢 —— 它仍在 ``events`` 表里（状态落 ``failed`` 等人处理）。
        死信表是「这条推不出去」的专门落点：值班台与复核从这里看
        「哪条没推出去、最后报了什么错、原卡长什么样」。
        """
        moment = (created_at or datetime.now(timezone.utc)).isoformat()
        with self._transaction():
            cursor = self._conn.execute(
                """
                INSERT INTO notify_dead_letters (
                    event_id, card_id, kind, round_key, channel, attempts,
                    last_error, card_content, payload, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    card_id,
                    kind,
                    round_key,
                    channel,
                    int(attempts),
                    str(last_error or ""),
                    str(card_content or ""),
                    str(payload or ""),
                    moment,
                ),
            )
            return int(cursor.lastrowid or 0)

    def dead_letters_of(self, event_id: str) -> list[dict[str, Any]]:
        """一条事件的全部死信（按落档顺序）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM notify_dead_letters WHERE event_id = ? ORDER BY dead_letter_id",
                (event_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def dead_letters(self, limit: int = 0) -> list[dict[str, Any]]:
        """全部死信（``limit<=0`` 表示不限量）。"""
        sql = "SELECT * FROM notify_dead_letters ORDER BY dead_letter_id"
        params: list[Any] = []
        if limit and limit > 0:
            sql += " LIMIT ?"
            params.append(int(limit))
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def claim_delivery_round(
        self,
        round_key: str,
        *,
        event_id: str = "",
        kind: str = "",
        attempt_round: str = "",
        channel: str = "",
        stale_after_seconds: int = 300,
    ) -> bool:
        """抢占一个**投递轮次**（重复投递抑制的物理落点）。

        返回 ``True`` = 本次由我推送；``False`` = 该轮次已被推过（或正在被推），
        调用方**直接跳过** —— 这就是「同一事件在同一轮次内只推一次」。

        三种既有状态的处理：

        * ``delivered`` —— 已推成功，**不再推**（抑制）；
        * ``in_flight`` —— 正在推；若已超过陈旧窗口（进程崩了没释放），
          允许抢占，否则并发第二个推送会被挡住；
        * ``failed`` —— 上一轮推失败（已落死信），**允许重新驱动**
          （人修好 webhook 后重推同一条）。
        """
        moment = datetime.now(timezone.utc)
        with self._transaction():
            row = self._conn.execute(
                "SELECT state, updated_at FROM notify_delivery_rounds WHERE round_key = ?",
                (round_key,),
            ).fetchone()
            if row is None:
                self._conn.execute(
                    """
                    INSERT INTO notify_delivery_rounds (
                        round_key, event_id, kind, attempt_round, channel,
                        state, deliveries, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 'in_flight', 1, ?)
                    """,
                    (
                        round_key,
                        event_id,
                        kind,
                        attempt_round,
                        channel,
                        moment.isoformat(),
                    ),
                )
                return True
            state = str(row["state"] or "")
            if state == "delivered":
                return False
            if state == "in_flight" and not _is_stale(
                str(row["updated_at"] or ""), stale_after_seconds
            ):
                return False
            self._conn.execute(
                """
                UPDATE notify_delivery_rounds
                   SET state = 'in_flight', deliveries = deliveries + 1,
                       channel = ?, updated_at = ?
                 WHERE round_key = ?
                """,
                (channel, moment.isoformat(), round_key),
            )
            return True

    def mark_delivery_round(self, round_key: str, state: str) -> None:
        """把投递轮次标成终态（``delivered`` / ``failed``）。"""
        moment = datetime.now(timezone.utc)
        with self._transaction():
            self._conn.execute(
                """
                UPDATE notify_delivery_rounds
                   SET state = ?, updated_at = ?
                 WHERE round_key = ?
                """,
                (str(state or ""), moment.isoformat(), round_key),
            )

    def delivery_round_state(self, round_key: str) -> str:
        """查一个投递轮次的状态（没有这一行返回空串）。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT state FROM notify_delivery_rounds WHERE round_key = ?",
                (round_key,),
            ).fetchone()
        return str(row["state"] or "") if row is not None else ""

    def delivery_rounds(self, event_id: str = "") -> list[dict[str, Any]]:
        """投递轮次清单（可按事件过滤）—— 复核「推了几轮、每轮什么状态」。"""
        sql = "SELECT * FROM notify_delivery_rounds"
        params: list[Any] = []
        if event_id:
            sql += " WHERE event_id = ?"
            params.append(event_id)
        sql += " ORDER BY updated_at, round_key"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def idempotency_stats(self) -> dict[str, Any]:
        """幂等统计（窗口内键数 / 命中次数 / 窗口长度）。"""
        stats = self.idempotency_window.stats()
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n, COALESCE(SUM(hits), 0) AS h FROM idempotency_keys"
            ).fetchone()
        stats["db_keys"] = int(row["n"] if row else 0)
        stats["db_hits"] = int(row["h"] if row else 0)
        return stats

    # ------------------------------------------------------------------
    # 回写既有事实与尝试流水（任务 id 43）
    # ------------------------------------------------------------------
    def record_writeback_receipt(
        self,
        message_control_id: str,
        event_id: str,
        payload: Mapping[str, Any],
        *,
        payload_hash: str = "",
        http_status: int = 0,
        at: Optional[datetime] = None,
    ) -> bool:
        """登记「已成功回写」的既有事实，返回**是不是这次才写进去的**。

        **只写一次，不覆盖** —— 同一个 ``message_control_id`` 再写一次，
        既有事实原样保留（返回 ``False``）。这条就是幂等冲突检测的
        基准：拿它跟新载荷比，内容不同就是冲突，既有事实一个字都不改。
        """
        key = str(message_control_id or "").strip()
        if not key:
            return False
        moment = (at or datetime.now(timezone.utc)).isoformat()
        body = json.dumps(dict(payload or {}), ensure_ascii=False, sort_keys=True)
        with self._transaction():
            cursor = self._conn.execute(
                """
                INSERT OR IGNORE INTO writeback_receipts
                    (message_control_id, event_id, payload_json, payload_hash,
                     receipt_status, http_status, written_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    key,
                    str(event_id or ""),
                    body,
                    str(payload_hash or ""),
                    str((payload or {}).get("receipt_status") or ""),
                    int(http_status or 0),
                    moment,
                ),
            )
            return cursor.rowcount > 0

    def get_writeback_receipt(
        self, message_control_id: str
    ) -> Optional[dict[str, Any]]:
        """按幂等键取已写进去的回执行（没有返回 ``None``）。"""
        key = str(message_control_id or "").strip()
        if not key:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM writeback_receipts WHERE message_control_id = ?",
                (key,),
            ).fetchone()
        return dict(row) if row else None

    def writeback_receipts(self, limit: int = 0) -> list[dict[str, Any]]:
        """已写进去的回执行清单（复核用）。"""
        sql = "SELECT * FROM writeback_receipts ORDER BY written_at, message_control_id"
        params: list[Any] = []
        if limit and limit > 0:
            sql += " LIMIT ?"
            params.append(int(limit))
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def record_writeback_attempt(
        self,
        event_id: str,
        message_control_id: str,
        *,
        payload_hash: str = "",
        outcome: str = "",
        failure_class: str = "",
        retryable: bool = False,
        http_status: int = 0,
        error: str = "",
        detail: str = "",
        at: Optional[datetime] = None,
    ) -> int:
        """追加一条回写尝试流水，返回流水号。

        **追加式** —— 成功、契约错、幂等冲突各留一行，回答「这条回执试过
        几次、每次为什么没成」。
        """
        moment = (at or datetime.now(timezone.utc)).isoformat()
        with self._transaction():
            cursor = self._conn.execute(
                """
                INSERT INTO writeback_attempts
                    (event_id, message_control_id, payload_hash, outcome,
                     failure_class, retryable, http_status, error, detail, at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(event_id or ""),
                    str(message_control_id or ""),
                    str(payload_hash or ""),
                    str(outcome or ""),
                    str(failure_class or ""),
                    1 if retryable else 0,
                    int(http_status or 0),
                    str(error or ""),
                    str(detail or ""),
                    moment,
                ),
            )
            return int(cursor.lastrowid or 0)

    def writeback_attempts(
        self,
        message_control_id: str = "",
        event_id: str = "",
        limit: int = 0,
    ) -> list[dict[str, Any]]:
        """回写尝试流水（可按幂等键 / 事件过滤，按时间倒序）。"""
        sql = "SELECT * FROM writeback_attempts"
        clauses: list[str] = []
        params: list[Any] = []
        if message_control_id:
            clauses.append("message_control_id = ?")
            params.append(str(message_control_id))
        if event_id:
            clauses.append("event_id = ?")
            params.append(str(event_id))
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY attempt_id DESC"
        if limit and limit > 0:
            sql += " LIMIT ?"
            params.append(int(limit))
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def last_writeback_attempt(
        self, message_control_id: str
    ) -> Optional[dict[str, Any]]:
        """某个幂等键的**最近一次**回写尝试（没有返回 ``None``）。"""
        key = str(message_control_id or "").strip()
        if not key:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM writeback_attempts WHERE message_control_id = ?"
                " ORDER BY attempt_id DESC LIMIT 1",
                (key,),
            ).fetchone()
        return dict(row) if row else None

    def writeback_attempt_count(self, message_control_id: str) -> int:
        """某个幂等键已经试过几次（自动重试次数上限用得上）。"""
        key = str(message_control_id or "").strip()
        if not key:
            return 0
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM writeback_attempts WHERE message_control_id = ?",
                (key,),
            ).fetchone()
        return int(row["n"] if row else 0)

    # ------------------------------------------------------------------
    # 读
    # ------------------------------------------------------------------
    def count_events(self) -> int:
        """事件总数（幂等验收就看这个数）。"""
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()
            return int(row["n"] if row else 0)

    def get_event(self, event_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM events WHERE event_id = ?", (event_id,)
            ).fetchone()
            return dict(row) if row else None

    def list_events(self, limit: int = 100) -> list[dict[str, Any]]:
        """列事件（``limit<=0`` 表示不限量）。

        不限量这个口径是给「到期复查」用的 —— 复查要扫全量等回执的事件，
        不能只扫最近 100 条。
        """
        sql = "SELECT * FROM events ORDER BY observed_at DESC, event_id"
        params: list[Any] = []
        if limit and limit > 0:
            sql += " LIMIT ?"
            params.append(int(limit))
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
            return [dict(row) for row in rows]

    def query_rows(
        self, sql: str, params: Iterable[Any] = ()
    ) -> list[dict[str, Any]]:
        """只读查询：参数化 SQL → 字典行列表。

        **只接受 SELECT** —— 写事务一律走 ``_transaction``，这里不给写
        路径，免得「顺手在查询函数里改了点什么」。

        多维查询（``critval/ledger/query.py``）与复核报表
        （``critval/ledger/report.py``）都走这个入口：SQL 模板固定，
        **值一律进参数位**，不做字符串拼接 —— 条件值里带引号、带分号都
        只是值，不是语法。
        """
        text = str(sql or "").strip()
        if not text.upper().startswith("SELECT"):
            raise ValueError("query_rows 只接受 SELECT 语句")
        with self._lock:
            rows = self._conn.execute(text, tuple(params)).fetchall()
            return [dict(row) for row in rows]

    def citations_of(self, event_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM citations WHERE event_id = ? ORDER BY citation_id",
                (event_id,),
            ).fetchall()
            return [dict(row) for row in rows]

    def judged_events(
        self,
        *,
        observed_from: str = "",
        observed_to: str = "",
        dept_code: str = "",
        limit: int = 0,
    ) -> list[dict[str, Any]]:
        """取**已判定**的历史事件行（供改版影响面重算用）。

        **只读**：调用方拿这份行做重算与比对，**不得回写** —— 历史事实
        不可改写（任务 id 35）。时间窗按 ``observed_at`` 的 ISO 文本比较
        （同一格式才可比），边界为纯日期时由调用方补到当天首/末秒。
        """
        clauses = ["judgement_decision != ''"]
        params: list[Any] = []
        if observed_from:
            clauses.append("observed_at >= ?")
            params.append(observed_from)
        if observed_to:
            clauses.append("observed_at <= ?")
            params.append(observed_to)
        if dept_code:
            clauses.append("dept_code = ?")
            params.append(dept_code)
        sql = (
            "SELECT * FROM events WHERE "
            + " AND ".join(clauses)
            + " ORDER BY observed_at, event_id"
        )
        if limit and limit > 0:
            sql += " LIMIT ?"
            params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
            return [dict(row) for row in rows]

    def idempotency_hits(self, key: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT hits FROM idempotency_keys WHERE key = ?", (key,)
            ).fetchone()
            return int(row["hits"]) if row else 0

    def table_names(self) -> tuple[str, ...]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            ).fetchall()
            return tuple(str(row["name"]) for row in rows)

    # ------------------------------------------------------------------
    # 体检（doctor 用）
    # ------------------------------------------------------------------
    def pragmas(self) -> dict[str, Any]:
        """当前连接的 SQLite 运行参数（并发加固是否真的开着的证据）。"""
        with self._lock:
            def _pragma(name: str) -> Any:
                row = self._conn.execute(f"PRAGMA {name}").fetchone()
                return row[0] if row is not None else None

            journal = _pragma("journal_mode")
            busy = _pragma("busy_timeout")
            fk = _pragma("foreign_keys")
            sync = _pragma("synchronous")
        return {
            "journal_mode": str(journal or ""),
            "busy_timeout": int(busy or 0),
            "foreign_keys": int(fk or 0),
            "synchronous": int(sync or 0),
        }

    def status_counts(self) -> dict[str, int]:
        """各状态的事件数（值班台首页与 doctor 都看这个分布）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) AS n FROM events GROUP BY status"
            ).fetchall()
        return {str(row["status"]): int(row["n"]) for row in rows}

    def idempotency_index_present(self) -> bool:
        """幂等键上的**唯一**索引在不在（跨进程去重权威）。

        列级 UNIQUE 会生成 ``sqlite_autoindex_*``，显式索引是
        ``idx_events_idempotency`` —— 两者都算数。
        """
        with self._lock:
            indexes = self._conn.execute("PRAGMA index_list(events)").fetchall()
            for row in indexes:
                if int(row["unique"] or 0) != 1:
                    continue
                name = str(row["name"] or "")
                if name == "idx_events_idempotency":
                    return True
                if name.startswith("sqlite_autoindex"):
                    columns = self._conn.execute(
                        f'PRAGMA index_info("{name}")'
                    ).fetchall()
                    names = [str(col["name"] or "") for col in columns]
                    if names == ["idempotency_key"]:
                        return True
        return False

    def integrity_check(self) -> tuple[bool, str]:
        """``PRAGMA integrity_check``：库有没有损坏（并发写竞争的体检项）。"""
        with self._lock:
            rows = self._conn.execute("PRAGMA integrity_check").fetchall()
        messages = [str(row[0]) for row in rows]
        text = "；".join(messages) if messages else ""
        ok = bool(messages) and all(item.lower() == "ok" for item in messages)
        return ok, text
