"""条款类型标签与类型权重表 —— 制度条款的「用途分类」。

## 为什么重排还要按条款类型加权

同一部危急值报告制度里，不同条款的**用途**差别很大：

* 「上报时限：10 分钟内」—— 值班人问「多久要报」时，只有这条直接回答；
* 「记录去向：危急值登记系统」—— 问「报到哪里」时才有用；
* 「通知对象：当班检验科技师」—— 问「找谁」时才有用；
* 「本制度自发布之日起施行」—— 总则，**哪一问都不直接回答**。

融合分只回答「这条依据有多相关」，回答不了「这条依据**是干什么用的**」。
所以重排层在字段命中权重、章节位置权重、词项覆盖率之外，还要加一层
**条款类型加权**：时限与去向条款优先于总则条款。

## 标签从哪来 —— 不猜，用解析字段

标签**由制度条款解析字段推导**，不是关键词猜测。制度解析层（``kb/policy.py``）
已经把「通知对象 / 上报时限 / 记录去向」三项事实抽了出来，重排层直接读它们。
再自己用关键词猜一遍，等于同一口径两份实现 —— 解析层改了口径，重排层还按
旧的算，两边对不上，出处与结论就对不上了。

条款条目进索引时，这三项事实随行进入结构化字段（``critval/index/builder.py``），
所以重排层拿到条目就能判断它**是干什么用的**。

## 五个类型

======================  ==================  ==========================================
类型                    标签                推导依据
======================  ==================  ==========================================
``target``              通知对象            条款写了「通知对象：…」
``deadline``            上报时限            条款写了「上报时限：…」
``destination``         记录去向            条款写了「记录去向：…」
``general``             总则                制度条款，但三项事实都没写
``other``               其他                非制度条款（阈值表行 / 条款语料行）
======================  ==================  ==========================================

一条条款可能同时写了多项事实（样例制度里 5 条写全三项）。**主类型**按用途
优先级取一个：``deadline`` > ``destination`` > ``target`` —— 危急值处置里
「多久内上报」是硬时限，「报到哪里」「通知谁」是并列责任。

## 权重表可配，非法即报错并指明字段

类型权重表随重排档位走（``RerankProfile.clause_type_weights``）：

* **负数** → 报错并指明是哪个类型的哪个字段；
* **未知类型名** → 报错并列出可用类型；
* **全零** → 报错（类型因子无从取值）。

权重表给的是**相对优先关系**：表里写到的类型按「表内最大值」归一，表里没写
到的类型沿用默认表里的份额。这样院内只想把「通知对象」条款提到前面时，改一
个键就行，不必把五种类型都重写一遍。

**总则条款被打折、非制度条款不打折**是这张表的默认口径：总则条款不直接回答
值班人的任何一问，压一压是对的；阈值表行是判定依据本身、条款语料行是批量
条款正文，都不该因为「不是制度条款」而吃亏。
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Any, Mapping, Optional

from critval.retrieve.errors import ParamCheckError

__all__ = [
    "CLAUSE_TYPE_DESTINATION",
    "CLAUSE_TYPE_DEADLINE",
    "CLAUSE_TYPE_FACT_FIELDS",
    "CLAUSE_TYPE_GENERAL",
    "CLAUSE_TYPE_LABELS",
    "CLAUSE_TYPE_OTHER",
    "CLAUSE_TYPE_PRIORITY",
    "CLAUSE_TYPE_TARGET",
    "CLAUSE_TYPES",
    "DEFAULT_CLAUSE_TYPE_WEIGHTS",
    "clause_type_label",
    "derive_clause_type",
    "normalize_clause_type_weights",
]

#: 通知对象类条款。
CLAUSE_TYPE_TARGET = "target"
#: 上报时限类条款。
CLAUSE_TYPE_DEADLINE = "deadline"
#: 记录去向类条款。
CLAUSE_TYPE_DESTINATION = "destination"
#: 总则类条款（制度条款但没写三项事实）。
CLAUSE_TYPE_GENERAL = "general"
#: 非制度条款（阈值表行 / 条款语料行）。
CLAUSE_TYPE_OTHER = "other"

#: 全部条款类型（权重表、校验、报错的可用类型清单）。
CLAUSE_TYPES: tuple[str, ...] = (
    CLAUSE_TYPE_DEADLINE,
    CLAUSE_TYPE_DESTINATION,
    CLAUSE_TYPE_TARGET,
    CLAUSE_TYPE_OTHER,
    CLAUSE_TYPE_GENERAL,
)

#: 类型 → 人读标签（CLI 与值班台用中文，不给英文标识）。
CLAUSE_TYPE_LABELS: Mapping[str, str] = MappingProxyType(
    {
        CLAUSE_TYPE_DEADLINE: "上报时限",
        CLAUSE_TYPE_DESTINATION: "记录去向",
        CLAUSE_TYPE_TARGET: "通知对象",
        CLAUSE_TYPE_OTHER: "其他",
        CLAUSE_TYPE_GENERAL: "总则",
    }
)

#: 制度解析层抽出的三项事实字段 → 条款类型。
#:
#: 字段名与 ``kb.policy.FACT_FIELDS`` 同源（一份事实字段清单，不抄第二遍）。
CLAUSE_TYPE_FACT_FIELDS: Mapping[str, str] = MappingProxyType(
    {
        "notify_target": CLAUSE_TYPE_TARGET,
        "deadline": CLAUSE_TYPE_DEADLINE,
        "record_destination": CLAUSE_TYPE_DESTINATION,
    }
)

#: 一条条款同时写了多项事实时的**主类型优先级**（先命中者为主类型）。
CLAUSE_TYPE_PRIORITY: tuple[str, ...] = (
    CLAUSE_TYPE_DEADLINE,
    CLAUSE_TYPE_DESTINATION,
    CLAUSE_TYPE_TARGET,
)

#: 默认类型权重表（相对优先关系，归一后落在 ``(0, 1]``）。
#:
#: * 时限 / 去向 / 通知对象 —— 操作细则类，直接回答值班人的某一问；
#: * 其他 —— 非制度条款，**中性不打折**（阈值依据行是判定依据本身）；
#: * 总则 —— 不直接回答任何一问，压一压。
DEFAULT_CLAUSE_TYPE_WEIGHTS: Mapping[str, float] = MappingProxyType(
    {
        CLAUSE_TYPE_DEADLINE: 1.0,
        CLAUSE_TYPE_DESTINATION: 0.95,
        CLAUSE_TYPE_TARGET: 0.90,
        CLAUSE_TYPE_OTHER: 1.0,
        CLAUSE_TYPE_GENERAL: 0.60,
    }
)


def clause_type_label(clause_type: str) -> str:
    """类型 → 人读标签（未知类型给「其他」，不抛错 —— 标签只用于展示）。"""
    return CLAUSE_TYPE_LABELS.get(clause_type, CLAUSE_TYPE_LABELS[CLAUSE_TYPE_OTHER])


def derive_clause_type(document: Optional[Any]) -> str:
    """从条目推导条款类型。

    * 制度条款：按 ``通知对象 / 上报时限 / 记录去向`` 三项事实推导主类型；
      三项都没写 → ``general``（总则）；
    * 其它条目（阈值表行 / 条款语料行）：``other``。

    推导只看**结构化字段**，不看正文关键词 —— 正文里的"上报"两个字不等于这条
    条款就是时限条款，事实字段才是制度解析层认过的口径。
    """
    if document is None:
        return CLAUSE_TYPE_OTHER
    kind = getattr(document, "kind", "")
    fields = getattr(document, "fields", None) or {}
    if kind == "policy_clause":
        for clause_type in CLAUSE_TYPE_PRIORITY:
            fact_field = _fact_field_of(clause_type)
            value = fields.get(fact_field) or ""
            if str(value).strip():
                return clause_type
        return CLAUSE_TYPE_GENERAL
    return CLAUSE_TYPE_OTHER


def _fact_field_of(clause_type: str) -> str:
    """类型 → 对应的事事实字段名。"""
    for field_name, mapped in CLAUSE_TYPE_FACT_FIELDS.items():
        if mapped == clause_type:
            return field_name
    return ""


def normalize_clause_type_weights(
    weights: Optional[Mapping[str, float]] = None,
) -> dict[str, float]:
    """校验并归一化类型权重表 → 每个类型一个 ``(0, 1]`` 的类型因子。

    * ``weights`` 为空 → 用默认表；
    * 表里写到的类型：按**表内最大值**归一（表给的是相对优先关系）；
    * 表里没写到的类型：沿用默认表里的份额；
    * 非法（负数 / 未知类型 / 非数值 / 全零）→ 抛 ``ParamCheckError``
      并**指明字段**（错误信息里带是哪个类型的哪个键）。

    事实字段清单与 ``kb.policy.FACT_FIELDS`` 对齐（同一份事实口径）。
    """
    if not weights:
        return {
            name: value / max(DEFAULT_CLAUSE_TYPE_WEIGHTS.values())
            for name, value in DEFAULT_CLAUSE_TYPE_WEIGHTS.items()
        }

    given: dict[str, float] = {}
    for name, value in weights.items():
        field = f"clause_type_weights.{name}"
        if name not in CLAUSE_TYPES:
            raise ParamCheckError(
                f"条款类型权重表里有未知类型：{name!r}"
                f"（可用类型：{'、'.join(CLAUSE_TYPES)}）",
                field_name=field,
            )
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ParamCheckError(
                f"条款类型 {name} 的权重必须是数值：{value!r}",
                field_name=field,
            )
        if value < 0:
            raise ParamCheckError(
                f"条款类型 {name} 的权重不能为负：{value!r}",
                field_name=field,
            )
        given[name] = float(value)

    top = max(given.values())
    if top <= 0:
        raise ParamCheckError(
            "条款类型权重表不能全为 0（类型因子无从取值）",
            field_name="clause_type_weights",
        )

    default_max = max(DEFAULT_CLAUSE_TYPE_WEIGHTS.values())
    normalized: dict[str, float] = {}
    for name in CLAUSE_TYPES:
        if name in given:
            normalized[name] = given[name] / top
        else:
            normalized[name] = DEFAULT_CLAUSE_TYPE_WEIGHTS[name] / default_max
    return normalized
