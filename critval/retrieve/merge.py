"""两路候选合并 —— 按稳定主键去重，两路的分与命中说明都留着。

## 为什么要合并

结构化精确路与词法 BM25 路**会命中同一条材料**：

* 制度条款的正文里点了检验项目码（``item_codes`` 多值键），值班人问"血红蛋白
  危急值多久要上报"时，**结构化路**按项目码精确命中这条条款，**词法路**按
  "血红蛋白 / 危急值 / 上报时限" 命中同一条；
* 阈值表行虽然只在结构化路出现，但若语料里也有它的文字描述，两路同样会撞上。

不合并的话，同一条依据在结果里出现两次（一次分高一次分低），值班人会以为
是两条不同的依据。合并的判据必须**稳定**：

* 制度条款 → ``source_sha256`` + 段落序号（材料换了内容，sha256 就变，是另一条）；
* 阈值表行 → **文件名 + 行号**（院内习惯按"某张表的第几行"指认一条依据）；
* 兜底 → ``doc_id``（内容寻址，材料没变就同一个 id）。

## 合并后保留什么

合并**不是**把两路分数一锅端。合并结果里两路的原始分与归一化分**分开记**：

* ``structured_score`` / ``lexical_score`` —— 归一化后的分（都在 ``[0, 1]``）；
* ``structured_raw_score`` / ``lexical_raw_score`` —— 归一化之前的原始分
  （结构化路是"命中了几个键"，词法路是 BM25 原始分）；
* ``matched_keys`` / ``matched_values`` —— 结构化路的命中键说明（供重排加权）；
* ``matched_terms`` —— 词法路的命中词；
* ``paths`` —— 命中了哪几路（只有一路命中的候选不会假装两路都命中）。

这样重排层（任务 id 30）可以按"命中键类型 + 命中词覆盖率"加权，冲突归因层
（任务 id 33）能看出两路对同一条款的判断差异。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Optional, Sequence

from critval.index.structured import IndexDocument

__all__ = [
    "LEXICAL_PATH",
    "MergedCandidate",
    "PathCandidate",
    "STRUCTURED_PATH",
    "merge_candidates",
    "stable_primary_key",
]

#: 两路的机器可读路名。
STRUCTURED_PATH = "structured"
LEXICAL_PATH = "lexical"


@dataclass(frozen=True)
class PathCandidate:
    """一路召回里的一个候选。

    ``score`` 是**归一化后**的分数（进入融合前必须落在 ``[0, 1]``）；
    ``raw_score`` 是归一化之前的原始分，保留下来是为了能复算"这一分是怎么来的"。
    """

    doc_id: str
    path: str
    score: float
    raw_score: float = 0.0
    matched_keys: tuple[str, ...] = ()
    matched_values: tuple[tuple[str, str], ...] = ()
    matched_terms: tuple[str, ...] = ()
    document: Optional[IndexDocument] = None
    stable_key: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "doc_id": self.doc_id,
            "path": self.path,
            "score": self.score,
            "raw_score": self.raw_score,
            "matched_keys": list(self.matched_keys),
            "matched_values": [list(pair) for pair in self.matched_values],
            "matched_terms": list(self.matched_terms),
            "stable_key": self.stable_key,
        }


@dataclass(frozen=True)
class MergedCandidate:
    """两路合并后的一个候选（同一份依据只出现一次）。"""

    stable_key: str
    doc_id: str
    structured_score: Optional[float] = None
    lexical_score: Optional[float] = None
    structured_raw_score: Optional[float] = None
    lexical_raw_score: Optional[float] = None
    matched_keys: tuple[str, ...] = ()
    matched_values: tuple[tuple[str, str], ...] = ()
    matched_terms: tuple[str, ...] = ()
    paths: tuple[str, ...] = ()
    document: Optional[IndexDocument] = None
    #: 融合分（由 ``fusion.fuse`` 写入）。
    fused_score: float = 0.0

    @property
    def locator(self) -> str:
        """一句话定位（``文件名 第 N 行 · 版本``），来自条目的出处。"""
        if self.document is None:
            return self.doc_id
        citation = self.document.citation
        if citation is None:
            return self.document.source_name
        return citation.locator

    def to_dict(self) -> dict[str, Any]:
        return {
            "stable_key": self.stable_key,
            "doc_id": self.doc_id,
            "structured_score": self.structured_score,
            "lexical_score": self.lexical_score,
            "structured_raw_score": self.structured_raw_score,
            "lexical_raw_score": self.lexical_raw_score,
            "matched_keys": list(self.matched_keys),
            "matched_values": [list(pair) for pair in self.matched_values],
            "matched_terms": list(self.matched_terms),
            "paths": list(self.paths),
            "locator": self.locator,
            "fused_score": self.fused_score,
        }


def stable_primary_key(document: IndexDocument) -> str:
    """一个条目的**稳定主键**（两路去重的判据）。

    * 制度条款 → ``policy:{材料 sha256}:{段落序号}``；
    * 阈值表行 → ``threshold:{文件名}:{行号}``（院内按"某张表第几行"指认依据）；
    * 其余（语料行等）→ ``doc:{doc_id}``（doc_id 本身是内容寻址的）。
    """
    citation = document.citation
    if citation is not None:
        if citation.kind == "policy_clause":
            locator = citation.paragraph_no
            if locator is None:
                locator = citation.line_no
            if locator is not None:
                return f"policy:{citation.source_sha256}:{locator}"
        elif citation.kind == "threshold_row":
            if citation.line_no is not None:
                return f"threshold:{document.source_name}:{citation.line_no}"
    return f"doc:{document.doc_id}"


def _candidate_key(candidate: PathCandidate) -> str:
    if candidate.stable_key:
        return candidate.stable_key
    if candidate.document is not None:
        return stable_primary_key(candidate.document)
    return f"doc:{candidate.doc_id}"


def merge_candidates(
    structured: Sequence[PathCandidate] = (),
    lexical: Sequence[PathCandidate] = (),
) -> list[MergedCandidate]:
    """按稳定主键合并两路候选。

    * 同一份依据被两路命中 → 合成**一条**，两路的分与命中说明都留着；
    * 只被一路命中 → 也保留，``paths`` 里只写这一路；
    * 同一路里出现重复主键 → 取该路**分高**的一条（不重复计数）。

    输出按稳定主键升序（确定性），融合排序由 ``fusion.fuse`` 负责。
    """
    merged: dict[str, MergedCandidate] = {}
    order: list[str] = []

    def _absorb(candidate: PathCandidate) -> None:
        key = _candidate_key(candidate)
        existing = merged.get(key)
        if existing is None:
            merged[key] = MergedCandidate(
                stable_key=key,
                doc_id=candidate.doc_id,
                structured_score=None,
                lexical_score=None,
                structured_raw_score=None,
                lexical_raw_score=None,
                matched_keys=(),
                matched_values=(),
                matched_terms=(),
                paths=(),
                document=candidate.document,
            )
            order.append(key)
            existing = merged[key]
        if candidate.path == STRUCTURED_PATH:
            better = (
                existing.structured_score is None
                or candidate.score > existing.structured_score
            )
            if better:
                merged[key] = replace(
                    existing,
                    structured_score=candidate.score,
                    structured_raw_score=candidate.raw_score,
                    matched_keys=candidate.matched_keys,
                    matched_values=candidate.matched_values,
                )
        elif candidate.path == LEXICAL_PATH:
            better = (
                existing.lexical_score is None
                or candidate.score > existing.lexical_score
            )
            if better:
                merged[key] = replace(
                    existing,
                    lexical_score=candidate.score,
                    lexical_raw_score=candidate.raw_score,
                    matched_terms=candidate.matched_terms,
                )
        else:
            raise ValueError(f"不认识的召回路径：{candidate.path}")

        current = merged[key]
        if candidate.path not in current.paths:
            merged[key] = replace(current, paths=current.paths + (candidate.path,))
        if current.document is None and candidate.document is not None:
            merged[key] = replace(merged[key], document=candidate.document)

    for candidate in structured:
        _absorb(candidate)
    for candidate in lexical:
        _absorb(candidate)

    return [merged[key] for key in sorted(order)]
