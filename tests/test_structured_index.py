"""结构化精确索引深化 + 打分归一化契约 + 两路候选合并（任务 id 28）。

覆盖四组硬要求：

* **多键组合查询** —— 键间 AND、键内 OR，返回候选 + 命中键说明（供重排加权）；
* **归一化契约拦截未归一分数** —— 分数越界时融合入口抛错，错误信息指明
  **哪一路**、**多少分**；
* **候选合并去重** —— 两路命中同一份依据时合成一条，两路的分与命中说明都留着；
* **失效版本不参与检索** —— 给定观察时刻，失效 / 未生效版本不出现在候选里。
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

from critval.index.builder import MaterialIndex, MaterialSource
from critval.index.inverted import InvertedIndex, SearchHit
from critval.index.structured import (
    IndexDocument,
    KeyMatch,
    StructuredIndex,
    documents_from_fields,
    explain_hit,
    normalize_filters,
)
from critval.index.tokenizer import tokenize
from critval.retrieve.fusion import FusionWeights, fuse
from critval.retrieve.merge import (
    LEXICAL_PATH,
    STRUCTURED_PATH,
    PathCandidate,
    merge_candidates,
    stable_primary_key,
)
from critval.retrieve.normalize import (
    NormalizationError,
    ScoreNotNormalizedError,
    bm25_max_possible,
    is_normalized,
    normalize_max_relative,
    normalize_quantile,
    normalize_ratio,
    require_normalized,
)
from critval.retrieve.paths import lexical_candidates, structured_candidates

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SAMPLE_THRESHOLD = PROJECT_ROOT / "samples" / "threshold" / "示例危急值阈值表.xlsx"
SAMPLE_POLICY = PROJECT_ROOT / "samples" / "policy" / "示例危急值报告制度.docx"


# —— 测试用的条目构造 ——


def _doc(
    doc_id: str,
    *,
    item_code: str = "",
    direction: str = "",
    unit: str = "",
    version: str = "",
    effective_from: date | None = None,
    effective_to: date | None = None,
    item_codes: tuple[str, ...] = (),
    section_title: str = "",
    kind: str = "threshold_row",
    source_name: str = "测试材料",
    source_sha256: str = "sha-test",
    citation: object = None,
) -> IndexDocument:
    return documents_from_fields(
        doc_id=doc_id,
        source_name=source_name,
        source_sha256=source_sha256,
        kind=kind,
        text=f"{item_code} {section_title} {unit}",
        fields={
            "item_code": item_code,
            "direction": direction,
            "unit": unit,
            "version": version,
            "section_title": section_title,
        },
        multi_fields={"item_codes": item_codes},
        effective_from=effective_from,
        effective_to=effective_to,
        version=version,
        citation=citation,
    )


# —— 多键组合查询 ——


def test_多键组合查询是键间且语义():
    index = StructuredIndex()
    index.add(_doc("t1", item_code="718-7", direction="both", unit="g/L", version="示例 v1.0"))
    index.add(_doc("t2", item_code="718-7", direction="low", unit="g/dL", version="示例 v1.0"))
    index.add(_doc("t3", item_code="2601-3", direction="high", unit="mmol/L", version="示例 v1.0"))

    hits = index.query(item_code="718-7", unit="g/L", version="示例 v1.0")
    assert [hit.doc_id for hit in hits] == ["t1"]
    assert hits[0].matched_keys == ("item_code", "unit", "version")


def test_三键组合任一键不符即不命中():
    index = StructuredIndex()
    index.add(_doc("t1", item_code="718-7", direction="both", unit="g/L"))
    assert index.query(item_code="718-7", direction="both", unit="mg/dL") == []
    assert index.query(item_code="718-7", direction="low", unit="g/L") == []


def test_键内多值是或语义():
    index = StructuredIndex()
    index.add(_doc("t1", item_code="718-7"))
    index.add(_doc("t2", item_code="2601-3"))
    index.add(_doc("t3", item_code="777-3"))

    hits = index.query(item_code=("718-7", "2601-3"))
    assert [hit.doc_id for hit in hits] == ["t1", "t2"]
    assert hits[0].matched_values == {"item_code": ("718-7",)}


def test_多值命中时记录具体命中值():
    index = StructuredIndex()
    index.add(_doc("p1", item_codes=("718-7", "2601-3")))
    hits = index.query(item_codes=("718-7", "777-3"))
    assert [hit.doc_id for hit in hits] == ["p1"]
    assert hits[0].key_matches == (KeyMatch(key="item_codes", values=("718-7",)),)
    assert hits[0].matched_values["item_codes"] == ("718-7",)


def test_命中键说明可读且带具体值():
    index = StructuredIndex()
    index.add(_doc("t1", item_code="718-7", unit="g/L"))
    hits = index.query(item_code="718-7", unit="g/L")
    text = explain_hit(hits[0])
    assert "item_code=718-7" in text
    assert "unit=g/L" in text


def test_查询条件归一化丢弃空值():
    wanted = normalize_filters({"a": "x", "b": "", "c": None, "d": ("y", "")})
    assert wanted == {"a": ("x",), "d": ("y",)}


def test_无过滤条件时返回全部条目():
    index = StructuredIndex()
    index.add(_doc("t1"))
    index.add(_doc("t2"))
    assert [hit.doc_id for hit in index.query()] == ["t1", "t2"]


def test_查询键顺序可列出():
    index = StructuredIndex()
    assert index.query_keys(item_code="718-7", unit="g/L") == ("item_code", "unit")


# —— 失效版本不参与检索 ——


def test_失效版本不参与检索():
    index = StructuredIndex()
    index.add(
        _doc(
            "old",
            item_code="718-7",
            version="示例 v1.0",
            effective_from=date(2026, 1, 1),
            effective_to=date(2026, 1, 31),
        )
    )
    index.add(
        _doc(
            "new",
            item_code="718-7",
            version="示例 v2.0",
            effective_from=date(2026, 2, 5),
        )
    )
    at = date(2026, 3, 1)
    hits = index.query(at=at, item_code="718-7")
    assert [hit.doc_id for hit in hits] == ["new"]
    assert index.expired_at(at) == ("old",)
    assert index.pending_at(date(2026, 1, 20)) == ("new",)


def test_不给时刻则不做时间过滤():
    index = StructuredIndex()
    index.add(
        _doc(
            "old",
            item_code="718-7",
            version="示例 v1.0",
            effective_to=date(2026, 1, 31),
        )
    )
    assert [hit.doc_id for hit in index.query(item_code="718-7")] == ["old"]


# —— 归一化契约 ——


def test_比例归一化落在零到一():
    assert normalize_ratio(3, 4) == pytest.approx(0.75)
    assert normalize_ratio(0, 4) == 0.0
    assert normalize_ratio(4, 4) == 1.0


def test_分母非正时报错():
    with pytest.raises(NormalizationError):
        normalize_ratio(1, 0)
    with pytest.raises(NormalizationError):
        normalize_ratio(1, -2)


def test_归一化后越界时报错而不是截断():
    with pytest.raises(ScoreNotNormalizedError) as excinfo:
        normalize_ratio(5, 4, path=STRUCTURED_PATH)
    assert excinfo.value.path == STRUCTURED_PATH
    assert excinfo.value.score == pytest.approx(1.25)


def test_负分同样不许悄悄归零():
    with pytest.raises(ScoreNotNormalizedError):
        normalize_ratio(-1, 4, path=LEXICAL_PATH)


def test_越界分数的报错指明是哪一路和多少分():
    with pytest.raises(ScoreNotNormalizedError) as excinfo:
        require_normalized(LEXICAL_PATH, 1.35)
    message = str(excinfo.value)
    assert "词法 BM25 路" in message
    assert "lexical" in message
    assert "1.35" in message
    assert excinfo.value.path == LEXICAL_PATH
    assert excinfo.value.score == pytest.approx(1.35)


def test_合法分数通过校验():
    assert require_normalized(STRUCTURED_PATH, 0.0) == 0.0
    assert require_normalized(STRUCTURED_PATH, 1.0) == 1.0
    assert is_normalized(0.5) is True
    assert is_normalized(1.2) is False
    assert is_normalized(float("nan")) is False


def test_分位归一化同分取平均名次且可复算():
    scores = {"a": 3.0, "b": 1.0, "c": 3.0}
    normalized = normalize_quantile(scores)
    assert normalized["b"] == pytest.approx(1 / 3)
    assert normalized["a"] == pytest.approx(normalized["c"])
    # 并列最高分取平均名次（2 与 3 的平均 = 2.5），不因并列而虚高
    assert normalized["a"] == pytest.approx(2.5 / 3)
    assert normalize_quantile({"a": 1.0, "b": 2.0})["b"] == pytest.approx(1.0)
    assert normalize_quantile(scores) == normalized  # 两次运行逐位一致


def test_相对最大值归一化最好的一条记满分():
    normalized = normalize_max_relative({"a": 4.0, "b": 2.0})
    assert normalized["a"] == pytest.approx(1.0)
    assert normalized["b"] == pytest.approx(0.5)


def test_空分数表归一化报错():
    with pytest.raises(NormalizationError):
        normalize_quantile({})
    with pytest.raises(NormalizationError):
        normalize_max_relative({})


def test_bm25理论上界不小于任何一篇的实际得分():
    index = InvertedIndex()
    index.add("d1", ["危急值", "登记"] * 5)
    index.add("d2", ["危急值"])
    tokens = tokenize("血红蛋白危急值上报时限")
    ceiling = bm25_max_possible(index, tokens)
    assert ceiling > 0
    for doc_id in index.doc_ids():
        assert index.score_document(tokens, doc_id) <= ceiling + 1e-9


# —— 两路候选构造 ——


def test_结构化路分数是命中键覆盖率():
    index = StructuredIndex()
    index.add(_doc("t1", item_code="718-7", unit="g/L"))
    hits = index.query(item_code="718-7", unit="g/L")
    candidates = structured_candidates(hits, requested_keys=2)
    assert candidates[0].score == pytest.approx(1.0)
    assert candidates[0].raw_score == 2.0
    assert candidates[0].path == STRUCTURED_PATH
    assert candidates[0].matched_keys == ("item_code", "unit")


def test_结构化路覆盖率按请求键数归一():
    index = StructuredIndex()
    index.add(_doc("t1", item_code="718-7"))
    hits = index.query(item_code=("718-7", "2601-3"))
    candidates = structured_candidates(hits, requested_keys=1)
    assert candidates[0].score == pytest.approx(1.0)
    # 请求两个键时只命中一个 → 覆盖率 0.5
    candidates = structured_candidates(hits, requested_keys=2)
    assert candidates[0].score == pytest.approx(0.5)


def test_结构化路请求键数非法时报错():
    with pytest.raises(ValueError):
        structured_candidates([], requested_keys=0)


def test_词法路分数按候选集最高原始分归一():
    hits = [
        SearchHit(doc_id="d1", score=4.0, matched_terms=("危急值",)),
        SearchHit(doc_id="d2", score=1.0, matched_terms=("危急值",)),
    ]
    candidates = lexical_candidates(hits)
    assert candidates[0].score == pytest.approx(1.0)
    assert candidates[0].raw_score == 4.0
    assert candidates[1].score == pytest.approx(0.25)
    assert candidates[1].matched_terms == ("危急值",)


def test_词法路全零分时不给满分():
    hits = [SearchHit(doc_id="d1", score=0.0)]
    assert lexical_candidates(hits)[0].score == 0.0
    assert lexical_candidates([]) == []


# —— 候选合并去重 ——


def test_两路命中同一份依据时合成一条():
    document = _doc("p1", item_codes=("718-7",))
    structured = [
        PathCandidate(
            doc_id="p1",
            path=STRUCTURED_PATH,
            score=1.0,
            raw_score=1.0,
            matched_keys=("item_codes",),
            document=document,
        )
    ]
    lexical = [
        PathCandidate(
            doc_id="p1",
            path=LEXICAL_PATH,
            score=0.6,
            raw_score=7.2,
            matched_terms=("危急值", "上报时限"),
            document=document,
        )
    ]
    merged = merge_candidates(structured, lexical)
    assert len(merged) == 1
    item = merged[0]
    assert item.structured_score == pytest.approx(1.0)
    assert item.lexical_score == pytest.approx(0.6)
    assert item.structured_raw_score == pytest.approx(1.0)
    assert item.lexical_raw_score == pytest.approx(7.2)
    assert item.matched_keys == ("item_codes",)
    assert item.matched_terms == ("危急值", "上报时限")
    assert set(item.paths) == {STRUCTURED_PATH, LEXICAL_PATH}


def test_只被一路命中的候选保留且不假装两路都命中():
    document = _doc("t1", item_code="718-7")
    merged = merge_candidates(
        [PathCandidate(doc_id="t1", path=STRUCTURED_PATH, score=1.0, document=document)],
        [],
    )
    assert len(merged) == 1
    assert merged[0].paths == (STRUCTURED_PATH,)
    assert merged[0].lexical_score is None


def test_同一路重复主键取分高的一条():
    document = _doc("t1", item_code="718-7")
    merged = merge_candidates(
        [
            PathCandidate(doc_id="t1", path=STRUCTURED_PATH, score=0.4, document=document),
            PathCandidate(doc_id="t1", path=STRUCTURED_PATH, score=0.9, document=document),
        ],
        [],
    )
    assert len(merged) == 1
    assert merged[0].structured_score == pytest.approx(0.9)


def test_不认识的路名报错():
    with pytest.raises(ValueError):
        merge_candidates(
            [PathCandidate(doc_id="t1", path="mystery", score=1.0)],
            [],
        )


def test_稳定主键按出处口径():
    from critval.models import Citation

    policy = _doc(
        "policy_clause:sha:7",
        kind="policy_clause",
        source_sha256="sha-policy",
        citation=Citation(
            source_name="示例危急值报告制度.docx",
            source_sha256="sha-policy",
            kind="policy_clause",
            paragraph_no=7,
            line_no=7,
            file_version="示例 v1.0",
        ),
    )
    assert stable_primary_key(policy) == "policy:sha-policy:7"

    threshold = _doc(
        "threshold_row:sha:2",
        kind="threshold_row",
        source_name="示例危急值阈值表.xlsx",
        citation=Citation(
            source_name="示例危急值阈值表.xlsx",
            source_sha256="sha-threshold",
            kind="threshold_row",
            line_no=2,
            file_version="示例 v1.0",
        ),
    )
    assert stable_primary_key(threshold) == "threshold:示例危急值阈值表.xlsx:2"

    plain = _doc("corpus:sha:1")
    assert stable_primary_key(plain) == "doc:corpus:sha:1"


# —— 融合入口 ——


def test_融合入口拦下未归一的分数并指明违规路():
    bad = [PathCandidate(doc_id="d1", path=LEXICAL_PATH, score=1.35)]
    with pytest.raises(ScoreNotNormalizedError) as excinfo:
        fuse([], bad)
    message = str(excinfo.value)
    assert "词法 BM25 路" in message
    assert "1.35" in message
    assert excinfo.value.path == LEXICAL_PATH


def test_融合入口拦下结构化路的越界分数():
    bad = [PathCandidate(doc_id="d1", path=STRUCTURED_PATH, score=2.0)]
    with pytest.raises(ScoreNotNormalizedError) as excinfo:
        fuse(bad, [])
    assert excinfo.value.path == STRUCTURED_PATH
    assert "结构化精确路" in str(excinfo.value)


def test_融合分按权重加权():
    document = _doc("d1", item_code="718-7")
    merged = fuse(
        [PathCandidate(doc_id="d1", path=STRUCTURED_PATH, score=1.0, document=document)],
        [PathCandidate(doc_id="d1", path=LEXICAL_PATH, score=0.5, document=document)],
        FusionWeights(structured=0.6, lexical=0.4),
    )
    assert len(merged) == 1
    assert merged[0].fused_score == pytest.approx(0.6 * 1.0 + 0.4 * 0.5)


def test_只被一路命中时按命中路的权重归一():
    document = _doc("t1", item_code="718-7")
    merged = fuse(
        [PathCandidate(doc_id="t1", path=STRUCTURED_PATH, score=0.8, document=document)],
        [],
        FusionWeights(structured=0.5, lexical=0.5),
    )
    assert merged[0].fused_score == pytest.approx(0.8)


def test_融合排序按分数降序且同分按稳定主键升序():
    def _candidate(doc_id: str, score: float) -> PathCandidate:
        return PathCandidate(doc_id=doc_id, path=STRUCTURED_PATH, score=score)

    merged = fuse(
        [_candidate("doc-b", 0.5), _candidate("doc-a", 0.5), _candidate("doc-c", 0.9)],
        [],
    )
    assert [item.doc_id for item in merged] == ["doc-c", "doc-a", "doc-b"]
    # 同参数两次运行逐位一致
    again = fuse(
        [_candidate("doc-b", 0.5), _candidate("doc-a", 0.5), _candidate("doc-c", 0.9)],
        [],
    )
    assert [item.doc_id for item in again] == [item.doc_id for item in merged]


def test_权重非法时报错并指明字段():
    with pytest.raises(ValueError) as excinfo:
        FusionWeights(structured=-0.1, lexical=1.1).validate()
    assert "不能为负" in str(excinfo.value)
    with pytest.raises(ValueError) as excinfo:
        FusionWeights(structured=0.3, lexical=0.3).validate()
    assert "之和必须为 1" in str(excinfo.value)


# —— 真实材料上的两路合并 ——


def test_真实材料上两路命中同一条款时合并成一条():
    index = MaterialIndex()
    index.rebuild(
        [
            MaterialSource.from_path(SAMPLE_THRESHOLD, kind="threshold_table", key="threshold"),
            MaterialSource.from_path(SAMPLE_POLICY, kind="policy_document", key="policy"),
        ]
    )
    structured_hits = index.structured.query(item_codes="718-7")
    lexical_hits = index.search("血红蛋白 危急值 上报时限", top_k=20)
    assert structured_hits, "样例制度里应有条款点名 718-7"

    documents = index.documents_by_id()
    structured = structured_candidates(structured_hits, requested_keys=1)
    lexical = lexical_candidates(lexical_hits, documents)

    merged = merge_candidates(structured, lexical)
    # 两路各自命中的依据集合
    structured_keys = {item.stable_key for item in structured}
    lexical_keys = {item.stable_key for item in lexical}
    assert structured_keys & lexical_keys, "样例材料上两路应当至少撞上同一条依据"

    # 合并后总条数少于两路条数之和（撞上的被合成一条）
    assert len(merged) < len(structured) + len(lexical)
    both = [item for item in merged if len(item.paths) == 2]
    assert both
    for item in both:
        assert item.structured_score is not None
        assert item.lexical_score is not None
        assert 0.0 <= item.structured_score <= 1.0
        assert 0.0 <= item.lexical_score <= 1.0


def test_真实材料上融合结果按融合分降序且可复现():
    index = MaterialIndex()
    index.rebuild(
        [
            MaterialSource.from_path(SAMPLE_THRESHOLD, kind="threshold_table", key="threshold"),
            MaterialSource.from_path(SAMPLE_POLICY, kind="policy_document", key="policy"),
        ]
    )
    documents = index.documents_by_id()

    def _run() -> list[tuple[str, float]]:
        structured = structured_candidates(
            index.structured.query(item_codes="718-7"), requested_keys=1
        )
        lexical = lexical_candidates(index.search("血红蛋白 危急值 上报时限", top_k=20), documents)
        return [
            (item.stable_key, round(item.fused_score, 6))
            for item in fuse(structured, lexical)
        ]

    first = _run()
    assert first == _run()
    assert first == sorted(first, key=lambda item: -item[1])


# —— CLI：两路合并演练 ——


def _run_cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "critval.cli", "recall-merge", *args],
        capture_output=True,
        text=True,
        cwd=str(PROJECT_ROOT),
    )


def test_CLI两路合并输出稳定主键与两路分():
    result = _run_cli(
        "--threshold",
        str(SAMPLE_THRESHOLD),
        "--policy",
        str(SAMPLE_POLICY),
        "--query",
        "血红蛋白 危急值 上报时限",
        "--item-code",
        "718-7",
    )
    assert result.returncode == 0, result.stderr
    assert "两路都命中" in result.stdout
    assert "稳定主键" in result.stdout
    assert "融合分" in result.stdout
    assert "示例危急值阈值表.xlsx · 第 2 行" in result.stdout


def test_CLI两路合并的JSON输出可被解析():
    result = _run_cli(
        "--threshold",
        str(SAMPLE_THRESHOLD),
        "--policy",
        str(SAMPLE_POLICY),
        "--json",
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["weights"] == {"structured": 0.5, "lexical": 0.5}
    assert payload["merged_candidates"] > 0
    assert payload["both_paths"] >= 1
    first = payload["results"][0]
    assert first["stable_key"]
    assert 0.0 <= first["fused_score"] <= 1.0
    assert first["locator"]


def test_CLI跳过归一化时被契约拦下并报出违规路与分值():
    result = _run_cli(
        "--threshold",
        str(SAMPLE_THRESHOLD),
        "--policy",
        str(SAMPLE_POLICY),
        "--raw-scores",
    )
    assert result.returncode == 1
    assert "归一化契约" in result.stderr
    assert "lexical" in result.stderr
    assert "词法 BM25 路" in result.stderr


def test_CLI权重非法时报参数错():
    result = _run_cli(
        "--threshold",
        str(SAMPLE_THRESHOLD),
        "--policy",
        str(SAMPLE_POLICY),
        "--structured-weight",
        "0.3",
        "--lexical-weight",
        "0.3",
    )
    assert result.returncode == 2
    assert "之和必须为 1" in result.stderr
