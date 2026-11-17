"""规则重排 —— 在融合分之上按三条可解释的因子给依据重新定序。

## 为什么融合分不够用

两路融合分解决的是"**这条依据有多相关**"，但值班人问的是另一个问题：
"**这几条里哪条才是我该照着做的**"。按项目码精确查表时，阈值行是结构化路的
满分命中，可条款语料里成百上千条近似条款的 BM25 分更高 —— 纯按融合分排，
真正的阈值依据会被一堆近似条款压到后面（实测 ``threshold_lookup`` 族命中率
只有 59.3%，而结构化路本身是满分命中）。

所以融合分之后还要有一层**规则重排**，把三个可解释的因子算成加分：

1. **字段命中权重**（field hit）—— 命中了哪些结构化键。精确命中项目码
   （``item_code``）比命中单位（``unit``）值钱得多；命中键的权重相加后归一到
   ``[0, 1]``。
2. **章节位置权重**（section position）—— 同一部材料里，章节越靠后越接近
   值班人照着做的操作细则（总则在前、报告流程与记录去向在后）。按"该章节序号
   在材料全部可见章节中的相对位置"归一。
3. **词项覆盖率**（term coverage）—— 查询词有多少被这条依据覆盖了。

三个因子各自有**上限**（``field_gain`` / ``section_gain`` / ``coverage_gain``），
不会把融合分的判断整个掀翻 —— 重排是"加分"，不是"另起一套排序"。

## 第四个因子：条款类型加权（任务 id 30）

前三个因子回答的都是"**这条依据有多相关 / 在哪 / 覆盖没覆盖我的词**"，没有一个
回答"**这条依据是干什么用的**"。同一本制度里：

* 「上报时限：10 分钟内」—— 直接回答"多久要报"；
* 「本制度自发布之日起施行」—— 总则，哪一问都不直接回答。

所以重排再加一个因子：**条款类型权重**（``clause_type``）。类型由**制度条款
解析字段**推导（不是关键词猜测），权重表随重排档位走，非法即报错并指明字段。

类型因子**乘在**「字段命中加成 + 章节位置加成」之上：

    最终分 = 融合分
            + 类型因子 × (字段加成 + 章节加成)
            + 覆盖率加成

读法：**总则条款即使精确命中了项目码、又在靠后章节，它拿到的也只是六成
"依据分量"** —— 因为它没有操作细则。词项覆盖率**不打折**（问的词被覆盖了
就是覆盖了，与条款类型无关）。类型因子的强度由 ``clause_type_gain`` 控制
（``0`` = 关闭类型加权，``1`` = 全额生效）。

## 稳定序

重排后按 ``(-最终分, 稳定主键)`` 排序，稳定主键 = **材料指纹 → 位置 → 条目键**
（制度条款是 ``source_sha256`` + 段落序号，阈值表行是 ``source_sha256`` + 行号）。
**同分保持稳定序**：同样参数两次运行，排序逐位一致 —— 值班人复核时"上次排第一
的条款这次排第三"必须是可解释的，而不能是集合遍历顺序变了。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Optional, Sequence

from critval.index.structured import IndexDocument
from critval.retrieve.clause_type import (
    CLAUSE_TYPE_OTHER,
    DEFAULT_CLAUSE_TYPE_WEIGHTS,
    clause_type_label,
    derive_clause_type,
    normalize_clause_type_weights,
)
from critval.retrieve.errors import ParamCheckError
from critval.retrieve.merge import MergedCandidate

__all__ = [
    "DEFAULT_CLAUSE_TYPE_WEIGHTS",
    "DEFAULT_FIELD_WEIGHTS",
    "RERANK_PROFILES",
    "RerankFactors",
    "RerankProfile",
    "RerankedCandidate",
    "chapter_positions",
    "profile_for_id",
    "rerank",
    "stable_sort_key",
]

#: 结构化键的命中权重（键 → 权重）。
#:
#: 口径：**命中越"硬"的键越值钱** —— 项目码是院内约定的唯一标识，单位 / 危急
#: 方向只是这次结果的自述，章节标题与条款号是"能被指认的条款"的标志。
DEFAULT_FIELD_WEIGHTS: Mapping[str, float] = MappingProxyType(
    {
        "item_code": 1.0,      # 阈值表行按项目码精确命中
        "item_codes": 0.9,     # 制度条款点名了项目码
        "clause_no": 0.5,      # 可编号引用的成条款
        "section_title": 0.4,  # 章节标题
        "notify_target": 0.5,  # 条款写明了通知对象
        "deadline": 0.5,       # 条款写明了上报时限
        "record_destination": 0.5,  # 条款写明了记录去向
        "item_name": 0.3,
        "direction": 0.3,
        "unit": 0.3,
        "version": 0.1,
        "source_name": 0.05,
        "kind": 0.0,
    }
)


@dataclass(frozen=True)
class RerankProfile:
    """一档规则重排的档位。

    ``field_gain`` / ``section_gain`` / ``coverage_gain`` 是三个因子对最终分的
    **加成上限**（不是乘数）—— 因子值本身归一到 ``[0, 1]``。

    ``clause_type_weights`` 是**条款类型权重表**（相对优先关系，归一后落在
    ``(0, 1]``）；``clause_type_gain`` 是类型加权的强度（``0`` = 关闭，
    ``1`` = 全额生效）。类型表非法（负数 / 未知类型 / 全零）→ 报错并指明字段。
    """

    name: str = "default"
    field_weights: Mapping[str, float] = DEFAULT_FIELD_WEIGHTS
    unknown_field_weight: float = 0.2
    field_gain: float = 0.30
    section_gain: float = 0.10
    coverage_gain: float = 0.20
    clause_type_weights: Mapping[str, float] = DEFAULT_CLAUSE_TYPE_WEIGHTS
    clause_type_gain: float = 1.0

    def weight_of(self, key: str) -> float:
        """一个命中键的权重（表里没有的键走 ``unknown_field_weight``）。"""
        return float(self.field_weights.get(key, self.unknown_field_weight))

    def type_weights(self) -> dict[str, float]:
        """归一化后的类型权重表（``(0, 1]``）。"""
        return normalize_clause_type_weights(self.clause_type_weights)

    def validate(self) -> "RerankProfile":
        for name in ("field_gain", "section_gain", "coverage_gain"):
            value = getattr(self, name)
            if value < 0:
                raise ValueError(f"{name} 不能为负：{value}")
        gain = self.clause_type_gain
        if isinstance(gain, bool) or not isinstance(gain, (int, float)):
            raise ParamCheckError(
                f"clause_type_gain 必须是数值：{gain!r}",
                field_name="clause_type_gain",
            )
        if gain < 0 or gain > 1:
            raise ParamCheckError(
                f"clause_type_gain 必须在 [0, 1] 区间内：{gain!r}",
                field_name="clause_type_gain",
            )
        # 类型权重表非法（负数 / 未知类型 / 全零）→ 报错并指明字段。
        self.type_weights()
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "field_gain": self.field_gain,
            "section_gain": self.section_gain,
            "coverage_gain": self.coverage_gain,
            "clause_type_gain": self.clause_type_gain,
            "field_weights": dict(self.field_weights),
            "clause_type_weights": dict(self.clause_type_weights),
        }


#: 预置档位（``rerank_id`` 选档）。
RERANK_PROFILES: Mapping[str, RerankProfile] = MappingProxyType(
    {
        "default": RerankProfile(name="default"),
        # 偏结构化精确：现场查阈值依据时用，把"精确命中"压得更实。
        "field_first": RerankProfile(
            name="field_first",
            field_gain=0.45,
            section_gain=0.05,
            coverage_gain=0.10,
        ),
        # 偏自然语言：值班人拿一句话问制度时用，把词项覆盖率抬高。
        "coverage_first": RerankProfile(
            name="coverage_first",
            field_gain=0.15,
            section_gain=0.05,
            coverage_gain=0.40,
        ),
        # 偏操作细则：只认"能照着做"的条款 —— 总则条款压得更狠，
        # 时限 / 去向条款优先（值班人问的是"多久报、报给谁、记哪里"）。
        "action_first": RerankProfile(
            name="action_first",
            field_gain=0.30,
            section_gain=0.10,
            coverage_gain=0.15,
            clause_type_weights={
                "deadline": 1.0,
                "destination": 0.95,
                "target": 0.90,
                "other": 1.0,
                "general": 0.40,
            },
        ),
    }
)


def profile_for_id(rerank_id: str) -> RerankProfile:
    """按 ``rerank_id`` 取重排档位；未知档位报错并指明字段。"""
    key = (rerank_id or "").strip()
    if not key:
        key = "default"
    profile = RERANK_PROFILES.get(key)
    if profile is None:
        raise ParamCheckError(
            f"[召回] 重排档位不存在：{key!r}（可用：{'、'.join(sorted(RERANK_PROFILES))}）",
            field_name="rerank_id",
        )
    return profile.validate()


@dataclass(frozen=True)
class RerankFactors:
    """四个重排因子的取值（各自在 ``[0, 1]``）。

    ``clause_type_factor`` 是**类型因子**：条款类型权重经 ``clause_type_gain``
    折算后的实际乘子（``1`` = 不打折，``0.6`` = 总则条款只拿六成分量）。
    """

    field_hit: float = 0.0
    section_position: float = 0.0
    term_coverage: float = 0.0
    clause_type_factor: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "field_hit": round(self.field_hit, 6),
            "section_position": round(self.section_position, 6),
            "term_coverage": round(self.term_coverage, 6),
            "clause_type_factor": round(self.clause_type_factor, 6),
        }


@dataclass(frozen=True)
class RerankedCandidate:
    """重排后的一条依据。"""

    candidate: MergedCandidate
    base_score: float
    final_score: float
    factors: RerankFactors = field(default_factory=RerankFactors)
    #: 条款类型（``deadline`` / ``destination`` / ``target`` / ``general`` /
    #: ``other``）—— 由制度条款解析字段推导，供"为什么这条排第一"复算。
    clause_type: str = CLAUSE_TYPE_OTHER

    @property
    def stable_key(self) -> str:
        return self.candidate.stable_key

    @property
    def doc_id(self) -> str:
        return self.candidate.doc_id

    @property
    def clause_type_label(self) -> str:
        """条款类型的人读标签。"""
        return clause_type_label(self.clause_type)

    @property
    def document(self) -> Optional[IndexDocument]:
        return self.candidate.document

    def to_dict(self) -> dict[str, Any]:
        return {
            "stable_key": self.stable_key,
            "doc_id": self.candidate.doc_id,
            "base_score": round(self.base_score, 6),
            "final_score": round(self.final_score, 6),
            "factors": self.factors.to_dict(),
            "clause_type": self.clause_type,
            "clause_type_label": self.clause_type_label,
            "paths": list(self.candidate.paths),
            "locator": self.candidate.locator,
        }


def chapter_positions(
    documents: Iterable[IndexDocument],
) -> dict[tuple[str, str], float]:
    """每份材料里各章节的相对位置（``0`` = 第一章，``1`` = 最后一章）。

    * 章节顺序按**该章节最早出现的位置**（段落序号）定，不按字符串排序 ——
      "第三章" 排在 "第十章" 后面这种事，靠字典序会排错。
    * 只有一章时该章记 ``0.5``（不奖不罚）。
    * 没有章节概念的条目（阈值表行、语料行）不参与，位置权重记 ``0``。
    * 键是 ``(材料 sha256, 章节标题)`` —— 不同材料的同名章节互不干扰。
    """
    by_source: dict[str, dict[str, int]] = {}
    for document in documents:
        citation = document.citation
        if citation is None or not citation.section_path:
            continue
        heading = citation.section_path[0]
        position = (
            citation.paragraph_no
            if citation.paragraph_no is not None
            else (citation.line_no if citation.line_no is not None else 0)
        )
        bucket = by_source.setdefault(citation.source_sha256, {})
        if heading not in bucket or position < bucket[heading]:
            bucket[heading] = position

    positions: dict[tuple[str, str], float] = {}
    for source_sha, chapters in by_source.items():
        ordered = sorted(chapters.items(), key=lambda item: (item[1], item[0]))
        total = len(ordered)
        if total == 1:
            positions[(source_sha, ordered[0][0])] = 0.5
            continue
        for index, (heading, _) in enumerate(ordered):
            positions[(source_sha, heading)] = index / (total - 1)
    return positions


def _field_hit_factor(
    candidate: MergedCandidate,
    profile: RerankProfile,
) -> float:
    """字段命中权重（命中键权重之和，归一到 ``[0, 1]``）。"""
    if not candidate.matched_keys:
        return 0.0
    total = sum(profile.weight_of(key) for key in candidate.matched_keys)
    return min(1.0, total)


def _section_position_factor(
    candidate: MergedCandidate,
    positions: Mapping[tuple[str, str], float],
) -> float:
    """章节位置权重（该条款所在章节在材料中的相对位置）。"""
    document = candidate.document
    if document is None or document.citation is None:
        return 0.0
    section_path = document.citation.section_path
    if not section_path:
        return 0.0
    key = (document.citation.source_sha256, section_path[0])
    return float(positions.get(key, 0.0))


def _term_coverage_factor(
    candidate: MergedCandidate,
    query_terms: Sequence[str],
) -> float:
    """词项覆盖率（查询词被这条依据覆盖的比例）。"""
    if not query_terms:
        return 0.0
    matched = set(candidate.matched_terms)
    hit = sum(1 for term in set(query_terms) if term in matched)
    return min(1.0, hit / len(set(query_terms)))


def stable_sort_key(candidate: MergedCandidate) -> tuple[str, int, str]:
    """同分并列时的**稳定主键**（材料指纹 → 位置 → 条目键）。

    * 制度条款 → ``source_sha256`` + 段落序号（位置取段落号，无段落号取行号）；
    * 阈值表行 → ``source_sha256`` + 行号；
    * 没有出处的条目 → 空指纹 + 0 位置 + 条目键。

    排序按它升序，**同样输入两次运行逐位一致** —— 入参顺序变了、集合遍历顺序
    变了，排序都不变。
    """
    document = candidate.document
    citation = document.citation if document is not None else None
    if citation is not None:
        position = citation.paragraph_no
        if position is None:
            position = citation.line_no
        return (citation.source_sha256 or "", int(position or 0), candidate.stable_key)
    source = document.source_sha256 if document is not None else ""
    return (source or "", 0, candidate.stable_key)


def rerank(
    candidates: Sequence[MergedCandidate],
    query_terms: Sequence[str] = (),
    profile: Optional[RerankProfile] = None,
    positions: Optional[Mapping[tuple[str, str], float]] = None,
) -> list[RerankedCandidate]:
    """对融合后的候选做规则重排。

    * 最终分 = ``融合分 + 类型因子 × (字段加成 + 章节加成) + 覆盖率加成``；
    * 类型因子由**条款类型权重表**折算（总则条款打折，操作细则不打折）；
    * 排序按 ``(-最终分, 稳定主键)`` —— **同分保持稳定序**，同样输入两次运行
      逐位一致。
    """
    effective = (profile or RerankProfile()).validate()
    chapter_map = positions or {}
    type_table = effective.type_weights()
    gain = float(effective.clause_type_gain)

    reranked: list[RerankedCandidate] = []
    for candidate in candidates:
        field_hit = _field_hit_factor(candidate, effective)
        section_position = _section_position_factor(candidate, chapter_map)
        coverage = _term_coverage_factor(candidate, query_terms)
        clause_type = derive_clause_type(candidate.document)
        type_weight = float(type_table.get(clause_type, 1.0))
        # 类型因子：权重 → 乘子（gain=0 时完全不打折，gain=1 时全额生效）。
        type_factor = 1.0 - gain * (1.0 - type_weight)
        final_score = (
            candidate.fused_score
            + type_factor
            * (effective.field_gain * field_hit + effective.section_gain * section_position)
            + effective.coverage_gain * coverage
        )
        reranked.append(
            RerankedCandidate(
                candidate=candidate,
                base_score=candidate.fused_score,
                final_score=final_score,
                factors=RerankFactors(
                    field_hit=field_hit,
                    section_position=section_position,
                    term_coverage=coverage,
                    clause_type_factor=type_factor,
                ),
                clause_type=clause_type,
            )
        )

    reranked.sort(key=lambda item: (-item.final_score, stable_sort_key(item.candidate)))
    return reranked
