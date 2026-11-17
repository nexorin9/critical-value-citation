"""材料投递目录监听：新增 / 变更 / 删除事件 + 变更即重建。

检验科改版后不会通知信息科，只会把新文件丢进共享目录。阈值表因此随时
可能被替换 —— 这件事必须有人盯，否则判定还在用上一版口径。

本模块盯住这个目录：

* **指纹比对**：只有 sha256 变了才算「变更」；内容没变的重复投递**不重复入库**；
* **事件三类**：``added`` / ``changed`` / ``deleted``；
* **变更即重建**：按材料类型派发重建动作 —— 阈值表变更会重新解析出行级条目、
  刷新解析报告、把疑点推进人工确认队列，并**重编译规则集并原子切换**
  （沿用规则集注册表的快照机制，判定方拿快照不拿活对象）；
* **重建动作可插拔**：制度文件（docx / pdf）在制度解析模块落地前，
  扫描照常报告事件、重建动作明确说「尚未注册」，不假装已经处理。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from critval.kb.loader import load_threshold_table
from critval.kb.parse_queue import ParseQueue, queue_items_from_report
from critval.kb.parse_report import MaterialReport, ParseReportStore
from critval.kb.version import sha256_file

__all__ = [
    "MATERIAL_KINDS",
    "MaterialCatalog",
    "MaterialEvent",
    "MaterialWatcher",
    "default_catalog_path",
]

#: 文件后缀 → 材料类型。
MATERIAL_KINDS: dict[str, str] = {
    ".xlsx": "threshold",
    ".xlsm": "threshold",
    ".docx": "policy",
    ".pdf": "policy",
}

#: 事件类型。
ACTION_ADDED = "added"
ACTION_CHANGED = "changed"
ACTION_DELETED = "deleted"

#: 重建结果状态。
STATUS_REBUILT = "rebuilt"
STATUS_SKIPPED = "skipped"
STATUS_FAILED = "failed"


def default_catalog_path() -> Path:
    """材料台账默认落盘位置（运行期数据，可再生，不进仓库）。"""
    return Path("data") / "material_catalog.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class MaterialEvent:
    """一次材料变化。"""

    action: str
    path: str
    name: str
    kind: str
    sha256: str = ""
    previous_sha256: str = ""
    detected_at: str = field(default_factory=_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "path": self.path,
            "name": self.name,
            "kind": self.kind,
            "sha256": self.sha256,
            "previous_sha256": self.previous_sha256,
            "detected_at": self.detected_at,
        }


@dataclass
class MaterialCatalog:
    """已入库材料的指纹台账（路径 → 指纹 + 类型）。

    这就是「材料条目索引」：目录里有哪些材料、各自的内容指纹是什么。
    """

    path: Path = field(default_factory=default_catalog_path)
    entries: dict[str, dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | Path = default_catalog_path()) -> "MaterialCatalog":
        """读台账；文件不存在或损坏时从空台账起步（不静默丢事件）。"""
        target = Path(path)
        catalog = cls(path=target)
        if not target.exists():
            return catalog
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return catalog
        entries = payload.get("entries")
        if isinstance(entries, dict):
            catalog.entries = {str(k): dict(v) for k, v in entries.items()}
        return catalog

    def save(self) -> Path:
        """落盘台账（原子写）。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "updated_at": _now(),
            "entries": self.entries,
        }
        temp = self.path.with_suffix(".json.tmp")
        temp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        temp.replace(self.path)
        return self.path

    def digest_of(self, path: str | Path) -> str:
        entry = self.entries.get(str(path))
        return str(entry.get("sha256", "")) if entry else ""

    def record(self, event: MaterialEvent, size: int = 0) -> None:
        """登记一次入库 / 变更。"""
        self.entries[event.path] = {
            "sha256": event.sha256,
            "name": event.name,
            "kind": event.kind,
            "size": size,
            "action": event.action,
            "detected_at": event.detected_at,
        }

    def forget(self, path: str) -> None:
        """移除一个已删除材料。"""
        self.entries.pop(path, None)


Rebuilder = Callable[[MaterialEvent], dict[str, Any]]


