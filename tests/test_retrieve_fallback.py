"""召回分级降级：原样 → 关键词扩展 → 章节级兜底 → 转人工（任务 id 32）。

覆盖四组硬要求：

* **四级降级各有一条路径** —— 第 0/1/2/3 级各有构造用例；
* **降级后引用带层级** —— 依据的 ``level`` 写在引用上，卡片按它标
  「依据来自第几级降级」；
* **第 3 级零引用** —— 降级用尽时不产出任何引用，事件转
  ``transferred_to_human`` 并写审计；
* **不存在「无依据却产出引用」的路径** —— 遍历所有分支，每条引用都必须
  带出处（材料指纹 + 行号）。

## 装置说明

**召回范围与术语表是两回事**：召回只在「制度」材料里找条款（阈值依据由判定
环节拿着），但**别名表来自全部材料** —— 阈值表里的「项目码 ↔ 项目名」正是
值班人问法与制度写法的桥。所以装置里阈值表常常在索引里、却不在召回范围里。
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from critval.card.builder import build_card
from critval.card.provenance import RETRIEVAL_LEVELS
from critval.index.builder import MaterialIndex, MaterialSource
from critval.index.structured import documents_from_fields
from critval.models import CardProvenance, Citation, CriticalEvent, PolicyClause
from critval.retrieve.citation import (
    AUDIT_EMPTY_RETRIEVAL,
    HUMAN_REVIEW_STATUS,
    apply_retrieval_outcome,
    resolve_retrieval,
)
from critval.retrieve.engine import HybridRetriever
from critval.retrieve.fallback import (
    GradedRetriever,
    build_alias_table,
    fallback_provenance,
)
from critval.retrieve.levels import (
    LEVEL_CHAPTER,
    LEVEL_EXACT,
    LEVEL_EXPANDED,
    LEVEL_NONE,
)
from critval.retrieve.params import RetrievalParams

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SAMPLE_THRESHOLD = PROJECT_ROOT / "samples" / "threshold" / "示例危急值阈值表.xlsx"
SAMPLE_POLICY = PROJECT_ROOT / "samples" / "policy" / "示例危急值报告制度.docx"

#: 一句与所有条款正文都不重叠的查询（用来制造"原样查不到"）。
MISS_QUERY = "标本接收窗口"


# —— 测试装置 ——


def _threshold_doc(
    doc_id: str,
    *,
    item_code: str = "718-7",
    item_name: str = "血红蛋白",
    text: str = "",
) -> object:
    """构造一条阈值行条目。"""
    return documents_from_fields(
        doc_id=doc_id,
        source_name="测试阈值表.xlsx",
        source_sha256="sha-threshold",
        kind="threshold_row",
        text=text or f"{item_code} {item_name} g/L < 30 或 > 200",
        fields={
            "item_code": item_code,
            "item_name": item_name,
            "direction": "both",
            "unit": "g/L",
            "version": "示例 v1.0",
            "source_name": "测试阈值表.xlsx",
            "kind": "threshold_row",
        },
        version="示例 v1.0",
        effective_from=date(2026, 1, 1),
        citation=Citation(
            source_name="测试阈值表.xlsx",
            source_sha256="sha-threshold",
            kind="threshold_row",
            line_no=2,
            file_version="示例 v1.0",
        ),
    )


def _policy_doc(
    doc_id: str,
    *,
    heading: str,
    clause_no: str,
    text: str,
    paragraph_no: int,
    item_codes: tuple[str, ...] = (),
    item_names: tuple[str, ...] = (),
    with_citation: bool = True,
) -> object:
    """构造一条制度条款条目。"""
    citation = (
        Citation(
            source_name="测试制度.docx",
            source_sha256="sha-policy",
            kind="policy_clause",
            paragraph_no=paragraph_no,
            section_path=(heading,),
            line_no=paragraph_no,
            file_version="示例 v1.0",
        )
        if with_citation
        else None
    )
    return documents_from_fields(
        doc_id=doc_id,
        source_name="测试制度.docx",
        source_sha256="sha-policy",
        kind="policy_clause",
        text=text,
        fields={
            "clause_no": clause_no,
            "section_title": heading,
            "notify_target": "检验科值班技师" if clause_no == "第七条" else "",
            "deadline": "10 分钟内" if clause_no == "第七条" else "",
            "record_destination": "危急值登记系统" if clause_no == "第七条" else "",
            "version": "示例 v1.0",
            "source_name": "测试制度.docx",
            "kind": "policy_clause",
        },
        multi_fields={"item_codes": item_codes, "item_names": item_names},
        version="示例 v1.0",
        effective_from=date(2026, 1, 1),
        citation=citation,
    )


def _index(documents: list) -> MaterialIndex:
    """把构造好的条目按材料范围键登记进索引。"""
    index = MaterialIndex()
    index.ingest_documents("policy", [doc for doc in documents if doc.kind == "policy_clause"])
    index.ingest_documents(
        "threshold", [doc for doc in documents if doc.kind == "threshold_row"]
    )
    return index


def _retriever(index: MaterialIndex, **overrides) -> HybridRetriever:
    """默认只在「制度」材料里召回（阈值依据由判定环节拿着）。"""
    params = RetrievalParams.for_materials("policy", **overrides)
    return HybridRetriever(index, params)


# —— 第 0 级：原样查询 ——


def test_原样查得到就停在第0级():
    """问法与制度条文对得上 → 不降级，引用层级 0。"""
    index = _index(
        [
            _policy_doc(
                "policy:19",
                heading="第三章 报告流程与通知时限",
                clause_no="第七条",
                text="第七条 危急值应在 10 分钟内通知检验科值班技师，并记入危急值登记系统",
                paragraph_no=19,
            ),
        ]
    )
    result = GradedRetriever(_retriever(index)).retrieve_graded(
        "危急值 10 分钟内 通知 检验科"
    )

    assert result.level == LEVEL_EXACT
    assert result.hits
    assert [attempt.level for attempt in result.fallback_attempts] == [LEVEL_EXACT]
    assert all(hit.level == LEVEL_EXACT for hit in result.hits)


def test_第0级的引用带出处():
    """每条引用都要有材料指纹与可读行号（无出处进不了引用）。"""
    index = _index(
        [
            _policy_doc(
                "policy:19",
                heading="第三章 报告流程与通知时限",
                clause_no="第七条",
                text="第七条 危急值应在 10 分钟内通知检验科值班技师",
                paragraph_no=19,
            ),
        ]
    )
    result = GradedRetriever(_retriever(index)).retrieve_graded("危急值 10 分钟内 通知")

    citations = resolve_retrieval(result).citations
    assert citations
    for citation in citations:
        assert citation.source_sha256
        assert citation.line_no is not None
        assert citation.level == LEVEL_EXACT


# —— 第 1 级：关键词扩展 ——


def test_别名表从材料自动抽取():
    """别名表不是手写词表：项目码 ↔ 项目名 由材料自己抽出来。"""
    index = _index(
        [
            _threshold_doc("threshold:2", item_code="718-7", item_name="血红蛋白"),
        ]
    )
    alias = build_alias_table(index.documents_by_id().values())

    assert "718-7" in alias.mapping
    assert "血红蛋白" in alias.mapping["718-7"]
    assert "718-7" in alias.mapping["血红蛋白"]


def test_别名表也抽制度章节标题与项目码():
    """制度里点名了项目 → 章节标题与项目码互为别名。"""
    index = _index(
        [
            _policy_doc(
                "policy:10",
                heading="第二章 危急值项目与阈值",
                clause_no="第六条",
                text="第六条 血红蛋白（718-7）的参考区间为 115-150，单位 g/L",
                paragraph_no=10,
                item_codes=("718-7",),
                item_names=("血红蛋白",),
            ),
        ]
    )
    alias = build_alias_table(index.documents_by_id().values())

    assert "718-7" in alias.mapping["血红蛋白"]
    assert "第二章 危急值项目与阈值" in alias.mapping


def test_第1级_关键词扩展后查到制度条款():
    """值班人问项目名、制度正文只写项目码 → 扩展后才查到，引用标第 1 级。"""
    index = _index(
        [
            # 阈值表提供「718-7 ↔ 血红蛋白」这条别名（不在召回范围里）。
            _threshold_doc("threshold:2", item_code="718-7", item_name="血红蛋白"),
            _policy_doc(
                "policy:10",
                heading="第二章 危急值项目与阈值",
                clause_no="第六条",
                # 正文里只有项目码，没有中文项目名 —— 原样查不到。
                text="第六条 718-7 数值下限 30 g/L",
                paragraph_no=10,
                item_codes=("718-7",),
            ),
        ]
    )
    graded = GradedRetriever(_retriever(index))
    plain = graded.retriever.retrieve("血红蛋白 危急值")

    # 原样查不到制度条款（阈值行不在召回范围里）。
    assert plain.hits == ()

    result = graded.retrieve_graded("血红蛋白 危急值")

    assert result.level == LEVEL_EXPANDED
    assert [attempt.level for attempt in result.fallback_attempts] == [
        LEVEL_EXACT,
        LEVEL_EXPANDED,
    ]
    clause_hits = [hit for hit in result.hits if hit.kind == "policy_clause"]
    assert clause_hits
    assert all(hit.level == LEVEL_EXPANDED for hit in clause_hits)
    assert all(hit.citation.level == LEVEL_EXPANDED for hit in clause_hits)
    # 扩展词记在留痕里，值班人能看出"换了个说法"。
    expanded_note = [
        attempt.note for attempt in result.fallback_attempts if attempt.level == LEVEL_EXPANDED
    ][0]
    assert "关键词扩展" in expanded_note
    assert "718-7" in expanded_note


def test_第1级_结构化命中的阈值行不算降级():
    """查表命中的阈值行不是降级来的 —— 即使整次召回靠扩展才凑到制度依据。"""
    index = _index(
        [
            _threshold_doc("threshold:2", item_code="718-7", item_name="血红蛋白"),
            _policy_doc(
                "policy:10",
                heading="第二章 危急值项目与阈值",
                clause_no="第六条",
                text="第六条 718-7 数值下限 30 g/L",
                paragraph_no=10,
                item_codes=("718-7",),
            ),
        ]
    )
    params = RetrievalParams.for_materials(
        "threshold", "policy", similarity_threshold=0.05
    )
    result = GradedRetriever(HybridRetriever(index, params)).retrieve_graded(
        "血红蛋白 危急值", {"item_code": "718-7"}
    )

    assert result.level == LEVEL_EXPANDED
    threshold_hits = [hit for hit in result.hits if hit.kind == "threshold_row"]
    assert threshold_hits
    assert all(hit.level == LEVEL_EXACT for hit in threshold_hits)
    clause_hits = [hit for hit in result.hits if hit.kind == "policy_clause"]
    assert clause_hits
    assert all(hit.level == LEVEL_EXPANDED for hit in clause_hits)


# —— 第 2 级：章节级兜底 ——


def test_第2级_按项目码定位章节并取该章节全部条款():
    """制度正文点名了项目码 → 章节级兜底找得到（取该章节全部条款）。"""
    index = _index(
        [
            _policy_doc(
                "policy:10",
                heading="第二章 危急值项目与阈值",
                clause_no="第六条",
                text="第六条 718-7 数值下限 30 g/L",
                paragraph_no=10,
                item_codes=("718-7",),
            ),
            _policy_doc(
                "policy:11",
                heading="第二章 危急值项目与阈值",
                clause_no="第七条之一",
                text="第七条之一 718-7 的危急上限 200 g/L",
                paragraph_no=11,
            ),
            _policy_doc(
                "policy:19",
                heading="第三章 报告流程与通知时限",
                clause_no="第八条",
                text="第八条 危急值应在 10 分钟内通知检验科值班技师",
                paragraph_no=19,
            ),
        ]
    )
    result = GradedRetriever(_retriever(index)).retrieve_graded(
        MISS_QUERY, {"item_code": "718-7"}
    )

    assert result.level == LEVEL_CHAPTER
    clause_hits = [hit for hit in result.hits if hit.kind == "policy_clause"]
    assert clause_hits
    assert all(hit.level == LEVEL_CHAPTER for hit in clause_hits)
    assert all(hit.citation.level == LEVEL_CHAPTER for hit in clause_hits)
    # 取的是**该章节**的条款（第二章），第三章不在其中。
    assert {hit.citation.section_path[0] for hit in clause_hits} == {
        "第二章 危急值项目与阈值"
    }
    chapter_note = [
        attempt.note
        for attempt in result.fallback_attempts
        if attempt.level == LEVEL_CHAPTER
    ][0]
    assert "章节级兜底" in chapter_note
    assert "718-7" in chapter_note


def test_第2级_制度里没提到该项目就定位不到章节():
    """项目码在制度里压根没出现 → 不硬凑章节（不编依据）。"""
    index = _index(
        [
            _policy_doc(
                "policy:19",
                heading="第三章 报告流程与通知时限",
                clause_no="第八条",
                text="第八条 危急值应在 10 分钟内通知检验科值班技师",
                paragraph_no=19,
            ),
        ]
    )
    result = GradedRetriever(_retriever(index)).retrieve_graded(
        MISS_QUERY, {"item_code": "9999-9"}
    )

    assert result.level == LEVEL_NONE
    assert result.hits == ()


# —— 第 3 级：无依据转人工 ——


def test_第3级_降级用尽不产出任何引用():
    """四级用尽 → 零引用、事件转人工、写审计留痕。"""
    index = _index(
        [
            _policy_doc(
                "policy:19",
                heading="第三章 报告流程与通知时限",
                clause_no="第八条",
                text="第八条 危急值应在 10 分钟内通知检验科值班技师",
                paragraph_no=19,
            ),
        ]
    )
    result = GradedRetriever(_retriever(index)).retrieve_graded(
        MISS_QUERY, {"item_code": "9999-9"}
    )

    assert result.level == LEVEL_NONE
    assert result.hits == ()
    assert [attempt.level for attempt in result.fallback_attempts] == [
        LEVEL_EXACT,
        LEVEL_EXPANDED,
        LEVEL_CHAPTER,
        LEVEL_NONE,
    ]

    outcome = resolve_retrieval(result)
    assert outcome.empty is True
    assert outcome.citations == ()
    assert outcome.status == HUMAN_REVIEW_STATUS
    assert outcome.audit_action == AUDIT_EMPTY_RETRIEVAL
    assert outcome.level == LEVEL_NONE


def test_第3级_台账里不落任何引用():
    """降级用尽时台账的引用表零行（审计里也不落患者明细）。"""
    from critval.ledger.store import LedgerStore

    index = _index(
        [
            _policy_doc(
                "policy:19",
                heading="第三章 报告流程与通知时限",
                clause_no="第八条",
                text="第八条 危急值应在 10 分钟内通知检验科值班技师",
                paragraph_no=19,
            ),
        ]
    )
    result = GradedRetriever(_retriever(index)).retrieve_graded(
        MISS_QUERY, {"item_code": "9999-9"}
    )
    outcome = resolve_retrieval(result)

    event = CriticalEvent(
        event_id="evt-fallback-0001",
        message_control_id="MC-1",
        specimen_no="SPEC-0001",
        item_code="9999-9",
        item_name="未知项目",
        value=Decimal("1"),
        unit="U",
        observed_at=datetime(2026, 3, 1, 8, 30),
        dept_code="检验科",
    )
    store = LedgerStore(":memory:")
    store.record_event(event)
    apply_retrieval_outcome(
        store,
        event.event_id,
        outcome,
        dept_code=event.dept_code,
        item_code=event.item_code,
        specimen_no=event.specimen_no,
        message_control_id=event.message_control_id,
    )

    assert store.citations_of(event.event_id) == []
    assert store.get_event(event.event_id)["status"] == HUMAN_REVIEW_STATUS
    audits = store.audit_entries(event.event_id)
    assert any(audit["action"] == AUDIT_EMPTY_RETRIEVAL for audit in audits)


# —— 卡片可见性 ——


def test_卡片标出依据来自第几级降级():
    """降级召回的依据上卡片要写明"第几级降级"，不能当成同等强度。"""
    from critval.judge.critical import judge_event

    index = _index(
        [
            _threshold_doc("threshold:2", item_code="718-7", item_name="血红蛋白"),
            _policy_doc(
                "policy:10",
                heading="第二章 危急值项目与阈值",
                clause_no="第六条",
                text="第六条 718-7 数值下限 30 g/L",
                paragraph_no=10,
                item_codes=("718-7",),
            ),
        ]
    )
    result = GradedRetriever(_retriever(index)).retrieve_graded("血红蛋白 危急值")

    event = CriticalEvent(
        event_id="evt-fallback-0002",
        message_control_id="MC-2",
        specimen_no="SPEC-0002",
        item_code="718-7",
        item_name="血红蛋白",
        value=Decimal("62"),
        unit="g/L",
        observed_at=datetime(2026, 3, 1, 8, 30),
        dept_code="检验科",
    )
    judgement = judge_event(event, [])
    provenance = fallback_provenance(result)
    assert provenance.level == LEVEL_EXPANDED
    assert RETRIEVAL_LEVELS[provenance.level] == "关键词扩展"

    clause = PolicyClause(
        section_title="第二章 危急值项目与阈值",
        body="第六条 718-7 的危急下限 30 g/L",
        notify_target="检验科值班技师",
        deadline_text="10 分钟内",
        deadline_minutes=10,
        record_destination="危急值登记系统",
        citation=Citation(
            source_name="测试制度.docx",
            source_sha256="sha-policy",
            kind="policy_clause",
            paragraph_no=10,
            section_path=("第二章 危急值项目与阈值",),
            line_no=10,
            file_version="示例 v1.0",
        ),
        clause_no="第六条",
        item_codes=("718-7",),
    )
    card = build_card(
        event,
        judgement,
        policy_clause=clause,
        provenance=CardProvenance(policy_retrieval=provenance),
    )

    # 制度类要素（时限 / 通知对象 / 记录去向）标成 degraded 并写明降级层级。
    assert card.deadline.status == "degraded"
    assert card.deadline.reason == "retrieval_fallback_level"
    assert "关键词扩展" in card.deadline.note
    assert card.provenance.policy_retrieval.level == LEVEL_EXPANDED


def test_卡片层级号与召回层一致():
    """卡片写"第 2 级"时，召回层也得是 2 —— 层级号只有一份真源。"""
    assert RETRIEVAL_LEVELS[LEVEL_EXACT] == "原样查询"
    assert RETRIEVAL_LEVELS[LEVEL_EXPANDED] == "关键词扩展"
    assert RETRIEVAL_LEVELS[LEVEL_CHAPTER] == "章节级兜底"
    assert RETRIEVAL_LEVELS[LEVEL_NONE] == "无依据转人工"


# —— 不变式：不存在「无依据却产出引用」的路径 ——


@pytest.mark.parametrize(
    "query,filters",
    [
        ("危急值 10 分钟内 通知", {}),
        ("血红蛋白 危急值", {"item_code": "718-7"}),
        (MISS_QUERY, {"item_code": "718-7"}),
        (MISS_QUERY, {"item_code": "9999-9"}),
    ],
)
def test_任何分支都不会产出没有出处的引用(query, filters):
    """遍历所有分支：有引用必有出处，无依据必无引用。"""
    index = _index(
        [
            _threshold_doc("threshold:2", item_code="718-7", item_name="血红蛋白"),
            _policy_doc(
                "policy:10",
                heading="第二章 危急值项目与阈值",
                clause_no="第六条",
                text="第六条 718-7 数值下限 30 g/L",
                paragraph_no=10,
                item_codes=("718-7",),
            ),
            _policy_doc(
                "policy:19",
                heading="第三章 报告流程与通知时限",
                clause_no="第七条",
                text="第七条 危急值应在 10 分钟内通知检验科值班技师，并记入危急值登记系统",
                paragraph_no=19,
            ),
        ]
    )
    params = RetrievalParams.for_materials(
        "threshold", "policy", similarity_threshold=0.05
    )
    result = GradedRetriever(HybridRetriever(index, params)).retrieve_graded(query, filters)
    outcome = resolve_retrieval(result)

    if outcome.empty:
        # 没依据 → 一条引用都不产出。
        assert outcome.citations == ()
        assert outcome.status == HUMAN_REVIEW_STATUS
    for citation in outcome.citations:
        assert citation.source_sha256
        assert citation.line_no is not None
        assert citation.level in (LEVEL_EXACT, LEVEL_EXPANDED, LEVEL_CHAPTER)


def test_章节兜底不采无出处的条款():
    """没有出处的条款进不了引用（宁可不召回，也不挂一条查无出处的依据）。"""
    index = _index(
        [
            _policy_doc(
                "policy:10",
                heading="第二章 危急值项目与阈值",
                clause_no="第六条",
                text="第六条 718-7 数值下限 30 g/L",
                paragraph_no=10,
                item_codes=("718-7",),
            ),
            _policy_doc(
                "policy:11",
                heading="第二章 危急值项目与阈值",
                clause_no="第六条之二",
                text="第六条之二 718-7 的危急上限 200 g/L",
                paragraph_no=11,
                with_citation=False,
            ),
        ]
    )
    result = GradedRetriever(_retriever(index)).retrieve_graded(
        MISS_QUERY, {"item_code": "718-7"}
    )

    assert result.level == LEVEL_CHAPTER
    assert len(result.hits) == 1
    for hit in result.hits:
        assert hit.citation is not None
        assert hit.citation.source_sha256


# —— 真实样例材料 ——


def test_真实样例材料上原样就能查到制度条款():
    """院内样例材料（阈值表 + 制度）上，原样查询就够 —— 降级不该乱触发。"""
    index = MaterialIndex()
    index.rebuild(
        [
            MaterialSource.from_path(SAMPLE_THRESHOLD, kind="threshold_table", key="threshold"),
            MaterialSource.from_path(SAMPLE_POLICY, kind="policy_document", key="policy"),
        ]
    )
    params = RetrievalParams.for_materials("threshold", "policy")
    result = GradedRetriever(HybridRetriever(index, params)).retrieve_graded(
        "血红蛋白危急值多久要上报"
    )

    assert result.level == LEVEL_EXACT
    clause_hits = [hit for hit in result.hits if hit.kind == "policy_clause"]
    assert clause_hits
    assert all(hit.citation.source_sha256 for hit in clause_hits)
