"""召回基线对比 —— 纯 BM25 单路 vs 融合引擎（任务 id 8 · 深度项 b）。

## 这层回答什么

「把结构化精确路与规则重排拿掉，只剩经典单路词法检索，**到底丢了多少**？」

丢的东西往往不在"能不能搜到"，而在"**能不能排在前面**"—— 单路 BM25 在
上千条近似条款里，把真正该用的那一条（阈值依据行、写着时限的操作细则）
挤到了后面去；值班人拿到卡片时先看到的是七条相似但不适用的总则。

## 两侧必须同口径，否则数字没意义

对比表里的两行跑的是**同一份材料、同一份评测集、同样的检索深度与 Top-N**，
只差召回做法：

* **可见范围一致** —— 同一材料范围键、同一生效期口径；
* **深度一致** —— 两侧都返回前 ``depth`` 条；
* **指标一致** —— 两侧都用 ``critval.evaluate`` 的同一个评测器算
  命中率 / Top-N 命中率 / 出处覆盖率。

差在哪儿只有三处，而且都是有意差出来的：

======================  ==========================  ==========================
                        基线（纯 BM25）              融合引擎（产品主路径）
======================  ==========================  ==========================
结构化精确路            无                          有（项目码 / 方向 / 单位）
综合分硬闸门            无                          有（低于阈值即截断）
规则重排                无                          有（条款类型 / 字段 / 章节）
======================  ==========================  ==========================

## 这个模块住在哪、为什么

编排（建索引 → 跑两侧 → 收数 → 渲染 → 落盘）放在**评测层**（``critval.evaluate``
的邻居），不放在召回层：召回层不能 import 评测层（评测层已经 import 召回层，
反向依赖会绕成环）。脚本 ``scripts/compare_baseline.py`` 与运维 CLI
``recall-baseline`` 都调这里 —— **换的是入口，不是口径**。
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from critval.evaluate import EvalReport, EvalSet, evaluate
from critval.index.builder import MaterialIndex, MaterialSource
from critval.retrieve.baseline import BM25BaselineRetriever, HybridEvalRetriever
from critval.retrieve.params import RetrievalParams

__all__ = [
    "BASELINE_NAME",
    "CSV_COLUMNS",
    "FUSED_NAME",
    "BaselineComparison",
    "compare_baselines",
    "render_comparison_markdown",
    "write_comparison_csv",
]

#: 两个参赛者的名字（对比表两行）。
BASELINE_NAME = "基线（纯 BM25 单路）"
FUSED_NAME = "融合引擎（结构化 + 词法 + 重排）"

#: 对比表 CSV 的列。
CSV_COLUMNS = (
    "参赛者",
    "命中项数",
    "期望条目数",
    "命中率",
    "Top-N命中率",
    "出处覆盖率",
    "出处齐备数",
    "带出处引用数",
    "检索返回条目数",
    "负例召回条目数",
)


@dataclass(frozen=True)
class BaselineComparison:
    """一次对比的完整结果（两行数据 + 两份报告 + 参数面）。"""

    rows: tuple[Mapping[str, Any], ...]
    reports: Mapping[str, EvalReport]
    params: RetrievalParams
    depth: int
    top_n: int
    index_documents: int = 0
    query_count: int = 0

    @property
    def baseline(self) -> Mapping[str, Any]:
        return self.rows[0]

    @property
    def fused(self) -> Mapping[str, Any]:
        return self.rows[1]

    def to_dict(self) -> dict[str, Any]:
        return {
            "depth": self.depth,
            "top_n": self.top_n,
            "params": self.params.to_dict(),
            "index_documents": self.index_documents,
            "query_count": self.query_count,
            "rows": [dict(row) for row in self.rows],
            "by_kind": {
                name: report.by_kind() for name, report in self.reports.items()
            },
        }


def _row(name: str, report: EvalReport) -> dict[str, Any]:
    """把一份评测报告压成对比表的一行。"""
    return {
        "参赛者": name,
        "命中项数": report.hit_total,
        "期望条目数": report.expected_total,
        "命中率": round(report.hit_rate, 6),
        "Top-N命中率": round(report.top_n_hit_rate, 6),
        "出处覆盖率": round(report.citation_coverage, 6),
        "出处齐备数": report.citation_complete,
        "带出处引用数": report.cited_total,
        "检索返回条目数": report.retrieved_total,
        "负例召回条目数": report.negative_retrieved_total,
    }


def compare_baselines(
    eval_set: EvalSet,
    sources: Sequence[MaterialSource],
    *,
    depth: int = 10,
    top_n: int = 5,
    params: Optional[RetrievalParams] = None,
    candidate_depth: int = 50,
    kb_ids: Sequence[str] = ("threshold", "policy", "corpus"),
) -> BaselineComparison:
    """跑两侧召回并算三个指标。

    ``params`` 是**融合侧**的参数面（阈值 / 两路权重 / 候选池 / 返回条数）；
    基线不吃这些（它没有闸门、没有权重、没有重排），只吃同一份材料范围与
    同一份生效期口径。
    """
    effective = params or RetrievalParams(
        similarity_threshold=0.2,
        keywords_similarity_weight=0.5,
        top_n=depth,
        top_k=1024,
        kb_ids=tuple(kb_ids),
    )

    index = MaterialIndex()
    index.rebuild(list(sources))

    baseline = BM25BaselineRetriever(
        index, candidate_depth=candidate_depth, kb_ids=tuple(kb_ids)
    )
    fused = HybridEvalRetriever(index, effective, depth=depth)

    base_report = evaluate(eval_set, baseline, depth=depth, top_n=top_n)
    fused_report = evaluate(eval_set, fused, depth=depth, top_n=top_n)

    rows = (
        _row(BASELINE_NAME, base_report),
        _row(FUSED_NAME, fused_report),
    )
    return BaselineComparison(
        rows=rows,
        reports={BASELINE_NAME: base_report, FUSED_NAME: fused_report},
        params=effective,
        depth=depth,
        top_n=top_n,
        index_documents=index.doc_count(),
        query_count=len(eval_set),
    )


def render_comparison_markdown(
    comparison: BaselineComparison,
    *,
    eval_meta: Optional[Mapping[str, Any]] = None,
) -> str:
    """渲染人读的对比表（**不写时间戳**，同一命令两次结果逐字节相同）。"""
    rows = comparison.rows
    base_row, fused_row = rows[0], rows[1]
    params = comparison.params
    depth = comparison.depth
    top_n = comparison.top_n
    meta = dict(eval_meta or {})

    lines: list[str] = []
    lines.append("# 召回基线对比：纯 BM25 单路 vs 融合引擎")
    lines.append("")
    lines.append(
        "同一份材料、同一份评测集、同一把尺子（`critval.evaluate.evaluate`），"
        "只差召回做法：**基线**不做结构化精确匹配、不做规则重排、不做综合分硬闸门；"
        "**融合**是产品主路径的混合召回引擎。"
    )
    lines.append("")

    lines.append("## 参数面")
    lines.append("")
    lines.append("| 项 | 取值 |")
    lines.append("|------|------|")
    lines.append("| 检索深度 | {} |".format(depth))
    lines.append("| Top-N | {} |".format(top_n))
    lines.append("| 综合分硬闸门 | {} |".format(params.similarity_threshold))
    lines.append("| 结构化精确路权重 | {:.2f} |".format(params.structured_weight))
    lines.append("| BM25 词法路权重 | {:.2f} |".format(params.keywords_similarity_weight))
    lines.append("| 候选池 | {} |".format(params.top_k))
    lines.append("| 材料范围 | {} |".format("、".join(params.kb_ids)))
    lines.append("| 索引条目 | {} |".format(comparison.index_documents))
    lines.append(
        "| 评测集查询数 | {}（正例 {} / 负例 {}） |".format(
            comparison.query_count,
            meta.get("positive", comparison.query_count),
            meta.get("negative", 0),
        )
    )
    lines.append("")

    lines.append("## 对比表（三项指标）")
    lines.append("")
    lines.append("| 参赛者 | 命中项数 | Top-{n} 命中率 | 出处覆盖率 |".format(n=top_n))
    lines.append("|------|----------|--------------|------------|")
    for row in rows:
        lines.append(
            "| {} | {} / {}（{:.1%}） | {:.1%} | {:.1%} |".format(
                row["参赛者"],
                row["命中项数"],
                row["期望条目数"],
                row["命中率"],
                row["Top-N命中率"],
                row["出处覆盖率"],
            )
        )
    lines.append("")
    lines.append(
        "**差值**：命中项数 {delta} 条（{b_hit} → {f_hit}），Top-{n} 命中率 "
        "{dtop}（{b_top:.1%} → {f_top:.1%}），出处覆盖率 {dcit}（{b_cit:.1%} → {f_cit:.1%}）。".format(
            delta=fused_row["命中项数"] - base_row["命中项数"],
            b_hit=base_row["命中项数"],
            f_hit=fused_row["命中项数"],
            dtop=fused_row["Top-N命中率"] - base_row["Top-N命中率"],
            b_top=base_row["Top-N命中率"],
            f_top=fused_row["Top-N命中率"],
            dcit=fused_row["出处覆盖率"] - base_row["出处覆盖率"],
            b_cit=base_row["出处覆盖率"],
            f_cit=fused_row["出处覆盖率"],
            n=top_n,
        )
    )
    lines.append("")

    lines.append("## 分类指标（差在哪儿）")
    lines.append("")
    lines.append("| 查询类型 | 条数 | 期望条目 | 基线命中率 | 融合命中率 | 差值 |")
    lines.append("|------|------|----------|------------|------------|------|")
    base_kinds = comparison.reports[BASELINE_NAME].by_kind()
    fused_kinds = comparison.reports[FUSED_NAME].by_kind()
    for kind in sorted(set(base_kinds) | set(fused_kinds)):
        base_item = base_kinds.get(kind, {"queries": 0, "expected": 0, "hit_rate": 0.0})
        fused_item = fused_kinds.get(kind, {"queries": 0, "expected": 0, "hit_rate": 0.0})
        lines.append(
            "| {} | {} | {} | {:.1%} | {:.1%} | {:+.1%} |".format(
                kind,
                base_item["queries"],
                base_item["expected"],
                base_item["hit_rate"],
                fused_item["hit_rate"],
                fused_item["hit_rate"] - base_item["hit_rate"],
            )
        )
    lines.append("")
    lines.append(
        "> 负例一类（材料里根本没有该项目的查询）期望条目数为 0，**不进命中率分母**，"
        "只报「负例召回条目数」供闸门与降级策略参考 —— 基线 {} 条 / 融合 {} 条。".format(
            base_row["负例召回条目数"], fused_row["负例召回条目数"]
        )
    )
    lines.append("")

    lines.append("## 怎么读这张表")
    lines.append("")
    lines.append(
        "* **出处覆盖率两侧都是 100%** —— 因为出处是在**材料解析时**就挂到每条"
        "依据上的（阈值行 / 制度条款），两侧读的是同一份索引。出处覆盖不区分"
        "召回做法，它区分的是「这条依据能不能被定位到行」。"
    )
    lines.append(
        "* **差值集中在 `threshold_lookup` 一族** —— 那一族问的是"
        "「这个检验项目的危急阈值是多少」。单路 BM25 要在几千条近似条款里"
        "把阈值依据行排到前面，靠的是词面碰运气；融合引擎有结构化精确路"
        "（按项目码查表命中即满分），这类查询是**查表**不是**猜词**。"
    )
    lines.append(
        "* **出处覆盖率不是区分点，排序才是** —— 这正是「依据可核对」与"
        "「依据排得对不对」两件事：出处保证值班人能翻回原文，排序保证"
        "值班人先看到该看的那一条。"
    )
    lines.append("")

    lines.append("## 复算")
    lines.append("")
    lines.append("```bash")
    lines.append("python3 scripts/compare_baseline.py")
    lines.append("```")
    lines.append("")
    lines.append(
        "同一命令跑两次得到逐字节相同的 `docs/examples/recall_baseline_compare.md` "
        "与 `reports/recall_baseline_compare.csv`（两个产物都不写时间戳）。"
    )
    lines.append("")
    lines.append(
        "口径说明：命中率按**条目数**汇总（逐查询命中数 ÷ 期望条目数，负例不进分母）；"
        "Top-N 命中率只数落在前 {} 名的期望条目；出处覆盖率只对**带出处引用**的结果计分，"
        "出处字段齐备与否按材料类型判定。".format(top_n)
    )
    return "\n".join(lines) + "\n"


def write_comparison_csv(
    comparison: BaselineComparison,
    path: str | Path,
) -> Path:
    """写对比表 CSV（UTF-8 BOM，Excel 直接双击能看）。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(CSV_COLUMNS))
        writer.writeheader()
        for row in comparison.rows:
            writer.writerow(row)
    return target
