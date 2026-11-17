"""台账体检（``doctor`` 子命令的落点）。

值班台第二天早上开机第一件事是问「昨晚这台机器上的台账还好吗」。本模块
回答的不是「跑没跑」，而是**四件能核对的事**：

1. **库还完整吗** —— ``PRAGMA integrity_check``；并发写竞争把库写坏时
   这里会直接报出来（而不是等到某条事件查不出来才发现）。
2. **并发加固开着吗** —— WAL / busy_timeout / 幂等键唯一索引。
   这三项没开，守护进程与 CLI 同时开库就会互相打架、重复投递挡不住。
3. **事件都走到哪一步了** —— 各状态分布 + 待办计数（等回执的、
   回写失败的、转人工的、没推送的）。这些是**要人处理**的事，
   不是「异常」—— 体检报告把它们点出来，但不因此判失败。
4. **现在用的是哪份配置与哪版规则** —— 配置版本号 + 规则集快照 ID。
   事后复核「这条当时按哪版阈值表判的」靠的就是这两个号。

**体检不替人做决定**：发现的问题按严重度分三级 —— ``error``（必须处理，
退出码非零）、``warn``（要盯着）、``info``（当前状态说明）。
只有 ``error`` 会让 ``doctor`` 非零退出。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from critval.ledger.states import (
    STATUS_CARD_BUILT,
    STATUS_FAILED,
    STATUS_JUDGED,
    STATUS_NOT_CRITICAL,
    STATUS_PENDING_ACK,
    STATUS_RECEIVED,
    STATUS_REGISTERED,
    STATUS_TRANSFERRED_TO_HUMAN,
    STATUS_WRITEBACK_FAILED,
)

__all__ = [
    "LEVEL_ERROR",
    "LEVEL_INFO",
    "LEVEL_WARN",
    "DoctorFinding",
    "DoctorReport",
    "run_doctor",
]

LEVEL_ERROR = "error"
LEVEL_WARN = "warn"
LEVEL_INFO = "info"

#: 待办口径：状态 → 人话标签。
_PENDING_LABELS: dict[str, str] = {
    STATUS_PENDING_ACK: "已推送待回执",
    STATUS_WRITEBACK_FAILED: "回写失败待补写",
    STATUS_FAILED: "推送重试耗尽待人工",
    STATUS_TRANSFERRED_TO_HUMAN: "转人工待接手",
    STATUS_CARD_BUILT: "已出卡未推送",
    STATUS_RECEIVED: "已接收未判定",
    STATUS_JUDGED: "已判定未出卡",
}


@dataclass(frozen=True)
class DoctorFinding:
    """一条体检结论。"""

    level: str
    code: str
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {"level": self.level, "code": self.code, "message": self.message}


@dataclass
class DoctorReport:
    """体检报告。"""

    db_path: str
    generated_at: str
    findings: list[DoctorFinding] = field(default_factory=list)
    status_counts: dict[str, int] = field(default_factory=dict)
    pending: dict[str, int] = field(default_factory=dict)
    pragmas: dict[str, Any] = field(default_factory=dict)
    event_total: int = 0
    config_version: int = 0
    ruleset_snapshot_id: str = ""
    db_exists: bool = True

    @property
    def errors(self) -> list[DoctorFinding]:
        return [item for item in self.findings if item.level == LEVEL_ERROR]

    @property
    def warnings(self) -> list[DoctorFinding]:
        return [item for item in self.findings if item.level == LEVEL_WARN]

    @property
    def ok(self) -> bool:
        """没有 ``error`` 级问题即为通过（warn 不影响退出码）。"""
        return not self.errors

    def counts(self) -> dict[str, int]:
        counts = {LEVEL_ERROR: 0, LEVEL_WARN: 0, LEVEL_INFO: 0}
        for item in self.findings:
            counts[item.level] = counts.get(item.level, 0) + 1
        return counts

    def to_dict(self) -> dict[str, Any]:
        return {
            "db_path": self.db_path,
            "generated_at": self.generated_at,
            "db_exists": self.db_exists,
            "ok": self.ok,
            "event_total": self.event_total,
            "status_counts": dict(self.status_counts),
            "pending": dict(self.pending),
            "pragmas": dict(self.pragmas),
            "config_version": self.config_version,
            "ruleset_snapshot_id": self.ruleset_snapshot_id,
            "findings": [item.to_dict() for item in self.findings],
            "counts": self.counts(),
        }

    def to_text(self) -> str:
        lines = [
            "危急值台账体检",
            "──────────────────────────────",
            f"台账路径   : {self.db_path}",
            f"体检时刻   : {self.generated_at}",
            f"事件总数   : {self.event_total} 条",
            f"配置版本   : {self.config_version}",
            f"规则快照   : {self.ruleset_snapshot_id or '（未装载规则集）'}",
        ]
        pragmas = self.pragmas
        if pragmas:
            lines.append(
                f"并发加固   : journal_mode={pragmas.get('journal_mode', '?')}"
                f" · busy_timeout={pragmas.get('busy_timeout', '?')}ms"
                f" · foreign_keys={pragmas.get('foreign_keys', '?')}"
            )
        lines.append("──────────────────────────────")
        lines.append("状态分布")
        if self.status_counts:
            for status, count in sorted(self.status_counts.items()):
                lines.append(f"  · {status:<22} {count}")
        else:
            lines.append("  · （台账里还没有事件）")
        lines.append("──────────────────────────────")
        lines.append("待办计数")
        if self.pending:
            for status, count in sorted(self.pending.items()):
                label = _PENDING_LABELS.get(status, status)
                lines.append(f"  · {label:<16} {count}")
        else:
            lines.append("  · （没有待办）")
        lines.append("──────────────────────────────")
        lines.append("体检结论")
        if not self.findings:
            lines.append("  · 未发现问题")
        for item in self.findings:
            marker = {"error": "✘", "warn": "!", "info": "·"}.get(item.level, "·")
            lines.append(f"  {marker} [{item.level}] {item.message}")
        counts = self.counts()
        lines.append(
            f"小结       : 异常 {counts.get(LEVEL_ERROR, 0)} 项"
            f" · 提醒 {counts.get(LEVEL_WARN, 0)} 项"
            f" · 说明 {counts.get(LEVEL_INFO, 0)} 项"
        )
        lines.append("自检结论   : " + ("通过" if self.ok else "不通过（有必须处理的异常项）"))
        return "\n".join(lines)


def _pending_counts(
    store: Any, status_counts: dict[str, int], now: datetime
) -> dict[str, int]:
    """待办计数：按状态归类，并把「已过时限还没回执」单独点出来。"""
    pending: dict[str, int] = {}
    for status, count in status_counts.items():
        if status in _PENDING_LABELS and count:
            pending[status] = count
    overdue = 0
    for row in store.list_events(limit=0):
        status = str(row.get("status", "") or "")
        if status not in (STATUS_PENDING_ACK, "notified"):
            continue
        due_at = str(row.get("due_at", "") or "")
        if not due_at:
            continue
        try:
            moment = datetime.fromisoformat(due_at)
        except ValueError:
            continue
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        if moment <= now:
            overdue += 1
    if overdue:
        pending["overdue_ack"] = overdue
    return pending


def run_doctor(
    store: Any,
    *,
    db_path: str = "",
    config_version: int = 0,
    ruleset_snapshot_id: str = "",
    now: Optional[datetime] = None,
) -> DoctorReport:
    """跑一遍体检，返回报告。"""
    moment = now or datetime.now(timezone.utc)
    report = DoctorReport(
        db_path=db_path,
        generated_at=moment.isoformat(),
        config_version=int(config_version or 0),
        ruleset_snapshot_id=str(ruleset_snapshot_id or ""),
    )

    # ① 库完整性
    try:
        integrity_ok, integrity_text = store.integrity_check()
    except Exception as exc:  # 库打不开 —— 本身就是异常项
        report.findings.append(
            DoctorFinding(
                LEVEL_ERROR,
                "integrity_unreadable",
                f"库完整性检查失败：{exc}",
            )
        )
        return report
    if integrity_ok:
        report.findings.append(
            DoctorFinding(LEVEL_INFO, "integrity_ok", f"库完整性检查通过（{integrity_text}）")
        )
    else:
        report.findings.append(
            DoctorFinding(
                LEVEL_ERROR,
                "integrity_failed",
                f"库完整性检查未通过：{integrity_text}",
            )
        )

    # ② 并发加固
    pragmas = store.pragmas()
    report.pragmas = pragmas
    journal = str(pragmas.get("journal_mode", "") or "")
    if journal in ("wal", "memory"):
        report.findings.append(
            DoctorFinding(LEVEL_INFO, "journal_mode_ok", f"日志模式 {journal}（读写不互斥）")
        )
    else:
        report.findings.append(
            DoctorFinding(
                LEVEL_WARN,
                "journal_mode_rollback",
                f"日志模式是 {journal or '未知'}，不是 WAL —— 读写会互相阻塞",
            )
        )
    busy = int(pragmas.get("busy_timeout", 0) or 0)
    if busy > 0:
        report.findings.append(
            DoctorFinding(
                LEVEL_INFO, "busy_timeout_ok", f"写锁等待 {busy} 毫秒（跨进程并发不打架）"
            )
        )
    else:
        report.findings.append(
            DoctorFinding(
                LEVEL_ERROR,
                "busy_timeout_missing",
                "busy_timeout 为 0 —— 并发写会直接报「database is locked」",
            )
        )
    if store.idempotency_index_present():
        report.findings.append(
            DoctorFinding(
                LEVEL_INFO, "idempotency_index_ok", "幂等键唯一索引在（跨进程去重有保障）"
            )
        )
    else:
        report.findings.append(
            DoctorFinding(
                LEVEL_ERROR,
                "idempotency_index_missing",
                "幂等键上的唯一索引不在 —— 并发下同一结果会被当成两条事件",
            )
        )

    # ③ 状态分布与待办
    status_counts = store.status_counts()
    report.status_counts = status_counts
    report.event_total = sum(status_counts.values())
    report.pending = _pending_counts(store, status_counts, moment)
    if report.event_total == 0:
        report.findings.append(
            DoctorFinding(LEVEL_WARN, "ledger_empty", "台账里还没有事件 —— 确认守护进程与入站通道都通着")
        )
    overdue = report.pending.get("overdue_ack", 0)
    if overdue:
        report.findings.append(
            DoctorFinding(
                LEVEL_WARN,
                "ack_overdue",
                f"{overdue} 条已过上报时限还没回执（到期复查会重推）",
            )
        )
    failed = status_counts.get(STATUS_FAILED, 0)
    if failed:
        report.findings.append(
            DoctorFinding(
                LEVEL_WARN,
                "notify_exhausted",
                f"{failed} 条推送重试已耗尽，留在台账里等人处理",
            )
        )
    writeback_failed = status_counts.get(STATUS_WRITEBACK_FAILED, 0)
    if writeback_failed:
        report.findings.append(
            DoctorFinding(
                LEVEL_WARN,
                "writeback_failed",
                f"{writeback_failed} 条回写失败待补写（已确认回执未丢）",
            )
        )
    transferred = status_counts.get(STATUS_TRANSFERRED_TO_HUMAN, 0)
    if transferred:
        report.findings.append(
            DoctorFinding(
                LEVEL_INFO,
                "transferred_to_human",
                f"{transferred} 条转人工待接手",
            )
        )
    registered = status_counts.get(STATUS_REGISTERED, 0)
    not_critical = status_counts.get(STATUS_NOT_CRITICAL, 0)
    report.findings.append(
        DoctorFinding(
            LEVEL_INFO,
            "status_summary",
            f"已回写 {registered} 条 · 不危急 {not_critical} 条",
        )
    )

    # ④ 配置与规则版本
    if report.config_version <= 0:
        report.findings.append(
            DoctorFinding(LEVEL_WARN, "config_version_missing", "取不到配置版本号 —— 事后无法追溯当时用的哪份配置")
        )
    if not report.ruleset_snapshot_id:
        report.findings.append(
            DoctorFinding(
                LEVEL_WARN,
                "ruleset_missing",
                "未装载规则集 —— 判定引擎没有生效版本（阈值表没绑上？）",
            )
        )
    return report
