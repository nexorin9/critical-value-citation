"""召回评测器与评测集的验收测试。

覆盖三件事：

1. **三个指标算得对** —— 用手算的小集合逐个核对（命中率 / Top-N 命中率 / 出处覆盖率）；
2. **口径不被悄悄改掉** —— 负例不进分母、缺出处字段的引用不进出处覆盖率分子、
   评测键在"真值侧"与"检索侧"是同一个函数算出来的；
3. **评测集可复算** —— 重跑生成脚本得到同一份真值，且期望条目在索引里都找得到。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from critval.evaluate import (  # noqa: E402
    EvalQuery,
    EvalSet,
    MaterialRetriever,
    RetrievedItem,
    canonical_key,
    citation_missing_fields,
    evaluate,
    load_eval_set,
    render_markdown,
)
from critval.index.builder import MaterialIndex, MaterialSource  # noqa: E402
from critval.models import Citation  # noqa: E402

from build_eval_set import (  # noqa: E402
    DEFAULT_CORPUS,
    DEFAULT_POLICY,
    DEFAULT_THRESHOLD,
    DEFAULT_TRUTH,
    build_eval_set,
    ensure_corpus,
)


# ---------------------------------------------------------------------------
# 手算小集合：三个指标的核对
# ---------------------------------------------------------------------------


def _citation(name: str = "示例材料.xlsx", line_no: int = 7, paragraph_no: int | None = None,
              sha: str = "a" * 64, version: str = "示例 v1.0") -> Citation:
    return Citation(
        source_name=name,
        source_sha256=sha,
        kind="policy_clause" if paragraph_no is not None else "threshold_row",
        paragraph_no=paragraph_no,
        line_no=line_no,
        file_version=version,
    )


def _item(key: str, rank: int, citation: Citation | None = None, score: float = 1.0) -> RetrievedItem:
    return RetrievedItem(key=key, rank=rank, score=score, citation=citation)


class _FakeRetriever:
    """按查询 id 返回预先摆好的结果（用来手算指标）。"""

    def __init__(self, plan: dict[str, list[RetrievedItem]]):
        self.plan = plan
        self.calls: list[tuple[str, int]] = []

    def retrieve(self, query: EvalQuery, depth: int):
        self.calls.append((query.query_id, depth))
        return list(self.plan.get(query.query_id, []))[:depth]


def test_命中率按命中条目数除以期望条目数():
    # q1 期望 2 条，命中 1 条；q2 期望 1 条，命中 1 条。
    eval_set = EvalSet(queries=(
        EvalQuery(query_id="q1", text="t1", expected_keys=("a", "b")),
        EvalQuery(query_id="q2", text="t2", expected_keys=("c",)),
    ))
    retriever = _FakeRetriever({
        "q1": [_item("a", 1), _item("z", 2)],
        "q2": [_item("c", 1)],
    })
    report = evaluate(eval_set, retriever, depth=10, top_n=5)
    assert report.expected_total == 3
    assert report.hit_total == 2
    assert report.hit_rate == pytest.approx(2 / 3)
    assert report.positive_query_count == 2
    assert report.negative_query_count == 0


def test_TopN命中率只数落在前N名的期望条目():
    eval_set = EvalSet(queries=(
        EvalQuery(query_id="q1", text="t1", expected_keys=("a", "b")),
    ))
    # 期望条目 a 排第 3、b 排第 6 —— 检索深度 10 内两条都命中，前 5 名只命中 a。
    retriever = _FakeRetriever({
        "q1": [_item("x", 1), _item("y", 2), _item("a", 3), _item("z", 4), _item("w", 5), _item("b", 6)],
    })
    report = evaluate(eval_set, retriever, depth=10, top_n=5)
    assert report.hit_total == 2          # 检索深度 10 内 a、b 都召回
    assert report.top_n_hit_total == 1    # 前 5 名内只有 a
    assert report.hit_rate == pytest.approx(1.0)
    assert report.top_n_hit_rate == pytest.approx(0.5)
    assert report.top_n_full_hit_rate == pytest.approx(0.0)


def test_负例不计入命中率分母():
    """负例（期望为空）既不进分母，也不因为"召回了东西"被算成命中。"""
    positive = EvalSet(queries=(
        EvalQuery(query_id="q1", text="t1", expected_keys=("a",)),
    ))
    retriever = _FakeRetriever({"q1": [_item("a", 1)]})
    base = evaluate(positive, retriever, depth=10, top_n=5)

    with_negative = EvalSet(queries=(
        EvalQuery(query_id="q1", text="t1", expected_keys=("a",)),
        EvalQuery(query_id="qneg", text="t2", expected_keys=()),
    ))
    retriever2 = _FakeRetriever({
        "q1": [_item("a", 1)],
        "qneg": [_item("x", 1), _item("y", 2), _item("z", 3)],
    })
    mixed = evaluate(with_negative, retriever2, depth=10, top_n=5)

    assert mixed.hit_rate == base.hit_rate
    assert mixed.expected_total == base.expected_total
    assert mixed.hit_total == base.hit_total
    assert mixed.negative_query_count == 1
    assert mixed.positive_query_count == 1
    # 负例召回了多少条单独统计（供阈值闸门与降级策略参考），不影响三个指标。
    assert mixed.negative_retrieved_total == 3
    assert base.negative_retrieved_total == 0


def test_出处覆盖率只对带出处的引用计分且缺字段不进分子():
    eval_set = EvalSet(queries=(
        EvalQuery(query_id="q1", text="t1", expected_keys=("a",)),
    ))
    retriever = _FakeRetriever({
        "q1": [
            _item("a", 1, _citation("制度.docx", line_no=7, paragraph_no=7)),
            _item("b", 2, _citation("阈值表.xlsx", line_no=3)),          # 齐备（kind_aware）
            _item("c", 3, _citation("残缺.xlsx", line_no=None)),          # 缺可读行号
            _item("d", 4, None),                                          # 压根没出处
        ],
    })
    report = evaluate(eval_set, retriever, depth=10, top_n=5)
    assert report.cited_total == 3          # d 没有出处，不进分母
    assert report.citation_complete == 2    # c 缺行号，不进分子
    assert report.citation_coverage == pytest.approx(2 / 3)
    assert report.citation_presence_rate == pytest.approx(3 / 4)


def test_出处齐备按材料类型判定_严格口径三件套缺一不可():
    threshold_citation = _citation("阈值表.xlsx", line_no=3)          # 无段落序号
    policy_citation = _citation("制度.docx", line_no=7, paragraph_no=7)

    assert citation_missing_fields(threshold_citation, "kind_aware") == ()
    assert citation_missing_fields(threshold_citation, "strict") == ("paragraph_no",)
    assert citation_missing_fields(policy_citation, "kind_aware") == ()
    assert citation_missing_fields(policy_citation, "strict") == ()
    assert citation_missing_fields(None, "kind_aware") == ("citation",)


def test_评测参数非法时报错():
    eval_set = EvalSet(queries=(EvalQuery(query_id="q1", text="t1", expected_keys=("a",)),))
    retriever = _FakeRetriever({"q1": [_item("a", 1)]})
    with pytest.raises(ValueError):
        evaluate(eval_set, retriever, depth=0, top_n=1)
    with pytest.raises(ValueError):
        evaluate(eval_set, retriever, depth=2, top_n=5)      # Top-N 大于检索深度
    with pytest.raises(ValueError):
        evaluate(eval_set, retriever, depth=2, top_n=1, citation_mode="unknown")


# ---------------------------------------------------------------------------
# 评测集本身：可复算 + 真值一致
# ---------------------------------------------------------------------------


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
def material_index() -> MaterialIndex:
    index = MaterialIndex()
    index.rebuild([
        MaterialSource.from_path(DEFAULT_THRESHOLD, kind="threshold_table", key="threshold"),
        MaterialSource.from_path(DEFAULT_POLICY, kind="policy_document", key="policy"),
        MaterialSource.from_path(DEFAULT_CORPUS, kind="corpus", key="corpus"),
    ])
    return index


def test_评测集规模与正负例构成(eval_set: EvalSet):
    assert len(eval_set.queries) >= 120
    assert eval_set.negative_queries, "评测集里必须有负例（材料里没有该项目的查询）"
    assert eval_set.positive_queries
    for query in eval_set.positive_queries:
        assert query.expected_keys, "正例必须写清期望命中条目"
    for query in eval_set.negative_queries:
        assert query.expected_count == 0


def test_评测集可复算_两次生成得到同一份真值(eval_set: EvalSet):
    again = build_eval_set(
        threshold_path=DEFAULT_THRESHOLD,
        policy_path=DEFAULT_POLICY,
        corpus_path=DEFAULT_CORPUS,
        truth_path=DEFAULT_TRUTH,
    )
    assert [q.to_dict() for q in again.queries] == [q.to_dict() for q in eval_set.queries]


def test_评测集里的期望条目在索引里都找得到(eval_set: EvalSet, material_index: MaterialIndex):
    """真值侧与检索侧用的是同一个键函数 —— 这里逐条核对，不靠"约定"。"""
    known = {canonical_key(document) for document in material_index.documents_by_id().values()}
    missing = [
        key
        for query in eval_set.queries
        for key in query.expected_keys
        if key not in known
    ]
    assert not missing, f"评测集里有期望条目在索引里不存在：{missing[:5]}"


def test_负例的项目确实不在材料里(eval_set: EvalSet):
    names = set()
    for query in eval_set.queries:
        if query.kind == "threshold_lookup":
            names.add(query.meta.get("item_code"))
    for query in eval_set.negative_queries:
        probe = query.meta.get("probe_name", "")
        assert probe not in names
        assert probe  # 负例必须写明"问的是哪个材料里没有的项目"


def test_默认召回器能取回阈值行并给出出处(eval_set: EvalSet, material_index: MaterialIndex):
    """结构化路对阈值行是精确命中（命中键覆盖率 = 1.0），出处齐备。"""
    retriever = MaterialRetriever(material_index, candidate_depth=50)
    query = [q for q in eval_set.queries if q.kind == "threshold_lookup"][0]
    items = retriever.retrieve(query, depth=50)
    keys = {item.key for item in items}
    assert query.expected_keys[0] in keys, "阈值行应当被召回（检索深度 50 内）"
    hit = [item for item in items if item.key == query.expected_keys[0]][0]
    assert hit.citation is not None
    assert citation_missing_fields(hit.citation, "kind_aware") == ()
    assert hit.citation.source_sha256 and hit.citation.line_no


def test_评测集读写往返(eval_set: EvalSet, tmp_path: Path):
    from critval.evaluate import write_eval_set

    path = tmp_path / "queries.jsonl"
    write_eval_set(eval_set, path)
    loaded = load_eval_set(path)
    assert len(loaded.queries) == len(eval_set.queries)
    assert loaded.queries[0].to_dict() == eval_set.queries[0].to_dict()
    assert loaded.meta.get("eval_set_version") == eval_set.meta.get("eval_set_version")


def test_报告渲染含三个指标与材料表(eval_set: EvalSet, material_index: MaterialIndex):
    retriever = MaterialRetriever(material_index, candidate_depth=20)
    report = evaluate(eval_set, retriever, depth=10, top_n=5)
    markdown = render_markdown(report)
    assert "命中率" in markdown
    assert "出处覆盖率" in markdown
    assert "## 三个指标" in markdown
    assert "评测材料" in markdown
    # 三个指标的数值必须出现在报告里（不是只写指标名）
    assert "{:.1%}".format(report.hit_rate) in markdown
    assert "{:.1%}".format(report.citation_coverage) in markdown


def test_评测报告落盘含三个指标(eval_set: EvalSet, tmp_path: Path):
    """``scripts/run_eval.py`` 的落盘产物：Markdown + JSON。"""
    from critval.evaluate import run_evaluation

    report = run_evaluation(
        eval_set,
        depth=10,
        top_n=5,
        candidate_depth=20,
    )
    payload = report.to_dict()
    assert payload["query_count"] == len(eval_set.queries)
    assert 0.0 <= payload["hit_rate"] <= 1.0
    assert 0.0 <= payload["top_n_hit_rate"] <= 1.0
    assert 0.0 <= payload["citation_coverage"] <= 1.0
    assert payload["expected_total"] == sum(q.expected_count for q in eval_set.positive_queries)
    # 材料指纹一致（评测集就是用这些材料生成的）
    assert report.meta.get("materials_match") is True
