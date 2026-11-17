"""打分归一化契约 —— 两路分数必须落在同一把尺子上。

## 为什么要有这份契约

源产品的检索主路径把「向量相似度」与「关键词相似度」按互补权重直接相加
（``agent/component/retrieval.py``：``retrieval(..., similarity_threshold,
1 - keywords_similarity_weight, ...)``）。权重互补说明**两路分数被当成同一把
尺子上的数** —— 那把尺子就是 ``[0, 1]``。

本产品不做向量召回，但融合结构照搬：**结构化精确路**（查表命中）与**词法
BM25 路**（自然语言命中）的分数要按同一把尺子加权。BM25 原始分是无界的
（idf 与 tf 决定），把它和 0/1 的结构化命中分直接相加，等于拿"米"去加
"千克"；权重一旦调过，排序就没法解释，值班人问"为什么这条排第一"也答不上。

因此这里定一份**归一化契约**：

* 进入融合的分数必须落在 **``[0, 1]``**；
* 越界的分数**在融合入口直接抛错**，错误信息指明**哪一路**、**多少分**——
  悄悄截断会让"结构化权重 0.6"变成一句谎话（分数被截到 1.0，权重就失效了）；
* 归一化只有三个口径（**比例 / 分位 / 相对最大值**），**两路统一走同一个
  ``normalize_ratio``**（另两个作为调参口径备选，见任务 id 31 的网格扫描）。

## 三个口径的取舍

* ``normalize_ratio(score, max_possible)`` —— **默认口径**。分母由调用方给：
  结构化路给"请求了几个键"（命中键覆盖率），词法路给"本次候选集里 BM25 的
  最高原始分"。同一次查询里两路都用比例口径，尺度可比。
* ``normalize_quantile(scores)`` —— 分位口径。把一批分数按名次映射到
  ``(0, 1]``，同分取平均名次（确定性，不随运行漂移）。调参时用它能看"这一路
  的名次"，不依赖分数的绝对量纲。
* ``normalize_max_relative(scores)`` —— 相对最大值口径，等价于比例口径在
  "分母 = 集合内最大值"时的特例，保留成独立函数是为了让调用方的意图写在
  代码里，而不是现场手写 ``score / max(scores)``。
"""

from __future__ import annotations

import math
from typing import Any, Iterable, Mapping, Optional, Sequence

__all__ = [
    "NORMALIZED_MAX",
    "NORMALIZED_MIN",
    "NormalizationError",
    "ScoreContractError",
    "ScoreNotNormalizedError",
    "bm25_max_possible",
    "is_normalized",
    "normalize_max_relative",
    "normalize_quantile",
    "normalize_ratio",
    "require_normalized",
]

#: 归一化后的分数区间 —— 契约里唯一认可的尺子。
NORMALIZED_MIN = 0.0
NORMALIZED_MAX = 1.0

#: 路径名 → 人话标签（错误信息里用，避免值班人看到英文路名一头雾水）。
PATH_LABELS = {
    "structured": "结构化精确路",
    "lexical": "词法 BM25 路",
}


def path_label(path: str) -> str:
    """路径 id → 人话标签（未知路径原样返回）。"""
    return PATH_LABELS.get(path, path or "未标注路径")


class ScoreContractError(ValueError):
    """归一化契约被违反。

    ``path`` 是机器可读的路名（``structured`` / ``lexical``），``score`` 是违规
    分值，``detail`` 是补充说明。三者都带上，便于调用方把错误原样写进审计。
    """

    def __init__(
        self,
        message: str,
        *,
        path: str = "",
        score: Optional[float] = None,
        detail: str = "",
    ) -> None:
        super().__init__(message)
        self.path = path
        self.score = score
        self.detail = detail

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "path_label": path_label(self.path),
            "score": self.score,
            "detail": self.detail,
            "message": str(self),
        }


class ScoreNotNormalizedError(ScoreContractError):
    """分数不在 ``[0, 1]`` —— 融合入口必须拦下它。"""

    def __init__(self, path: str, score: Any, detail: str = "") -> None:
        message = (
            f"归一化契约被违反：{path_label(path)}（path={path or 'unknown'}）的分数 "
            f"{score!r} 不在 [{NORMALIZED_MIN}, {NORMALIZED_MAX}] 区间内。"
            " 融合前必须先归一化（normalize_ratio / normalize_quantile / "
            "normalize_max_relative），分数越界说明两路不在同一把尺子上。"
        )
        if detail:
            message = f"{message} 补充：{detail}"
        super().__init__(message, path=path, score=score, detail=detail)


class NormalizationError(ScoreContractError):
    """归一化输入本身不合法（分母非正、分数表为空、键重复等）。"""


