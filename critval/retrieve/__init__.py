"""召回层：混合召回、规则重排、带出处的引用。

分工：

* ``normalize`` —— **打分归一化契约**：两路分数必须落在同一把尺子（``[0, 1]``），
  越界的分数在融合入口直接抛错并报出是哪一路、多少分；
* ``merge`` —— 两路候选按**稳定主键**去重合并，两路的分与命中说明都留着；
* ``paths`` —— 两路候选的构造（结构化路的命中率 / 词法路的 BM25 分归一化）；
* ``fusion`` —— 融合入口（验尺子 → 去重合并 → 加权融合）；
* ``params`` —— **召回参数面**（阈值 / 权重 / 候选池 / 返回条数 / 材料范围 /
  重排档位 / 空结果话术），逐项 ``check()`` 校验；
* ``rerank`` —— **规则重排**（字段命中权重 / 章节位置权重 / 词项覆盖率 /
  条款类型加权）；
* ``clause_type`` —— **条款类型标签与类型权重表**（由制度条款解析字段推导，
  非法权重表报错并指明字段）；
* ``engine`` —— **混合召回引擎**：参数面 → 两路融合 → 阈值硬闸门 → 规则重排
  → Top-N 截断；
* ``result`` —— **召回结果条目与召回器协议**（评测器与召回实现之间的唯一接口）；
* ``citation`` —— **出处随命中走**：引用收集 / 落库 / 空召回兜底 / 版本一致性断言；
* ``levels`` —— **降级层级口径**（0 原样 / 1 关键词扩展 / 2 章节级兜底 /
  3 无依据转人工）的唯一真源，卡片层引用同一份；
* ``fallback`` —— **分级降级召回**：原样查不到时按「关键词扩展 → 章节级兜底
  → 转人工」逐级降级，每一级都标在引用与卡片上，**绝不编依据**；
* ``baseline`` —— **纯 BM25 单路召回基线**与评测参赛者装配（融合引擎的对照组）。
"""

from critval.retrieve.baseline import BM25BaselineRetriever, HybridEvalRetriever
from critval.retrieve.citation import (
    AUDIT_EMPTY_RETRIEVAL,
    CITATION_LOCATOR_FIELDS,
    HUMAN_REVIEW_STATUS,
    RetrievalOutcome,
    apply_retrieval_outcome,
    assert_versions_consistent,
    citation_gaps,
    citations_of_hits,
    describe_citations,
    persist_citations,
    resolve_retrieval,
)
from critval.retrieve.fallback import (
    AliasTable,
    GradedRetriever,
    build_alias_table,
    fallback_provenance,
)
from critval.retrieve.levels import (
    FALLBACK_LEVELS,
    LEVEL_CHAPTER,
    LEVEL_EXACT,
    LEVEL_EXPANDED,
    LEVEL_NONE,
    FallbackAttempt,
    level_label,
)
from critval.retrieve.clause_type import (
    CLAUSE_TYPE_DEADLINE,
    CLAUSE_TYPE_DESTINATION,
    CLAUSE_TYPE_GENERAL,
    CLAUSE_TYPE_LABELS,
    CLAUSE_TYPE_OTHER,
    CLAUSE_TYPE_TARGET,
    CLAUSE_TYPES,
    DEFAULT_CLAUSE_TYPE_WEIGHTS,
    derive_clause_type,
    normalize_clause_type_weights,
)
from critval.retrieve.engine import HybridRetriever, RetrievalHit, RetrievalResult
from critval.retrieve.errors import ParamCheckError
from critval.retrieve.fusion import FusionWeights, fuse
from critval.retrieve.merge import (
    LEXICAL_PATH,
    STRUCTURED_PATH,
    MergedCandidate,
    PathCandidate,
    merge_candidates,
    stable_primary_key,
)
from critval.retrieve.normalize import (
    NormalizationError,
    ScoreContractError,
    ScoreNotNormalizedError,
    is_normalized,
    normalize_max_relative,
    normalize_quantile,
    normalize_ratio,
    require_normalized,
)
from critval.retrieve.params import DEFAULT_EMPTY_RESPONSE, RetrievalParams
from critval.retrieve.paths import lexical_candidates, structured_candidates
from critval.retrieve.rerank import (
    RERANK_PROFILES,
    RerankFactors,
    RerankProfile,
    RerankedCandidate,
    chapter_positions,
    profile_for_id,
    rerank,
    stable_sort_key,
)
from critval.retrieve.result import RetrievedItem, Retriever, document_key

__all__ = [
    "AUDIT_EMPTY_RETRIEVAL",
    "AliasTable",
    "BM25BaselineRetriever",
    "CITATION_LOCATOR_FIELDS",
    "CLAUSE_TYPES",
    "CLAUSE_TYPE_DEADLINE",
    "CLAUSE_TYPE_DESTINATION",
    "CLAUSE_TYPE_GENERAL",
    "CLAUSE_TYPE_LABELS",
    "CLAUSE_TYPE_OTHER",
    "CLAUSE_TYPE_TARGET",
    "DEFAULT_CLAUSE_TYPE_WEIGHTS",
    "DEFAULT_EMPTY_RESPONSE",
    "FALLBACK_LEVELS",
    "FallbackAttempt",
    "FusionWeights",
    "GradedRetriever",
    "HUMAN_REVIEW_STATUS",
    "HybridEvalRetriever",
    "HybridRetriever",
    "LEXICAL_PATH",
    "LEVEL_CHAPTER",
    "LEVEL_EXACT",
    "LEVEL_EXPANDED",
    "LEVEL_NONE",
    "MergedCandidate",
    "NormalizationError",
    "ParamCheckError",
    "PathCandidate",
    "RERANK_PROFILES",
    "RerankFactors",
    "RerankProfile",
    "RerankedCandidate",
    "RetrievalHit",
    "RetrievalOutcome",
    "RetrievalParams",
    "RetrievalResult",
    "RetrievedItem",
    "Retriever",
    "STRUCTURED_PATH",
    "ScoreContractError",
    "ScoreNotNormalizedError",
    "apply_retrieval_outcome",
    "assert_versions_consistent",
    "build_alias_table",
    "chapter_positions",
    "citation_gaps",
    "citations_of_hits",
    "derive_clause_type",
    "describe_citations",
    "document_key",
    "fallback_provenance",
    "fuse",
    "is_normalized",
    "level_label",
    "lexical_candidates",
    "merge_candidates",
    "normalize_clause_type_weights",
    "normalize_max_relative",
    "normalize_quantile",
    "normalize_ratio",
    "persist_citations",
    "profile_for_id",
    "require_normalized",
    "rerank",
    "resolve_retrieval",
    "stable_primary_key",
    "stable_sort_key",
    "structured_candidates",
]
