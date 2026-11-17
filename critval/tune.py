"""召回参数敏感度分析 —— 网格扫描 + 可复现调参对比表。

## 这一层回答什么

「相似度阈值调到几、两路权重怎么分」是召回质量的两个杠杆。拍脑袋定出来的
参数没有说服力：**说不出「为什么是这个数」，就等于没调过**。这一层把调参变成
**可复算的表格**：

* 在同一份评测集（任务 id 29 生成，期望命中取自生成器真值）上，遍历
  「相似度阈值 × 结构化路权重」网格，每个网格点跑一遍完整召回，输出
  **三个指标**（命中率 / Top-N 命中率 / 出处覆盖率）；
* 按一条**固定且可复算**的规则挑出推荐参数，并把「为什么是它」写清楚；
* 结果落 ``reports/recall_grid.csv``（机器读）与
  ``docs/examples/recall_tuning.md``（人读、随仓库提交）。

## 扫描跑的是产品主路径，不是玩具路径

网格点用的是 ``critval.retrieve.engine.HybridRetriever`` —— 与守护进程线上
跑的是同一条主路径（两路候选 → 融合 → 综合分硬闸门 → 规则重排 → 截断），
不是另写一套「只为评测」的简化实现。候选只取一次（``gather`` / ``assemble``
拆开），换网格点时重算的只有打分部分，因此扫描可以在几十秒内跑完上百个网格点，
且结果与线上逐位一致。

## 推荐规则：先可交付，再可信，再好看

推荐规则写死在代码里（``recommend``），不靠人临时判断。**逐级判定，每一级都
是可复算的排序键**：

* **第 0 级 · 可交付性（产品底线）** —— 相似度阈值必须 ``> 0``，两路权重必须
  都 ``> 0``。理由不是"数字不好看"，是这两条配置**把机制关掉了**：

  * 阈值 = 0 → 综合分硬闸门形同虚设，召回永远不为空，「没依据就说没依据、
    转人工并留痕」这条底线（以及后面的分级降级链）永远走不到；
  * 某一路权重 = 0 → 这一路单独命中的依据综合分恒为 0（融合按"命中路的权重
    之和"归一），等于把这一路废掉 —— 值班人只说了一句自然语言时，整条路哑火。

  这两类点**不进推荐池**，但**照样出现在对比表里**，并写明排除原因 —— 指标
  好看不等于能上线。
* **第 1 级 · 可信性** —— 出处覆盖率必须 100%（无出处即不可信）。
* **第 2 级 · 效果** —— 最大化 Top-N 命中率（值班人只看得见前 N 条），
  再最大化命中率，再最大化 Top-N 全中率。
* **第 3 级 · 平手裁决** —— 闸门截断更少者胜（保留依据更多）→ 结构化路权重
  更大者胜（精确匹配优先）→ 阈值更小者胜。

同分就是同分，不靠「感觉差不多」。

## 指标会骗人，所以表里还留了噪声列

三个指标只奖励「找到」，**不惩罚「多召回」** —— 阈值压到 0 时命中率必然不差。
因此对比表额外记录**负例召回条数**（材料里根本没有该项目的查询召回了多少条 =
噪声量），并把「指标最优但不可交付」的点单独列出来：它买到了什么、代价是什么，
都写清楚，不藏在脚注里。

## 非法组合不进网格

阈值必须在 ``[0, 1]``、结构化路权重必须在 ``[0, 1]``（词法路权重恒为
``1 - 结构化路权重``）。越界即报错并指明字段 —— 网格里混进一个非法点，
推荐结论就是假的。
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from critval.evaluate import (
    EvalQuery,
    EvalSet,
    RetrievedItem,
    canonical_key,
    default_material_sources,
    evaluate,
)
from critval.index.builder import MaterialIndex, MaterialSource
from critval.retrieve.engine import CandidateBundle, HybridRetriever
from critval.retrieve.params import RetrievalParams

__all__ = [
    "DEFAULT_STRUCTURED_WEIGHTS",
    "DEFAULT_THRESHOLDS",
    "GridPoint",
    "RECOMMEND_RULE",
    "TuningRetriever",
    "build_grid",
    "recommend",
    "render_grid_csv",
    "render_tuning_markdown",
    "scan_grid",
    "write_grid_csv",
    "write_tuning_markdown",
]

#: 默认网格：相似度阈值若干档。
DEFAULT_THRESHOLDS: tuple[float, ...] = (0.0, 0.05, 0.1, 0.15, 0.2, 0.25, 0.3, 0.4, 0.5)

#: 默认网格：结构化路权重若干档（词法路权重恒为 ``1 - 本值``）。
DEFAULT_STRUCTURED_WEIGHTS: tuple[float, ...] = (
    0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0,
)

#: 默认候选池上限（两路各自进池的条数上限；与线上参数面默认值一致）。
DEFAULT_TOP_K = 1024

#: 默认材料范围（评测集覆盖三类材料：阈值表 / 制度 / 条款语料）。
DEFAULT_KB_IDS: tuple[str, ...] = ("threshold", "policy", "corpus")

#: 出处覆盖率门槛（低于此值一律不得被推荐）。
CITATION_COVERAGE_FLOOR = 1.0

#: 推荐规则的人话版（写进报告，也写进 README）。
RECOMMEND_RULE = (
    "第 0 级 可交付性：相似度阈值 > 0（硬闸门必须活着，「没依据就说没依据」"
    "才有出口）且两路权重都 > 0（权重为 0 的一路，其单独命中的依据综合分恒为 0，"
    "等于把这一路废掉）；"
    "第 1 级 可信性：出处覆盖率必须 100%（无出处即不可信）；"
    "第 2 级 效果：最大化 Top-N 命中率 → 命中率 → Top-N 全中率；"
    "第 3 级 平手裁决：闸门截断更少者胜 → 结构化路权重更大者胜 → 阈值更小者胜"
)


# ---------------------------------------------------------------------------
# 网格点
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GridPoint:
    """一个网格点的完整结果（三个指标 + 推荐资格）。

    * ``deliverable`` —— 产品可交付性：阈值 > 0 且两路权重都 > 0。不可交付的
      点**照样出现在对比表里**，只是不进推荐池。
    * ``eligible`` —— 进推荐池的资格 = 可交付 **且** 出处覆盖率 100%。
    * ``excluded_reason`` —— 为什么没进推荐池（写进表里，不藏在脚注）。
    * ``negative_retrieved`` —— 负例（材料里没有该项目的查询）召回了多少条 =
      **噪声量**。三个指标只奖励「找到」不惩罚「多召回」，这一列是唯一的
      噪声旁证。
    * ``by_kind`` —— 按查询族分的指标（哪一族拖后腿一眼可见）。
    """

    threshold: float
    structured_weight: float
    lexical_weight: float
    depth: int
    top_n: int
    hit_rate: float
    top_n_hit_rate: float
    citation_coverage: float
    citation_coverage_strict: float
    top_n_full_hit_rate: float
    hit_total: int
    top_n_hit_total: int
    expected_total: int
    cited_total: int
    citation_complete: int
    retrieved_total: int
    gated_out_total: int = 0
    negative_retrieved: int = 0
    deliverable: bool = False
    deliverable_reason: str = ""
    eligible: bool = False
    excluded_reason: str = ""
    by_kind: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)

    @property
    def label(self) -> str:
        """一句话参数标签（表格与命令行输出用）。"""
        return "阈值 {:.2f} · 结构化权重 {:.2f}".format(self.threshold, self.structured_weight)

    def to_dict(self) -> dict[str, Any]:
        return {
            "threshold": self.threshold,
            "structured_weight": self.structured_weight,
            "lexical_weight": self.lexical_weight,
            "depth": self.depth,
            "top_n": self.top_n,
            "hit_rate": round(self.hit_rate, 6),
            "top_n_hit_rate": round(self.top_n_hit_rate, 6),
            "citation_coverage": round(self.citation_coverage, 6),
            "citation_coverage_strict": round(self.citation_coverage_strict, 6),
            "top_n_full_hit_rate": round(self.top_n_full_hit_rate, 6),
            "hit_total": self.hit_total,
            "top_n_hit_total": self.top_n_hit_total,
            "expected_total": self.expected_total,
            "cited_total": self.cited_total,
            "citation_complete": self.citation_complete,
            "retrieved_total": self.retrieved_total,
            "gated_out_total": self.gated_out_total,
            "negative_retrieved": self.negative_retrieved,
            "deliverable": self.deliverable,
            "deliverable_reason": self.deliverable_reason,
            "eligible": self.eligible,
            "excluded_reason": self.excluded_reason,
            "by_kind": {kind: dict(item) for kind, item in self.by_kind.items()},
        }


def build_grid(
    thresholds: Sequence[float] = DEFAULT_THRESHOLDS,
    structured_weights: Sequence[float] = DEFAULT_STRUCTURED_WEIGHTS,
) -> list[tuple[float, float]]:
    """生成网格（阈值 × 结构化路权重），词法路权重恒为 ``1 - 结构化路权重``。

    非法取值（越界 / 非数值）即报错并指明字段 —— 网格里混进一个非法点，
    整张对比表与推荐结论就都是假的。完全重复的点只保留一个（网格点必须
    唯一，否则「扫描规模」与排序都说不清）。
    """
    clean_thresholds = _validated_values(thresholds, "similarity_threshold")
    clean_weights = _validated_values(structured_weights, "structured_weight")
    grid: list[tuple[float, float]] = []
    seen: set[tuple[float, float]] = set()
    for threshold in clean_thresholds:
        for weight in clean_weights:
            key = (round(threshold, 6), round(weight, 6))
            if key in seen:
                continue
            seen.add(key)
            grid.append(key)
    return grid


def _validated_values(values: Sequence[float], field_name: str) -> list[float]:
    """校验一维取值（数值 + 落在 ``[0, 1]``），非法即报错并指明字段。"""
    cleaned: list[float] = []
    for value in values:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{field_name} 必须是数值：{value!r}")
        number = float(value)
        if number < 0.0 or number > 1.0:
            raise ValueError(f"{field_name} 必须在 [0, 1] 区间内：{value!r}")
        if round(number, 6) not in {round(item, 6) for item in cleaned}:
            cleaned.append(number)
    if not cleaned:
        raise ValueError(f"{field_name} 不能为空")
    return cleaned


# ---------------------------------------------------------------------------
# 适配器：把产品主路径接到评测器的召回协议上
# ---------------------------------------------------------------------------


class TuningRetriever:
    """按参数面组装召回结果，**候选只取一次**（跨网格点复用）。

    评测器（任务 id 29）只认一个协议：``retrieve(query, depth) → 结果列表``。
    本类把产品主路径（``HybridRetriever``）接上去，并把结果翻译成评测器要的
    四样：**评测键 / 名次 / 分 / 出处**。评测键必须与评测集用**同一个函数**
    算（``canonical_key``），否则真值与检索侧对不上，命中率全是假的。
    """

    def __init__(
        self,
        engine: HybridRetriever,
        params: RetrievalParams,
        bundles: Optional[Mapping[str, CandidateBundle]] = None,
    ) -> None:
        self.engine = engine
        self.params = params.check()
        self.bundles = dict(bundles or {})
        self.documents = engine.index.documents_by_id()
        #: 本次评测里被硬闸门截掉的候选总数（保留依据多少的旁证）。
        self.gated_out_total = 0

    def prepare(self, eval_set: EvalSet) -> None:
        """预取候选（每个查询取一次，网格点之间复用）。"""
        for query in eval_set.queries:
            if query.query_id not in self.bundles:
                self.bundles[query.query_id] = self.engine.gather(
                    query.text, query.filters, top_k=self.params.top_k
                )

    def retrieve(self, query: EvalQuery, depth: int) -> list[RetrievedItem]:
        """跑一次召回并翻译成评测器要的形状。"""
        bundle = self.bundles.get(query.query_id)
        if bundle is None:
            bundle = self.engine.gather(query.text, query.filters, top_k=self.params.top_k)
            self.bundles[query.query_id] = bundle
        result = self.engine.assemble(bundle, self.params)
        self.gated_out_total += result.gated_out
        items: list[RetrievedItem] = []
        for hit in result.hits:
            document = self.documents.get(hit.doc_id)
            items.append(
                RetrievedItem(
                    key=canonical_key(document) if document is not None else hit.doc_id,
                    rank=hit.rank,
                    score=hit.score,
                    citation=hit.citation,
                    stable_key=hit.stable_key,
                    doc_id=hit.doc_id,
                    paths=hit.paths,
                    item_code=document.field("item_code") if document is not None else "",
                )
            )
        return items


# ---------------------------------------------------------------------------
# 扫描
# ---------------------------------------------------------------------------


def scan_grid(
    eval_set: EvalSet,
    index: MaterialIndex,
    thresholds: Sequence[float] = DEFAULT_THRESHOLDS,
    structured_weights: Sequence[float] = DEFAULT_STRUCTURED_WEIGHTS,
    depth: int = 10,
    top_n: int = 5,
    at: Optional[date] = None,
    top_k: int = DEFAULT_TOP_K,
    kb_ids: Sequence[str] = DEFAULT_KB_IDS,
    rerank_id: str = "",
    citation_mode: str = "kind_aware",
) -> list[GridPoint]:
    """在评测集上跑网格扫描，返回每个网格点的三个指标。

    * ``depth`` —— 检索深度（参与命中率判定）；
    * ``top_n`` —— 前 N 名（参与 Top-N 命中率判定），必须 ``≤ depth``。
    """
    if depth <= 0:
        raise ValueError(f"检索深度必须为正：{depth}")
    if top_n <= 0:
        raise ValueError(f"Top-N 必须为正：{top_n}")
    if top_n > depth:
        raise ValueError(f"Top-N（{top_n}）不能大于检索深度（{depth}）")

    grid = build_grid(thresholds, structured_weights)
    engine = HybridRetriever(
        index=index,
        params=RetrievalParams(
            similarity_threshold=0.0,
            keywords_similarity_weight=0.5,
            top_n=depth,
            top_k=top_k,
            kb_ids=tuple(kb_ids),
            rerank_id=rerank_id,
        ),
        at=at,
    )

    # 候选只取一次：阈值与权重改不动候选。
    base = TuningRetriever(engine, engine.params)
    base.prepare(eval_set)

    points: list[GridPoint] = []
    for threshold, structured_weight in grid:
        lexical_weight = 1.0 - structured_weight
        params = RetrievalParams(
            similarity_threshold=threshold,
            keywords_similarity_weight=lexical_weight,
            top_n=depth,
            top_k=top_k,
            kb_ids=tuple(kb_ids),
            rerank_id=rerank_id,
        ).check()
        retriever = TuningRetriever(engine, params, base.bundles)
        report = evaluate(
            eval_set,
            retriever,
            depth=depth,
            top_n=top_n,
            citation_mode=citation_mode,
        )
        eligible, reason = _eligibility(
            report.citation_coverage, threshold, structured_weight, lexical_weight
        )
        deliverable, deliverable_reason = _deliverability(threshold, structured_weight)
        points.append(
            GridPoint(
                threshold=threshold,
                structured_weight=structured_weight,
                lexical_weight=lexical_weight,
                depth=depth,
                top_n=top_n,
                hit_rate=report.hit_rate,
                top_n_hit_rate=report.top_n_hit_rate,
                citation_coverage=report.citation_coverage,
                citation_coverage_strict=report.citation_coverage_strict,
                top_n_full_hit_rate=report.top_n_full_hit_rate,
                hit_total=report.hit_total,
                top_n_hit_total=report.top_n_hit_total,
                expected_total=report.expected_total,
                cited_total=report.cited_total,
                citation_complete=report.citation_complete,
                retrieved_total=report.retrieved_total,
                gated_out_total=retriever.gated_out_total,
                negative_retrieved=report.negative_retrieved_total,
                deliverable=deliverable,
                deliverable_reason=deliverable_reason,
                eligible=eligible,
                excluded_reason=reason,
                by_kind=report.by_kind(),
            )
        )
    return points


def _deliverability(
    threshold: float,
    structured_weight: float,
) -> tuple[bool, str]:
    """产品可交付性：阈值必须 > 0，两路权重必须都 > 0。

    这不是"数字好不好看"，是两条机制会不会被关掉：

    * 阈值 = 0 → 综合分硬闸门形同虚设，召回永远不为空，「没依据就说没依据、
      转人工并留痕」这条底线走不到；
    * 某一路权重 = 0 → 这一路单独命中的依据综合分恒为 0（融合按"命中路的权重
      之和"归一），等于把这一路废掉。
    """
    reasons: list[str] = []
    if threshold <= 0.0:
        reasons.append("阈值 = 0 等于关掉综合分硬闸门")
    if structured_weight <= 0.0:
        reasons.append("结构化路权重 = 0 等于废掉精确匹配路")
    if structured_weight >= 1.0:
        reasons.append("词法路权重 = 0 等于废掉 BM25 词法路")
    if reasons:
        return False, "；".join(reasons) + "（不可交付）"
    return True, ""


def _eligibility(
    citation_coverage: float,
    threshold: float,
    structured_weight: float,
    lexical_weight: float,
) -> tuple[bool, str]:
    """推荐资格 = 可交付 **且** 出处覆盖率 100%。"""
    reasons: list[str] = []
    deliverable, deliverable_reason = _deliverability(threshold, structured_weight)
    if not deliverable:
        reasons.append(deliverable_reason)
    if citation_coverage < CITATION_COVERAGE_FLOOR - 1e-9:
        reasons.append(
            f"出处覆盖率 {citation_coverage:.1%} < 100%（无出处即不可信，不得推荐）"
        )
    if reasons:
        return False, "；".join(reasons)
    return True, ""


# ---------------------------------------------------------------------------
# 推荐
# ---------------------------------------------------------------------------


def _rank_key(point: GridPoint) -> tuple:
    """推荐规则的排序键（第 2 + 第 3 级，逐级比较）。

    1. Top-N 命中率最大；
    2. 命中率最大；
    3. Top-N 全中率最大；
    4. 闸门截断更少者胜（保留依据更多）；
    5. 结构化路权重更大者胜（精确匹配优先）；
    6. 阈值更小者胜。

    全部取四位小数对齐后再比 —— 免得浮点尾数把"其实并列"判成"差一点"。
    """
    return (
        round(point.top_n_hit_rate, 4),
        round(point.hit_rate, 4),
        round(point.top_n_full_hit_rate, 4),
        -point.gated_out_total,
        point.structured_weight,
        -point.threshold,
    )


def recommend(
    points: Sequence[GridPoint],
) -> tuple[Optional[GridPoint], list[GridPoint], Optional[GridPoint]]:
    """按固定规则挑推荐参数。

    返回 ``(推荐点, 合格点列表, 指标最优点)``：

    * **推荐点** —— 在合格点（可交付 + 出处覆盖率 100%）里按 ``_rank_key`` 挑
      出的那一个；一个合格点都没有时返回 ``None`` —— **不硬凑一个推荐**。
    * **合格点列表** —— 全部合格点（用于核对推荐是否真的按规则挑的）。
    * **指标最优点** —— 不看可交付性、按同一排序键在全网格里挑出的最高分点。
      它常常是「把一路权重推到 0 / 把阈值压到 0」的边界点，指标好看但机制被
      关掉了 —— 单独列出来写明原因，不藏。
    """
    eligible = [point for point in points if point.eligible]
    recommended = max(eligible, key=_rank_key) if eligible else None
    best_overall = max(points, key=_rank_key) if points else None
    return recommended, eligible, best_overall


# ---------------------------------------------------------------------------
# 落盘：CSV（机器读）+ Markdown（人读、随仓库提交）
# ---------------------------------------------------------------------------

CSV_COLUMNS = (
    "threshold",
    "structured_weight",
    "lexical_weight",
    "depth",
    "top_n",
    "hit_rate",
    "top_n_hit_rate",
    "citation_coverage",
    "citation_coverage_strict",
    "top_n_full_hit_rate",
    "hit_total",
    "top_n_hit_total",
    "expected_total",
    "cited_total",
    "citation_complete",
    "retrieved_total",
    "gated_out_total",
    "negative_retrieved",
    "deliverable",
    "deliverable_reason",
    "eligible",
    "excluded_reason",
)


def render_grid_csv(points: Sequence[GridPoint]) -> str:
    """渲染网格对比表（CSV 文本）。

    比率列写成**百分比数值**（``89.0`` 表示 89%），排序键与正文里看到的一致，
    便于直接在表格软件里排序比较。
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(CSV_COLUMNS)
    for point in points:
        writer.writerow(
            [
                "{:.2f}".format(point.threshold),
                "{:.2f}".format(point.structured_weight),
                "{:.2f}".format(point.lexical_weight),
                point.depth,
                point.top_n,
                "{:.4f}".format(point.hit_rate * 100),
                "{:.4f}".format(point.top_n_hit_rate * 100),
                "{:.4f}".format(point.citation_coverage * 100),
                "{:.4f}".format(point.citation_coverage_strict * 100),
                "{:.4f}".format(point.top_n_full_hit_rate * 100),
                point.hit_total,
                point.top_n_hit_total,
                point.expected_total,
                point.cited_total,
                point.citation_complete,
                point.retrieved_total,
                point.gated_out_total,
                point.negative_retrieved,
                "true" if point.deliverable else "false",
                point.deliverable_reason,
                "true" if point.eligible else "false",
                point.excluded_reason,
            ]
        )
    return buffer.getvalue()


