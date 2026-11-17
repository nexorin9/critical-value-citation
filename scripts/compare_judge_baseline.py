#!/usr/bin/env python3
"""判定基线对比：只看 OBX-8 的朴素口径 vs 院内阈值表判定引擎（任务 id 9）。

## 这张表回答什么

「值班技师按「LIS 说异常就报危急值」处理，和按院内阈值表判定，到底差多少？」

差在两处，而且**两处都会在业务上出事**：

* **告警疲劳** —— ``OBX-8`` 标的是「偏离参考区间」，危急阈值比参考区间宽得多；
* **漏报** —— 有些危急值 LIS 自己没标异常。

## 两侧跑的是同一批样例

样例集由阈值表**确定性生成**（不掺随机数），两侧看到同一份阈值表，
真值取样例生成时标注的**场景意图**。编排与渲染都在
``critval.judge.baseline_compare`` 里 —— 运维 CLI 的 ``judge-baseline``
子命令调的是同一份代码，**换的是入口，不是口径**。

## 产物

* ``docs/examples/judge_baseline_compare.md`` —— 人读的对比表（含复算命令）；
* ``reports/judge_baseline_compare.csv`` —— 机器读的两行数据。

两个产物都**不含时间戳**：同一命令跑两次应得到逐字节相同的文件。

用法::

    python3 scripts/compare_judge_baseline.py                    # 默认阈值表
    python3 scripts/compare_judge_baseline.py --json             # 只打 JSON
    python3 scripts/compare_judge_baseline.py --threshold <xlsx> # 换阈值表
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from critval.judge.baseline_compare import (  # noqa: E402
    build_judge_cases,
    compare_judge_baselines,
    render_comparison_markdown,
    write_comparison_csv,
)
from critval.judge.ruleset import RuleSet  # noqa: E402
from critval.kb.loader import load_threshold_table  # noqa: E402

DEFAULT_THRESHOLD = PROJECT_ROOT / "samples" / "threshold" / "示例危急值阈值表.xlsx"
DEFAULT_MD = PROJECT_ROOT / "docs" / "examples" / "judge_baseline_compare.md"
DEFAULT_CSV = PROJECT_ROOT / "reports" / "judge_baseline_compare.csv"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python3 scripts/compare_judge_baseline.py",
        description="判定基线对比（只看 OBX-8 vs 院内阈值表判定引擎）",
    )
    parser.add_argument(
        "--threshold", default=str(DEFAULT_THRESHOLD), help="院内阈值表 xlsx 路径"
    )
    parser.add_argument("--out", default=str(DEFAULT_MD), help="Markdown 对比表落盘路径")
    parser.add_argument("--csv-out", default=str(DEFAULT_CSV), help="CSV 对比表落盘路径")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出（机器可读）")
    args = parser.parse_args(argv)

    threshold_path = Path(args.threshold)
    if not threshold_path.exists():
        print(f"阈值表不存在：{threshold_path}", file=sys.stderr)
        return 2

    table = load_threshold_table(threshold_path)
    # 规则集快照 ID 由**材料指纹**推导（不用注册表的版本计数器）——
    # 注册表的计数器随进程内重载次数递增，会让同一份材料的对比表
    # 在不同进程里长得不一样，「同一命令可复算」就守不住了。
    ruleset = RuleSet.compile(
        table.rows,
        snapshot_id=f"rs-cmp-{table.source_sha256[:12]}",
        source_name=table.source_name,
        source_sha256=table.source_sha256,
    )
    cases = build_judge_cases(table.rows)
    comparison = compare_judge_baselines(cases, table.rows, ruleset=ruleset)

    markdown = render_comparison_markdown(comparison)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(markdown, encoding="utf-8")
    csv_path = write_comparison_csv(comparison, Path(args.csv_out))

    payload = comparison.to_dict()
    payload["out"] = str(out_path)
    payload["csv_out"] = str(csv_path)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    base_row, engine_row = comparison.rows
    print(
        f"判定基线对比（{comparison.case_count} 条样例 · 阈值表 "
        f"{comparison.threshold_source or threshold_path.name}）"
    )
    for row in comparison.rows:
        print(
            f"  {row['参赛者']:<26} 判危急 {row['判危急数']:>3} ｜ 判不危急 "
            f"{row['判不危急数']:>3} ｜ 不判定 {row['不判定数']:>3} ｜ 口径分歧检出 "
            f"{row['口径分歧检出数']:>3} ｜ 误报 {row['误报数']:>3} ｜ 漏报 {row['漏报数']:>3}"
        )
    print(
        f"  差值：误报 {base_row['误报数'] - engine_row['误报数']:+d} 条 ｜ 漏报 "
        f"{base_row['漏报数'] - engine_row['漏报数']:+d} 条 ｜ 口径分歧检出 "
        f"{engine_row['口径分歧检出数'] - base_row['口径分歧检出数']:+d} 条"
    )
    print(f"对比表 → {out_path} ｜ {csv_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
