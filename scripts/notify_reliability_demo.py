"""通知投递可靠性的本机演示（任务 id 42）。

**这是什么**：一条命令把「退避重试 → 死信 → 重复投递抑制 → 到期重推」四段
可靠性机制跑一遍，每一段都走**真实 HTTP 协议**（真实 POST、真实企微
``errcode`` 判读、真实退避等待）。

**对端是谁**：本机临时起的**协议替身**（在 ``tests/fixtures/`` 下）——
它替代的只是**对端系统本身**，不是协议：产品代码发出去的就是院内真实群
机器人 webhook 的报文格式。

**院内上线时**：把 ``CRITVAL_WECOM_WEBHOOK_URL`` 指向真实值班群，
产品代码一行不改。

跑法::

    python3 scripts/notify_reliability_demo.py
    python3 scripts/notify_reliability_demo.py --json
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "tests"))

from critval.card.builder import build_card  # noqa: E402
from critval.judge.critical import judge_event  # noqa: E402
from critval.kb.loader import load_threshold_table  # noqa: E402
from critval.ledger.store import LedgerStore  # noqa: E402
from critval.models import Citation, CriticalEvent, PolicyClause  # noqa: E402
from critval.notify.local import LocalNotifier  # noqa: E402
from critval.notify.reliability import (  # noqa: E402
    ROUND_KIND_INITIAL,
    ROUND_KIND_RETRY,
    BackoffPolicy,
    ReliableNotifier,
    build_round_key,
)
from critval.notify.wecom import WeComNotifier  # noqa: E402
from fixtures.wecom_double import WeComDouble  # noqa: E402

THRESHOLD = PROJECT_ROOT / "samples" / "threshold" / "示例危急值阈值表.xlsx"
DEPT = "ICU"
CONFIRM_URL = "http://127.0.0.1:8848/events/evt-demo-0001/ack"
NOW = datetime(2026, 1, 2, 8, 30, tzinfo=timezone.utc)

POLICY_CITATION = Citation(
    source_name="示例危急值报告制度.docx",
    source_sha256="ef5be6701ca68a67" * 4,
    kind="policy_clause",
    paragraph_no=19,
    section_path=("第三章 报告流程与通知时限",),
    line_no=19,
    file_version="示例 v1.0",
)


def _event(event_id: str) -> CriticalEvent:
    return CriticalEvent(
        event_id=event_id,
        message_control_id=f"MC-{event_id}",
        specimen_no="SP-0001",
        item_code="718-7",
        item_name="血红蛋白",
        value=Decimal("25"),
        unit="g/L",
        observed_at=NOW,
        dept_code=DEPT,
    )


def _clause() -> PolicyClause:
    return PolicyClause(
        section_title="第三章 报告流程与通知时限",
        body="第七条 检验科发现危急值后应当班复核并通知临床科室……",
        notify_target="检验科值班技师",
        deadline_text="10 分钟内",
        deadline_minutes=10,
        record_destination="危急值登记系统",
        citation=POLICY_CITATION,
    )


def _card(event: CriticalEvent, table) -> object:
    judgement = judge_event(event, table.rows)
    return build_card(event, judgement, policy_clause=_clause(), duty_person="检验科值班技师")


def _card_text(card) -> str:
    from critval.card.renderer import render_markdown

    return render_markdown(card, CONFIRM_URL)


def _prepare(store: LedgerStore, event: CriticalEvent, card) -> None:
    """把事件推到「已出卡」状态（首次推送的合法起点）。"""
    store.record_event(event)
    store.set_status(event.event_id, "judged")
    store.record_card(card)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python3 scripts/notify_reliability_demo.py",
        description="危急值处置依据检索系统 · 通知投递可靠性演示",
    )
    parser.add_argument("--json", action="store_true", help="以 JSON 输出（机器可读）")
    args = parser.parse_args(argv)

    table = load_threshold_table(THRESHOLD)
    summary: dict[str, object] = {}
    lines: list[str] = []

    with WeComDouble() as double:
        notifier = WeComNotifier(double.webhook_url, timeout=3.0)
        policy = BackoffPolicy(
            base_seconds=0.2, factor=2.0, max_seconds=1.0, jitter_ratio=0.0, max_attempts=3
        )
        store = LedgerStore(":memory:")
        local = LocalNotifier(Path("/tmp/critval_demo_local_queue.jsonl"))
        reliable = ReliableNotifier(
            store,
            channel="wecom",
            policy=policy,
            sleep=time.sleep,
            local=local,
        )

        # ---- 场景 1：限流 → 退避重试 → 成功 ---------------------------
        event = _event("evt-demo-0001")
        card = _card(event, table)
        _prepare(store, event, card)
        content = _card_text(card)

        double.errcode = 45009
        double.errmsg = "接口调用超过频率限制"
        # 前 N 次限流、之后放行 —— 走真实的 HTTP 重试
        original_send = notifier.send_once
        state = {"calls": 0, "fail_until": 0}

        def flaky_send(payload):
            state["calls"] += 1
            if state["calls"] <= state["fail_until"]:
                double.errcode = 45009
                double.errmsg = "接口调用超过频率限制"
            else:
                double.errcode = 0
                double.errmsg = "ok"
            return original_send(payload)

        notifier.send_once = flaky_send  # type: ignore[method-assign]
        state["fail_until"] = 2  # 前两次限流，第三次放行
        started = time.monotonic()
        outcome = reliable.notify_event(
            event.event_id,
            push=notifier.push_callable(content, confirm_url=CONFIRM_URL),
            content=content,
            card_id=card.card_id,
            kind=ROUND_KIND_INITIAL,
            round_key=build_round_key(ROUND_KIND_INITIAL, 1),
            notify_target="检验科值班技师",
            due_minutes=10,
        )
        elapsed = time.monotonic() - started
        summary["scenario_1_retry_then_ok"] = outcome.to_dict()
        summary["scenario_1_wall_seconds"] = round(elapsed, 3)
        lines.append("── 场景 1：限流（45009）→ 退避重试 → 第 3 次成功")
        lines.append(
            f"   结论：{'推送成功' if outcome.ok else '推送失败'} · "
            f"尝试 {outcome.attempt_count} 次 · 耗时 {elapsed:.2f} 秒"
        )
        lines.append(f"   事件状态：{store.get_status(event.event_id)}")
        for row in store.delivery_attempts_of(event.event_id):
            lines.append(
                f"   · 第 {row['attempt']} 次 · 状态码 {row['status_code']} · "
                f"退避 {row['waited_ms']} ms · {'成功' if row['ok'] else '失败'}"
            )

        # ---- 场景 2：重复投递抑制（同一轮次） ------------------------
        calls_before = double.call_count
        suppressed = reliable.deliver(
            event.event_id,
            push=notifier.push_callable(content, confirm_url=CONFIRM_URL),
            content=content,
            card_id=card.card_id,
            kind=ROUND_KIND_INITIAL,
            round_key=build_round_key(ROUND_KIND_INITIAL, 1),
        )
        summary["scenario_2_suppressed"] = suppressed.to_dict()
        lines.append("")
        lines.append("── 场景 2：同一轮次重复投递 → 被抑制（不刷屏）")
        lines.append(
            f"   结论：suppressed={suppressed.suppressed} · "
            f"企微收到请求 {calls_before} → {double.call_count} 次（没多发）"
        )

        # ---- 场景 3：到期重推（不同轮次 → 不被误抑制） ---------------
        repush = reliable.deliver(
            event.event_id,
            push=notifier.push_callable(content, confirm_url=CONFIRM_URL),
            content=content,
            card_id=card.card_id,
            kind=ROUND_KIND_RETRY,
            round_key=build_round_key(ROUND_KIND_RETRY, 1),
        )
        summary["scenario_3_repush_not_suppressed"] = repush.to_dict()
        lines.append("")
        lines.append("── 场景 3：到期复查重推（轮次 retry:1）→ 不被首次推送误抑制")
        lines.append(
            f"   结论：suppressed={repush.suppressed} · ok={repush.ok} · "
            f"企微累计收到 {double.call_count} 次"
        )

        # ---- 场景 4：持续失败 → 死信 + 事件 failed -------------------
        event2 = _event("evt-demo-0002")
        card2 = _card(event2, table)
        _prepare(store, event2, card2)
        state["fail_until"] = 10**6  # 一直限流，推不出去
        double.errcode = 45009
        double.errmsg = "接口调用超过频率限制"

        failed = reliable.notify_event(
            event2.event_id,
            push=notifier.push_callable(_card_text(card2), confirm_url=CONFIRM_URL),
            content=_card_text(card2),
            card_id=card2.card_id,
            kind=ROUND_KIND_INITIAL,
            round_key=build_round_key(ROUND_KIND_INITIAL, 1),
            notify_target="检验科值班技师",
            due_minutes=10,
        )
        summary["scenario_4_dead_letter"] = failed.to_dict()
        letters = store.dead_letters_of(event2.event_id)
        summary["scenario_4_dead_letters"] = letters
        lines.append("")
        lines.append("── 场景 4：持续限流 → 重试耗尽 → 死信（事件不丢）")
        lines.append(
            f"   结论：尝试 {failed.attempt_count} 次 · dead_lettered={failed.dead_lettered} · "
            f"事件状态 {store.get_status(event2.event_id)}"
        )
        lines.append(
            f"   死信 {len(letters)} 条 · 最后错误：{letters[0]['last_error'] if letters else '-'}"
        )
        lines.append(
            f"   事件仍在台账里：{store.get_event(event2.event_id) is not None} · "
            f"本机队列 {len(local.records_of(event2.event_id))} 条"
        )
        for entry in store.audit_entries(event2.event_id):
            if entry["action"] == "notify_dead_lettered":
                lines.append(f"   审计：{entry['detail']}")

        store.close()

    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
