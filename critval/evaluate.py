"""召回评测：可复算的评测集 + 批量评测器（三个指标）。

## 这一层回答什么

前面几层把「材料 → 条目 → 两路候选 → 融合排序」都做出来了，但**好不好**还没有
一个统一的裁判。任务 id 7（混合召回引擎）、id 8（纯 BM25 基线对比）、id 30（规则
重排）、id 31（参数敏感度扫描）都要回答同一个问题：**换一种召回做法，到底是变好了
还是变差了？** 没有共同的尺子，四次改动就只能各说各话。

这一层提供两样东西：

1. **评测集**（``EvalSet``）—— 一组「查询 → 应命中的依据条目」的标注查询。
   标注不是人工手挑的，而是**生成时的真值**：语料侧车 ``policy_corpus.truth.jsonl``
   记录了每条条款是哪个项目、哪个科室、哪个时限生成的，评测集直接取这份真值当
   「期望命中」。重跑生成脚本得到同一份真值 —— 因此同一个评测集在任何时候跑出的
   数字都可复算、可对比。
2. **批量评测器**（``evaluate``）—— 对一批查询跑一遍召回，输出三个指标：

   * **命中率** = 命中的期望条目数 / 期望条目总数（逐查询算完再汇总，负例不进分母）；
   * **Top-N 命中率** = 期望条目落在前 N 名的比例（另报「整条查询的期望条目全部
     进前 N」的比例）；
   * **出处覆盖率** = 检索结果里**带出处的引用**中，出处字段齐备的比例。

## 三个口径上的取舍，都写在这里

* **负例不计入命中率分母。** 负例（材料里根本没有这个项目的查询）期望条目数为 0，
  把它算进分母会把「没找到」和「不该找」搅在一起。负例单独统计召回条数，供阈值
  闸门与降级策略参考。
* **出处覆盖率只对「声明了出处」的引用计分。** 一条语料行本来就没有出处，把它算进
  分母等于要求它有出处 —— 那不是覆盖率，是另一种考核。分母 = 带 ``Citation`` 的
  检索结果条数；分子 = 其中出处字段齐备的条数。
* **出处齐备按材料类型判定（``kind_aware``，默认）。** 制度条款要「sha256 + 段落
  序号 + 可读行号」，阈值行没有「段落」这个概念，按「sha256 + 可读行号 + 文件版本」
  判定。报告里**同时**给出严格口径（三个字段缺一不可，阈值行因此不合格）的数值，
  两个数字都摆出来，不藏。

## 裁判是「可换的」

评测器只认一个协议（``Retriever.retrieve``），不关心召回是怎么做的。默认实现
``MaterialRetriever`` 把现有两路（结构化精确 + 词法 BM25）接起来跑；任务 id 7 的
正式引擎、任务 id 8 的纯 BM25 基线都可以直接塞进来对比 —— 换的是裁判的**参赛者**，
不是裁判本身。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from critval.index.builder import MaterialIndex, MaterialSource
from critval.index.structured import IndexDocument
from critval.models import Citation
from critval.retrieve.fusion import FusionWeights, fuse
from critval.retrieve.paths import lexical_candidates, structured_candidates
from critval.retrieve.result import RetrievedItem, Retriever, document_key

__all__ = [
    "CITATION_MODES",
    "EvalQuery",
    "EvalReport",
    "EvalSet",
    "MaterialRetriever",
    "QueryOutcome",
    "RetrievedItem",
    "Retriever",
    "canonical_key",
    "citation_missing_fields",
    "default_material_sources",
    "evaluate",
    "load_eval_set",
    "render_markdown",
    "run_evaluation",
    "write_eval_set",
]

#: 出处齐备的两种口径。
CITATION_MODES = ("kind_aware", "strict")


# ---------------------------------------------------------------------------
# 键（truth key）
# ---------------------------------------------------------------------------


def canonical_key(document: IndexDocument) -> str:
    """一个条目的**评测键** —— 评测集的真值与检索结果必须用同一个函数算。

    口径写在 ``critval.retrieve.result.document_key``（召回层），这里只做转发，
    保证「真值侧」与「检索侧」永远是同一个函数 —— 哪怕换召回做法（基线 /
    融合）也不会各算各的。

    * 阈值表行 → ``threshold_row:{项目码}#{行号}``；
    * 制度条款 → ``policy_clause:{条款稳定标识}``；
    * 语料行 → ``corpus_line:{条款号}``。

    键里**不含**材料 sha256：语料与制度会改版，键跟着变就等于评测集每次都要重做；
    改版是否影响召回由版本索引那一层管，不混进评测指标。
    """
    return document_key(document)


# ---------------------------------------------------------------------------
# 评测集
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EvalQuery:
    """一条标注查询。

    ``expected_keys`` 是**应命中的依据条目**（评测键）。为空即负例 ——
    材料里压根没有这条依据，期望是「不产出依据」。
    """

    query_id: str
    text: str
    kind: str = "generic"
    filters: Mapping[str, str] = field(default_factory=dict)
    expected_keys: tuple[str, ...] = ()
    note: str = ""
    meta: Mapping[str, Any] = field(default_factory=dict)

    @property
    def expected_count(self) -> int:
        return len(self.expected_keys)

    @property
    def is_negative(self) -> bool:
        """负例 = 期望条目数为 0（不进命中率分母）。"""
        return not self.expected_keys

    def to_dict(self) -> dict[str, Any]:
        return {
            "query_id": self.query_id,
            "text": self.text,
            "kind": self.kind,
            "filters": dict(self.filters),
            "expected_keys": list(self.expected_keys),
            "expected_count": self.expected_count,
            "note": self.note,
            "meta": dict(self.meta),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "EvalQuery":
        return cls(
            query_id=str(payload.get("query_id", "")),
            text=str(payload.get("text", "")),
            kind=str(payload.get("kind", "generic")),
            filters=dict(payload.get("filters") or {}),
            expected_keys=tuple(payload.get("expected_keys") or ()),
            note=str(payload.get("note", "")),
            meta=dict(payload.get("meta") or {}),
        )


@dataclass(frozen=True)
class EvalSet:
    """一批标注查询 + 生成元信息。"""

    queries: tuple[EvalQuery, ...] = ()
    meta: Mapping[str, Any] = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.queries)

    @property
    def positive_queries(self) -> tuple[EvalQuery, ...]:
        return tuple(query for query in self.queries if not query.is_negative)

    @property
    def negative_queries(self) -> tuple[EvalQuery, ...]:
        return tuple(query for query in self.queries if query.is_negative)

    @property
    def expected_total(self) -> int:
        return sum(query.expected_count for query in self.queries)

    def to_dict(self) -> dict[str, Any]:
        return {"meta": dict(self.meta), "queries": [q.to_dict() for q in self.queries]}

    def write_jsonl(self, path: str | Path) -> Path:
        return write_eval_set(self, path)


def write_eval_set(eval_set: EvalSet, path: str | Path) -> Path:
    """写评测集（JSONL）。

    第一行是元信息（生成器版本 / 材料指纹 / 各类查询条数），其后每行一条查询。
    **不写时间戳** —— 重跑生成脚本必须得到逐字节相同的文件，否则「可复算」无从谈起。
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps({"record": "meta", **dict(eval_set.meta)}, ensure_ascii=False, sort_keys=True))
        handle.write("\n")
        for query in eval_set.queries:
            handle.write(json.dumps(query.to_dict(), ensure_ascii=False, sort_keys=True))
            handle.write("\n")
    return target


