#!/usr/bin/env python3
"""生成「改版后」的阈值表样例：v2 单版 + v1/v2 共存集。

为什么要两份：

* **v2 单版**（``示例危急值阈值表_v2.xlsx``）—— 用来跑改版影响面
  （``scripts/diff_versions.py``）：哪些项目新增 / 删除 / 阈值变了 /
  方向变了 / 单位变了，检验科组长签发前先看这张清单；
* **v1/v2 共存集**（``示例危急值阈值表_改版共存.xlsx``）—— 用来演示
  **多版本共存与生效期切换**：v1 于 2026-01-31 失效、v2 自 2026-02-05 生效，
  中间 2026-02-01 ~ 2026-02-04 是**版本空档**（落在空档里的结果不取就近
  版本，转人工）。

改版内容从**真实的 v1 样例**派生（不是另写一张表），保证两份表确实是
「同一张表改了一版」，diff 出来的清单可复核、可重跑。

    python3 scripts/build_version_samples.py
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _datapipe import PROJECT_ROOT, sha256_file  # noqa: E402

#: v2 的生效日期与版本号。
V2_EFFECTIVE_FROM = "2026-02-05"
V2_VERSION = "示例 v2.0"
#: v1 在共存集里的失效日期（2026-02-01 ~ 2026-02-04 因此成为版本空档）。
V1_EFFECTIVE_TO = "2026-01-31"

V1_PATH = PROJECT_ROOT / "samples" / "threshold" / "示例危急值阈值表.xlsx"
V2_PATH = PROJECT_ROOT / "samples" / "threshold" / "示例危急值阈值表_v2.xlsx"
COEXIST_PATH = PROJECT_ROOT / "samples" / "threshold" / "示例危急值阈值表_改版共存.xlsx"

HEADER = [
    "项目码",
    "项目名",
    "单位",
    "低危急值",
    "高危急值",
    "参考区间",
    "生效日期",
    "失效日期",
    "版本号",
]

#: 改版内容（项目码 → 变更说明）。值改动见 ``_apply_changes``。
CHANGES = {
    "718-7": "撤下高危急值 200（危急方向 both → low）",
    "6690-2": "高危急值 30 → 35",
    "2823-3": "低危急值 2.8 → 2.5",
    "777-3": "单位 10*9/L → 10^9/L（写法规范化，数量级不变）",
    "2601-3": "停用该项目",
    "1988-5": "新增高敏 C 反应蛋白",
}


def _read_v1(path: Path) -> list[list]:
    """读 v1 样例的表头与数据行。"""
    from openpyxl import load_workbook

    workbook = load_workbook(path, data_only=True, read_only=True)
    try:
        sheet = workbook.worksheets[0]
        rows = [list(row) for row in sheet.iter_rows(values_only=True)]
    finally:
        workbook.close()
    header, data = rows[0], rows[1:]
    # v1 的表头没有「失效日期」列 —— 补上，共存集要用。
    if "失效日期" not in header:
        header = header[:7] + ["失效日期", "版本号"]
        data = [row[:7] + [None] + row[7:] for row in data]
    return header, data


def _apply_changes(rows: list[list]) -> list[list]:
    """按 ``CHANGES`` 改出 v2 的行（新增 / 删除 / 阈值 / 方向 / 单位）。"""
    out: list[list] = []
    for row in rows:
        code = str(row[0] or "").strip()
        if code == "2601-3":
            continue  # 停用
        new_row = list(row)
        if code == "718-7":
            new_row[4] = None  # 撤下高危急值 → 方向 both → low
        elif code == "6690-2":
            new_row[4] = 35
        elif code == "2823-3":
            new_row[3] = 2.5
        elif code == "777-3":
            new_row[2] = "10^9/L"
        new_row[6] = V2_EFFECTIVE_FROM
        new_row[7] = None
        new_row[8] = V2_VERSION
        out.append(new_row)
    out.append(
        [
            "1988-5",
            "高敏C反应蛋白",
            "mg/L",
            None,
            100,
            "0-8",
            V2_EFFECTIVE_FROM,
            None,
            V2_VERSION,
        ]
    )
    return out


def _write(path: Path, header: list[str], rows: list[list]) -> Path:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "危急值阈值表"
    sheet.append(header)
    for cell in sheet[1]:
        cell.font = Font(bold=True)
        cell.fill = PatternFill("solid", fgColor="E8EEF7")
        cell.alignment = Alignment(horizontal="center", vertical="center")
    for row in rows:
        sheet.append(row)
    widths = [16, 22, 12, 12, 12, 16, 14, 14, 14]
    for index, width in enumerate(widths, start=1):
        sheet.column_dimensions[chr(64 + index)].width = width
    sheet.freeze_panes = "A2"
    path.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(path)
    return path


def build(dest_v2: Path, dest_coexist: Path, source: Path = V1_PATH) -> dict:
    """生成 v2 单版与共存集，返回产物信息。"""
    header, v1_rows = _read_v1(source)
    v2_rows = _apply_changes(v1_rows)

    _write(dest_v2, header, v2_rows)

    # 共存集：v1 行（带失效日期）+ v2 行。
    coexist_rows: list[list] = []
    for row in v1_rows:
        old = list(row)
        old[7] = V1_EFFECTIVE_TO
        coexist_rows.append(old)
    coexist_rows.extend(v2_rows)
    _write(dest_coexist, header, coexist_rows)

    return {
        "source": str(source),
        "source_sha256": sha256_file(source),
        "v2": {
            "path": str(dest_v2),
            "rows": len(v2_rows),
            "sha256": sha256_file(dest_v2),
        },
        "coexist": {
            "path": str(dest_coexist),
            "rows": len(coexist_rows),
            "sha256": sha256_file(dest_coexist),
        },
        "changes": CHANGES,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成改版后的阈值表样例（v2 单版 + v1/v2 共存集）")
    parser.add_argument("--source", default=str(V1_PATH), help="v1 阈值表样例路径")
    parser.add_argument("--v2", default=str(V2_PATH), help="v2 单版输出路径")
    parser.add_argument("--coexist", default=str(COEXIST_PATH), help="共存集输出路径")
    args = parser.parse_args(argv)

    info = build(
        dest_v2=Path(args.v2),
        dest_coexist=Path(args.coexist),
        source=Path(args.source),
    )
    print(f"源表      : {info['source']}")
    print(f"v2 单版   : {info['v2']['path']}（{info['v2']['rows']} 行）")
    print(f"共存集    : {info['coexist']['path']}（{info['coexist']['rows']} 行）")
    print("改版内容  ：")
    for code, note in info["changes"].items():
        print(f"  · {code}：{note}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
