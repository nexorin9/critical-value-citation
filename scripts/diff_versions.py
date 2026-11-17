#!/usr/bin/env python3
"""阈值表改版影响面：比较两版阈值表，输出变更项目清单。

**谁用**：检验科组长。阈值表改版签发前，先跑一次这个命令，看清
「哪些项目的判定门槛变了、哪些项目新增或删除」，再决定何时切换。
清单写进 ``reports/threshold_version_diff.csv``（UTF-8 BOM，Excel 直开），
可以附在改版审批单后面。

    python3 scripts/diff_versions.py \
        --old samples/threshold/示例危急值阈值表.xlsx \
        --new samples/threshold/示例危急值阈值表_v2.xlsx

退出码：0 = 比完并落盘；1 = 材料读不出来；2 = 参数错。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from critval.kb.loader import load_threshold_table  # noqa: E402
from critval.kb.version_diff import (  # noqa: E402
    CHANGE_LABELS,
    diff_threshold_tables,
    summarize,
    write_csv,
)

DEFAULT_OUT = Path("reports/threshold_version_diff.csv")


def _versions_of(versions) -> str:
    """版本号列表 → 人读文本。"""
    return "、".join(versions) if versions else "（无版本号）"


def build_report(old_path: Path, new_path: Path) -> dict:
    """读两版阈值表并算出变更清单。"""
    old_table = load_threshold_table(old_path)
    new_table = load_threshold_table(new_path)
    diffs = diff_threshold_tables(old_table, new_table)
    summary = summarize(diffs)
    return {
        "old": {
            "path": str(old_path),
            "name": old_table.source_name,
            "sha256": old_table.source_sha256,
            "versions": old_table.versions(),
            "rows": len(old_table.rows),
            "parse_ok": old_table.report.ok,
        },
        "new": {
            "path": str(new_path),
            "name": new_table.source_name,
            "sha256": new_table.source_sha256,
            "versions": new_table.versions(),
            "rows": len(new_table.rows),
            "parse_ok": new_table.report.ok,
        },
        "summary": summary,
        "diffs": [diff.to_dict() for diff in diffs],
    }


def render_text(report: dict) -> str:
    """人读版改版影响面。"""
    old = report["old"]
    new = report["new"]
    summary = report["summary"]
    counts = summary["counts"]

    lines = [
        "阈值表改版影响面",
        "──────────────────",
        f"旧版本 : {old['name']} · {_versions_of(old['versions'])} · {old['rows']} 行"
        f" · sha256 {old['sha256'][:12]}",
        f"新版本 : {new['name']} · {_versions_of(new['versions'])} · {new['rows']} 行"
        f" · sha256 {new['sha256'][:12]}",
    ]
    if not old["parse_ok"] or not new["parse_ok"]:
        lines.append(
            "⚠ 两版材料都有未识别行（解析报告未通过），清单不完整 —— 先确认材料再谈改版"
        )
    lines.append(
        f"变更合计: {summary['total']} 条，涉及 {summary['changed_item_count']} 个项目"
    )
    lines.append(
        "分类计数: "
        + " / ".join(f"{CHANGE_LABELS[key]} {counts.get(key, 0)}" for key in CHANGE_LABELS)
    )
    lines.append("")
    if not report["diffs"]:
        lines.append("（两版逐项一致，无变更）")
        return "\n".join(lines)

    lines.append("变更清单：")
    for diff in report["diffs"]:
        where = ""
        if diff["old_line_no"] and diff["new_line_no"]:
            where = f"旧第 {diff['old_line_no']} 行 → 新第 {diff['new_line_no']} 行"
        elif diff["new_line_no"]:
            where = f"新第 {diff['new_line_no']} 行"
        elif diff["old_line_no"]:
            where = f"旧第 {diff['old_line_no']} 行"
        lines.append(f"  · [{diff['label']}] {diff['item_code']} {diff['item_name']} —— {diff['detail']}")
        if where:
            lines.append(f"      {where}")
    lines.append("")
    lines.append("复核提示：阈值变化与方向变化会影响历史判定，改版后需复核；"
                 "删除项目意味着那条兜底没了，须确认是否有意停用。")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="比较两版阈值表，输出改版影响面清单")
    parser.add_argument("--old", required=True, help="旧版阈值表 xlsx")
    parser.add_argument("--new", required=True, help="新版阈值表 xlsx")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="变更清单 CSV 输出路径")
    parser.add_argument("--markdown", default=None, help="变更清单 Markdown 输出路径（可选）")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出（机器可读）")
    args = parser.parse_args(argv)

    old_path = Path(args.old)
    new_path = Path(args.new)
    for path in (old_path, new_path):
        if not path.exists():
            print(f"阈值表不存在：{path}", file=sys.stderr)
            return 2

    report = build_report(old_path, new_path)
    diffs = report["diffs"]

    # 重新算一遍对象以便写 CSV（JSON 化后丢了 dataclass 类型）。
    from critval.kb.version_diff import ItemDiff

    objects = [
        ItemDiff(
            item_code=item["item_code"],
            item_name=item["item_name"],
            change=item["change"],
            detail=item["detail"],
            old_line_no=item["old_line_no"],
            new_line_no=item["new_line_no"],
            old_value=item["old_value"],
            new_value=item["new_value"],
            old_version=item["old_version"],
            new_version=item["new_version"],
        )
        for item in diffs
    ]
    out_path = write_csv(objects, args.out)

    if args.markdown:
        md_path = Path(args.markdown)
        md_path.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            "# 阈值表改版影响面",
            "",
            f"- 旧版本：{report['old']['name']} · {report['old']['versions']}",
            f"- 新版本：{report['new']['name']} · {report['new']['versions']}",
            f"- 变更合计：{report['summary']['total']} 条，涉及 {report['summary']['changed_item_count']} 个项目",
            "",
            "| 项目码 | 项目名 | 变更类型 | 变更说明 | 旧行号 | 新行号 |",
            "|------|------|------|------|------|------|",
        ]
        for item in diffs:
            lines.append(
                f"| {item['item_code']} | {item['item_name']} | {item['label']} | "
                f"{item['detail']} | {item['old_line_no'] or ''} | {item['new_line_no'] or ''} |"
            )
        md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    if args.json:
        report["csv_path"] = str(out_path)
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    print(render_text(report))
    print()
    print(f"变更清单已写入: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
