"""判定可解释与判定回放测试（任务 id 36）。

盯住五件事：

1. **判定记录字段齐备** —— 结论、理由、依据出处、规则 ID、七段判定链都在；
2. **单位换算这一步看得见** —— 原值多少、换算后多少、精度几位，一段不缺；
3. **判定记录随事件落库**，可按事件 ID 取回；
4. **判定记录里没有患者可识别明细**（连不透明的 ``patient_ref`` 都不写）；
5. **回放与落库记录逐字段一致** —— 材料没换时能复算出来；材料换了要报差异，
   而不是悄悄给个新结论。
"""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from critval.cli import main as cli_main
from critval.judge.critical import DECISION_CRITICAL, judge_event
from critval.judge.explain import (
    REPLAY_COMPARE_FIELDS,
    STAGE_CONCLUSION,
    STAGE_ORDER,
    STAGE_RAW_VALUE,
    STAGE_RULE_SELECTION,
    STAGE_THRESHOLD_COMPARE,
    STAGE_UNIT_CONVERSION,
    JudgementRecord,
    PatientDetailLeak,
    assert_no_patient_detail,
    build_judgement_record,
    diff_records,
    event_from_row,
)
from critval.kb.loader import load_threshold_table
from critval.ledger.store import LedgerStore
from critval.models import Citation, CriticalEvent, ThresholdRow

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SAMPLE_THRESHOLD = PROJECT_ROOT / "samples" / "threshold" / "示例危急值阈值表.xlsx"
SAMPLE_THRESHOLD_V2 = PROJECT_ROOT / "samples" / "threshold" / "示例危急值阈值表_v2.xlsx"


# ---------------------------------------------------------------------------
# 造料
# ---------------------------------------------------------------------------


def _row(**overrides) -> ThresholdRow:
    """一条脱敏阈值行（血红蛋白）。"""
    base = {
        "item_code": "718-7",
        "item_name": "血红蛋白",
        "unit": "g/L",
        "low_critical": Decimal("30"),
        "high_critical": Decimal("200"),
        "reference_range_raw": "115-150",
        "effective_from": date(2026, 1, 1),
        "version": "示例 v1.0",
        "citation": Citation(
            source_name="示例危急值阈值表.xlsx",
            source_sha256="a" * 64,
            kind="threshold_row",
            line_no=2,
            file_version="示例 v1.0",
        ),
        "row_no": 2,
    }
    base.update(overrides)
    return ThresholdRow(**base)  # type: ignore[arg-type]


def _event(**overrides) -> CriticalEvent:
    """一条脱敏危急值事件。"""
    base = {
        "event_id": "evt-explain-0001",
        "message_control_id": "MCID-0001",
        "specimen_no": "SPEC-0001",
        "item_code": "718-7",
        "item_name": "血红蛋白",
        "value": Decimal("249.29"),
        "unit": "g/L",
        "observed_at": datetime(2026, 10, 30, 8, 30, 0),
        "dept_code": "神经内科",
        "obx_flag": "HH",
        "patient_ref": "pt-0123456789ab",
    }
    base.update(overrides)
    return CriticalEvent(**base)  # type: ignore[arg-type]


def _record_for(event: CriticalEvent, rows=None):
    judgement = judge_event(event, list(rows or [_row()]))
    return judgement, build_judgement_record(event, judgement)


# ---------------------------------------------------------------------------
# 1 · 判定记录字段齐备
# ---------------------------------------------------------------------------


