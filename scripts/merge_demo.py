#!/usr/bin/env python3
"""合并通知演示：**一条标本一张卡**（任务 id 34）。

跑的是产品主路径的**通知规划那一段**：LIS 推来一条含多个危急项目的
``ORU^R01`` 帧 → 逐项判定 → 按「标本 + 科室 + 时间窗」合并 → **一张**
处置卡推送出去，卡内逐项给出各自的阈值依据行与出处行号。

用法：

    python3 scripts/merge_demo.py                        # 跑默认多项目样例帧
    python3 scripts/merge_demo.py --frames samples/hl7   # 跑整个样例帧目录
    python3 scripts/merge_demo.py --db /tmp/merge.db     # 换库
    python3 scripts/merge_demo.py --json                 # 机器可读输出

**合并边界**：不同标本、不同科室、不在同一时间窗（``--window`` 秒，默认取
``CRITVAL_MERGE_WINDOW_SECONDS``）的结果不合到一张卡。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _samplerefs import read_sample_clause  # noqa: E402
from critval.card.merge import (  # noqa: E402
    merge_window_seconds,
    plan_notification_cards,
    render_merged_markdown,
    render_merged_text,
)
from critval.config import Config  # noqa: E402
from critval.hl7.normalize import normalize_events, parse_oru_r01  # noqa: E402
from critval.judge.critical import judge_event  # noqa: E402
from critval.judge.priority import UrgencyTable  # noqa: E402
from critval.kb.loader import load_threshold_table  # noqa: E402
from critval.ledger.store import LedgerStore  # noqa: E402

DEFAULT_FRAME = PROJECT_ROOT / "samples" / "hl7" / "示例ORU_R01_多项目.hl7"
DEFAULT_THRESHOLD = PROJECT_ROOT / "samples" / "threshold" / "示例危急值阈值表.xlsx"
DEFAULT_POLICY = PROJECT_ROOT / "samples" / "policy" / "示例危急值报告制度.docx"
DEFAULT_URGENCY = PROJECT_ROOT / "data" / "urgency.csv"
DEFAULT_DB = PROJECT_ROOT / "data" / "critval.db"

#: 帧文件后缀（ER7 是纯文本，常见 .hl7 / .txt）。
FRAME_SUFFIXES = (".hl7", ".txt")


def iter_frames(target: Path) -> list[Path]:
    """列出待处理的帧文件（按文件名排序，便于复现）。"""
    if target.is_file():
        return [target]
    if not target.exists():
        return []
    return sorted(
        path
        for path in target.iterdir()
        if path.is_file() and path.suffix.lower() in FRAME_SUFFIXES
    )


def run(
    frames: Path,
    db_path: Path,
    threshold_path: Path,
    policy_path: Path,
    urgency_path: Path,
    window: int | None,
) -> dict[str, object]:
    """跑一遍「判定 → 合并 → 出卡 → 落库」，返回可核对的摘要。"""
    Config.init_env()

    table = load_threshold_table(threshold_path)
    clause = read_sample_clause(policy_path)
    urgency = UrgencyTable.load(urgency_path)
    effective_window = merge_window_seconds() if window is None else window

    summary: dict[str, object] = {
        "frames": 0,
        "events": 0,
        "critical": 0,
        "cards": 0,
        "merged_items": 0,
        "window_seconds": effective_window,
        "threshold_table": threshold_path.name,
        "urgency_source": urgency.source,
        "cards_detail": [],
    }

    with LedgerStore(db_path) as store:
        events = []
        judgements = {}
        for frame_path in iter_frames(frames):
            summary["frames"] = int(summary["frames"]) + 1
            message = parse_oru_r01(frame_path.read_bytes())
            frame_events, unparsed = normalize_events(message)
            for record in unparsed:
                store.append_audit(
                    "unparsed_event",
                    message_control_id=record.message_control_id,
                    specimen_no=record.specimen_no,
                    detail=f"{record.reason}：{record.detail}",
                )
            for event in frame_events:
                store.record_event(event)
                judgement = judge_event(event, table.rows)
                store.record_judgement(event.event_id, judgement)
                events.append(event)
                judgements[event.event_id] = judgement
                if judgement.decision == "critical":
                    summary["critical"] = int(summary["critical"]) + 1

        summary["events"] = len(events)

        cards = plan_notification_cards(
            events,
            judgements,
            policy_clause=clause,
            urgency_table=urgency,
            window_seconds=effective_window,
        )
        summary["cards"] = len(cards)
        for card in cards:
            store.record_merged_card(card)
            summary["merged_items"] = int(summary["merged_items"]) + len(card.items)
            summary["cards_detail"].append(  # type: ignore[union-attr]
                {
                    "card_id": card.card_id,
                    "specimen_no": card.specimen_no,
                    "dept_code": card.dept_code,
                    "item_count": len(card.items),
                    "items": [
                        {
                            "item_code": item.item_code,
                            "item_name": item.item_name,
                            "value": str(item.value),
                            "unit": item.unit,
                            "priority_score": str(item.priority.score),
                            "priority_band": item.priority.band,
                            "threshold_basis": item.threshold_basis.value,
                            "basis_status": item.threshold_basis.status,
                            "locator": (
                                item.threshold_basis.source.locator
                                if item.threshold_basis.source is not None
                                else ""
                            ),
                        }
                        for item in card.items
                    ],
                    "missing_elements": list(card.missing_elements()),
                    "markdown": render_merged_markdown(card),
                    "text": render_merged_text(card),
                }
            )

    return summary


def render(summary: dict[str, object]) -> str:
    """人读的输出：合并结论 + 处置卡正文。"""
    lines: list[str] = []
    lines.append(
        f"[材料] 阈值表 {summary['threshold_table']}"
        f" · 项目紧急度表来源 {summary['urgency_source']}"
    )
    lines.append(
        f"[入站] 读帧 {summary['frames']} 条 · 事件 {summary['events']} 条"
        f" · 判危急 {summary['critical']} 条"
    )
    lines.append(
        f"[合并] 时间窗 {summary['window_seconds']} 秒"
        f" · 通知卡 {summary['cards']} 张 · 卡内危急项 {summary['merged_items']} 项"
    )
    for card in summary["cards_detail"]:  # type: ignore[index]
        lines.append("")
        lines.append(
            f"[通知卡] {card['card_id']} · 标本 {card['specimen_no']}"
            f" · 科室 {card['dept_code']} · {card['item_count']} 项合并"
        )
        for item in card["items"]:  # type: ignore[index]
            lines.append(
                f"  {item['priority_band']} · {item['item_code']} {item['item_name']}"
                f" = {item['value']} {item['unit']}"
                f"（优先级分值 {item['priority_score']}）"
            )
            lines.append(f"    依据行：{item['threshold_basis']}")
            lines.append(f"    出处  ：{item['locator']}")
        lines.append("")
        lines.append(card["markdown"])  # type: ignore[index]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="判定 → 合并 → 一条标本一张卡 → 落库")
    parser.add_argument("--frames", default=str(DEFAULT_FRAME), help="ER7 帧文件或目录")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite 库路径")
    parser.add_argument("--threshold", default=str(DEFAULT_THRESHOLD), help="阈值表 xlsx 路径")
    parser.add_argument("--policy", default=str(DEFAULT_POLICY), help="院内危急值报告制度 docx 路径")
    parser.add_argument("--urgency", default=str(DEFAULT_URGENCY), help="项目紧急度表 CSV 路径")
    parser.add_argument(
        "--window",
        type=int,
        default=None,
        help="合并时间窗（秒）；缺省取 CRITVAL_MERGE_WINDOW_SECONDS（默认 300）",
    )
    parser.add_argument("--json", action="store_true", help="输出 JSON 摘要")
    args = parser.parse_args(argv)

    summary = run(
        frames=Path(args.frames),
        db_path=Path(args.db),
        threshold_path=Path(args.threshold),
        policy_path=Path(args.policy),
        urgency_path=Path(args.urgency),
        window=args.window,
    )

    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(render(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
