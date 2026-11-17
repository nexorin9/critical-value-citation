"""纯 BM25 单路基线 vs 融合引擎（任务 id 8 · 深度项 b 的可复现对比）。

覆盖四组硬要求：

* **基线真的是"单路"** —— 不走结构化精确路、不做规则重排、不做综合分硬闸门；
* **两侧同口径** —— 同一份材料、同一份评测集、同样的检索深度与 Top-N，
  指标由同一个评测器算；
* **差在哪儿说得清** —— 融合在"查阈值"这一族上明显优于基线（结构化路是
  **查表**不是**猜词**）；
* **对比表可复算** —— 同一命令跑两次得到逐字节相同的表。
"""

from __future__ import annotations

import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

from critval.evaluate import EvalQuery, EvalSet, evaluate
from critval.index.builder import MaterialIndex
from critval.index.structured import documents_from_fields
from critval.models import Citation
from critval.retrieve.baseline import BM25BaselineRetriever, HybridEvalRetriever
from critval.retrieve.params import RetrievalParams

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CORPUS = PROJECT_ROOT / "data" / "synth" / "policy_corpus.txt"
EVAL_SET = PROJECT_ROOT / "data" / "eval" / "queries.jsonl"
COMPARE_SCRIPT = PROJECT_ROOT / "scripts" / "compare_baseline.py"


# —— 测试装置 ——


def _threshold_doc(doc_id: str, *, item_code: str, item_name: str, version: str = "示例 v1.0"):
    """阈值行条目。

    **正文刻意只留项目码与单位** —— 阈值表本来就是一张「项目码 → 数值」的表，
    项目名在行里、词面很薄。这正是结构化路存在的理由：值班人问「血红蛋白危急值
    阈值是多少」，词法路在薄正文上撞不出这条行，结构化路按项目码一查即中。
    """
    return documents_from_fields(
        doc_id=doc_id,
        source_name="示例危急值阈值表.xlsx",
        source_sha256="sha-threshold-0001",
        kind="threshold_row",
        text=f"{item_code} g/L",
        fields={
            "item_code": item_code,
            "item_name": item_name,
            "direction": "both",
            "unit": "g/L",
            "version": version,
        },
        version=version,
        effective_from=date(2026, 1, 1),
        citation=Citation(
            source_name="示例危急值阈值表.xlsx",
            source_sha256="sha-threshold-0001",
            kind="threshold_row",
            line_no=2,
            file_version=version,
        ),
    )


def _policy_doc(
    doc_id: str,
    *,
    clause_no: str,
    text: str,
    paragraph_no: int,
    version: str = "示例 v1.0",
):
    return documents_from_fields(
        doc_id=doc_id,
        source_name="示例危急值报告制度.docx",
        source_sha256="sha-policy-0001",
        kind="policy_clause",
        text=text,
        fields={"clause_no": clause_no, "version": version},
        version=version,
        effective_from=date(2026, 1, 1),
        citation=Citation(
            source_name="示例危急值报告制度.docx",
            source_sha256="sha-policy-0001",
            kind="policy_clause",
            paragraph_no=paragraph_no,
            section_path=("第三章 报告流程与通知时限",),
            line_no=paragraph_no,
            file_version=version,
        ),
    )


def _toy_index() -> MaterialIndex:
    """小索引：两条阈值行（材料键 threshold）+ 两条制度条款（材料键 policy）。"""
    index = MaterialIndex()
    index.ingest_documents(
        "threshold",
        [
            _threshold_doc("th-718", item_code="718-7", item_name="血红蛋白"),
            _threshold_doc("th-2345", item_code="2345-7", item_name="葡萄糖"),
        ],
    )
    index.ingest_documents(
        "policy",
        [
            _policy_doc(
                "po-deadline",
                clause_no="第六条",
                paragraph_no=19,
                text="危急值报告 上报时限 10 分钟内 通知检验科值班技师",
            ),
            _policy_doc(
                "po-destination",
                clause_no="第七条",
                paragraph_no=27,
                text="危急值报告 记录去向 危急值登记系统 上报时限 10 分钟内",
            ),
        ],
    )
    return index


def _toy_eval_set() -> EvalSet:
    """小评测集：一条查阈值（结构化路能救）、一条查时限（词法路能救）、一条负例。"""
    return EvalSet(
        queries=(
            EvalQuery(
                query_id="q-threshold",
                text="血红蛋白危急值阈值是多少",
                kind="threshold_lookup",
                filters={"item_code": "718-7"},
                expected_keys=("threshold_row:th-718",),
            ),
            EvalQuery(
                query_id="q-policy",
                text="危急值上报时限 记录去向",
                kind="policy_flow_lookup",
                expected_keys=("policy_clause:po-deadline",),
            ),
            EvalQuery(
                query_id="q-negative",
                text="火星语料转换协议",
                kind="negative_no_material",
                expected_keys=(),
            ),
        ),
        meta={"query_count": 3},
    )


