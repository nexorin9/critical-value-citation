"""召回参数敏感度分析与调参对比表的验收测试。

覆盖四件事：

1. **扫描可复现** —— 同一份材料、同一份评测集、同一组参数，两次扫描逐点一致；
2. **推荐规则固定且可复算** —— 推荐点一定在合格点集合里，且是按
   「出处覆盖率 → Top-N 命中率 → 命中率 → 全中率 → 闸门截断 → 结构化权重 →
   阈值」这条链挑出来的；
3. **不合格的点进不了推荐** —— 出处覆盖率不到 100% 的点、以及把阈值/权重推到
   边界的不可交付点，都必须被排除并写明原因；
4. **产物可核对** —— CSV 与 Markdown 都落盘，且含每个网格点的三指标数值。
"""

from __future__ import annotations

import csv
import io
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from build_eval_set import (  # noqa: E402
    DEFAULT_CORPUS,
    DEFAULT_POLICY,
    DEFAULT_THRESHOLD,
    DEFAULT_TRUTH,
    build_eval_set,
    ensure_corpus,
)
from critval.evaluate import EvalSet  # noqa: E402
from critval.index.builder import MaterialIndex, MaterialSource  # noqa: E402
from critval.retrieve.engine import HybridRetriever  # noqa: E402
from critval.retrieve.params import RetrievalParams  # noqa: E402
from critval.tune import (  # noqa: E402
    GridPoint,
    build_grid,
    recommend,
    render_tuning_markdown,
    scan_grid,
    write_grid_csv,
    write_tuning_markdown,
)

#: 测试用的小网格（够覆盖阈值与权重两个维度，又不至于跑太久）。
SMALL_THRESHOLDS = (0.0, 0.05, 0.2)
SMALL_WEIGHTS = (0.0, 0.5, 0.9, 1.0)

#: 测试只取评测集前若干条（覆盖各查询族，够验证口径，不必跑满 139 条）。
SUBSET_SIZE = 30


@pytest.fixture(scope="module")
def eval_set() -> EvalSet:
    ensure_corpus(DEFAULT_CORPUS, DEFAULT_TRUTH)
    return build_eval_set(
        threshold_path=DEFAULT_THRESHOLD,
        policy_path=DEFAULT_POLICY,
        corpus_path=DEFAULT_CORPUS,
        truth_path=DEFAULT_TRUTH,
    )


@pytest.fixture(scope="module")
def subset(eval_set: EvalSet) -> EvalSet:
    return EvalSet(queries=eval_set.queries[:SUBSET_SIZE], meta=dict(eval_set.meta))


@pytest.fixture(scope="module")
def material_index() -> MaterialIndex:
    index = MaterialIndex()
    index.rebuild([
        MaterialSource.from_path(DEFAULT_THRESHOLD, kind="threshold_table", key="threshold"),
        MaterialSource.from_path(DEFAULT_POLICY, kind="policy_document", key="policy"),
        MaterialSource.from_path(DEFAULT_CORPUS, kind="corpus", key="corpus"),
    ])
    return index


@pytest.fixture(scope="module")
def scan(subset: EvalSet, material_index: MaterialIndex) -> list[GridPoint]:
    return scan_grid(
        subset,
        material_index,
        thresholds=SMALL_THRESHOLDS,
        structured_weights=SMALL_WEIGHTS,
        depth=10,
        top_n=5,
    )


# ---------------------------------------------------------------------------
# 网格本身
# ---------------------------------------------------------------------------


def test_默认网格至少12个网格点():
    """扫描规模下限：对比表至少要有 12 个网格点才谈得上「敏感度」。"""
    grid = build_grid()
    assert len(grid) >= 12


def test_网格里词法路权重恒为结构化权重的补数():
    grid = build_grid()
    for threshold, structured_weight in grid:
        assert 0.0 <= threshold <= 1.0
        assert 0.0 <= structured_weight <= 1.0
        # 词法权重由 build_grid 的互补关系决定（见 tune.build_grid 文档）
        assert abs((1.0 - structured_weight) - (1.0 - structured_weight)) < 1e-9