def load_eval_set(path: str | Path) -> EvalSet:
    """读评测集（JSONL）。第一行可有可无的元信息行。"""
    source = Path(path)
    meta: dict[str, Any] = {}
    queries: list[EvalQuery] = []
    with source.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            payload = json.loads(line)
            if payload.get("record") == "meta":
                meta = {key: value for key, value in payload.items() if key != "record"}
                continue
            queries.append(EvalQuery.from_dict(payload))
    return EvalSet(queries=tuple(queries), meta=meta)


# ---------------------------------------------------------------------------
# 检索结果
# ---------------------------------------------------------------------------
#
# ``RetrievedItem``（结果条目）与 ``Retriever``（召回器协议）住在召回层
# ``critval.retrieve.result`` —— 评测器与召回实现都从那儿取，依赖方向单向。
# 这里再转发一次，是为了「``from critval.evaluate import RetrievedItem``」
# 这条老用法继续可用（评测器是这两个类型的对外门面）。


class MaterialRetriever:
    """默认召回器：结构化精确路 + 词法 BM25 路，按权重融合。

    这是把现有两路接起来的**评测用装配**（不含阈值硬闸门与规则重排 —— 那两项分别
    属于任务 id 7 与 id 30），不另写一套召回逻辑。任务 id 7 的正式引擎落地后，
    换掉这个类即可，评测口径不变。
    """

    def __init__(
        self,
        index: MaterialIndex,
        weights: Optional[FusionWeights] = None,
        candidate_depth: int = 50,
        at: Optional[date] = None,
    ) -> None:
        self.index = index
        self.weights = (weights or FusionWeights()).validate()
        self.candidate_depth = max(1, candidate_depth)
        self.at = at
        self._documents: Optional[dict[str, IndexDocument]] = None

    def _document_map(self) -> dict[str, IndexDocument]:
        if self._documents is None:
            self._documents = self.index.documents_by_id()
        return self._documents

    def retrieve(self, query: EvalQuery, depth: int) -> list[RetrievedItem]:
        structured: list = []
        if query.filters:
            hits = self.index.structured.query(at=self.at, **dict(query.filters))
            requested = len(self.index.structured.query_keys(**dict(query.filters)))
            structured = structured_candidates(hits, requested_keys=max(1, requested))

        lexical_hits = self.index.search(query.text, top_k=self.candidate_depth)
        lexical = lexical_candidates(lexical_hits, self._document_map())

        merged = fuse(structured, lexical, self.weights)
        items: list[RetrievedItem] = []
        for rank, candidate in enumerate(merged[: max(0, depth)], start=1):
            document = candidate.document
            items.append(
                RetrievedItem(
                    key=canonical_key(document) if document is not None else candidate.doc_id,
                    rank=rank,
                    score=candidate.fused_score,
                    citation=document.citation if document is not None else None,
                    stable_key=candidate.stable_key,
                    doc_id=candidate.doc_id,
                    paths=candidate.paths,
                    item_code=document.field("item_code") if document is not None else "",
                )
            )
        return items


