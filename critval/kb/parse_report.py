"""材料解析报告：识别行数 / 未识别列 / 未识别行清单 / 疑似合并单元格 / 疑似多版本。

一份阈值表被读进来之后，「读全了吗」不能只靠一句「解析成功」说了算。
本模块把三件事落到**可核对**的结构上：

1. **识别行数 vs 总行数** —— 少读一行阈值，等于少一条病人安全兜底；
2. **未识别列 / 未识别行清单** —— 读不到的东西必须列出来，不静默跳过；
3. **疑似合并单元格 / 疑似多版本** —— Excel 里最容易把口径带偏的两类情况，
   单独标出来提示人工确认（合并单元格会让项目码列整片留空；同一项目多版本
   同时在表里会让「哪一版说了算」变得不确定）。

报告**可落盘**（``ParseReportStore``）—— 材料换版之后，事后复核要能看到
「当时那份材料读成了什么样」，而不是重新解析一遍现在的文件。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Sequence

from critval.kb.version import ParseReport

__all__ = [
    "MaterialReport",
    "ParseReportStore",
    "default_report_dir",
    "detect_merged_cells",
    "detect_multi_versions",
    "read_merged_ranges",
]


def default_report_dir() -> Path:
    """解析报告默认落盘目录（运行期产物，可再生）。"""
    return Path("reports") / "parse_reports"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _cell_text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def read_merged_ranges(path: str | Path) -> tuple[tuple[int, int, int, int], ...]:
    """读 xlsx 里的**合并单元格区域**（权威信号，不是猜的）。

    返回 ``(最小列, 最小行, 最大列, 最大行)`` 元组序列，列行均为 1 基。

    合并区域从工作簿 XML 里读，不依赖 openpyxl 的只读模式（只读工作表拿不到
    ``merged_cells``）。读不到就返回空 —— 读不到**不等于**没有合并区域，
    调用方需要知道这个区别。
    """
    import re
    import zipfile

    ref_pattern = re.compile(r"^([A-Z]+)(\d+):([A-Z]+)(\d+)$")
    try:
        with zipfile.ZipFile(Path(path)) as archive:
            names = set(archive.namelist())
            sheet_path = _first_sheet_path(archive, names)
            if sheet_path is None:
                return ()
            xml = archive.read(sheet_path).decode("utf-8", errors="replace")
    except (OSError, zipfile.BadZipFile):
        return ()

    ranges: list[tuple[int, int, int, int]] = []
    for ref in re.findall(r'<mergeCell[^>]*ref="([^"]+)"', xml):
        matched = ref_pattern.match(ref.strip())
        if not matched:
            continue
        min_col = _column_index(matched.group(1))
        max_col = _column_index(matched.group(3))
        min_row = int(matched.group(2))
        max_row = int(matched.group(4))
        ranges.append((min_col, min_row, max_col, max_row))
    return tuple(ranges)


def _first_sheet_path(archive: Any, names: set[str]) -> Optional[str]:
    """解析第一个工作表在 zip 里的路径。"""
    import re

    if "xl/workbook.xml" not in names:
        return None
    try:
        workbook_xml = archive.read("xl/workbook.xml").decode("utf-8", errors="replace")
    except KeyError:  # pragma: no cover - 上面已判存在
        return None

    sheet_ids = re.findall(r'<sheet[^>]*r:id="([^"]+)"', workbook_xml)
    rels = ""
    if "xl/_rels/workbook.xml.rels" in names:
        rels = archive.read("xl/_rels/workbook.xml.rels").decode("utf-8", errors="replace")
    for sheet_id in sheet_ids:
        target = ""
        for rel_id, rel_target in re.findall(
            r'<Relationship[^>]*Id="([^"]+)"[^>]*Target="([^"]+)"', rels
        ):
            if rel_id == sheet_id:
                target = rel_target
                break
        if not target:
            continue
        candidate = target if target.startswith("xl/") else f"xl/{target.lstrip('/')}"
        if candidate in names:
            return candidate
    return "xl/worksheets/sheet1.xml" if "xl/worksheets/sheet1.xml" in names else None


def _column_index(letters: str) -> int:
    """``A`` → 1、``B`` → 2 …（与 Excel 列号一致）。"""
    index = 0
    for char in letters:
        index = index * 26 + (ord(char) - ord("A") + 1)
    return index


def detect_merged_cells(
    raw_rows: Sequence[Sequence[Any]],
    mapping: dict[str, int],
    header_index: int,
    merged_ranges: Sequence[tuple[int, int, int, int]] = (),
) -> list[dict[str, Any]]:
    """疑似合并单元格的行。

    判据用**工作簿自己的合并区域**（不是猜的）：

    * 合并区域落在「项目码」列 → 下面的行根本没有项目码，读不成阈值行；
    * 合并区域落在别的列（常见的是「项目名」）→ 下面那些行沿用了上一行的值，
      看着像同一个项目，其实可能是好几行不同的项目。

    两种情况都会让口径悄悄跑偏，必须单独列出来交人工确认。
    """
    suspects: list[dict[str, Any]] = []
    if not merged_ranges:
        return suspects

    # mapping 的列下标是 0 基，合并区域用的是 Excel 的 1 基列号 —— 对齐一下。
    column_fields = {index + 1: name for name, index in mapping.items()}
    for min_col, min_row, max_col, max_row in merged_ranges:
        if max_row <= min_row:
            continue  # 只在同一行内合并（横向合并）不影响「哪一行缺值」
        field = column_fields.get(min_col)
        if field is None:
            continue  # 合并的是我们不认识的列，不影响阈值口径
        for row_no in range(min_row + 1, max_row + 1):
            if row_no <= header_index + 1:
                continue  # 表头行不算数据行
            index = row_no - 1
            values = tuple(_cell_text(v) for v in raw_rows[index]) if index < len(raw_rows) else ()
            if field == "item_code":
                reason = (
                    f"疑似合并单元格：「项目码」列在第 {min_row}~{max_row} 行是合并区域，"
                    f"第 {row_no} 行没有项目码，读不成阈值行"
                )
            else:
                reason = (
                    f"疑似合并单元格：「{field}」列在第 {min_row}~{max_row} 行是合并区域，"
                    f"第 {row_no} 行沿用上一行的值，若各行本应是不同项目需人工确认"
                )
            suspects.append(
                {
                    "row_no": row_no,
                    "field": field,
                    "merged_from_row": min_row,
                    "reason": reason,
                    "values": list(values),
                }
            )
    suspects.sort(key=lambda item: (item["row_no"], item["field"]))
    return suspects


def detect_multi_versions(rows: Sequence[Any]) -> list[dict[str, Any]]:
    """疑似多版本：同一个项目码在表里出现了多个版本号。

    同一项目多版本**共存**本身是正常的（改版期新旧两版同时在目录里），
    但**同一张表里**同一个项目码挂着两个版本号，通常意味着这张表本身
    是两版拼起来的 —— 哪一版说了算不确定，必须人工确认。
    """
    grouped: dict[str, list[Any]] = {}
    for row in rows:
        code = getattr(row, "item_code", "") or ""
        if not code:
            continue
        grouped.setdefault(code, []).append(row)

    suspects: list[dict[str, Any]] = []
    for code in sorted(grouped):
        bucket = grouped[code]
        versions = sorted({(getattr(row, "version", "") or "无版本号") for row in bucket})
        if len(versions) < 2:
            continue
        row_nos = sorted(
            getattr(row, "row_no", 0) or 0 for row in bucket
        )
        suspects.append(
            {
                "item_code": code,
                "versions": versions,
                "row_nos": row_nos,
                "reason": (
                    f"项目 {code} 在同一份表里出现 {len(versions)} 个版本号"
                    f"（{'、'.join(versions)}），疑似多版本拼表，口径需人工确认"
                ),
            }
        )
    return suspects


@dataclass
class MaterialReport:
    """一份材料的解析报告（JSON 友好，可落盘、可复核）。"""

    source_name: str
    source_sha256: str
    source_path: str = ""
    file_version: str = ""
    total_rows: int = 0
    recognized_rows: int = 0
    unrecognized_rows: list[dict[str, Any]] = field(default_factory=list)
    incomplete_rows: list[dict[str, Any]] = field(default_factory=list)
    unrecognized_columns: list[str] = field(default_factory=list)
    merged_cell_rows: list[dict[str, Any]] = field(default_factory=list)
    multi_version_groups: list[dict[str, Any]] = field(default_factory=list)
    applied_overrides: list[dict[str, Any]] = field(default_factory=list)
    unapplied_overrides: list[dict[str, Any]] = field(default_factory=list)
    parsed_at: str = field(default_factory=_now)

    # ---- 构造 -----------------------------------------------------------
    @classmethod
    def from_parse_report(cls, report: ParseReport) -> "MaterialReport":
        """从 :class:`ParseReport` 升级成带疑似项的完整报告。"""
        return cls(
            source_name=report.source_name,
            source_sha256=report.source_sha256,
            source_path=report.source_path,
            file_version=report.file_version,
            total_rows=report.total_rows,
            recognized_rows=report.recognized_rows,
            unrecognized_rows=[r.to_dict() for r in report.unrecognized_rows],
            incomplete_rows=[dict(r) for r in report.incomplete_rows],
            unrecognized_columns=list(report.unrecognized_columns),
            merged_cell_rows=[dict(r) for r in report.merged_cell_rows],
            multi_version_groups=[dict(r) for r in report.multi_version_groups],
            applied_overrides=[dict(r) for r in report.applied_overrides],
            unapplied_overrides=[dict(r) for r in report.unapplied_overrides],
            parsed_at=report.parsed_at,
        )

    # ---- 判读 -----------------------------------------------------------
    @property
    def ok(self) -> bool:
        """没有未识别行才算读全了。字段缺失行不算未识别，但会单独列出来。"""
        return not self.unrecognized_rows

    @property
    def suspect_count(self) -> int:
        """需要人工留意的条数（未识别行 + 字段缺失行 + 疑似合并单元格 + 疑似多版本）。"""
        return (
            len(self.unrecognized_rows)
            + len(self.incomplete_rows)
            + len(self.merged_cell_rows)
            + len(self.multi_version_groups)
        )

    def summary(self) -> str:
        return (
            f"{self.source_name}：共 {self.total_rows} 行，识别 {self.recognized_rows} 行，"
            f"未识别 {len(self.unrecognized_rows)} 行，字段缺失 {len(self.incomplete_rows)} 行，"
            f"未识别列 {len(self.unrecognized_columns)} 个，"
            f"疑似合并单元格 {len(self.merged_cell_rows)} 行，"
            f"疑似多版本 {len(self.multi_version_groups)} 组"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_name": self.source_name,
            "source_sha256": self.source_sha256,
            "source_path": self.source_path,
            "file_version": self.file_version,
            "total_rows": self.total_rows,
            "recognized_rows": self.recognized_rows,
            "unrecognized_rows": list(self.unrecognized_rows),
            "incomplete_rows": list(self.incomplete_rows),
            "unrecognized_columns": list(self.unrecognized_columns),
            "merged_cell_rows": list(self.merged_cell_rows),
            "multi_version_groups": list(self.multi_version_groups),
            "applied_overrides": list(self.applied_overrides),
            "unapplied_overrides": list(self.unapplied_overrides),
            "parsed_at": self.parsed_at,
            "ok": self.ok,
            "suspect_count": self.suspect_count,
            "summary": self.summary(),
        }


class ParseReportStore:
    """解析报告落盘（按材料指纹寻址）。

    文件名带材料 sha256 前 16 位 —— 同一份材料重复解析不会覆盖成另一份，
    换了内容自然落到新文件。
    """

    def __init__(self, root: str | Path = default_report_dir()) -> None:
        self.root = Path(root)

    def _path_for(self, source_sha256: str) -> Path:
        return self.root / f"{source_sha256[:16]}.json"

    def save(self, report: MaterialReport) -> Path:
        """落盘一份报告，返回文件路径。"""
        self.root.mkdir(parents=True, exist_ok=True)
        target = self._path_for(report.source_sha256)
        payload = report.to_dict()
        temp = target.with_suffix(".json.tmp")
        temp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        temp.replace(target)
        return target

    def load(self, source_sha256: str) -> Optional[MaterialReport]:
        """按材料指纹取回报告；取不到返回 ``None``（不报错）。"""
        target = self._path_for(source_sha256)
        if not target.exists():
            return None
        payload = json.loads(target.read_text(encoding="utf-8"))
        return MaterialReport(
            source_name=payload.get("source_name", ""),
            source_sha256=payload.get("source_sha256", ""),
            source_path=payload.get("source_path", ""),
            file_version=payload.get("file_version", ""),
            total_rows=int(payload.get("total_rows", 0)),
            recognized_rows=int(payload.get("recognized_rows", 0)),
            unrecognized_rows=list(payload.get("unrecognized_rows", [])),
            incomplete_rows=list(payload.get("incomplete_rows", [])),
            unrecognized_columns=list(payload.get("unrecognized_columns", [])),
            merged_cell_rows=list(payload.get("merged_cell_rows", [])),
            multi_version_groups=list(payload.get("multi_version_groups", [])),
            applied_overrides=list(payload.get("applied_overrides", [])),
            unapplied_overrides=list(payload.get("unapplied_overrides", [])),
            parsed_at=payload.get("parsed_at", ""),
        )

    def list_reports(self) -> list[MaterialReport]:
        """列出全部已落盘报告（按落盘文件名排序）。"""
        if not self.root.exists():
            return []
        reports: list[MaterialReport] = []
        for path in sorted(self.root.glob("*.json")):
            payload = json.loads(path.read_text(encoding="utf-8"))
            reports.append(
                MaterialReport(
                    source_name=payload.get("source_name", ""),
                    source_sha256=payload.get("source_sha256", ""),
                    source_path=payload.get("source_path", ""),
                    file_version=payload.get("file_version", ""),
                    total_rows=int(payload.get("total_rows", 0)),
                    recognized_rows=int(payload.get("recognized_rows", 0)),
                    unrecognized_rows=list(payload.get("unrecognized_rows", [])),
                    incomplete_rows=list(payload.get("incomplete_rows", [])),
                    unrecognized_columns=list(payload.get("unrecognized_columns", [])),
                    merged_cell_rows=list(payload.get("merged_cell_rows", [])),
                    multi_version_groups=list(payload.get("multi_version_groups", [])),
                    applied_overrides=list(payload.get("applied_overrides", [])),
                    unapplied_overrides=list(payload.get("unapplied_overrides", [])),
                    parsed_at=payload.get("parsed_at", ""),
                )
            )
        return reports
