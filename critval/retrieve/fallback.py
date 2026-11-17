"""召回分级降级 —— 原样查不到时先降级再转人工，**绝不编依据**。

## 这条主路径在做什么

源产品的检索层在综合分低于阈值时**一刀切停下**（``retrieval.py`` 的
``if sim[i] < similarity_threshold: break``），停下之后就是空结果 +
一句 ``empty_response``。一刀切的代价是：明明有依据，只是**问法对不上**，
值班人却拿不到任何东西。

院内场景里"查不到"大多数不是"没依据"，而是"换个说法"：

* 值班人问"血红蛋白"，制度里点名的是项目码"718-7"；
* 制度里没点名这个项目，但**同一条流程照样适用**（第三章的时限与通知对象
  对所有危急值项目一视同仁）。

所以本产品把"查不到"拆成**四级**，每降一级都把依据的可信度标出来：

============  ==================  ==========================================
层级          做法                值班人怎么理解
============  ==================  ==========================================
``0``         原样查询            直接命中的依据，最硬
``1``         关键词扩展          换了个说法才查到（别名表自动抽取）
``2``         章节级兜底          定位到制度对应章节，取该章节条款
``3``         无依据转人工        降级用尽 —— **一条引用都不产出**
============  ==================  ==========================================

## 别名表为什么自动抽取

别名表**不手写**。手写的词表在院内必然过期：项目一改版、科室一改叫法，
词表就成了"去年的人才知道的叫法"。本产品从**材料本身**抽：

* 阈值表行 → ``项目码 ↔ 项目名``（718-7 ↔ 血红蛋白）；
* 制度条款 → 条款点名的 ``项目码 ↔ 项目名``；
* 制度章节标题 → 该章节点名的项目码（"第二章 危急值项目与阈值" → 718-7 …）。

材料一改，别名表跟着改 —— 换材料不用改代码。

**代价要说清楚**：中文没有词边界，别名靠**子串**命中，所以短名（如"钾"）
可能带来无关扩展。因此扩展词数有上限（每个别名最多 3 个、全局最多 8 个），
而且扩展**只影响召回范围**，不影响判定权威 —— 判定权威始终是院内阈值表。

## 章节级兜底怎么定位章节

用**结构化路命中的项目码**去制度里找点名了该项目的条款，取这些条款所在的
章节，再把**该章节的全部条款**取出来（标 level 2）。

为什么不是"取该项目的条款"？因为制度里写着"血红蛋白（718-7）"的第二章
只讲阈值口径，**时限与通知对象在第三章**。值班人真正要照着做的是第三章。
章节级兜底给的是"这一章讲这件事"，弱于点名，但强于没有。

## 最硬的一条线

第 3 级**一条引用都不产出**（沿用任务 id 8 的 ``empty_response`` 语义）：
事件状态转 ``transferred_to_human`` + 写一条 ``retrieve_empty_no_citation``
审计。降级的目的是"多找找"，不是"凑一条"。

本模块保证的不变式：**有引用必有出处**（章节兜底只取带出处的条款；
无出处的条目进不了引用），**无依据必无引用**（第 3 级产出零引用）。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Mapping, Optional, Sequence

from critval.index.structured import IndexDocument
from critval.index.tokenizer import normalize
from critval.retrieve.clause_type import (
    CLAUSE_TYPE_DEADLINE,
    CLAUSE_TYPE_DESTINATION,
    CLAUSE_TYPE_GENERAL,
    CLAUSE_TYPE_OTHER,
    CLAUSE_TYPE_TARGET,
    clause_type_label,
    derive_clause_type,
)
from critval.retrieve.engine import HybridRetriever, RetrievalHit, RetrievalResult
from critval.retrieve.levels import (
    LEVEL_CHAPTER,
    LEVEL_EXACT,
    LEVEL_EXPANDED,
    LEVEL_NONE,
    FallbackAttempt,
    level_label,
)
from critval.retrieve.merge import STRUCTURED_PATH, stable_primary_key

__all__ = [
    "MAX_ALIASES_PER_KEY",
    "MAX_EXPANSIONS",
    "AliasTable",
    "GradedRetriever",
    "build_alias_table",
    "fallback_provenance",
]

#: 一个别名最多带几个替代词（章节标题能点名十几个项目，全带上就是噪声）。
MAX_ALIASES_PER_KEY = 3

#: 一次扩展最多加几个词（扩展词越多，越容易把不相关的东西拽进来）。
MAX_EXPANSIONS = 8

#: 条款类型的排序优先级（降级兜底时把"能照着做"的条款排在前面）。
_TYPE_ORDER: Mapping[str, int] = {
    CLAUSE_TYPE_DEADLINE: 0,
    CLAUSE_TYPE_DESTINATION: 1,
    CLAUSE_TYPE_TARGET: 2,
    CLAUSE_TYPE_OTHER: 3,
    CLAUSE_TYPE_GENERAL: 4,
}


# ---------------------------------------------------------------------------
# 别名表
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AliasTable:
    """别名表 —— ``词 → 替代词``（键已归一：NFKC + 小写）。

    ``mapping`` 的键是归一化后的别名，值是替代词（原样，可直接拼进查询）。
    """

    mapping: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    def alternatives(self, term: str) -> tuple[str, ...]:
        """某个词的替代词（没有则空 tuple）。"""
        return tuple(self.mapping.get(normalize(term), ()))

    def expand(self, query: str) -> tuple[str, tuple[str, ...]]:
        """按别名表扩展查询。

        返回 ``(扩展后的查询, 新增的词)``；没有可扩展的词时原样返回。

        * 命中判定：**子串**（中文没有词边界）；
        * 每个别名最多带 ``MAX_ALIASES_PER_KEY`` 个替代词；
        * 全局最多 ``MAX_EXPANSIONS`` 个新增词；
        * 命中的别名按**词长降序**处理（长词更具体，先用）。
        """
        if not query:
            return query, ()
        haystack = normalize(query)
        additions: list[str] = []
        for key in sorted(self.mapping, key=lambda item: (-len(item), item)):
            if key not in haystack:
                continue
            taken = 0
            for alternative in self.mapping[key]:
                if taken >= MAX_ALIASES_PER_KEY:
                    break
                if not alternative:
                    continue
                if normalize(alternative) in haystack:
                    continue
                if any(normalize(alternative) == normalize(item) for item in additions):
                    continue
                additions.append(alternative)
                taken += 1
                if len(additions) >= MAX_EXPANSIONS:
                    break
            if len(additions) >= MAX_EXPANSIONS:
                break
        if not additions:
            return query, ()
        return f"{query} {' '.join(additions)}", tuple(additions)

    def to_dict(self) -> dict[str, Any]:
        return {
            "alias_count": len(self.mapping),
            "mapping": {key: list(values) for key, values in sorted(self.mapping.items())},
        }


def build_alias_table(documents: Iterable[IndexDocument]) -> AliasTable:
    """从材料条目里**自动抽取**别名表（不手写大词表）。

    抽取三类关系：

    1. 阈值表行 → ``项目码 ↔ 项目名``；
    2. 制度条款 → 条款点名的 ``项目码 ↔ 项目名``；
    3. 制度条款 → ``章节标题 ↔ 该章节点名的项目码``。

    关系是**双向**的：查"血红蛋白"能扩到"718-7"，查"718-7"也能扩到
    "血红蛋白"。
    """
    pairs: dict[str, set[str]] = {}

    def _link(left: str, right: str) -> None:
        left_key = normalize(left)
        right_key = normalize(right)
        if not left_key or not right_key or left_key == right_key:
            return
        pairs.setdefault(left_key, set()).add(right)
        pairs.setdefault(right_key, set()).add(left)

    for document in documents:
        if document.kind == "threshold_row":
            _link(document.field("item_code"), document.field("item_name"))
            continue
        if document.kind != "policy_clause":
            continue
        codes = document.values_of("item_codes")
        names = document.values_of("item_names")
        for code in codes:
            for name in names:
                _link(code, name)
        # 章节标题 → 该章节点名的项目码（"第二章 危急值项目与阈值" → 718-7 …）。
        heading = document.field("section_title")
        if not heading:
            citation = document.citation
            heading = citation.section_path[0] if citation and citation.section_path else ""
        for code in codes:
            _link(heading, code)

    return AliasTable(
        mapping={key: tuple(sorted(values)) for key, values in pairs.items() if values}
    )


# ---------------------------------------------------------------------------
# 分级降级召回器
# ---------------------------------------------------------------------------


class GradedRetriever:
    """分级降级召回器：原样 → 关键词扩展 → 章节级兜底 → 转人工。

    包在 ``HybridRetriever`` 外面用::

        graded = GradedRetriever(HybridRetriever(index, params))
        result = graded.retrieve_graded("血红蛋白危急值多久上报", {"item_code": "718-7"})
        result.level          # 0 / 1 / 2 / 3
        result.fallback_attempts  # 逐级留痕
    """

    def __init__(self, retriever: HybridRetriever) -> None:
        self.retriever = retriever

    # —— 公开入口 ——

    def retrieve_graded(
        self,
        query: str = "",
        filters: Optional[Mapping[str, Any]] = None,
    ) -> RetrievalResult:
        """跑一次分级降级召回。

        **"空"的判据是"没有制度依据"**，不是"一条命中都没有" —— 召回层要
        回答的是「时限 / 通知对象 / 记录去向」这三个问题，答案在制度条款里。
        只捞到阈值行（结构化精确命中）并不算"查到了依据"：阈值行由判定环节
        拿着，制度依据缺了，处置卡照样出不来。

        每一级都记一条 ``FallbackAttempt``；返回结果的 ``level`` 是**最终
        取到制度依据的那一级**（取不到是 3），每条命中另有自己的 ``level``。
        """
        wanted = dict(filters or {})
        attempts: list[FallbackAttempt] = []

        # —— 第 0 级：原样查询 ——
        base = self.retriever.retrieve(query, wanted)
        attempts.append(
            FallbackAttempt(
                LEVEL_EXACT,
                query,
                len(base.hits),
                "原样查询命中 {} 条制度依据".format(self._policy_count(base.hits))
                if self._has_policy_basis(base.hits)
                else "原样查询未命中制度依据",
            )
        )
        if self._has_policy_basis(base.hits):
            return replace(base, level=LEVEL_EXACT, fallback_attempts=tuple(attempts))

        best = base
        item_code = self.retriever.item_code_of(wanted)
        visible = self.retriever.visible_documents(item_code)

        # —— 第 1 级：关键词扩展 ——
        # 别名表来自**全部材料**（不只是本次可见的那几份）：它是"院内术语
        # 表"，阈值表里的「项目码 ↔ 项目名」正是值班人问法与制度写法的桥。
        # 检索范围仍然只管命中范围，扩展词进不来范围外的材料。
        alias = build_alias_table(self.retriever.index.documents_by_id().values())
        expanded, added = alias.expand(query)
        if added:
            expanded_result = self.retriever.retrieve(expanded, wanted)
            attempts.append(
                FallbackAttempt(
                    LEVEL_EXPANDED,
                    expanded,
                    len(expanded_result.hits),
                    f"关键词扩展：新增 {len(added)} 个扩展词（{'、'.join(added)}）",
                )
            )
            if self._has_policy_basis(expanded_result.hits):
                return replace(
                    expanded_result,
                    level=LEVEL_EXPANDED,
                    fallback_attempts=tuple(attempts),
                    hits=self._stamp(expanded_result.hits, LEVEL_EXPANDED),
                )
            best = self._better(best, expanded_result)
        else:
            attempts.append(
                FallbackAttempt(
                    LEVEL_EXPANDED,
                    query,
                    0,
                    "关键词扩展：别名表未命中查询词，无可扩展项",
                )
            )

        # —— 第 2 级：章节级兜底 ——
        chapter_hits, chapter_note = self._chapter_fallback(wanted, visible, best)
        attempts.append(FallbackAttempt(LEVEL_CHAPTER, "", len(chapter_hits), chapter_note))
        if chapter_hits:
            return RetrievalResult(
                hits=self._merge_with_base(best, chapter_hits),
                params=best.params,
                structured_candidates=best.structured_candidates,
                lexical_candidates=best.lexical_candidates,
                merged_candidates=best.merged_candidates,
                gated_out=best.gated_out,
                pool_dropped=best.pool_dropped,
                empty_response="",
                level=LEVEL_CHAPTER,
                fallback_attempts=tuple(attempts),
            )

        # —— 第 3 级：无依据转人工（**零引用**）——
        attempts.append(
            FallbackAttempt(
                LEVEL_NONE,
                "",
                0,
                "四级降级用尽：不产出任何引用，转人工并留痕（不编造依据）",
            )
        )
        return replace(
            best,
            hits=(),
            level=LEVEL_NONE,
            empty_response=best.empty_response
            or (best.params.empty_response if best.params else ""),
            fallback_attempts=tuple(attempts),
        )

    # —— 内部 ——

    @staticmethod
    def _has_policy_basis(hits: Sequence[RetrievalHit]) -> bool:
        """这次召回有没有拿到**制度依据**（阈值行不算）。"""
        return any(hit.kind == "policy_clause" for hit in hits)

    @staticmethod
    def _policy_count(hits: Sequence[RetrievalHit]) -> int:
        return sum(1 for hit in hits if hit.kind == "policy_clause")

    @staticmethod
    def _better(first: RetrievalResult, second: RetrievalResult) -> RetrievalResult:
        """两份结果里留**依据更多**的那份（条数相同则留先来的那份）。"""
        if len(second.hits) > len(first.hits):
            return second
        return first

    @staticmethod
    def _merge_with_base(
        base: RetrievalResult,
        chapter_hits: Sequence[RetrievalHit],
    ) -> tuple[RetrievalHit, ...]:
        """章节兜底结果 = 章节条款（level 2）+ 原样命中的非制度依据（level 0）。

        阈值行是**结构化精确命中**，它不是降级来的 —— 降级只影响"制度依据
        怎么找到的"，不影响"阈值查表命中"这件事。
        """
        merged: list[RetrievalHit] = [
            hit for hit in base.hits if hit.kind != "policy_clause"
        ]
        merged.extend(chapter_hits)
        return tuple(
            replace(hit, rank=index) for index, hit in enumerate(merged, start=1)
        )

    @staticmethod
    def _stamp(
        hits: Sequence[RetrievalHit],
        level: int,
    ) -> tuple[RetrievalHit, ...]:
        """给命中打上"实际是靠哪一级拿到的"层级。

        结构化精确命中的依据**不降级** —— 查表命中就是查表命中，即使整次
        召回是靠关键词扩展才凑到结果的。所以 level 1 时，结构化路也命中的
        那条仍记 0。
        """
        stamped: list[RetrievalHit] = []
        for hit in hits:
            effective = LEVEL_EXACT if (
                level == LEVEL_EXPANDED and STRUCTURED_PATH in hit.paths
            ) else level
            citation = hit.citation
            if citation is not None and citation.level != effective:
                citation = replace(citation, level=effective)
            stamped.append(replace(hit, level=effective, citation=citation))
        return tuple(stamped)

    def _chapter_fallback(
        self,
        filters: Mapping[str, Any],
        visible: Mapping[str, IndexDocument],
        result: RetrievalResult,
    ) -> tuple[tuple[RetrievalHit, ...], str]:
        """章节级兜底：按项目码定位制度章节，取该章节全部条款。

        章节的定位有两条路（都靠材料本身，不靠猜）：

        1. 条款的**结构化项目码**里点名了这个项目码；
        2. 条款**正文里**写了项目码或项目名（制度里写"血红蛋白（718-7）"
           但解析没抽出项目码时走这条）。

        返回 ``(命中列表, 说明)``；定位不到章节时返回空列表并说明原因。
        """
        item_codes = list(self.retriever.item_codes_of(filters))
        # 结构化路实际命中的项目码也一并算上（查询条件没给项目码时可能有）。
        matched_codes, structured_score = self._structured_item_codes(filters, visible)
        for code in matched_codes:
            if code not in item_codes:
                item_codes.append(code)
        if not item_codes:
            return (), "章节级兜底：本次查询没有项目码，定位不到章节"

        terms = tuple(item_codes) + self._item_names(item_codes, visible)

        # 找到点名了这些项目的制度条款 → 它们所在的章节。
        headings: list[tuple[str, str]] = []
        for document in visible.values():
            if document.kind != "policy_clause":
                continue
            if not self._mentions_item(document, item_codes, terms):
                continue
            citation = document.citation
            if citation is None or not citation.section_path:
                continue
            key = (citation.source_sha256, citation.section_path[0])
            if key not in headings:
                headings.append(key)
        if not headings:
            return (), (
                "章节级兜底：制度里没有点名这些项目的条款（"
                + "、".join(item_codes)
                + "），定位不到章节"
            )

        # 取这些章节的全部条款（带出处才要 —— 无出处的条目进不了引用）。
        chapter_documents: list[IndexDocument] = []
        for document in visible.values():
            if document.kind != "policy_clause":
                continue
            citation = document.citation
            if citation is None or not citation.section_path:
                continue
            key = (citation.source_sha256, citation.section_path[0])
            if key in headings:
                chapter_documents.append(document)

        if not chapter_documents:
            return (), "章节级兜底：定位到章节但章节内没有带出处的条款"

        chapter_documents.sort(key=self._chapter_order)
        limit = result.params.top_n if result.params else len(chapter_documents)
        selected = chapter_documents[: max(1, limit)]

        score = structured_score
        hits: list[RetrievalHit] = []
        for document in selected:
            citation = document.citation
            if citation is None:
                continue
            clause_type = derive_clause_type(document)
            hits.append(
                RetrievalHit(
                    rank=0,
                    stable_key=stable_primary_key(document),
                    doc_id=document.doc_id,
                    score=score,
                    base_score=score,
                    structured_score=None,
                    lexical_score=None,
                    paths=(STRUCTURED_PATH,),
                    matched_keys=("chapter", citation.section_path[0] if citation.section_path else ""),
                    matched_terms=(),
                    factors={"chapter_fallback": 1.0},
                    citation=replace(citation, level=LEVEL_CHAPTER),
                    locator=citation.locator,
                    kind=document.kind,
                    clause_type=clause_type,
                    clause_type_label=clause_type_label(clause_type),
                    level=LEVEL_CHAPTER,
                )
            )

        heading_names = "、".join(heading for _, heading in headings)
        note = (
            f"章节级兜底：按项目码 {'、'.join(item_codes)} 定位到章节「{heading_names}」，"
            f"取该章节条款 {len(selected)} 条"
        )
        return tuple(hits), note

    @staticmethod
    def _mentions_item(
        document: IndexDocument,
        item_codes: Sequence[str],
        terms: Sequence[str],
    ) -> bool:
        """这条条款是不是"讲这个项目"的。

        * 结构化项目码里点名了 → 是；
        * 正文里写了项目码或项目名 → 是（解析没抽出项目码时的兜底）。
        """
        codes = document.values_of("item_codes")
        if any(code in item_codes for code in codes):
            return True
        haystack = normalize(document.text)
        return any(normalize(term) in haystack for term in terms if term)

    @staticmethod
    def _item_names(
        item_codes: Sequence[str],
        visible: Mapping[str, IndexDocument],
    ) -> tuple[str, ...]:
        """项目码 → 项目名（从阈值表行里取，材料里没有就是空）。"""
        names: list[str] = []
        for document in visible.values():
            if document.kind != "threshold_row":
                continue
            code = document.field("item_code")
            if code not in item_codes:
                continue
            name = document.field("item_name")
            if name and name not in names:
                names.append(name)
        return tuple(names)

    @staticmethod
    def _chapter_order(document: IndexDocument) -> tuple[int, int, str]:
        """章节内条款的排序：先按条款类型（能照着做的在前），再按段落号。"""
        citation = document.citation
        paragraph = 0
        if citation is not None:
            paragraph = citation.paragraph_no or citation.line_no or 0
        return (
            _TYPE_ORDER.get(derive_clause_type(document), 3),
            int(paragraph),
            document.doc_id,
        )

    def _structured_item_codes(
        self,
        filters: Mapping[str, Any],
        visible: Mapping[str, IndexDocument],
    ) -> tuple[tuple[str, ...], float]:
        """结构化路**实际命中**的项目码 + 它的归一化分（命中键覆盖率）。

        * 只查项目码相关的键（``item_code`` / ``item_codes``）—— 章节是按
          项目码定位的，其它结构化条件（单位 / 版本）与定位无关；
        * 只认**本次可见**的条目（材料范围 / 版本绑定 / 生效期三道闸）；
        * 命中分 = 命中键数 / 请求键数，与结构化路的归一化口径一致
          （``critval.retrieve.paths.structured_candidates``）—— 章节兜底的
          强度来自查表命中，不自己编一个分。
        """
        code_filters = {
            key: value
            for key, value in filters.items()
            if key in ("item_code", "item_codes") and value
        }
        if not code_filters:
            return (), 0.0
        hits = [
            hit
            for hit in self.retriever.index.structured.query(
                at=self.retriever.at, **code_filters
            )
            if hit.doc_id in visible
        ]
        if not hits:
            return (), 0.0
        codes: list[str] = []
        for hit in hits:
            for match in hit.key_matches:
                if match.key not in ("item_code", "item_codes"):
                    continue
                for value in match.values:
                    if value not in codes:
                        codes.append(value)
        requested = len(self.retriever.index.structured.query_keys(**code_filters))
        matched = max(len(hit.matched_keys) for hit in hits)
        score = min(1.0, matched / requested) if requested else 0.0
        return tuple(codes), score


# ---------------------------------------------------------------------------
# 卡片标注
# ---------------------------------------------------------------------------


def fallback_provenance(result: RetrievalResult) -> Any:
    """召回结果 → 卡片上的「制度召回来源」标注。

    level > 0 时卡片会把该要素标成 ``degraded`` 并写明"走了第几级降级"，
    值班人一眼能看出这条依据弱在哪（``critval.card.provenance`` 同一份层级号）。
    """
    from critval.card.provenance import retrieval_provenance

    level = int(getattr(result, "level", 0) or 0)
    detail = ""
    attempts = tuple(getattr(result, "fallback_attempts", ()) or ())
    if level > 0:
        detail = f"第 {level} 级降级（{level_label(level)}）"
        for attempt in attempts:
            if getattr(attempt, "level", None) == level and getattr(attempt, "note", ""):
                detail = attempt.note
                break
    return retrieval_provenance(level, detail)