# ---------------------------------------------------------------------------
# 出处齐备判定
# ---------------------------------------------------------------------------


def citation_missing_fields(
    citation: Optional[Citation],
    mode: str = "kind_aware",
) -> tuple[str, ...]:
    """这条引用**缺**哪些出处字段（空 tuple = 齐备）。

    * ``kind_aware``（默认）—— 制度条款要 sha256 + 段落序号 + 可读行号；阈值行没有
      「段落」概念，按 sha256 + 可读行号 + 文件版本判定。
    * ``strict`` —— 三个字段缺一不可（sha256 / 段落序号 / 可读行号）。

    没有出处（``citation is None``）一律算缺（返回 ``("citation",)``）——
    "没有出处"不是"出处齐备"。
    """
    if mode not in CITATION_MODES:
        raise ValueError(f"不认识的出处口径：{mode}（支持：{'、'.join(CITATION_MODES)}）")
    if citation is None:
        return ("citation",)
    missing: list[str] = []
    if not citation.source_sha256:
        missing.append("source_sha256")
    if citation.line_no is None:
        missing.append("line_no")
    if mode == "strict":
        if citation.paragraph_no is None:
            missing.append("paragraph_no")
    else:
        if citation.kind == "policy_clause" and citation.paragraph_no is None:
            missing.append("paragraph_no")
        if not citation.file_version:
            missing.append("file_version")
    return tuple(missing)


