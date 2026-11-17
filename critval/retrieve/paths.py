"""两路候选的构造 —— 结构化精确路与词法 BM25 路各自怎么打分。

## 两路各是什么

* **结构化精确路**（``structured``）—— 值班人给的是**结构化事实**：检验项目码、
  单位、危急方向、材料版本。命中就是"查表命中"，分数 = **命中键覆盖率**
  （命中了几个键 / 请求了几个键），天然落在 ``[0, 1]``。
* **词法 BM25 路**（``lexical``）—— 值班人给的是**一句自然语言**（"血红蛋白危急值
  多久要上报"）。BM25 原始分无界，分数 = **原始分 / 本次候选集内的最高原始分**
  （相对最大值口径），落在 ``[0, 1]``。

## 为什么两路用同一个 ``normalize_ratio``

口径可以不同（覆盖率 vs 相对最大值），但**分母与校验必须走同一个函数**
（``normalize.normalize_ratio``）—— 这样"两路分数在 ``[0, 1]`` 里"这句话才是
契约，而不是各写各的、靠约定维持。融合入口还会再校验一次（``fusion.fuse``），
越界直接抛错并报出是哪一路。

## 原始分也留着

归一化分是给融合用的，**原始分是给人复核用的**：BM25 原始分能让人复算"这条
为什么是这个名次"，命中键数能让人看出"精确命中了几个维度"。两者都进候选对象。
"""

from __future__ import annotations

from typing import Mapping, Optional, Sequence

from critval.index.inverted import SearchHit
from critval.index.structured import IndexDocument, StructuredHit
from critval.retrieve.merge import (
    LEXICAL_PATH,
    STRUCTURED_PATH,
    PathCandidate,
    stable_primary_key,
)
from critval.retrieve.normalize import normalize_ratio

__all__ = [
    "lexical_candidates",
    "structured_candidates",
]


def structured_candidates(
    hits: Sequence[StructuredHit],
    requested_keys: int,
) -> list[PathCandidate]:
    """结构化路候选：分数 = 命中键覆盖率。

    ``requested_keys`` 是本次查询**请求了几个键**（多键组合查询时大于 1）。
    命中键覆盖率天然在 ``[0, 1]``，但仍走 ``normalize_ratio`` 统一出口。
    """
    if requested_keys <= 0:
        raise ValueError(f"请求键数必须为正：{requested_keys}")
    candidates: list[PathCandidate] = []
    for hit in hits:
        matched = len(hit.matched_keys)
        score = normalize_ratio(matched, requested_keys, path=STRUCTURED_PATH)
        document = hit.document
        candidates.append(
            PathCandidate(
                doc_id=hit.doc_id,
                path=STRUCTURED_PATH,
                score=score,
                raw_score=float(matched),
                matched_keys=hit.matched_keys,
                matched_values=tuple(
                    (match.key, ",".join(match.values))
                    for match in hit.key_matches
                ),
                document=document,
                stable_key=stable_primary_key(document) if document is not None else "",
            )
        )
    return candidates


def lexical_candidates(
    hits: Sequence[SearchHit],
    documents: Optional[Mapping[str, IndexDocument]] = None,
) -> list[PathCandidate]:
    """词法路候选：分数 = BM25 原始分 / 本次候选集内最高原始分。

    分母为 0（没有候选或全零分）时全部记 0.0 —— 没有可比的分就不给分，
    而不是给一个假的满分。

    ``documents`` 是 ``doc_id → IndexDocument`` 的查表（材料索引侧提供），
    用来给候选算**稳定主键**；缺了它就退化成 ``doc:{doc_id}``，与结构化路
    的主键口径不一致，同一条依据就合并不掉 —— 所以两路合并时务必传进来。
    """
    if not hits:
        return []
    ceiling = max(hit.score for hit in hits)
    candidates: list[PathCandidate] = []
    for hit in hits:
        if ceiling > 0:
            score = normalize_ratio(hit.score, ceiling, path=LEXICAL_PATH)
        else:
            score = 0.0
        document = documents.get(hit.doc_id) if documents is not None else None
        candidates.append(
            PathCandidate(
                doc_id=hit.doc_id,
                path=LEXICAL_PATH,
                score=score,
                raw_score=hit.score,
                matched_terms=hit.matched_terms,
                document=document,
                stable_key=(
                    stable_primary_key(document) if document is not None else ""
                ),
            )
        )
    return candidates
