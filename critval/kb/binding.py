"""版本绑定表：项目码 → （阈值表版本 + 制度版本）。

**绑定是「哪两套材料配套使用」的院内声明。** 同一个检验项目，阈值表改版
与制度改版未必同步 —— 阈值表按 ``示例 v1.0`` 判危急，制度却按
``示例 v2.0`` 的时限通知，这两套口径放在一起就是自相矛盾。

绑定表把这件事写死在数据里（不是靠代码里的 if）：

* ``bind`` 阶段：按观察时刻，为每个项目码定下「当前该用哪一版阈值表 +
  哪一版制度」；
* ``recall`` 阶段：**只召回被绑定的版本**，未绑版本不出现在引用里 ——
  引用里出现别的版本，等于值班人拿到一张口径不一致的处置卡。

对应源参考 ``web/src/pages/file-manager/connect-to-knowledge-modal/index.tsx``
里 ``handleOk()`` 取 ``knowledgeIds`` 提交的动作 —— 源产品是「把文档绑到
知识库」，本项目是「把检验项目码绑到材料版本对」。

绑定表没有的项目码**不受版本限制**（没有声明就没有约束），但调用方应
据此标「需人工核对」；绑定表本身缺失也**不阻断**出卡 —— 它是加固项，
不是唯一防线（唯一防线是 ``critval.kb.consistency`` 的版本一致性闸门）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Optional, Sequence

from critval.kb.consistency import VersionBinding, load_bindings
from critval.kb.policy import PolicyDocument, select_policy_version
from critval.kb.version import build_versions, select_version
from critval.models import Citation, PolicyClause, ThresholdRow

__all__ = [
    "BindingTable",
    "build_binding_table",
    "load_binding_table",
    "save_binding_table",
    "unbound_versions",
    "visible_citations",
    "visible_policy_clauses",
    "visible_threshold_rows",
]


@dataclass(frozen=True)
class BindingTable:
    """项目码 → 版本对的绑定表。"""

    bindings: tuple[VersionBinding, ...] = ()

    def for_item(self, item_code: str) -> Optional[VersionBinding]:
        """取某个项目码的绑定；没绑返回 ``None``。"""
        for binding in self.bindings:
            if binding.item_code == item_code:
                return binding
        return None

    def item_codes(self) -> tuple[str, ...]:
        return tuple(binding.item_code for binding in self.bindings)

    @property
    def ok(self) -> bool:
        """绑定表是否可用（至少有一条）。"""
        return bool(self.bindings)

    def to_dict(self) -> dict[str, Any]:
        return {
            "bindings": [binding.to_dict() for binding in self.bindings],
            "item_count": len(self.bindings),
        }

    def summary_text(self) -> str:
        if not self.bindings:
            return "版本绑定表为空（没有声明版本对，引用不受版本限制，但需人工核对）"
        lines = [f"版本绑定表：{len(self.bindings)} 个项目码"]
        for binding in self.bindings:
            lines.append(
                f"  · {binding.item_code} → 阈值表 {binding.threshold_version or '（未声明）'}"
                f" + 制度 {binding.policy_version or '（未声明）'}"
                + (f"（{binding.note}）" if binding.note else "")
            )
        return "\n".join(lines)


def load_binding_table(path: str | Path) -> BindingTable:
    """从 JSON 读绑定表。

    文件缺失 / 格式错 → 返回空绑定表（**不抛错**）：绑定表是加固项，
    读不到不该把整个服务拖垮，但调用方要能看出「没有绑定」。
    """
    return BindingTable(bindings=load_bindings(path))


def save_binding_table(table: BindingTable, path: str | Path) -> Path:
    """把绑定表写成 JSON（院内可手改、可入库）。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "bindings": [binding.to_dict() for binding in table.bindings],
    }
    target.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return target


def build_binding_table(
    threshold_rows: Sequence[ThresholdRow],
    policy_documents: Sequence[PolicyDocument] = (),
    at: Optional[date] = None,
) -> BindingTable:
    """按观察时刻**推导**绑定表（院内没手工声明时的兜底）。

    推导口径：

    * 阈值表版本 —— 该项目码在 ``at`` 时刻生效的那一版
      （版本空档 / 口径互斥 → 留空，等人工声明）；
    * 制度版本 —— ``at`` 时刻生效的那一版制度；该版制度里点了这个项目码
      的条款会在备注里写明（没点名就写「制度未点名该项目」）。
    """
    moment = at or date.today()
    versions = build_versions(threshold_rows)
    policy_selection = (
        select_policy_version(policy_documents, moment) if policy_documents else None
    )
    policy_document = policy_selection.document if policy_selection else None
    policy_version = policy_selection.version if policy_selection else ""

    item_codes: list[str] = []
    for row in threshold_rows:
        if row.item_code and row.item_code not in item_codes:
            item_codes.append(row.item_code)

    bindings: list[VersionBinding] = []
    for item_code in item_codes:
        selection = select_version(versions, item_code, moment)
        threshold_version = selection.version.version_no if selection.ok else ""
        note_parts: list[str] = []
        if selection.ok:
            note_parts.append(f"阈值表按 {moment.isoformat()} 生效版本推导")
        else:
            note_parts.append(f"阈值表版本需人工声明（{selection.status}）")
        if policy_document is not None:
            clauses = policy_document.clauses_for_item(item_code)
            if clauses:
                where = "、".join(
                    f"{clause.clause_no or '（无条号）'} 第 {clause.citation.paragraph_no} 段"
                    for clause in clauses
                )
                note_parts.append(f"制度点名条款：{where}")
            else:
                note_parts.append("制度未点名该项目")
        bindings.append(
            VersionBinding(
                item_code=item_code,
                threshold_version=threshold_version,
                policy_version=policy_version,
                note="；".join(note_parts),
            )
        )
    return BindingTable(bindings=tuple(bindings))


def visible_policy_clauses(
    clauses: Sequence[PolicyClause],
    item_code: str = "",
    binding: Optional[VersionBinding] = None,
) -> tuple[PolicyClause, ...]:
    """按绑定过滤制度条款：**只留被绑版本**。

    没有绑定 → 不做版本限制（全部可见），调用方按「未声明绑定」处理。
    """
    if binding is None or not binding.policy_version:
        return tuple(clauses)
    return tuple(
        clause
        for clause in clauses
        if (clause.citation.file_version or "") == binding.policy_version
    )


def visible_threshold_rows(
    rows: Sequence[ThresholdRow],
    item_code: str = "",
    binding: Optional[VersionBinding] = None,
) -> tuple[ThresholdRow, ...]:
    """按绑定过滤阈值行：**只留被绑版本**。"""
    if binding is None or not binding.threshold_version:
        return tuple(rows)
    return tuple(
        row for row in rows if (row.citation.file_version or "") == binding.threshold_version
    )


def visible_citations(
    clauses: Sequence[PolicyClause],
    item_code: str = "",
    binding: Optional[VersionBinding] = None,
) -> tuple[Citation, ...]:
    """可见条款的出处 —— 未绑版本不会出现在这里。"""
    return tuple(clause.citation for clause in visible_policy_clauses(clauses, item_code, binding))


def unbound_versions(
    clauses: Sequence[PolicyClause],
    binding: Optional[VersionBinding] = None,
) -> tuple[str, ...]:
    """条款集合里**没被绑定**的版本号（应为空；非空说明引用会串版本）。"""
    if binding is None or not binding.policy_version:
        return ()
    seen: list[str] = []
    for clause in clauses:
        version = clause.citation.file_version or ""
        if version and version != binding.policy_version and version not in seen:
            seen.append(version)
    return tuple(seen)
