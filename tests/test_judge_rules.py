"""规则引擎测试：编译正确性、缺口清单、分档择一、生效期切换、互斥拒绝、快照落库。

规则引擎的两条底线：

1. **编不出规则的行不进判定集**，而是带行号与原因进缺口清单 —— 不静默跳过；
2. **择不出唯一规则就不判定**（人群上下文不足 / 版本空档 / 口径互斥），
   宁可转人工，也不拿半对的规则去判危急值。
"""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from critval.cli import main as cli_main
from critval.judge.critical import (
    DECISION_AMBIGUOUS,
    DECISION_CRITICAL,
    DECISION_NO_THRESHOLD,
    DECISION_NOT_CRITICAL,
    judge_event,
)
from critval.judge.registry import RuleRegistry
from critval.judge.rules import (
    GAP_DIRECTION_MISSING,
    GAP_MISSING_COLUMN,
    GAP_RANGE_REVERSED,
    GAP_SCOPE_UNPARSED,
    GAP_THRESHOLD_NOT_NUMERIC,
    compile_rule,
    compile_rules,
)
from critval.judge.ruleset import (
    SELECTION_CONFLICT,
    SELECTION_NO_MATCH,
    SELECTION_SELECTED,
    RuleSet,
    select_rule,
)
from critval.models import CriticalEvent, LedgerRecord

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "脱敏样例规则阈值表.xlsx"


@pytest.fixture(scope="module")
def table():
    from critval.kb.loader import load_threshold_table

    return load_threshold_table(FIXTURE)


@pytest.fixture(scope="module")
def ruleset(table):
    return RuleSet.compile(
        table.rows,
        snapshot_id="rs-0001-testtesttest",
        source_name=table.source_name,
        source_sha256=table.source_sha256,
    )


def make_event(
    item_code: str = "718-7",
    value: str = "25",
    unit: str = "g/L",
    when: datetime | None = None,
) -> CriticalEvent:
    return CriticalEvent(
        event_id="evt-rule-0001",
        message_control_id="MSG-RULE-0001",
        specimen_no="SPEC-RULE-0001",
        item_code=item_code,
        item_name="样例项目",
        value=Decimal(value),
        unit=unit,
        observed_at=when or datetime(2026, 1, 10, 8, 30),
        dept_code="ICU",
    )


# ---- 编译正确性 ----------------------------------------------------------


def test_阈值行编译成可执行规则(table):
    row = table.first("718-7")
    rule, gap = compile_rule(row)
    assert gap is None
    assert rule is not None
    assert rule.item_code == "718-7"
    assert rule.direction == "both"
    assert rule.low_critical == Decimal("30")
    assert rule.high_critical == Decimal("200")
    assert rule.unit == "g/L"
    assert rule.version == "测试 v1.0"
    assert rule.effective_from == date(2026, 1, 1)
    assert rule.citation.line_no == 2  # 出处带可读行号
    assert rule.citation.source_sha256 == table.source_sha256
    assert rule.rule_id == "718-7#r2@测试 v1.0"


def test_裸数字阈值默认严格不等(table):
    """院内习惯：写「低于 30」即等于 30 不算危急。"""
    rule, _ = compile_rule(table.first("718-7"))
    assert rule.low_inclusive is False
    assert rule.high_inclusive is False


def test_带比较符的阈值取闭区间语义(table):
    """源表写 ≤20 / >=1000 时，等于阈值也算危急。"""
    rule, _ = compile_rule(table.first("777-3"))
    assert rule.low_inclusive is True
    assert rule.high_inclusive is True
    assert rule.low_critical == Decimal("20")
    assert rule.high_critical == Decimal("1000")


def test_适用人群分档被解析进规则(table):
    male = [r for r in table.by_item_code("2345-7") if r.applies_sex == "男"][0]
    rule, gap = compile_rule(male)
    assert gap is None
    assert rule.scope.sex == "男"
    assert rule.scope.age_min == 18
    assert rule.scope.age_max == 60
    assert rule.scope.specificity == 3  # 性别 2 分 + 年龄段 1 分


def test_规则集覆盖全部生效版本(ruleset):
    assert set(ruleset.versions) == {"测试 v1.0", "测试 v2.0", "测试 v0.9"}
    assert ruleset.item_codes


# ---- 缺口清单：编不出规则的行不静默跳过 ---------------------------------


def test_缺口清单覆盖五类归因(ruleset):
    reasons = {gap.reason for gap in ruleset.gaps}
    assert reasons == {
        GAP_DIRECTION_MISSING,
        GAP_THRESHOLD_NOT_NUMERIC,
        GAP_RANGE_REVERSED,
        GAP_MISSING_COLUMN,
        GAP_SCOPE_UNPARSED,
    }


