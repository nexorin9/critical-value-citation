#!/usr/bin/env python3
"""处置链本机演示：一条链走完「收到 → 判定 → 召回 → 出卡 → 推送 → 确认 → 回写」。

**这是什么**：把产品主路径画成的那张图**真的跑一遍**，每一步都能看见：
节点跑没跑、结论是什么、台账状态走到哪、审计留了什么。

**对端是谁**：本机临时起的**协议替身**（在 ``tests/fixtures/`` 下）——
排班系统、企微群机器人、危急值登记系统各一个。替身替代的只是**对端系统
本身**，不是协议：产品代码走的就是院内真实接口的报文格式（真实 URL、
真实 ``Authorization: Bearer`` 头、真实 JSON 体）。

**院内上线时**：把三个环境变量指向真实地址即可，产品代码一行不改：

* ``NURSE_ROSTER_BASE_URL`` / ``ROSTER_TOKEN``
* ``CRITVAL_WECOM_WEBHOOK_URL``
* ``CRITVAL_BASE_URL`` / ``CRITVAL_TOKEN``

跑法::

    python3 scripts/flow_demo.py                    # 正常路径
    python3 scripts/flow_demo.py --break card_built # 节点炸了 → 回落转人工
    python3 scripts/flow_demo.py --break judge      # 判定环节炸了 → 回落转人工
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "tests"))

from critval.flow.dsl import DEFAULT_FALLBACK_COMPONENT, FlowDSL  # noqa: E402
from critval.flow.nodes import (  # noqa: E402
    AckNode,
    CardNode,
    JudgeNode,
    NotifyNode,
    RetrieveNode,
    StageNode,
    TransferToHumanNode,
    WritebackNode,
)
from critval.flow.pipeline import Pipeline  # noqa: E402
from critval.kb.loader import load_threshold_table  # noqa: E402
from critval.ledger.store import LedgerStore  # noqa: E402
from critval.models import CriticalEvent  # noqa: E402
from critval.notify.local import FallbackNotifier  # noqa: E402
from critval.notify.wecom import WeComNotifier  # noqa: E402
from critval.roster.cache import RosterCache  # noqa: E402
from critval.roster.client import RosterClient  # noqa: E402
from critval.writeback.client import RegistrationSystemClient  # noqa: E402
from critval.writeback.registry import RegistryWriteback  # noqa: E402
from fixtures.registry_double import RegistryDouble  # noqa: E402
from fixtures.roster_double import RosterDouble  # noqa: E402
from fixtures.wecom_double import WeComDouble  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _samplerefs import read_sample_clause  # noqa: E402

THRESHOLD = PROJECT_ROOT / "samples" / "threshold" / "示例危急值阈值表.xlsx"
POLICY = PROJECT_ROOT / "samples" / "policy" / "示例危急值报告制度.docx"

DEPT = "ICU"
CONFIRM_URL = "http://127.0.0.1:8848/events/evt-flow-0001/ack"
OBSERVED_AT = datetime(2026, 1, 2, 8, 30, tzinfo=timezone.utc)


def _event() -> CriticalEvent:
    return CriticalEvent(
        event_id="evt-flow-0001",
        message_control_id="MC-FLOW-0001",
        specimen_no="SP-FLOW-0001",
        item_code="718-7",
        item_name="血红蛋白",
        value=Decimal("25"),
        unit="g/L",
        observed_at=OBSERVED_AT,
        dept_code=DEPT,
        patient_ref="pt-000000000001",
    )


class _Boom:
    """把某个节点换成「一跑就炸」的替身（演示异常回落用）。"""

    def __init__(self, inner) -> None:
        self.inner = inner

    def set_exception(self, exc) -> None:
        setter = getattr(self.inner, "set_exception", None)
        if callable(setter):
            setter(exc)

    def run(self, context):
        raise RuntimeError(f"演示注入：节点 {self.inner.name} 处理失败")


def _build_chain(*, rows, clause, notifier, registry, confirm_url) -> FlowDSL:
    dsl = FlowDSL()
    dsl.add("received", "received", {}, downstream=["judged"])
    dsl.add("judged", "judge", {"rows": rows}, downstream=["retrieved"])
    dsl.add("retrieved", "retrieve", {"clause_provider": clause}, downstream=["card_built"])
    dsl.add("card_built", "card", {"confirm_url": confirm_url}, downstream=["notified"])
    dsl.add("notified", "notify", {"notifier": notifier}, downstream=["acknowledged"])
    dsl.add("acknowledged", "ack", {}, downstream=["registered"])
    dsl.add("registered", "writeback", {"registry": registry}, downstream=["archived"])
    dsl.add("archived", "archived", {}, downstream=[])
    dsl.add(DEFAULT_FALLBACK_COMPONENT, DEFAULT_FALLBACK_COMPONENT, {}, downstream=[])
    return dsl


def main() -> int:
    parser = argparse.ArgumentParser(description="处置链本机演示")
    parser.add_argument("--db", default=":memory:", help="台账库路径（默认内存库）")
    parser.add_argument(
        "--break",
        dest="break_node",
        default="",
        help="把这个节点换成「一跑就炸」的替身，看异常回落转人工",
    )
    args = parser.parse_args()

    roster_double = RosterDouble()
    wecom_double = WeComDouble()
    registry_double = RegistryDouble()
    roster_double.start()
    wecom_double.start()
    registry_double.start()
    store = LedgerStore(args.db)
    try:
        roster_double.seed(
            DEPT,
            [
                {
                    "姓名": "李静",
                    "工号": "10231",
                    "岗位": "检验科值班技师",
                    "shift_start": "08:00",
                    "shift_end": "20:00",
                }
            ],
        )

        # ① 排班系统取当班人（真实 HTTP 协议 → 本机协议替身）。
        client = RosterClient(
            roster_double.base_url,
            roster_double.token,
            cache=RosterCache(Path("data/roster_cache.json")),
        )
        roster = client.fetch(DEPT)
        target = roster.resolve(at=OBSERVED_AT)

        # ② 材料：阈值表 + 院内制度条款。
        table = load_threshold_table(THRESHOLD)
        clause = read_sample_clause(POLICY)

        # ③ 通知与回写出口（真实协议 → 本机协议替身）。
        notifier = FallbackNotifier(
            primary=WeComNotifier(wecom_double.webhook_url, sleep=lambda _s: None)
        )
        registry = RegistryWriteback(
            RegistrationSystemClient(registry_double.base_url, registry_double.token),
            store,
        )

        dsl = _build_chain(
            rows=table.rows,
            clause=lambda _event: clause,
            notifier=notifier,
            registry=registry,
            confirm_url=CONFIRM_URL,
        )

        from critval.flow.dsl import NodeRegistry

        registry_nodes = NodeRegistry()
        registry_nodes.register("received", lambda n, p: StageNode(n, p, "received"))
        registry_nodes.register("judge", JudgeNode)
        registry_nodes.register("retrieve", RetrieveNode)
        registry_nodes.register("card", CardNode)
        registry_nodes.register("notify", NotifyNode)
        registry_nodes.register("ack", AckNode)
        registry_nodes.register("writeback", WritebackNode)
        registry_nodes.register("archived", lambda n, p: StageNode(n, p, "archived"))
        registry_nodes.register(DEFAULT_FALLBACK_COMPONENT, TransferToHumanNode)

        pipeline = Pipeline(dsl, registry_nodes, store=store)
        if args.break_node:
            target_comp = dsl.components.get(args.break_node)
            if target_comp is None:
                print(f"处置链里没有节点 {args.break_node}", file=sys.stderr)
                return 2
            target_comp.obj = _Boom(target_comp.obj)

        event = _event()
        context = {
            "event": event,
            "duty_person": target.label() or None,
            "notify_target": target.label(),
            "due_minutes": 10,
            "acknowledged": True,
            "acknowledged_by": "检验科值班技师",
        }

        print("处置链：received → judged → retrieved → card_built → notified"
              " → acknowledged → registered → archived")
        print(f"当班人：{target.label() or '（无）'} · 择人方式 {target.selection}")
        print("-" * 68)

        result = pipeline.run(context)

        for index, step in enumerate(result.path, start=1):
            print(f"第 {index} 步：{' → '.join(step)}")
        print("-" * 68)
        print(f"链结论：{result.status} · 跑过 {len(result.executed)} 个节点"
              f" · 失败节点 {result.failed_node or '（无）'}")
        if result.error:
            print(f"原因：{result.error}")
        print(f"台账状态：{store.get_status(event.event_id)}")
        print(f"登记系统收到的 POST 次数：{len(registry_double.calls)}")
        print("审计流水：")
        for row in store.audit_entries(event.event_id):
            print(f"  · {row['action']} — {row['detail']}")
        return 0
    finally:
        store.close()
        roster_double.stop()
        wecom_double.stop()
        registry_double.stop()


if __name__ == "__main__":
    raise SystemExit(main())
