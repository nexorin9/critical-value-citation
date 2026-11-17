"""召回参数面 —— 源产品 ``RetrievalParam`` 的同构移植。

## 为什么要有一份独立的参数面

源产品的检索入口把「阈值 / 权重 / 候选池 / 知识库范围 / 重排器 / 空结果话术」
六个旋钮收在一个参数对象里，装载时逐项 ``check()``。本产品照搬这份结构，
差别只在**两个槽位换掉了指向**：

======================  ==========================  ==========================
源产品槽位              源产品指向                  本产品指向
======================  ==========================  ==========================
``similarity_threshold`` 向量 / 关键词综合分闸门      结构化 + BM25 综合分硬闸门
``keywords_similarity_weight`` 关键词路权重         BM25 词法路权重
（向量权重槽位）           向量路权重              结构化精确匹配权重 = 1 - 词法权重
======================  ==========================  ==========================

**闸门语义与源产品保持一致**：综合分低于阈值即截断（同源检索层的
``if sim[i] < similarity_threshold: break``）。向量那一半在 ``ai_role=none``
下主动放弃，所以「向量权重槽位」由结构化精确匹配接管 —— 两路权重互补、
和恒为 1 的口径不变。

## 参数非法即报错，并指明字段

``check()`` 逐项校验（源产品的 ``check_decimal_float`` / ``check_positive_number``
/ ``check_empty`` 同构），错误信息带**字段名**：面板配错一个键，报错就要说清
是哪个键、为什么错，而不是等到召回跑出来少了几条依据才去猜。

``kb_ids`` 在本产品里是**材料范围键**（``threshold`` / ``policy`` / ``corpus``
…，即 ``kb.source`` 的 key）。它**不能为空** —— 检索必须限定范围，取不到就
报错，不静默地"检索全部材料"。这条与源产品 ``check_empty(self.kb_ids)`` 同源。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Optional

from critval.retrieve.errors import ParamCheckError
from critval.retrieve.fusion import FusionWeights
from critval.retrieve.rerank import RerankProfile, profile_for_id

__all__ = [
    "DEFAULT_EMPTY_RESPONSE",
    "ParamCheckError",
    "RetrievalParams",
]

#: 召回为空时的默认话术（源产品 ``empty_response`` 的同源槽位）。
#: 口径：**没检索到依据就说没检索到**，转人工并留痕，不编依据。
DEFAULT_EMPTY_RESPONSE = "未检索到可引用的处置依据，转人工并留痕（不编造依据）"


@dataclass(frozen=True)
class RetrievalParams:
    """召回参数面（源产品 ``RetrievalParam`` 的同构移植）。

    字段与源产品一一对应，含义按上表换过：

    * ``similarity_threshold`` —— **综合分硬闸门**。融合分低于它就截断
      （同源语义）。默认 0.2。
    * ``keywords_similarity_weight`` —— **BM25 词法路权重**。结构化精确路
      的权重是 ``1 - 本值``（与源产品「向量权重 = 1 - 关键词权重」同构）。
    * ``top_n`` —— 最终返回条数（默认 8）。
    * ``top_k`` —— **候选池大小**（默认 1024）：两路各自进池的条数上限。
    * ``kb_ids`` —— **材料范围键**（必填，不许为空）。
    * ``rerank_id`` —— 规则重排档位（``""`` = 默认档）。
    * ``empty_response`` —— 召回为空时的兜底话术。
    """

    similarity_threshold: float = 0.2
    keywords_similarity_weight: float = 0.5
    top_n: int = 8
    top_k: int = 1024
    kb_ids: tuple[str, ...] = ()
    rerank_id: str = ""
    empty_response: str = DEFAULT_EMPTY_RESPONSE

    def __post_init__(self) -> None:
        # kb_ids 允许传单个字符串或任意可迭代 —— 统一收成元组，
        # 免得字符串被逐字符拆开（"threshold" 变成 9 个字符）。
        raw = self.kb_ids
        if isinstance(raw, str):
            raw = (raw,)
        object.__setattr__(self, "kb_ids", tuple(str(item) for item in raw))

    # —— 派生量 ——

    @property
    def structured_weight(self) -> float:
        """结构化精确路权重（= 1 - 词法路权重，与源产品互补写法一致）。"""
        return 1.0 - self.keywords_similarity_weight

    def fusion_weights(self) -> FusionWeights:
        """两路融合权重（和恒为 1）。"""
        return FusionWeights(
            structured=self.structured_weight,
            lexical=self.keywords_similarity_weight,
        ).validate()

    def profile(self) -> RerankProfile:
        """本参数面选中的规则重排档位。"""
        return profile_for_id(self.rerank_id)

    @classmethod
    def for_materials(
        cls,
        *kb_ids: str,
        **overrides: Any,
    ) -> "RetrievalParams":
        """便捷构造：指定材料范围键，其余用默认值。"""
        overrides.setdefault("kb_ids", tuple(kb_ids))
        return cls(**overrides)

    # —— 逐项校验（源产品 check_* 同构）——

    def check_decimal_float(self, value: Any, name: str) -> None:
        """小数校验：必须是数值且落在 ``[0, 1]``（同源语义）。"""
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ParamCheckError(
                f"{name} 必须是 0 到 1 之间的小数：{value!r}", field_name=name
            )
        if value < 0 or value > 1:
            raise ParamCheckError(
                f"{name} 必须在 [0, 1] 区间内：{value!r}", field_name=name
            )

    def check_positive_number(self, value: Any, name: str) -> None:
        """正数校验：必须大于 0（同源语义）。"""
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ParamCheckError(
                f"{name} 必须是正数：{value!r}", field_name=name
            )
        if value <= 0:
            raise ParamCheckError(
                f"{name} 必须是正数：{value!r}", field_name=name
            )

    def check_empty(self, value: Optional[Iterable[Any]], name: str) -> None:
        """非空校验：空集合即报错（同源语义：检索必须限定范围）。"""
        if value is None or len(tuple(value)) < 1:
            raise ParamCheckError(
                f"{name} 不能为空：检索必须限定材料范围，取不到就报错、不静默",
                field_name=name,
            )

    def check_text(self, value: Any, name: str) -> None:
        """文本校验：必须是字符串。"""
        if not isinstance(value, str):
            raise ParamCheckError(
                f"{name} 必须是字符串：{value!r}", field_name=name
            )

    def check(self) -> "RetrievalParams":
        """逐项校验整个参数面；任一项非法即抛 ``ParamCheckError``（带字段名）。"""
        self.check_decimal_float(self.similarity_threshold, "[召回] 综合分阈值")
        self.check_decimal_float(
            self.keywords_similarity_weight, "[召回] BM25 词法路权重"
        )
        self.check_positive_number(self.top_n, "[召回] 返回条数")
        self.check_positive_number(self.top_k, "[召回] 候选池大小")
        self.check_empty(self.kb_ids, "[召回] 材料范围")
        self.check_text(self.rerank_id, "[召回] 重排档位")
        self.check_text(self.empty_response, "[召回] 空结果话术")
        # 未知重排档位 —— 报错并指明字段（档位名直接进消息）。
        try:
            profile_for_id(self.rerank_id)
        except ParamCheckError:
            raise
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "similarity_threshold": self.similarity_threshold,
            "keywords_similarity_weight": self.keywords_similarity_weight,
            "structured_weight": self.structured_weight,
            "top_n": self.top_n,
            "top_k": self.top_k,
            "kb_ids": list(self.kb_ids),
            "rerank_id": self.rerank_id,
            "empty_response": self.empty_response,
        }
