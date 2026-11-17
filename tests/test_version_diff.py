"""阈值表多版本共存、生效期切换、跨版本拒出卡与改版影响面（任务 id 26）。

覆盖四条主线：

1. **多版本共存** —— 同一项目的新旧两版同时在目录里，按观察时刻择一；
2. **版本空档** —— 观察时刻落在两版之间时不取就近版本，转人工；
3. **跨版本拒出卡** —— 依据口径自相矛盾的处置卡出不来；
4. **改版影响面** —— 新旧两版逐项对齐，给组长一张可复核的变更清单。
"""

from __future__ import annotations

import csv
import json
import subprocess
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from critval.card.builder import build_card
from critval.judge.critical import judge_event, requires_human_review
from critval.kb.consistency import (
    VERSION_CONSISTENT,
    VERSION_MISMATCH,
    VERSION_UNVERIFIABLE,
    VersionBinding,
    VersionMismatchError,
    check_version_consistency,
    load_bindings,
)
from critval.kb.loader import load_threshold_table
from critval.kb.version import (
    VERSION_ACTIVE,
    VERSION_DRAFT,
    VERSION_EXPIRED,
    build_versions,
    select_version,
)
from critval.kb.version_diff import (
    CHANGE_ADDED,
    CHANGE_DIRECTION,
    CHANGE_REMOVED,
    CHANGE_THRESHOLD,
    CHANGE_UNIT,
    diff_threshold_tables,
    summarize,
    write_csv,
)
from critval.models import Citation, CriticalEvent, PolicyClause

ROOT = Path(__file__).resolve().parent.parent
V1_PATH = ROOT / "samples" / "threshold" / "示例危急值阈值表.xlsx"
V2_PATH = ROOT / "samples" / "threshold" / "示例危急值阈值表_v2.xlsx"
COEXIST_PATH = ROOT / "samples" / "threshold" / "示例危急值阈值表_改版共存.xlsx"


@pytest.fixture(scope="module")
def coexist_table():
    return load_threshold_table(COEXIST_PATH)


@pytest.fixture(scope="module")
def v1_table():
    return load_threshold_table(V1_PATH)


@pytest.fixture(scope="module")
def v2_table():
    return load_threshold_table(V2_PATH)


def make_event(observed_at: datetime, **kwargs) -> CriticalEvent:
    payload = {
        "event_id": "evt-v26",
        "message_control_id": "MSGV26001",
        "specimen_no": "SPEC-V26",
        "item_code": "718-7",
        "item_name": "血红蛋白",
        "value": Decimal("25"),
        "unit": "g/L",
        "observed_at": observed_at,
        "dept_code": "ICU",
        "patient_ref": "PATREF-V26",
    }
    payload.update(kwargs)
    return CriticalEvent(**payload)


def policy_clause(version: str = "示例 v1.0") -> PolicyClause:
    return PolicyClause(
        section_title="第三章 报告流程与通知时限",
        body="第七条 检验科发现危急值后应当班复核……",
        notify_target="检验科值班技师与检验科组长",
        deadline_text="10 分钟内",
        deadline_minutes=10,
        record_destination="危急值登记系统，并同步记入检验科危急值台账",
        citation=Citation(
            source_name="示例危急值报告制度.docx",
            source_sha256="b" * 64,
            kind="policy_clause",
            paragraph_no=19,
            section_path=("第三章 报告流程与通知时限",),
            line_no=19,
            file_version=version,
        ),
        clause_no="第七条",
    )


# ---------------------------------------------------------------------------
# 1) 多版本共存与生效期择一
# ---------------------------------------------------------------------------


def test_同项目多版本共存时按观察时刻择一(coexist_table):
    versions = build_versions(coexist_table.rows)
    assert {v.version_no for v in versions} == {"示例 v1.0", "示例 v2.0"}

    old = select_version(versions, "718-7", date(2026, 1, 15))
    assert old.status == "selected"
    assert old.version.version_no == "示例 v1.0"
    assert old.version.effective_to == date(2026, 1, 31)

    new = select_version(versions, "718-7", date(2026, 3, 1))
    assert new.status == "selected"
    assert new.version.version_no == "示例 v2.0"
    assert new.version.effective_from == date(2026, 2, 5)


