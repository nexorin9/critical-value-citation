"""院内危急值报告制度解析（docx / pdf）与版本生效期（任务 id 5）。

覆盖五条主线：

1. **条款级解析** —— 章节标题 / 条款正文 / 通知对象 / 上报时限 / 记录去向；
2. **出处五件套** —— 每条条款都能回答「哪份文件第几段哪一版」；
3. **不静默跳过** —— 空正文条款、含要素却无条款号的段落都进解析报告；
4. **缺项不编造** —— 只写了一半事实的条款标「字段缺失」；
5. **版本与生效期** —— 按观察时刻择一，版本空档不取就近版本，转人工。
"""

from __future__ import annotations

import builtins
import csv
import json
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

from critval.kb.policy import (
    PolicyEntry,
    load_policy_document,
    parse_policy_entries,
    select_policy_version,
)
from critval.kb.version import sha256_file

ROOT = Path(__file__).resolve().parent.parent
SAMPLE_POLICY = ROOT / "samples" / "policy" / "示例危急值报告制度.docx"
FIXTURE_POLICY = ROOT / "tests" / "fixtures" / "脱敏样例制度.docx"
FIXTURE_POLICY_V2 = ROOT / "tests" / "fixtures" / "脱敏样例制度_v2.docx"
FIXTURE_POLICY_PDF = ROOT / "tests" / "fixtures" / "脱敏样例制度.pdf"
POLICY_V1 = FIXTURE_POLICY
POLICY_V2 = FIXTURE_POLICY_V2


@pytest.fixture(scope="module")
def sample_document():
    return load_policy_document(SAMPLE_POLICY)


@pytest.fixture(scope="module")
def fixture_document():
    return load_policy_document(FIXTURE_POLICY)


@pytest.fixture(scope="module")
def fixture_document_v2():
    return load_policy_document(FIXTURE_POLICY_V2)


# ---------------------------------------------------------------------------
# 1) 条款级解析
# ---------------------------------------------------------------------------


def test_样例制度解析出条款数与识别行数(sample_document):
    report = sample_document.report
    assert report is not None
    assert report.recognized_rows == len(sample_document.clauses)
    assert report.recognized_rows == 21
    assert report.total_rows == 28
    assert report.ok is True


def test_通知对象上报时限记录去向三个事实字段被抽出(sample_document):
    clause = next(c for c in sample_document.clauses if c.clause_no == "第七条")
    assert clause.notify_target == "检验科值班技师与检验科组长"
    assert clause.deadline_text == "10 分钟内"
    assert clause.deadline_minutes == 10
    assert clause.record_destination == "危急值登记系统，并同步记入检验科危急值台账"


def test_条款正文与章节标题分别落在body与section(sample_document):
    clause = next(c for c in sample_document.clauses if c.clause_no == "第七条")
    assert clause.section_title == "第三章 报告流程与通知时限"
    assert clause.body.startswith("第七条 检验科发现危急值后应当班复核")


def test_版本与生效日期从制度元信息行读出(sample_document):
    assert sample_document.version == "示例 v1.0"
    assert sample_document.effective_from == date(2026, 1, 1)
    assert sample_document.effective_to is None


def test_章节路径跟随标题层级(sample_document):
    clause = next(c for c in sample_document.clauses if c.clause_no == "第七条")
    assert clause.citation.section_path == ("第三章 报告流程与通知时限",)


def test_条款正文里的检验项目码被抽出(fixture_document):
    clause = next(c for c in fixture_document.clauses if c.clause_no == "第四条")
    assert clause.item_codes == ("718-7",)
    assert clause.item_names == ("血红蛋白",)


def test_重复条款号按项目码区分稳定标识():
    entries = [
        PolicyEntry(index=1, text="第一条 甲项目（111-1）的规定。"),
        PolicyEntry(index=2, text="第一条 乙项目（222-2）的规定。"),
    ]
    document = parse_policy_entries(entries, "样例.docx", "a" * 64)
    assert [c.stable_id for c in document.clauses] == ["第一条#111-1", "第一条#222-2"]


def test_说明性条款不算字段缺失(fixture_document):
    """总则类条款三项事实都没写 —— 这不是缺项，是它本来就不写。"""
    clause = next(c for c in fixture_document.clauses if c.clause_no == "第一条")
    assert clause.missing_fields == ()
    assert fixture_document.report.incomplete_rows == [
        {
            "row_no": 7,
            "reason": "字段缺失：notify_target、record_destination",
            "values": ["第三条 临床科室值班护士长应在 15 分钟内响应；上报时限：15 分钟内。"],
        }
    ]


