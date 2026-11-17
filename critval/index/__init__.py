"""索引层：中文分词、BM25 倒排、结构化与版本索引、材料增量重建。

四块的分工：

* ``tokenizer`` —— 中文双字切分 + 停用词（词法侧的入口）；
* ``inverted`` —— BM25 倒排与打分（词法召回底座）；
* ``structured`` —— 结构化精确索引 + 版本索引（生效期过滤，失效版本不参与召回）；
* ``builder`` —— 材料 → 条目，按 sha256 增量重建。
"""

from critval.index.builder import MaterialIndex, MaterialSource, RebuildResult
from critval.index.inverted import BM25Params, InvertedIndex, SearchHit
from critval.index.structured import (
    IndexDocument,
    KeyMatch,
    StructuredHit,
    StructuredIndex,
    documents_from_fields,
    explain_hit,
    normalize_filters,
)
from critval.index.tokenizer import STOPWORDS, bigrams, is_stopword, normalize, tokenize

__all__ = [
    "BM25Params",
    "IndexDocument",
    "InvertedIndex",
    "KeyMatch",
    "MaterialIndex",
    "MaterialSource",
    "RebuildResult",
    "STOPWORDS",
    "SearchHit",
    "StructuredHit",
    "StructuredIndex",
    "bigrams",
    "documents_from_fields",
    "explain_hit",
    "is_stopword",
    "normalize",
    "normalize_filters",
    "tokenize",
]