def test_缺口带行号与原始内容(ruleset):
    by_reason = {gap.reason: gap for gap in ruleset.gaps}
    numeric = by_reason[GAP_THRESHOLD_NOT_NUMERIC]
    assert numeric.row_no == 9
    assert numeric.item_code == "6666-6"
    assert "危急" in numeric.detail
    assert numeric.values  # 保留原始单元格内容
    reversed_gap = by_reason[GAP_RANGE_REVERSED]
    assert reversed_gap.row_no == 10
    assert "区间反写" in reversed_gap.detail


def test_缺口行不进入判定集(ruleset):
    """编不出规则的行绝不能变成规则。"""
    gap_codes = {gap.item_code for gap in ruleset.gaps}
    rule_codes = {rule.item_code for rule in ruleset.rules}
    assert gap_codes == {"8888-8", "6666-6", "5555-5", "4444-4", "3333-3"}
    assert not (gap_codes & rule_codes)


def test_缺口行不参与判定(ruleset):
    """缺方向的行既没有规则，也没有可选规则 → 不判定。"""
    selection = select_rule(ruleset, "8888-8", date(2026, 1, 10))
    assert selection.status == SELECTION_NO_MATCH
    assert selection.rule is None


def test_编译结果行数守恒(table, ruleset):
    """每一行要么成为规则、要么进缺口清单 —— 两边加起来等于总行数。"""
    rules, gaps = compile_rules(table.rows)
    assert len(rules) + len(gaps) == len(table.rows)
    assert len(rules) == len(ruleset.rules)


# ---- 分档择一 ------------------------------------------------------------


def test_按性别择一(ruleset):
    male = select_rule(ruleset, "2345-7", date(2026, 1, 10), sex="男", age=30)
    female = select_rule(ruleset, "2345-7", date(2026, 1, 10), sex="女", age=30)
    assert male.status == SELECTION_SELECTED
    assert female.status == SELECTION_SELECTED
    assert male.rule.scope.sex == "男"
    assert female.rule.scope.sex == "女"
    assert male.rule.high_critical != female.rule.high_critical


def test_年龄段不匹配时不选该规则(ruleset):
    """70 岁不落在 18-60 的分档里。"""
    selection = select_rule(ruleset, "2345-7", date(2026, 1, 10), sex="男", age=70)
    assert selection.status == SELECTION_NO_MATCH
    assert "分档" in selection.reason


def test_性别未知时不硬套性别规则(ruleset):
    """上下文缺失 → 转人工，而不是拿某个性别的规则硬套。"""
    selection = select_rule(ruleset, "2345-7", date(2026, 1, 10), sex=None, age=30)
    assert selection.status == SELECTION_NO_MATCH
    assert selection.rule is None
    assert "转人工" in selection.reason


# ---- 生效期切换与版本空档 ------------------------------------------------


def test_生效期切换后用新版本规则(ruleset):
    before = select_rule(ruleset, "2345-7", date(2026, 1, 10), sex="男", age=30)
    after = select_rule(ruleset, "2345-7", date(2026, 2, 5), sex="男", age=30)
    assert before.rule.version == "测试 v1.0"
    assert after.rule.version == "测试 v2.0"
    assert after.rule.high_critical == Decimal("25.0")


def test_版本空档转人工(ruleset):
    """观察时刻落在两版本之间 → 不取就近版本，转人工。"""
    selection = select_rule(ruleset, "2823-3", date(2026, 1, 20))
    assert selection.status == SELECTION_NO_MATCH
    assert "空档" in selection.reason
    assert selection.rule is None


def test_生效期内正常命中(ruleset):
    selection = select_rule(ruleset, "2823-3", date(2026, 1, 10))
    assert selection.status == SELECTION_SELECTED
    assert selection.rule.version == "测试 v0.9"


# ---- 互斥拒绝 ------------------------------------------------------------


def test_同档互斥阈值拒绝自动判定(ruleset):
    selection = select_rule(ruleset, "2222-2", date(2026, 1, 10), sex="男", age=30)
    assert selection.status == SELECTION_CONFLICT
    assert selection.rule is None
    assert "互斥" in selection.reason
    assert len(selection.candidates) == 2


def test_互斥时判定结论为不判定(ruleset):
    judgement = judge_event(
        make_event(item_code="2222-2", value="3.0", unit="mmol/L"),
        [],
        sex="男",
        age=30,
        ruleset=ruleset,
    )
    assert judgement.decision == DECISION_AMBIGUOUS
    assert judgement.judged is False


# ---- 判定与快照落库 ------------------------------------------------------


