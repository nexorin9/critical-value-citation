"""召回基线与评测参赛者装配 —— 「只留词法那一路」对照组。

## 基线是什么

**纯 BM25 单路召回**：不做结构化精确匹配、不做规则重排、不做综合分硬闸门，
纯粹按 BM25 原始分排序取前 N 条。

它是融合引擎的**对照组**，回答一个很具体的问题：

> 把结构化精确路与规则重排拿掉，只剩经典单路词法检索，**到底丢了多少**？

丢的东西往往不在"能不能搜到"，而在"**能不能排在前面**"—— 单路 BM25 在
上千条近似条款里，把真正该用的那一条（阈值依据行、写着时限的操作细则）
挤到了后面去；值班人拿到卡片时先看到的是七条相似但不适用的总则。

## 两侧必须同口径，否则数字没意义

对比表里的两行跑的是**同一份材料、同一份评测集、同样的检索深度与 Top-N**，
只差召回做法：

* **可见范围一致** —— 同一材料范围键、同一生效期口径；
* **深度一致** —— 两侧都返回前 ``depth`` 条；
* **指标一致** —— 两侧都用 ``critval.evaluate`` 的同一个评测器算
  命中率 / Top-N 命中率 / 出处覆盖率。

差在哪儿只有三处，而且都是有意差出来的：

======================  ==========================  ==========================
                        基线（纯 BM25）              融合引擎（产品主路径）
======================  ==========================  ==========================
结构化精确路            无                          有（项目码 / 方向 / 单位）
综合分硬闸门            无                          有（低于阈值即截断）
规则重排                无                          有（条款类型 / 字段 / 章节）
======================  ==========================  ==========================

## 为什么基线的分数是 BM25 原始分

BM25 原始分无界（一条长条款轻易上 6 分），与融合分不在同一把尺子上。
基线**不参与融合**，所以不需要归一化；但要说清"这个分是原始分"，
否则读者会拿它和融合分直接比大小。评测指标只用名次，不用分数，
所以这不影响任何指标计算。

## 参赛者装配

``HybridEvalRetriever`` 把正式引擎（``HybridRetriever``）接到评测协议上 ——
换的是**参赛者**，不是裁判（评测口径不动）。
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Optional, Sequence

from critval.index.builder import MaterialIndex
from critval.index.structured import IndexDocument
from critval.kb.binding import BindingTable
from critval.retrieve.engine import HybridRetriever
from critval.retrieve.merge import LEXICAL_PATH, stable_primary_key
from critval.retrieve.result import RetrievedItem, document_key

__all__ = [
    "BM25BaselineRetriever",
    "HybridEvalRetriever",
]


class BM25BaselineRetriever:
    """纯 BM25 单路召回（基线）。

    用法::

        baseline = BM25BaselineRetriever(index, candidate_depth=50)
        items = baseline.retrieve(query, depth=10)
    """

    def __init__(
        self,
        index: MaterialIndex,
        candidate_depth: int = 50,
        at: Optional[Any] = None,
        kb_ids: Optional[Sequence[str]] = None,
    ) -> None:
        self.index = index
        self.candidate_depth = max(1, candidate_depth)
        self.at = at
        self.kb_ids = tuple(kb_ids) if kb_ids else ()
        self._documents: Optional[dict[str, IndexDocument]] = None

    def _document_map(self) -> dict[str, IndexDocument]:
        if self._documents is None:
            self._documents = self.index.documents_by_id()
        return self._documents

    def _visible(self, document: IndexDocument) -> bool:
        """可见范围：材料范围键 + 生效期（与融合引擎同口径）。"""
        if self.kb_ids:
            if self.index.source_key_of(document.doc_id) not in self.kb_ids:
                return False
        if self.at is not None and not document.covers(self.at):
            return False
        return True

    def retrieve(self, query: Any, depth: int) -> list[RetrievedItem]:
        """跑一次纯 BM25 召回，返回按名次排好的前 ``depth`` 条。"""
        text = getattr(query, "text", "") or ""
        hits = self.index.search(text, top_k=self.candidate_depth)
        documents = self._document_map()
        items: list[RetrievedItem] = []
        for hit in hits:
            if len(items) >= max(0, depth):
                break
            document = documents.get(hit.doc_id)
            if document is None or not self._visible(document):
                continue
            items.append(
                RetrievedItem(
                    key=document_key(document),
                    rank=len(items) + 1,
                    score=hit.score,
                    citation=document.citation,
                    stable_key=stable_primary_key(document),
                    doc_id=document.doc_id,
                    paths=(LEXICAL_PATH,),
                    item_code=document.field("item_code"),
                )
            )
        return items


class HybridEvalRetriever:
    """把正式混合召回引擎接到评测协议上（参赛者装配）。

    引擎的 ``top_n`` 与评测深度对齐 —— 否则引擎先截断一次、评测再截断一次，
    两处截断混在一起就说不清"少的那几条是被谁截掉的"。
    """

    def __init__(
        self,
        index: MaterialIndex,
        params: Any,
        bindings: Optional[BindingTable] = None,
        at: Optional[Any] = None,
        depth: int = 10,
    ) -> None:
        self.depth = max(1, depth)
        self.retriever = HybridRetriever(
            index=index,
            params=replace(params, top_n=self.depth),
            bindings=bindings,
            at=at,
        )

    def retrieve(self, query: Any, depth: int) -> list[RetrievedItem]:
        """跑一次融合召回，返回按名次排好的前 ``depth`` 条。"""
        text = getattr(query, "text", "") or ""
        filters = dict(getattr(query, "filters", {}) or {})
        result = self.retriever.retrieve(text, filters)
        items: list[RetrievedItem] = []
        for hit in result.hits:
            if len(items) >= max(0, depth):
                break
            document = self.retriever.index.document_of(hit.doc_id)
            items.append(
                RetrievedItem(
                    key=document_key(document) if document is not None else hit.doc_id,
                    rank=len(items) + 1,
                    score=hit.score,
                    citation=hit.citation,
                    stable_key=hit.stable_key,
                    doc_id=hit.doc_id,
                    paths=hit.paths,
                    item_code=document.field("item_code") if document is not None else "",
                )
            )
        return items