def _toy_params() -> RetrievalParams:
    return RetrievalParams(
        similarity_threshold=0.05,
        keywords_similarity_weight=0.5,
        top_n=10,
        top_k=1024,
        kb_ids=("threshold", "policy"),
    )


# ---------------------------------------------------------------------------
# 基线真的是"单路"
# ---------------------------------------------------------------------------


def test_基线不吃结构化过滤条件():
    """给项目码也救不了它 —— 基线只认查询文本，结构化路对它不存在。"""
    index = _toy_index()
    baseline = BM25BaselineRetriever(index, candidate_depth=50)
    query = EvalQuery(
        query_id="q",
        text="这个项目危急值到底是多少",
        filters={"item_code": "718-7"},
        expected_keys=(),
    )
    items = baseline.retrieve(query, depth=10)
    keys = {item.key for item in items}
    assert "threshold_row:th-718" not in keys


def test_基线命中的都标着词法路():
    index = _toy_index()
    baseline = BM25BaselineRetriever(index, candidate_depth=50)
    query = EvalQuery(query_id="q", text="危急值 上报时限", expected_keys=())
    items = baseline.retrieve(query, depth=10)
    assert items
    assert all(item.paths == ("lexical",) for item in items)


def test_基线不做综合分硬闸门():
    """闸门关到 1.0（谁都过不了），基线照样返回结果 —— 它没有闸门这回事。"""
    index = _toy_index()
    baseline = BM25BaselineRetriever(index, candidate_depth=50)
    query = EvalQuery(query_id="q", text="危急值 上报时限", expected_keys=())
    assert baseline.retrieve(query, depth=10)


def test_基线按名次排好且分数递减():
    index = _toy_index()
    baseline = BM25BaselineRetriever(index, candidate_depth=50)
    query = EvalQuery(query_id="q", text="危急值 上报时限 记录去向", expected_keys=())
    items = baseline.retrieve(query, depth=10)
    assert [item.rank for item in items] == list(range(1, len(items) + 1))
    scores = [item.score for item in items]
    assert scores == sorted(scores, reverse=True)


def test_基线尊重材料范围():
    """材料范围外的条目不参与（与融合引擎同口径）。"""
    index = _toy_index()
    baseline = BM25BaselineRetriever(index, candidate_depth=50, kb_ids=("threshold",))
    query = EvalQuery(query_id="q", text="危急值 上报时限 记录去向", expected_keys=())
    items = baseline.retrieve(query, depth=10)
    assert all(item.key.startswith("threshold_row:") for item in items)


# ---------------------------------------------------------------------------
# 两侧同口径可比
# ---------------------------------------------------------------------------


def test_两侧用同一个评测器算指标():
    """两侧跑同一份评测集、同一深度与 Top-N —— 指标可比。"""
    index = _toy_index()
    eval_set = _toy_eval_set()
    baseline = BM25BaselineRetriever(index, candidate_depth=50)
    fused = HybridEvalRetriever(index, _toy_params(), depth=10)
    base_report = evaluate(eval_set, baseline, depth=10, top_n=5)
    fused_report = evaluate(eval_set, fused, depth=10, top_n=5)
    assert base_report.query_count == fused_report.query_count == 3
    assert base_report.expected_total == fused_report.expected_total
    assert base_report.depth == fused_report.depth == 10
    assert base_report.top_n == fused_report.top_n == 5


def test_融合在查阈值这一族上优于基线():
    """结构化路是**查表**不是**猜词** —— 阈值行不必靠词面碰运气。"""
    index = _toy_index()
    eval_set = _toy_eval_set()
    baseline = BM25BaselineRetriever(index, candidate_depth=50)
    fused = HybridEvalRetriever(index, _toy_params(), depth=10)
    base_report = evaluate(eval_set, baseline, depth=10, top_n=5)
    fused_report = evaluate(eval_set, fused, depth=10, top_n=5)
    base_kinds = base_report.by_kind()
    fused_kinds = fused_report.by_kind()
    assert fused_kinds["threshold_lookup"]["hit_rate"] > base_kinds["threshold_lookup"]["hit_rate"]
    assert fused_report.hit_total > base_report.hit_total


def test_负例不进命中率分母且两侧一致():
    index = _toy_index()
    eval_set = _toy_eval_set()
    baseline = BM25BaselineRetriever(index, candidate_depth=50)
    fused = HybridEvalRetriever(index, _toy_params(), depth=10)
    base_report = evaluate(eval_set, baseline, depth=10, top_n=5)
    fused_report = evaluate(eval_set, fused, depth=10, top_n=5)
    assert base_report.expected_total == fused_report.expected_total == 2
    assert base_report.negative_query_count == 1


