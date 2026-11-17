"""复核报表：日报 / 周报（次日复核的真实需求 · 任务 id 37）。

**谁在什么时候用**：检验科组长每天早上要回答三个问题 ——
「昨天一共报了几条危急值」「哪些到现在还没人确认」「哪几条卡在回写上」。
纸质登记本答不了这个：登记本只记「已通知」，不记「谁确认的、什么时候
确认的、依据是哪一行」。

**三个视角**（同一批数据，切三刀）：

* **按科室** —— 哪个科室报得多、哪个科室的处置卡超时最多；
* **按项目** —— 哪些检验项目是本院的告急重灾区（改阈值表时要看）；
* **按依据条款** —— 哪些事件是靠同一条阈值依据行判的（依据改了，
  影响面就在这张表上）。

**三类异常计数**（复核时真正要盯的）：

* **超时未确认** —— 到期时刻已过，人还没确认（``due_at`` 已过且未确认）；
* **待回写** —— 人已确认，但回执还没写进登记系统；
* **失败** —— 推送重试耗尽（``failed``）或回写失败（``writeback_failed``）。

**口径写在纸上，不埋在代码里**：报表里每一列都有说明（导出 xlsx 的
「列说明」页就是这张表），拿给别人看时不必再解释一遍「超期」到底含不含
已确认的那几条 —— 本模块里**「超期」= 到期时刻已过（不论是否已确认）**，
「超时未确认」= 超期 **且** 仍未确认，两个口径分开数。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from critval.judge.critical import DECISION_CRITICAL, DECISION_NOT_CRITICAL
from critval.ledger.query import LedgerQuery, query_events
from critval.ledger.states import (
    STATUS_ACKNOWLEDGED,
    STATUS_ARCHIVED,
    STATUS_FAILED,
    STATUS_REGISTERED,
    STATUS_TRANSFERRED_TO_HUMAN,
    STATUS_WRITEBACK_FAILED,
    describe_status,
)

__all__ = [
    "AnomalyCounts",
    "DETAIL_COLUMNS",
    "DetailColumn",
    "PerspectiveRow",
    "ReviewReport",
    "SUMMARY_COLUMNS",
    "build_report",
    "build_daily_report",
    "build_weekly_report",
    "detail_rows",
    "summary_rows",
]


#: 状态集合：已确认（含已回写、已归档）。
_ACKNOWLEDGED_STATUSES = frozenset({STATUS_ACKNOWLEDGED, STATUS_REGISTERED, STATUS_ARCHIVED})


@dataclass(frozen=True)
class DetailColumn:
    """明细表的一列：表头 + 含义说明 + 列宽。"""

    key: str
    header: str
    note: str
    width: int = 18


#: 明细表列定义 —— CSV、xlsx、列说明页**共用这一份**，三处口径不会漂。
DETAIL_COLUMNS: tuple[DetailColumn, ...] = (
    DetailColumn("observed_at", "观察时刻", "危急值结果的观察时刻（日报按观察日期切）", 22),
    DetailColumn("dept_code", "科室", "结果所属科室码", 12),
    DetailColumn("item_code", "项目码", "检验项目码（阈值表主键）", 12),
    DetailColumn("item_name", "项目名", "检验项目名称", 16),
    DetailColumn("value", "结果值", "检验结果原值（未换算）", 12),
    DetailColumn("unit", "单位", "结果单位", 10),
    DetailColumn(
        "judgement_decision",
        "判定",
        "critical=危急 / not_critical=不危急 / 空=尚未判定",
        16,
    ),
    DetailColumn("status", "状态", "台账状态机当前状态", 20),
    DetailColumn(
        "threshold_row",
        "依据行",
        "命中的阈值依据行（文件名 · 行号 · 版本）",
        34,
    ),
    DetailColumn("notify_target", "通知对象", "当班人与科室", 18),
    DetailColumn("due_minutes", "上报时限(分钟)", "制度里写的上报时限", 16),
    DetailColumn("due_at", "到期时刻", "到期未回执即触发复查重推", 22),
    DetailColumn("overdue", "是否超期", "到期时刻已过（不论是否已确认）", 12),
    DetailColumn("acknowledged_by", "确认人角色", "值班人确认时记录的角色", 14),
    DetailColumn("acknowledged_at", "确认时刻", "人工确认时刻", 22),
    DetailColumn("registered_at", "回写时刻", "回执写进登记系统成功的时刻", 22),
    DetailColumn(
        "message_control_id",
        "消息控制号",
        "HL7 MSH-10（事件幂等键的一部分）",
        18,
    ),
    DetailColumn("event_id", "事件号", "台账事件号", 26),
)


#: 汇总表列定义（视角 / 分组 / 计数）。
SUMMARY_COLUMNS: tuple[DetailColumn, ...] = (
    DetailColumn("perspective", "视角", "按科室 / 按项目 / 按依据条款", 14),
    DetailColumn("key", "分组", "分组键（科室码 / 项目码 / 依据行定位）", 34),
    DetailColumn("label", "显示名", "给人看的名称", 30),
    DetailColumn("total", "合计", "该分组的事件条数", 10),
    DetailColumn("critical", "危急", "判定为危急的条数", 10),
    DetailColumn("acknowledged", "已确认", "值班人已确认（含已回写、已归档）", 12),
    DetailColumn("registered", "已回写", "回执已写进登记系统", 10),
    DetailColumn("overdue_unconfirmed", "超时未确认", "到期时刻已过且仍未确认", 14),
    DetailColumn("pending_writeback", "待回写", "已确认但尚未回写登记系统", 12),
    DetailColumn("failed", "失败", "推送重试耗尽 + 回写失败", 10),
)


@dataclass(frozen=True)
class AnomalyCounts:
    """三类异常计数（复核时真正要盯的）。"""

    overdue_unconfirmed: int = 0
    pending_writeback: int = 0
    notify_failed: int = 0
    writeback_failed: int = 0

    @property
    def failed(self) -> int:
        """失败合计 = 推送失败 + 回写失败。"""
        return self.notify_failed + self.writeback_failed

    def to_dict(self) -> dict[str, int]:
        return {
            "overdue_unconfirmed": self.overdue_unconfirmed,
            "pending_writeback": self.pending_writeback,
            "notify_failed": self.notify_failed,
            "writeback_failed": self.writeback_failed,
            "failed": self.failed,
        }


@dataclass(frozen=True)
class PerspectiveRow:
    """某个视角下某一分组的计数。"""

    key: str
    label: str
    total: int = 0
    critical: int = 0
    acknowledged: int = 0
    registered: int = 0
    overdue_unconfirmed: int = 0
    pending_writeback: int = 0
    failed: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "total": self.total,
            "critical": self.critical,
            "acknowledged": self.acknowledged,
            "registered": self.registered,
            "overdue_unconfirmed": self.overdue_unconfirmed,
            "pending_writeback": self.pending_writeback,
            "failed": self.failed,
        }


@dataclass(frozen=True)
class ReviewReport:
    """一份复核报表（日报或周报）。"""

    scope: str
    day: str
    period_start: str
    period_end: str
    generated_at: str
    total_events: int = 0
    critical: int = 0
    not_critical: int = 0
    transferred_to_human: int = 0
    anomalies: AnomalyCounts = AnomalyCounts()
    by_dept: tuple[PerspectiveRow, ...] = ()
    by_item: tuple[PerspectiveRow, ...] = ()
    by_basis: tuple[PerspectiveRow, ...] = ()
    events: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "day": self.day,
            "period_start": self.period_start,
            "period_end": self.period_end,
            "generated_at": self.generated_at,
            "total_events": self.total_events,
            "critical": self.critical,
            "not_critical": self.not_critical,
            "transferred_to_human": self.transferred_to_human,
            "anomalies": self.anomalies.to_dict(),
            "by_dept": [row.to_dict() for row in self.by_dept],
            "by_item": [row.to_dict() for row in self.by_item],
            "by_basis": [row.to_dict() for row in self.by_basis],
        }

    def to_text(self) -> str:
        """人读版（终端直接打印）。"""
        scope_label = "日报" if self.scope == "daily" else "周报"
        lines = [
            f"危急值复核{scope_label}（观察日期 {self.period_start} ~ {self.period_end}）",
            "──────────────────────────────",
            f"事件合计      : {self.total_events}",
            f"判定危急      : {self.critical}",
            f"判定不危急    : {self.not_critical}",
            f"转人工        : {self.transferred_to_human}",
            "异常计数：",
            f"  超时未确认  : {self.anomalies.overdue_unconfirmed}",
            f"  待回写      : {self.anomalies.pending_writeback}",
            f"  失败        : {self.anomalies.failed}"
            f"（推送失败 {self.anomalies.notify_failed} + 回写失败 {self.anomalies.writeback_failed}）",
        ]
        for title, rows in (
            ("按科室", self.by_dept),
            ("按项目", self.by_item),
            ("按依据条款", self.by_basis),
        ):
            lines.append("")
            lines.append(f"{title}：")
            if not rows:
                lines.append("  （本期无记录）")
                continue
            for row in rows:
                lines.append(
                    f"  {row.label} · 合计 {row.total} · 危急 {row.critical}"
                    f" · 已确认 {row.acknowledged} · 已回写 {row.registered}"
                    f" · 超时未确认 {row.overdue_unconfirmed}"
                    f" · 待回写 {row.pending_writeback} · 失败 {row.failed}"
                )
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# 分类口径
# ---------------------------------------------------------------------------


def _is_overdue(row: dict[str, Any], now_text: str) -> bool:
    """到期时刻已过即算超期（**不论是否已确认**）。"""
    due_at = str(row.get("due_at", "") or "")
    return bool(due_at) and due_at < now_text


def _is_unconfirmed(row: dict[str, Any]) -> bool:
    """还没确认（未确认 / 未回写 / 未归档）。"""
    return str(row.get("status", "") or "") not in _ACKNOWLEDGED_STATUSES


def classify(row: dict[str, Any], now_text: str) -> dict[str, Any]:
    """把一条事件行归到各个计数字段上。

    单独抽出来是为了**口径只有一处** —— 汇总表的每一格与异常计数都从这里
    出，不会出现「汇总说是 3 条、异常计数说是 2 条」这种事。
    """
    status = str(row.get("status", "") or "")
    decision = str(row.get("judgement_decision", "") or "")
    overdue = _is_overdue(row, now_text)
    unconfirmed = _is_unconfirmed(row)
    registered_at = str(row.get("registered_at", "") or "")
    return {
        "critical": decision == DECISION_CRITICAL,
        "not_critical": decision == DECISION_NOT_CRITICAL,
        "transferred_to_human": status == STATUS_TRANSFERRED_TO_HUMAN,
        "acknowledged": status in _ACKNOWLEDGED_STATUSES,
        "registered": bool(registered_at),
        "overdue_unconfirmed": overdue and unconfirmed,
        "pending_writeback": status == STATUS_ACKNOWLEDGED and not registered_at,
        "notify_failed": status == STATUS_FAILED,
        "writeback_failed": status == STATUS_WRITEBACK_FAILED,
    }


def _bucket(
    rows: list[dict[str, Any]],
    key_of: Any,
    label_of: Any,
    now_text: str,
) -> tuple[PerspectiveRow, ...]:
    """按某个键分组计数（组内计数与总计数同口径）。"""
    buckets: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for row in rows:
        key = str(key_of(row) or "")
        if key not in buckets:
            buckets[key] = PerspectiveRow(key=key, label=str(label_of(row) or key))
            order.append(key)
        marks = classify(row, now_text)
        current = buckets[key]
        buckets[key] = PerspectiveRow(
            key=current.key,
            label=current.label,
            total=current.total + 1,
            critical=current.critical + int(marks["critical"]),
            acknowledged=current.acknowledged + int(marks["acknowledged"]),
            registered=current.registered + int(marks["registered"]),
            overdue_unconfirmed=current.overdue_unconfirmed
            + int(marks["overdue_unconfirmed"]),
            pending_writeback=current.pending_writeback + int(marks["pending_writeback"]),
            failed=current.failed
            + int(marks["notify_failed"])
            + int(marks["writeback_failed"]),
        )
    ordered = sorted(
        buckets.values(), key=lambda item: (-item.total, item.key)
    )
    return tuple(ordered)


# ---------------------------------------------------------------------------
# 报表构建
# ---------------------------------------------------------------------------


def _period(scope: str, day: str) -> tuple[str, str]:
    """报表区间：日报 = 当天；周报 = 截至该日的 7 天。"""
    anchor = date.fromisoformat(day)
    if scope == "weekly":
        start = anchor - timedelta(days=6)
    else:
        start = anchor
    return start.isoformat(), anchor.isoformat()


def build_report(
    store: Any,
    scope: str = "daily",
    day: str = "",
    *,
    now: Optional[datetime] = None,
    critical_only: bool = False,
) -> ReviewReport:
    """构建一份复核报表。

    ``day`` 给的是**观察日期**（``YYYY-MM-DD``），默认今天。
    ``now`` 决定「超期」的判定时刻，默认当前时间 —— 测试里把它钉住，
    报表就是可复算的。
    """
    if scope not in ("daily", "weekly"):
        raise ValueError(f"未知报表口径 {scope!r}（支持 daily / weekly）")
    moment = now or datetime.now(timezone.utc)
    day = day or moment.date().isoformat()
    day_from, day_to = _period(scope, day)
    now_text = moment.isoformat()

    query = LedgerQuery(day_from=day_from, day_to=day_to, critical_only=critical_only)
    rows = query_events(store, query, now=moment)

    critical = 0
    not_critical = 0
    transferred = 0
    overdue_unconfirmed = 0
    pending_writeback = 0
    notify_failed = 0
    writeback_failed = 0
    for row in rows:
        marks = classify(row, now_text)
        critical += int(marks["critical"])
        not_critical += int(marks["not_critical"])
        transferred += int(marks["transferred_to_human"])
        overdue_unconfirmed += int(marks["overdue_unconfirmed"])
        pending_writeback += int(marks["pending_writeback"])
        notify_failed += int(marks["notify_failed"])
        writeback_failed += int(marks["writeback_failed"])

    by_dept = _bucket(
        rows,
        lambda row: row.get("dept_code") or "（未填科室）",
        lambda row: row.get("dept_code") or "（未填科室）",
        now_text,
    )
    by_item = _bucket(
        rows,
        lambda row: row.get("item_code") or "（未填项目码）",
        lambda row: " ".join(
            part
            for part in (str(row.get("item_code") or ""), str(row.get("item_name") or ""))
            if part
        )
        or "（未填项目码）",
        now_text,
    )
    by_basis = _bucket(
        rows,
        lambda row: row.get("threshold_row") or "（未命中依据）",
        lambda row: row.get("threshold_row") or "（未命中依据）",
        now_text,
    )

    return ReviewReport(
        scope=scope,
        day=day,
        period_start=day_from,
        period_end=day_to,
        generated_at=now_text,
        total_events=len(rows),
        critical=critical,
        not_critical=not_critical,
        transferred_to_human=transferred,
        anomalies=AnomalyCounts(
            overdue_unconfirmed=overdue_unconfirmed,
            pending_writeback=pending_writeback,
            notify_failed=notify_failed,
            writeback_failed=writeback_failed,
        ),
        by_dept=by_dept,
        by_item=by_item,
        by_basis=by_basis,
        events=tuple(dict(row) for row in rows),
    )


def build_daily_report(
    store: Any, day: str = "", *, now: Optional[datetime] = None
) -> ReviewReport:
    """日报（观察日期 = ``day``，默认今天）。"""
    return build_report(store, "daily", day, now=now)


def build_weekly_report(
    store: Any, day: str = "", *, now: Optional[datetime] = None
) -> ReviewReport:
    """周报（截至 ``day`` 的 7 天）。"""
    return build_report(store, "weekly", day, now=now)


# ---------------------------------------------------------------------------
# 导出用的两张表
# ---------------------------------------------------------------------------


def detail_rows(report: ReviewReport) -> list[dict[str, Any]]:
    """明细表（每条事件一行）。

    ``overdue`` 是**算出来的列**（库里没有这一列），其余各列直接取台账。
    """
    now_text = report.generated_at
    rows: list[dict[str, Any]] = []
    for event in report.events:
        row = dict(event)
        row["overdue"] = "是" if _is_overdue(event, now_text) else "否"
        row["status_label"] = describe_status(str(event.get("status", "") or ""))
        rows.append(row)
    return rows


def summary_rows(report: ReviewReport) -> list[dict[str, Any]]:
    """汇总表（三个视角铺平成一张长表）。"""
    rows: list[dict[str, Any]] = []
    for perspective, bucket in (
        ("按科室", report.by_dept),
        ("按项目", report.by_item),
        ("按依据条款", report.by_basis),
    ):
        for item in bucket:
            row = item.to_dict()
            row["perspective"] = perspective
            rows.append(row)
    return rows
