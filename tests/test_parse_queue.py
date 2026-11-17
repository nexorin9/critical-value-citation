"""人工确认队列：疑点入队 / 幂等入库 / 回填确认触发重解析。

验收口径：

* **未识别行不静默跳过** —— 一份含脏行的阈值表，每一条未识别行都能在队列里
  看到（连同缺字段行、没认出来的列、疑似合并单元格、疑似多版本）；
* **回填要真的生效** —— 确认值写回材料重解析，那一行读出来了、疑点关闭；
  写不进去（表里没那列）时明确说「回填未生效」，不装作成功；
* **重复投递不重复灌队列**，人已确认过的条目不会被机器打回待处理。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from openpyxl import Workbook

from critval.cli import main
from critval.kb.loader import load_threshold_table
from critval.kb.parse_queue import (
    QUEUE_CONFIRMED,
    QUEUE_IGNORED,
    QUEUE_OPEN,
    ParseQueue,
    queue_items_from_report,
    reparse_after_confirm,
)
from critval.kb.parse_report import MaterialReport, ParseReportStore

HEADER = ["项目码", "项目名", "单位", "低危急值", "高危急值", "参考区间", "生效日期", "版本号"]


def _write_table(path: Path, rows: list[list], header: list[str] | None = None) -> Path:
    """写一份阈值表 xlsx。"""
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "危急值阈值表"
    sheet.append(header or HEADER)
    for row in rows:
        sheet.append(row)
    path.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(path)
    return path


def _report_of(path: Path) -> MaterialReport:
    return MaterialReport.from_parse_report(load_threshold_table(path).report)


@pytest.fixture
def dirty_table(tmp_path: Path) -> Path:
    """含多类疑点的阈值表：

    * 第 3 行缺项目码 → 未识别行；
    * 第 4 行阈值列为空 → 字段缺失行；
    * 第 5 列表头认不出来 → 未识别列；
    * 第 6 行同一项目码挂两个版本号 → 疑似多版本。
    """
    return _write_table(
        tmp_path / "脏阈值表.xlsx",
        [
            ["718-7", "血红蛋白", "g/L", 30, 200, "115-150", "2026-01-01", "测试 v1.0"],
            ["2823-3", "钾", "mmol/L", 2.8, 6.5, "3.5-5.3", "2026-01-01", "测试 v1.0"],
            ["", "缺项目码行", "mmol/L", 1, 2, "1-2", "2026-01-01", "测试 v1.0"],
            ["8888-8", "缺阈值行", "mmol/L", None, None, "1-2", "2026-01-01", "测试 v1.0"],
            ["777-3", "血小板", "10*9/L", 30, 100, "125-350", "2026-01-01", "测试 v1.0"],
            ["777-3", "血小板", "10*9/L", 20, 90, "125-350", "2026-02-01", "测试 v2.0"],
        ],
        header=[*HEADER, "备注列"],
    )


@pytest.fixture
def queue(tmp_path: Path):
    instance = ParseQueue(tmp_path / "queue.db")
    yield instance
    instance.close()


# ---------------------------------------------------------------------------
# 解析报告：四类疑点都要在报告里
# ---------------------------------------------------------------------------


def test_解析报告列出未识别行与未识别列(dirty_table: Path):
    report = _report_of(dirty_table)
    assert report.unrecognized_rows, "缺项目码的行必须在报告里"
    assert any(entry["row_no"] == 4 for entry in report.unrecognized_rows)
    assert report.unrecognized_columns == ["备注列"]
    assert report.ok is False
    assert "未识别 1 行" in report.summary()


def test_解析报告单列字段缺失行与未识别行(dirty_table: Path):
    report = _report_of(dirty_table)
    incomplete_rows = [entry["row_no"] for entry in report.incomplete_rows]
    assert 5 in incomplete_rows, "阈值列为空的行进字段缺失清单"
    assert 4 not in incomplete_rows, "缺项目码的行归未识别，不重复算字段缺失"


def test_解析报告标出疑似多版本(dirty_table: Path):
    report = _report_of(dirty_table)
    assert report.multi_version_groups, "同一项目码挂两个版本号要标出来"
    group = report.multi_version_groups[0]
    assert group["item_code"] == "777-3"
    assert set(group["versions"]) == {"测试 v1.0", "测试 v2.0"}


def test_解析报告标出疑似合并单元格(tmp_path: Path):
    """项目码列做合并单元格 → 下面的行读不成阈值行，单独标出来。"""
    path = tmp_path / "合并表.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(HEADER)
    sheet.append(["718-7", "血红蛋白", "g/L", 30, 200, "115-150", "2026-01-01", "测试 v1.0"])
    sheet.append(["", "血红蛋白", "g/L", 25, 200, "115-150", "2026-01-01", "测试 v1.0"])
    sheet.merge_cells("A2:A3")
    workbook.save(path)

    report = _report_of(path)
    assert report.merged_cell_rows, "合并区域要进报告"
    suspect = report.merged_cell_rows[0]
    assert suspect["row_no"] == 3
    assert suspect["field"] == "item_code"
    assert "合并" in suspect["reason"]


def test_没有合并单元格时不误报(tmp_path: Path):
    """普通表不该被当成合并单元格 —— 缺项目码是缺项目码，不是合并。"""
    path = _write_table(
        tmp_path / "普通表.xlsx",
        [["718-7", "血红蛋白", "g/L", 30, 200, "115-150", "2026-01-01", "测试 v1.0"],
         ["", "缺项目码行", "mmol/L", 1, 2, "1-2", "2026-01-01", "测试 v1.0"]],
    )
    report = _report_of(path)
    assert report.merged_cell_rows == []


# ---------------------------------------------------------------------------
# 入队：每条疑点都进队列，重复投递不重复灌
# ---------------------------------------------------------------------------


def test_含脏行的阈值表每条未识别行都能在队列里看到(dirty_table: Path, queue: ParseQueue):
    report = _report_of(dirty_table)
    items = queue_items_from_report(report)
    added, skipped = queue.add_items(items)
    assert added == len(items)
    assert skipped == 0

    rows = sorted(item.row_no for item in queue.list_items(status=QUEUE_OPEN))
    assert 4 in rows, "未识别行（第 4 行）必须在队列里"
    assert 5 in rows, "字段缺失行（第 5 行）必须在队列里"
    categories = {item.category for item in queue.list_items()}
    assert "unrecognized_row" in categories
    assert "incomplete_row" in categories
    assert "unrecognized_column" in categories
    assert "multi_version" in categories


def test_重复投递同一文件不重复灌队列(dirty_table: Path, queue: ParseQueue):
    items = queue_items_from_report(_report_of(dirty_table))
    queue.add_items(items)
    total = queue.stats()["total"]

    added, skipped = queue.add_items(items)
    assert added == 0
    assert skipped == len(items)
    assert queue.stats()["total"] == total


def test_已确认的条目不会被重复投递打回待处理(dirty_table: Path, queue: ParseQueue):
    items = queue_items_from_report(_report_of(dirty_table))
    queue.add_items(items)
    target = next(item for item in items if item.category == "unrecognized_row")
    queue.confirm(target.queue_id, value="718-7", handler="检验科 张三")

    queue.add_items(items)
    refreshed = queue.get(target.queue_id)
    assert refreshed.status == QUEUE_CONFIRMED
    assert refreshed.handler == "检验科 张三"
    assert refreshed.confirmed_value == "718-7"


def test_缺项目码的疑点能推回该补哪个字段(dirty_table: Path):
    items = queue_items_from_report(_report_of(dirty_table))
    row_item = next(item for item in items if item.category == "unrecognized_row")
    assert row_item.target_field == "item_code"


def test_队列概览按状态与类别计数(dirty_table: Path, queue: ParseQueue):
    queue.add_items(queue_items_from_report(_report_of(dirty_table)))
    stats = queue.stats()
    assert stats["total"] == stats["pending"]
    assert stats["by_category"]["unrecognized_row"] == 1


# ---------------------------------------------------------------------------
# 回填确认 → 重解析
# ---------------------------------------------------------------------------


def test_回填确认后重解析该行读出来了(dirty_table: Path, queue: ParseQueue, tmp_path: Path):
    """确认值真的写回材料重解析 —— 识别行数增加，那一行不再是未识别行。"""
    report = _report_of(dirty_table)
    queue.add_items(queue_items_from_report(report))
    item = next(i for i in queue.list_items() if i.category == "unrecognized_row")

    before = report.recognized_rows
    queue.confirm(item.queue_id, value="7777-7", handler="检验科 李四", note="补项目码")
    updated = queue.get(item.queue_id)
    assert updated.status == QUEUE_CONFIRMED

    store = ParseReportStore(tmp_path / "reports")
    store.save(report)
    outcome = reparse_after_confirm(queue, updated, "7777-7", report_store=store)

    assert outcome["reparsed"] is True
    assert outcome["applied_overrides"], "回填必须真的写回材料"
    assert outcome["applied_overrides"][0]["row_no"] == item.row_no
    assert outcome["applied_overrides"][0]["field"] == "item_code"
    assert outcome["recognized_rows"] == before + 1, "回填后那一行被识别了"
    assert outcome["unrecognized_rows"] == 0
    assert item.row_no in outcome["resolved_rows"]


def test_回填后重解析的结果落盘可查(dirty_table: Path, queue: ParseQueue, tmp_path: Path):
    report = _report_of(dirty_table)
    queue.add_items(queue_items_from_report(report))
    item = next(i for i in queue.list_items() if i.category == "unrecognized_row")
    queue.confirm(item.queue_id, value="7777-7", handler="检验科 李四")
    store = ParseReportStore(tmp_path / "reports")
    store.save(report)
    reparse_after_confirm(queue, queue.get(item.queue_id), "7777-7", report_store=store)

    saved = store.load(report.source_sha256)
    assert saved is not None
    assert saved.recognized_rows == report.recognized_rows + 1
    assert saved.applied_overrides


def test_材料不在原处时明说不能重解析(dirty_table: Path, queue: ParseQueue, tmp_path: Path):
    queue.add_items(queue_items_from_report(_report_of(dirty_table)))
    item = next(i for i in queue.list_items() if i.category == "unrecognized_row")
    queue.confirm(item.queue_id, value="7777-7", handler="检验科 李四")
    missing = queue.get(item.queue_id)
    object.__setattr__(missing, "source_path", str(tmp_path / "已移走.xlsx"))

    outcome = reparse_after_confirm(queue, missing, "7777-7")
    assert outcome["reparsed"] is False
    assert "无法重解析" in outcome["reason"]


def test_忽略疑点要留处理人(dirty_table: Path, queue: ParseQueue):
    queue.add_items(queue_items_from_report(_report_of(dirty_table)))
    item = next(i for i in queue.list_items() if i.category == "unrecognized_row")
    updated = queue.ignore(item.queue_id, handler="检验科 王五", note="本行已作废")
    assert updated.status == QUEUE_IGNORED
    assert updated.handler == "检验科 王五"
    assert updated.note == "本行已作废"


# ---------------------------------------------------------------------------
# CLI：parse-queue / kb-watch
# ---------------------------------------------------------------------------


def test_CLI列出待确认项(tmp_path: Path, dirty_table: Path, capsys):
    queue_path = tmp_path / "queue.db"
    queue = ParseQueue(queue_path)
    queue.add_items(queue_items_from_report(_report_of(dirty_table)))
    queue.close()

    code = main(["parse-queue", "list", "--queue", str(queue_path)])
    assert code == 0
    out = capsys.readouterr().out
    assert "待处理" in out
    assert "缺少项目码" in out


def test_CLI回填确认触发重解析(tmp_path: Path, dirty_table: Path, capsys):
    queue_path = tmp_path / "queue.db"
    report_dir = tmp_path / "reports"
    queue = ParseQueue(queue_path)
    report = _report_of(dirty_table)
    queue.add_items(queue_items_from_report(report))
    item = next(i for i in queue.list_items() if i.category == "unrecognized_row")
    queue.close()

    code = main(
        [
            "parse-queue",
            "confirm",
            "--id",
            item.queue_id,
            "--value",
            "7777-7",
            "--handler",
            "检验科 李四",
            "--queue",
            str(queue_path),
            "--report-dir",
            str(report_dir),
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "重解析：已执行" in out
    assert "回填生效" in out

    queue = ParseQueue(queue_path)
    refreshed = queue.get(item.queue_id)
    assert refreshed.status == QUEUE_CONFIRMED
    assert refreshed.confirmed_value == "7777-7"
    queue.close()


def test_CLI忽略疑点必须写处理人(tmp_path: Path, dirty_table: Path, capsys):
    queue_path = tmp_path / "queue.db"
    queue = ParseQueue(queue_path)
    queue.add_items(queue_items_from_report(_report_of(dirty_table)))
    item = next(i for i in queue.list_items() if i.category == "unrecognized_row")
    queue.close()

    code = main(["parse-queue", "ignore", "--id", item.queue_id, "--queue", str(queue_path)])
    assert code == 2
    assert "必须写处理人" in capsys.readouterr().err


def test_CLI扫描投递目录只报事件不动手(tmp_path: Path, dirty_table: Path, capsys):
    root = tmp_path / "materials"
    root.mkdir()
    (root / "阈值表.xlsx").write_bytes(dirty_table.read_bytes())

    code = main(["kb-watch", "--dir", str(root), "--queue", str(tmp_path / "q.db")])
    assert code == 0
    out = capsys.readouterr().out
    assert "检出 1 条变化" in out
    assert "--apply" in out


def test_CLI扫描投递目录加apply执行重建(tmp_path: Path, dirty_table: Path, capsys):
    root = tmp_path / "materials"
    root.mkdir()
    (root / "阈值表.xlsx").write_bytes(dirty_table.read_bytes())

    code = main(
        [
            "kb-watch",
            "--dir",
            str(root),
            "--apply",
            "--queue",
            str(tmp_path / "q.db"),
            "--catalog",
            str(tmp_path / "catalog.json"),
            "--report-dir",
            str(tmp_path / "reports"),
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "[rebuilt]" in out
    assert "确认队列" in out

    queue = ParseQueue(tmp_path / "q.db")
    assert queue.stats()["pending"] > 0
    queue.close()


def test_CLI队列输出可被JSON解析(tmp_path: Path, dirty_table: Path, capsys):
    queue_path = tmp_path / "queue.db"
    queue = ParseQueue(queue_path)
    queue.add_items(queue_items_from_report(_report_of(dirty_table)))
    queue.close()

    code = main(["parse-queue", "list", "--queue", str(queue_path), "--json"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["stats"]["total"] >= 1
    assert payload["items"][0]["queue_id"]