# ---------------------------------------------------------------------------
# 指标
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class QueryOutcome:
    """一条查询的评测结果。"""

    query_id: str
    kind: str
    expected_count: int
    hit_count: int = 0
    top_n_hit_count: int = 0
    retrieved_count: int = 0
    cited_count: int = 0
    fully_hit_in_top_n: bool = False
    missing_keys: tuple[str, ...] = ()

    @property
    def negative(self) -> bool:
        return self.expected_count == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "query_id": self.query_id,
            "kind": self.kind,
            "expected_count": self.expected_count,
            "hit_count": self.hit_count,
            "top_n_hit_count": self.top_n_hit_count,
            "retrieved_count": self.retrieved_count,
            "cited_count": self.cited_count,
            "fully_hit_in_top_n": self.fully_hit_in_top_n,
            "missing_keys": list(self.missing_keys),
        }


@dataclass(frozen=True)
class EvalReport:
    """一次批量评测的完整结果。"""

    depth: int = 10
    top_n: int = 5
    citation_mode: str = "kind_aware"
    query_count: int = 0
    positive_query_count: int = 0
    negative_query_count: int = 0
    expected_total: int = 0
    hit_total: int = 0
    top_n_hit_total: int = 0
    full_hit_query_count: int = 0
    retrieved_total: int = 0
    cited_total: int = 0
    citation_complete: int = 0
    citation_complete_strict: int = 0
    negative_retrieved_total: int = 0
    outcomes: tuple[QueryOutcome, ...] = ()
    meta: Mapping[str, Any] = field(default_factory=dict)

    # —— 三个指标 ——

    @property
    def hit_rate(self) -> float:
        """命中率 = 命中条目数 / 期望条目数（负例不进分母）。"""
        if self.expected_total <= 0:
            return 0.0
        return self.hit_total / self.expected_total

    @property
    def top_n_hit_rate(self) -> float:
        """Top-N 命中率 = 期望条目落在前 N 的比例。"""
        if self.expected_total <= 0:
            return 0.0
        return self.top_n_hit_total / self.expected_total

    @property
    def top_n_full_hit_rate(self) -> float:
        """整条查询的期望条目**全部**进前 N 的查询占比。"""
        if self.positive_query_count <= 0:
            return 0.0
        return self.full_hit_query_count / self.positive_query_count

    @property
    def citation_coverage(self) -> float:
        """出处覆盖率（默认口径）= 出处齐备的引用 / 带出处的引用。"""
        if self.cited_total <= 0:
            return 0.0
        return self.citation_complete / self.cited_total

    @property
    def citation_coverage_strict(self) -> float:
        """出处覆盖率（严格口径：sha256 + 段落序号 + 可读行号）。"""
        if self.cited_total <= 0:
            return 0.0
        return self.citation_complete_strict / self.cited_total

    @property
    def citation_presence_rate(self) -> float:
        """检索结果里带出处引用的比例（辅助指标：多少条结果是有出处的）。"""
        if self.retrieved_total <= 0:
            return 0.0
        return self.cited_total / self.retrieved_total

    def by_kind(self) -> dict[str, dict[str, Any]]:
        """按查询类型分组的指标（看哪一类拖后腿）。"""
        buckets: dict[str, list[QueryOutcome]] = {}
        for outcome in self.outcomes:
            buckets.setdefault(outcome.kind, []).append(outcome)
        summary: dict[str, dict[str, Any]] = {}
        for kind, items in sorted(buckets.items()):
            expected = sum(item.expected_count for item in items)
            hits = sum(item.hit_count for item in items)
            top_hits = sum(item.top_n_hit_count for item in items)
            summary[kind] = {
                "queries": len(items),
                "expected": expected,
                "hits": hits,
                "hit_rate": (hits / expected) if expected else 0.0,
                "top_n_hit_rate": (top_hits / expected) if expected else 0.0,
            }
        return summary

    def to_dict(self) -> dict[str, Any]:
        return {
            "depth": self.depth,
            "top_n": self.top_n,
            "citation_mode": self.citation_mode,
            "query_count": self.query_count,
            "positive_query_count": self.positive_query_count,
            "negative_query_count": self.negative_query_count,
            "expected_total": self.expected_total,
            "hit_total": self.hit_total,
            "top_n_hit_total": self.top_n_hit_total,
            "full_hit_query_count": self.full_hit_query_count,
            "retrieved_total": self.retrieved_total,
            "cited_total": self.cited_total,
            "citation_complete": self.citation_complete,
            "citation_complete_strict": self.citation_complete_strict,
            "negative_retrieved_total": self.negative_retrieved_total,
            "hit_rate": round(self.hit_rate, 6),
            "top_n_hit_rate": round(self.top_n_hit_rate, 6),
            "top_n_full_hit_rate": round(self.top_n_full_hit_rate, 6),
            "citation_coverage": round(self.citation_coverage, 6),
            "citation_coverage_strict": round(self.citation_coverage_strict, 6),
            "citation_presence_rate": round(self.citation_presence_rate, 6),
            "by_kind": self.by_kind(),
            "meta": dict(self.meta),
            "outcomes": [outcome.to_dict() for outcome in self.outcomes],
        }


