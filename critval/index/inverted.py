"""BM25 倒排索引 —— 词法召回的底座。

## 为什么自己写 BM25，而不是用现成的向量库

本产品的 ``ai_role`` 是 ``none``（不接模型网关），召回全部靠确定性路径完成：
一条危急值要能在**离线复算**的前提下说清"为什么命中了这条制度条款"。向量召回
做不到这件事 —— 同一份材料两次嵌入可能给出略有差异的排序，而值班人复核时需要
的是"这条依据为什么排在第一"这种能复述的答案。

BM25 的每个量都能落到纸面上：词频 tf、文档频率 df、平均文档长度 avgdl、
参数 k1 / b。打分公式没有黑箱。

## 关键口径

* **重复条目按覆盖处理**：同一个 ``doc_id`` 再加一次 = 覆盖上一次，文档数不变、
  倒排表里不会重复计 tf。这对应真实场景 —— 材料投递目录里同一份文件被反复投递
  （task id 27 的投递增量入库），索引不能因此把一份制度算成两份、把得分翻倍。
* **统计量增量维护**：df / 总长度随增删实时更新，**不做全量重算**。这是"增量
  重建耗时低于全量重建"能成立的前提 —— 否则改一份材料也要把整个语料的统计量
  重算一遍。
* **同分稳定序**：得分相同按 ``doc_id`` 升序。复核时"上次排第一的条款这次排第
  三"必须是可解释的，不能是随运行漂移。
* **空索引不炸**：没有文档时搜索返回空列表，``avgdl`` 为 0 不参与除法。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

__all__ = ["BM25Params", "InvertedIndex", "SearchHit"]


@dataclass(frozen=True)
class BM25Params:
    """BM25 参数面（k1 / b 可配）。

    * ``k1`` —— 词频饱和点。取值 1.2 ~ 2.0 之间；越大越看重"同一文档里重复出现"。
    * ``b`` —— 文档长度归一化强度。0 = 完全不管文档长短；1 = 完全按长度归一。
      制度条款长短差异大（有的三行、有的一段），取 0.75 兼顾。
    """

    k1: float = 1.5
    b: float = 0.75

    def validate(self) -> "BM25Params":
        if self.k1 < 0:
            raise ValueError(f"BM25 参数 k1 不能为负：{self.k1}")
        if not 0.0 <= self.b <= 1.0:
            raise ValueError(f"BM25 参数 b 必须落在 [0, 1]：{self.b}")
        return self


@dataclass(frozen=True)
class SearchHit:
    """一次命中的结果。"""

    doc_id: str
    score: float
    matched_terms: tuple[str, ...] = ()
    payload: Any = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "doc_id": self.doc_id,
            "score": self.score,
            "matched_terms": list(self.matched_terms),
        }


@dataclass
class InvertedIndex:
    """倒排索引：``term → {doc_id: tf}``。

    统计量（df / 文档长度 / 平均长度）随增删实时维护，不做全量重算。
    """

    params: BM25Params = field(default_factory=BM25Params)
    _postings: dict[str, dict[str, int]] = field(default_factory=dict, init=False)
    _doc_len: dict[str, int] = field(default_factory=dict, init=False)
    _doc_terms: dict[str, frozenset[str]] = field(default_factory=dict, init=False)
    _payloads: dict[str, Any] = field(default_factory=dict, init=False)
    _total_len: int = field(default=0, init=False)
    _replacements: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        self.params = self.params.validate()

    # —— 写入 ——

    def add(
        self,
        doc_id: str,
        tokens: Sequence[str] | Iterable[str],
        payload: Any = None,
    ) -> bool:
        """加入（或覆盖）一篇文档。

        返回 ``True`` 表示这是一次**覆盖**（同一个 ``doc_id`` 之前已在索引里）。
        """
        if not doc_id:
            raise ValueError("doc_id 不能为空")
        token_list = list(tokens)
        replaced = doc_id in self._doc_len
        if replaced:
            self.remove(doc_id)
        counts: dict[str, int] = {}
        for token in token_list:
            counts[token] = counts.get(token, 0) + 1
        for token, tf in counts.items():
            self._postings.setdefault(token, {})[doc_id] = tf
        self._doc_len[doc_id] = len(token_list)
        self._doc_terms[doc_id] = frozenset(counts)
        self._payloads[doc_id] = payload
        self._total_len += len(token_list)
        if replaced:
            self._replacements += 1
        return replaced

    def remove(self, doc_id: str) -> bool:
        """移除一篇文档。返回是否真的移掉了。"""
        if doc_id not in self._doc_len:
            return False
        for token in self._doc_terms.get(doc_id, frozenset()):
            postings = self._postings.get(token)
            if postings is None:
                continue
            postings.pop(doc_id, None)
            if not postings:
                self._postings.pop(token, None)
        length = self._doc_len.pop(doc_id, 0)
        self._doc_terms.pop(doc_id, None)
        self._payloads.pop(doc_id, None)
        self._total_len -= length
        return True

    def clear(self) -> None:
        """清空索引（重建时用）。"""
        self._postings.clear()
        self._doc_len.clear()
        self._doc_terms.clear()
        self._payloads.clear()
        self._total_len = 0

    # —— 统计 ——

    def __len__(self) -> int:
        return len(self._doc_len)

    def __contains__(self, doc_id: object) -> bool:
        return doc_id in self._doc_len

    @property
    def doc_count(self) -> int:
        return len(self._doc_len)

    @property
    def term_count(self) -> int:
        return len(self._postings)

    @property
    def total_length(self) -> int:
        return self._total_len

    @property
    def replacements(self) -> int:
        """被覆盖掉的重复条目次数。"""
        return self._replacements

    @property
    def average_length(self) -> float:
        if not self._doc_len:
            return 0.0
        return self._total_len / len(self._doc_len)

    def document_frequency(self, term: str) -> int:
        """多少篇文档含这个词。"""
        postings = self._postings.get(term)
        return len(postings) if postings else 0

    def document_length(self, doc_id: str) -> int:
        return self._doc_len.get(doc_id, 0)

    def terms_of(self, doc_id: str) -> tuple[str, ...]:
        return tuple(sorted(self._doc_terms.get(doc_id, frozenset())))

    def doc_ids(self) -> tuple[str, ...]:
        return tuple(self._doc_len.keys())

    def payload_of(self, doc_id: str) -> Any:
        return self._payloads.get(doc_id)

    def stats(self) -> dict[str, Any]:
        return {
            "doc_count": self.doc_count,
            "term_count": self.term_count,
            "total_length": self._total_len,
            "average_length": round(self.average_length, 4),
            "replacements": self._replacements,
            "k1": self.params.k1,
            "b": self.params.b,
        }

    # —— 打分 ——

    def idf(self, term: str) -> float:
        """逆文档频率。

        用 ``ln(1 + (N - df + 0.5) / (df + 0.5))`` 而不是经典的
        ``ln((N - df + 0.5) / (df + 0.5))``：后者在 df 接近 N 时会取到负数，
        负 idf 会把"到处都出现的词"变成负贡献，排序会翻车。
        """
        n = self.doc_count
        df = self.document_frequency(term)
        if n <= 0:
            return 0.0
        return math.log(1.0 + (n - df + 0.5) / (df + 0.5))

    def score_document(self, query_tokens: Sequence[str], doc_id: str) -> float:
        """一篇文档的 BM25 得分。"""
        if doc_id not in self._doc_len or not self.doc_count:
            return 0.0
        doc_len = self._doc_len[doc_id]
        avgdl = self.average_length or 1.0
        k1 = self.params.k1
        b = self.params.b
        total = 0.0
        for term in dict.fromkeys(query_tokens):
            postings = self._postings.get(term)
            if not postings:
                continue
            tf = postings.get(doc_id)
            if not tf:
                continue
            denom = tf + k1 * (1.0 - b + b * doc_len / avgdl)
            if denom <= 0:
                continue
            total += self.idf(term) * (tf * (k1 + 1.0)) / denom
        return total

    def match_count(
        self,
        query_tokens: Sequence[str] | Iterable[str],
    ) -> int:
        """有多少篇条目**至少命中一个查询词**（不含 ``top_k`` 截断）。

        用来回答"候选池截掉了多少条" —— ``search`` 只返回前 ``top_k`` 条，
        光看返回值是答不上这个问题的。
        """
        if not self.doc_count:
            return 0
        tokens = list(query_tokens)
        if not tokens:
            return 0
        candidates: set[str] = set()
        for term in dict.fromkeys(tokens):
            postings = self._postings.get(term)
            if postings:
                candidates.update(postings)
        return len(candidates)

    def search(
        self,
        query_tokens: Sequence[str] | Iterable[str],
        top_k: int = 10,
    ) -> list[SearchHit]:
        """检索：返回得分最高的 ``top_k`` 篇。

        查询词按**去重后**参与打分（经典 BM25 的做法：查询侧词频不重复加权）。
        排序同分时按 ``doc_id`` 升序，保证同样输入两次运行结果逐位一致。
        """
        if top_k <= 0:
            return []
        tokens = list(query_tokens)
        if not tokens or not self.doc_count:
            return []
        candidates: set[str] = set()
        for term in dict.fromkeys(tokens):
            postings = self._postings.get(term)
            if postings:
                candidates.update(postings)
        if not candidates:
            return []
        hits: list[SearchHit] = []
        for doc_id in candidates:
            score = self.score_document(tokens, doc_id)
            if score <= 0.0:
                continue
            matched = tuple(
                term
                for term in dict.fromkeys(tokens)
                if doc_id in (self._postings.get(term) or {})
            )
            hits.append(
                SearchHit(
                    doc_id=doc_id,
                    score=score,
                    matched_terms=matched,
                    payload=self._payloads.get(doc_id),
                )
            )
        hits.sort(key=lambda hit: (-hit.score, hit.doc_id))
        return hits[:top_k]