def test_判定记录字段齐备且七段判定链按序排列():
    _, record = _record_for(_event())

    assert record.decision == DECISION_CRITICAL
    assert record.reason
    assert record.item_code == "718-7"
    assert record.value == "249.29"
    assert record.unit == "g/L"
    assert record.rule_id
    assert record.threshold_locator == "示例危急值阈值表.xlsx · 第 2 行 · 示例 v1.0"
    assert record.source_name == "示例危急值阈值表.xlsx"
    assert record.source_sha256 == "a" * 64
    assert record.source_line == 2
    assert record.file_version == "示例 v1.0"
    assert record.threshold_source_sha256 == "a" * 64

    assert [step.stage for step in record.steps] == list(STAGE_ORDER)
    for step in record.steps:
        assert step.step_no >= 1
        assert step.input, f"{step.stage} 段缺输入"
        assert step.output, f"{step.stage} 段缺输出"
        assert step.basis, f"{step.stage} 段缺依据"

    # 每段都能被机器读出来（JSON 化不炸）。
    payload = record.to_dict()
    assert json.loads(json.dumps(payload, ensure_ascii=False))
    assert len(payload["steps"]) == len(STAGE_ORDER)


def test_判定链带规则ID与来源行号可跳回原始材料():
    _, record = _record_for(_event())
    rule_step = record.step(STAGE_RULE_SELECTION)
    assert rule_step is not None
    assert rule_step.rule_id == record.rule_id
    assert rule_step.source_line == 2
    assert rule_step.source_sha256 == "a" * 64
    assert rule_step.output["危急方向"] == "both"

    compare_step = record.step(STAGE_THRESHOLD_COMPARE)
    assert compare_step is not None
    assert compare_step.output["阈值行"] == record.threshold_locator
    assert compare_step.output["高侧触发"] is True
    assert compare_step.output["低侧触发"] is False


def test_结论段写明结论理由与是否转人工():
    _, record = _record_for(_event())
    conclusion = record.step(STAGE_CONCLUSION)
    assert conclusion is not None
    assert conclusion.output["结论"] == DECISION_CRITICAL
    assert conclusion.output["理由"] == record.reason
    assert conclusion.output["需转人工"] is False


# ---------------------------------------------------------------------------
# 2 · 单位换算这一步看得见
# ---------------------------------------------------------------------------


def test_单位换算步骤可见且原值与换算值都留痕():
    event = _event(value=Decimal("25"), unit="g/dL")
    _, record = _record_for(event)

    step = record.step(STAGE_UNIT_CONVERSION)
    assert step is not None
    assert step.input["原始值"] == "25"
    assert step.input["原始单位"] == "g/dL"
    assert step.input["阈值单位"] == "g/L"
    assert step.output["换算值"] == "250"
    assert step.output["换算单位"] == "g/L"
    assert step.output["换算方式"] == "查表换算"
    assert step.output["精度位数"] == 2

    raw = record.step(STAGE_RAW_VALUE)
    assert raw is not None
    assert raw.output["原始值"] == "25"
    assert raw.output["单位"] == "g/dL"

    assert record.converted_value == "250"
    assert record.converted_unit == "g/L"


def test_同单位比较不谎报换算():
    _, record = _record_for(_event())
    step = record.step(STAGE_UNIT_CONVERSION)
    assert step is not None
    assert step.output["换算方式"] == "同单位比较"


def test_单位换不来时这一步写明不判定():
    event = _event(value=Decimal("3.2"), unit="mmol/L")
    judgement, record = _record_for(event)
    step = record.step(STAGE_UNIT_CONVERSION)
    assert step is not None
    assert step.output["换算值"] == ""
    assert step.output["换算方式"] == "未换算"
    assert "转人工" in step.note
    assert judgement.decision == "unit_unknown"


# ---------------------------------------------------------------------------
# 3 · 判定记录随事件落库
# ---------------------------------------------------------------------------


def test_判定记录随事件落库并可按事件ID取回(tmp_path):
    db_path = tmp_path / "explain.db"
    event = _event()
    judgement, record = _record_for(event)

    with LedgerStore(db_path) as store:
        assert store.record_event(event) is True
        store.record_judgement(event.event_id, judgement)
        store.record_judgement_record(record)
        assert store.judgement_record_count() == 1

        payload = store.get_judgement_record(event.event_id)
        assert payload is not None
        assert payload["decision"] == DECISION_CRITICAL
        assert payload["threshold_locator"] == record.threshold_locator
        assert len(payload["steps"]) == len(STAGE_ORDER)

        restored = JudgementRecord.from_dict(payload)
        assert restored.decision == record.decision
        assert restored.reason == record.reason
        assert [s.stage for s in restored.steps] == list(STAGE_ORDER)
        assert diff_records(record, restored) == []


