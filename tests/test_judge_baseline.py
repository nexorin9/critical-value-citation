"""只看 OBX-8 的朴素基线 vs 院内阈值表判定引擎（任务 id 9 · 深度项 b）。

对比要成立，四件事必须同时为真：

* **两侧同口径** —— 同一批样例、同一份阈值表、同一把尺子（场景意图）；
* **基线真的是基线** —— 只看 ``OBX-8``，不查阈值表、不换算单位；
* **差在哪儿说得清** —— 基线在「异常但未达危急值」上大量误报，
  在「危急但没标志」上漏报；引擎两侧都不出错；
* **对比表可复算** —— 同一命令跑两次得到逐字节相同的表。
"""

from __future__ import annotations

import csv
import subprocess
import sys
from pathlib import Path

import pytest

from critval.judge.baseline_compare import (
    BASELINE_NAME,
    CSV_COLUMNS,
    ENGINE_NAME,
    SCENARIO_LABELS,
    build_judge_cases,
    compare_judge_baselines,
    render_comparison_markdown,
    write_comparison_csv,
)
from critval.judge.ruleset import RuleSet
from critval.kb.loader import load_threshold_table

PROJECT_ROOT = Path(__file__).resolve().parent.parent
THRESHOLD = PROJECT_ROOT / "samples" / "threshold" / "示例危急值阈值表.xlsx"
COMPARE_SCRIPT = PROJECT_ROOT / "scripts" / "compare_judge_baseline.py"


@pytest.fixture(scope="module")
def table():
    return load_threshold_table(THRESHOLD)


@pytest.fixture(scope="module")
def ruleset(table):
    return RuleSet.compile(
        table.rows,
        snapshot_id=f"rs-cmp-{table.source_sha256[:12]}",
        source_name=table.source_name,
        source_sha256=table.source_sha256,
    )


@pytest.fixture(scope="module")
def cases(table):
    return build_judge_cases(table.rows)


@pytest.fixture(scope="module")
def comparison(cases, table, ruleset):
    return compare_judge_baselines(cases, table.rows, ruleset=ruleset)


# ---------------------------------------------------------------------------
# 样例集：确定性、规模、覆盖面
# ---------------------------------------------------------------------------


def test_样例集由阈值表确定性生成(table):
    """同一份阈值表跑两次得到逐条相同的样例集（不掺随机数）。"""
    first = build_judge_cases(table.rows)
    second = build_judge_cases(table.rows)
    assert len(first) == len(second)
    for left, right in zip(first, second):
        assert left.scenario == right.scenario
        assert left.event.event_id == right.event.event_id
        assert left.event.value == right.event.value
        assert left.event.obx_flag == right.event.obx_flag
        assert left.expected_critical == right.expected_critical


def test_样例集规模足够且覆盖全部项目(cases, table):
    """样例不能只有三五行 —— 每个阈值行都要有覆盖。"""
    assert len(cases) >= 200
    covered = {case.event.item_code for case in cases}
    assert covered == {row.item_code for row in table.rows}


def test_样例集覆盖全部关键场景(cases):
    present = {case.scenario for case in cases}
    assert present == set(SCENARIO_LABELS)


def test_样例里每个项目都有多种场景(cases):
    """每个项目至少 5 个场景 —— 只有高危急阈值或只有低危急阈值的项目场景少一些。"""
    from collections import Counter

    counter = Counter(case.event.item_code for case in cases)
    assert min(counter.values()) >= 5


def test_样例的异常标志按参考区间偏离给出(cases, table):
    """标志不是随便填的 —— 它按 LIS 的真实行为（偏离参考区间）给出。"""
    row = table.first("718-7")
    assert row is not None
    by_scenario = {case.scenario: case for case in cases if case.event.item_code == row.item_code}
    # 低于低危急值 → LIS 给 panic 档 LL
    assert by_scenario["critical_low"].event.obx_flag == "LL"
    # 高于高危急值 → HH
    assert by_scenario["critical_high"].event.obx_flag == "HH"
    # 参考区间内 → N
    assert by_scenario["normal_in_range"].event.obx_flag == "N"
    # 异常但未达危急值 → 只有 H / L，不是 panic 档
    assert by_scenario["abnormal_not_critical_high"].event.obx_flag == "H"
    assert by_scenario["abnormal_not_critical_low"].event.obx_flag == "L"


# ---------------------------------------------------------------------------
# 两侧结论
# ---------------------------------------------------------------------------


def test_基线只看标志不查阈值表(comparison):
    """基线的「依据可追溯数」必须是 0 —— 它给不出依据。"""
    assert comparison.baseline["依据可追溯数"] == 0
    assert comparison.baseline["不判定数"] == 0