def is_normalized(score: Any) -> bool:
    """这个分数是否落在 ``[0, 1]``（NaN / inf 都不算）。"""
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        return False
    if not math.isfinite(float(score)):
        return False
    return NORMALIZED_MIN <= float(score) <= NORMALIZED_MAX


def require_normalized(path: str, score: Any) -> float:
    """校验一个分数已归一；不合规直接抛 ``ScoreNotNormalizedError``。

    这是**融合入口的唯一闸门**：两路分数各自算得好不好是召回层的事，但只要
    有一路拿没归一的分数来融合，这里就拦下来，并报出是哪一路、多少分。
    """
    if not is_normalized(score):
        raise ScoreNotNormalizedError(path, score)
    return float(score)


def normalize_ratio(
    score: float,
    max_possible: float,
    *,
    path: str = "",
) -> float:
    """比例归一化：``normalized = score / max_possible``。

    分母非正 → ``NormalizationError``；算出来超过 1 → ``ScoreNotNormalizedError``
    （说明"理论上界"给小了，或打分口径串了，截断会掩盖问题）。
    """
    if not isinstance(max_possible, (int, float)) or isinstance(max_possible, bool):
        raise NormalizationError(
            f"归一化分母必须是数值：{max_possible!r}",
            path=path,
            detail="max_possible 非法",
        )
    if max_possible <= 0:
        raise NormalizationError(
            f"归一化分母必须为正：{max_possible!r}",
            path=path,
            score=score,
            detail="max_possible 非正",
        )
    if not isinstance(score, (int, float)) or isinstance(score, bool):
        raise NormalizationError(
            f"待归一化的分数必须是数值：{score!r}",
            path=path,
            detail="score 非法",
        )
    value = float(score) / float(max_possible)
    if value < NORMALIZED_MIN:
        # 负分说明这一路的打分方向反了，同样不许悄悄截断。
        raise ScoreNotNormalizedError(path, value, detail=f"原始分 {score} 为负")
    if value > NORMALIZED_MAX:
        raise ScoreNotNormalizedError(
            path,
            value,
            detail=f"原始分 {score} 超过理论上限 {max_possible}",
        )
    return value


def normalize_quantile(
    scores: Mapping[str, float],
    *,
    path: str = "",
) -> dict[str, float]:
    """分位归一化：按名次映射到 ``(0, 1]``，同分取平均名次。

    确定性：同样的输入两次运行逐位一致（名次相同时按 key 排序打破并列）。
    """
    if not scores:
        raise NormalizationError(
            "分位归一化需要至少一个分数", path=path, detail="scores 为空"
        )
    ordered = sorted(scores.items(), key=lambda item: (item[1], item[0]))
    total = len(ordered)
    ranks: dict[str, float] = {}
    index = 0
    while index < total:
        end = index
        while end + 1 < total and ordered[end + 1][1] == ordered[index][1]:
            end += 1
        # 1 起的平均名次：并列时取首末的平均，保证同分同值。
        average_rank = (index + end) / 2.0 + 1.0
        for position in range(index, end + 1):
            ranks[ordered[position][0]] = average_rank
        index = end + 1
    return {key: ranks[key] / total for key, _ in ordered}


def normalize_max_relative(
    scores: Mapping[str, float],
    *,
    path: str = "",
) -> dict[str, float]:
    """相对最大值归一化：``score / max(scores)``（集合内最好的一条记 1.0）。

    集合内最大分非正时全部记 0.0（没有可比的分，就不给分）。
    """
    if not scores:
        raise NormalizationError(
            "相对最大值归一化需要至少一个分数", path=path, detail="scores 为空"
        )
    ceiling = max(scores.values())
    if ceiling <= 0:
        return {key: 0.0 for key in scores}
    return {key: float(value) / float(ceiling) for key, value in scores.items()}


def bm25_max_possible(index: Any, query_tokens: Sequence[str] | Iterable[str]) -> float:
    """BM25 原始分的**理论上界**（本查询下任何一篇文档都不可能超过它）。

    推导：单篇得分 ``Σ idf(t) · tf(t)(k1+1) / (tf(t) + k1·(1-b+b·dl/avgdl))``。
    分母里的长度归一项非负，故每个词的贡献 ``≤ idf(t)·(k1+1)``（tf → ∞ 时取到
    极限）。于是整篇的上界是 ``Σ idf(t)·(k1+1)``。

    用途：给词法路一个**不依赖候选集**的归一化分母（相对最大值口径依赖本次
    候选集，换一批候选就换了尺子；调参对比时要用绝对口径才可比）。
    """
    tokens = list(dict.fromkeys(query_tokens))
    if not tokens:
        return 0.0
    total = 0.0
    for token in tokens:
        total += index.idf(token) * (index.params.k1 + 1.0)
    return total