def test_版本状态随观察时刻变化(coexist_table):
    versions = build_versions(coexist_table.rows)
    v1 = next(v for v in versions if v.version_no == "示例 v1.0")
    v2 = next(v for v in versions if v.version_no == "示例 v2.0")

    assert v1.state_at(date(2026, 1, 15)) == VERSION_ACTIVE
    assert v1.state_at(date(2026, 2, 10)) == VERSION_EXPIRED
    assert v2.state_at(date(2026, 2, 1)) == VERSION_DRAFT
    assert v2.state_at(date(2026, 2, 10)) == VERSION_ACTIVE


def test_版本清单带指纹与项目覆盖数(coexist_table):
    versions = build_versions(coexist_table.rows)
    for version in versions:
        assert version.source_sha256 == coexist_table.source_sha256
        assert version.source_name == coexist_table.source_name
        assert version.item_codes
        if version.effective_from is not None:
            assert version.effective_from.isoformat() in version.period_text


# ---------------------------------------------------------------------------
# 2) 版本空档 → 转人工，不取就近版本
# ---------------------------------------------------------------------------


def test_观察时刻落在版本空档时不取就近版本(coexist_table):
    versions = build_versions(coexist_table.rows)
    selection = select_version(versions, "718-7", date(2026, 2, 3))

    assert selection.status == "gap"
    assert selection.version is None
    assert selection.needs_human is True
    assert "空档" in selection.reason
    assert "不取就近版本" in selection.reason
    assert "2026-01-31" in selection.reason
    assert "2026-02-05" in selection.reason


def test_两版同时生效时判定为口径冲突(v1_table, v2_table):
    # v1 没有失效日期 → 与 v2 同时覆盖 2026-03-01，口径互斥。
    versions = build_versions(list(v1_table.rows) + list(v2_table.rows))
    selection = select_version(versions, "718-7", date(2026, 3, 1))

    assert selection.status == "conflict"
    assert selection.version is None
    assert "互斥" in selection.reason
    assert "转人工" in selection.reason


def test_未收录项目时无可选版本(coexist_table):
    versions = build_versions(coexist_table.rows)
    selection = select_version(versions, "0000-0", date(2026, 3, 1))
    assert selection.status == "no_match"
    assert selection.version is None


def test_判定引擎在版本空档时转人工(coexist_table):
    """产品口径：空档里的结果不自动判定，落转人工出口。"""
    event = make_event(datetime(2026, 2, 3, 8, 30))
    judgement = judge_event(event, coexist_table.rows)

    assert judgement.decision == "no_threshold"
    assert requires_human_review(judgement.decision) is True
    assert "空档" in judgement.reason
    assert judgement.threshold_row is None


def test_空档之外按新版判定并记录版本(coexist_table):
    event = make_event(datetime(2026, 3, 1, 8, 30))
    judgement = judge_event(event, coexist_table.rows)

    assert judgement.decision == "critical"
    assert judgement.threshold_row is not None
    assert judgement.threshold_row.version == "示例 v2.0"
    assert judgement.threshold_row.citation.line_no is not None


# ---------------------------------------------------------------------------
# 3) 跨版本拒出卡
# ---------------------------------------------------------------------------


def test_同一次处置混用同一类材料的两个版本时拒绝出卡(v1_table):
    event = make_event(datetime(2026, 1, 2, 8, 30))
    judgement = judge_event(event, v1_table.rows)
    other_version = Citation(
        source_name="示例危急值阈值表_v2.xlsx",
        source_sha256="c" * 64,
        kind="threshold_row",
        line_no=2,
        file_version="示例 v2.0",
    )

    with pytest.raises(VersionMismatchError) as excinfo:
        build_card(
            event,
            judgement,
            policy_clause=policy_clause(),
            extra_citations=(other_version,),
        )
    assert excinfo.value.code == VERSION_MISMATCH
    assert "混用" in str(excinfo.value)