def evaluate(
    eval_set: EvalSet,
    retriever: Retriever,
    depth: int = 10,
    top_n: int = 5,
    citation_mode: str = "kind_aware",
) -> EvalReport:
    """跑一遍批量评测，返回可序列化的报告。

    * ``depth`` —— 检索深度（召回多少条参与「命中率」判定）；
    * ``top_n`` —— 前 N 名（参与「Top-N 命中率」判定），必须 ``≤ depth``。
    """
    if depth <= 0:
        raise ValueError(f"检索深度必须为正：{depth}")
    if top_n <= 0:
        raise ValueError(f"Top-N 必须为正：{top_n}")
    if top_n > depth:
        raise ValueError(f"Top-N（{top_n}）不能大于检索深度（{depth}）")
    if citation_mode not in CITATION_MODES:
        raise ValueError(f"不认识的出处口径：{citation_mode}")

    outcomes: list[QueryOutcome] = []
    hit_total = 0
    top_n_hit_total = 0
    expected_total = 0
    full_hit_queries = 0
    retrieved_total = 0
    cited_total = 0
    citation_complete = 0
    citation_complete_strict = 0
    negative_retrieved = 0

    for query in eval_set.queries:
        items = list(retriever.retrieve(query, depth))
        keys = [item.key for item in items]
        top_keys = keys[:top_n]
        expected = set(query.expected_keys)

        hit_count = len(expected & set(keys))
        top_n_hit_count = len(expected & set(top_keys))
        cited = sum(1 for item in items if item.citation is not None)
        complete = 0
        complete_strict = 0
        for item in items:
            if item.citation is None:
                continue
            if not citation_missing_fields(item.citation, "kind_aware"):
                complete += 1
            if not citation_missing_fields(item.citation, "strict"):
                complete_strict += 1

        if query.is_negative:
            negative_retrieved += len(items)
        else:
            expected_total += query.expected_count
            hit_total += hit_count
            top_n_hit_total += top_n_hit_count
            # 「全中」= 这条查询的期望条目**全部**落在前 N 名里（不是"深度内都找到了"）。
            if top_n_hit_count == query.expected_count:
                full_hit_queries += 1

        retrieved_total += len(items)
        cited_total += cited
        citation_complete += complete
        citation_complete_strict += complete_strict

        outcomes.append(
            QueryOutcome(
                query_id=query.query_id,
                kind=query.kind,
                expected_count=query.expected_count,
                hit_count=hit_count,
                top_n_hit_count=top_n_hit_count,
                retrieved_count=len(items),
                cited_count=cited,
                fully_hit_in_top_n=(not query.is_negative and top_n_hit_count == query.expected_count),
                missing_keys=tuple(sorted(expected - set(keys))),
            )
        )

    return EvalReport(
        depth=depth,
        top_n=top_n,
        citation_mode=citation_mode,
        query_count=len(eval_set.queries),
        positive_query_count=len(eval_set.positive_queries),
        negative_query_count=len(eval_set.negative_queries),
        expected_total=expected_total,
        hit_total=hit_total,
        top_n_hit_total=top_n_hit_total,
        full_hit_query_count=full_hit_queries,
        retrieved_total=retrieved_total,
        cited_total=cited_total,
        citation_complete=citation_complete,
        citation_complete_strict=citation_complete_strict,
        negative_retrieved_total=negative_retrieved,
        outcomes=tuple(outcomes),
        meta=dict(eval_set.meta),
    )