def test_朴素基线在异常但未达危急值上大量误报(comparison):
    """告警疲劳的来源：LIS 说异常，阈值表说不危急。"""
    assert comparison.baseline["误报数"] > 0
    over = [
        item
        for item in comparison.disagreements
        if item["direction"] == "baseline_over_call"
    ]
    assert len(over) == comparison.baseline["误报数"]
    assert all(item["engine_decision"] == "not_critical" for item in over)


def test_朴素基线漏掉没有异常标志的危急值(comparison):
    """标志不可信就会漏 —— 真危急但 LIS 没标异常的那几条全被放过。"""
    assert comparison.baseline["漏报数"] > 0
    under = [
        item
        for item in comparison.disagreements
        if item["direction"] == "baseline_under_call"
    ]
    assert len(under) == comparison.baseline["漏报数"]
    assert all(item["engine_decision"] == "critical" for item in under)


def test_引擎对场景真值零判错(comparison):
    """真值是场景意图（不是引擎结论倒推的）—— 引擎判错会被这张尺子量出来。"""
    assert comparison.engine["判错合计"] == 0
    assert comparison.engine["误报数"] == 0
    assert comparison.engine["漏报数"] == 0
    assert comparison.engine["该判未判数"] == 0


def test_引擎的全部不判定都来自单位无法换算(comparison, cases):
    """查不到换算就不判定 —— 转人工不是漏报，是拒绝猜。"""
    unknown = [case for case in cases if case.expected_critical is None]
    assert unknown
    assert all(case.scenario == "unit_unknown_not_judged" for case in unknown)
    assert comparison.engine["不判定数"] == len(unknown)


def test_引擎检出全部口径分歧(comparison):
    """口径分歧检出数 = 分歧总数；基线检出 0 条。"""
    assert comparison.engine["口径分歧检出数"] == len(comparison.disagreements)
    assert comparison.engine["未检出分歧数"] == 0
    assert comparison.baseline["口径分歧检出数"] == 0
    assert comparison.baseline["未检出分歧数"] == len(comparison.disagreements)


def test_引擎每条结论都能追溯到阈值行出处(comparison):
    assert comparison.engine["依据可追溯数"] == comparison.case_count


def test_对比表两行口径字段齐备(comparison):
    assert comparison.rows[0]["参赛者"] == BASELINE_NAME
    assert comparison.rows[1]["参赛者"] == ENGINE_NAME
    for row in comparison.rows:
        for key in CSV_COLUMNS:
            assert key in row
        assert row["样例数"] == comparison.case_count


# ---------------------------------------------------------------------------
# 产物可复算
# ---------------------------------------------------------------------------


def test_对比表两次渲染逐字节相同(comparison):
    first = render_comparison_markdown(comparison)
    second = render_comparison_markdown(comparison)
    assert first == second
    assert "判定基线对比" in first
    assert BASELINE_NAME in first
    assert ENGINE_NAME in first


def test_对比表CSV能落盘且列齐(comparison, tmp_path):
    target = write_comparison_csv(comparison, tmp_path / "judge_baseline_compare.csv")
    assert target.exists()
    raw = target.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")  # UTF-8 BOM
    with target.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 2
    assert list(rows[0].keys()) == list(CSV_COLUMNS)


def test_对比表同一命令跑两次逐字节相同(tmp_path):
    """可复算 = 同一命令两次结果一致（两个产物都不写时间戳）。"""
    out_md = tmp_path / "judge.md"
    out_csv = tmp_path / "judge.csv"
    command = [
        sys.executable,
        str(COMPARE_SCRIPT),
        "--out",
        str(out_md),
        "--csv-out",
        str(out_csv),
    ]
    first = subprocess.run(command, capture_output=True, text=True, cwd=PROJECT_ROOT)
    assert first.returncode == 0, first.stderr
    md_bytes = out_md.read_bytes()
    csv_bytes = out_csv.read_bytes()
    second = subprocess.run(command, capture_output=True, text=True, cwd=PROJECT_ROOT)
    assert second.returncode == 0, second.stderr
    assert out_md.read_bytes() == md_bytes
    assert out_csv.read_bytes() == csv_bytes


def test_脚本输出关键数字(tmp_path):
    out_md = tmp_path / "judge.md"
    out_csv = tmp_path / "judge.csv"
    result = subprocess.run(
        [
            sys.executable,
            str(COMPARE_SCRIPT),
            "--out",
            str(out_md),
            "--csv-out",
            str(out_csv),
            "--json",
        ],
        capture_output=True,
        text=True,
        cwd=PROJECT_ROOT,
    )
    assert result.returncode == 0, result.stderr
    import json

    payload = json.loads(result.stdout)
    assert payload["case_count"] >= 200
    assert payload["rows"][0]["参赛者"] == BASELINE_NAME
    assert payload["rows"][1]["口径分歧检出数"] > 0