def test_版本绑定不符时拒绝出卡(v1_table):
    event = make_event(datetime(2026, 1, 2, 8, 30))
    judgement = judge_event(event, v1_table.rows)
    binding = VersionBinding(
        item_code="718-7",
        threshold_version="示例 v1.0",
        policy_version="示例 v1.0",
    )

    with pytest.raises(VersionMismatchError) as excinfo:
        build_card(
            event,
            judgement,
            policy_clause=policy_clause(version="示例 v2.0"),
            version_binding=binding,
        )
    assert excinfo.value.code == VERSION_MISMATCH
    assert "跨版本绑定不一致" in str(excinfo.value)
    assert excinfo.value.consistency.expected_threshold_version == "示例 v1.0"


def test_版本落在绑定对内时正常出卡(v1_table):
    event = make_event(datetime(2026, 1, 2, 8, 30))
    judgement = judge_event(event, v1_table.rows)
    binding = VersionBinding(
        item_code="718-7",
        threshold_version="示例 v1.0",
        policy_version="示例 v1.0",
    )

    card = build_card(
        event,
        judgement,
        policy_clause=policy_clause(),
        version_binding=binding,
    )
    assert card.card_id
    assert [element.key for element in card.elements] == [
        "threshold_basis",
        "notify_target",
        "deadline",
        "record_destination",
    ]


def test_两份材料版本号不同不被误判为跨版本(v1_table):
    """阈值表与制度各自编号合法 —— 没有绑定声明时只标注，不拦下。"""
    event = make_event(datetime(2026, 1, 2, 8, 30))
    judgement = judge_event(event, v1_table.rows)

    card = build_card(
        event,
        judgement,
        policy_clause=policy_clause(version="示例 v2.0"),
    )
    assert card.card_id

    consistency = check_version_consistency(
        [judgement.threshold_row.citation, policy_clause(version="示例 v2.0").citation]
    )
    assert consistency.ok is True
    assert consistency.code == VERSION_UNVERIFIABLE
    assert consistency.needs_review is True


def test_统一发布号的院内可开严格比对(v1_table):
    event = make_event(datetime(2026, 1, 2, 8, 30))
    judgement = judge_event(event, v1_table.rows)

    with pytest.raises(VersionMismatchError):
        build_card(
            event,
            judgement,
            policy_clause=policy_clause(version="示例 v2.0"),
            strict_version_pair=True,
        )


def test_同一版本多行分档不算混版():
    citations = [
        Citation(source_name="表.xlsx", source_sha256="d" * 64, kind="threshold_row", line_no=2, file_version="示例 v1.0"),
        Citation(source_name="表.xlsx", source_sha256="d" * 64, kind="threshold_row", line_no=3, file_version="示例 v1.0"),
        Citation(source_name="制度.docx", source_sha256="e" * 64, kind="policy_clause", line_no=19, file_version="示例 v1.0"),
    ]
    consistency = check_version_consistency(citations)
    assert consistency.ok is True
    assert consistency.code == VERSION_CONSISTENT
    assert consistency.mixed_versions == ()


def test_缺版本号时标为待人工核对():
    citations = [
        Citation(source_name="表.xlsx", source_sha256="d" * 64, kind="threshold_row", line_no=2, file_version=None),
    ]
    consistency = check_version_consistency(citations)
    assert consistency.ok is True
    assert consistency.code == VERSION_UNVERIFIABLE
    assert consistency.needs_review is True


