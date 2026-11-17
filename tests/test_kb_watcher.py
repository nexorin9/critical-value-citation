"""材料投递目录监听：指纹比对 / 新增·变更·删除事件 / 变更即重建。

验收口径：

* **重复投递同一文件不重复入库** —— 指纹没变就没有事件，也不重复入队；
* **变更触发重建** —— 材料换内容后重新解析、落报告、疑点入队，
  规则集重编译并**原子切换**（快照 ID 变）；
* **删除有留痕** —— 台账摘除该条目，并说清规则集仍持旧快照。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from openpyxl import Workbook

from critval.judge.registry import RuleRegistry
from critval.kb.parse_queue import QUEUE_OPEN, ParseQueue
from critval.kb.parse_report import ParseReportStore
from critval.kb.watcher import (
    ACTION_ADDED,
    ACTION_CHANGED,
    ACTION_DELETED,
    MaterialCatalog,
    MaterialWatcher,
)


# ---------------------------------------------------------------------------
# 样例材料
# ---------------------------------------------------------------------------


def _write_threshold(path: Path, rows: list[list], version: str = "测试 v1.0") -> Path:
    """写一份阈值表 xlsx（列序同院内标准表）。"""
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "危急值阈值表"
    sheet.append(
        ["项目码", "项目名", "单位", "低危急值", "高危急值", "参考区间", "生效日期", "版本号"]
    )
    for row in rows:
        sheet.append([*row, version])
    path.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(path)
    return path


@pytest.fixture
def materials(tmp_path: Path) -> Path:
    """投递目录：一份正常阈值表 + 一份含脏行的阈值表。"""
    root = tmp_path / "materials"
    _write_threshold(
        root / "阈值表.xlsx",
        [["718-7", "血红蛋白", "g/L", 30, 200, "115-150", "2026-01-01"]],
    )
    _write_threshold(
        root / "脏阈值表.xlsx",
        [
            ["718-7", "血红蛋白", "g/L", 30, 200, "115-150", "2026-01-01"],
            ["", "缺项目码行", "mmol/L", 1, 2, "1-2", "2026-01-01"],
        ],
    )
    return root


@pytest.fixture
def watcher(materials: Path, tmp_path: Path):
    """带独立台账 / 队列 / 报告目录的监听器。"""
    catalog = MaterialCatalog.load(tmp_path / "catalog.json")
    queue = ParseQueue(tmp_path / "queue.db")
    store = ParseReportStore(tmp_path / "reports")
    instance = MaterialWatcher(materials, catalog=catalog, queue=queue, report_store=store)
    yield instance
    queue.close()


# ---------------------------------------------------------------------------
# 扫描：新增 / 变更 / 删除
# ---------------------------------------------------------------------------


def test_首次扫描把目录里的材料报成新增事件(watcher):
    events = watcher.scan()
    assert [event.action for event in events] == [ACTION_ADDED, ACTION_ADDED]
    assert {event.name for event in events} == {"阈值表.xlsx", "脏阈值表.xlsx"}
    assert all(event.kind == "threshold" for event in events)


def test_重复投递同一文件不产生事件也不重复入库(watcher, materials: Path):
    """内容没变就是没变 —— 重复投递不重复入库。"""
    first = watcher.refresh()
    assert first["event_count"] == 2

    second = watcher.refresh()
    assert second["event_count"] == 0
    assert second["results"] == []

    # 队列里不会因为重复扫描而多出条目
    assert watcher.queue.stats()["total"] == second["queue"]["total"]
    first_total = watcher.queue.stats()["total"]
    watcher.refresh()
    assert watcher.queue.stats()["total"] == first_total


def test_材料内容变了报成变更事件并带上前后指纹(watcher, materials: Path):
    watcher.refresh()
    _write_threshold(
        materials / "阈值表.xlsx",
        [["718-7", "血红蛋白", "g/L", 25, 200, "115-150", "2026-01-01"]],
        version="测试 v2.0",
    )
    events = watcher.scan()
    changed = [event for event in events if event.name == "阈值表.xlsx"]
    assert len(changed) == 1
    assert changed[0].action == ACTION_CHANGED
    assert changed[0].previous_sha256 and changed[0].sha256
    assert changed[0].previous_sha256 != changed[0].sha256


def test_材料被删掉报成删除事件并从台账摘除(watcher, materials: Path):
    watcher.refresh()
    (materials / "脏阈值表.xlsx").unlink()
    events = watcher.scan()
    assert [event.action for event in events] == [ACTION_DELETED]
    result = watcher.refresh()
    assert result["results"][0]["status"] == "skipped"
    assert "规则集" in result["results"][0]["detail"]["reason"]
    assert all("脏阈值表.xlsx" not in key for key in watcher.catalog.entries)


def test_非材料文件不进扫描范围(watcher, materials: Path):
    (materials / "说明.txt").write_text("随便写点什么", encoding="utf-8")
    events = watcher.scan()
    assert all(event.name != "说明.txt" for event in events)


# ---------------------------------------------------------------------------
# 重建：变更即重解析 + 规则集原子切换
# ---------------------------------------------------------------------------


def test_重建会重解析并落解析报告(watcher):
    result = watcher.refresh()
    entry = next(e for e in result["results"] if e["name"] == "阈值表.xlsx")
    assert entry["status"] == "rebuilt"
    detail = entry["detail"]
    assert detail["parsed_rows"] == 1
    assert detail["total_rows"] == 1
    assert detail["report_path"]
    assert Path(detail["report_path"]).exists()


def test_变更触发规则集重编译并原子切换(watcher, materials: Path):
    RuleRegistry.reset()
    watcher.refresh()
    before = RuleRegistry.snapshot_id()
    assert before, "首次重建后应持有规则集快照"

    _write_threshold(
        materials / "阈值表.xlsx",
        [["718-7", "血红蛋白", "g/L", 25, 200, "115-150", "2026-01-01"]],
        version="测试 v2.0",
    )
    result = watcher.refresh()
    entry = next(e for e in result["results"] if e["name"] == "阈值表.xlsx")
    assert entry["status"] == "rebuilt"
    detail = entry["detail"]
    assert detail["ruleset_changed"] is True
    assert detail["ruleset_snapshot_before"] == before
    assert detail["ruleset_snapshot_after"] != before
    assert RuleRegistry.snapshot_id() == detail["ruleset_snapshot_after"]
    RuleRegistry.reset()


def test_重建后规则集指向的是新版本(watcher, materials: Path):
    RuleRegistry.reset()
    watcher.refresh()
    _write_threshold(
        materials / "阈值表.xlsx",
        [["718-7", "血红蛋白", "g/L", 25, 200, "115-150", "2026-01-01"]],
        version="测试 v2.0",
    )
    watcher.refresh()
    ruleset = RuleRegistry.get_ruleset()
    assert ruleset is not None
    assert ruleset.versions == ("测试 v2.0",)
    assert ruleset.rules[0].high_critical is not None
    RuleRegistry.reset()


def test_重建时把疑点推进人工确认队列(watcher):
    watcher.refresh()
    items = watcher.queue.list_items(status=QUEUE_OPEN)
    assert items, "含脏行的表应产出待确认条目"
    source_names = {item.source_name for item in items}
    assert "脏阈值表.xlsx" in source_names


def test_制度类材料在解析模块落地前只报事件不动手(watcher, materials: Path):
    """docx 来了：事件照报，重建动作明说「尚未注册」，不假装处理过。"""
    (materials / "危急值报告制度.docx").write_bytes(b"PK\x03\x04 fake docx")
    result = watcher.refresh()
    entry = next(e for e in result["results"] if e["name"] == "危急值报告制度.docx")
    assert entry["kind"] == "policy"
    assert entry["status"] == "skipped"
    assert "尚未注册重建动作" in entry["detail"]["reason"]


def test_扫描不改任何东西只报事件(watcher, materials: Path):
    events = watcher.scan()
    assert events
    assert watcher.queue.stats()["total"] == 0
    assert watcher.catalog.entries == {}


# ---------------------------------------------------------------------------
# 台账
# ---------------------------------------------------------------------------


def test_台账落盘后重新载入内容一致(watcher, tmp_path: Path):
    watcher.refresh()
    watcher.catalog.save()
    reloaded = MaterialCatalog.load(tmp_path / "catalog.json")
    assert set(reloaded.entries) == set(watcher.catalog.entries)
    for key, entry in reloaded.entries.items():
        assert entry["sha256"] == watcher.catalog.entries[key]["sha256"]
        assert entry["kind"] == "threshold"


def test_台账文件损坏时从空台账起步不崩(tmp_path: Path):
    broken = tmp_path / "catalog.json"
    broken.write_text("{ 这不是 JSON", encoding="utf-8")
    catalog = MaterialCatalog.load(broken)
    assert catalog.entries == {}
