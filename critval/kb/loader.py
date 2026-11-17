"""阈值表 xlsx → 行级条目。

读进来的每一行都带：规范字段 + sha256 指纹 + 版本号 + 生效期 + **可读行号**
（xlsx 里第几行）。可读行号是处置卡上「依据第几行」的来源。

未识别的行（没有项目码）**不静默跳过**，一律进解析报告待人工确认；
识别了但阈值缺失的行也单独列出来 —— 「读到了但不能用」和「没读到」是两回事。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Sequence

from critval.kb.parse_report import (
    detect_merged_cells,
    detect_multi_versions,
    read_merged_ranges,
)
from critval.kb.schema import (
    parse_applies_age,
    parse_applies_sex,
    parse_date,
    parse_threshold_value,
    resolve_threshold_columns,
)
from critval.kb.version import ParseReport, sha256_file
from critval.models import Citation, ThresholdRow

__all__ = ["ThresholdTable", "load_threshold_table", "load_threshold_rows"]

#: 表头最多往下找几行（有些表前面有标题行）。
_HEADER_SEARCH_DEPTH = 5

#: 人工回填覆盖：``Excel 行号 → {规范字段名: 值}``。
OverrideMap = dict[int, dict[str, Any]]


@dataclass
class ThresholdTable:
    """一份阈值表的读取结果：行级条目 + 解析报告。"""

    source_name: str
    source_sha256: str
    rows: list[ThresholdRow]
    report: ParseReport
    raw_rows: list[list[Any]] = field(default_factory=list)

    def by_item_code(self, item_code: str) -> list[ThresholdRow]:
        return [row for row in self.rows if row.item_code == item_code]

    def first(self, item_code: str) -> Optional[ThresholdRow]:
        matches = self.by_item_code(item_code)
        return matches[0] if matches else None

    def versions(self) -> list[str]:
        seen: list[str] = []
        for row in self.rows:
            if row.version and row.version not in seen:
                seen.append(row.version)
        return seen


def _cell(row: Sequence[Any], index: Optional[int]) -> Any:
    if index is None or index >= len(row):
        return None
    return row[index]


def _is_blank_row(values: Sequence[Any]) -> bool:
    return all(v is None or (isinstance(v, str) and not v.strip()) for v in values)


def _find_header(rows: list[Sequence[Any]]) -> int:
    """找表头行下标；找不到就退回第 0 行。"""
    best_index = 0
    best_score = -1
    for index, row in enumerate(rows[:_HEADER_SEARCH_DEPTH]):
        mapping, _ = resolve_threshold_columns(list(row))
        score = len(mapping)
        if "item_code" in mapping:
            score += 5
        if score > best_score:
            best_score = score
            best_index = index
    return best_index if best_score > 0 else 0


def load_threshold_table(
    path: str | Path,
    overrides: Optional[OverrideMap] = None,
) -> ThresholdTable:
    """读取阈值表 xlsx。

    缺失依赖（openpyxl 未装）时显式报错，不静默返回空表。

    ``overrides`` 是人工回填的内容（``Excel 行号 → {规范字段名: 值}``），
    解析前先把回填值写回对应单元格 —— 确认过的东西要真的生效，
    不能只在队列里记一笔。
    """
    try:
        from openpyxl import load_workbook
    except ImportError as exc:  # pragma: no cover - 环境问题
        raise RuntimeError("读取阈值表需要 openpyxl，请先安装 requirements.txt 中的依赖") from exc

    source = Path(path)
    if not source.exists():
        raise FileNotFoundError(f"阈值表不存在：{source}")

    digest = sha256_file(source)
    workbook = load_workbook(source, data_only=True, read_only=True)
    try:
        sheet = workbook.worksheets[0]
        raw_rows = [list(row) for row in sheet.iter_rows(values_only=True)]
    finally:
        workbook.close()

    rows, report = load_threshold_rows(
        raw_rows,
        source_name=source.name,
        source_sha256=digest,
        overrides=overrides,
        merged_ranges=read_merged_ranges(source),
    )
    report.source_path = str(source)
    return ThresholdTable(
        source_name=source.name,
        source_sha256=digest,
        rows=rows,
        report=report,
        raw_rows=raw_rows,
    )


def _apply_overrides(
    raw_rows: list[Sequence[Any]],
    mapping: dict[str, int],
    overrides: Optional[OverrideMap],
    report: ParseReport,
) -> list[list[Any]]:
    """把人工回填值写回原始行。

    写不进去的（表里根本没有那一列、行号越界）**照实记进未生效清单** ——
    假装回填成功比不回填更危险。
    """
    if not overrides:
        return [list(row) for row in raw_rows]

    patched = [list(row) for row in raw_rows]
    for row_no, fields in sorted(overrides.items()):
        index = row_no - 1
        if index < 0 or index >= len(patched):
            report.unapplied_overrides.append(
                {
                    "row_no": row_no,
                    "field": "",
                    "value": "",
                    "reason": f"回填未生效：行号 {row_no} 超出表格范围",
                }
            )
            continue
        for field_name, value in fields.items():
            column = mapping.get(field_name)
            if column is None:
                report.unapplied_overrides.append(
                    {
                        "row_no": row_no,
                        "field": field_name,
                        "value": str(value),
                        "reason": f"回填未生效：表里没有「{field_name}」这一列",
                    }
                )
                continue
            row = patched[index]
            while len(row) <= column:
                row.append(None)
            row[column] = value
            report.applied_overrides.append(
                {
                    "row_no": row_no,
                    "field": field_name,
                    "value": str(value),
                    "reason": "回填已生效",
                }
            )
    return patched


def load_threshold_rows(
    raw_rows: list[Sequence[Any]],
    source_name: str,
    source_sha256: str,
    overrides: Optional[OverrideMap] = None,
    merged_ranges: Sequence[tuple[int, int, int, int]] = (),
) -> tuple[list[ThresholdRow], ParseReport]:
    """把表格原始行解析成阈值行 + 解析报告（不碰文件，便于单测）。"""
    report = ParseReport(source_name=source_name, source_sha256=source_sha256)

    if not raw_rows:
        return [], report

    header_index = _find_header(raw_rows)
    header = list(raw_rows[header_index])
    mapping, unrecognized_columns = resolve_threshold_columns(header)
    report.unrecognized_columns = unrecognized_columns

    raw_rows = _apply_overrides(list(raw_rows), mapping, overrides, report)

    if "item_code" not in mapping:
        report.add_unrecognized(
            row_no=header_index + 1,
            reason="表头缺少「项目码」列，整表无法作为阈值表使用",
            values=tuple(header),
        )
        report.total_rows = len(raw_rows) - header_index - 1
        return [], report

    rows: list[ThresholdRow] = []
    file_versions: list[str] = []

    for offset, raw in enumerate(raw_rows[header_index + 1 :], start=header_index + 2):
        if _is_blank_row(raw):
            continue
        report.total_rows += 1
        row_no = offset

        item_code_raw = _cell(raw, mapping.get("item_code"))
        item_code = str(item_code_raw).strip() if item_code_raw is not None else ""
        if not item_code:
            report.add_unrecognized(row_no=row_no, reason="缺少项目码，无法与检验结果匹配", values=tuple(raw))
            continue

        item_name_raw = _cell(raw, mapping.get("item_name"))
        unit_raw = _cell(raw, mapping.get("unit"))
        low_cell = _cell(raw, mapping.get("low_critical"))
        high_cell = _cell(raw, mapping.get("high_critical"))
        low, low_inclusive = parse_threshold_value(low_cell)
        high, high_inclusive = parse_threshold_value(high_cell)
        raw_low = "" if low_cell is None else str(low_cell).strip()
        raw_high = "" if high_cell is None else str(high_cell).strip()
        range_raw = _cell(raw, mapping.get("reference_range"))
        effective_from = parse_date(_cell(raw, mapping.get("effective_from")))
        effective_to = parse_date(_cell(raw, mapping.get("effective_to")))
        version_raw = _cell(raw, mapping.get("version"))
        version = str(version_raw).strip() if version_raw is not None else ""
        if version:
            file_versions.append(version)

        # 适用人群分档：性别 / 年龄段（同一项目可拆成多行）
        sex_cell = _cell(raw, mapping.get("applies_sex"))
        applies_sex, sex_parsed = parse_applies_sex(sex_cell)
        age_scope = parse_applies_age(_cell(raw, mapping.get("applies_age")))

        scope_errors: list[str] = []
        if not sex_parsed:
            scope_errors.append("适用性别写法无法识别")
        if not age_scope.parsed:
            scope_errors.append("适用年龄段写法无法识别")
        scope_error = "、".join(scope_errors)

        missing: list[str] = []
        if low is None and high is None:
            missing.append("低危急值/高危急值")
        if not unit_raw:
            missing.append("单位")
        missing.extend(scope_errors)
        if missing:
            report.add_incomplete(row_no=row_no, reason="字段缺失：" + "、".join(missing), values=tuple(raw))

        citation = Citation(
            source_name=source_name,
            source_sha256=source_sha256,
            kind="threshold_row",
            line_no=row_no,
            file_version=version or None,
        )
        rows.append(
            ThresholdRow(
                item_code=item_code,
                item_name=str(item_name_raw).strip() if item_name_raw is not None else "",
                unit=str(unit_raw).strip() if unit_raw is not None else "",
                low_critical=low,
                high_critical=high,
                reference_range_raw=str(range_raw).strip() if range_raw is not None else "",
                effective_from=effective_from,
                version=version,
                citation=citation,
                row_no=row_no,
                note="；".join(missing),
                raw_low=raw_low,
                raw_high=raw_high,
                low_inclusive=low_inclusive,
                high_inclusive=high_inclusive,
                effective_to=effective_to,
                applies_sex=applies_sex,
                applies_age_min=age_scope.age_min,
                applies_age_max=age_scope.age_max,
                applies_age_label=age_scope.label,
                scope_error=scope_error,
            )
        )
        report.recognized_rows += 1

    # 两类最容易把口径带偏的情况单独标出来，交人工确认：
    # 合并单元格（项目码整列留空）与同一张表里混着多个版本。
    report.merged_cell_rows = detect_merged_cells(raw_rows, mapping, header_index, merged_ranges)
    report.multi_version_groups = detect_multi_versions(rows)

    report.file_version = file_versions[0] if file_versions else ""
    return rows, report
