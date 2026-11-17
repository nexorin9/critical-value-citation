#!/usr/bin/env python3
"""崩溃恢复演练：真的把进程打死，再真的按状态续跑。

这不是「模拟异常」—— 脚本会调用 :func:`os._exit` **硬退出**（不走
``finally``、不关连接、不刷缓冲），模拟发版重启 / OOM / ``kill -9``。
台账（WAL）不会因为这样死掉而损坏，重启后按事件当前状态续跑。

两个模式：

``--mode crash``
    读一帧 → 解析 → 判定 → 出卡（状态到 ``card_built``）→ 在指定崩溃点
    **硬退出**。

``--mode resume``
    重新打开库 → 跑 :class:`critval.ledger.recovery.RecoveryRunner` →
    打印逐条结论。

推送渠道在这个演练里是**本地投递箱**（一个 JSONL 文件）—— 值班台本身
就是权威，所以「推过没有」可以回查，续跑据此**不重复推送**。

用法::

    python3 scripts/crash_recovery_demo.py --mode crash --db /tmp/crash.db
    python3 scripts/crash_recovery_demo.py --mode resume --db /tmp/crash.db

崩溃点（``--crash-at``）：

``before_push``   出卡后立刻死 —— 续跑要补推一次；
``after_push``    推完（投递箱已有一行）但状态没更新就死 —— 续跑只补
                  状态、**不再推**，投递箱仍只有一行。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _samplerefs import read_sample_clause  # noqa: E402
from critval.card.builder import build_card  # noqa: E402
from critval.hl7.normalize import normalize_events, parse_oru_r01  # noqa: E402
from critval.judge.critical import judge_event  # noqa: E402
from critval.kb.loader import load_threshold_table  # noqa: E402
from critval.ledger.recovery import RecoveryRunner  # noqa: E402
from critval.ledger.store import LedgerStore  # noqa: E402

DEFAULT_FRAMES = PROJECT_ROOT / "samples" / "hl7"
DEFAULT_THRESHOLD = PROJECT_ROOT / "samples" / "threshold" / "示例危急值阈值表.xlsx"
DEFAULT_POLICY = PROJECT_ROOT / "samples" / "policy" / "示例危急值报告制度.docx"
DEFAULT_OUTBOX = PROJECT_ROOT / "reports" / "push_outbox.jsonl"


def outbox_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def push_to_outbox(path: Path, row: dict) -> bool:
    """本地投递箱渠道：每次推送追加一行（**不覆盖**）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "event_id": str(row.get("event_id", "")),
                    "card_id": str(row.get("card_id", "")),
                    "item_code": str(row.get("item_code", "")),
                    "status": str(row.get("status", "")),
                },
                ensure_ascii=False,
            )
            + "\n"
        )
        handle.flush()
        os.fsync(handle.fileno())
    return True


def already_pushed(path: Path, row: dict) -> bool:
    """渠道回查：这条事件推过没有（投递箱里有没有它）。"""
    event_id = str(row.get("event_id", ""))
    return any(str(item.get("event_id", "")) == event_id for item in outbox_rows(path))


def first_critical(store: LedgerStore, frames_dir: Path, threshold: Path, policy: Path):
    """找一条会判危急的事件，判定并出卡。"""
    table = load_threshold_table(threshold)
    clause = read_sample_clause(policy)
    for frame_path in sorted(frames_dir.glob("*.hl7")):
        message = parse_oru_r01(frame_path.read_bytes())
        events, _unparsed = normalize_events(message)
        for event in events:
            judgement = judge_event(event, table.rows)
            if judgement.decision != "critical":
                continue
            card = build_card(event, judgement, policy_clause=clause)
            return event, judgement, card, frame_path
    return None, None, None, None


def mode_crash(args: argparse.Namespace) -> int:
    with LedgerStore(args.db) as store:
        event, judgement, card, frame_path = first_critical(
            store, Path(args.frames), Path(args.threshold), Path(args.policy)
        )
        if event is None:
            print("样例帧里没有判危急的事件，演练无法进行", file=sys.stderr)
            return 2
        if store.record_event(event):
            store.record_judgement(event.event_id, judgement)
            store.record_card(card)
        print(
            f"[崩溃前] 事件 {event.event_id} · 判定 {judgement.decision}"
            f" · 处置卡 {card.card_id} · 状态 {store.get_status(event.event_id)}"
            f"（帧 {frame_path.name}）"
        )
        if args.crash_at == "after_push":
            push_to_outbox(Path(args.outbox), store.get_event(event.event_id) or {})
            print("[崩溃前] 已推送到本地投递箱（状态还没更新）")
    # 硬退出：模拟 kill -9 —— 不刷缓冲、不关连接、不走 finally。
    print(f"[崩溃] 在「{args.crash_at}」处硬退出（模拟 kill -9）")
    sys.stdout.flush()
    os._exit(9)


def mode_resume(args: argparse.Namespace) -> int:
    outbox = Path(args.outbox)
    with LedgerStore(args.db) as store:
        runner = RecoveryRunner(
            store,
            notifier=lambda row: push_to_outbox(outbox, row),
            push_check=lambda row: already_pushed(outbox, row),
            channel="local_outbox",
        )
        report = runner.resume()
    print(report.to_text())
    print("──────────────────────────────")
    rows = outbox_rows(outbox)
    print(f"投递箱投递条数 : {len(rows)} 条")
    for row in rows:
        print(f"  · {row.get('event_id', '')} · 卡 {row.get('card_id', '')}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="崩溃恢复演练（硬退出 + 按状态续跑）")
    parser.add_argument("--mode", choices=["crash", "resume"], required=True, help="演练哪一段")
    parser.add_argument("--db", default=str(PROJECT_ROOT / "data" / "crash_recovery.db"), help="SQLite 台账路径")
    parser.add_argument("--frames", default=str(DEFAULT_FRAMES), help="ER7 帧目录")
    parser.add_argument("--threshold", default=str(DEFAULT_THRESHOLD), help="阈值表 xlsx")
    parser.add_argument("--policy", default=str(DEFAULT_POLICY), help="院内制度 docx")
    parser.add_argument("--outbox", default=str(DEFAULT_OUTBOX), help="本地投递箱（JSONL）")
    parser.add_argument(
        "--crash-at",
        choices=["before_push", "after_push"],
        default="before_push",
        help="崩在推送之前还是推送之后（状态更新之前）",
    )
    args = parser.parse_args(argv)
    if args.mode == "crash":
        return mode_crash(args)
    return mode_resume(args)


if __name__ == "__main__":
    raise SystemExit(main())
