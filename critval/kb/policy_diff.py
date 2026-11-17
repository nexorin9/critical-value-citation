"""制度改版影响面：两版制度逐条款对齐，产出受影响条款清单。

制度改版不是「换个文件」那么简单 —— 医务科要先看清楚**哪几条改了**、
**哪几条删了**、**哪几条是新增的**，才敢签发新版本。处置卡上的时限 /
通知对象 / 记录去向都来自制度，改了哪一条，等于改了哪一类处置动作。

对齐口径：按**稳定标识**配对（``critval.kb.policy`` 里的 ``stable_id``）：

* 条款号唯一 → 用条款号；
* 条款号重复 → 用 ``条款号#项目码``；
* 条款号也没有 → 用正文摘要。

配对不上的，再按**条款号顺序**兜底配一次（有的制度改版会整段重排），
避免把「改了一条」报成「删一条 + 加一条」。

变更类型：

* ``added`` —— 新版新增条款；
* ``removed`` —— 新版删掉条款（这一类处置动作从此没有依据了）；
* ``facts_changed`` —— 通知对象 / 上报时限 / 记录去向变了（**影响处置动作**）；
* ``body_changed`` —— 正文变了但三项事实没变（措辞修订）。
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

from critval.models import PolicyClause

__all__ = [
    "CHANGE_ADDED",
    "CHANGE_BODY",
    "CHANGE_FACTS",
    "CHANGE_REMOVED",
    "CHANGE_LABELS",
    "ClauseDiff",
    "diff_clauses",
    "diff_policy_documents",
    "summarize",
    "write_csv",
]

CHANGE_ADDED = "added"
CHANGE_REMOVED = "removed"
CHANGE_FACTS = "facts_changed"
CHANGE_BODY = "body_changed"

CHANGE_LABELS: dict[str, str] = {
    CHANGE_ADDED: "新增条款",
    CHANGE_REMOVED: "删除条款",
    CHANGE_FACTS: "事实要素变化",
    CHANGE_BODY: "正文修订",
}

#: CSV 表头（改版影响面清单的固定列序）。
CSV_HEADER = (
    "条款号",
    "稳定标识",
    "变更类型",
    "变更说明",
    "旧段落号",
    "新段落号",
    "旧通知对象",
    "新通知对象",
    "旧上报时限",
    "新上报时限",
    "旧记录去向",
    "新记录去向",
    "旧版本",
    "新版本",
)

#: 三项事实字段（比对口径固定，顺序即清单里的展示顺序）。
_FACT_FIELDS = ("notify_target", "deadline_text", "record_destination")

#: 事实字段 → 人读标签（变更说明里给中文，不给英文属性名）。
FIELD_LABELS: dict[str, str] = {
    "notify_target": "通知对象",
    "deadline_text": "上报时限",
    "record_destination": "记录去向",
}


@dataclass(frozen=True)
class ClauseDiff:
    """一条制度改版变更。"""

    stable_id: str
    clause_no: str
    change: str
    detail: str
    old_paragraph_no: Optional[int] = None
    new_paragraph_no: Optional[int] = None
    changed_fields: tuple[str, ...] = ()
    old_values: tuple[tuple[str, str], ...] = ()
    new_values: tuple[tuple[str, str], ...] = ()
    old_version: str = ""
    new_version: str = ""

    @property
    def label(self) -> str:
        return CHANGE_LABELS.get(self.change, self.change)

    def to_dict(self) -> dict[str, Any]:
        return {
            "stable_id": self.stable_id,
            "clause_no": self.clause_no,
            "change": self.change,
            "label": self.label,
            "detail": self.detail,
            "old_paragraph_no": self.old_paragraph_no,
            "new_paragraph_no": self.new_paragraph_no,
            "changed_fields": list(self.changed_fields),
            "old_values": dict(self.old_values),
            "new_values": dict(self.new_values),
            "old_version": self.old_version,
            "new_version": self.new_version,
        }


def _fact_values(clause: PolicyClause) -> tuple[tuple[str, str], ...]:
    return tuple((field, getattr(clause, field, "") or "") for field in _FACT_FIELDS)


def _changed_facts(
    old_clause: PolicyClause,
    new_clause: PolicyClause,
) -> tuple[tuple[str, ...], tuple[tuple[str, str], ...], tuple[tuple[str, str], ...]]:
    """比三项事实 → ``(变化字段, 旧值, 新值)``。"""
    old_values = _fact_values(old_clause)
    new_values = _fact_values(new_clause)
    changed = tuple(
        field
        for (field, old_value), (_f, new_value) in zip(old_values, new_values)
        if old_value != new_value
    )
    return changed, old_values, new_values


def _diff_pair(
    old_clause: PolicyClause,
    new_clause: PolicyClause,
    *,
    note: str = "",
) -> Optional[ClauseDiff]:
    """配对成功的两条条款 → 变更条目（没变则返回 ``None``）。"""
    changed, old_values, new_values = _changed_facts(old_clause, new_clause)
    if changed:
        detail_parts = []
        old_map = dict(old_values)
        new_map = dict(new_values)
        for field in changed:
            label = FIELD_LABELS.get(field, field)
            detail_parts.append(
                f"{label}：{old_map.get(field) or '（空）'} → {new_map.get(field) or '（空）'}"
            )
        detail = "；".join(detail_parts) + "（处置动作变了，须复核）"
        if note:
            detail = f"{note}{detail}"
        return ClauseDiff(
            stable_id=new_clause.stable_id or old_clause.stable_id,
            clause_no=new_clause.clause_no or old_clause.clause_no or "",
            change=CHANGE_FACTS,
            detail=detail,
            old_paragraph_no=old_clause.citation.paragraph_no,
            new_paragraph_no=new_clause.citation.paragraph_no,
            changed_fields=changed,
            old_values=old_values,
            new_values=new_values,
            old_version=old_clause.citation.file_version or "",
            new_version=new_clause.citation.file_version or "",
        )
    if (old_clause.body or "").strip() != (new_clause.body or "").strip():
        detail = "条款正文有修订，三项事实未变"
        if note:
            detail = f"{note}{detail}"
        return ClauseDiff(
            stable_id=new_clause.stable_id or old_clause.stable_id,
            clause_no=new_clause.clause_no or old_clause.clause_no or "",
            change=CHANGE_BODY,
            detail=detail,
            old_paragraph_no=old_clause.citation.paragraph_no,
            new_paragraph_no=new_clause.citation.paragraph_no,
            changed_fields=(),
            old_values=old_values,
            new_values=new_values,
            old_version=old_clause.citation.file_version or "",
            new_version=new_clause.citation.file_version or "",
        )
    return None


def diff_clauses(
    old_clauses: Sequence[PolicyClause],
    new_clauses: Sequence[PolicyClause],
) -> tuple[ClauseDiff, ...]:
    """逐条款对齐两版制度，产出变更清单。"""
    old_index = {clause.stable_id: clause for clause in old_clauses if clause.stable_id}
    new_index = {clause.stable_id: clause for clause in new_clauses if clause.stable_id}

    diffs: list[ClauseDiff] = []
    matched_old: set[str] = set()
    matched_new: set[str] = set()

    for stable_id in sorted(set(old_index) & set(new_index)):
        diff = _diff_pair(old_index[stable_id], new_index[stable_id])
        matched_old.add(stable_id)
        matched_new.add(stable_id)
        if diff is not None:
            diffs.append(diff)

    # 兜底配对：同一条款号但稳定标识没对上（改版重排 / 条号重号变动）。
    leftover_old = [c for sid, c in old_index.items() if sid not in matched_old]
    leftover_new = [c for sid, c in new_index.items() if sid not in matched_new]
    by_clause_no: dict[str, list[PolicyClause]] = {}
    for clause in leftover_new:
        if clause.clause_no:
            by_clause_no.setdefault(clause.clause_no, []).append(clause)

    still_new: list[PolicyClause] = []
    for old_clause in leftover_old:
        bucket = by_clause_no.get(old_clause.clause_no or "")
        if not bucket:
            continue
        new_clause = bucket.pop(0)
        diff = _diff_pair(old_clause, new_clause, note="（按条款号顺序配对）")
        if diff is not None:
            diffs.append(diff)
        matched_old.add(old_clause.stable_id)
        matched_new.add(new_clause.stable_id)
    for bucket in by_clause_no.values():
        still_new.extend(bucket)

    for stable_id, clause in sorted(old_index.items()):
        if stable_id in matched_old:
            continue
        diffs.append(
            ClauseDiff(
                stable_id=stable_id,
                clause_no=clause.clause_no or "",
                change=CHANGE_REMOVED,
                detail=(
                    "新版删掉该条款 —— 这条处置动作从此没有制度依据，"
                    "须确认是否有意停用"
                ),
                old_paragraph_no=clause.citation.paragraph_no,
                old_values=_fact_values(clause),
                old_version=clause.citation.file_version or "",
            )
        )

    for stable_id, clause in sorted(new_index.items()):
        if stable_id in matched_new:
            continue
        diffs.append(
            ClauseDiff(
                stable_id=stable_id,
                clause_no=clause.clause_no or "",
                change=CHANGE_ADDED,
                detail="新版新增条款",
                new_paragraph_no=clause.citation.paragraph_no,
                new_values=_fact_values(clause),
                new_version=clause.citation.file_version or "",
            )
        )

    order = {CHANGE_FACTS: 0, CHANGE_ADDED: 1, CHANGE_REMOVED: 2, CHANGE_BODY: 3}
    diffs.sort(key=lambda d: (order.get(d.change, 9), d.clause_no, d.stable_id))
    return tuple(diffs)


def diff_policy_documents(old_document: Any, new_document: Any) -> tuple[ClauseDiff, ...]:
    """对齐两份制度文件（``PolicyDocument``）的变更清单。"""
    return diff_clauses(old_document.clauses, new_document.clauses)


def summarize(diffs: Iterable[ClauseDiff]) -> dict[str, Any]:
    """变更清单的计数摘要。"""
    items = list(diffs)
    counts = {change: 0 for change in CHANGE_LABELS}
    for diff in items:
        counts[diff.change] = counts.get(diff.change, 0) + 1
    affected = sorted({diff.stable_id or diff.clause_no for diff in items})
    return {
        "total": len(items),
        "counts": counts,
        "affected_clauses": affected,
        "affected_clause_count": len(affected),
    }


def write_csv(diffs: Iterable[ClauseDiff], path: str | Path) -> Path:
    """把变更清单写成 CSV（UTF-8 BOM，Excel 直开）。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(CSV_HEADER)
        for diff in diffs:
            old_map = dict(diff.old_values)
            new_map = dict(diff.new_values)
            writer.writerow(
                [
                    diff.clause_no,
                    diff.stable_id,
                    diff.label,
                    diff.detail,
                    "" if diff.old_paragraph_no is None else diff.old_paragraph_no,
                    "" if diff.new_paragraph_no is None else diff.new_paragraph_no,
                    old_map.get("notify_target", ""),
                    new_map.get("notify_target", ""),
                    old_map.get("deadline_text", ""),
                    new_map.get("deadline_text", ""),
                    old_map.get("record_destination", ""),
                    new_map.get("record_destination", ""),
                    diff.old_version,
                    diff.new_version,
                ]
            )
    return target
