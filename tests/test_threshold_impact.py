"""阈值表改版影响面与历史判定重算测试。

盯住四件事：

1. **口径比对只报真变化** —— 生效期迁移、单位写法归一（``10*9/L`` 与
   ``10^9/L``）都**不算**判定口径变化，不能把组长吓一跳；
2. **翻转值域算得准** —— 含开闭边界（``< 30`` 与 ``≤ 30`` 不是一回事）；
3. **历史重算能命中翻转**，而且**不改写历史台账** —— 台账逐字未变、
   指纹前后一致；
4. **复核清单落盘可读** —— 报告与翻转 CSV 能交人工处置。
"""

from __future__ import annotations

import csv
import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from critval.judge.critical import (
    DECISION_CRITICAL,
    DECISION_NOT_CRITICAL,
    judge_event,
)
from critval.judge.impact import (
    Band,
    CHANGE_ADDED,
    CHANGE_MODIFIED,
    CHANGE_REMOVED,
    FIELD_DIRECTION,
    FIELD_LOW,
    FIELD_UNIT,
    build_impact_report,
    diff_rulesets,
    load_ruleset,
    recompute_history,
    write_flip_csv,
    write_report,
)
from critval.judge.ruleset import RuleSet
from critval.ledger.idempotency import event_id_for_key, idempotency_key_of
from critval.ledger.store import LedgerStore
from critval.models import Citation, CriticalEvent, ThresholdRow

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SAMPLE_OLD = PROJECT_ROOT / "samples" / "threshold" / "示例危急值阈值表.xlsx"
SAMPLE_NEW = PROJECT_ROOT / "samples" / "threshold" / "示例危急值阈值表_v2.xlsx"

#: 口径比对统一用这个基准时刻（两版阈值表在该时刻都已生效）。
AT = date(2026, 10, 30)


# ---------------------------------------------------------------------------
# 造料
# ---------------------------------------------------------------------------


def _row(
    item_code: str = "718-7",
    item_name: str = "血红蛋白",
    unit: str = "g/L",
    low: str | None = "30",
    high: str | None = "200",
    *,
    low_inclusive: bool = False,
    high_inclusive: bool = False,
    version: str = "测试 v1.0",
    effective_from: str | None = "2026-01-01",
    effective_to: str | None = None,
    sex: str = "",
    age_min: int | None = None,
    age_max: int | None = None,
    age_label: str = "",
    line_no: int = 2,
    source: str = "样例阈值表.xlsx",
) -> ThresholdRow:
    """造一行阈值行（用于口径比对的边界用例）。"""
    return ThresholdRow(
        item_code=item_code,
        item_name=item_name,
        unit=unit,
        low_critical=Decimal(low) if low is not None else None,
        high_critical=Decimal(high) if high is not None else None,
        reference_range_raw="",
        effective_from=date.fromisoformat(effective_from) if effective_from else None,
        version=version,
        citation=Citation(
            source_name=source,
            source_sha256="0" * 64,
            line_no=line_no,
            file_version=version,
        ),
        row_no=line_no,
        raw_low=low or "",
        raw_high=high or "",
        low_inclusive=low_inclusive,
        high_inclusive=high_inclusive,
        effective_to=date.fromisoformat(effective_to) if effective_to else None,
        applies_sex=sex,
        applies_age_min=age_min,
        applies_age_max=age_max,
        applies_age_label=age_label,
    )


def _ruleset(rows: list[ThresholdRow], snapshot_id: str = "rs-0001-testtesttest") -> RuleSet:
    return RuleSet.compile(
        rows,
        snapshot_id=snapshot_id,
        source_name="样例阈值表.xlsx",
        source_sha256="0" * 64,
    )


@pytest.fixture(scope="module")
def old_ruleset() -> RuleSet:
    return load_ruleset(SAMPLE_OLD, label="old")


@pytest.fixture(scope="module")
def new_ruleset() -> RuleSet:
    return load_ruleset(SAMPLE_NEW, label="new")


def _seed_history(
    store: LedgerStore,
    ruleset: RuleSet,
    specs: list[dict],
) -> list[str]:
    """按**旧口径**判定并落台账，返回落库的 event_id 列表。"""
    event_ids: list[str] = []
    for spec in specs:
        observed_at = datetime.fromisoformat(spec["observed_at"])
        event = CriticalEvent(
            event_id="",
            message_control_id=spec["mcid"],
            specimen_no=spec.get("specimen_no", "SPEC-0001"),
            item_code=spec["item_code"],
            item_name=spec.get("item_name", ""),
            value=Decimal(spec["value"]),
            unit=spec.get("unit", ""),
            observed_at=observed_at,
            dept_code=spec.get("dept_code", "EMERG"),
            obx_flag=spec.get("obx_flag", ""),
        )
        key = idempotency_key_of(event)
        event = CriticalEvent(**{**event.__dict__, "event_id": event_id_for_key(key)})
        store.record_event(event)
        judgement = judge_event(event, rows=(), ruleset=ruleset)
        store.record_judgement(event.event_id, judgement)
        event_ids.append(event.event_id)
    return event_ids