def test_版本绑定表可从JSON加载(tmp_path):
    path = tmp_path / "bindings.json"
    path.write_text(
        json.dumps(
            {
                "bindings": [
                    {
                        "item_code": "718-7",
                        "threshold_version": "示例 v1.0",
                        "policy_version": "示例 v1.0",
                        "note": "检验科 2026-01 确认",
                    },
                    {"item_code": "", "threshold_version": "x", "policy_version": "y"},
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    bindings = load_bindings(path)
    assert len(bindings) == 1
    assert bindings[0].item_code == "718-7"
    assert bindings[0].policy_version == "示例 v1.0"


def test_版本绑定表读不到时返回空绑定(tmp_path):
    assert load_bindings(tmp_path / "缺失.json") == ()


# ---------------------------------------------------------------------------
# 4) 改版影响面
# ---------------------------------------------------------------------------


def test_改版影响面清单覆盖五类变更(v1_table, v2_table):
    diffs = diff_threshold_tables(v1_table, v2_table)
    summary = summarize(diffs)
    counts = summary["counts"]

    assert counts[CHANGE_ADDED] == 1
    assert counts[CHANGE_REMOVED] == 1
    assert counts[CHANGE_THRESHOLD] == 3
    assert counts[CHANGE_DIRECTION] == 1
    assert counts[CHANGE_UNIT] == 1
    assert summary["changed_item_count"] == 6


def test_改版影响面逐项可核对(v1_table, v2_table):
    diffs = diff_threshold_tables(v1_table, v2_table)
    by_code: dict[str, list] = {}
    for diff in diffs:
        by_code.setdefault(diff.item_code, []).append(diff)

    added = by_code["1988-5"][0]
    assert added.change == CHANGE_ADDED
    assert added.new_line_no == 28
    assert added.old_line_no is None
    assert "新增兜底" in added.detail

    removed = by_code["2601-3"][0]
    assert removed.change == CHANGE_REMOVED
    assert removed.old_line_no == 10
    assert removed.new_line_no is None

    direction = next(d for d in by_code["718-7"] if d.change == CHANGE_DIRECTION)
    assert direction.old_value == "both"
    assert direction.new_value == "low"
    assert direction.old_line_no == 2
    assert direction.new_line_no == 2

    threshold = next(d for d in by_code["2823-3"] if d.change == CHANGE_THRESHOLD)
    assert "低 2.8" in threshold.old_value
    assert "低 2.5" in threshold.new_value

    unit = by_code["777-3"][0]
    assert unit.change == CHANGE_UNIT
    assert unit.old_value == "10*9/L"
    assert unit.new_value == "10^9/L"


def test_改版影响面清单写入CSV可复核(v1_table, v2_table, tmp_path):
    diffs = diff_threshold_tables(v1_table, v2_table)
    target = write_csv(diffs, tmp_path / "reports" / "threshold_version_diff.csv")
    assert target.exists()

    with open(target, encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.reader(handle))
    assert rows[0][:4] == ["项目码", "项目名", "变更类型", "变更说明"]
    assert len(rows) == len(diffs) + 1

    body = "\n".join(",".join(row) for row in rows[1:])
    assert "阈值变化" in body
    assert "单位变化" in body
    assert "方向变化" in body


def test_改版影响面命令行产出清单(tmp_path):
    out = tmp_path / "threshold_version_diff.csv"
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "diff_versions.py"),
            "--old",
            str(V1_PATH),
            "--new",
            str(V2_PATH),
            "--out",
            str(out),
        ],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
    )
    assert result.returncode == 0, result.stderr
    assert out.exists()
    assert "改版影响面" in result.stdout
    assert "阈值变化" in result.stdout
    assert "新增项目" in result.stdout


def test_改版影响面命令行JSON可解析(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "diff_versions.py"),
            "--old",
            str(V1_PATH),
            "--new",
            str(V2_PATH),
            "--out",
            str(tmp_path / "diff.csv"),
            "--json",
        ],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["summary"]["total"] == 7
    assert payload["old"]["versions"] == ["示例 v1.0"]
    assert payload["new"]["versions"] == ["示例 v2.0"]


# ---------------------------------------------------------------------------
# 5) 版本体检命令
# ---------------------------------------------------------------------------


def test_版本体检命令报出版本空档():
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "critval.cli",
            "version-check",
            "--threshold",
            str(COEXIST_PATH),
            "--at",
            "2026-02-03",
            "--json",
        ],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
    )
    assert result.returncode == 1  # 有空档 → 需要人工处理
    payload = json.loads(result.stdout)
    assert len(payload["versions"]) == 2
    assert payload["gaps"]
    assert "空档" in payload["gaps"][0]["reason"]
    assert payload["conflicts"] == []


def test_版本体检命令在生效时刻报唯一生效版本():
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "critval.cli",
            "version-check",
            "--threshold",
            str(COEXIST_PATH),
            "--at",
            "2026-03-01",
        ],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
    )
    assert result.returncode == 0, result.stdout
    assert "示例 v2.0" in result.stdout
    assert "版本空档（" not in result.stdout
    assert "当前生效分布：示例 v2.0" in result.stdout
    # 改版后停用的项目（2601-3 镁）没有生效版本 —— 报「无生效版本」而不是空档。
    assert "无生效版本" in result.stdout
    assert "2601-3" in result.stdout
    assert "停用" in result.stdout