def write_grid_csv(points: Sequence[GridPoint], path: str | Path) -> Path:
    """写 ``reports/recall_grid.csv``（UTF-8 BOM，Excel 直接打开不乱码）。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_grid_csv(points), encoding="utf-8-sig")
    return target


def render_tuning_markdown(
    points: Sequence[GridPoint],
    recommendation: Optional[GridPoint],
    meta: Optional[Mapping[str, Any]] = None,
    title: str = "召回参数敏感度对比表",
    best_overall: Optional[GridPoint] = None,
    eligible: Optional[Sequence[GridPoint]] = None,
) -> str:
    """渲染人读的调参对比表（落 ``docs/examples/recall_tuning.md``）。"""
    info = dict(meta or {})
    eligible_points = list(eligible) if eligible is not None else [p for p in points if p.eligible]
    eligible_count = len(eligible_points)
    lines: list[str] = []
    lines.append(f"# {title}")
    lines.append("")
    lines.append(
        "> 本表是**可复算**的：在同一份评测集上遍历「相似度阈值 × 结构化路权重」"
        "网格，每个网格点跑一遍完整召回主路径，逐点记录三个指标；"
        "推荐参数由固定规则挑出（见文末），不靠临时判断。"
    )
    lines.append("")

    lines.append("## 扫描规模与口径")
    lines.append("")
    lines.append("| 项 | 取值 |")
    lines.append("|------|------|")
    lines.append("| 网格点总数 | {} |".format(len(points)))
    lines.append("| 合格点（可交付 + 出处覆盖率 100%） | {} |".format(eligible_count))
    lines.append("| 被排除点 | {} |".format(len(points) - eligible_count))
    lines.append("| 评测查询数 | {} |".format(info.get("query_count", "—")))
    lines.append("| 检索深度 / Top-N | {} / {} |".format(
        points[0].depth if points else "—",
        points[0].top_n if points else "—",
    ))
    lines.append("| 期望条目总数 | {} |".format(
        points[0].expected_total if points else "—",
    ))
    if info.get("materials"):
        lines.append("| 评测材料 | {} |".format(
            "、".join(str(item.get("name", "")) for item in info["materials"])
        ))
    if info.get("eval_set_sha256"):
        lines.append("| 评测集指纹 | `{}` |".format(str(info["eval_set_sha256"])[:16]))
    lines.append("")

    lines.append("## 推荐参数")
    lines.append("")
    if recommendation is None:
        lines.append("**没有合格点** —— 没有同时满足「可交付」与「出处覆盖率 100%」的网格点，按规则不得推荐。")
        lines.append("")
    else:
        lines.append("| 项 | 取值 |")
        lines.append("|------|------|")
        lines.append("| 相似度阈值（综合分硬闸门） | **{:.2f}** |".format(recommendation.threshold))
        lines.append("| 结构化精确路权重 | **{:.2f}** |".format(recommendation.structured_weight))
        lines.append("| 词法 BM25 路权重 | **{:.2f}** |".format(recommendation.lexical_weight))
        lines.append("| 命中率 | **{:.1%}**（命中 {} / 期望 {}） |".format(
            recommendation.hit_rate, recommendation.hit_total, recommendation.expected_total))
        lines.append("| Top-{n} 命中率 | **{:.1%}**（前 {n} 名命中 {hit} / 期望 {exp}） |".format(
            recommendation.top_n_hit_rate, n=recommendation.top_n,
            hit=recommendation.top_n_hit_total, exp=recommendation.expected_total))
        lines.append("| 出处覆盖率 | **{:.1%}**（出处齐备 {} / 带出处引用 {}） |".format(
            recommendation.citation_coverage,
            recommendation.citation_complete,
            recommendation.cited_total))
        lines.append("| 闸门截断候选数 | {} |".format(recommendation.gated_out_total))
        lines.append("| 负例召回条数（噪声量） | {} |".format(recommendation.negative_retrieved))
        lines.append("")
        lines.append("**推荐规则**：{}。".format(RECOMMEND_RULE))
        lines.append("")
        if best_overall is not None and best_overall is not recommendation:
            lines.append(
                "> **指标最优的点不是这一个** —— 全网格里指标最高的是「{label}」"
                "（Top-{n} 命中率 {rate:.1%}），但它被排除了：{reason}。"
                "指标好看不等于能上线，边界配置写进参数面只会让「没依据就说没依据」"
                "这条底线失效。".format(
                    label=best_overall.label,
                    n=best_overall.top_n,
                    rate=best_overall.top_n_hit_rate,
                    reason=best_overall.excluded_reason or "未通过推荐规则",
                )
            )
            lines.append("")

    lines.append("## 推荐参数下的分类指标")
    lines.append("")
    if recommendation is not None and recommendation.by_kind:
        lines.append(
            "| 查询族 | 条数 | 期望条目 | 命中率 | Top-{n} 命中率 |".format(n=recommendation.top_n)
        )
        lines.append("|--------|------|----------|--------|--------------|")
        for kind, item in recommendation.by_kind.items():
            lines.append(
                "| {} | {} | {} | {:.1%} | {:.1%} |".format(
                    kind,
                    item.get("queries", 0),
                    item.get("expected", 0),
                    float(item.get("hit_rate", 0.0)),
                    float(item.get("top_n_hit_rate", 0.0)),
                )
            )
        lines.append("")
    else:
        lines.append("（没有推荐参数，无分类指标）")
        lines.append("")

    lines.append("## 每个网格点的三个指标")
    lines.append("")
    lines.append(
        "| 阈值 | 结构化权重 | 词法权重 | 命中率 | Top-{n} 命中率 | 出处覆盖率 | 闸门截断 | 负例噪声 | 合格 |".format(
            n=points[0].top_n if points else 5
        )
    )
    lines.append("|------|------------|----------|--------|--------------|------------|----------|----------|------|")
    for point in points:
        lines.append(
            "| {:.2f} | {:.2f} | {:.2f} | {:.1%} | {:.1%} | {:.1%} | {} | {} | {} |".format(
                point.threshold,
                point.structured_weight,
                point.lexical_weight,
                point.hit_rate,
                point.top_n_hit_rate,
                point.citation_coverage,
                point.gated_out_total,
                point.negative_retrieved,
                "✅" if point.eligible else "❌",
            )
        )
    lines.append("")

    excluded = [point for point in points if not point.eligible]
    if excluded:
        lines.append("### 被排除的网格点")
        lines.append("")
        lines.append("| 阈值 | 结构化权重 | Top-{n} 命中率 | 出处覆盖率 | 排除原因 |".format(
            n=points[0].top_n if points else 5
        ))
        lines.append("|------|------------|--------------|------------|----------|")
        for point in excluded:
            lines.append(
                "| {:.2f} | {:.2f} | {:.1%} | {:.1%} | {} |".format(
                    point.threshold,
                    point.structured_weight,
                    point.top_n_hit_rate,
                    point.citation_coverage,
                    point.excluded_reason,
                )
            )
        lines.append("")

    if info.get("notes"):
        lines.append("## 已知短板与后续")
        lines.append("")
        for note in info["notes"]:
            lines.append("- {}".format(note))
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append(
        "口径说明：命中率 = 命中条目数 / 期望条目数（负例不进分母）；"
        "Top-N 命中率只数落在前 N 名的期望条目；出处覆盖率只对**带出处的引用**计分；"
        "负例噪声 = 材料里根本没有该项目的查询召回了多少条（三个指标只奖励「找到」，"
        "不惩罚「多召回」，这一列是唯一的噪声旁证）。"
        "扫描跑的是线上同一条召回主路径（两路候选 → 融合 → 综合分硬闸门 → 规则重排 → 截断），"
        "候选只取一次、逐网格点重算打分，因此结果与线上逐位一致、可复算。"
    )
    return "\n".join(lines) + "\n"


def write_tuning_markdown(
    points: Sequence[GridPoint],
    recommendation: Optional[GridPoint],
    path: str | Path,
    meta: Optional[Mapping[str, Any]] = None,
    title: str = "召回参数敏感度对比表",
    best_overall: Optional[GridPoint] = None,
    eligible: Optional[Sequence[GridPoint]] = None,
) -> Path:
    """写 ``docs/examples/recall_tuning.md``。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        render_tuning_markdown(
            points,
            recommendation,
            meta=meta,
            title=title,
            best_overall=best_overall,
            eligible=eligible,
        ),
        encoding="utf-8",
    )
    return target


def build_index(
    materials: Optional[Sequence[MaterialSource]] = None,
    root: Optional[Path] = None,
) -> MaterialIndex:
    """按材料建索引（扫描前调用一次；网格点之间复用同一份索引）。"""
    sources = list(materials) if materials is not None else default_material_sources(root)
    index = MaterialIndex()
    index.rebuild(sources)
    return index
