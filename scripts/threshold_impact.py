#!/usr/bin/env python3
"""阈值表改版影响面 + 历史判定重算（**只读复核，不改写历史事实**）。

**谁用**：检验科组长 / 质控专员。阈值表改版签发前跑一次这个命令，
看清两件事：

1. **哪些项目的判定口径变了**（阈值 / 危急方向 / 单位 / 适用人群分档），
   以及**哪些取值区间会因此翻转结论**；
2. **历史上按旧版判过的那些结论，在新版下还成不成立** —— 翻转事件清单。

**它不会改写历史台账**。重算只在内存里跑一遍，产出一份复核清单
（``reports/threshold_impact.md`` + 翻转清单 CSV）交人工处置；台账里
「当时按哪一版判的、值班人确认过没有」一行不动。要更正历史，由人另开
处置流程。

    python3 scripts/threshold_impact.py \
        --old samples/threshold/示例危急值阈值表.xlsx \
        --new samples/threshold/示例危急值阈值表_v2.xlsx \
        --db data/critval.db

退出码：0 = 跑完并落盘；1 = 材料或台账读不出来；2 = 参数错。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from critval.judge.impact import (  # noqa: E402
    build_impact_report,
    load_ruleset,
    write_flip_csv,
    write_report,
)
from critval.ledger.store import LedgerStore  # noqa: E402

DEFAULT_MD = Path("reports/threshold_impact.md")
DEFAULT_CSV = Path("reports/threshold_impact_flipped.csv")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="阈值表改版影响面分析与历史判定重算（只读，不改写历史台账）"
    )
    parser.add_argument("--old", required=True, help="改版前的阈值表 xlsx")
    parser.add_argument("--new", required=True, help="改版后的阈值表 xlsx")
    parser.add_argument(
        "--db",
        default=None,
        help="SQLite 台账路径（默认 data/critval.db；不存在则只比口径不重算）",
    )
    parser.add_argument("--from", dest="window_from", default="", help="重算窗口起 YYYY-MM-DD")
    parser.add_argument("--to", dest="window_to", default="", help="重算窗口止 YYYY-MM-DD")
    parser.add_argument("--dept", default="", help="只重算某个科室的事件（科室码）")
    parser.add_argument("--at", default=None, help="口径比对的基准时刻 YYYY-MM-DD（默认今天）")
    parser.add_argument("--limit", type=int, default=0, help="重算事件条数上限（0 = 不限）")
    parser.add_argument("--out", default=str(DEFAULT_MD), help="影响面报告 Markdown 落盘路径")
    parser.add_argument("--csv", default=str(DEFAULT_CSV), help="翻转事件清单 CSV 落盘路径")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出（机器可读）")
    args = parser.parse_args(argv)

    old_path = Path(args.old)
    new_path = Path(args.new)
    for path in (old_path, new_path):
        if not path.exists():
            print(f"阈值表不存在：{path}", file=sys.stderr)
            return 2

    old_ruleset = load_ruleset(old_path, label="old")
    new_ruleset = load_ruleset(new_path, label="new")

    store = None
    db_path = Path(args.db) if args.db else Path("data/critval.db")
    if db_path.exists():
        store = LedgerStore(db_path)
    else:
        print(
            f"提示：台账 {db_path} 不存在 —— 本次只比口径，不做历史重算。",
            file=sys.stderr,
        )

    report = build_impact_report(
        old_ruleset,
        new_ruleset,
        store,
        at=date_from(args.at),
        observed_from=args.window_from,
        observed_to=args.window_to,
        dept_code=args.dept,
        limit=args.limit,
    )

    md_path = write_report(report, args.out)
    csv_path = None
    if report.recompute is not None:
        csv_path = write_flip_csv(report.recompute.flipped, args.csv)

    payload = report.to_dict()
    payload["report_path"] = str(md_path)
    payload["flip_csv_path"] = str(csv_path) if csv_path else ""

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    print(report.render_markdown())
    print()
    print(f"影响面报告已写入: {md_path}")
    if csv_path:
        print(f"翻转清单已写入: {csv_path}")
    return 0


def date_from(text: str | None):
    """``YYYY-MM-DD`` → ``date``；空则由调用方取今天。"""
    if not text:
        return None
    from datetime import date

    return date.fromisoformat(text)


if __name__ == "__main__":
    raise SystemExit(main())