def test_没有判定记录时按事件ID取回返回空(tmp_path):
    db_path = tmp_path / "empty.db"
    with LedgerStore(db_path) as store:
        assert store.get_judgement_record("evt-not-there") is None


# ---------------------------------------------------------------------------
# 4 · 不落患者可识别明细
# ---------------------------------------------------------------------------


def test_判定记录里没有患者可识别明细():
    event = _event(patient_ref="pt-0123456789ab")
    _, record = _record_for(event)
    payload = record.to_dict()
    flat = json.dumps(payload, ensure_ascii=False)

    assert "patient_ref" not in flat
    assert "pt-0123456789ab" not in flat
    for key in ("姓名", "身份证", "住院号"):
        assert key not in flat


def test_落库载荷里也没有患者可识别明细(tmp_path):
    db_path = tmp_path / "leak.db"
    event = _event(patient_ref="pt-0123456789ab")
    judgement, record = _record_for(event)
    with LedgerStore(db_path) as store:
        store.record_event(event)
        store.record_judgement(event.event_id, judgement)
        store.record_judgement_record(record)
        raw = db_path.read_bytes().decode("utf-8", errors="ignore")
    assert "pt-0123456789ab" not in raw


def test_患者明细守卫拦得住():
    with pytest.raises(PatientDetailLeak):
        assert_no_patient_detail({"decision": "critical", "patient_ref": "pt-x"})
    with pytest.raises(PatientDetailLeak):
        assert_no_patient_detail({"steps": [{"output": {"patient_name": "某某"}}]})
    # 正常载荷不误伤
    assert_no_patient_detail({"decision": "critical", "item_name": "血红蛋白"})


# ---------------------------------------------------------------------------
# 5 · 回放与落库记录逐字段比对
# ---------------------------------------------------------------------------


def test_回放与落库记录逐字段一致():
    _, stored = _record_for(_event())
    _, replayed = _record_for(_event())
    assert diff_records(stored, replayed) == []
    assert len(REPLAY_COMPARE_FIELDS) >= 20


def test_材料变了要报差异而不是悄悄换结论():
    _, stored = _record_for(_event())
    # 阈值行被改小 —— 同一个值不再危急。
    changed = _row(high_critical=Decimal("300"))
    _, replayed = _record_for(_event(), rows=[changed])
    differences = diff_records(stored, replayed)
    assert differences
    fields = {item["field"] for item in differences}
    assert "decision" in fields


def test_台账行能还原成规范化事件供回放复算():
    event = _event()
    row = {
        "event_id": event.event_id,
        "message_control_id": event.message_control_id,
        "specimen_no": event.specimen_no,
        "item_code": event.item_code,
        "item_name": event.item_name,
        "value": str(event.value),
        "unit": event.unit,
        "observed_at": event.observed_at.isoformat(),
        "dept_code": event.dept_code,
        "obx_flag": event.obx_flag,
        "result_status": event.result_status,
        "patient_ref": event.patient_ref,
        "source_channel": "hl7",
    }
    restored = event_from_row(row)
    assert restored.event_id == event.event_id
    assert restored.value == event.value
    assert restored.observed_at == event.observed_at


# ---------------------------------------------------------------------------
# 6 · CLI 回放命令
# ---------------------------------------------------------------------------


def _seed_ledger(db_path: Path, event: CriticalEvent, table) -> None:
    judgement = judge_event(event, list(table.rows))
    with LedgerStore(db_path) as store:
        store.record_event(event)
        store.record_judgement(event.event_id, judgement)
        store.record_judgement_record(
            build_judgement_record(event, judgement, threshold_source_sha256=table.source_sha256)
        )


