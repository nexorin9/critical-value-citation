"""版本绑定表与制度改版影响面（任务 id 5）。

绑定表回答一个问题：**这个检验项目现在该配哪一版阈值表、哪一版制度**。
绑定一旦落库，召回就只认被绑版本 —— 未绑版本不出现在引用里，
引用里出现别的版本 = 值班人拿到一张口径不一致的处置卡。

另含制度改版 diff：按稳定标识逐条对齐，给医务科一张「哪几条改了」的清单。
"""

from __future__ import annotations

import csv
import json
import subprocess
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from critval.card.builder import build_card
from critval.kb.binding import (
    BindingTable,
    build_binding_table,
    load_binding_table,
    save_binding_table,
    unbound_versions,
    visible_citations,
    visible_policy_clauses,
    visible_threshold_rows,
)
from critval.kb.consistency import VersionBinding, VersionMismatchError
from critval.kb.loader import load_threshold_table
from critval.kb.policy import load_policy_document
from critval.kb.policy_diff import (
    CHANGE_ADDED,
    CHANGE_BODY,
    CHANGE_FACTS,
    CHANGE_REMOVED,
    diff_policy_documents,
    summarize,
    write_csv,
)
from critval.models import Citation, CriticalEvent, Judgement

ROOT = Path(__file__).resolve().parent.parent
COEXIST_TABLE = ROOT / "samples" / "threshold" / "示例危急值阈值表_改版共存.xlsx"
POLICY_V1 = ROOT / "tests" / "fixtures" / "脱敏样例制度.docx"
POLICY_V2 = ROOT / "tests" / "fixtures" / "脱敏样例制度_v2.docx"


@pytest.fixture(scope="module")
def policy_v1():
    return load_policy_document(POLICY_V1)


@pytest.fixture(scope="module")
def policy_v2():
    return load_policy_document(POLICY_V2)


@pytest.fixture(scope="module")
def coexist_table():
    return load_threshold_table(COEXIST_TABLE)


def binding(item_code: str = "718-7") -> VersionBinding:
    return VersionBinding(
        item_code=item_code,
        threshold_version="示例 v1.0",
        policy_version="测试 v1.0",
        note="检验科 2026-02 确认",
    )


# ---------------------------------------------------------------------------
# 1) 绑定表本身
# ---------------------------------------------------------------------------


