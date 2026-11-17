#!/usr/bin/env python3
"""台账闭环演示：接收 → 判定 → 出卡 → 推送 → 待回执 → 到期重推 → 确认 → 回写。

用法：

    python3 scripts/ledger_demo.py
    python3 scripts/ledger_demo.py --db data/ledger_demo.db --item 718-7 --value 25

跑的是台账这条主路径（产品主路径的「事实线」），演示五件事：

1. **状态机按表推进** —— 跳步会被拒（演示里故意跳一次 ``judged → notified``）；
2. **每次迁移写审计** —— 审计流水逐条能看到状态怎么走的；
3. **到期重推不重复计数** —— 首次推送时刻不被覆盖，「推送条数」按去重事件数算；
4. **人工确认点不可越过** —— 只有值班人确认后才回写登记系统；
5. **四问可答** —— 按哪条依据 / 通知了谁 / 几分钟内 / 谁确认的。

推送出口用的是本脚本内的**本地记录器**（把推送内容记到内存并打印）——
正式出站通道（企微群机器人）在 ``critval/notify/``，本脚本不冒充它。
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from critval.card.builder import build_card  # noqa: E402
from critval.config import Config  # noqa: E402
from critval.judge.critical import judge_event  # noqa: E402
from critval.kb.loader import load_threshold_table  # noqa: E402
from critval.ledger.audit import AuditLog  # noqa: E402
from critval.ledger.retry import RetryPlanner, RetryRunner  # noqa: E402
from critval.ledger.store import LedgerStore  # noqa: E402
from critval.models import CriticalEvent  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _samplerefs import read_sample_clause  # noqa: E402

DEFAULT_THRESHOLD = PROJECT_ROOT / "samples" / "threshold" / "示例危急值阈值表.xlsx"
DEFAULT_POLICY = PROJECT_ROOT / "samples" / "policy" / "示例危急值报告制度.docx"
DEFAULT_DB = PROJECT_ROOT / "data" / "ledger_demo.db"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="台账闭环演示（状态机 + 审计 + 到期重推）")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite 台账路径")
    parser.add_argument("--threshold", default=str(DEFAULT_THRESHOLD), help="阈值表 xlsx 路径")
    parser.add_argument("--policy", default=str(DEFAULT_POLICY), help="制度 docx 路径")
    parser.add_argument("--item", default="718-7", help="检验项目码")
    parser.add_argument("--value", default="25", help="观察值")
    parser.add_argument("--unit", default="g/L", help="单位")
    parser.add_argument("--dept", default="ICU", help="患者所在科室")
    parser.add_argument("--duty", default="检验科值班技师", help="当班人（角色）")
    parser.add_argument("--keep", action="store_true", help="保留已有台账（默认先删旧演示库）")
    args = parser.parse_args(argv)

    Config.init_env()

    db_path = Path(args.db)
    if db_path.exists() and not args.keep:
        db_path.unlink()
        for suffix in ("-wal", "-shm"):
            extra = db_path.with_name(db_path.name + suffix)
            if extra.exists():
                extra.unlink()

    table = load_threshold_table(args.threshold)
    row = table.first(args.item)
    if row is None:
        print(f"阈值表未收录项目 {args.item}")
        return 2

    pushed_at = datetime(2026, 1, 2, 8, 30, tzinfo=timezone.utc)
    event = CriticalEvent(
        event_id="demo-ledger-0001",
        message_control_id="DEMOLEDGER0001",
        specimen_no="DEMO-SPEC-0001",
        item_code=args.item,
        item_name=row.item_name,
        value=Decimal(args.value),
        unit=args.unit,
        observed_at=pushed_at,
        dept_code=args.dept,
        source_channel="demo",
    )
    judgement = judge_event(event, table.rows)
    clause = read_sample_clause(Path(args.policy))
    card = build_card(event, judgement, policy_clause=clause, duty_person=args.duty)

    due_minutes = 10
    print(f"[材料] 阈值表 {table.source_name} · 第 {row.citation.line_no} 行 · 版本 {row.version}")
    print(f"[判定] {judgement.decision} · {judgement.reason[:60]}")

    with LedgerStore(db_path) as store:
        # 1. 接收 → 判定 → 出卡
        store.record_event(event)
        store.record_judgement(event.event_id, judgement)
        store.record_card(card)
        print(f"[台账] 状态 {store.get_status(event.event_id)}（判定 + 出卡各写一条审计）")

        # 2. 跳步演示：card_built 直接到 acknowledged 会被状态机拒掉
        #    （绕过「已推送」与「人工确认」两步，等于没让人确认就当已处置）
        try:
            store.set_status(event.event_id, "acknowledged")
        except Exception as exc:
            print(f"[状态机] 跳步被拒：{exc}")

        # 3. 推送 → 待回执
        store.mark_notified(
            event.event_id,
            notify_target=args.duty,
            due_minutes=due_minutes,
            channel="wecom",
            at=pushed_at,
        )
        store.mark_pending_ack(event.event_id, at=pushed_at)
        print(f"[推送] 已推送，状态 {store.get_status(event.event_id)}，上报时限 {due_minutes} 分钟")

        # 4. 到期复查：先看没到期时不动，到期后重推一次
        planner = RetryPlanner(store)
        due_later = pushed_at + timedelta(minutes=due_minutes + 1)
        print(f"[复查] 未到期时待推事件数 {len(planner.due_events(pushed_at + timedelta(minutes=5)))}")

        pushed: list[str] = []

        def push(row_data):
            pushed.append(str(row_data.get("event_id", "")))
            print(f"[复查] 到期重推 → {row_data.get('event_id')}（第 1 次重推）")
            return True

        report = RetryRunner(
            store,
            push,
            max_retries=3,
            clock=lambda: due_later,
        ).run()
        print(
            f"[复查] 重推 {report.summary['repushed']} 次 · 失败 {report.summary['push_failed']} 次"
            f" · 耗尽 {report.summary['exhausted']} 次"
        )
        stats = store.notify_stats()
        print(
            f"[计数] 去重通知事件 {stats['notified_events']} 条 · 真实尝试 {stats['attempts']} 次"
            f"（重推 {stats['retries']} 次）—— 重推不把「今天推送多少条」灌大"
        )

        # 5. 人工确认 → 回写登记系统 → 归档
        store.mark_acknowledged(event.event_id, actor=args.duty, at=due_later)
        print(f"[确认] 状态 {store.get_status(event.event_id)}（人工确认点）")
        store.mark_registered(event.event_id, at=due_later)
        print(f"[回写] 状态 {store.get_status(event.event_id)}（已生效事实）")

        # 6. 四问 + 审计流水
        answers = AuditLog(store).trace(event.event_id)
        print("──────────────────────────────")
        print(f"按哪条依据 : {answers.basis}")
        print(f"通知了谁   : {answers.notified_who}")
        print(f"几分钟内   : {answers.due_minutes}")
        print(f"谁确认的   : {answers.acknowledged_by}（{answers.acknowledged_at}）")
        print("──────────────────────────────")
        print("审计流水：")
        for entry in store.audit_entries(event.event_id):
            print(f"  · {entry['at']} {entry['action']:<22} {entry['detail']}")

    print()
    print(
        f"[复核] python3 -m critval.cli ledger-trace --event {event.event_id} --db {db_path}"
    )
    print(f"[复核] python3 -m critval.cli audit-stats --db {db_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