def test_judge_replay输出五段且与落库记录一致(tmp_path, capsys):
    table = load_threshold_table(SAMPLE_THRESHOLD)
    event = _event(observed_at=datetime(2026, 10, 30, 8, 30, 0))
    db_path = tmp_path / "replay.db"
    _seed_ledger(db_path, event, table)

    code = cli_main(
        [
            "judge-replay",
            "--event",
            event.event_id,
            "--db",
            str(db_path),
            "--threshold",
            str(SAMPLE_THRESHOLD),
        ]
    )
    out = capsys.readouterr().out
    assert code == 0

    # 五段：原始值 / 换算值 / 命中规则 / 阈值行 / 结论
    assert "原始值" in out
    assert "换算值" in out
    assert "命中规则" in out
    assert "阈值行" in out
    assert "结论" in out
    assert "249.29" in out
    assert "示例危急值阈值表.xlsx · 第 2 行 · 示例 v1.0" in out
    assert "逐字段一致" in out
    # 七段判定链逐段打印
    for stage in STAGE_ORDER:
        assert stage in out
    assert "1/7" in out and "7/7" in out


def test_judge_replay_JSON输出可解析且带差异清单(tmp_path, capsys):
    table = load_threshold_table(SAMPLE_THRESHOLD)
    event = _event(observed_at=datetime(2026, 10, 30, 8, 30, 0))
    db_path = tmp_path / "replay_json.db"
    _seed_ledger(db_path, event, table)

    code = cli_main(
        [
            "judge-replay",
            "--event",
            event.event_id,
            "--db",
            str(db_path),
            "--threshold",
            str(SAMPLE_THRESHOLD),
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["consistent"] is True
    assert payload["differences"] == []
    assert payload["event_id"] == event.event_id
    assert payload["decision"] == DECISION_CRITICAL
    assert len(payload["replayed"]["steps"]) == len(STAGE_ORDER)
    assert payload["version_check"]["threshold_source_sha256"] == table.source_sha256


def test_judge_replay用错材料时报不一致(tmp_path, capsys):
    table = load_threshold_table(SAMPLE_THRESHOLD)
    event = _event(observed_at=datetime(2026, 10, 30, 8, 30, 0))
    db_path = tmp_path / "replay_diff.db"
    _seed_ledger(db_path, event, table)

    code = cli_main(
        [
            "judge-replay",
            "--event",
            event.event_id,
            "--db",
            str(db_path),
            "--threshold",
            str(SAMPLE_THRESHOLD_V2),
        ]
    )
    out = capsys.readouterr().out
    assert code == 1
    assert "不一致" in out
    assert "decision" in out


def test_judge_replay查不到事件或记录时报业务失败(tmp_path, capsys):
    table = load_threshold_table(SAMPLE_THRESHOLD)
    event = _event(observed_at=datetime(2026, 10, 30, 8, 30, 0))
    db_path = tmp_path / "replay_missing.db"
    _seed_ledger(db_path, event, table)

    code = cli_main(
        [
            "judge-replay",
            "--event",
            "evt-not-there",
            "--db",
            str(db_path),
            "--threshold",
            str(SAMPLE_THRESHOLD),
        ]
    )
    assert code == 1
    assert "没有事件" in capsys.readouterr().err

    # 事件在、判定记录不在（本功能上线前入库的旧事件）
    with LedgerStore(db_path) as store:
        store.record_event(
            _event(event_id="evt-old-0001", message_control_id="MCID-OLD-0001")
        )
    code = cli_main(
        [
            "judge-replay",
            "--event",
            "evt-old-0001",
            "--db",
            str(db_path),
            "--threshold",
            str(SAMPLE_THRESHOLD),
        ]
    )
    assert code == 1
    assert "没有判定记录" in capsys.readouterr().err