# ---------------------------------------------------------------------------
# 2) 出处五件套
# ---------------------------------------------------------------------------


def test_出处五件套齐全(sample_document):
    clause = next(c for c in sample_document.clauses if c.clause_no == "第七条")
    citation = clause.citation
    assert citation.kind == "policy_clause"
    assert citation.source_sha256 == sha256_file(SAMPLE_POLICY)
    assert citation.paragraph_no == 19
    assert citation.line_no == 19
    assert citation.section_path == ("第三章 报告流程与通知时限",)
    assert citation.file_version == "示例 v1.0"
    assert citation.locator == "示例危急值报告制度.docx · 第 19 行 · 示例 v1.0"


def test_出处随条款一起序列化(fixture_document):
    clause = fixture_document.clauses[1]
    payload = clause.to_dict()
    assert payload["citation"]["source_sha256"]
    assert payload["citation"]["line_no"] == 6
    assert payload["stable_id"]
    assert payload["item_codes"] == []


# ---------------------------------------------------------------------------
# 3) 未识别行不静默跳过
# ---------------------------------------------------------------------------


def test_空正文条款进未识别行清单(fixture_document):
    rows = fixture_document.report.unrecognized_rows
    reasons = [row.reason for row in rows]
    assert any("正文为空" in reason for reason in reasons)
    empty = next(row for row in rows if "正文为空" in row.reason)
    assert empty.row_no == 10
    assert empty.values == ("第六条",)


def test_含要素却无条款号的段落进未识别行清单(fixture_document):
    rows = fixture_document.report.unrecognized_rows
    orphan = next(row for row in rows if "无条款编号" in row.reason)
    assert orphan.row_no == 11
    assert "通知对象：检验科值班技师" in orphan.values[0]


def test_未识别段落不计入识别条款数(fixture_document):
    assert fixture_document.report.recognized_rows == 6
    assert len(fixture_document.report.unrecognized_rows) == 2
    assert fixture_document.ok is False


def test_解析报告摘要写出识别条款数与未识别行数(fixture_document):
    summary = fixture_document.summary_text()
    assert "识别 6 条条款" in summary
    assert "未识别 2 段" in summary
    assert "字段缺失 1 条" in summary


# ---------------------------------------------------------------------------
# 4) 缺项不编造
# ---------------------------------------------------------------------------


def test_只写一半事实的条款标缺项且不编造(fixture_document):
    clause = next(c for c in fixture_document.clauses if c.clause_no == "第三条")
    assert clause.deadline_text == "15 分钟内"
    assert clause.deadline_minutes == 15
    assert clause.notify_target == ""
    assert clause.record_destination == ""
    assert clause.missing_fields == ("notify_target", "record_destination")


def test_制度完全没写的字段进未覆盖清单():
    entries = [
        PolicyEntry(index=1, text="第一条 检验科发现危急值后应报告；上报时限：10 分钟内。"),
    ]
    document = parse_policy_entries(entries, "样例.docx", "b" * 64)
    assert document.report.unrecognized_columns == ["notify_target", "record_destination"]
    assert "未覆盖事实字段 2 个" in document.summary_text()


# ---------------------------------------------------------------------------
# 5) pdf 通道（真实 pypdf）与依赖缺失时的显式降级
# ---------------------------------------------------------------------------


def test_pdf制度用真实pypdf解析出条款():
    document = load_policy_document(FIXTURE_POLICY_PDF)
    assert document.degraded is False
    assert document.version == "测试 v1.0"
    clause = next(c for c in document.clauses if c.clause_no == "第二条")
    assert clause.notify_target == "检验科值班技师"
    assert clause.deadline_minutes == 10
    assert clause.citation.source_sha256 == sha256_file(FIXTURE_POLICY_PDF)
    assert clause.citation.section_path == ("第二章 报告流程与通知时限",)


