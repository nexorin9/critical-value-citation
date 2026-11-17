#!/usr/bin/env python3
"""生成单测用的脱敏阈值表样例（可重跑）。

样例刻意包含三类"不整齐"的行，用来验证解析报告：

* 缺项目码的行 → 未识别行（必须进报告，不静默跳过）；
* 阈值列为空的行 → 字段缺失行（识别到了但不可判定）；
* 单位写得非标准的行 → 换算查不到时不得判定。

    python3 tests/fixtures/build_threshold_fixture.py
"""

from __future__ import annotations

from pathlib import Path

from openpyxl import Workbook

HEADER = ["项目码", "项目名", "单位", "低危急值", "高危急值", "参考区间", "生效日期", "版本号"]

ROWS = [
    ["718-7", "血红蛋白", "g/L", 30, 200, "115-150", "2026-01-01", "测试 v1.0"],
    ["2823-3", "钾", "mmol/L", 2.8, 6.5, "3.5-5.3", "2026-01-01", "测试 v1.0"],
    ["2345-7", "葡萄糖", "mmol/L", 2.8, 22.3, "男 3.9-6.1；女 3.9-6.1", "2026-01-01", "测试 v1.0"],
    ["9999-9", "某未标定项目", "未标定单位", 1, 2, "1-2", "2026-01-01", "测试 v1.0"],
    ["8888-8", "缺阈值项目", "mmol/L", None, None, "1-2", "2026-01-01", "测试 v1.0"],
    ["", "缺项目码行", "mmol/L", 1, 2, "1-2", "2026-01-01", "测试 v1.0"],
]


def build(dest: Path) -> Path:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "危急值阈值表"
    sheet.append(HEADER)
    for row in ROWS:
        sheet.append(row)
    dest.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(dest)
    return dest


if __name__ == "__main__":
    target = Path(__file__).resolve().parent / "脱敏样例阈值表.xlsx"
    print("已生成", build(target))