#: 一批历史事件：既有会翻转的，也有一致的，还含判不出与新判出的。
HISTORY: list[dict] = [
    # 钾 2.6：旧口径 < 2.8 判危急，新口径 < 2.5 不判危急 → 翻转（旧危急→新不危急）
    {
        "mcid": "MSG-K-01",
        "item_code": "2823-3",
        "item_name": "钾",
        "value": "2.6",
        "unit": "mmol/L",
        "observed_at": "2026-03-01T08:15:00",
        "dept_code": "EMERG",
    },
    # 白细胞 32：旧 > 30 危急，新 > 35 不危急 → 翻转
    {
        "mcid": "MSG-W-01",
        "item_code": "6690-2",
        "item_name": "白细胞计数",
        "value": "32",
        "unit": "10*9/L",
        "observed_at": "2026-03-01T09:00:00",
        "dept_code": "EMERG",
    },
    # 血红蛋白 250：旧 > 200 危急，新版只有低阈值 → 翻转
    {
        "mcid": "MSG-H-01",
        "item_code": "718-7",
        "item_name": "血红蛋白",
        "value": "250",
        "unit": "g/L",
        "observed_at": "2026-03-01T10:30:00",
        "dept_code": "ICU",
    },
    # 血红蛋白 25：两版都不危急 → 结论一致
    {
        "mcid": "MSG-H-02",
        "item_code": "718-7",
        "item_name": "血红蛋白",
        "value": "25",
        "unit": "g/L",
        "observed_at": "2026-03-01T11:00:00",
        "dept_code": "ICU",
    },
    # 白细胞 40：两版都危急 → 结论一致
    {
        "mcid": "MSG-W-02",
        "item_code": "6690-2",
        "item_name": "白细胞计数",
        "value": "40",
        "unit": "10*9/L",
        "observed_at": "2026-03-02T09:00:00",
        "dept_code": "EMERG",
    },
    # 血红蛋白 25，但观察时刻落在新版生效之前 → 新口径判不出（版本空档）
    {
        "mcid": "MSG-H-03",
        "item_code": "718-7",
        "item_name": "血红蛋白",
        "value": "25",
        "unit": "g/L",
        "observed_at": "2026-01-15T11:00:00",
        "dept_code": "ICU",
    },
    # 镁：旧版有口径、新版删了 → 新口径判不出
    {
        "mcid": "MSG-M-01",
        "item_code": "2601-3",
        "item_name": "镁",
        "value": "3.1",
        "unit": "mmol/L",
        "observed_at": "2026-03-01T12:00:00",
        "dept_code": "EMERG",
    },
    # 高敏 CRP：旧版没有这条口径 → 旧判不出，新口径判出
    {
        "mcid": "MSG-C-01",
        "item_code": "1988-5",
        "item_name": "高敏C反应蛋白",
        "value": "150",
        "unit": "mg/L",
        "observed_at": "2026-03-01T13:00:00",
        "dept_code": "ICU",
    },
]


@pytest.fixture()
def seeded_store(tmp_path, old_ruleset):
    """建一个装了历史事件的台账。"""
    db_path = tmp_path / "impact.db"
    store = LedgerStore(db_path)
    _seed_history(store, old_ruleset, HISTORY)
    yield store
    store.close()


# ---------------------------------------------------------------------------
# 一、口径比对
# ---------------------------------------------------------------------------


def test_阈值变化被检出(old_ruleset, new_ruleset):
    diff = diff_rulesets(old_ruleset, new_ruleset, at=AT)
    item = next(i for i in diff.items if i.item_code == "2823-3")
    assert item.change == CHANGE_MODIFIED
    assert item.item_name == "钾"
    fields = {change.field for change in item.changes}
    assert FIELD_LOW in fields
    low = next(c for c in item.changes if c.field == FIELD_LOW)
    assert low.old_value == "< 2.8"
    assert low.new_value == "< 2.5"


