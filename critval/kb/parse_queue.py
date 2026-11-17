"""人工确认队列：未识别行 / 字段缺失行 / 未识别列 / 疑似项 —— 等人来处理。

解析报告说清了「哪里没读懂」，但**光说清楚不够**，得有人去处理。
本模块提供一条可落库、可追溯的人工确认通道：

* 条目状态只有三种：``open``（待处理） / ``confirmed``（已回填确认） /
  ``ignored``（已忽略 —— 必须写明理由）；
* 条目 ID **确定性强**（材料指纹 + 类别 + 行号/列序），同一份材料重复投递
  不会灌出重复条目；
* 回填后能触发**重解析**：确认值回到解析环节，重解析结果变了就说明回填生效；
  改不动的（比如表里根本没有那一列）会明确说「回填不生效」，不装作成功。
"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Sequence

from critval.kb.parse_report import MaterialReport

__all__ = [
    "ParseQueue",
    "ParseQueueItem",
    "QUEUE_CATEGORIES",
    "QUEUE_CONFIRMED",
    "QUEUE_IGNORED",
    "QUEUE_OPEN",
    "default_queue_path",
    "queue_items_from_report",
    "reparse_after_confirm",
]

#: 待处理。
QUEUE_OPEN = "open"
#: 已回填确认。
QUEUE_CONFIRMED = "confirmed"
#: 已忽略（须写明理由）。
QUEUE_IGNORED = "ignored"

#: 队列条目类别。
QUEUE_CATEGORIES: tuple[str, ...] = (
    "unrecognized_row",  # 一行没读成阈值行
    "incomplete_row",  # 读到了但字段缺失
    "unrecognized_column",  # 一列没认出来
    "merged_cell",  # 疑似合并单元格
    "multi_version",  # 疑似多版本拼表
)

#: 「字段缺失」原因里出现的中文名 → 规范字段名（回填时按规范字段写回）。
_FIELD_LABELS: tuple[tuple[str, str], ...] = (
    ("低危急值", "low_critical"),
    ("高危急值", "high_critical"),
    ("适用性别", "applies_sex"),
    ("适用年龄段", "applies_age"),
    ("项目码", "item_code"),
    ("项目名", "item_name"),
    ("单位", "unit"),
    ("参考区间", "reference_range"),
    ("生效日期", "effective_from"),
    ("失效日期", "effective_to"),
    ("版本号", "version"),
)


def default_queue_path() -> Path:
    """确认队列默认落盘位置（运行期数据，可再生，不进仓库）。"""
    return Path("data") / "parse_queue.db"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def infer_target_field(reason: str) -> str:
    """从「为什么没读懂」推回**该补哪个字段**（推不出就留空）。

    推不出来不是失败 —— 有些疑点本来就只能靠人看一眼表才知道
    （疑似合并单元格、疑似多版本），回填位留空由人自己判断。
    """
    for label, canonical in _FIELD_LABELS:
        if label in reason:
            return canonical
    return ""


@dataclass(frozen=True)
class ParseQueueItem:
    """一条待人工确认的疑点。"""

    queue_id: str
    source_name: str
    source_sha256: str
    category: str
    reason: str
    row_no: int = 0
    column_label: str = ""
    target_field: str = ""
    values: tuple[str, ...] = ()
    status: str = QUEUE_OPEN
    handler: str = ""
    note: str = ""
    confirmed_value: str = ""
    source_path: str = ""
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "queue_id": self.queue_id,
            "source_name": self.source_name,
            "source_sha256": self.source_sha256,
            "source_path": self.source_path,
            "category": self.category,
            "reason": self.reason,
            "row_no": self.row_no,
            "column_label": self.column_label,
            "target_field": self.target_field,
            "values": list(self.values),
            "status": self.status,
            "handler": self.handler,
            "note": self.note,
            "confirmed_value": self.confirmed_value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "ParseQueueItem":
        return cls(
            queue_id=row["queue_id"],
            source_name=row["source_name"],
            source_sha256=row["source_sha256"],
            category=row["category"],
            reason=row["reason"],
            row_no=int(row["row_no"]),
            column_label=row["column_label"],
            target_field=row["target_field"],
            values=tuple(json_loads_list(row["raw_values"])),
            status=row["status"],
            handler=row["handler"],
            note=row["note"],
            confirmed_value=row["confirmed_value"],
            source_path=row["source_path"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


def json_loads_list(raw: str) -> list[str]:
    import json

    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return [str(v) for v in payload] if isinstance(payload, list) else []


def _queue_id(source_sha256: str, category: str, key: str) -> str:
    """确定性条目 ID —— 同一份材料的同一个疑点永远是同一条。"""
    return f"pq-{source_sha256[:8]}-{category}-{key}"


def queue_items_from_report(report: MaterialReport) -> list[ParseQueueItem]:
    """把一份解析报告转成待确认条目。

    疑点分四类进队列：没读成的行、读到但缺字段的行、没认出来的列、
    疑似合并单元格 / 疑似多版本。**每一类都进队列**，不挑着进。
    """
    items: list[ParseQueueItem] = []
    sha = report.source_sha256

    for entry in report.unrecognized_rows:
        row_no = int(entry.get("row_no", 0))
        reason = str(entry.get("reason", ""))
        items.append(
            ParseQueueItem(
                queue_id=_queue_id(sha, "unrecognized_row", str(row_no)),
                source_name=report.source_name,
                source_sha256=sha,
                source_path=report.source_path,
                category="unrecognized_row",
                reason=reason,
                row_no=row_no,
                target_field=infer_target_field(reason),
                values=tuple(str(v) for v in entry.get("values", [])),
            )
        )

    for entry in report.incomplete_rows:
        row_no = int(entry.get("row_no", 0))
        reason = str(entry.get("reason", ""))
        items.append(
            ParseQueueItem(
                queue_id=_queue_id(sha, "incomplete_row", str(row_no)),
                source_name=report.source_name,
                source_sha256=sha,
                source_path=report.source_path,
                category="incomplete_row",
                reason=reason,
                row_no=row_no,
                target_field=infer_target_field(reason),
                values=tuple(str(v) for v in entry.get("values", [])),
            )
        )

    for index, label in enumerate(report.unrecognized_columns, start=1):
        items.append(
            ParseQueueItem(
                queue_id=_queue_id(sha, "unrecognized_column", f"{index:02d}"),
                source_name=report.source_name,
                source_sha256=sha,
                source_path=report.source_path,
                category="unrecognized_column",
                reason=f"表头有列认不出来：{label}（该列内容不参与判定）",
                column_label=label,
            )
        )

    for entry in report.merged_cell_rows:
        row_no = int(entry.get("row_no", 0))
        items.append(
            ParseQueueItem(
                queue_id=_queue_id(sha, "merged_cell", str(row_no)),
                source_name=report.source_name,
                source_sha256=sha,
                source_path=report.source_path,
                category="merged_cell",
                reason=str(entry.get("reason", "")),
                row_no=row_no,
                values=tuple(str(v) for v in entry.get("values", [])),
            )
        )

    for entry in report.multi_version_groups:
        code = str(entry.get("item_code", ""))
        items.append(
            ParseQueueItem(
                queue_id=_queue_id(sha, "multi_version", code or "unknown"),
                source_name=report.source_name,
                source_sha256=sha,
                source_path=report.source_path,
                category="multi_version",
                reason=str(entry.get("reason", "")),
                row_no=0,
                values=tuple(str(v) for v in entry.get("row_nos", [])),
            )
        )

    return items


_SCHEMA = """
CREATE TABLE IF NOT EXISTS parse_queue (
    queue_id TEXT PRIMARY KEY,
    source_name TEXT NOT NULL,
    source_sha256 TEXT NOT NULL,
    category TEXT NOT NULL,
    reason TEXT NOT NULL,
    row_no INTEGER NOT NULL DEFAULT 0,
    column_label TEXT NOT NULL DEFAULT '',
    target_field TEXT NOT NULL DEFAULT '',
    raw_values TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'open',
    handler TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    confirmed_value TEXT NOT NULL DEFAULT '',
    source_path TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_parse_queue_status ON parse_queue(status);
CREATE INDEX IF NOT EXISTS idx_parse_queue_source ON parse_queue(source_sha256);
"""


class ParseQueue:
    """人工确认队列（SQLite 落库）。

    入队是**幂等**的：同一条疑点重复入队不会灌出第二条；已经确认过的条目
    不会被后续的重复投递打回「待处理」 —— 人的确认结果不该被机器冲掉。
    """

    def __init__(self, path: str | Path = default_queue_path()) -> None:
        self.path = str(path)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self.initialize()

    # ---- 生命周期 -------------------------------------------------------
    def initialize(self) -> None:
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> "ParseQueue":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ---- 写 -------------------------------------------------------------
    def add_items(self, items: Sequence[ParseQueueItem]) -> tuple[int, int]:
        """入队。返回 ``(新增条数, 跳过条数)``。

        已存在且已确认 / 已忽略的条目**保持原状**（只刷新原因与取值），
        不把人处理过的结果打回待处理。
        """
        added = 0
        skipped = 0
        with self._lock:
            for item in items:
                existing = self._conn.execute(
                    "SELECT status FROM parse_queue WHERE queue_id = ?", (item.queue_id,)
                ).fetchone()
                if existing is None:
                    self._conn.execute(
                        """
                        INSERT INTO parse_queue (
                            queue_id, source_name, source_sha256, category, reason,
                            row_no, column_label, target_field, raw_values, status,
                            handler, note, confirmed_value, source_path,
                            created_at, updated_at
                        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            item.queue_id,
                            item.source_name,
                            item.source_sha256,
                            item.category,
                            item.reason,
                            item.row_no,
                            item.column_label,
                            item.target_field,
                            _dump_values(item.values),
                            item.status,
                            item.handler,
                            item.note,
                            item.confirmed_value,
                            item.source_path,
                            item.created_at,
                            item.updated_at,
                        ),
                    )
                    added += 1
                else:
                    # 已处理过的条目只更新描述性字段，状态与人处理结果保留。
                    self._conn.execute(
                        """
                        UPDATE parse_queue
                           SET reason = ?, raw_values = ?, target_field = ?, updated_at = ?
                         WHERE queue_id = ?
                        """,
                        (
                            item.reason,
                            _dump_values(item.values),
                            item.target_field,
                            _now(),
                            item.queue_id,
                        ),
                    )
                    skipped += 1
            self._conn.commit()
        return added, skipped

    def confirm(self, queue_id: str, value: str, handler: str = "", note: str = "") -> ParseQueueItem:
        """确认一条疑点（回填值 + 处理人 + 备注）。"""
        return self._close(queue_id, QUEUE_CONFIRMED, value, handler, note)

    def ignore(self, queue_id: str, handler: str = "", note: str = "") -> ParseQueueItem:
        """忽略一条疑点（必须写处理人；建议写明理由）。"""
        return self._close(queue_id, QUEUE_IGNORED, "", handler, note)

    def _close(
        self,
        queue_id: str,
        status: str,
        value: str,
        handler: str,
        note: str,
    ) -> ParseQueueItem:
        with self._lock:
            cursor = self._conn.execute(
                """
                UPDATE parse_queue
                   SET status = ?, confirmed_value = ?, handler = ?, note = ?, updated_at = ?
                 WHERE queue_id = ?
                """,
                (status, value, handler, note, _now(), queue_id),
            )
            self._conn.commit()
            if cursor.rowcount == 0:
                raise KeyError(f"确认队列里没有这条：{queue_id}")
        return self.get(queue_id)  # type: ignore[return-value]

    # ---- 读 -------------------------------------------------------------
    def get(self, queue_id: str) -> Optional[ParseQueueItem]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM parse_queue WHERE queue_id = ?", (queue_id,)
            ).fetchone()
        return ParseQueueItem.from_row(row) if row else None

    def list_items(
        self,
        status: str = "",
        source_sha256: str = "",
        category: str = "",
    ) -> list[ParseQueueItem]:
        """列出队列条目（按行号与类别排序，顺序稳定可复核）。"""
        clauses: list[str] = []
        params: list[Any] = []
        if status and status != "all":
            clauses.append("status = ?")
            params.append(status)
        if source_sha256:
            clauses.append("source_sha256 = ?")
            params.append(source_sha256)
        if category:
            clauses.append("category = ?")
            params.append(category)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"SELECT * FROM parse_queue{where} ORDER BY source_name, row_no, category, queue_id"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [ParseQueueItem.from_row(row) for row in rows]

    def stats(self) -> dict[str, Any]:
        """队列概览（按状态计数 + 按类别计数）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) AS n FROM parse_queue GROUP BY status"
            ).fetchall()
            cat_rows = self._conn.execute(
                "SELECT category, COUNT(*) AS n FROM parse_queue GROUP BY category"
            ).fetchall()
        by_status = {row["status"]: int(row["n"]) for row in rows}
        by_category = {row["category"]: int(row["n"]) for row in cat_rows}
        return {
            "total": sum(by_status.values()),
            "by_status": by_status,
            "by_category": by_category,
            "pending": by_status.get(QUEUE_OPEN, 0),
        }


def _dump_values(values: Sequence[str]) -> str:
    import json

    return json.dumps(list(values), ensure_ascii=False)


def reparse_after_confirm(
    queue: "ParseQueue",
    item: ParseQueueItem,
    value: str,
    report_store: Optional[Any] = None,
) -> dict[str, Any]:
    """回填确认后**重解析**：确认值真的写回材料再读一遍。

    重解析结果有三种，落盘前先说清是哪一种：

    * ``reparsed=True`` 且有 ``applied_overrides`` —— 回填生效，那一行读出来了；
    * ``reparsed=True`` 但 ``unapplied_overrides`` 非空 —— 表里没有那一列，
      回填**没生效**，原样说，不装作成功；
    * ``reparsed=False`` —— 材料文件不在了或读不了，原因原样带出去。

    顺带把因这次回填而不再成立的疑点关掉（状态转 ``confirmed``，
    处理人记为「系统（回填生效）」）。
    """
    from critval.kb.loader import load_threshold_table

    source_path = item.source_path
    if not source_path or not Path(source_path).exists():
        return {
            "reparsed": False,
            "reason": (
                f"材料文件不在原处（{source_path or '未记录路径'}），"
                "无法重解析 —— 请先确认材料当前路径"
            ),
            "applied_overrides": [],
            "unapplied_overrides": [],
            "resolved_rows": [],
        }

    overrides: dict[int, dict[str, Any]] = {}
    if item.target_field and item.row_no and value:
        overrides[item.row_no] = {item.target_field: value}

    table = load_threshold_table(source_path, overrides=overrides or None)
    report = MaterialReport.from_parse_report(table.report)

    # 这次回填让哪些行不再有疑点？
    previous_report = None
    if report_store is not None:
        try:
            previous_report = report_store.load(item.source_sha256)
        except Exception:  # noqa: BLE001 - 旧报告读不到不该阻断重解析
            previous_report = None
    previous_problem_rows = _problem_rows(previous_report) if previous_report else set()
    current_problem_rows = _problem_rows(report)
    resolved_rows = sorted(previous_problem_rows - current_problem_rows)

    for other in queue.list_items(source_sha256=item.source_sha256, status=QUEUE_OPEN):
        if other.queue_id == item.queue_id:
            continue
        if other.row_no and other.row_no in resolved_rows:
            queue.confirm(
                other.queue_id,
                value="",
                handler="系统（回填生效）",
                note=f"第 {other.row_no} 行在回填后重解析已识别，疑点自动关闭",
            )

    added, skipped = queue.add_items(queue_items_from_report(report))
    if report_store is not None:
        report_store.save(report)

    return {
        "reparsed": True,
        "source_path": source_path,
        "source_sha256": item.source_sha256,
        "recognized_rows": report.recognized_rows,
        "total_rows": report.total_rows,
        "unrecognized_rows": len(report.unrecognized_rows),
        "incomplete_rows": len(report.incomplete_rows),
        "merged_cell_rows": len(report.merged_cell_rows),
        "multi_version_groups": len(report.multi_version_groups),
        "applied_overrides": list(report.applied_overrides),
        "unapplied_overrides": list(report.unapplied_overrides),
        "resolved_rows": resolved_rows,
        "queue_added": added,
        "queue_skipped": skipped,
        "summary": report.summary(),
    }


def _problem_rows(report: Optional[MaterialReport]) -> set[int]:
    """报告里「有疑点的行号」集合。"""
    if report is None:
        return set()
    rows: set[int] = set()
    for entry in report.unrecognized_rows:
        rows.add(int(entry.get("row_no", 0)))
    for entry in report.incomplete_rows:
        rows.add(int(entry.get("row_no", 0)))
    return {r for r in rows if r}
