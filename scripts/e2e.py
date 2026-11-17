#!/usr/bin/env python3
"""端到端复核：ER7 帧 → 解析 → 判定 → 处置卡 → SQLite 落盘。

跑的是产品主路径的**入站那一段**：LIS 推来的 ``ORU^R01`` 帧，经过
ER7 解析与事件规范化，按院内阈值表判定危急，装配成带出处的处置卡，
最后把事件、判定结论、卡片与引用出处一起落进 SQLite 台账。

用法：

    python3 scripts/e2e.py                       # 读 samples/hl7/ 下的样例帧
    python3 scripts/e2e.py --frames data/synth/oru --limit 20
    python3 scripts/e2e.py --db /tmp/e2e.db      # 换库
    python3 scripts/e2e.py --json                # 机器可读输出

**幂等口径**：事件幂等键 = ``MSH-10 + OBX-3 + OBX-14``。同一条结果重复到达
只处理一次 —— 连跑两遍，第二遍全部是「幂等跳过」，台账行数不增加。

制度条款用的是样例 docx 的**最小读取器**（正式制度解析器在 ``critval/kb``，
任务 id 5），此处不冒充它。
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
from critval.card.builder import build_card  # noqa: E402
from critval.config import Config  # noqa: E402
from critval.hl7.normalize import normalize_events, parse_oru_r01  # noqa: E402
from critval.judge.critical import judge_event  # noqa: E402
from critval.judge.explain import build_judgement_record  # noqa: E402
from critval.kb.loader import load_threshold_table  # noqa: E402
from critval.ledger.store import LedgerStore, unique_citations  # noqa: E402

DEFAULT_FRAMES = PROJECT_ROOT / "samples" / "hl7"
DEFAULT_THRESHOLD = PROJECT_ROOT / "samples" / "threshold" / "示例危急值阈值表.xlsx"
DEFAULT_POLICY = PROJECT_ROOT / "samples" / "policy" / "示例危急值报告制度.docx"
DEFAULT_DB = PROJECT_ROOT / "data" / "critval.db"

#: 帧文件后缀（ER7 是纯文本，常见 .hl7 / .txt）。
FRAME_SUFFIXES = (".hl7", ".txt")


def iter_frames(directory: Path, limit: int = 0) -> list[Path]:
    """列出待处理的帧文件（按文件名排序，便于复现）。"""
    if not directory.exists():
        return []
    frames = sorted(
        path
        for path in directory.iterdir()
        if path.is_file() and path.suffix.lower() in FRAME_SUFFIXES
    )
    return frames[:limit] if limit else frames


def run(
    frames_dir: Path,
    db_path: Path,
    threshold_path: Path,
    policy_path: Path,
    limit: int = 0,
) -> dict[str, object]:
    """跑一遍入站主路径，返回可核对的处理摘要。"""
    Config.init_env()

    table = load_threshold_table(threshold_path)
    clause = read_sample_clause(policy_path)

    summary: dict[str, object] = {
        "frames": 0,
        "events_new": 0,
        "events_duplicate": 0,
        "unparsed": 0,
        "cards": [],
        "unparsed_records": [],
        "event_total": 0,
        "threshold_table": threshold_path.name,
        "parse_report": table.report.summary(),
    }

    with LedgerStore(db_path) as store:
        for frame_path in iter_frames(frames_dir, limit):
            summary["frames"] = int(summary["frames"]) + 1
            message = parse_oru_r01(frame_path.read_bytes())
            events, unparsed = normalize_events(message)

            for record in unparsed:
                summary["unparsed"] = int(summary["unparsed"]) + 1
                store.append_audit(
                    "unparsed_event",
                    message_control_id=record.message_control_id,
                    specimen_no=record.specimen_no,
                    item_code="",
                    detail=f"{record.reason}：{record.detail}",
                )
                summary["unparsed_records"].append(  # type: ignore[union-attr]
                    {
                        "frame": frame_path.name,
                        "reason": record.reason,
                        "detail": record.detail,
                    }
                )

            for event in events:
                if not store.record_event(event):
                    summary["events_duplicate"] = int(summary["events_duplicate"]) + 1
                    store.append_audit(
                        "event_duplicate",
                        event_id=event.event_id,
                        dept_code=event.dept_code,
                        specimen_no=event.specimen_no,
                        item_code=event.item_code,
                        detail="幂等键已存在，本次跳过（既有事实不被覆盖）",
                    )
                    continue

                summary["events_new"] = int(summary["events_new"]) + 1
                judgement = judge_event(event, table.rows)
                store.record_judgement(event.event_id, judgement)
                # 判定链（任务 id 36）：结论写在事件行，过程写在判定记录表，
                # 事后按事件 ID 可逐步复算。
                store.record_judgement_record(
                    build_judgement_record(
                        event, judgement, threshold_source_sha256=table.source_sha256
                    )
                )

                card = build_card(event, judgement, policy_clause=clause)
                store.record_card(card)

                citations = [
                    {
                        "source_name": citation.source_name,
                        "source_sha256": citation.source_sha256,
                        "line_no": citation.line_no,
                        "paragraph_no": citation.paragraph_no,
                        "kind": citation.kind,
                        "locator": citation.locator,
                    }
                    for citation in unique_citations(card.citations)
                ]
                summary["cards"].append(  # type: ignore[union-attr]
                    {
                        "frame": frame_path.name,
                        "event_id": event.event_id,
                        "message_control_id": event.message_control_id,
                        "specimen_no": event.specimen_no,
                        "item_code": event.item_code,
                        "item_name": event.item_name,
                        "value": str(event.value),
                        "unit": event.unit,
                        "observed_at": event.observed_at.isoformat(),
                        "dept_code": event.dept_code,
                        "obx_flag": event.obx_flag,
                        "result_status": event.result_status,
                        "decision": judgement.decision,
                        "reason": judgement.reason,
                        "card_id": card.card_id,
                        "missing_elements": list(card.missing_elements()),
                        "citations": citations,
                    }
                )

        summary["event_total"] = store.count_events()

    return summary


def render(summary: dict[str, object]) -> str:
    """人读的输出：每条事件 + 它的出处。"""
    lines: list[str] = []
    lines.append(f"[材料解析] 阈值表 {summary['threshold_table']} · {summary['parse_report']}")
    lines.append(
        f"[入站] 读帧 {summary['frames']} 条 · 新增事件 {summary['events_new']} 条"
        f" · 幂等跳过 {summary['events_duplicate']} 条 · 未识别 {summary['unparsed']} 条"
    )
    for record in summary["unparsed_records"]:  # type: ignore[index]
        lines.append(f"  ! 未识别 {record['frame']} · {record['reason']} · {record['detail']}")
    for card in summary["cards"]:  # type: ignore[index]
        lines.append("")
        lines.append(
            f"[事件] {card['event_id']} · 消息 {card['message_control_id']}"
            f" · 标本 {card['specimen_no']} · 科室 {card['dept_code']}"
        )
        lines.append(
            f"  结果 {card['item_code']} {card['item_name']} = {card['value']} {card['unit']}"
            f"（观察 {card['observed_at']} · 异常标志 {card['obx_flag'] or '无'}"
            f" · 结果状态 {card['result_status'] or '无'}）"
        )
        lines.append(f"  判定 {card['decision']} · {card['reason']}")
        lines.append(f"  处置卡 {card['card_id']} · 缺项 {card['missing_elements'] or '无'}")
        for citation in card["citations"]:
            lines.append(
                f"  出处 {citation['source_name']} · 第 {citation['line_no']} 行"
                f" · sha256 {citation['source_sha256'][:16]}…"
            )
    lines.append("")
    lines.append(f"[台账] {summary['event_total']} 条事件已落库（{summary['threshold_table']}）")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ER7 帧 → 判定 → 处置卡 → SQLite 落盘")
    parser.add_argument("--frames", default=str(DEFAULT_FRAMES), help="ER7 帧目录")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite 库路径")
    parser.add_argument("--threshold", default=str(DEFAULT_THRESHOLD), help="阈值表 xlsx 路径")
    parser.add_argument("--policy", default=str(DEFAULT_POLICY), help="院内危急值报告制度 docx 路径")
    parser.add_argument("--limit", type=int, default=0, help="最多处理多少帧（0 = 全部）")
    parser.add_argument("--json", action="store_true", help="输出 JSON 摘要")
    args = parser.parse_args(argv)

    summary = run(
        frames_dir=Path(args.frames),
        db_path=Path(args.db),
        threshold_path=Path(args.threshold),
        policy_path=Path(args.policy),
        limit=args.limit,
    )

    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(render(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