def test_规则引擎判定按命中规则(ruleset):
    judgement = judge_event(
        make_event(item_code="718-7", value="25", unit="g/L"),
        [],
        ruleset=ruleset,
    )
    assert judgement.decision == DECISION_CRITICAL
    assert judgement.rule_id == "718-7#r2@测试 v1.0"
    assert "低于低危急值 30" in judgement.reason


def test_闭区间规则等于边界判危急(ruleset):
    """规则写 ≤20 时，20 就是危急值。"""
    judgement = judge_event(
        make_event(item_code="777-3", value="20", unit="10*9/L"),
        [],
        ruleset=ruleset,
    )
    assert judgement.decision == DECISION_CRITICAL
    assert "低于或等于低危急值 20" in judgement.reason


def test_严格不等规则等于边界不判危急(ruleset):
    judgement = judge_event(
        make_event(item_code="718-7", value="30", unit="g/L"),
        [],
        ruleset=ruleset,
    )
    assert judgement.decision == DECISION_NOT_CRITICAL


def test_判定记录规则快照ID(ruleset):
    """事件落库时记录所用规则快照 ID —— 事后复核能定位到当时的规则版本。"""
    judgement = judge_event(
        make_event(item_code="718-7", value="25", unit="g/L"),
        [],
        ruleset=ruleset,
    )
    assert judgement.ruleset_snapshot_id == ruleset.snapshot_id
    assert judgement.to_dict()["ruleset_snapshot_id"] == ruleset.snapshot_id
    record = LedgerRecord(event_id=judgement.item_code, ruleset_snapshot_id=judgement.ruleset_snapshot_id)
    assert record.to_dict()["ruleset_snapshot_id"] == ruleset.snapshot_id


def test_规则引擎下无适用规则不判定(ruleset):
    judgement = judge_event(
        make_event(item_code="0000-0", value="1", unit="g/L"),
        [],
        ruleset=ruleset,
    )
    assert judgement.decision == DECISION_NO_THRESHOLD
    assert judgement.judged is False


# ---- 规则集热加载 --------------------------------------------------------


def test_注册表装载后给出快照ID与摘要():
    RuleRegistry.reset()
    ruleset = RuleRegistry.init_config(FIXTURE)
    assert ruleset.snapshot_id.startswith("rs-0001-")
    assert RuleRegistry.snapshot_id() == ruleset.snapshot_id
    summary = RuleRegistry.summary()
    assert summary["rule_count"] == len(ruleset.rules)
    assert summary["gap_count"] == len(ruleset.gaps)
    assert summary["versions"]


def test_材料指纹未变则不换快照(tmp_path):
    import shutil

    target = tmp_path / "阈值表.xlsx"
    shutil.copy(FIXTURE, target)
    RuleRegistry.reset()
    RuleRegistry.init_config(target)
    before = RuleRegistry.snapshot_id()
    RuleRegistry.reload(target)
    assert RuleRegistry.snapshot_id() == before, "同一份材料反复重载不应冲淡版本号"


def test_材料变更后重编译并换快照(tmp_path):
    import shutil

    from openpyxl import load_workbook

    target = tmp_path / "阈值表.xlsx"
    shutil.copy(FIXTURE, target)
    RuleRegistry.reset()
    RuleRegistry.init_config(target)
    before = RuleRegistry.snapshot_id()
    before_rules = len(RuleRegistry.get_ruleset().rules)

    workbook = load_workbook(target)
    sheet = workbook.worksheets[0]
    sheet.append(["7777-7", "新增项目", "mmol/L", 1, 2, "1-2", "2026-01-01", None, "测试 v1.0", "", ""])
    workbook.save(target)

    ruleset = RuleRegistry.reload(target)
    assert RuleRegistry.snapshot_id() != before
    assert len(ruleset.rules) == before_rules + 1
    assert ruleset.snapshot_id.startswith("rs-0002-")
    assert "7777-7" in ruleset.item_codes


# ---- CLI 摘要 ------------------------------------------------------------


def test_cli_打印规则集摘要(capsys):
    code = cli_main(["rules-summary", "--threshold", str(FIXTURE)])
    out = capsys.readouterr().out
    assert code == 0
    assert "规则条数" in out
    assert "生效版本" in out
    assert "缺口条数" in out
    assert "规则集快照 ID" in out


def test_cli_输出可被json解析(capsys):
    code = cli_main(["rules-summary", "--threshold", str(FIXTURE), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["rule_count"] > 0
    assert payload["snapshot_id"].startswith("rs-")


def test_cli_热载入显示快照未变(capsys):
    code = cli_main(["rules-reload", "--threshold", str(FIXTURE)])
    out = capsys.readouterr().out
    assert code == 0
    assert "热载入前快照" in out
    assert "沿用原快照" in out
