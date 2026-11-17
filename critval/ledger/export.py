"""复核报表导出：xlsx（表头样式 + 列说明）与 csv（UTF-8 BOM，Excel 直开）。

**为什么 CSV 要带 BOM**：检验科组长的电脑上开的是 Excel（或 WPS）。
没有 BOM 的 UTF-8 CSV 在 Excel 里打开是乱码 —— **打开一张乱码的表比
没有表更糟**，人只会以为是系统坏了。BOM 那三个字节是 Excel 认 UTF-8
的信号，去掉它省不下任何东西。

**xlsx 里为什么有「列说明」页**：这张表是要交给质控专员、可能再转给
评审专家看的。每列到底什么意思写在表里，收到表的人不用回头问人 ——
「上报时限(分钟)」是制度里写的还是系统自己定的、「依据行」是阈值表
的行还是制度的条款，光看列名猜不出来。

**导出的内容永远是台账里的原样**：明细表的每一列都直接取自台账行，
不做二次加工（「导出来的和库里不一样」是复核最不能接受的事）。
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any, Optional, Sequence

from critval.ledger.report import (
    DETAIL_COLUMNS,
    SUMMARY_COLUMNS,
    ReviewReport,
    detail_rows,
    summary_rows,
)

__all__ = [
    "ExportDependencyError",
    "export_detail_csv",
    "export_report_xlsx",
    "export_summary_csv",
]


class ExportDependencyError(RuntimeError):
    """导出 xlsx 需要的 openpyxl 没装。"""


#: 表头配色（与值班台语义色同一套：危急红 / 待确认琥珀 / 已完成绿）。
_HEADER_FILL = "1F3A5F"
_HEADER_FONT_COLOR = "FFFFFF"
_LABEL_FILL = "EEF2F7"


def _ensure_openpyxl() -> Any:
    try:
        import openpyxl  # noqa: WPS433
    except ImportError as exc:  # pragma: no cover - 环境问题
        raise ExportDependencyError(
            "导出 xlsx 需要 openpyxl，请先安装 requirements.txt 中的依赖"
            "（或改用 --format csv 导出，CSV 路径不依赖 openpyxl）"
        ) from exc
    return openpyxl


def _prepare(path: str | Path) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


def export_detail_csv(report: ReviewReport, path: str | Path) -> Path:
    """导出明细表 CSV（UTF-8 BOM，Excel 直开）。"""
    target = _prepare(path)
    rows = detail_rows(report)
    with target.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow([column.header for column in DETAIL_COLUMNS])
        for row in rows:
            writer.writerow(
                [str(row.get(column.key, "") if row.get(column.key) is not None else "")
                 for column in DETAIL_COLUMNS]
            )
    return target


def export_summary_csv(report: ReviewReport, path: str | Path) -> Path:
    """导出汇总表 CSV（三个视角铺平成一张长表，UTF-8 BOM）。"""
    target = _prepare(path)
    rows = summary_rows(report)
    with target.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow([column.header for column in SUMMARY_COLUMNS])
        for row in rows:
            writer.writerow(
                [str(row.get(column.key, "") if row.get(column.key) is not None else "")
                 for column in SUMMARY_COLUMNS]
            )
    return target


def _style_header(worksheet: Any, columns: Sequence[Any], openpyxl: Any) -> None:
    """表头样式：深底白字加粗、居中、冻结首行。"""
    from openpyxl.styles import Alignment, Font, PatternFill

    fill = PatternFill("solid", fgColor=_HEADER_FILL)
    font = Font(bold=True, color=_HEADER_FONT_COLOR, size=11)
    align = Alignment(horizontal="center", vertical="center", wrap_text=True)
    for index, column in enumerate(columns, start=1):
        cell = worksheet.cell(row=1, column=index)
        cell.fill = fill
        cell.font = font
        cell.alignment = align
        worksheet.column_dimensions[cell.column_letter].width = column.width
    worksheet.freeze_panes = "A2"


def export_report_xlsx(report: ReviewReport, path: str | Path) -> Path:
    """导出一份 xlsx 复核报表（三个页签）。

    * **复核汇总** —— 口径、区间、总量、三类异常计数 + 三个视角的分组计数；
    * **事件明细** —— 每条事件一行（表头带样式、冻结首行、可筛选）；
    * **列说明** —— 每列的含义（明细表与汇总表各列都写清楚）。
    """
    openpyxl = _ensure_openpyxl()
    from openpyxl.styles import Alignment, Font, PatternFill

    target = _prepare(path)
    workbook = openpyxl.Workbook()

    # ---------------- 复核汇总 ----------------
    summary_sheet = workbook.active
    summary_sheet.title = "复核汇总"
    scope_label = "日报" if report.scope == "daily" else "周报"
    summary_rows_data: list[tuple[str, Any]] = [
        ("报表口径", scope_label),
        ("观察日期区间", f"{report.period_start} ~ {report.period_end}"),
        ("生成时刻", report.generated_at),
        ("事件合计", report.total_events),
        ("判定危急", report.critical),
        ("判定不危急", report.not_critical),
        ("转人工", report.transferred_to_human),
        ("超时未确认", report.anomalies.overdue_unconfirmed),
        ("待回写", report.anomalies.pending_writeback),
        (
            "失败",
            f"{report.anomalies.failed}"
            f"（推送失败 {report.anomalies.notify_failed}"
            f" + 回写失败 {report.anomalies.writeback_failed}）",
        ),
    ]
    label_font = Font(bold=True)
    label_fill = PatternFill("solid", fgColor=_LABEL_FILL)
    for row_index, (label, value) in enumerate(summary_rows_data, start=1):
        label_cell = summary_sheet.cell(row=row_index, column=1, value=label)
        label_cell.font = label_font
        label_cell.fill = label_fill
        summary_sheet.cell(row=row_index, column=2, value=value)
    summary_sheet.column_dimensions["A"].width = 18
    summary_sheet.column_dimensions["B"].width = 42

    start = len(summary_rows_data) + 2
    for perspective, bucket in (
        ("按科室", report.by_dept),
        ("按项目", report.by_item),
        ("按依据条款", report.by_basis),
    ):
        summary_sheet.cell(row=start, column=1, value=perspective).font = label_font
        start += 1
        headers = [column.header for column in SUMMARY_COLUMNS[2:]]
        for index, header in enumerate(headers, start=1):
            summary_sheet.cell(row=start, column=index, value=header)
        _style_header(summary_sheet, SUMMARY_COLUMNS[2:], openpyxl)
        start += 1
        for item in bucket:
            values = [
                item.key,
                item.label,
                item.total,
                item.critical,
                item.acknowledged,
                item.registered,
                item.overdue_unconfirmed,
                item.pending_writeback,
                item.failed,
            ]
            for index, value in enumerate(values, start=1):
                summary_sheet.cell(row=start, column=index, value=value)
            start += 1
        start += 1

    # ---------------- 事件明细 ----------------
    detail_sheet = workbook.create_sheet("事件明细")
    headers = [column.header for column in DETAIL_COLUMNS]
    for index, header in enumerate(headers, start=1):
        detail_sheet.cell(row=1, column=index, value=header)
    _style_header(detail_sheet, DETAIL_COLUMNS, openpyxl)
    for row_index, row in enumerate(detail_rows(report), start=2):
        for col_index, column in enumerate(DETAIL_COLUMNS, start=1):
            value = row.get(column.key, "")
            detail_sheet.cell(row=row_index, column=col_index, value=value)
    detail_sheet.auto_filter.ref = (
        f"A1:{detail_sheet.cell(row=1, column=len(DETAIL_COLUMNS)).column_letter}1"
    )

    # ---------------- 列说明 ----------------
    doc_sheet = workbook.create_sheet("列说明")
    doc_headers = ["表", "表头", "字段", "含义"]
    for index, header in enumerate(doc_headers, start=1):
        doc_sheet.cell(row=1, column=index, value=header)
    from openpyxl.styles import Font as _Font
    from openpyxl.styles import PatternFill as _Fill

    doc_fill = _Fill("solid", fgColor=_HEADER_FILL)
    doc_font = _Font(bold=True, color=_HEADER_FONT_COLOR, size=11)
    for index in range(1, len(doc_headers) + 1):
        cell = doc_sheet.cell(row=1, column=index)
        cell.fill = doc_fill
        cell.font = doc_font
        cell.alignment = Alignment(horizontal="center", vertical="center")
    doc_sheet.column_dimensions["A"].width = 10
    doc_sheet.column_dimensions["B"].width = 20
    doc_sheet.column_dimensions["C"].width = 24
    doc_sheet.column_dimensions["D"].width = 52
    row_index = 2
    for table_name, columns in (("事件明细", DETAIL_COLUMNS), ("汇总", SUMMARY_COLUMNS)):
        for column in columns:
            doc_sheet.cell(row=row_index, column=1, value=table_name)
            doc_sheet.cell(row=row_index, column=2, value=column.header)
            doc_sheet.cell(row=row_index, column=3, value=column.key)
            doc_sheet.cell(row=row_index, column=4, value=column.note)
            row_index += 1
    doc_sheet.freeze_panes = "A2"

    workbook.save(target)
    return target


def default_export_paths(
    scope: str, day: str, out: Optional[str] = "", *, fmt: str = "csv"
) -> dict[str, Path]:
    """导出文件默认落点（未显式指定 ``--out`` 时）。"""
    base = Path("reports") / f"ledger_review_{scope}_{day}"
    if fmt == "xlsx":
        return {"main": base.with_suffix(".xlsx")}
    return {"main": Path(f"{base}_明细.csv"), "summary": Path(f"{base}_汇总.csv")}
