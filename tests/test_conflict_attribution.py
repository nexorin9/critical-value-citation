"""双路召回差异归因的测试（任务 id 33）。

覆盖四类归因（口径冲突 / 版本冲突 / 范围冲突 / 无冲突）各一组构造用例，
并验证三件事：

1. 冲突时**系统不自动择一** —— 两方的取值与出处都被保留；
2. 冲突挂到处置卡的**待人工裁决区块**，未裁决时相关要素**不标「已解决」**；
3. 冲突写进**审计流水**，审计里有两方出处，且不落患者明细。
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from critval.card.builder import build_card
from critval.card.provenance import REASON_CONFLICT_PENDING
from critval.card.renderer import build_render_payload, render_card
from critval.judge.critical import judge_event
from critval.ledger.store import LedgerStore
from critval.models import (
    Citation,
    ConflictItem,
    CriticalEvent,
    PolicyClause,
    ThresholdRow,
    to_jsonable,
)
from critval.retrieve.conflict import (
    CONFLICT_CALIBER,
    CONFLICT_NONE,
    CONFLICT_SCOPE,
    CONFLICT_VERSION,
    FIELD_DEADLINE,
    FIELD_NOTIFY_TARGET,
    FIELD_THRESHOLD_VALUE,
    PATH_LEXICAL,
    PATH_STRUCTURED,
    ROLE_POLICY_CLAUSE,
    ROLE_THRESHOLD_ROW,
    attribute_conflicts,
    classify_conflicts,
    clause_evidence,
    parse_policy_threshold,
    threshold_evidence,
    write_conflict_audit,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SAMPLE_THRESHOLD = PROJECT_ROOT / "samples" / "threshold" / "示例危急值阈值表.xlsx"
SAMPLE_POLICY = PROJECT_ROOT / "samples" / "policy" / "示例危急值报告制度.docx"


# ---------------------------------------------------------------------------
# 构造工具
# ---------------------------------------------------------------------------


def _citation(
    source_name: str = "脱敏样例阈值表.xlsx",
    line_no: int | None = 2,
    paragraph_no: int | None = None,
    version: str = "测试 v1.0",
    kind: str = "threshold_row",
) -> Citation:
    return Citation(
        source_name=source_name,
        source_sha256="sha256-" + "0" * 8,
        kind=kind,
        paragraph_no=paragraph_no,
        section_path=("第三章 报告流程与通知时限",) if kind == "policy_clause" else (),
        line_no=line_no,
        file_version=version,
    )


def _row(
    item_code: str = "718-7",
    item_name: str = "血红蛋白",
    low: str | None = "30",
    high: str | None = "200",
    unit: str = "g/L",
    version: str = "测试 v1.0",
    line_no: int = 2,
    low_inclusive: bool = False,
    high_inclusive: bool = False,
) -> ThresholdRow:
    return ThresholdRow(
        item_code=item_code,
        item_name=item_name,
        unit=unit,
        low_critical=Decimal(low) if low is not None else None,
        high_critical=Decimal(high) if high is not None else None,
        reference_range_raw="115-150",
        effective_from=None,
        version=version,
        citation=_citation(version=version, line_no=line_no),
        row_no=line_no,
        low_inclusive=low_inclusive,
        high_inclusive=high_inclusive,
    )


def _clause(
    body: str = "第七条 危急值报告应在上报时限内发出；通知对象：检验科值班技师；上报时限：10 分钟内；记录去向：危急值登记系统。",
    notify_target: str = "检验科值班技师",
    deadline_text: str = "10 分钟内",
    deadline_minutes: int | None = 10,
    record_destination: str = "危急值登记系统",
    item_codes: tuple[str, ...] = (),
    item_names: tuple[str, ...] = (),
    paragraph_no: int = 19,
    version: str = "测试 v1.0",
    source_name: str = "脱敏样例危急值报告制度.docx",
) -> PolicyClause:
    return PolicyClause(
        section_title="第三章 报告流程与通知时限",
        body=body,
        notify_target=notify_target,
        deadline_text=deadline_text,
        deadline_minutes=deadline_minutes,
        record_destination=record_destination,
        citation=_citation(
            source_name=source_name,
            paragraph_no=paragraph_no,
            line_no=paragraph_no,
            version=version,
            kind="policy_clause",
        ),
        clause_no="第七条",
        item_codes=item_codes,
        item_names=item_names,
        stable_id=f"第七条#{paragraph_no}",
    )


def _event(item_code: str = "718-7", value: str = "25", unit: str = "g/L") -> CriticalEvent:
    from datetime import datetime

    return CriticalEvent(
        event_id="evt-conflict-0001",
        message_control_id="CONFMSG0001",
        specimen_no="SPEC-CONF-0001",
        item_code=item_code,
        item_name="血红蛋白",
        value=Decimal(value),
        unit=unit,
        observed_at=datetime(2026, 1, 2, 8, 30),
        dept_code="ICU",
        source_channel="demo",
    )


# ---------------------------------------------------------------------------
# 一、口径冲突
# ---------------------------------------------------------------------------


def test_时限口径冲突_两路各执一词且两方出处都保留():
    """结构化路说 10 分钟、词法路说 15 分钟 → 口径冲突，两方都留着。"""
    row = _row()
    structured_clause = _clause(
        body="第七条 血红蛋白（718-7）危急值应在 10 分钟内报告；上报时限：10 分钟内。",
        deadline_text="10 分钟内",
        deadline_minutes=10,
        item_codes=("718-7",),
        paragraph_no=19,
    )
    lexical_clause = _clause(
        body="第八条 危急值报告发出后护士长应在 15 分钟内响应；上报时限：15 分钟内。",
        notify_target="检验科值班技师",
        deadline_text="15 分钟内",
        deadline_minutes=15,
        paragraph_no=20,
    )

    conflicts = attribute_conflicts(
        "718-7",
        [threshold_evidence(row), clause_evidence(structured_clause, PATH_STRUCTURED)],
        [clause_evidence(lexical_clause, PATH_LEXICAL)],
    )

    assert len(conflicts) == 1
    conflict = conflicts[0]
    assert conflict.kind == CONFLICT_CALIBER
    assert conflict.field == FIELD_DEADLINE
    assert conflict.status == "pending"
    # 两方取值与出处都在 —— 系统没有替值班人选一个
    assert conflict.structured_side is not None
    assert conflict.lexical_side is not None
    assert conflict.structured_side.value == "10 分钟内"
    assert conflict.lexical_side.value == "15 分钟内"
    assert "第 19 行" in conflict.structured_side.locator
    assert "第 20 行" in conflict.lexical_side.locator
    assert "10 分钟内" in conflict.description
    assert "15 分钟内" in conflict.description


def test_通知对象口径冲突_两路给出的通知对象不一致():
    row = _row()
    structured_clause = _clause(
        notify_target="检验科值班技师与检验科组长",
        deadline_text="10 分钟内",
        deadline_minutes=10,
        item_codes=("718-7",),
    )
    lexical_clause = _clause(
        notify_target="临床科室值班护士长",
        deadline_text="10 分钟内",
        deadline_minutes=10,
        paragraph_no=20,
    )

    conflicts = attribute_conflicts(
        "718-7",
        [threshold_evidence(row), clause_evidence(structured_clause, PATH_STRUCTURED)],
        [clause_evidence(lexical_clause, PATH_LEXICAL)],
    )

    assert len(conflicts) == 1
    assert conflicts[0].kind == CONFLICT_CALIBER
    assert conflicts[0].field == FIELD_NOTIFY_TARGET
    assert conflicts[0].structured_side.value == "检验科值班技师与检验科组长"
    assert conflicts[0].lexical_side.value == "临床科室值班护士长"


def test_危急阈值数值口径冲突_制度正文与阈值表那一行不一致():
    """制度写"低于 30"，阈值表写 25 → 数值口径冲突。"""
    row = _row(low="25", high="200")
    clause = _clause(
        body="第六条 血红蛋白（718-7）低于 30 或高于 200 判定为危急值。",
        notify_target="",
        deadline_text="",
        deadline_minutes=None,
        record_destination="",
        item_codes=("718-7",),
        item_names=("血红蛋白",),
        paragraph_no=10,
    )

    conflicts = attribute_conflicts(
        "718-7",
        [threshold_evidence(row)],
        [clause_evidence(clause, PATH_LEXICAL)],
    )

    assert len(conflicts) == 1
    conflict = conflicts[0]
    assert conflict.kind == CONFLICT_CALIBER
    assert conflict.field == FIELD_THRESHOLD_VALUE
    assert conflict.structured_side.value == "< 25 或 > 200 g/L"
    assert conflict.lexical_side.value == "< 30 或 > 200 g/L"


def test_危急阈值开闭语义不同也算口径冲突():
    """阈值表写 ≤30、制度写"低于 30" —— 临界点上两种说法不一样。"""
    row = _row(low="30", high="200", low_inclusive=True)
    clause = _clause(
        body="第六条 血红蛋白（718-7）低于 30 或高于 200 判定为危急值。",
        notify_target="",
        deadline_text="",
        deadline_minutes=None,
        record_destination="",
        item_codes=("718-7",),
        item_names=("血红蛋白",),
        paragraph_no=10,
    )

    conflicts = attribute_conflicts(
        "718-7",
        [threshold_evidence(row)],
        [clause_evidence(clause, PATH_LEXICAL)],
    )

    assert len(conflicts) == 1
    assert conflicts[0].field == FIELD_THRESHOLD_VALUE


def test_制度正文里的危急阈值解析不出来时不硬凑():
    """正文没有比较词（只有参考区间）→ 解析不出阈值，不报数值冲突。"""
    row = _row(low="25", high="200")
    clause = _clause(
        body="第六条 血红蛋白（718-7）的参考区间为 115-150，单位 g/L。",
        notify_target="",
        deadline_text="",
        deadline_minutes=None,
        record_destination="",
        item_codes=("718-7",),
        item_names=("血红蛋白",),
        paragraph_no=10,
    )

    conflicts = attribute_conflicts(
        "718-7",
        [threshold_evidence(row)],
        [clause_evidence(clause, PATH_LEXICAL)],
    )

    assert conflicts == []


def test_参考区间不会被误读成危急阈值():
    parsed = parse_policy_threshold("血红蛋白（718-7）的参考区间为 115-150，单位 g/L")
    assert parsed.low is None
    assert parsed.high is None


# ---------------------------------------------------------------------------
# 二、版本冲突
# ---------------------------------------------------------------------------


def test_版本冲突_引用了非生效版本的制度条款():
    clause = _clause(version="测试 v1.0", paragraph_no=19)

    conflicts = attribute_conflicts(
        "718-7",
        [],
        [clause_evidence(clause, PATH_LEXICAL)],
        effective_policy_version="测试 v2.0",
    )

    assert len(conflicts) == 1
    conflict = conflicts[0]
    assert conflict.kind == CONFLICT_VERSION
    assert "测试 v1.0" in conflict.description
    assert "测试 v2.0" in conflict.description
    assert conflict.sides[0].citation is not None
    assert conflict.sides[0].citation.file_version == "测试 v1.0"


def test_版本一致时不报版本冲突():
    clause = _clause(version="测试 v2.0")
    conflicts = attribute_conflicts(
        "718-7",
        [],
        [clause_evidence(clause, PATH_LEXICAL)],
        effective_policy_version="测试 v2.0",
    )
    assert conflicts == []


# ---------------------------------------------------------------------------
# 三、范围冲突
# ---------------------------------------------------------------------------


def test_范围冲突_条款点名的项目不是本次项目():
    """词法路按文字捞上来一条别的项目的条款 → 范围不匹配。"""
    row = _row(item_code="718-7", item_name="血红蛋白")
    other_clause = _clause(
        body="第六条 钾（2823-3）的参考区间为 3.5-5.3，单位 mmol/L；低于 2.8 或高于 6.5 判定为危急值。",
        notify_target="",
        deadline_text="",
        deadline_minutes=None,
        record_destination="",
        item_codes=("2823-3",),
        item_names=("钾",),
        paragraph_no=14,
    )

    conflicts = attribute_conflicts(
        "718-7",
        [threshold_evidence(row)],
        [clause_evidence(other_clause, PATH_LEXICAL)],
    )

    assert len(conflicts) == 1
    conflict = conflicts[0]
    assert conflict.kind == CONFLICT_SCOPE
    assert "2823-3" in conflict.description
    assert conflict.structured_side.value == "718-7"
    assert conflict.lexical_side.value == "2823-3"


def test_范围一致时不报范围冲突():
    row = _row()
    clause = _clause(item_codes=("718-7",), item_names=("血红蛋白",))
    conflicts = attribute_conflicts(
        "718-7",
        [threshold_evidence(row)],
        [clause_evidence(clause, PATH_LEXICAL)],
    )
    assert conflicts == []


# ---------------------------------------------------------------------------
# 四、无冲突
# ---------------------------------------------------------------------------


def test_无冲突_两路说法一致():
    row = _row()
    structured_clause = _clause(
        deadline_text="10 分钟内", deadline_minutes=10, item_codes=("718-7",)
    )
    lexical_clause = _clause(
        deadline_text="10 分钟内", deadline_minutes=10, paragraph_no=20
    )

    conflicts = attribute_conflicts(
        "718-7",
        [threshold_evidence(row), clause_evidence(structured_clause, PATH_STRUCTURED)],
        [clause_evidence(lexical_clause, PATH_LEXICAL)],
    )

    assert conflicts == []
    assert classify_conflicts(conflicts) == CONFLICT_NONE


def test_只有一路有说法不算冲突():
    """只有一路开口就没有对立面，不硬凑冲突。"""
    row = _row()
    clause = _clause(deadline_text="10 分钟内", deadline_minutes=10)

    conflicts = attribute_conflicts(
        "718-7",
        [threshold_evidence(row)],
        [clause_evidence(clause, PATH_LEXICAL)],
    )

    assert conflicts == []
    assert classify_conflicts(conflicts) == CONFLICT_NONE


def test_整体归因取最严重的一类():
    row = _row()
    scoped = _clause(item_codes=("718-7",), deadline_text="10 分钟内", deadline_minutes=10)
    other = _clause(deadline_text="15 分钟内", deadline_minutes=15, paragraph_no=20)

    conflicts = attribute_conflicts(
        "718-7",
        [threshold_evidence(row), clause_evidence(scoped, PATH_STRUCTURED)],
        [clause_evidence(other, PATH_LEXICAL)],
        effective_policy_version="测试 v2.0",
    )

    kinds = {conflict.kind for conflict in conflicts}
    assert CONFLICT_VERSION in kinds
    assert CONFLICT_CALIBER in kinds
    # 口径冲突最严重 → 整体归因取它
    assert classify_conflicts(conflicts) == CONFLICT_CALIBER


# ---------------------------------------------------------------------------
# 五、冲突进处置卡（待人工裁决区块）
# ---------------------------------------------------------------------------


def _card_with_conflict(conflicts: list[ConflictItem], clause: PolicyClause | None = None):
    row = _row()
    event = _event()
    judgement = judge_event(event, [row])
    return build_card(
        event,
        judgement,
        policy_clause=clause if clause is not None else _clause(),
        conflicts=conflicts,
    )


def test_冲突进处置卡_未裁决时相关要素不标已解决():
    row = _row()
    structured_clause = _clause(
        deadline_text="10 分钟内", deadline_minutes=10, item_codes=("718-7",)
    )
    lexical_clause = _clause(
        deadline_text="15 分钟内",
        deadline_minutes=15,
        paragraph_no=20,
    )
    conflicts = attribute_conflicts(
        "718-7",
        [threshold_evidence(row), clause_evidence(structured_clause, PATH_STRUCTURED)],
        [clause_evidence(lexical_clause, PATH_LEXICAL)],
    )

    card = _card_with_conflict(conflicts)

    assert card.conflicts
    assert card.pending_conflicts()
    assert card.conflicting_elements() == ("上报时限",)
    deadline = card.deadline
    assert deadline.status != "resolved"
    assert deadline.status == "degraded"
    assert deadline.reason == REASON_CONFLICT_PENDING
    # 未受牵连的要素不受影响
    assert card.threshold_basis.status == "resolved"
    # 两方出处都留在卡上
    locators = {side.locator for side in card.conflicts[0].sides}
    assert any("第 19 行" in text for text in locators)
    assert any("第 20 行" in text for text in locators)


def test_三渠道都渲染待人工裁决区块():
    row = _row()
    structured_clause = _clause(
        deadline_text="10 分钟内", deadline_minutes=10, item_codes=("718-7",)
    )
    lexical_clause = _clause(
        deadline_text="15 分钟内", deadline_minutes=15, paragraph_no=20
    )
    conflicts = attribute_conflicts(
        "718-7",
        [threshold_evidence(row), clause_evidence(structured_clause, PATH_STRUCTURED)],
        [clause_evidence(lexical_clause, PATH_LEXICAL)],
    )
    card = _card_with_conflict(conflicts)

    markdown = render_card(card, "markdown")
    html = render_card(card, "html")
    text = render_card(card, "text")

    assert "待人工裁决" in markdown
    assert "待人工裁决" in html
    assert "待人工裁决" in text
    # 两方取值都出现
    for rendered in (markdown, html, text):
        assert "10 分钟内" in rendered
        assert "15 分钟内" in rendered
    # 两方出处行号都在
    assert "第 19 行" in markdown
    assert "第 20 行" in markdown
    assert "第 19 行" in html
    assert "第 20 行" in html
    assert "第 19 行" in text
    assert "第 20 行" in text


def test_渲染载荷里两方说法都在且可JSON化():
    row = _row()
    structured_clause = _clause(
        deadline_text="10 分钟内", deadline_minutes=10, item_codes=("718-7",)
    )
    lexical_clause = _clause(
        deadline_text="15 分钟内", deadline_minutes=15, paragraph_no=20
    )
    conflicts = attribute_conflicts(
        "718-7",
        [threshold_evidence(row), clause_evidence(structured_clause, PATH_STRUCTURED)],
        [clause_evidence(lexical_clause, PATH_LEXICAL)],
    )
    card = _card_with_conflict(conflicts)

    payload = build_render_payload(card)
    assert payload["conflicts"]
    entry = payload["conflicts"][0]
    assert entry["kind"] == CONFLICT_CALIBER
    assert entry["field"] == FIELD_DEADLINE
    values = sorted(side["value"] for side in entry["sides"])
    assert values == ["10 分钟内", "15 分钟内"]
    json.dumps(to_jsonable(payload), ensure_ascii=False)  # 可 JSON 化


def test_已裁决的冲突不再拖累要素():
    row = _row()
    structured_clause = _clause(
        deadline_text="10 分钟内", deadline_minutes=10, item_codes=("718-7",)
    )
    lexical_clause = _clause(
        deadline_text="15 分钟内", deadline_minutes=15, paragraph_no=20
    )
    conflicts = attribute_conflicts(
        "718-7",
        [threshold_evidence(row), clause_evidence(structured_clause, PATH_STRUCTURED)],
        [clause_evidence(lexical_clause, PATH_LEXICAL)],
    )
    adjudicated = [
        ConflictItem(
            conflict_id=conflict.conflict_id,
            kind=conflict.kind,
            field=conflict.field,
            item_code=conflict.item_code,
            description=conflict.description,
            sides=conflict.sides,
            status="adjudicated",
        )
        for conflict in conflicts
    ]

    card = _card_with_conflict(adjudicated)

    assert card.deadline.status == "resolved"
    assert not card.pending_conflicts()
    assert card.conflicting_elements() == ()


# ---------------------------------------------------------------------------
# 六、审计留痕
# ---------------------------------------------------------------------------


def test_冲突写审计_两方出处都在且不落患者明细():
    row = _row()
    structured_clause = _clause(
        deadline_text="10 分钟内", deadline_minutes=10, item_codes=("718-7",)
    )
    lexical_clause = _clause(
        deadline_text="15 分钟内", deadline_minutes=15, paragraph_no=20
    )
    conflicts = attribute_conflicts(
        "718-7",
        [threshold_evidence(row), clause_evidence(structured_clause, PATH_STRUCTURED)],
        [clause_evidence(lexical_clause, PATH_LEXICAL)],
    )

    store = LedgerStore(":memory:")
    try:
        written = write_conflict_audit(
            store,
            conflicts,
            event_id="evt-conflict-0001",
            item_code="718-7",
            specimen_no="SPEC-CONF-0001",
            message_control_id="CONFMSG0001",
            dept_code="ICU",
        )
        assert len(written) == len(conflicts) == 1

        entries = store.audit_entries()
        assert len(entries) == 1
        entry = entries[0]
        assert entry["action"] == "recall_conflict_detected"
        assert entry["item_code"] == "718-7"
        assert entry["specimen_no"] == "SPEC-CONF-0001"
        assert "第 19 行" in entry["detail"]
        assert "第 20 行" in entry["detail"]
        assert "口径冲突" in entry["detail"]
        # 审计不落患者明细
        for forbidden in ("姓名", "身份证", "住院号", "病案号"):
            assert forbidden not in entry["detail"]
    finally:
        store.close()


def test_审计可按事件查回():
    conflicts = [
        ConflictItem(
            conflict_id="cf-caliber-deadline-0001",
            kind=CONFLICT_CALIBER,
            field=FIELD_DEADLINE,
            item_code="718-7",
            description="上报时限口径不一致",
            sides=(),
            status="pending",
        )
    ]
    store = LedgerStore(":memory:")
    try:
        write_conflict_audit(store, conflicts, event_id="evt-x", item_code="718-7")
        assert store.audit_entries("evt-x")
        assert store.audit_entries("evt-other") == []
    finally:
        store.close()


# ---------------------------------------------------------------------------
# 七、真实样例材料（回归：样例材料本身没有口径冲突）
# ---------------------------------------------------------------------------


def test_样例材料_阈值表与制度正文的危急阈值一致不报冲突():
    """样例阈值表 718-7 写 30/200，制度正文写"低于 30 或高于 200" → 一致。"""
    from critval.kb.loader import load_threshold_table
    from critval.kb.policy import load_policy_document

    table = load_threshold_table(SAMPLE_THRESHOLD)
    policy_document = load_policy_document(SAMPLE_POLICY)
    row = next(r for r in table.rows if r.item_code == "718-7")
    clause = next(c for c in policy_document.clauses if "718-7" in c.item_codes)

    conflicts = attribute_conflicts(
        "718-7",
        [threshold_evidence(row)],
        [clause_evidence(clause, PATH_LEXICAL)],
    )

    assert conflicts == []
    assert classify_conflicts(conflicts) == CONFLICT_NONE


def test_样例制度_两处不同时限会被归因为口径冲突():
    """样例制度里第七条 10 分钟、第九条 30 分钟 —— 结构化路与词法路各执一词。"""
    from critval.kb.loader import load_threshold_table
    from critval.kb.policy import load_policy_document

    table = load_threshold_table(SAMPLE_THRESHOLD)
    policy_document = load_policy_document(SAMPLE_POLICY)
    row = next(r for r in table.rows if r.item_code == "718-7")
    seventh = next(c for c in policy_document.clauses if c.clause_no == "第七条")
    ninth = next(c for c in policy_document.clauses if c.clause_no == "第九条")

    conflicts = attribute_conflicts(
        "718-7",
        [threshold_evidence(row), clause_evidence(seventh, PATH_STRUCTURED)],
        [clause_evidence(ninth, PATH_LEXICAL)],
    )

    deadline_conflicts = [c for c in conflicts if c.field == FIELD_DEADLINE]
    assert len(deadline_conflicts) == 1
    conflict = deadline_conflicts[0]
    assert conflict.kind == CONFLICT_CALIBER
    assert conflict.structured_side.value == "10 分钟内"
    assert conflict.lexical_side.value == "30 分钟内"
    # 两处出处都在：第七条第 19 段 / 第九条第 21 段
    assert "第 19 行" in conflict.structured_side.locator
    assert "第 21 行" in conflict.lexical_side.locator


def test_依据侧角色与路径都记在案():
    row = _row()
    clause = _clause()
    assert threshold_evidence(row).role == ROLE_THRESHOLD_ROW
    assert threshold_evidence(row).path == PATH_STRUCTURED
    assert clause_evidence(clause).role == ROLE_POLICY_CLAUSE
    assert clause_evidence(clause, PATH_LEXICAL).path == PATH_LEXICAL


def test_空冲突列表不报错且归因结论为无冲突():
    assert attribute_conflicts("718-7", [], []) == []
    assert classify_conflicts([]) == CONFLICT_NONE