class MaterialWatcher:
    """投递目录监听器。

    用法：

    .. code-block:: python

        watcher = MaterialWatcher(Path("materials"))
        result = watcher.refresh()      # 扫描 + 重建 + 落台账
    """

    def __init__(
        self,
        root: str | Path,
        catalog: Optional[MaterialCatalog] = None,
        queue: Optional[ParseQueue] = None,
        report_store: Optional[ParseReportStore] = None,
        rebuilders: Optional[dict[str, Rebuilder]] = None,
    ) -> None:
        self.root = Path(root)
        self.catalog = catalog if catalog is not None else MaterialCatalog.load()
        self.queue = queue if queue is not None else ParseQueue()
        self.report_store = (
            report_store if report_store is not None else ParseReportStore()
        )
        self._rebuilders: dict[str, Rebuilder] = dict(rebuilders or {})
        self._rebuilders.setdefault("threshold", self._rebuild_threshold)

    # ---- 重建动作注册 ---------------------------------------------------
    def register_rebuilder(self, kind: str, rebuilder: Rebuilder) -> None:
        """注册某类材料的重建动作（索引模块落地后在此挂载）。"""
        self._rebuilders[kind] = rebuilder

    # ---- 扫描 -----------------------------------------------------------
    def _material_files(self) -> list[Path]:
        if not self.root.exists():
            return []
        found: list[Path] = []
        for path in sorted(self.root.rglob("*")):
            if not path.is_file():
                continue
            kind = MATERIAL_KINDS.get(path.suffix.lower())
            if kind:
                found.append(path)
        return found

    def scan(self) -> list[MaterialEvent]:
        """比对指纹，得出新增 / 变更 / 删除三类事件。

        **内容没变就是没变** —— 同一份文件重复投递不产生事件，
        也不会重复入库、重复出报告、重复入队。
        """
        events: list[MaterialEvent] = []
        seen: set[str] = set()
        for path in self._material_files():
            key = str(path)
            seen.add(key)
            digest = sha256_file(path)
            previous = self.catalog.digest_of(key)
            kind = MATERIAL_KINDS[path.suffix.lower()]
            if not previous:
                events.append(
                    MaterialEvent(
                        action=ACTION_ADDED,
                        path=key,
                        name=path.name,
                        kind=kind,
                        sha256=digest,
                    )
                )
            elif previous != digest:
                events.append(
                    MaterialEvent(
                        action=ACTION_CHANGED,
                        path=key,
                        name=path.name,
                        kind=kind,
                        sha256=digest,
                        previous_sha256=previous,
                    )
                )
        for key in list(self.catalog.entries):
            if key not in seen:
                events.append(
                    MaterialEvent(
                        action=ACTION_DELETED,
                        path=key,
                        name=str(self.catalog.entries[key].get("name", "")),
                        kind=str(self.catalog.entries[key].get("kind", "")),
                        previous_sha256=str(self.catalog.entries[key].get("sha256", "")),
                    )
                )
        return events

    # ---- 重建 -----------------------------------------------------------
    def apply(self, events: Sequence[MaterialEvent]) -> list[dict[str, Any]]:
        """逐条处理事件，返回每条的处理结果。"""
        results: list[dict[str, Any]] = []
        for event in events:
            if event.action == ACTION_DELETED:
                results.append(self._handle_deleted(event))
                continue
            rebuilder = self._rebuilders.get(event.kind)
            if rebuilder is None:
                results.append(
                    {
                        **event.to_dict(),
                        "status": STATUS_SKIPPED,
                        "detail": {
                            "reason": (
                                f"「{event.kind}」类材料尚未注册重建动作，"
                                "事件已记录，等待对应解析模块落地后接入"
                            )
                        },
                    }
                )
                self.catalog.record(event)
                continue
            try:
                outcome = rebuilder(event)
                status = str(outcome.get("status", STATUS_REBUILT))
                detail = dict(outcome.get("detail", {}))
            except Exception as exc:  # noqa: BLE001 - 单条失败不拖垮整批
                status = STATUS_FAILED
                detail = {"reason": f"重建失败：{exc}"}
            results.append({**event.to_dict(), "status": status, "detail": detail})
            if status != STATUS_FAILED:
                self.catalog.record(event)
        return results

    def refresh(self) -> dict[str, Any]:
        """扫描 → 重建 → 落台账，返回本次结果摘要。"""
        events = self.scan()
        results = self.apply(events)
        self.catalog.save()
        return {
            "root": str(self.root),
            "scanned_at": _now(),
            "event_count": len(events),
            "events": [event.to_dict() for event in events],
            "results": results,
            "queue": self.queue.stats(),
        }

    # ---- 内置重建动作 ---------------------------------------------------
    def _rebuild_threshold(self, event: MaterialEvent) -> dict[str, Any]:
        """阈值表重建：重解析 → 落解析报告 → 疑点入队 → 规则集重编译。

        规则集重编译沿用规则集注册表的**快照 + 原子切换**机制：
        材料指纹变了才换快照 ID，判定方持有快照不会看到半程切换。
        """
        from critval.judge.registry import RuleRegistry

        path = Path(event.path)
        table = load_threshold_table(path)
        report = MaterialReport.from_parse_report(table.report)
        report_path = self.report_store.save(report)

        items = queue_items_from_report(report)
        added, skipped = self.queue.add_items(items)

        before = RuleRegistry.snapshot_id()
        ruleset = RuleRegistry.reload(path)
        after = RuleRegistry.snapshot_id()

        return {
            "status": STATUS_REBUILT,
            "detail": {
                "parsed_rows": report.recognized_rows,
                "total_rows": report.total_rows,
                "unrecognized_rows": len(report.unrecognized_rows),
                "incomplete_rows": len(report.incomplete_rows),
                "merged_cell_rows": len(report.merged_cell_rows),
                "multi_version_groups": len(report.multi_version_groups),
                "report_path": str(report_path),
                "queue_added": added,
                "queue_skipped": skipped,
                "ruleset_snapshot_before": before,
                "ruleset_snapshot_after": after,
                "ruleset_changed": before != after,
                "rule_count": len(ruleset.rules) if ruleset else 0,
                "gap_count": len(ruleset.gaps) if ruleset else 0,
            },
        }

    def _handle_deleted(self, event: MaterialEvent) -> dict[str, Any]:
        """材料被删除：台账摘掉它，并说明规则集仍持旧快照。"""
        self.catalog.forget(event.path)
        return {
            **event.to_dict(),
            "status": STATUS_SKIPPED,
            "detail": {
                "reason": (
                    "材料已从投递目录移除，台账已摘除该条目；"
                    "若当前规则集来自这份材料，仍持有删除前的快照，"
                    "需人工确认切换到哪一版"
                )
            },
        }
