"""召回降级层级 —— 0 原样 / 1 关键词扩展 / 2 章节级兜底 / 3 无依据转人工。

## 为什么要有「层级」这回事

源产品的检索层在综合分低于 ``similarity_threshold`` 时**一刀切**地停下
（``if sim[i] < similarity_threshold: break``），停下之后就是空的
（``retrieval.py:77-81`` —— 取不到就返回空结果 + 一句 ``empty_response``）。

本产品不做一刀切。院内场景里"原样查不到"往往是**问法问题**而不是"没依据"：

* 值班人问的是"血红蛋白"，制度里写的是项目码"718-7" —— 换个说法就有依据；
* 制度里没点名这个项目，但**同一条流程照样适用**（第三章的时限与通知对象
  对所有危急值项目一视同仁）。

所以本产品把"查不到"拆成**四级**，每降一级都把依据的**可信度**标出来：

======================  ==================  ==========================
层级                    含义                值班人怎么理解
======================  ==================  ==========================
``0`` 原样查询          按原问法查到了      这条依据是直接命中的，最硬
``1`` 关键词扩展        换了个说法才查到    依据成立，但要看清是"扩展"来的
``2`` 章节级兜底        定位到制度对应章节  依据是**同章节**的条款，不是点名这条的
``3`` 无依据转人工      四级用尽仍无依据    没有依据，转人工，**不编**
======================  ==================  ==========================

## 最硬的一条线

第 3 级**一条引用都不产出**。降级的目的是"多找找"，不是"凑一条"——
真找不到就说找不到，转人工并留痕（沿用任务 id 8 的 ``empty_response`` 语义）。

层级常量住在**召回层**，卡片层（``critval.card.provenance``）引用同一份，
避免两处各写一套层级号。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = [
    "FALLBACK_LEVELS",
    "LEVEL_CHAPTER",
    "LEVEL_EXACT",
    "LEVEL_EXPANDED",
    "LEVEL_NONE",
    "FallbackAttempt",
    "level_label",
]

#: 原样查询（没降级）。
LEVEL_EXACT = 0
#: 关键词扩展（按别名表扩展查询后重跑召回）。
LEVEL_EXPANDED = 1
#: 章节级兜底（按结构化路命中的项目码定位制度章节，取该章节条款）。
LEVEL_CHAPTER = 2
#: 无依据转人工（降级用尽，**不产出引用**）。
LEVEL_NONE = 3

#: 层级 → 人话标签（卡片、CLI、日志共用一份）。
FALLBACK_LEVELS: dict[int, str] = {
    LEVEL_EXACT: "原样查询",
    LEVEL_EXPANDED: "关键词扩展",
    LEVEL_CHAPTER: "章节级兜底",
    LEVEL_NONE: "无依据转人工",
}


def level_label(level: int) -> str:
    """层级号 → 人话标签（未知层级不编名字，标明是第几级）。"""
    try:
        key = int(level)
    except (TypeError, ValueError):
        return f"第 {level} 级降级"
    return FALLBACK_LEVELS.get(key, f"第 {key} 级降级")


@dataclass(frozen=True)
class FallbackAttempt:
    """一次降级尝试的留痕（哪一级、查了什么、捞到几条）。

    留痕的价值：值班人问"为什么这条依据是降级来的"，这里能答出
    **试过哪几级、每一级查了什么词、每一级捞到几条**。
    """

    level: int
    query: str = ""
    hit_count: int = 0
    note: str = ""

    @property
    def label(self) -> str:
        return level_label(self.level)

    def to_dict(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "level_label": self.label,
            "query": self.query,
            "hit_count": self.hit_count,
            "note": self.note,
        }