def test_危急方向变化被检出(old_ruleset, new_ruleset):
    diff = diff_rulesets(old_ruleset, new_ruleset, at=AT)
    item = next(i for i in diff.items if i.item_code == "718-7")
    fields = {change.field for change in item.changes}
    assert FIELD_DIRECTION in fields
    direction = next(c for c in item.changes if c.field == FIELD_DIRECTION)
    assert direction.old_value == "高低都判"
    assert direction.new_value == "只判低危"


def test_新增与删除口径分别被检出(old_ruleset, new_ruleset):
    diff = diff_rulesets(old_ruleset, new_ruleset, at=AT)
    added = next(i for i in diff.items if i.item_code == "1988-5")
    removed = next(i for i in diff.items if i.item_code == "2601-3")
    assert added.change == CHANGE_ADDED
    assert added.item_name == "高敏C反应蛋白"
    assert removed.change == CHANGE_REMOVED
    assert removed.item_name == "镁"


def test_口径没变的项目不进清单(old_ruleset, new_ruleset):
    diff = diff_rulesets(old_ruleset, new_ruleset, at=AT)
    changed_codes = {item.item_code for item in diff.items}
    assert "789-8" not in changed_codes  # 红细胞压积两版一致
    assert "2951-2" not in changed_codes  # 钠两版一致
    assert "789-8" in diff.unchanged_items


def test_生效期迁移不算口径变化():
    """只换版本号与生效期、阈值一字未改 → 不算口径变化。"""
    old = _ruleset([_row(version="测试 v1.0", effective_from="2026-01-01")])
    new = _ruleset(
        [_row(version="测试 v2.0", effective_from="2026-02-05", source="样例阈值表_v2.xlsx")]
    )
    diff = diff_rulesets(old, new, at=AT)
    assert diff.items == ()


def test_单位写法归一等价不算口径变化但有提示():
    """``10*9/L`` 与 ``10^9/L`` 是同一个量级 —— 不能被读成口径变更。"""
    old = _ruleset([_row(item_code="777-3", item_name="血小板计数", unit="10*9/L", low="20", high="1000")])
    new = _ruleset(
        [_row(item_code="777-3", item_name="血小板计数", unit="10^9/L", low="20", high="1000")]
    )
    diff = diff_rulesets(old, new, at=AT)
    assert diff.items == ()
    assert len(diff.notation_notes) == 1
    assert "判定口径不变" in diff.notation_notes[0]


def test_单位真变化被检出():
    old = _ruleset([_row(unit="mg/dL", low="10", high="50")])
    new = _ruleset([_row(unit="g/L", low="0.1", high="0.5")])
    diff = diff_rulesets(old, new, at=AT)
    item = diff.items[0]
    assert item.change == CHANGE_MODIFIED
    unit_change = next(c for c in item.changes if c.field == FIELD_UNIT)
    assert unit_change.old_value == "mg/dL"
    assert unit_change.new_value == "g/L"
    # 单位不同 → 值域不可比，必须说清楚而不是瞎算
    assert item.band is not None
    assert item.band.available is False
    assert "须人工核对" in item.band.note


def test_人群分档变化被检出且不算成新增加删除():
    old = _ruleset([_row(item_code="2345-7", item_name="葡萄糖")])
    new = _ruleset([_row(item_code="2345-7", item_name="葡萄糖", sex="男", age_min=0, age_max=120, age_label="0-120 岁")])
    diff = diff_rulesets(old, new, at=AT)
    assert len(diff.items) == 1
    item = diff.items[0]
    assert item.change == CHANGE_MODIFIED
    assert item.band is not None
    assert item.band.available is False
    assert "适用人群分档不同" in item.band.note


def test_翻转值域边界准确(old_ruleset, new_ruleset):
    """钾的低危值 2.8 → 2.5：``[2.5, 2.8)`` 这一段结论会翻转。"""
    diff = diff_rulesets(old_ruleset, new_ruleset, at=AT)
    item = next(i for i in diff.items if i.item_code == "2823-3")
    assert item.band is not None
    assert item.band.available is True
    assert len(item.band.no_longer_critical) == 1
    band = item.band.no_longer_critical[0]
    assert band.lo == Decimal("2.5") and band.lo_incl is True
    assert band.hi == Decimal("2.8") and band.hi_incl is False
    assert band.contains(Decimal("2.5")) is True
    assert band.contains(Decimal("2.8")) is False


def test_翻转值域与实际判定口径一致(old_ruleset, new_ruleset):
    """值域里的取值，重算后结论必须真的翻转 —— 值域不是纸上谈兵。"""
    diff = diff_rulesets(old_ruleset, new_ruleset, at=AT)
    item = next(i for i in diff.items if i.item_code == "6690-2")
    band = item.band.no_longer_critical[0]
    assert band.contains(Decimal("32")) is True
    assert band.contains(Decimal("35")) is True  # 35 在旧口径危急、新口径不危急
    assert band.contains(Decimal("30")) is False
    assert band.contains(Decimal("36")) is False