def test_绑定表可从JSON加载(tmp_path):
    path = tmp_path / "版本绑定表.json"
    path.write_text(
        json.dumps(
            {
                "bindings": [
                    {
                        "item_code": "718-7",
                        "threshold_version": "示例 v1.0",
                        "policy_version": "测试 v1.0",
                        "note": "检验科 2026-02 确认",
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    table = load_binding_table(path)
    assert table.ok is True
    assert table.item_codes() == ("718-7",)
    found = table.for_item("718-7")
    assert found is not None
    assert found.threshold_version == "示例 v1.0"
    assert found.policy_version == "测试 v1.0"


def test_绑定表文件缺失时返回空绑定而不是报错(tmp_path):
    table = load_binding_table(tmp_path / "不存在.json")
    assert table.ok is False
    assert table.for_item("718-7") is None
    assert "为空" in table.summary_text()


def test_绑定表可写回JSON(tmp_path):
    table = BindingTable(bindings=(binding(),))
    path = save_binding_table(table, tmp_path / "绑定.json")
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["bindings"][0]["item_code"] == "718-7"
    assert load_binding_table(path).for_item("718-7").policy_version == "测试 v1.0"


def test_绑定表可由阈值行与制度自动推导(coexist_table, policy_v1):
    table = build_binding_table(coexist_table.rows, [policy_v1], date(2026, 2, 10))
    assert table.ok is True
    found = table.for_item("718-7")
    assert found is not None
    assert found.threshold_version == "示例 v2.0"  # 2026-02-10 落在 v2.0 生效期内
    assert found.policy_version == "测试 v1.0"
    assert "阈值表按" in found.note


def test_推导绑定表在版本空档时留空等人工声明(coexist_table, policy_v1):
    table = build_binding_table(coexist_table.rows, [policy_v1], date(2026, 2, 2))
    found = table.for_item("718-7")
    assert found is not None
    assert found.threshold_version == ""
    assert "需人工声明" in found.note


# ---------------------------------------------------------------------------
# 2) 绑定后只召回被绑版本
# ---------------------------------------------------------------------------


def test_绑定后只召回被绑的制度版本(policy_v1, policy_v2):
    clauses = list(policy_v1.clauses) + list(policy_v2.clauses)
    visible = visible_policy_clauses(clauses, "718-7", binding())
    assert visible
    assert {clause.citation.file_version for clause in visible} == {"测试 v1.0"}


def test_未绑版本不出现在引用里(policy_v1, policy_v2):
    clauses = list(policy_v1.clauses) + list(policy_v2.clauses)
    citations = visible_citations(clauses, "718-7", binding())
    assert citations
    for citation in citations:
        assert citation.file_version == "测试 v1.0"
        assert "测试 v2.0" not in citation.locator
    assert unbound_versions(clauses, binding()) == ("测试 v2.0",)


def test_阈值行同样按绑定过滤(coexist_table):
    rows = list(coexist_table.rows)
    visible = visible_threshold_rows(rows, "718-7", binding())
    assert visible
    assert {row.citation.file_version for row in visible} == {"示例 v1.0"}


def test_没有绑定时不限制版本但要提示(coexist_table, policy_v1):
    clauses = list(policy_v1.clauses)
    visible = visible_policy_clauses(clauses, "718-7", None)
    assert len(visible) == len(clauses)
    assert unbound_versions(clauses, None) == ()


def test_引用了未绑版本的处置卡被版本闸门拒绝(coexist_table, policy_v1, policy_v2):
    """制度引了「测试 v2.0」，但绑定声明的是「测试 v1.0」→ 拒绝出卡。"""
    table = load_threshold_table(COEXIST_TABLE)
    row = next(r for r in table.rows if r.item_code == "718-7" and r.version == "示例 v1.0")
    clause = next(c for c in policy_v2.clauses if c.clause_no == "第七条")
    event = CriticalEvent(
        event_id="evt-bind",
        message_control_id="MSGBIND01",
        specimen_no="SPEC-BIND",
        item_code="718-7",
        item_name="血红蛋白",
        value=Decimal("25"),
        unit="g/L",
        observed_at=date(2026, 2, 10),
    )
    judgement = Judgement(
        decision="critical",
        reason="低于低危急值",
        item_code="718-7",
        value=Decimal("25"),
        unit="g/L",
        threshold_row=row,
    )
    with pytest.raises(VersionMismatchError) as excinfo:
        build_card(event, judgement, policy_clause=clause, version_binding=binding())
    assert excinfo.value.code == "version_mismatch"
    assert "绑定版本" in excinfo.value.consistency.reason


# ---------------------------------------------------------------------------
# 3) 制度改版影响面
# ---------------------------------------------------------------------------


def test_制度改版按稳定标识diff出受影响条款(policy_v1, policy_v2):
    diffs = diff_policy_documents(policy_v1, policy_v2)
    changes = {diff.clause_no: diff for diff in diffs}
    assert "第七条" in changes
    assert changes["第七条"].change == CHANGE_FACTS
    assert "deadline_text" in changes["第七条"].changed_fields
    assert changes["第五条"].change == CHANGE_REMOVED
    assert changes["第八条"].change == CHANGE_ADDED


def test_制度改版diff摘要可读(policy_v1, policy_v2):
    summary = summarize(diff_policy_documents(policy_v1, policy_v2))
    assert summary["total"] == 3
    assert summary["counts"][CHANGE_FACTS] == 1
    assert summary["counts"][CHANGE_REMOVED] == 1
    assert summary["counts"][CHANGE_ADDED] == 1
    assert summary["affected_clause_count"] == 3


def test_制度改版diff清单可写CSV(policy_v1, policy_v2, tmp_path):
    path = write_csv(diff_policy_documents(policy_v1, policy_v2), tmp_path / "制度改版.csv")
    with open(path, encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.reader(handle))
    assert rows[0][0] == "条款号"
    assert len(rows) == 4  # 表头 + 3 条变更
    body = "\n".join(",".join(row) for row in rows[1:])
    assert "第七条" in body
    assert "事实要素变化" in body


def test_同一版制度与自身diff无变更(policy_v1):
    diffs = diff_policy_documents(policy_v1, policy_v1)
    assert diffs == ()


def test_只改措辞不改事实算正文修订():
    from critval.models import PolicyClause

    def clause(body: str) -> PolicyClause:
        return PolicyClause(
            section_title="第二章 报告流程与通知时限",
            body=body,
            notify_target="检验科值班技师",
            deadline_text="10 分钟内",
            deadline_minutes=10,
            record_destination="危急值登记系统",
            citation=Citation(
                source_name="制度.docx",
                source_sha256="c" * 64,
                kind="policy_clause",
                paragraph_no=1,
                line_no=1,
                file_version="v1",
            ),
            clause_no="第一条",
            stable_id="第一条",
        )

    diffs = diff_policy_documents_docx(clause("第一条 原措辞"), clause("第一条 新措辞"))
    assert len(diffs) == 1
    assert diffs[0].change == CHANGE_BODY


def diff_policy_documents_docx(old_clause, new_clause):
    """按条款直接 diff（正文修订这一类不涉及整份文件的场景）。"""
    from critval.kb.policy_diff import diff_clauses

    return diff_clauses([old_clause], [new_clause])


# ---------------------------------------------------------------------------
# 4) 运维 CLI
# ---------------------------------------------------------------------------


def test_绑定表命令行可查看可写(tmp_path):
    path = tmp_path / "版本绑定表.json"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "critval.cli",
            "bind",
            "--bindings",
            str(path),
            "--item",
            "718-7",
            "--threshold-version",
            "示例 v1.0",
            "--policy-version",
            "示例 v1.0",
            "--note",
            "检验科 2026-01 确认",
        ],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
    )
    assert result.returncode == 0, result.stderr
    assert "718-7 → 阈值表 示例 v1.0 + 制度 示例 v1.0" in result.stdout
    assert path.exists()

    listed = subprocess.run(
        [
            sys.executable,
            "-m",
            "critval.cli",
            "bind",
            "--bindings",
            str(path),
            "--list",
            "--json",
        ],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
    )
    assert listed.returncode == 0, listed.stderr
    payload = json.loads(listed.stdout)
    assert payload["bindings"][0]["item_code"] == "718-7"


def test_绑定表命令行可按材料推导(tmp_path):
    path = tmp_path / "推导绑定表.json"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "critval.cli",
            "bind",
            "--bindings",
            str(path),
            "--derive",
            "--threshold",
            str(COEXIST_TABLE),
            "--policy",
            str(POLICY_V1),
            "--at",
            "2026-02-10",
        ],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
    )
    assert result.returncode == 0, result.stderr
    assert "718-7 → 阈值表 示例 v2.0" in result.stdout
    assert "制度点名条款" in result.stdout
