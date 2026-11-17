"""材料 → 可召回索引（按材料 sha256 增量重建）。

## 这一层解决什么

阈值表 xlsx 与制度 docx / pdf 进来之后，召回层需要的是**条目级的可查事实**：
每个项目一行、每条制度一个条款，各自带出处与生效期。本模块把「材料」翻成
``IndexDocument``，同时喂进两路索引：

* 词法路（``InvertedIndex``，BM25）—— 值班人用自然语言描述问题（"血红蛋白
  危急值多久要上报"）时靠它；
* 结构化路（``StructuredIndex``）—— 按项目码 / 危急方向 / 单位 / 版本精确命中，
  这是**判定依据**该有的样子。

## 增量重建的口径

共享目录里的材料随时会被替换（改版、误传、撤回）。重建的判据只有一个：
**文件内容的 sha256 变了没有**。

* 没变 → 这一份**不碰**（`unchanged`）：不重新解析、不重新分词、不动倒排表；
* 变了或新增 → 先把这份材料原来的条目**全部撤掉**，再重新入库（`updated` /
  `added`）—— 不撤旧的就会留下"上一版的条款还在检索结果里"，值班人拿到两条
  互相矛盾的依据；
* 材料被撤走 → 对应条目一并摘除（`removed`）。

因此"增量重建耗时低于全量重建"不是口号：全量重建要把所有材料重新解析 + 重新
分词，增量只碰变了的那几份。

## 与既有模块的关系

* 阈值表解析走 ``kb.loader.load_threshold_table``（列名别名映射、未识别行进
  解析报告）；
* 制度解析走 ``kb.policy.load_policy_document``（条款级条目、出处五件套、
  依赖缺失显式降级）；
* 本模块**不重复实现**这两套解析 —— 索引只消费它们的产出。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Sequence

from critval.index.inverted import BM25Params, InvertedIndex, SearchHit
from critval.index.structured import IndexDocument, StructuredIndex, documents_from_fields
from critval.index.tokenizer import tokenize
from critval.kb.version import sha256_file

__all__ = [
    "MaterialIndex",
    "MaterialSource",
    "RebuildResult",
    "documents_for_source",
]


@dataclass(frozen=True)
class MaterialSource:
    """一份待索引的材料。

    ``kind`` 取值：``threshold_table``（xlsx）/ ``policy_document``（docx、pdf）/
    ``corpus``（制表符分隔的条款语料，供召回评测与吞吐基准使用）。
    """

    key: str
    path: str
    kind: str
    sha256: str = ""

    @classmethod
    def from_path(cls, path: str | Path, kind: str, key: str = "") -> "MaterialSource":
        source = Path(path)
        return cls(
            key=key or source.name,
            path=str(source),
            kind=kind,
            sha256=sha256_file(source),
        )


@dataclass
class RebuildResult:
    """一次重建的结果（增量或全量）。"""

    added: int = 0
    updated: int = 0
    unchanged: int = 0
    removed: int = 0
    docs_before: int = 0
    docs_after: int = 0
    indexed_documents: int = 0
    changed_sources: tuple[str, ...] = ()
    elapsed_seconds: float = 0.0
    full_rebuild_seconds: Optional[float] = None

    @property
    def touched(self) -> int:
        return self.added + self.updated + self.removed

    def summary_text(self) -> str:
        parts = [
            f"新增 {self.added} 份",
            f"变更 {self.updated} 份",
            f"未变 {self.unchanged} 份",
            f"撤回 {self.removed} 份",
        ]
        if self.full_rebuild_seconds is not None:
            parts.append(f"全量重建对照 {self.full_rebuild_seconds:.3f}s")
        return " · ".join(parts) + f" · 耗时 {self.elapsed_seconds:.3f}s"

    def to_dict(self) -> dict[str, Any]:
        return {
            "added": self.added,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "removed": self.removed,
            "docs_before": self.docs_before,
            "docs_after": self.docs_after,
            "indexed_documents": self.indexed_documents,
            "changed_sources": list(self.changed_sources),
            "elapsed_seconds": round(self.elapsed_seconds, 6),
            "full_rebuild_seconds": (
                round(self.full_rebuild_seconds, 6)
                if self.full_rebuild_seconds is not None
                else None
            ),
        }


def _threshold_documents(path: str, sha256: str) -> list[IndexDocument]:
    """阈值表 → 每个项目一行。"""
    from critval.kb.loader import load_threshold_table

    table = load_threshold_table(path)
    documents: list[IndexDocument] = []
    for index, row in enumerate(table.rows):
        locator = row.row_no if row.row_no is not None else index + 1
        doc_id = f"threshold_row:{sha256[:12]}:{locator}"
        text = " ".join(
            part
            for part in (
                row.item_code,
                row.item_name,
                row.unit,
                row.threshold_text(),
                row.reference_range_raw,
                row.version,
            )
            if part
        )
        documents.append(
            documents_from_fields(
                doc_id=doc_id,
                source_name=table.source_name,
                source_sha256=sha256,
                kind="threshold_row",
                text=text,
                fields={
                    "item_code": row.item_code,
                    "item_name": row.item_name,
                    "direction": row.direction,
                    "unit": row.unit,
                    "version": row.version,
                    "source_name": table.source_name,
                    "kind": "threshold_row",
                },
                effective_from=row.effective_from,
                effective_to=row.effective_to,
                version=row.version,
                stable_id=f"{row.item_code}#{locator}",
                citation=row.citation,
            )
        )
    return documents


def _policy_documents(path: str, sha256: str) -> list[IndexDocument]:
    """制度 docx / pdf → 每条条款一条。"""
    from critval.kb.policy import load_policy_document

    document = load_policy_document(path)
    documents: list[IndexDocument] = []
    for index, clause in enumerate(document.clauses):
        locator = clause.citation.paragraph_no
        if locator is None:
            locator = index + 1
        doc_id = f"policy_clause:{sha256[:12]}:{locator}"
        text = " ".join(
            part
            for part in (
                clause.section_title,
                clause.clause_no or "",
                clause.body,
                clause.notify_target,
                clause.deadline_text,
                clause.record_destination,
                " ".join(clause.item_codes),
                " ".join(clause.item_names),
            )
            if part
        )
        documents.append(
            documents_from_fields(
                doc_id=doc_id,
                source_name=document.source_name,
                source_sha256=sha256,
                kind="policy_clause",
                text=text,
                fields={
                    "clause_no": clause.clause_no or "",
                    "section_title": clause.section_title,
                    # 三项事实随行带上：重排层据此判断条款**是干什么用的**
                    # （时限 / 去向 / 通知对象 / 总则），不必再猜一遍正文。
                    "notify_target": clause.notify_target,
                    "deadline": clause.deadline_text,
                    "record_destination": clause.record_destination,
                    "version": document.version,
                    "source_name": document.source_name,
                    "kind": "policy_clause",
                },
                multi_fields={
                    "item_codes": clause.item_codes,
                    # 项目名一起进索引：降级层要靠「项目码 ↔ 项目名」自动抽
                    # 别名表（值班人问"血红蛋白"、制度写"718-7"时要能对上）。
                    "item_names": clause.item_names,
                },
                effective_from=document.effective_from,
                effective_to=document.effective_to,
                version=document.version,
                stable_id=clause.stable_id,
                citation=clause.citation,
            )
        )
    return documents


def _corpus_documents(path: str, sha256: str) -> list[IndexDocument]:
    """条款语料（制表符分隔：``条款号\\t章节\\t正文``）→ 每行一条。"""
    source = Path(path)
    documents: list[IndexDocument] = []
    with source.open("r", encoding="utf-8") as handle:
        for number, raw in enumerate(handle, start=1):
            line = raw.rstrip("\n").rstrip("\r")
            if not line.strip():
                continue
            parts = line.split("\t")
            line_id = parts[0].strip() if parts else ""
            section = parts[1].strip() if len(parts) > 1 else ""
            body = "\t".join(parts[2:]).strip() if len(parts) > 2 else ""
            if not line_id:
                line_id = f"line-{number}"
            doc_id = f"corpus:{sha256[:12]}:{line_id}"
            documents.append(
                documents_from_fields(
                    doc_id=doc_id,
                    source_name=source.name,
                    source_sha256=sha256,
                    kind="corpus_line",
                    text=f"{section} {body}".strip(),
                    fields={
                        "section_title": section,
                        "clause_no": line_id,
                        "source_name": source.name,
                        "kind": "corpus_line",
                    },
                    stable_id=line_id,
                )
            )
    return documents


_LOADERS = {
    "threshold_table": _threshold_documents,
    "policy_document": _policy_documents,
    "corpus": _corpus_documents,
}


def documents_for_source(source: MaterialSource) -> list[IndexDocument]:
    """把一份材料翻成条目列表。"""
    loader = _LOADERS.get(source.kind)
    if loader is None:
        raise ValueError(
            f"不认识的材料类型：{source.kind}（支持：{'、'.join(sorted(_LOADERS))}）"
        )
    digest = source.sha256 or sha256_file(source.path)
    return loader(source.path, digest)


class MaterialIndex:
    """词法路 + 结构化路的联合索引，带按材料指纹的增量重建。"""

    def __init__(self, params: Optional[BM25Params] = None) -> None:
        self.inverted = InvertedIndex(params=params or BM25Params())
        self.structured = StructuredIndex()
        #: ``材料 key → 内容 sha256``
        self._manifest: dict[str, str] = {}
        #: ``材料 key → 该材料贡献的条目 id``
        self._docs_by_source: dict[str, tuple[str, ...]] = {}

    # —— 状态 ——

    @property
    def manifest(self) -> dict[str, str]:
        return dict(self._manifest)

    def doc_count(self) -> int:
        return self.inverted.doc_count

    def stats(self) -> dict[str, Any]:
        return {
            "inverted": self.inverted.stats(),
            "structured": self.structured.stats(),
            "materials": len(self._manifest),
        }

    def material_status(self, sources: Sequence[MaterialSource]) -> dict[str, str]:
        """看一眼这些材料相对当前索引是什么状态（不改索引）。

        取值：``added`` / ``changed`` / ``unchanged`` / ``removed``。
        """
        status: dict[str, str] = {}
        seen: set[str] = set()
        for source in sources:
            seen.add(source.key)
            digest = source.sha256 or sha256_file(source.path)
            known = self._manifest.get(source.key)
            if known is None:
                status[source.key] = "added"
            elif known != digest:
                status[source.key] = "changed"
            else:
                status[source.key] = "unchanged"
        for key in self._manifest:
            if key not in seen:
                status[key] = "removed"
        return status

    # —— 重建 ——

    def rebuild(self, sources: Sequence[MaterialSource]) -> RebuildResult:
        """增量重建：只处理**内容变了**的材料。

        撤回的材料（这轮没投进来的）对应的条目一并摘除 —— 共享目录里撤掉的
        文件不应该继续参与召回。
        """
        started = time.perf_counter()
        before = self.inverted.doc_count
        result = RebuildResult(docs_before=before)
        seen: set[str] = set()
        changed: list[str] = []

        for source in sources:
            seen.add(source.key)
            digest = source.sha256 or sha256_file(source.path)
            known = self._manifest.get(source.key)
            if known == digest and source.key in self._docs_by_source:
                result.unchanged += 1
                continue
            changed.append(source.key)
            if known is None:
                result.added += 1
            else:
                result.updated += 1
                self._drop_source(source.key)
            self._index_source(source, digest)

        for key in list(self._manifest):
            if key not in seen:
                result.removed += 1
                changed.append(key)
                self._drop_source(key)
                self._manifest.pop(key, None)

        result.docs_after = self.inverted.doc_count
        result.changed_sources = tuple(changed)
        result.indexed_documents = result.docs_after
        result.elapsed_seconds = time.perf_counter() - started
        return result

    def full_rebuild(self, sources: Sequence[MaterialSource]) -> RebuildResult:
        """全量重建：清空后全部重新入库（基准对照用）。"""
        self.inverted.clear()
        self.structured.clear()
        self._manifest.clear()
        self._docs_by_source.clear()
        result = self.rebuild(sources)
        result.full_rebuild_seconds = result.elapsed_seconds
        return result

    # —— 内部 ——

    def _drop_source(self, key: str) -> None:
        for doc_id in self._docs_by_source.get(key, ()):  # 先撤旧条目
            self.inverted.remove(doc_id)
            self.structured.remove(doc_id)
        self._docs_by_source.pop(key, None)

    def _index_source(self, source: MaterialSource, digest: str) -> None:
        documents = documents_for_source(
            MaterialSource(key=source.key, path=source.path, kind=source.kind, sha256=digest)
        )
        self._index_documents(source.key, documents, digest)

    def _index_documents(
        self,
        key: str,
        documents: Sequence[IndexDocument],
        digest: str = "",
    ) -> None:
        """把一批条目登记到某个材料范围键下（先撤掉该键原有的条目）。"""
        self._drop_source(key)
        doc_ids: list[str] = []
        for document in documents:
            tokens = tokenize(document.text)
            self.inverted.add(document.doc_id, tokens, payload=document.doc_id)
            self.structured.add(document)
            doc_ids.append(document.doc_id)
        self._docs_by_source[key] = tuple(doc_ids)
        self._manifest[key] = digest

    def ingest_documents(
        self,
        key: str,
        documents: Sequence[IndexDocument],
        digest: str = "",
    ) -> int:
        """直接登记一批已解析好的条目（程序化装配 / 测试替身用）。

        正常路径是「材料文件 → 解析 → 入库」；这个入口给的是**已经解析好的条目**
        （例如院内系统直接推来的条目、测试里手工构造的多版本材料）。
        返回登记的条目数。同一 ``key`` 再次登记会**先撤掉旧条目**再登记新的。
        """
        if not key:
            raise ValueError("材料范围键不能为空")
        self._index_documents(key, documents, digest)
        return len(documents)

    # —— 检索 ——

    def search(self, query: str, top_k: int = 10) -> list[SearchHit]:
        """词法检索（BM25）。"""
        return self.inverted.search(tokenize(query), top_k=top_k)

    def lookup(self, **filters: str) -> list[str]:
        """结构化精确检索（返回条目 id 列表）。"""
        return [hit.doc_id for hit in self.structured.query(**filters)]

    def document_of(self, doc_id: str) -> Optional[IndexDocument]:
        """按条目 id 取回条目（两路合并算稳定主键时用）。"""
        return self.structured.document(doc_id)

    def source_key_of(self, doc_id: str) -> str:
        """条目来自哪一份材料（材料范围键，召回时限定检索范围用）。

        找不到时返回空串 —— 调用方据此把条目排除在本次检索范围之外。
        """
        for key, doc_ids in self._docs_by_source.items():
            if doc_id in doc_ids:
                return key
        return ""

    def docs_by_source(self) -> dict[str, tuple[str, ...]]:
        """材料范围键 → 该材料贡献的条目 id（只读副本）。"""
        return {key: tuple(ids) for key, ids in self._docs_by_source.items()}

    def documents_by_id(self) -> dict[str, IndexDocument]:
        """全部条目（``doc_id → 条目``），供词法路候选补齐出处与稳定主键。"""
        return {
            doc_id: document
            for doc_id in self.structured.doc_ids()
            if (document := self.structured.document(doc_id)) is not None
        }
