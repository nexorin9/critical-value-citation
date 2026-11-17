#!/usr/bin/env python3
"""MLLP 入站主通道复核：真起 socket → 真发帧 → 真收到事件。

跑的是产品主路径的**入站主通道那一段**：本机起一个 MLLP 监听器（帧以
``\\x0b`` 起、``\\x1c\\r`` 止），用真 socket 把一条 ``ORU^R01`` 帧送过去，
再按院内阈值表判定危急、装配带出处的处置卡并落进 SQLite 台账。

**刻意拆包发**：把一帧分成两次 ``send``（中间停一下），证明「读到一半不提前
交帧、拆包不丢帧」不是纸面承诺。

用法：

    python3 scripts/mllp_demo.py                  # 用样例帧 + 样例阈值表
    python3 scripts/mllp_demo.py --db /tmp/x.db   # 换库
    python3 scripts/mllp_demo.py --json           # 机器可读输出

**幂等口径**：事件幂等键 = ``MSH-10 + OBX-3 + OBX-14``。同一帧再送一次，
台账条数不增加（第二次被幂等挡下）。
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _samplerefs import read_sample_clause  # noqa: E402
from critval.card.builder import build_card  # noqa: E402
from critval.hl7.mllp import (  # noqa: E402
    MllpInboundProcessor,
    MllpListener,
    frames_to_mllp,
)
from critval.judge.critical import judge_event  # noqa: E402
from critval.kb.loader import load_threshold_table  # noqa: E402
from critval.ledger.store import LedgerStore, unique_citations  # noqa: E402


def _count_events(store: LedgerStore) -> int:
    with store._conn:  # noqa: SLF001 —— 复核脚本直接读一次计数
        cursor = store._conn.execute("SELECT COUNT(*) FROM events")  # noqa: SLF001
        row = cursor.fetchone()
    return int(row[0]) if row else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MLLP 入站主通道复核")
    parser.add_argument(
        "--frame",
        default=str(PROJECT_ROOT / "samples" / "hl7" / "示例ORU_R01_正常.hl7"),
        help="要送过去的 ER7 帧路径",
    )
    parser.add_argument(
        "--threshold",
        default=str(PROJECT_ROOT / "samples" / "threshold" / "示例危急值阈值表.xlsx"),
        help="阈值表 xlsx 路径",
    )
    parser.add_argument("--db", default="/tmp/critval_mllp_demo.db", help="SQLite 台账路径")
    parser.add_argument(
        "--policy",
        default=str(PROJECT_ROOT / "samples" / "policy" / "示例危急值报告制度.docx"),
        help="院内危急值报告制度 docx 路径（样例材料）",
    )
    parser.add_argument("--json", action="store_true", help="机器可读输出")
    args = parser.parse_args(argv)

    frame_bytes = Path(args.frame).read_bytes().strip()
    threshold = load_threshold_table(args.threshold)
    policy_path = Path(args.policy)
    store = LedgerStore(args.db)

    listener = MllpListener(
        "127.0.0.1:0",
        lambda frame, peer: None,
        store=store,
        host="127.0.0.1",
        port=0,
    )
    listener.start()

    outcomes: list = []
    processor = MllpInboundProcessor(
        store=store,
        dialect=None,
        item_matcher=None,
    )

    def handle(frame: bytes, peer: str) -> None:
        outcomes.append(processor(frame, peer))

    listener.handler = handle

    try:
        # 拆包发：先发半帧，停一下，再发剩下的。
        payload = frames_to_mllp(frame_bytes)
        with socket.create_connection(("127.0.0.1", listener.port), timeout=5) as client:
            client.sendall(payload[: len(payload) // 2])
            time.sleep(0.15)
            client.sendall(payload[len(payload) // 2 :])
        deadline = time.time() + 5
        while not outcomes and time.time() < deadline:
            time.sleep(0.02)
        # 再送一次同样的帧 —— 幂等要挡住。
        with socket.create_connection(("127.0.0.1", listener.port), timeout=5) as client:
            client.sendall(payload)
        time.sleep(0.3)
    finally:
        listener.stop()

    events_before = _count_events(store)
    details: list[dict[str, str]] = []
    for outcome in outcomes:
        for event in outcome.events:
            judgement = judge_event(event, threshold.rows)
            clause = read_sample_clause(policy_path)
            card = build_card(event, judgement, policy_clause=clause)
            recorded = store.record_event(event)
            if not recorded:
                # 幂等键已存在 —— 这一帧是重复投递，台账不增行、不重复出卡。
                continue
            store.record_judgement(event.event_id, judgement)
            store.record_card(card)
            details.append(
                {
                    "event_id": event.event_id,
                    "message_control_id": event.message_control_id,
                    "specimen_no": event.specimen_no,
                    "item_code": event.item_code,
                    "item_name": event.item_name,
                    "value": str(event.value),
                    "unit": event.unit,
                    "dept_code": event.dept_code,
                    "decision": judgement.decision,
                    "reason": judgement.reason,
                    "threshold_row": (
                        judgement.threshold_row.citation.locator
                        if judgement.threshold_row is not None
                        else ""
                    ),
                    "card_id": card.card_id,
                    "citations": " | ".join(
                        c.locator for c in unique_citations(card.citations)
                    ),
                }
            )
    events_after = _count_events(store)

    payload_out = {
        "listen": f"127.0.0.1:{listener.port}",
        "frames_received": len(outcomes),
        "events": details,
        "ledger_events": events_after,
        "frame_issues": [
            issue.to_dict() for outcome in outcomes for issue in outcome.issues
        ],
    }

    if args.json:
        print(json.dumps(payload_out, ensure_ascii=False, indent=2))
        return 0

    print(f"[监听] MLLP 监听已起 127.0.0.1:{listener.port}（帧 \\x0b 起、\\x1c\\r 止）")
    print(f"[发送] 一帧 {len(frame_bytes)} 字节，**拆成两次 send** 发出去")
    print(
        f"[收到] 帧 {len(outcomes)} 条（同一帧送了两次）"
        f" → 入台账 {len(details)} 条事件，重复的那次被幂等挡下"
    )
    for item in details:
        print(
            f"  [事件] {item['event_id']} · 消息 {item['message_control_id']} · "
            f"标本 {item['specimen_no']} · 科室 {item['dept_code']}"
        )
        print(
            f"    结果 {item['item_code']} {item['item_name']} = {item['value']} "
            f"{item['unit']}"
        )
        print(f"    判定 {item['decision']} · {item['reason']}")
        print(f"    依据 {item['threshold_row']}")
        print(f"    处置卡 {item['card_id']}")
        print(f"    出处 {item['citations']}")
    print(
        f"[台账] 事件 {events_after} 条"
        f"（本次新增 {events_after - events_before} 条；重复帧只处理一次，台账不增行）"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
