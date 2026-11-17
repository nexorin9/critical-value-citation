"""规则重排深化：条款类型加权与稳定排序（任务 id 30）。

覆盖四组硬要求：

* **类型标签由制度条款解析字段推导** —— 对象 / 时限 / 去向 / 总则 / 其他，
  不是关键词猜测；
* **类型加权生效** —— 时限与去向条款优先于总则条款，最终分可自己复算；
* **权重表非法即报错并指明字段** —— 负数 / 未知类型 / 全零 / 非数值；
* **稳定序可复现** —— 同参数两次运行排序逐位一致；同分并列按
  ``source_sha256 + 段落序号`` 升序。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from critval.index.builder import MaterialIndex, MaterialSource
from critval.index.structured import documents_from_fields
from critval.models import Citation
from critval.retrieve.clause_type import (
    CLAUSE_TYPE_DEADLINE,
    CLAUSE_TYPE_DESTINATION,
    CLAUSE_TYPE_GENERAL,
    CLAUSE_TYPE_OTHER,
    CLAUSE_TYPE_TARGET,
    DEFAULT_CLAUSE_TYPE_WEIGHTS,
    derive_clause_type,
    normalize_clause_type_weights,
)
from critval.retrieve.engine import HybridRetriever
from critval.retrieve.errors import ParamCheckError
from critval.retrieve.merge import MergedCandidate
from critval.retrieve.params import RetrievalParams
from critval.retrieve.rerank import (
    RERANK_PROFILES,
    RerankProfile,
    chapter_positions,
    rerank,
    stable_sort_key,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SAMPLE_THRESHOLD = PROJECT_ROOT / "samples" / "threshold" / "示例危急值阈值表.xlsx"
SAMPLE_POLICY = PROJECT_ROOT / "samples" / "policy" / "示例危急值报告制度.docx"


# —— 测试装置 ——


def _clause_doc(
    doc_id: str,
    *,
    notify_target: str = "",
    deadline: str = "",
    record_destination: str = "",
    paragraph_no: int = 1,
    source_sha256: str = "sha-policy",
    version: str = "示例 v1.0",
) -> object:
    """构造一条制度条款条目（三项事实按需给）。"""
    return documents_from_fields(
        doc_id=doc_id,
        source_name="测试制度.docx",
        source_sha256=source_sha256,
        kind="policy_clause",
        text="危急值报告 通知对象 上报时限 记录去向",
        fields={
            "clause_no": f"第{paragraph_no}条",
            "section_title": "第三章 报告流程与通知时限",
            "notify_target": notify_target,
            "deadline": deadline,
            "record_destination": record_destination,
            "version": version,
            "source_name": "测试制度.docx",
            "kind": "policy_clause",
        },
        version=version,
        citation=Citation(
            source_name="测试制度.docx",
            source_sha256=source_sha256,
            kind="policy_clause",
            paragraph_no=paragraph_no,
            section_path=("第三章 报告流程与通知时限",),
            line_no=paragraph_no,
            file_version=version,
        ),
    )


def _threshold_doc(doc_id: str, *, line_no: int = 2) -> object:
    """构造一条阈值行条目。"""
    return documents_from_fields(
        doc_id=doc_id,
        source_name="测试阈值表.xlsx",
        source_sha256="sha-threshold",
        kind="threshold_row",
        text="718-7 血红蛋白 g/L < 30 或 > 200",
        fields={
            "item_code": "718-7",
            "item_name": "血红蛋白",
            "direction": "both",
            "unit": "g/L",
            "version": "示例 v1.0",
            "source_name": "测试阈值表.xlsx",
            "kind": "threshold_row",
        },
        version="示例 v1.0",
        citation=Citation(
            source_name="测试阈值表.xlsx",
            source_sha256="sha-threshold",
            kind="threshold_row",
            paragraph_no=None,
            section_path=(),
            line_no=line_no,
            file_version="示例 v1.0",
        ),
    )


def _candidate(
    doc_id: str,
    document: object,
    *,
    fused_score: float = 0.5,
    matched_keys: tuple[str, ...] = (),
    matched_terms: tuple[str, ...] = (),
) -> MergedCandidate:
    """构造一个合并后的候选。"""
    stable_key = f"policy:{document.source_sha256}:{document.citation.paragraph_no}"
    return MergedCandidate(
        stable_key=stable_key,
        doc_id=doc_id,
        structured_score=0.8,
        lexical_score=0.4,
        fused_score=fused_score,
        matched_keys=matched_keys,
        matched_terms=matched_terms,
        paths=("structured", "lexical"),
        document=document,
    )


# —— 类型标签推导 ——


def test_条款写了上报时限则类型为时限():
    document = _clause_doc("c1", deadline="10 分钟内")
    assert derive_clause_type(document) == CLAUSE_TYPE_DEADLINE


def test_条款写了通知对象则类型为通知对象():
    document = _clause_doc("c1", notify_target="检验科值班技师")
    assert derive_clause_type(document) == CLAUSE_TYPE_TARGET


def test_条款写了记录去向则类型为记录去向():
    document = _clause_doc("c1", record_destination="危急值登记系统")
    assert derive_clause_type(document) == CLAUSE_TYPE_DESTINATION


def test_三项事实齐全时主类型按时限优先():
    """一条条款写全三项 —— 主类型取「时限」（危急值处置里最紧的一问）。"""
    document = _clause_doc(
        "c1",
        notify_target="检验科值班技师",
        deadline="10 分钟内",
        record_destination="危急值登记系统",
    )
    assert derive_clause_type(document) == CLAUSE_TYPE_DEADLINE


def test_制度条款没写三项事实则为总则():
    document = _clause_doc("c1")
    assert derive_clause_type(document) == CLAUSE_TYPE_GENERAL


def test_阈值行与语料行归为其他():
    assert derive_clause_type(_threshold_doc("t1")) == CLAUSE_TYPE_OTHER
    assert derive_clause_type(None) == CLAUSE_TYPE_OTHER


def test_空值事实不算写明():
    """事实字段是空串 —— 不算"写了"，否则总则条款会被误判成操作细则。"""
    document = _clause_doc("c1", deadline="   ")
    assert derive_clause_type(document) == CLAUSE_TYPE_GENERAL


# —— 类型加权生效 ——


def test_类型因子乘在字段加成与章节加成之上():
    profile = RERANK_PROFILES["default"]
    document = _clause_doc("c1", deadline="10 分钟内")
    candidate = _candidate("c1", document, matched_keys=("item_codes",))
    positions = {("sha-policy", "第三章 报告流程与通知时限"): 1.0}

    item = rerank([candidate], ("危急值",), profile, positions)[0]

    assert item.clause_type == CLAUSE_TYPE_DEADLINE
    assert item.factors.clause_type_factor == pytest.approx(1.0)
    expected = (
        candidate.fused_score
        + item.factors.clause_type_factor
        * (
            profile.field_gain * item.factors.field_hit
            + profile.section_gain * item.factors.section_position
        )
        + profile.coverage_gain * item.factors.term_coverage
    )
    assert item.final_score == pytest.approx(expected, abs=1e-12)


def test_总则条款的类型因子低于时限条款():
    """总则不直接回答值班人的任何一问 —— 它的"依据分量"打折。"""
    profile = RERANK_PROFILES["default"]
    action = _clause_doc("c-action", deadline="10 分钟内", paragraph_no=7)
    general = _clause_doc("c-general", paragraph_no=8)

    action_item = rerank([_candidate("c-action", action)], ("危急值",), profile)[0]
    general_item = rerank([_candidate("c-general", general)], ("危急值",), profile)[0]

    assert action_item.factors.clause_type_factor == pytest.approx(1.0)
    assert general_item.factors.clause_type_factor == pytest.approx(
        DEFAULT_CLAUSE_TYPE_WEIGHTS[CLAUSE_TYPE_GENERAL]
    )
    assert general_item.factors.clause_type_factor < action_item.factors.clause_type_factor


def test_总则条款因为打折排到操作细则条款之后():
    """同分同因子时，时限条款排在总则条款前面。"""
    profile = RERANK_PROFILES["default"]
    action = _clause_doc("c-action", deadline="10 分钟内", paragraph_no=7)
    general = _clause_doc("c-general", paragraph_no=8)
    candidates = [
        _candidate("c-general", general, matched_keys=("item_codes",)),
        _candidate("c-action", action, matched_keys=("item_codes",)),
    ]

    ranked = rerank(candidates, ("危急值",), profile)

    assert [item.doc_id for item in ranked] == ["c-action", "c-general"]
    assert ranked[0].final_score > ranked[1].final_score


def test_阈值行不因不是制度条款而打折():
    """阈值依据行是判定依据本身 —— 不能因为"不是制度条款"就被压下去。"""
    profile = RERANK_PROFILES["default"]
    row = _threshold_doc("t1")
    item = rerank([_candidate("t1", row, matched_keys=("item_code",))], ("危急值",), profile)[0]
    assert item.clause_type == CLAUSE_TYPE_OTHER
    assert item.factors.clause_type_factor == pytest.approx(1.0)


def test_类型权重表可配置且按表内最大值归一():
    table = normalize_clause_type_weights({"deadline": 10.0, "general": 4.0})
    assert table[CLAUSE_TYPE_DEADLINE] == pytest.approx(1.0)
    assert table[CLAUSE_TYPE_GENERAL] == pytest.approx(0.4)
    # 表里没写到的类型沿用默认份额。
    assert table[CLAUSE_TYPE_DESTINATION] == pytest.approx(
        DEFAULT_CLAUSE_TYPE_WEIGHTS[CLAUSE_TYPE_DESTINATION]
    )


def test_自定义类型权重表能把通知对象条款提到前面():
    profile = RerankProfile(
        name="custom",
        clause_type_weights={"target": 1.0, "deadline": 0.5, "general": 0.2},
    )
    target_doc = _clause_doc("c-target", notify_target="检验科值班技师", paragraph_no=3)
    deadline_doc = _clause_doc("c-deadline", deadline="10 分钟内", paragraph_no=4)
    candidates = [
        _candidate("c-deadline", deadline_doc, matched_keys=("item_codes",)),
        _candidate("c-target", target_doc, matched_keys=("item_codes",)),
    ]
    ranked = rerank(candidates, ("危急值",), profile)
    assert [item.doc_id for item in ranked] == ["c-target", "c-deadline"]


def test_类型加权强度为零时类型不影响排序():
    profile = RerankProfile(name="off", clause_type_gain=0.0)
    action = _clause_doc("c-action", deadline="10 分钟内", paragraph_no=7)
    general = _clause_doc("c-general", paragraph_no=8)
    candidates = [
        _candidate("c-general", general, matched_keys=("item_codes",)),
        _candidate("c-action", action, matched_keys=("item_codes",)),
    ]
    ranked = rerank(candidates, ("危急值",), profile)
    assert all(item.factors.clause_type_factor == pytest.approx(1.0) for item in ranked)
    # 强度为 0 → 两条同分，稳定主键决定顺序（段落号小的在前）。
    assert [item.doc_id for item in ranked] == ["c-action", "c-general"]


def test_操作细则档位把总则压得更狠():
    strict = RERANK_PROFILES["action_first"]
    general = _clause_doc("c-general", paragraph_no=8)
    item = rerank([_candidate("c-general", general)], ("危急值",), strict)[0]
    assert item.factors.clause_type_factor == pytest.approx(0.4)


# —— 权重表非法即报错并指明字段 ——


def test_类型权重为负报错并指明字段():
    profile = RerankProfile(name="bad", clause_type_weights={"deadline": -0.5})
    with pytest.raises(ParamCheckError) as error:
        profile.validate()
    assert error.value.field_name == "clause_type_weights.deadline"
    assert "不能为负" in str(error.value)


def test_类型权重表里有未知类型报错并列出可用类型():
    profile = RerankProfile(name="bad", clause_type_weights={"不存在的类型": 1.0})
    with pytest.raises(ParamCheckError) as error:
        profile.validate()
    assert error.value.field_name == "clause_type_weights.不存在的类型"
    assert "未知类型" in str(error.value)
    assert "deadline" in str(error.value)


def test_类型权重表全为零报错():
    profile = RerankProfile(name="bad", clause_type_weights={"deadline": 0.0, "general": 0.0})
    with pytest.raises(ParamCheckError) as error:
        profile.validate()
    assert error.value.field_name == "clause_type_weights"
    assert "全为 0" in str(error.value)


def test_类型权重非数值报错():
    profile = RerankProfile(name="bad", clause_type_weights={"deadline": "很重"})
    with pytest.raises(ParamCheckError) as error:
        profile.validate()
    assert error.value.field_name == "clause_type_weights.deadline"


def test_类型加权强度越界报错并指明字段():
    for gain in (-0.1, 1.5):
        profile = RerankProfile(name="bad", clause_type_gain=gain)
        with pytest.raises(ParamCheckError) as error:
            profile.validate()
        assert error.value.field_name == "clause_type_gain"


def test_非法权重表在重排入口同样拦得住():
    profile = RerankProfile(name="bad", clause_type_weights={"deadline": -1.0})
    with pytest.raises(ParamCheckError):
        rerank([], ("危急值",), profile)


# —— 稳定序 ——


def test_同参数两次运行排序逐位一致():
    candidates = [
        _candidate("c-1", _clause_doc("c-1", deadline="10 分钟内", paragraph_no=4)),
        _candidate("c-2", _clause_doc("c-2", paragraph_no=9)),
        _candidate("c-3", _clause_doc("c-3", deadline="20 分钟内", paragraph_no=6)),
    ]
    first = rerank(candidates, ("危急值",))
    second = rerank(list(candidates), ("危急值",))
    assert [(item.doc_id, round(item.final_score, 12)) for item in first] == [
        (item.doc_id, round(item.final_score, 12)) for item in second
    ]


def test_入参顺序变了排序不变():
    candidates = [
        _candidate("c-1", _clause_doc("c-1", deadline="10 分钟内", paragraph_no=4)),
        _candidate("c-2", _clause_doc("c-2", paragraph_no=9)),
        _candidate("c-3", _clause_doc("c-3", deadline="20 分钟内", paragraph_no=6)),
    ]
    forward = rerank(candidates, ("危急值",))
    backward = rerank(list(reversed(candidates)), ("危急值",))
    assert [item.doc_id for item in forward] == [item.doc_id for item in backward]


def test_同分并列按材料指纹与段落序号升序():
    """同分时先看材料指纹、再看段落序号 —— 与入参顺序无关。"""
    later = _candidate("c-late", _clause_doc("c-late", paragraph_no=20))
    earlier = _candidate("c-early", _clause_doc("c-early", paragraph_no=3))
    other_source = _candidate(
        "c-other", _clause_doc("c-other", paragraph_no=1, source_sha256="sha-aaa")
    )

    ranked = rerank([later, other_source, earlier], ("危急值",))

    assert [item.doc_id for item in ranked] == ["c-other", "c-early", "c-late"]
    assert stable_sort_key(earlier) == ("sha-policy", 3, earlier.stable_key)


def test_稳定主键在无出处条目上退化为条目键():
    candidate = MergedCandidate(stable_key="a", doc_id="d1", fused_score=0.5)
    assert stable_sort_key(candidate) == ("", 0, "a")


# —— 端到端（真实样例材料） ——


def _sample_index() -> MaterialIndex:
    index = MaterialIndex()
    index.rebuild(
        [
            MaterialSource.from_path(SAMPLE_THRESHOLD, kind="threshold_table", key="threshold"),
            MaterialSource.from_path(SAMPLE_POLICY, kind="policy_document", key="policy"),
        ]
    )
    return index


def test_真实样例材料上召回结果带条款类型():
    index = _sample_index()
    params = RetrievalParams.for_materials("threshold", "policy", top_n=8, top_k=50)
    result = HybridRetriever(index, params).retrieve(
        "血红蛋白危急值上报时限", {"item_code": "718-7"}
    )
    assert result.hits
    for hit in result.hits:
        assert hit.clause_type
        assert hit.clause_type_label
        assert 0.0 < hit.factors["clause_type_factor"] <= 1.0
    kinds = {hit.kind for hit in result.hits}
    assert "threshold_row" in kinds


def test_真实样例制度里写了三项事实的条款类型为时限():
    """样例制度里 5 条条款写全了三项事实 —— 重排层能认出它们是操作细则。"""
    index = _sample_index()
    documents = index.documents_by_id()
    types = [
        derive_clause_type(document)
        for document in documents.values()
        if document.kind == "policy_clause"
    ]
    assert types.count(CLAUSE_TYPE_DEADLINE) == 5
    assert types.count(CLAUSE_TYPE_GENERAL) == 16


def test_真实样例材料上总则条款排在操作细则条款之后():
    index = _sample_index()
    params = RetrievalParams.for_materials("threshold", "policy", top_n=20, top_k=100)
    result = HybridRetriever(index, params).retrieve(
        "血红蛋白危急值上报时限 通知对象 记录去向", {"item_code": "718-7"}
    )
    by_type = {hit.clause_type: hit.rank for hit in result.hits}
    if CLAUSE_TYPE_DEADLINE in by_type and CLAUSE_TYPE_GENERAL in by_type:
        assert by_type[CLAUSE_TYPE_DEADLINE] < by_type[CLAUSE_TYPE_GENERAL]


def test_章节位置权重仍按材料内章节顺序归一():
    index = _sample_index()
    positions = chapter_positions(index.documents_by_id().values())
    assert positions
    assert all(0.0 <= value <= 1.0 for value in positions.values())
