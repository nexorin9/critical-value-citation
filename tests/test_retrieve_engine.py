"""混合召回引擎：参数面 + 双路融合 + 阈值硬闸门 + 规则重排（任务 id 7）。

覆盖六组硬要求：

* **参数面逐项校验** —— 阈值 / 权重 / 条数 / 候选池 / 材料范围 / 重排档位，
  任一非法即报错并**指明字段**；
* **权重融合结果可复算** —— 融合分 = ``Σ(权重 × 该路分) / Σ(命中路的权重之和)``，
  拿结果里的两路分能自己算一遍；
* **阈值硬闸门截断** —— 综合分低于 ``similarity_threshold`` 的一律截断，
  且截掉多少条有计数；
* **候选池与返回条数两级截断都生效** —— ``top_k`` 限池、``top_n`` 限返回；
* **材料范围与版本绑定** —— ``kb_ids`` 限定检索范围，绑定表限定可见版本；
* **确定性** —— 同参数两次运行，排序逐位一致。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from critval.index.builder import MaterialIndex, MaterialSource
from critval.index.structured import documents_from_fields
from critval.kb.binding import BindingTable
from critval.kb.consistency import VersionBinding
from critval.models import Citation
from critval.retrieve.engine import HybridRetriever
from critval.retrieve.errors import ParamCheckError
from critval.retrieve.params import RetrievalParams
from critval.retrieve.rerank import (
    RERANK_PROFILES,
    chapter_positions,
    profile_for_id,
    rerank,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SAMPLE_THRESHOLD = PROJECT_ROOT / "samples" / "threshold" / "示例危急值阈值表.xlsx"
SAMPLE_POLICY = PROJECT_ROOT / "samples" / "policy" / "示例危急值报告制度.docx"


# —— 测试装置 ——


def _sample_index() -> MaterialIndex:
    """样例材料索引（阈值表 + 制度）。"""
    index = MaterialIndex()
    index.rebuild(
        [
            MaterialSource.from_path(SAMPLE_THRESHOLD, kind="threshold_table", key="threshold"),
            MaterialSource.from_path(SAMPLE_POLICY, kind="policy_document", key="policy"),
        ]
    )
    return index


def _policy_doc(
    doc_id: str,
    *,
    version: str,
    heading: str,
    clause_no: str,
    item_codes: tuple[str, ...] = (),
    text: str = "",
    paragraph_no: int = 1,
) -> object:
    """构造一条制度条款条目（带出处五件套）。"""
    return documents_from_fields(
        doc_id=doc_id,
        source_name="测试制度.docx",
        source_sha256=f"sha-{version}",
        kind="policy_clause",
        text=text or f"{heading} {clause_no} 危急值报告 上报时限 记录去向",
        fields={
            "clause_no": clause_no,
            "section_title": heading,
            "version": version,
            "source_name": "测试制度.docx",
            "kind": "policy_clause",
        },
        multi_fields={"item_codes": item_codes},
        version=version,
        effective_from=date(2026, 1, 1),
        citation=Citation(
            source_name="测试制度.docx",
            source_sha256=f"sha-{version}",
            kind="policy_clause",
            paragraph_no=paragraph_no,
            section_path=(heading,),
            line_no=paragraph_no,
            file_version=version,
        ),
    )


def _threshold_doc(doc_id: str, *, version: str, item_code: str = "718-7") -> object:
    """构造一条阈值行条目。"""
    return documents_from_fields(
        doc_id=doc_id,
        source_name="测试阈值表.xlsx",
        source_sha256=f"sha-th-{version}",
        kind="threshold_row",
        text=f"{item_code} 血红蛋白 g/L < 30 或 > 200",
        fields={
            "item_code": item_code,
            "item_name": "血红蛋白",
            "direction": "both",
            "unit": "g/L",
            "version": version,
            "source_name": "测试阈值表.xlsx",
            "kind": "threshold_row",
        },
        version=version,
        effective_from=date(2026, 1, 1),
        citation=Citation(
            source_name="测试阈值表.xlsx",
            source_sha256=f"sha-th-{version}",
            kind="threshold_row",
            paragraph_no=None,
            section_path=(),
            line_no=2,
            file_version=version,
        ),
    )


def _versioned_index() -> MaterialIndex:
    """两版阈值表 + 两版制度（用于版本绑定测试）。"""
    index = MaterialIndex()
    index.ingest_documents(
        "threshold",
        [
            _threshold_doc("th-v1", version="示例 v1.0"),
            _threshold_doc("th-v2", version="示例 v2.0"),
        ],
        digest="sha-th",
    )
    index.ingest_documents(
        "policy",
        [
            _policy_doc(
                "po-v1",
                version="示例 v1.0",
                heading="第一章 总则",
                clause_no="第一条",
                item_codes=("718-7",),
                paragraph_no=4,
            ),
            _policy_doc(
                "po-v2",
                version="示例 v2.0",
                heading="第一章 总则",
                clause_no="第一条",
                item_codes=("718-7",),
                paragraph_no=4,
            ),
        ],
        digest="sha-po",
    )
    return index


# —— 参数面 ——


def test_参数面默认值与源产品槽位一致():
    params = RetrievalParams()
    assert params.similarity_threshold == 0.2
    assert params.keywords_similarity_weight == 0.5
    assert params.top_n == 8
    assert params.top_k == 1024
    assert params.rerank_id == ""
    assert params.empty_response


def test_结构化权重是词法权重的互补():
    params = RetrievalParams.for_materials("threshold", keywords_similarity_weight=0.3)
    assert params.structured_weight == pytest.approx(0.7)
    weights = params.fusion_weights()
    assert weights.structured + weights.lexical == pytest.approx(1.0)


def test_材料范围不能为空():
    with pytest.raises(ParamCheckError) as error:
        RetrievalParams().check()
    assert "材料范围" in str(error.value)
    assert error.value.field_name == "[召回] 材料范围"


def test_阈值超出零到一报错并指明字段():
    with pytest.raises(ParamCheckError) as error:
        RetrievalParams.for_materials("threshold", similarity_threshold=1.5).check()
    assert "综合分阈值" in str(error.value)


def test_词法权重非法报错并指明字段():
    with pytest.raises(ParamCheckError) as error:
        RetrievalParams.for_materials("threshold", keywords_similarity_weight=-0.2).check()
    assert "BM25 词法路权重" in str(error.value)


def test_返回条数与候选池必须为正():
    with pytest.raises(ParamCheckError) as error:
        RetrievalParams.for_materials("threshold", top_n=0).check()
    assert "返回条数" in str(error.value)
    with pytest.raises(ParamCheckError) as error:
        RetrievalParams.for_materials("threshold", top_k=0).check()
    assert "候选池大小" in str(error.value)


def test_未知重排档位报错并指明字段():
    with pytest.raises(ParamCheckError) as error:
        RetrievalParams.for_materials("threshold", rerank_id="不存在的档位").check()
    assert error.value.field_name == "rerank_id"
    assert "不存在的档位" in str(error.value)


def test_材料范围键传字符串不会被逐字符拆开():
    params = RetrievalParams.for_materials("threshold")
    assert params.kb_ids == ("threshold",)


# —— 融合与闸门 ——


def test_融合结果可复算():
    """拿结果里的两路分与权重，自己把融合分算一遍。"""
    index = _sample_index()
    params = RetrievalParams.for_materials(
        "threshold", "policy", top_n=5, top_k=50, keywords_similarity_weight=0.4
    )
    result = HybridRetriever(index, params).retrieve(
        "血红蛋白危急值上报时限", {"item_code": "718-7"}
    )
    assert result.hits
    weights = params.fusion_weights()
    for hit in result.hits:
        parts = []
        if hit.structured_score is not None:
            parts.append((weights.structured, hit.structured_score))
        if hit.lexical_score is not None:
            parts.append((weights.lexical, hit.lexical_score))
        expected = sum(w * v for w, v in parts) / sum(w for w, _ in parts)
        assert hit.base_score == pytest.approx(expected, abs=1e-9)


def test_阈值硬闸门截断低于综合分的依据():
    index = _sample_index()
    loose = RetrievalParams.for_materials("threshold", "policy", top_n=50, top_k=200)
    result = HybridRetriever(index, loose).retrieve(
        "血红蛋白危急值上报时限", {"item_code": "718-7"}
    )
    assert result.gated_out > 0
    assert all(hit.base_score >= loose.similarity_threshold for hit in result.hits)

    strict = RetrievalParams.for_materials(
        "threshold", "policy", top_n=50, top_k=200, similarity_threshold=0.99
    )
    strict_result = HybridRetriever(index, strict).retrieve(
        "血红蛋白危急值上报时限", {"item_code": "718-7"}
    )
    assert len(strict_result.hits) < len(result.hits)
    assert strict_result.gated_out > result.gated_out


def test_闸门按综合分判而不是按任一路分判():
    """两路都进闸门的是综合分 —— 只被一路命中且该路分低的一样要截断。"""
    index = _sample_index()
    params = RetrievalParams.for_materials(
        "threshold", "policy", top_n=50, top_k=200, similarity_threshold=0.5
    )
    result = HybridRetriever(index, params).retrieve(
        "血红蛋白危急值上报时限", {"item_code": "718-7"}
    )
    for hit in result.hits:
        assert hit.base_score >= 0.5


def test_候选池与返回条数两级截断都生效():
    index = _sample_index()
    pooled = RetrievalParams.for_materials(
        "threshold", "policy", top_n=50, top_k=2, similarity_threshold=0.0
    )
    result = HybridRetriever(index, pooled).retrieve(
        "危急值 上报时限 通知 记录", {"item_code": "718-7"}
    )
    assert result.lexical_candidates <= 2
    assert result.pool_dropped > 0

    trimmed = RetrievalParams.for_materials(
        "threshold", "policy", top_n=2, top_k=200, similarity_threshold=0.0
    )
    trimmed_result = HybridRetriever(index, trimmed).retrieve(
        "危急值 上报时限 通知 记录", {"item_code": "718-7"}
    )
    assert len(trimmed_result.hits) == 2


def test_同参数两次运行排序完全一致():
    index = _sample_index()
    params = RetrievalParams.for_materials("threshold", "policy", top_n=8, top_k=50)
    retriever = HybridRetriever(index, params)
    query = "血红蛋白危急值上报时限 通知对象 记录去向"
    first = retriever.retrieve(query, {"item_code": "718-7"})
    second = retriever.retrieve(query, {"item_code": "718-7"})
    assert [(h.stable_key, round(h.score, 9)) for h in first.hits] == [
        (h.stable_key, round(h.score, 9)) for h in second.hits
    ]


# —— 可见范围 ——


def test_材料范围限定可见条目():
    index = _sample_index()
    only_threshold = RetrievalParams.for_materials("threshold", top_n=20, top_k=50)
    result = HybridRetriever(index, only_threshold).retrieve(
        "危急值 上报时限", {"item_code": "718-7"}
    )
    assert result.hits
    assert all(hit.kind == "threshold_row" for hit in result.hits)


def test_版本绑定只放行被绑版本():
    index = _versioned_index()
    bindings = BindingTable(
        bindings=(
            VersionBinding(
                item_code="718-7",
                threshold_version="示例 v1.0",
                policy_version="示例 v1.0",
                note="检验科确认",
            ),
        )
    )
    params = RetrievalParams.for_materials("threshold", "policy", top_n=20, top_k=50)
    result = HybridRetriever(index, params, bindings=bindings).retrieve(
        "危急值 上报时限", {"item_code": "718-7"}
    )
    versions = {
        (hit.citation.file_version if hit.citation else "") for hit in result.hits
    }
    assert versions == {"示例 v1.0"}


def test_没有绑定时不限制版本():
    index = _versioned_index()
    params = RetrievalParams.for_materials("threshold", "policy", top_n=20, top_k=50)
    result = HybridRetriever(index, params).retrieve(
        "危急值 上报时限", {"item_code": "718-7"}
    )
    versions = {
        (hit.citation.file_version if hit.citation else "") for hit in result.hits
    }
    assert "示例 v2.0" in versions


def test_失效版本不参与召回():
    index = _versioned_index()
    params = RetrievalParams.for_materials("threshold", "policy", top_n=20, top_k=50)
    result = HybridRetriever(index, params, at=date(2025, 6, 1)).retrieve(
        "危急值 上报时限", {"item_code": "718-7"}
    )
    assert result.hits == ()


# —— 规则重排 ——


def test_重排四因子都参与打分():
    index = _sample_index()
    params = RetrievalParams.for_materials("threshold", "policy", top_n=8, top_k=50)
    result = HybridRetriever(index, params).retrieve(
        "血红蛋白危急值上报时限", {"item_code": "718-7"}
    )
    for hit in result.hits:
        assert set(hit.factors) == {
            "field_hit",
            "section_position",
            "term_coverage",
            "clause_type_factor",
        }
        assert 0.0 <= hit.factors["field_hit"] <= 1.0
        assert 0.0 <= hit.factors["section_position"] <= 1.0
        assert 0.0 <= hit.factors["term_coverage"] <= 1.0
        assert 0.0 < hit.factors["clause_type_factor"] <= 1.0


def test_结构化满分命中靠字段权重排到前面():
    """阈值行是结构化满分命中 —— 规则重排要把它顶到近似条款前面。"""
    index = _sample_index()
    params = RetrievalParams.for_materials("threshold", "policy", top_n=5, top_k=50)
    result = HybridRetriever(index, params).retrieve(
        "血红蛋白危急值上报时限", {"item_code": "718-7"}
    )
    first = result.hits[0]
    assert first.kind == "threshold_row"
    assert first.factors["field_hit"] == pytest.approx(1.0)
    assert first.score > result.hits[1].score


def test_最终分等于融合分加类型因子与三个因子加成():
    """最终分 = 融合分 + 类型因子 ×（字段加成 + 章节加成）+ 覆盖率加成。"""
    index = _sample_index()
    params = RetrievalParams.for_materials("threshold", "policy", top_n=8, top_k=50)
    profile = RERANK_PROFILES["default"]
    result = HybridRetriever(index, params).retrieve(
        "血红蛋白危急值上报时限", {"item_code": "718-7"}
    )
    for hit in result.hits:
        expected = (
            hit.base_score
            + hit.factors["clause_type_factor"]
            * (
                profile.field_gain * hit.factors["field_hit"]
                + profile.section_gain * hit.factors["section_position"]
            )
            + profile.coverage_gain * hit.factors["term_coverage"]
        )
        assert hit.score == pytest.approx(expected, abs=1e-9)


def test_重排同分保持稳定序():
    """同分的依据按稳定主键升序 —— 两次运行逐位一致。"""
    from critval.retrieve.merge import MergedCandidate

    candidates = [
        MergedCandidate(stable_key="b", doc_id="d2", structured_score=0.5, fused_score=0.5),
        MergedCandidate(stable_key="a", doc_id="d1", structured_score=0.5, fused_score=0.5),
        MergedCandidate(stable_key="c", doc_id="d3", structured_score=0.5, fused_score=0.5),
    ]
    first = rerank(candidates, ("危急值",))
    second = rerank(list(reversed(candidates)), ("危急值",))
    assert [item.stable_key for item in first] == ["a", "b", "c"]
    assert [item.stable_key for item in second] == ["a", "b", "c"]


def test_章节位置权重按材料内章节顺序归一():
    index = _versioned_index()
    positions = chapter_positions(index.documents_by_id().values())
    assert positions[("sha-示例 v1.0", "第一章 总则")] == 0.5


def test_未知重排档位在引擎里同样报错():
    index = _sample_index()
    params = RetrievalParams.for_materials("threshold", "policy")
    with pytest.raises(ParamCheckError) as error:
        HybridRetriever(index, params, profile=profile_for_id("不存在的档位"))
    assert error.value.field_name == "rerank_id"


# —— 空召回 ——


def test_召回为空时给出兜底话术且不编依据():
    index = _sample_index()
    params = RetrievalParams.for_materials("threshold", "policy", top_n=8, top_k=50)
    result = HybridRetriever(index, params).retrieve(
        "完全不相关的查询词 汽修车间", {"item_code": "不存在的项目码"}
    )
    assert result.is_empty
    assert result.empty_response
    assert result.citations == ()