def test_口径一致的项目没有翻转值域(old_ruleset, new_ruleset):
    diff = diff_rulesets(old_ruleset, new_ruleset, at=AT)
    assert all(item.band is None or item.band.empty is False for item in diff.items)
    unchanged = [i for i in diff.items if i.item_code == "789-8"]
    assert unchanged == []


def test_值域文本含无穷端时开闭写法正确():
    band = Band(lo=None, lo_incl=False, hi=Decimal("0.4"), hi_incl=False)
    assert band.describe() == "(-∞, 0.4)"
    upper = Band(lo=Decimal("100"), lo_incl=False, hi=None, hi_incl=False)
    assert upper.describe() == "(100, +∞)"
    closed = Band(lo=Decimal("2.5"), lo_incl=True, hi=Decimal("2.8"), hi_incl=False)
    assert closed.describe() == "[2.5, 2.8)"


def test_无穷端值域仍能判定包含关系():
    band = Band(lo=None, lo_incl=False, hi=Decimal("0.4"), hi_incl=False)
    assert band.contains(Decimal("-5")) is True
    assert band.contains(Decimal("0.4")) is False


# ---------------------------------------------------------------------------
# 二、历史判定重算
# ---------------------------------------------------------------------------


def test_重算命中结论翻转(seeded_store, new_ruleset):
    result = recompute_history(seeded_store, new_ruleset)
    flipped = {event.event_id: event for event in result.flipped}
    assert len(flipped) == 3
    kinds = {event.flip_kind for event in flipped.values()}
    assert kinds == {"no_longer_critical"}
    for event in flipped.values():
        assert event.old_decision == DECISION_CRITICAL
        assert event.new_decision == DECISION_NOT_CRITICAL


def test_翻转清单逐条给出旧依据与新依据(seeded_store, new_ruleset):
    result = recompute_history(seeded_store, new_ruleset)
    event = next(e for e in result.flipped if e.item_code == "2823-3")
    assert event.value == "2.6"
    assert event.unit == "mmol/L"
    assert event.dept_code == "EMERG"
    assert event.old_basis.startswith("示例危急值阈值表.xlsx")
    assert event.new_basis.startswith("示例危急值阈值表_v2.xlsx")
    assert event.new_reason


def test_结论一致的事件不进翻转清单(seeded_store, new_ruleset):
    result = recompute_history(seeded_store, new_ruleset)
    flipped_codes = {event.item_code for event in result.flipped}
    assert "718-7" in flipped_codes  # 血红蛋白 250 确实翻转了
    assert result.unchanged >= 2  # 血红蛋白 25 与白细胞 40 结论一致


def test_新口径判不出的事件落待复核清单(seeded_store, new_ruleset):
    result = recompute_history(seeded_store, new_ruleset)
    codes = {event.item_code for event in result.undecidable}
    assert "2601-3" in codes  # 新版删掉了镁这条口径
    assert "718-7" in codes  # 观察时刻落在新版生效之前（版本空档）
    for event in result.undecidable:
        assert event.reason


def test_旧口径判不出新口径判出单独计数(seeded_store, new_ruleset):
    result = recompute_history(seeded_store, new_ruleset)
    assert len(result.newly_decided) == 1
    event = result.newly_decided[0]
    assert event.item_code == "1988-5"
    assert event.new_decision == DECISION_CRITICAL
    assert event.old_decision not in (DECISION_CRITICAL, DECISION_NOT_CRITICAL)


def test_重算后历史台账逐字未变(seeded_store, new_ruleset):
    before = seeded_store.list_events(limit=1000)
    audit_before = seeded_store.audit_entries()
    count_before = seeded_store.count_events()

    recompute_history(seeded_store, new_ruleset)

    after = seeded_store.list_events(limit=1000)
    assert after == before
    assert seeded_store.audit_entries() == audit_before
    assert seeded_store.count_events() == count_before


def test_台账指纹前后一致(seeded_store, new_ruleset):
    result = recompute_history(seeded_store, new_ruleset)
    assert result.ledger_unchanged is True
    assert result.ledger_before == result.ledger_after
    assert result.ledger_before


def test_时间窗只取窗口内的事件(seeded_store, new_ruleset):
    result = recompute_history(
        seeded_store,
        new_ruleset,
        observed_from="2026-03-01",
        observed_to="2026-03-01",
    )
    assert result.scanned == 6  # 落在 2026-03-01 的六条
    assert all(event.observed_at.startswith("2026-03-01") for event in result.flipped)


