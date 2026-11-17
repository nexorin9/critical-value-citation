"""混合召回引擎 —— 参数面 → 两路融合 → 阈值硬闸门 → 规则重排 → Top-N。

## 这条主路径在做什么

守护进程收到一条危急值结果，要回答的是"**依据在哪一行、通知谁、几分钟内上报、
记录交回哪里**"。这四问的答案全部来自院内材料（阈值表 xlsx + 危急值报告制度
docx），所以召回必须做到三件事：

1. **两路都要走** —— 结构化精确路（按项目码查表，命中即满分）与词法 BM25 路
   （值班人一句自然语言）。只有精确路会漏掉"制度里没点名这个项目但流程照样适用"
   的条款；只有词法路会把上千条近似条款排在阈值依据前面。
2. **分数要在同一把尺子上** —— 两路分按互补权重融合（源产品同构：向量权重
   = 1 - 关键词权重，本产品向量槽位改为结构化精确权重）。
3. **没依据就说没依据** —— 综合分低于硬闸门的一律截断，召回为空时给
   ``empty_response``（转人工并留痕），**不编依据**。

## 可见范围：材料范围 + 版本绑定 + 生效期

召回能看到什么，由三道闸决定：

* **材料范围（``kb_ids``）** —— 只在参数面点名的材料里检索。检索必须限定范围，
  取不到就报错，不静默地"检索全部材料"（同源产品 ``check_empty(kb_ids)``）。
* **版本绑定** —— 院内声明了「某项目当前该用哪一版阈值表 + 哪一版制度」时，
  **只召回被绑定的那一版**。引用里出现别的版本，值班人拿到的就是一张口径
  自相矛盾的处置卡。
* **生效期（``at``）** —— 给定观察时刻，已失效 / 尚未生效的版本不参与召回。

## 截断的两级

* ``top_k`` —— **候选池**：两路各自进池的条数上限（池外的不参与融合）。
* ``top_n`` —— **返回条数**：融合、闸门、重排之后最终给下游的条数。

两级都真实生效，且都在结果里报出被截掉多少条 —— "为什么只有 3 条依据"要能
回答，不能靠猜。

## 确定性

同样参数跑两次，排序**逐位一致**：融合分同分按稳定主键升序，重排后同分仍按
稳定主键升序。值班人复核时"上次排第一的条款这次排第三"必须是可解释的。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Mapping, Optional, Sequence

from critval.index.builder import MaterialIndex
from critval.index.structured import IndexDocument
from critval.index.tokenizer import tokenize
from critval.kb.binding import BindingTable
from critval.models import Citation
from critval.retrieve.fusion import fuse
from critval.retrieve.merge import MergedCandidate, PathCandidate
from critval.retrieve.params import RetrievalParams
from critval.retrieve.paths import lexical_candidates, structured_candidates
from critval.retrieve.rerank import (
    RerankProfile,
    RerankedCandidate,
    chapter_positions,
    profile_for_id,
    rerank,
)

__all__ = [
    "CandidateBundle",
    "HybridRetriever",
    "RetrievalHit",
    "RetrievalResult",
]


@dataclass(frozen=True)
class RetrievalHit:
    """一条召回结果（重排后的最终序）。"""

    rank: int
    stable_key: str
    doc_id: str
    score: float
    base_score: float
    structured_score: Optional[float] = None
    lexical_score: Optional[float] = None
    paths: tuple[str, ...] = ()
    matched_keys: tuple[str, ...] = ()
    matched_terms: tuple[str, ...] = ()
    factors: Mapping[str, float] = field(default_factory=dict)
    citation: Optional[Citation] = None
    locator: str = ""
    kind: str = ""
    #: 条款类型（由制度条款解析字段推导；非制度条款为 ``other``）。
    clause_type: str = ""
    clause_type_label: str = ""
    #: 这一条依据实际是靠**哪一级**拿到的（任务 id 32）：0 原样 / 1 关键词扩展 /
    #: 2 章节级兜底。结构化精确命中的依据即使整次召回走了降级，仍记 0 ——
    #: 查表命中不是降级。
    level: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "rank": self.rank,
            "stable_key": self.stable_key,
            "doc_id": self.doc_id,
            "score": round(self.score, 6),
            "base_score": round(self.base_score, 6),
            "structured_score": self.structured_score,
            "lexical_score": self.lexical_score,
            "paths": list(self.paths),
            "matched_keys": list(self.matched_keys),
            "matched_terms": list(self.matched_terms),
            "factors": dict(self.factors),
            "citation": self.citation.to_dict() if self.citation else None,
            "locator": self.locator,
            "kind": self.kind,
            "clause_type": self.clause_type,
            "clause_type_label": self.clause_type_label,
            "level": self.level,
        }


@dataclass(frozen=True)
class RetrievalResult:
    """一次召回的完整结果（含被截断的计数，便于回答"为什么只有几条"）。"""

    hits: tuple[RetrievalHit, ...] = ()
    params: Optional[RetrievalParams] = None
    structured_candidates: int = 0
    lexical_candidates: int = 0
    merged_candidates: int = 0
    gated_out: int = 0
    pool_dropped: int = 0
    empty_response: str = ""
    #: 本次召回所处的**降级层级**（任务 id 32）：0 原样 / 1 关键词扩展 /
    #: 2 章节级兜底 / 3 无依据转人工。命中级（每条依据各自）看 ``hit.level``。
    level: int = 0
    #: 逐级降级的留痕（试过哪几级、每级捞到几条）。
    fallback_attempts: tuple[Any, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not self.hits

    @property
    def citations(self) -> tuple[Citation, ...]:
        """带出处的引用（无出处的条目不进引用）。"""
        return tuple(hit.citation for hit in self.hits if hit.citation is not None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "hits": [hit.to_dict() for hit in self.hits],
            "params": self.params.to_dict() if self.params else None,
            "structured_candidates": self.structured_candidates,
            "lexical_candidates": self.lexical_candidates,
            "merged_candidates": self.merged_candidates,
            "gated_out": self.gated_out,
            "pool_dropped": self.pool_dropped,
            "empty_response": self.empty_response,
            "is_empty": self.is_empty,
            "level": self.level,
            "fallback_attempts": [
                attempt.to_dict() if hasattr(attempt, "to_dict") else dict(attempt)
                for attempt in self.fallback_attempts
            ],
        }


@dataclass(frozen=True)
class CandidateBundle:
    """一次查询的**两路候选**（与阈值 / 权重无关的那一半）。

    ## 为什么要把「取候选」和「打分」拆开

    一次查询的候选只取决于三件事：材料、查询文本、结构化条件。**阈值和权重
    改不动它们**。但默认的 ``retrieve()`` 把两件事连在一起，于是网格扫描
    （任务 id 31）每换一个网格点就要把 BM25 与结构化查表重算一遍 —— 几百
    毫秒乘以上百个网格点就是几分钟，而且算出来的还是同一批候选。

    拆成 ``gather()``（取候选）与 ``assemble()``（融合 → 闸门 → 重排 →
    截断）之后，扫描时每个查询的候选**只取一次**，每个网格点只做后半段
    （毫秒级）。结果逐位一致 —— 因为后半段拿到的输入一个字节都没变。
    """

    structured: tuple[PathCandidate, ...] = ()
    lexical: tuple[PathCandidate, ...] = ()
    visible: Mapping[str, IndexDocument] = field(default_factory=dict)
    query_terms: tuple[str, ...] = ()
    pool_dropped: int = 0
    item_code: str = ""


class HybridRetriever:
    """混合召回器：结构化精确路 + 词法 BM25 路，按参数面融合与截断。

    用法::

        retriever = HybridRetriever(
            index=index,
            params=RetrievalParams.for_materials("threshold", "policy"),
        )
        result = retriever.retrieve("血红蛋白危急值上报时限", {"item_code": "718-7"})
    """

    def __init__(
        self,
        index: MaterialIndex,
        params: RetrievalParams,
        bindings: Optional[BindingTable] = None,
        at: Optional[date] = None,
        profile: Optional[RerankProfile] = None,
    ) -> None:
        self.index = index
        self.params = params.check()
        self.bindings = bindings if bindings is not None else BindingTable()
        self.at = at
        #: 重排档位：参数面指定优先，其次显式传入。
        self.profile = profile if profile is not None else profile_for_id(self.params.rerank_id)

    # —— 可见范围 ——

    def _item_code_of(self, filters: Mapping[str, Any]) -> str:
        """从结构化查询条件里取项目码（版本绑定按项目码查）。"""
        return self.item_code_of(filters)

    def item_code_of(self, filters: Mapping[str, Any]) -> str:
        """从结构化查询条件里取项目码（降级层也要用它定位制度章节）。"""
        for key in ("item_code", "item_codes"):
            value = filters.get(key)
            if isinstance(value, str) and value:
                return value
            if isinstance(value, (list, tuple)) and value:
                return str(value[0])
        return ""

    def item_codes_of(self, filters: Mapping[str, Any]) -> tuple[str, ...]:
        """结构化查询条件里的**全部**项目码（多值键也展开）。"""
        codes: list[str] = []
        for key in ("item_code", "item_codes"):
            value = filters.get(key)
            if isinstance(value, str) and value:
                codes.append(value)
            elif isinstance(value, (list, tuple)):
                codes.extend(str(item) for item in value if item)
        seen: list[str] = []
        for code in codes:
            if code not in seen:
                seen.append(code)
        return tuple(seen)

    def _version_allowed(
        self,
        document: IndexDocument,
        binding: Optional[Any],
    ) -> bool:
        """这条依据的版本是否与院内绑定一致。

        * 没有绑定 → 不做版本限制（没声明就没有约束），调用方按"需人工核对"处理；
        * 绑定里声明了版本 → 只放行同一版本的条目；
        * 没有出处的材料（语料行）不受版本限制。
        """
        if binding is None:
            return True
        citation = document.citation
        if citation is None:
            return True
        version = document.version or citation.file_version or ""
        if citation.kind == "threshold_row":
            if not binding.threshold_version:
                return True
            return version == binding.threshold_version
        if citation.kind == "policy_clause":
            if not binding.policy_version:
                return True
            return version == binding.policy_version
        return True

    def visible_documents(self, item_code: str = "") -> dict[str, IndexDocument]:
        """本次可见的条目（``doc_id → 条目``）。

        三道闸：材料范围（``kb_ids``）→ 版本绑定 → 生效期。
        """
        binding = self.bindings.for_item(item_code) if item_code else None
        visible: dict[str, IndexDocument] = {}
        for doc_id, document in self.index.documents_by_id().items():
            if self.index.source_key_of(doc_id) not in self.params.kb_ids:
                continue
            if not self._version_allowed(document, binding):
                continue
            if self.at is not None and not document.covers(self.at):
                continue
            visible[doc_id] = document
        return visible

    # —— 召回 ——

    def _structured_candidates(
        self,
        filters: Mapping[str, Any],
        visible: Mapping[str, IndexDocument],
    ) -> list:
        """结构化精确路的候选。

        没有结构化条件时**不走这条路** —— 没有"要精确匹配什么"，查表命中无从
        谈起；若强行把所有条目当成结构化命中（覆盖率 0），每条依据的融合分会被
        凭空砍掉一半。
        """
        if not filters:
            return []
        hits = [
            hit
            for hit in self.index.structured.query(at=self.at, **dict(filters))
            if hit.doc_id in visible
        ]
        requested = len(self.index.structured.query_keys(**dict(filters)))
        return structured_candidates(hits, requested_keys=max(1, requested))

    def _lexical_candidates(
        self,
        query: str,
        visible: Mapping[str, IndexDocument],
    ) -> tuple[list, int]:
        """词法 BM25 路的候选 + 被候选池截掉的条数。"""
        tokens = tokenize(query)
        raw_hits = self.index.search(query, top_k=self.params.top_k)
        matched_total = self.index.inverted.match_count(tokens)
        pool_dropped = max(0, matched_total - len(raw_hits))
        hits = [hit for hit in raw_hits if hit.doc_id in visible]
        return lexical_candidates(hits, visible), pool_dropped

    @staticmethod
    def _gate(
        candidates: Sequence[MergedCandidate],
        threshold: float,
    ) -> tuple[list[MergedCandidate], int]:
        """综合分硬闸门：低于阈值即截断（同源语义：降序排列，遇到第一个即停）。"""
        kept: list[MergedCandidate] = []
        for candidate in candidates:
            if candidate.fused_score < threshold:
                break
            kept.append(candidate)
        return kept, len(candidates) - len(kept)

    def retrieve(
        self,
        query: str = "",
        filters: Optional[Mapping[str, Any]] = None,
    ) -> RetrievalResult:
        """跑一次混合召回。

        ``query`` 是词法路的自然语言查询；``filters`` 是结构化路的事实条件
        （如 ``{"item_code": "718-7"}``）。两者都给 = 两路都走；只给一个 =
        只走那一路。

        本方法 = ``gather()``（取候选）+ ``assemble()``（打分），拆开是为了让
        参数扫描能复用候选（见 ``CandidateBundle``）。
        """
        bundle = self.gather(query, filters)
        return self.assemble(bundle, self.params)

    def gather(
        self,
        query: str = "",
        filters: Optional[Mapping[str, Any]] = None,
        top_k: Optional[int] = None,
    ) -> CandidateBundle:
        """取两路候选（**与阈值 / 权重无关**）。

        ``top_k`` 是候选池上限；不传则用参数面里的 ``top_k``。
        """
        limit = self.params.top_k if top_k is None else top_k
        wanted = dict(filters or {})
        item_code = self._item_code_of(wanted)
        visible = self.visible_documents(item_code)

        structured = self._structured_candidates(wanted, visible)[:limit]
        lexical, pool_dropped = self._lexical_candidates(query, visible)
        lexical = lexical[:limit]

        return CandidateBundle(
            structured=tuple(structured),
            lexical=tuple(lexical),
            visible=visible,
            query_terms=tuple(tokenize(query)),
            pool_dropped=pool_dropped,
            item_code=item_code,
        )

    def assemble(
        self,
        bundle: CandidateBundle,
        params: Optional[RetrievalParams] = None,
    ) -> RetrievalResult:
        """按参数面组装结果：融合 → 综合分硬闸门 → 规则重排 → 返回条数截断。

        与阈值、权重相关的部分全在这里；候选来自 ``bundle``，不重算。
        """
        effective = (params or self.params).check()

        # 融合（两路权重互补、和恒为 1）；分数越界在融合入口直接抛错。
        merged = fuse(bundle.structured, bundle.lexical, effective.fusion_weights())

        # 综合分硬闸门。
        kept, gated_out = self._gate(merged, effective.similarity_threshold)

        # 规则重排（字段命中权重 / 章节位置权重 / 词项覆盖率 / 条款类型）。
        positions = chapter_positions(bundle.visible.values())
        reranked = rerank(kept, bundle.query_terms, profile_for_id(effective.rerank_id), positions)

        # 返回条数截断。
        hits = tuple(
            self._to_hit(number, item)
            for number, item in enumerate(reranked[: effective.top_n], start=1)
        )
        return RetrievalResult(
            hits=hits,
            params=effective,
            structured_candidates=len(bundle.structured),
            lexical_candidates=len(bundle.lexical),
            merged_candidates=len(merged),
            gated_out=gated_out,
            pool_dropped=bundle.pool_dropped,
            empty_response=effective.empty_response if not hits else "",
        )

    @staticmethod
    def _to_hit(rank: int, item: RerankedCandidate) -> RetrievalHit:
        candidate = item.candidate
        document = candidate.document
        citation = document.citation if document is not None else None
        return RetrievalHit(
            rank=rank,
            stable_key=candidate.stable_key,
            doc_id=candidate.doc_id,
            score=item.final_score,
            base_score=item.base_score,
            structured_score=candidate.structured_score,
            lexical_score=candidate.lexical_score,
            paths=candidate.paths,
            matched_keys=candidate.matched_keys,
            matched_terms=candidate.matched_terms,
            factors=item.factors.to_dict(),
            citation=citation,
            locator=candidate.locator,
            kind=document.kind if document is not None else "",
            clause_type=item.clause_type,
            clause_type_label=item.clause_type_label,
        )