def test_两侧的出处覆盖率口径一致():
    """出处是在材料解析时挂上的 —— 两侧读同一份索引，出处覆盖率应当相同。"""
    index = _toy_index()
    eval_set = _toy_eval_set()
    baseline = BM25BaselineRetriever(index, candidate_depth=50)
    fused = HybridEvalRetriever(index, _toy_params(), depth=10)
    base_report = evaluate(eval_set, baseline, depth=10, top_n=5)
    fused_report = evaluate(eval_set, fused, depth=10, top_n=5)
    assert base_report.citation_coverage == fused_report.citation_coverage == 1.0


def test_两侧的评测键用同一个函数算():
    """真值侧与检索侧必须是同一个键 —— 否则命中数没有意义。"""
    index = _toy_index()
    baseline = BM25BaselineRetriever(index, candidate_depth=50)
    query = EvalQuery(query_id="q", text="危急值 上报时限", expected_keys=())
    item = baseline.retrieve(query, depth=1)[0]
    assert item.key.startswith(("policy_clause:", "threshold_row:"))


# ---------------------------------------------------------------------------
# 对比表可复算（跑真脚本）
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not CORPUS.exists() or not EVAL_SET.exists(),
    reason="需要先生成语料与评测集（scripts/fetch_data.py、scripts/build_eval_set.py）",
)
def test_对比表同一命令跑两次逐字节相同(tmp_path):
    """可复算 = 同一命令两次结果一致（两个产物都不写时间戳）。"""
    out_md = tmp_path / "compare.md"
    out_csv = tmp_path / "compare.csv"
    command = [
        sys.executable,
        str(COMPARE_SCRIPT),
        "--out",
        str(out_md),
        "--csv-out",
        str(out_csv),
    ]
    first = subprocess.run(command, capture_output=True, text=True, cwd=PROJECT_ROOT)
    assert first.returncode == 0, first.stderr
    md_bytes = out_md.read_bytes()
    csv_bytes = out_csv.read_bytes()
    second = subprocess.run(command, capture_output=True, text=True, cwd=PROJECT_ROOT)
    assert second.returncode == 0, second.stderr
    assert out_md.read_bytes() == md_bytes
    assert out_csv.read_bytes() == csv_bytes


@pytest.mark.skipif(
    not CORPUS.exists() or not EVAL_SET.exists(),
    reason="需要先生成语料与评测集（scripts/fetch_data.py、scripts/build_eval_set.py）",
)
def test_对比表含基线与融合两行且指标齐备(tmp_path):
    out_md = tmp_path / "compare.md"
    out_csv = tmp_path / "compare.csv"
    result = subprocess.run(
        [
            sys.executable,
            str(COMPARE_SCRIPT),
            "--out",
            str(out_md),
            "--csv-out",
            str(out_csv),
        ],
        capture_output=True,
        text=True,
        cwd=PROJECT_ROOT,
    )
    assert result.returncode == 0, result.stderr
    markdown = out_md.read_text(encoding="utf-8")
    assert "基线（纯 BM25 单路）" in markdown
    assert "融合引擎（结构化 + 词法 + 重排）" in markdown
    assert "命中项数" in markdown
    assert "出处覆盖率" in markdown
    csv_text = out_csv.read_text(encoding="utf-8-sig")
    lines = [line for line in csv_text.splitlines() if line.strip()]
    assert len(lines) == 3  # 表头 + 两行
    assert "参赛者" in lines[0]
    assert "命中率" in lines[0]
    assert "出处覆盖率" in lines[0]


@pytest.mark.skipif(
    not CORPUS.exists() or not EVAL_SET.exists(),
    reason="需要先生成语料与评测集（scripts/fetch_data.py、scripts/build_eval_set.py）",
)
def test_对比表数字与评测器直接算的一致(tmp_path):
    """表里的数不是另写一套算出来的 —— 必须与评测器同源。"""
    from critval.evaluate import default_material_sources, load_eval_set

    out_md = tmp_path / "compare.md"
    out_csv = tmp_path / "compare.csv"
    subprocess.run(
        [
            sys.executable,
            str(COMPARE_SCRIPT),
            "--out",
            str(out_md),
            "--csv-out",
            str(out_csv),
        ],
        capture_output=True,
        text=True,
        cwd=PROJECT_ROOT,
    )
    csv_text = out_csv.read_text(encoding="utf-8-sig")
    rows = [line.split(",") for line in csv_text.splitlines() if line.strip()]
    header, base_row, fused_row = rows[0], rows[1], rows[2]
    hit_index = header.index("命中项数")
    assert int(base_row[hit_index]) < int(fused_row[hit_index])

    # 用评测器直接复算一遍基线，数字必须与表里一致
    eval_set = load_eval_set(EVAL_SET)
    index = MaterialIndex()
    index.rebuild(default_material_sources(PROJECT_ROOT))
    baseline = BM25BaselineRetriever(index, candidate_depth=50, kb_ids=("threshold", "policy", "corpus"))
    report = evaluate(eval_set, baseline, depth=10, top_n=5)
    assert report.hit_total == int(base_row[hit_index])
