"""出处随命中走 · 空召回兜底 · 版本一致性断言（任务 id 8）。

覆盖四组硬要求：

* **出处五件套齐备** —— 引用带 sha256 + 段落序号 + 章节路径 + 可读行号 +
  文件版本；缺哪一项要说得出；
* **引用随命中落库** —— 召回命中里的出处逐条进 ``citations`` 表，事后查得回；
* **空召回不产出任何引用** —— 一条引用都不落，事件转 ``transferred_to_human``
  并写审计留痕（**绝不编依据**）；
* **版本一致性断言** —— 同一次处置混用同一类材料的多个版本 → 拒绝出卡并报
  ``version_mismatch``（同源产品「同一次处置只允许用同一套底座」的语义改写）。
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from critval.kb.consistency import VersionBinding, VersionMismatchError
from critval.ledger.store import LedgerStore
from critval.models import Citation, CriticalEvent
from critval.retrieve.citation import (
    AUDIT_EMPTY_RETRIEVAL,
    HUMAN_REVIEW_STATUS,
    apply_retrieval_outcome,
    assert_versions_consistent,
    citation_gaps,
    citations_of_hits,
    describe_citations,
    persist_citations,
    resolve_retrieval,
)
from critval.retrieve.engine import RetrievalResult

PROJECT_ROOT = Path(__file__).resolve().parent.parent


# —— 测试装置 ——


def _threshold_citation(version: str = "示例 v1.0", line_no: int = 2) -> Citation:
    return Citation(
        source_name="示例危急值阈值表.xlsx",
        source_sha256="sha-threshold-0001",
        kind="threshold_row",
        paragraph_no=None,
        section_path=(),
        line_no=line_no,
        file_version=version,
    )


def _policy_citation(version: str = "示例 v1.0", paragraph_no: int = 19) -> Citation:
    return Citation(
        source_name="示例危急值报告制度.docx",
        source_sha256="sha-policy-0001",
        kind="policy_clause",
        paragraph_no=paragraph_no,
        section_path=("第三章 报告流程与通知时限",),
        line_no=paragraph_no,
        file_version=version,
    )


class _Hit:
    """最小命中替身（只带出处 —— 引用收集只认这一样）。"""

    def __init__(self, citation):
        self.citation = citation


def _event(event_id: str = "evt-cite-0001") -> CriticalEvent:
    return CriticalEvent(
        event_id=event_id,
        message_control_id="MSG-CITE-0001",
        specimen_no="SP-0001",
        item_code="718-7",
        item_name="血红蛋白",
        value=Decimal("25"),
        unit="g/L",
        observed_at=datetime(2026, 3, 1, 8, 30, tzinfo=timezone.utc),
        dept_code="LAB",
        patient_ref="pt-abcdef012345",
    )


# ---------------------------------------------------------------------------
# 出处五件套
# ---------------------------------------------------------------------------


def test_出处五件套齐备时没有缺项():
    """制度条款与阈值行的出处都应被判为齐备（按材料类型判定）。"""
    assert citation_gaps(_policy_citation()) == ()
    assert citation_gaps(_threshold_citation()) == ()


def test_没有出处算缺而不是算齐备():
    """「没有出处」不是「出处齐备」。"""
    assert citation_gaps(None) == ("citation",)


def test_缺可读行号与版本号会被点出来():
    """缺哪一项要说得出，不能笼统一句「出处不全」。"""
    citation = Citation(
        source_name="示例危急值阈值表.xlsx",
        source_sha256="sha-threshold-0001",
        kind="threshold_row",
        line_no=None,
        file_version="",
    )
    gaps = citation_gaps(citation)
    assert "line_no" in gaps
    assert "file_version" in gaps
    assert "source_sha256" not in gaps


def test_制度条款缺段落序号算缺项但阈值行不算():
    """阈值行没有「段落」这个概念 —— 不能因为它没有段落序号就判它出处不全。"""
    citation = Citation(
        source_name="示例危急值报告制度.docx",
        source_sha256="sha-policy-0001",
        kind="policy_clause",
        line_no=19,
        file_version="示例 v1.0",
    )
    assert "paragraph_no" in citation_gaps(citation)
    threshold = Citation(
        source_name="示例危急值阈值表.xlsx",
        source_sha256="sha-threshold-0001",
        kind="threshold_row",
        line_no=2,
        file_version="示例 v1.0",
    )
    assert "paragraph_no" not in citation_gaps(threshold)


def test_严格口径要求章节路径也在内():
    """严格口径下五件套缺一不可。"""
    citation = _policy_citation()
    assert "section_path" not in citation_gaps(citation, "strict")
    no_section = Citation(
        source_name="示例危急值报告制度.docx",
        source_sha256="sha-policy-0001",
        kind="policy_clause",
        paragraph_no=19,
        section_path=(),
        line_no=19,
        file_version="示例 v1.0",
    )
    assert "section_path" in citation_gaps(no_section, "strict")


def test_不认识的出处口径报错():
    with pytest.raises(ValueError):
        citation_gaps(_policy_citation(), "whatever")


# ---------------------------------------------------------------------------
# 引用收集
# ---------------------------------------------------------------------------


def test_引用随命中收集且保序去重():
    """同一条依据被两路同时命中时只留一条，且保持名次顺序。"""
    hits = [
        _Hit(_threshold_citation()),
        _Hit(_policy_citation()),
        _Hit(_threshold_citation()),
    ]
    citations = citations_of_hits(hits)
    assert len(citations) == 2
    assert citations[0].kind == "threshold_row"
    assert citations[1].kind == "policy_clause"


def test_没有出处的命中不产出引用():
    hits = [_Hit(None), _Hit(_policy_citation())]
    citations = citations_of_hits(hits)
    assert len(citations) == 1
    assert citations[0].kind == "policy_clause"


def test_描述引用时把缺项写出来():
    """给人看的行里要写清缺什么，而不是只给一个定位串。"""
    broken = Citation(
        source_name="示例危急值阈值表.xlsx",
        source_sha256="sha-threshold-0001",
        kind="threshold_row",
        line_no=None,
        file_version="",
    )
    lines = describe_citations([broken])
    assert "出处缺" in lines[0]
    assert "line_no" in lines[0]


# ---------------------------------------------------------------------------
# 引用落库
# ---------------------------------------------------------------------------


def test_引用落库后能按事件查回():
    """出处随命中落库 —— 事后复核查得到「当时引用了哪一行」。"""
    store = LedgerStore(":memory:")
    try:
        store.record_event(_event())
        count = persist_citations(
            store, "evt-cite-0001", "card-0001", [_threshold_citation(), _policy_citation()]
        )
        assert count == 2
        rows = store.citations_of("evt-cite-0001")
        assert len(rows) == 2
        assert {row["kind"] for row in rows} == {"threshold_row", "policy_clause"}
        assert all(row["source_sha256"] for row in rows)
        assert all(row["locator"] for row in rows)
    finally:
        store.close()


# ---------------------------------------------------------------------------
# 空召回兜底
# ---------------------------------------------------------------------------


def test_空召回不产出任何引用():
    """没检索到依据 → 一条引用都不产出。"""
    result = RetrievalResult(hits=(), empty_response="未检索到可引用的处置依据")
    outcome = resolve_retrieval(result)
    assert outcome.empty is True
    assert outcome.citations == ()
    assert outcome.status == HUMAN_REVIEW_STATUS == "transferred_to_human"
    assert outcome.audit_action == AUDIT_EMPTY_RETRIEVAL


def test_空召回时事件转人工并写审计留痕():
    """空召回 → 状态转人工 + 一条审计（记标本号 / 项目码 / 科室，不落患者明细）。"""
    store = LedgerStore(":memory:")
    try:
        store.record_event(_event())
        result = RetrievalResult(hits=(), empty_response="未检索到可引用的处置依据")
        outcome = resolve_retrieval(result)
        apply_retrieval_outcome(
            store,
            "evt-cite-0001",
            outcome,
            dept_code="LAB",
            specimen_no="SP-0001",
            item_code="718-7",
            message_control_id="MSG-CITE-0001",
        )
        # 状态已迁移
        row = store.get_event("evt-cite-0001")
        assert row is not None
        assert row["status"] == HUMAN_REVIEW_STATUS
        # 一条引用都不落
        assert store.citations_of("evt-cite-0001") == []
        # 审计留痕（任务 id 10 起，每次状态迁移也写一条 status_transition）
        audits = store.audit_entries("evt-cite-0001")
        actions = [entry["action"] for entry in audits]
        assert AUDIT_EMPTY_RETRIEVAL in actions
        assert "status_transition" in actions
        empty_audit = next(
            entry for entry in audits if entry["action"] == AUDIT_EMPTY_RETRIEVAL
        )
        assert empty_audit["item_code"] == "718-7"
        assert empty_audit["specimen_no"] == "SP-0001"
        assert "不产出任何引用" in empty_audit["detail"]
        # 患者明细不进审计
        assert "abcdef012345" not in empty_audit["detail"]
    finally:
        store.close()


def test_有依据时状态不变也不写异常审计():
    store = LedgerStore(":memory:")
    try:
        store.record_event(_event())
        result = RetrievalResult(
            hits=(_Hit(_threshold_citation()),),
            empty_response="",
        )
        outcome = resolve_retrieval(result)
        assert outcome.empty is False
        assert outcome.status == ""
        assert outcome.audit_action == ""
        assert len(outcome.citations) == 1
        apply_retrieval_outcome(store, "evt-cite-0001", outcome, card_id="card-0001")
        assert len(store.citations_of("evt-cite-0001")) == 1
        assert store.audit_entries("evt-cite-0001") == []
    finally:
        store.close()


def test_空召回的兜底话术留给人看():
    """话术来自参数面（源产品 empty_response 槽位的同源语义）。"""
    result = RetrievalResult(hits=(), empty_response="未检索到可引用的处置依据，转人工并留痕（不编造依据）")
    outcome = resolve_retrieval(result)
    assert "不编造依据" in outcome.empty_response


# ---------------------------------------------------------------------------
# 版本一致性断言
# ---------------------------------------------------------------------------


def test_版本一致时断言通过():
    consistency = assert_versions_consistent([_threshold_citation(), _policy_citation()])
    assert consistency.ok is True


def test_同类材料混版时拒绝出卡并报version_mismatch():
    """阈值表 v1.0 与 v2.0 同时被引用 → 依据口径自相矛盾，拒绝出卡。"""
    citations = [_threshold_citation("示例 v1.0"), _threshold_citation("示例 v2.0")]
    with pytest.raises(VersionMismatchError) as excinfo:
        assert_versions_consistent(citations)
    assert excinfo.value.code == "version_mismatch"
    assert "混用" in excinfo.value.consistency.reason


def test_跨版本绑定不符时拒绝出卡():
    """引用了绑定之外的版本 → 拒绝出卡。"""
    binding = VersionBinding(
        item_code="718-7",
        threshold_version="示例 v1.0",
        policy_version="示例 v1.0",
    )
    with pytest.raises(VersionMismatchError) as excinfo:
        assert_versions_consistent(
            [_threshold_citation("示例 v2.0"), _policy_citation("示例 v1.0")],
            binding=binding,
        )
    assert excinfo.value.code == "version_mismatch"
    assert excinfo.value.consistency.expected_threshold_version == "示例 v1.0"


def test_版本不一致时出处的两方版本都留着供人工裁决():
    """拒卡不等于丢掉证据 —— 结论里要能看出是哪两个版本打架。"""
    citations = [_threshold_citation("示例 v1.0"), _threshold_citation("示例 v2.0")]
    with pytest.raises(VersionMismatchError) as excinfo:
        assert_versions_consistent(citations)
    mixed = excinfo.value.consistency.mixed_versions
    assert "示例 v1.0" in mixed
    assert "示例 v2.0" in mixed