def test_科室过滤只取该科室的事件(seeded_store, new_ruleset):
    result = recompute_history(seeded_store, new_ruleset, dept_code="ICU")
    assert result.dept_code == "ICU"
    assert result.scanned == 4
    assert all(stat.dept_code == "ICU" for stat in result.dept_stats)


def test_科室统计逐科室给出计数(seeded_store, new_ruleset):
    result = recompute_history(seeded_store, new_ruleset)
    stats = {stat.dept_code: stat for stat in result.dept_stats}
    assert set(stats) == {"EMERG", "ICU"}
    assert stats["ICU"].flipped == 1
    assert stats["EMERG"].flipped == 2
    assert sum(stat.scanned for stat in result.dept_stats) == result.scanned


def test_无可复核事件时清单为空(seeded_store, new_ruleset):
    result = recompute_history(seeded_store, new_ruleset, observed_from="2099-01-01")
    assert result.scanned == 0
    assert result.flipped == ()
    assert result.undecidable == ()
    assert result.dept_stats == ()


# ---------------------------------------------------------------------------
# 三、报告与 CLI
# ---------------------------------------------------------------------------


def test_报告含变化项目表与翻转清单与不改写声明(seeded_store, old_ruleset, new_ruleset):
    report = build_impact_report(
        old_ruleset,
        new_ruleset,
        seeded_store,
        at=AT,
    )
    text = report.render_markdown()
    assert "## 一、判定口径变化项目清单" in text
    assert "## 二、历史判定重算（不改写历史事实）" in text
    assert "## 三、涉及科室" in text
    assert "2823-3" in text and "钾" in text
    assert "结论翻转事件清单" in text
    assert "未改写历史台账" in text
    assert "台账指纹" in text


def test_报告与翻转CSV落盘(tmp_path, seeded_store, old_ruleset, new_ruleset):
    report = build_impact_report(old_ruleset, new_ruleset, seeded_store, at=AT)
    md_path = write_report(report, tmp_path / "threshold_impact.md")
    csv_path = write_flip_csv(report.recompute.flipped, tmp_path / "flipped.csv")
    assert md_path.exists() and md_path.stat().st_size > 0
    assert csv_path.exists()
    with open(csv_path, encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.reader(handle))
    assert rows[0][0] == "事件ID"
    assert len(rows) == 4  # 表头 + 3 条翻转


def test_CLI_impact_check可运行且JSON可解析(tmp_path, seeded_store, old_ruleset, new_ruleset):
    from critval.cli import main as cli_main

    db_path = tmp_path / "cli.db"
    seeded_store.close()
    store = LedgerStore(db_path)
    _seed_history(store, old_ruleset, HISTORY)
    store.close()

    out = tmp_path / "impact.md"
    csv_out = tmp_path / "flipped.csv"
    exit_code = cli_main(
        [
            "impact-check",
            "--old",
            str(SAMPLE_OLD),
            "--new",
            str(SAMPLE_NEW),
            "--db",
            str(db_path),
            "--at",
            AT.isoformat(),
            "--out",
            str(out),
            "--csv",
            str(csv_out),
            "--json",
        ]
    )
    assert exit_code == 0
    assert out.exists()
    assert csv_out.exists()


def test_CLI_impact_check输出可解析(tmp_path, capsys, old_ruleset, new_ruleset):
    from critval.cli import main as cli_main

    exit_code = cli_main(
        [
            "impact-check",
            "--old",
            str(SAMPLE_OLD),
            "--new",
            str(SAMPLE_NEW),
            "--at",
            AT.isoformat(),
            "--out",
            str(tmp_path / "impact.md"),
            "--csv",
            str(tmp_path / "flipped.csv"),
            "--json",
        ]
    )
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["diff"]["summary"]["changed_item_count"] == 5
    assert payload["old"]["source_name"] == "示例危急值阈值表.xlsx"
    assert payload["new"]["source_name"] == "示例危急值阈值表_v2.xlsx"


def test_两版口径完全一致时无变化也无翻转(tmp_path, old_ruleset, seeded_store):
    """同一份表跟自己比 —— 不该报出任何变化（防「空转也报一堆」）。"""
    diff = diff_rulesets(old_ruleset, old_ruleset, at=AT)
    assert diff.items == ()
    result = recompute_history(seeded_store, old_ruleset)
    assert result.flipped == ()
    assert result.unchanged == 7
    # 旧表里没有高敏 CRP 这条口径 —— 它在新旧两版下都判不出，不算翻转
    assert [event.item_code for event in result.undecidable] == ["1988-5"]
