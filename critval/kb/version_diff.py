"""阈值表改版影响面（任务 id 26）。

阈值表改版不是「换个文件」那么简单 —— 检验科组长要先把**哪些项目的
判定门槛变了**看清楚，才敢签发新版本。本模块把两版阈值表逐项目对齐，
产出可复核的变更清单：

* ``added`` —— 新版新增的项目（原来没有这条兜底）；
* ``removed`` —— 新版删掉的项目（原来有兜底，改版后没了）；
* ``threshold_changed`` —— 阈值数值变了；
* ``direction_changed`` —— 危急方向变了（只判高危 ↔ 高低都判）；
* ``unit_changed`` —— 单位变了（数量级可能跟着变，口径必须复核）。

对齐口径：按「项目码 + 适用人群分档」配对 —— 同一项目按性别 / 年龄段
拆成多行时逐档比对，不会把两档混成一行。
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

from critval.models import ThresholdRow

__all__ = [
    "CHANGE_ADDED",
    "CHANGE_DIRECTION",
    "CHANGE_REMOVED",
    "CHANGE_THRESHOLD",
    "CHANGE_UNIT",
    "ItemDiff",
    "diff_rows",
    "diff_threshold_tables",
    "summarize",
    "write_csv",
]

CHANGE_ADDED = "added"
CHANGE_REMOVED = "removed"
CHANGE_THRESHOLD = "threshold_changed"
CHANGE_DIRECTION = "direction_changed"
CHANGE_UNIT = "unit_changed"

#: 变更类型 → 人读标签。
CHANGE_LABELS = {
    CHANGE_ADDED: "新增项目",
    CHANGE_REMOVED: "删除项目",
    CHANGE_THRESHOLD: "阈值变化",
    CHANGE_DIRECTION: "方向变化",
    CHANGE_UNIT: "单位变化",
}

#: CSV 表头（改版影响面清单的固定列序）。
CSV_HEADER = (
    "项目码",
    "项目名",
    "变更类型",
    "变更说明",
    "旧行号",
    "新行号",
    "旧值",
    "新值",
    "旧版本",
    "新版本",
)


@dataclass(frozen=True)
class ItemDiff:
    """一条改版变更。"""

    item_code: str
    item_name: str
    change: str
    detail: str
    old_line_no: Optional[int] = None
    new_line_no: Optional[int] = None
    old_value: str = ""
    new_value: str = ""
    old_version: str = ""
    new_version: str = ""

    @property
    def label(self) -> str:
        return CHANGE_LABELS.get(self.change, self.change)

    def to_dict(self) -> dict[str, Any]:
        return {
            "item_code": self.item_code,
            "item_name": self.item_name,
            "change": self.change,
            "label": self.label,
            "detail": self.detail,
            "old_line_no": self.old_line_no,
            "new_line_no": self.new_line_no,
            "old_value": self.old_value,
            "new_value": self.new_value,
            "old_version": self.old_version,
            "new_version": self.new_version,
        }


def _scope_key(row: ThresholdRow) -> str:
    """适用人群分档的配对键（性别 + 年龄段）。"""
    return f"{row.applies_sex}|{row.applies_age_label}"


def _threshold_text(row: ThresholdRow) -> str:
    """阈值的可读文本（用于清单里的「旧值 / 新值」）。"""
    parts: list[str] = []
    if row.low_critical is not None:
        parts.append(f"低 {row.low_critical}")
    if row.high_critical is not None:
        parts.append(f"高 {row.high_critical}")
    return " / ".join(parts) if parts else "（未给出阈值）"


def _index_rows(rows: Sequence[ThresholdRow]) -> dict[tuple[str, str], ThresholdRow]:
    """按「项目码 + 分档」建索引；同键多行时取最后一行（后写的覆盖先写的）。"""
    index: dict[tuple[str, str], ThresholdRow] = {}
    for row in rows:
        index[(row.item_code, _scope_key(row))] = row
    return index


def diff_rows(
    old_rows: Sequence[ThresholdRow],
    new_rows: Sequence[ThresholdRow],
) -> tuple[ItemDiff, ...]:
    """逐项目对齐两版阈值行，产出变更清单。"""
    old_index = _index_rows(old_rows)
    new_index = _index_rows(new_rows)

    diffs: list[ItemDiff] = []
    keys = sorted(set(old_index) | set(new_index))
    for key in keys:
        item_code, scope = key
        old_row = old_index.get(key)
        new_row = new_index.get(key)
        scope_text = ""
        reference = new_row or old_row
        if reference is not None:
            scope_parts = [
                part
                for part in (reference.applies_sex, reference.applies_age_label)
                if part
            ]
            scope_text = f"（{' · '.join(scope_parts)}）" if scope_parts else ""

        if old_row is None and new_row is not None:
            diffs.append(
                ItemDiff(
                    item_code=item_code,
                    item_name=new_row.item_name,
                    change=CHANGE_ADDED,
                    detail=(
                        f"新版新增项目{scope_text}：{new_row.threshold_text()}"
                        f"（旧版无此条，改版后新增兜底）"
                    ),
                    new_line_no=new_row.row_no,
                    new_value=_threshold_text(new_row),
                    new_version=new_row.version,
                )
            )
            continue
        if new_row is None and old_row is not None:
            diffs.append(
                ItemDiff(
                    item_code=item_code,
                    item_name=old_row.item_name,
                    change=CHANGE_REMOVED,
                    detail=(
                        f"新版删除项目{scope_text}：原 {old_row.threshold_text()}"
                        f"（改版后这条兜底没有了，须确认是否有意停用）"
                    ),
                    old_line_no=old_row.row_no,
                    old_value=_threshold_text(old_row),
                    old_version=old_row.version,
                )
            )
            continue

        assert old_row is not None and new_row is not None
        if old_row.direction != new_row.direction:
            diffs.append(
                ItemDiff(
                    item_code=item_code,
                    item_name=new_row.item_name or old_row.item_name,
                    change=CHANGE_DIRECTION,
                    detail=(
                        f"危急方向 {old_row.direction} → {new_row.direction}{scope_text}"
                        f"（判定口径变了，须复核）"
                    ),
                    old_line_no=old_row.row_no,
                    new_line_no=new_row.row_no,
                    old_value=old_row.direction,
                    new_value=new_row.direction,
                    old_version=old_row.version,
                    new_version=new_row.version,
                )
            )
        if (
            old_row.low_critical != new_row.low_critical
            or old_row.high_critical != new_row.high_critical
            or old_row.low_inclusive != new_row.low_inclusive
            or old_row.high_inclusive != new_row.high_inclusive
        ):
            diffs.append(
                ItemDiff(
                    item_code=item_code,
                    item_name=new_row.item_name or old_row.item_name,
                    change=CHANGE_THRESHOLD,
                    detail=(
                        f"阈值 {_threshold_text(old_row)} → {_threshold_text(new_row)}"
                        f"{scope_text}（判定门槛变了，历史判定需复核）"
                    ),
                    old_line_no=old_row.row_no,
                    new_line_no=new_row.row_no,
                    old_value=_threshold_text(old_row),
                    new_value=_threshold_text(new_row),
                    old_version=old_row.version,
                    new_version=new_row.version,
                )
            )
        if (old_row.unit or "") != (new_row.unit or ""):
            diffs.append(
                ItemDiff(
                    item_code=item_code,
                    item_name=new_row.item_name or old_row.item_name,
                    change=CHANGE_UNIT,
                    detail=(
                        f"单位 {old_row.unit or '（空）'} → {new_row.unit or '（空）'}"
                        f"{scope_text}（数量级可能随之变化，口径须复核）"
                    ),
                    old_line_no=old_row.row_no,
                    new_line_no=new_row.row_no,
                    old_value=old_row.unit,
                    new_value=new_row.unit,
                    old_version=old_row.version,
                    new_version=new_row.version,
                )
            )
    return tuple(diffs)


def diff_threshold_tables(old_table: Any, new_table: Any) -> tuple[ItemDiff, ...]:
    """对齐两份阈值表（``ThresholdTable``）的变更清单。"""
    return diff_rows(old_table.rows, new_table.rows)


def summarize(diffs: Iterable[ItemDiff]) -> dict[str, Any]:
    """变更清单的计数摘要。"""
    items = list(diffs)
    counts = {change: 0 for change in CHANGE_LABELS}
    for diff in items:
        counts[diff.change] = counts.get(diff.change, 0) + 1
    changed_items = sorted({diff.item_code for diff in items})
    return {
        "total": len(items),
        "counts": counts,
        "changed_items": changed_items,
        "changed_item_count": len(changed_items),
    }


def write_csv(diffs: Iterable[ItemDiff], path: str | Path) -> Path:
    """把变更清单写成 CSV（UTF-8 BOM，Excel 直开）。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(CSV_HEADER)
        for diff in diffs:
            writer.writerow(
                [
                    diff.item_code,
                    diff.item_name,
                    diff.label,
                    diff.detail,
                    "" if diff.old_line_no is None else diff.old_line_no,
                    "" if diff.new_line_no is None else diff.new_line_no,
                    diff.old_value,
                    diff.new_value,
                    diff.old_version,
                    diff.new_version,
                ]
            )
    return target