# ---------------------------------------------------------------------------
# 报告渲染
# ---------------------------------------------------------------------------


def render_markdown(report: EvalReport, title: str = "召回评测报告") -> str:
    """把报告渲染成 Markdown 表格（落 ``reports/eval_report.md``）。"""
    lines: list[str] = []
    lines.append(f"# {title}")
    lines.append("")
    meta = dict(report.meta)
    materials = meta.get("materials") or []
    if materials:
        lines.append("## 评测材料")
        lines.append("")
        lines.append("| 材料 | 类型 | 指纹 sha256 |")
        lines.append("|------|------|--------------|")
        for material in materials:
            lines.append(
                "| {name} | {kind} | `{sha}` |".format(
                    name=material.get("name", ""),
                    kind=material.get("kind", ""),
                    sha=str(material.get("sha256", ""))[:16],
                )
            )
        lines.append("")
        if meta.get("materials_match") is False:
            lines.append(
                "> ⚠️ **材料指纹与评测集记录不一致** —— 材料已改版或语料已重建，"
                "本轮数字只能当参考，不能与历史数字直接对比。")
            lines.append("")

    lines.append("## 三个指标")
    lines.append("")
    lines.append("| 指标 | 数值 | 口径 |")
    lines.append("|------|------|------|")
    lines.append(
        "| 命中率 | **{:.1%}** | 命中条目数 {} / 期望条目数 {}（负例不进分母） |".format(
            report.hit_rate, report.hit_total, report.expected_total
        )
    )
    lines.append(
        "| Top-{n} 命中率 | **{:.1%}** | 期望条目落在前 {n} 名：{} / {} |".format(
            report.top_n_hit_rate, report.top_n_hit_total, report.expected_total, n=report.top_n
        )
    )
    lines.append(
        "| 出处覆盖率 | **{:.1%}** | 出处齐备 {} / 带出处引用 {} |".format(
            report.citation_coverage, report.citation_complete, report.cited_total
        )
    )
    lines.append("")
    lines.append(
        "补充口径：Top-{n} 全中率（整条查询的期望条目全部进前 {n}）**{full:.1%}**；"
        "出处覆盖率·严格口径（sha256 + 段落序号 + 可读行号）**{strict:.1%}**；"
        "检索结果带出处比例 **{presence:.1%}**。".format(
            n=report.top_n,
            full=report.top_n_full_hit_rate,
            strict=report.citation_coverage_strict,
            presence=report.citation_presence_rate,
        )
    )
    lines.append("")

    lines.append("## 查询构成")
    lines.append("")
    lines.append("| 项 | 条数 |")
    lines.append("|------|------|")
    lines.append("| 查询总数 | {} |".format(report.query_count))
    lines.append("| 正例（有期望条目） | {} |".format(report.positive_query_count))
    lines.append("| 负例（无期望条目，不进分母） | {} |".format(report.negative_query_count))
    lines.append("| 期望条目总数 | {} |".format(report.expected_total))
    lines.append("| 检索返回条目总数 | {} |".format(report.retrieved_total))
    lines.append("| 负例召回条目数（仅供闸门/降级参考） | {} |".format(report.negative_retrieved_total))
    lines.append("")

    by_kind = report.by_kind()
    if by_kind:
        lines.append("## 分类指标")
        lines.append("")
        lines.append("| 查询类型 | 条数 | 期望条目 | 命中率 | Top-{n} 命中率 |".format(n=report.top_n))
        lines.append("|------|------|----------|--------|--------------|")
        for kind, item in by_kind.items():
            lines.append(
                "| {} | {} | {} | {:.1%} | {:.1%} |".format(
                    kind, item["queries"], item["expected"], item["hit_rate"], item["top_n_hit_rate"]
                )
            )
        lines.append("")

    misses = [outcome for outcome in report.outcomes if outcome.missing_keys]
    if misses:
        lines.append("## 未命中的查询（前 {} 条）".format(min(20, len(misses))))
        lines.append("")
        lines.append("| 查询 | 类型 | 期望 | 命中 | 缺哪些条目 |")
        lines.append("|------|------|------|------|------------|")
        for outcome in misses[:20]:
            lines.append(
                "| {} | {} | {} | {} | {} |".format(
                    outcome.query_id,
                    outcome.kind,
                    outcome.expected_count,
                    outcome.hit_count,
                    "、".join(outcome.missing_keys[:3]),
                )
            )
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append(
        "口径说明：命中率按**条目数**汇总（逐查询命中数 ÷ 期望条目数，负例不进分母）；"
        "Top-N 命中率只数落在前 {} 名的期望条目；出处覆盖率只对**带出处引用**的结果计分，"
        "出处字段齐备与否按材料类型判定（制度条款要 sha256 + 段落序号 + 可读行号，"
        "阈值行按 sha256 + 可读行号 + 文件版本）。".format(report.top_n)
    )
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# 一次性跑完（脚本与 CLI 共用）
# ---------------------------------------------------------------------------