def test_非法参数组合被排除():
    """越界取值即报错并指明字段 —— 网格里混进非法点，推荐结论就是假的。"""
    with pytest.raises(ValueError) as error:
        build_grid(thresholds=(1.5,), structured_weights=(0.5,))
    assert "similarity_threshold" in str(error.value)

    with pytest.raises(ValueError) as error:
        build_grid(thresholds=(0.2,), structured_weights=(-0.1,))
    assert "structured_weight" in str(error.value)

    with pytest.raises(ValueError) as error:
        build_grid(thresholds=("0.2",), structured_weights=(0.5,))
    assert "similarity_threshold" in str(error.value)

    with pytest.raises(ValueError):
        build_grid(thresholds=(), structured_weights=(0.5,))


def test_重复的网格取值只保留一个():
    grid = build_grid(thresholds=(0.1, 0.1, 0.2), structured_weights=(0.5, 0.5))
    assert grid == [(0.1, 0.5), (0.2, 0.5)]


# ---------------------------------------------------------------------------
# 扫描：可复现 + 跑的是产品主路径
# ---------------------------------------------------------------------------


def test_扫描可复现_同一命令两次结果一致(subset: EvalSet, material_index: MaterialIndex):
    first = scan_grid(
        subset, material_index,
        thresholds=SMALL_THRESHOLDS, structured_weights=SMALL_WEIGHTS,
        depth=10, top_n=5,
    )
    second = scan_grid(
        subset, material_index,
        thresholds=SMALL_THRESHOLDS, structured_weights=SMALL_WEIGHTS,
        depth=10, top_n=5,
    )
    assert [point.to_dict() for point in first] == [point.to_dict() for point in second]


def test_扫描跑的是产品主路径_结果与引擎直接调用一致(
    subset: EvalSet, material_index: MaterialIndex
):
    """网格点的排序结果必须与引擎直接跑出来的一致（不是另写一套评测专用召回）。"""
    from critval.tune import TuningRetriever

    engine = HybridRetriever(
        index=material_index,
        params=RetrievalParams(
            similarity_threshold=0.05,
            keywords_similarity_weight=0.5,
            top_n=10,
            top_k=1024,
            kb_ids=("threshold", "policy", "corpus"),
        ),
    )
    query = subset.queries[0]
    bundle = engine.gather(query.text, query.filters, top_k=1024)
    direct = engine.assemble(bundle, engine.params)
    adapted = TuningRetriever(engine, engine.params, {query.query_id: bundle})
    items = adapted.retrieve(query, depth=10)

    assert [hit.doc_id for hit in direct.hits] == [item.doc_id for item in items]
    assert [round(hit.score, 9) for hit in direct.hits] == [round(item.score, 9) for item in items]


def test_候选复用不改变扫描结果(subset: EvalSet, material_index: MaterialIndex):
    """候选只取一次（扫描提速）不能改变任何指标。"""
    from critval.tune import TuningRetriever

    engine = HybridRetriever(
        index=material_index,
        params=RetrievalParams(
            similarity_threshold=0.05, top_n=10, top_k=1024,
            kb_ids=("threshold", "policy", "corpus"),
        ),
    )
    params = RetrievalParams(
        similarity_threshold=0.05, keywords_similarity_weight=0.5,
        top_n=10, top_k=1024, kb_ids=("threshold", "policy", "corpus"),
    )
    reused = TuningRetriever(engine, params)
    reused.prepare(subset)
    fresh = TuningRetriever(engine, params)
    for query in subset.queries:
        fresh.bundles.pop(query.query_id, None)
        assert [
            (item.key, round(item.score, 9))
            for item in reused.retrieve(query, depth=10)
        ] == [
            (item.key, round(item.score, 9))
            for item in fresh.retrieve(query, depth=10)
        ]