def test_pdf依赖缺失时显式降级而不是静默失败(monkeypatch):
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "pypdf" or name.startswith("pypdf."):
            raise ImportError("No module named 'pypdf'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    document = load_policy_document(FIXTURE_POLICY_PDF)
    assert document.degraded is True
    assert "pypdf" in document.degrade_reason
    assert document.clauses == ()
    assert document.ok is False
    reasons = [row.reason for row in document.report.unrecognized_rows]
    assert any("解析降级" in reason for reason in reasons)


def test_docx依赖缺失时显式降级(monkeypatch):
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "docx" or name.startswith("docx."):
            raise ImportError("No module named 'docx'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    document = load_policy_document(FIXTURE_POLICY)
    assert document.degraded is True
    assert "python-docx" in document.degrade_reason


def test_不支持的文件类型明确报错(tmp_path):
    target = tmp_path / "制度.txt"
    target.write_text("不是制度", encoding="utf-8")
    with pytest.raises(ValueError, match="不支持的制度文件类型"):
        load_policy_document(target)


# ---------------------------------------------------------------------------
# 6) 版本与生效期
# ---------------------------------------------------------------------------


def test_按观察时刻选生效的制度版本(sample_document):
    selection = select_policy_version([sample_document], date(2026, 3, 1))
    assert selection.ok is True
    assert selection.version == "示例 v1.0"
    assert selection.needs_human is False


def test_版本空档转人工不取就近版本(fixture_document, fixture_document_v2):
    """测试 v1.0 于 2026-02-25 失效、v2.0 于 2026-03-01 生效 —— 中间几天是空档。"""
    documents = [fixture_document, fixture_document_v2]
    gap = select_policy_version(documents, date(2026, 2, 27))
    assert gap.status == "gap"
    assert gap.ok is False
    assert gap.needs_human is True
    assert "不取就近版本" in gap.reason

    later = select_policy_version(documents, date(2026, 3, 5))
    assert later.ok is True
    assert later.version == "测试 v2.0"


def test_两版同时生效时口径互斥转人工(fixture_document):
    """同一份 v1.0 在两份文件里都覆盖该时刻 → 口径互斥。"""
    duplicate = load_policy_document(FIXTURE_POLICY)
    selection = select_policy_version([fixture_document, duplicate], date(2026, 2, 10))
    assert selection.status == "conflict"
    assert selection.ok is False
    assert "口径互斥" in selection.reason


def test_制度没有版本号时无法确认生效版本(fixture_document):
    document = load_policy_document(FIXTURE_POLICY)
    stripped = type(document)(
        source_name=document.source_name,
        source_sha256=document.source_sha256,
        clauses=document.clauses,
        report=document.report,
    )
    selection = select_policy_version([stripped], date(2026, 2, 10))
    assert selection.status == "no_match"
    assert selection.needs_human is True
    assert "未标注版本号" in selection.reason


def test_早于所有版本生效期时转人工(fixture_document):
    selection = select_policy_version([fixture_document], date(2026, 1, 1))
    assert selection.status == "no_match"
    assert selection.needs_human is True
    assert "早于所有制度版本的生效期" in selection.reason


# ---------------------------------------------------------------------------
# 7) 运维 CLI
# ---------------------------------------------------------------------------


def test_制度摘要命令行输出识别条款数与未识别行(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "critval.cli",
            "policy-summary",
            "--policy",
            str(FIXTURE_POLICY),
        ],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
    )
    assert result.returncode == 0, result.stderr
    assert "识别 6 条条款" in result.stdout
    assert "未识别 2 段" in result.stdout
    assert "正文为空" in result.stdout
    assert "无条款编号" in result.stdout
    assert "字段缺失" in result.stdout


def test_制度摘要命令行JSON可解析():
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "critval.cli",
            "policy-summary",
            "--policy",
            str(SAMPLE_POLICY),
            "--json",
        ],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["version"] == "示例 v1.0"
    assert payload["clause_count"] == 21
    assert payload["report"]["recognized_rows"] == 21


def test_制度改版命令行产出受影响条款清单(tmp_path):
    csv_path = tmp_path / "policy_version_diff.csv"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "critval.cli",
            "policy-diff",
            "--old",
            str(POLICY_V1),
            "--new",
            str(POLICY_V2),
            "--csv",
            str(csv_path),
        ],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
    )
    assert result.returncode == 0, result.stderr
    assert "制度改版影响面" in result.stdout
    assert "事实要素变化" in result.stdout
    assert "新增条款" in result.stdout
    assert csv_path.exists()
    with open(csv_path, encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.reader(handle))
    assert rows[0][0] == "条款号"
    assert len(rows) == 4