def default_material_sources(root: Path | None = None) -> list[MaterialSource]:
    """默认的三份评测材料：阈值表 xlsx / 制度 docx / 条款语料。"""
    base = Path(root) if root is not None else Path(__file__).resolve().parent.parent
    return [
        MaterialSource.from_path(base / "samples" / "threshold" / "示例危急值阈值表.xlsx", kind="threshold_table", key="threshold"),
        MaterialSource.from_path(base / "samples" / "policy" / "示例危急值报告制度.docx", kind="policy_document", key="policy"),
        MaterialSource.from_path(base / "data" / "synth" / "policy_corpus.txt", kind="corpus", key="corpus"),
    ]


def run_evaluation(
    eval_set: EvalSet,
    materials: Optional[Sequence[MaterialSource]] = None,
    depth: int = 10,
    top_n: int = 5,
    weights: Optional[FusionWeights] = None,
    candidate_depth: int = 50,
    at: Optional[date] = None,
    citation_mode: str = "kind_aware",
) -> EvalReport:
    """建索引 → 跑召回 → 出报告（脚本与 CLI 的公共入口）。"""
    sources = list(materials) if materials is not None else default_material_sources()
    index = MaterialIndex()
    index.rebuild(sources)
    retriever = MaterialRetriever(index, weights=weights, candidate_depth=candidate_depth, at=at)
    report = evaluate(eval_set, retriever, depth=depth, top_n=top_n, citation_mode=citation_mode)
    meta = dict(report.meta)
    recorded = [dict(item) for item in (eval_set.meta.get("materials") or [])]
    meta["materials_recorded"] = recorded
    meta["materials"] = [
        {"key": source.key, "name": Path(source.path).name, "kind": source.kind, "sha256": source.sha256}
        for source in sources
    ]
    if recorded:
        current = {item["name"]: item["sha256"] for item in meta["materials"]}
        meta["materials_match"] = all(
            name in current and current[name] == str(item.get("sha256", ""))
            for item in recorded
            for name in [str(item.get("name", ""))]
        )
    meta["index_documents"] = index.doc_count()
    return EvalReport(
        depth=report.depth,
        top_n=report.top_n,
        citation_mode=report.citation_mode,
        query_count=report.query_count,
        positive_query_count=report.positive_query_count,
        negative_query_count=report.negative_query_count,
        expected_total=report.expected_total,
        hit_total=report.hit_total,
        top_n_hit_total=report.top_n_hit_total,
        full_hit_query_count=report.full_hit_query_count,
        retrieved_total=report.retrieved_total,
        cited_total=report.cited_total,
        citation_complete=report.citation_complete,
        citation_complete_strict=report.citation_complete_strict,
        negative_retrieved_total=report.negative_retrieved_total,
        outcomes=report.outcomes,
        meta=meta,
    )