# ---------------------------------------------------------------------------
# 推荐规则
# ---------------------------------------------------------------------------


def test_推荐参数满足推荐规则(scan: list[GridPoint]):
    recommendation, eligible, best_overall = recommend(scan)
    assert recommendation is not None
    assert recommendation in eligible
    # 第 1 级：出处覆盖率必须 100%
    assert recommendation.citation_coverage == pytest.approx(1.0)
    # 第 0 级：可交付（阈值 > 0、两路权重都 > 0）
    assert recommendation.threshold > 0.0
    assert 0.0 < recommendation.structured_weight < 1.0
    # 第 2 级：在合格点里 Top-N 命中率最高
    assert recommendation.top_n_hit_rate == pytest.approx(
        max(point.top_n_hit_rate for point in eligible)
    )
    assert recommendation.hit_rate == pytest.approx(
        max(point.hit_rate for point in eligible)
    )
    # 第 3 级：平手裁决取闸门截断更少者
    tied = [p for p in eligible if p.top_n_hit_rate == pytest.approx(recommendation.top_n_hit_rate)
            and p.hit_rate == pytest.approx(recommendation.hit_rate)]
    assert recommendation.gated_out_total == min(point.gated_out_total for point in tied)
    # 指标最优点单独报出来（多半是不可交付的边界点）
    assert best_overall is not None
    assert best_overall.top_n_hit_rate >= recommendation.top_n_hit_rate


def test_出处覆盖率不到100的点不会被推荐():
    """出处不齐备 = 不可信，不管命中率多高都不得推荐。"""
    def point(**kwargs) -> GridPoint:
        base = dict(
            threshold=0.05, structured_weight=0.9, lexical_weight=0.1,
            depth=10, top_n=5,
            hit_rate=0.5, top_n_hit_rate=0.5,
            citation_coverage=1.0, citation_coverage_strict=0.9,
            top_n_full_hit_rate=0.5,
            hit_total=5, top_n_hit_total=5, expected_total=10,
            cited_total=10, citation_complete=10, retrieved_total=10,
            deliverable=True, eligible=True,
        )
        base.update(kwargs)
        return GridPoint(**base)

    good = point(threshold=0.05, structured_weight=0.9, lexical_weight=0.1)
    # 命中率更高、但出处覆盖率只有 90% 的点
    bad = point(
        threshold=0.05, structured_weight=0.5, lexical_weight=0.5,
        hit_rate=0.95, top_n_hit_rate=0.95, citation_coverage=0.9,
        eligible=False, excluded_reason="出处覆盖率 90.0% < 100%（无出处即不可信，不得推荐）",
    )
    recommendation, eligible, _ = recommend([good, bad])
    assert recommendation is good
    assert bad not in eligible


def test_边界配置不可交付且不得推荐():
    """阈值 = 0（闸门失效）或某一路权重 = 0（该路哑火）一律不进推荐池。"""
    def point(**kwargs) -> GridPoint:
        base = dict(
            threshold=0.05, structured_weight=0.9, lexical_weight=0.1,
            depth=10, top_n=5,
            hit_rate=0.5, top_n_hit_rate=0.5,
            citation_coverage=1.0, citation_coverage_strict=0.9,
            top_n_full_hit_rate=0.5,
            hit_total=5, top_n_hit_total=5, expected_total=10,
            cited_total=10, citation_complete=10, retrieved_total=10,
            deliverable=True, eligible=True,
        )
        base.update(kwargs)
        return GridPoint(**base)

    good = point()
    gate_off = point(
        threshold=0.0, structured_weight=0.9, lexical_weight=0.1,
        hit_rate=0.99, top_n_hit_rate=0.99,
        deliverable=False, eligible=False,
        excluded_reason="阈值 = 0 等于关掉综合分硬闸门（不可交付）",
    )
    lexical_dead = point(
        threshold=0.05, structured_weight=1.0, lexical_weight=0.0,
        hit_rate=0.98, top_n_hit_rate=0.98,
        deliverable=False, eligible=False,
        excluded_reason="词法路权重 = 0 等于废掉 BM25 词法路（不可交付）",
    )
    recommendation, eligible, best_overall = recommend([good, gate_off, lexical_dead])
    assert recommendation is good
    assert gate_off not in eligible and lexical_dead not in eligible
    # 指标最优点仍然是边界点 —— 报告里必须把它单独列出来写明原因
    assert best_overall in (gate_off, lexical_dead)


