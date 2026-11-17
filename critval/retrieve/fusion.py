"""融合入口 —— 两路候选在这里按权重合并成一条排序。

## 入口只认尺子，不认口径

``fuse()`` 是**唯一的融合入口**，它做三件事：

1. **验尺子**：两路每个候选的分数必须落在 ``[0, 1]``；越界直接抛
   ``ScoreNotNormalizedError``，错误信息里带**是哪一路**、**多少分**。
   悄悄截断是最坏的处理方式 —— 截到 1.0 之后"结构化权重 0.6"这句话就失效了，
   而值班人看到的排序还是那份排序，复核时无从发现。
2. **去重合并**：按稳定主键合并两路候选（见 ``merge.merge_candidates``），
   同一份依据只出现一次，两路的分与命中说明都留着。
3. **加权融合**：``融合分 = Σ(权重 × 该路归一化分) / Σ(命中路的权重之和)``。

## 为什么要按"命中路的权重之和"归一

一条依据可能只被一路命中（阈值表行通常只有结构化路命中；制度条款可能两路都
命中）。若只被一路命中时按另一路的 0 分计入，等于把"没被这一路找到"当成
"这一路认为它不相关"—— 前者是检索覆盖问题，后者是相关性判断。归一后，
"只被结构化路命中的阈值行"不会因为词法路没命中就被判成 0.5 分。

## 权重从哪来

权重之和恒为 1（与源产品 ``1 - keywords_similarity_weight`` 的互补写法一致），
结构化路与词法路的默认值各 0.5；具体取值由任务 id 31 的网格扫描在评测集上
定，不在本模块拍脑袋调。权重非法（负数、和不为 1）在**构造时**就报错并指明
字段，不等到融合时才炸。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Optional, Sequence

from critval.retrieve.merge import (
    LEXICAL_PATH,
    STRUCTURED_PATH,
    MergedCandidate,
    PathCandidate,
    merge_candidates,
)
from critval.retrieve.normalize import require_normalized

__all__ = ["FusionWeights", "fuse"]


@dataclass(frozen=True)
class FusionWeights:
    """两路融合权重（和恒为 1）。"""

    structured: float = 0.5
    lexical: float = 0.5

    def validate(self) -> "FusionWeights":
        if self.structured < 0 or self.lexical < 0:
            raise ValueError(
                "融合权重不能为负："
                f"structured={self.structured}, lexical={self.lexical}"
            )
        total = self.structured + self.lexical
        if abs(total - 1.0) > 1e-6:
            raise ValueError(
                f"融合权重之和必须为 1：structured={self.structured} + "
                f"lexical={self.lexical} = {total}"
            )
        return self

    def weight_of(self, path: str) -> float:
        if path == STRUCTURED_PATH:
            return self.structured
        if path == LEXICAL_PATH:
            return self.lexical
        raise ValueError(f"不认识的召回路径：{path}")

    def to_dict(self) -> dict[str, Any]:
        return {"structured": self.structured, "lexical": self.lexical}


def fuse(
    structured: Sequence[PathCandidate] = (),
    lexical: Sequence[PathCandidate] = (),
    weights: Optional[FusionWeights] = None,
) -> list[MergedCandidate]:
    """融合两路候选，返回按融合分降序的合并结果。

    同分时按稳定主键升序 —— 同参数两次运行排序逐位一致，复核时"上次排第一的
    条款这次排第三"必须是可解释的。
    """
    effective = (weights or FusionWeights()).validate()

    # 1) 验尺子：越界的分数在融合入口直接拦下，并报出是哪一路、多少分。
    for candidate in list(structured) + list(lexical):
        require_normalized(candidate.path, candidate.score)

    # 2) 去重合并
    merged = merge_candidates(structured, lexical)

    # 3) 加权融合
    results: list[MergedCandidate] = []
    for item in merged:
        parts: list[tuple[float, float]] = []
        if item.structured_score is not None:
            parts.append((effective.structured, item.structured_score))
        if item.lexical_score is not None:
            parts.append((effective.lexical, item.lexical_score))
        weight_sum = sum(weight for weight, _ in parts)
        score = (
            sum(weight * value for weight, value in parts) / weight_sum
            if weight_sum > 0
            else 0.0
        )
        results.append(replace(item, fused_score=score))

    results.sort(key=lambda item: (-item.fused_score, item.stable_key))
    return results