def test_没有合格点时不硬凑推荐():
    ineligible = GridPoint(
        threshold=0.0, structured_weight=1.0, lexical_weight=0.0,
        depth=10, top_n=5,
        hit_rate=0.9, top_n_hit_rate=0.9,
        citation_coverage=0.8, citation_coverage_strict=0.7,
        top_n_full_hit_rate=0.9,
        hit_total=9, top_n_hit_total=9, expected_total=10,
        cited_total=10, citation_complete=8, retrieved_total=10,
        deliverable=False, eligible=False, excluded_reason="不可交付且出处不齐备",
    )
    recommendation, eligible, best_overall = recommend([ineligible])
    assert recommendation is None
    assert eligible == []
    assert best_overall is ineligible


def test_推荐规则在真实扫描上挑不出不可交付的点(scan: list[GridPoint]):
    recommendation, eligible, _ = recommend(scan)
    assert recommendation is not None
    for point in eligible:
        assert point.threshold > 0.0
        assert 0.0 < point.structured_weight < 1.0
        assert point.citation_coverage == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# 产物落盘
# ---------------------------------------------------------------------------


def test_CSV落盘含三指标与排除原因(scan: list[GridPoint], tmp_path: Path):
    path = write_grid_csv(scan, tmp_path / "recall_grid.csv")
    assert path.exists()
    rows = list(csv.DictReader(io.StringIO(path.read_text(encoding="utf-8-sig"))))
    assert len(rows) == len(scan)
    for row, point in zip(rows, scan):
        assert float(row["threshold"]) == pytest.approx(point.threshold)
        assert float(row["lexical_weight"]) == pytest.approx(point.lexical_weight)
        assert float(row["hit_rate"]) == pytest.approx(point.hit_rate * 100, abs=1e-3)
        assert float(row["top_n_hit_rate"]) == pytest.approx(point.top_n_hit_rate * 100, abs=1e-3)
        assert float(row["citation_coverage"]) == pytest.approx(point.citation_coverage * 100, abs=1e-3)
        assert row["eligible"] in ("true", "false")
    # 排除原因必须落在表里（不藏在脚注）
    assert any(row["excluded_reason"] for row in rows)


def test_markdown落盘含网格点与三指标数值(scan: list[GridPoint], tmp_path: Path):
    recommendation, eligible, best_overall = recommend(scan)
    path = write_tuning_markdown(
        scan, recommendation, tmp_path / "recall_tuning.md",
        meta={"query_count": len(scan), "materials": []},
        best_overall=best_overall, eligible=eligible,
    )
    markdown = path.read_text(encoding="utf-8")
    assert "命中率" in markdown
    assert "Top-5 命中率" in markdown
    assert "出处覆盖率" in markdown
    # 每个网格点一行，阈值与权重都出现
    for point in scan:
        assert "{:.2f}".format(point.threshold) in markdown
        assert "{:.2f}".format(point.structured_weight) in markdown
    # 推荐参数与规则写进文档
    assert "推荐参数" in markdown
    assert "出处覆盖率必须 100%" in markdown
    assert recommendation is not None
    assert "{:.2f}".format(recommendation.threshold) in markdown


def test_渲染函数与落盘结果一致(scan: list[GridPoint]):
    recommendation, eligible, best_overall = recommend(scan)
    text = render_tuning_markdown(
        scan, recommendation,
        meta={"query_count": len(scan)},
        best_overall=best_overall, eligible=eligible,
    )
    assert text.endswith("\n")
    assert text.count("|") > 100
