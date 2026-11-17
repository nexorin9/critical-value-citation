"""三条真实出站通道的本机演示（任务 id 11）。

**这是什么**：一条命令把「取当班人 → 推送处置卡 → 值班人确认 → 回写登记
系统」整条链跑一遍，每一步都走**真实 HTTP 协议**（真实 URL、真实
``Authorization: Bearer`` 头、真实 JSON 体）。

**对端是谁**：本机临时起的**协议替身**（在 ``tests/fixtures/`` 下）——
排班系统、企微群机器人、危急值登记系统各一个。替身替代的只是**对端系统
本身**，不是协议：产品代码走的就是院内真实接口的报文格式。

**院内上线时**：把三个环境变量指向真实地址即可，产品代码一行不改：

* ``NURSE_ROSTER_BASE_URL`` / ``ROSTER_TOKEN``
* ``CRITVAL_WECOM_WEBHOOK_URL``
* ``CRITVAL_BASE_URL`` / ``CRITVAL_TOKEN``

跑法::

    python3 scripts/outbound_demo.py
    python3 scripts/outbound_demo.py --degrade-roster   # 排班系统挂掉，看降级
    python3 scripts/outbound_demo.py --registry-down    # 登记系统 500，看补写
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "tests"))

from critval.card.builder import build_card  # noqa: E402
from critval.card.provenance import ORIGIN_LIVE, describe_provenance  # noqa: E402
from critval.judge.critical import judge_event  # noqa: E402
from critval.kb.loader import load_threshold_table  # noqa: E402
from critval.ledger.store import LedgerStore  # noqa: E402
from critval.models import (  # noqa: E402
    CardProvenance,
    Citation,
    CriticalEvent,
    PolicyClause,
)
from critval.notify.wecom import WeComNotifier  # noqa: E402
from critval.roster.cache import RosterCache  # noqa: E402
from critval.roster.client import RosterClient  # noqa: E402
from critval.roster.resolve import target_provenance  # noqa: E402
from critval.writeback.client import RegistrationSystemClient  # noqa: E402
from critval.writeback.registry import RegistryWriteback  # noqa: E402
from fixtures.registry_double import RegistryDouble  # noqa: E402
from fixtures.roster_double import RosterDouble  # noqa: E402
from fixtures.wecom_double import WeComDouble  # noqa: E402

THRESHOLD = PROJECT_ROOT / "samples" / "threshold" / "示例危急值阈值表.xlsx"
DEPT = "ICU"
CONFIRM_URL = "http://127.0.0.1:8848/events/evt-demo-0001/ack"
#: 演示用的固定时刻 —— 择人（班次窗口判定）按这个时刻算，
#: 免得演示结果随运行时刻漂移。
AT = datetime(2026, 1, 2, 8, 30)

POLICY_CITATION = Citation(
    source_name="示例危急值报告制度.docx",
    source_sha256="ef5be6701ca68a67" * 4,
    kind="policy_clause",
    paragraph_no=19,
    section_path=("第三章 报告流程与通知时限",),
    line_no=19,
    file_version="示例 v1.0",
)


def _clause() -> PolicyClause:
    return PolicyClause(
        section_title="第三章 报告流程与通知时限",
        body="第七条 检验科发现危急值后应当班复核并通知临床科室……",
        notify_target="检验科值班技师与检验科组长",
        deadline_text="10 分钟内",
        deadline_minutes=10,
        record_destination="危急值登记系统，并同步记入检验科危急值台账",
        citation=POLICY_CITATION,
        clause_no="第七条",
    )


def _event() -> CriticalEvent:
    return CriticalEvent(
        event_id="evt-demo-0001",
        message_control_id="MSGDEMO0001",
        specimen_no="SPEC-DEMO-0001",
        item_code="718-7",
        item_name="血红蛋白",
        value=Decimal("25"),
        unit="g/L",
        observed_at=datetime(2026, 1, 2, 8, 30),
        dept_code=DEPT,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="三条真实出站通道的本机演示")
    parser.add_argument(
        "--degrade-roster",
        action="store_true",
        help="排班系统 503：看缓存降级与来源标注",
    )
    parser.add_argument(
        "--registry-down",
        action="store_true",
        help="登记系统 500：看回写失败落 writeback_failed 与补写",
    )
    parser.add_argument(
        "--db",
        default=":memory:",
        help="台账库路径（默认内存库）",
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

        # ① 排班系统读当班人
        cache = RosterCache(Path("data/roster_cache.json"))
        client = RosterClient(
            roster_double.base_url,
            roster_double.token,
            cache=cache,
        )
        roster = client.fetch(DEPT)
        if args.degrade_roster:
            # 先成功一次留缓存，再让对端挂掉 → 走缓存降级。
            roster_double.status_code = 503
            roster = client.fetch(DEPT)
        # 择人：班次窗口判定 + 角色优先级唯一化 + 值班长兜底（任务 id 41）。
        target = roster.resolve(at=AT)
        print(
            f"[① 排班系统] 取当班人：{roster.origin} · 名单 {len(roster.people)} 人"
            + (f" · 缓存时刻 {roster.as_of}" if roster.as_of else "")
        )
        print(
            f"[① 择人] 通知对象：{target.label() or '（无 —— 转人工）'}"
            f" · 择人方式 {target.selection} · 原因码 {target.reason}"
        )

        # ② 装配处置卡
        table = load_threshold_table(THRESHOLD)
        event = _event()
        judgement = judge_event(event, table.rows)
        card = build_card(
            event,
            judgement,
            policy_clause=_clause(),
            duty_person=target.label() or None,
            provenance=CardProvenance(duty_roster=target_provenance(target)),
        )
        store.record_event(event)
        store.record_judgement(event.event_id, judgement)
        store.set_status(event.event_id, "card_built")
        print(
            f"[② 出卡] {card.card_id} · 判定 {judgement.decision} · "
            f"缺项 {list(card.missing_elements()) or '无'} · "
            f"降级 {list(card.degraded_elements()) or '无'}"
        )
        if roster.degraded or target.origin != ORIGIN_LIVE:
            print(
                f"[② 出卡] 卡片来源标注：{describe_provenance(target_provenance(target))}"
            )

        # ③ 企微推送
        notifier = WeComNotifier(wecom_double.webhook_url, sleep=lambda _s: None)
        pushed = notifier.send_card(card, confirm_url=CONFIRM_URL)
        store.mark_notified(
            event.event_id,
            notify_target=target.label(),
            due_minutes=10,
            channel="wecom",
            card_id=card.card_id,
        )
        store.mark_pending_ack(event.event_id)
        print(
            f"[③ 企微推送] {'成功' if pushed.ok else '失败'} · 尝试 {pushed.attempts} 次 · "
            f"errcode {pushed.errcode} · 正文 {len(wecom_double.last_content)} 字"
        )

        # ④ 未确认 → 零回写
        writeback = RegistryWriteback(
            RegistrationSystemClient(registry_double.base_url, registry_double.token),
            store,
        )
        skipped = writeback.write_back(event.event_id)
        print(
            f"[④ 未确认] 回写跳过={skipped.skipped} · 理由 {skipped.reason} · "
            f"登记系统收到的 POST 次数 {len(registry_double.calls)}"
        )

        # ⑤ 值班人确认 → 回写
        store.mark_acknowledged(event.event_id, actor="检验科值班技师")
        if args.registry_down:
            registry_double.post_status_code = 500
        outcome = writeback.write_back(event.event_id)
        print(
            f"[⑤ 确认回写] {'成功' if outcome.ok else '失败'} · 台账状态 {outcome.status} · "
            f"登记系统收到的 POST 次数 {len(registry_double.calls)}"
        )
        if registry_double.posted:
            print("[⑤ 确认回写] 回写体字段：")
            for key, value in registry_double.posted[-1].items():
                print(f"      {key} = {value}")

        # ⑥ 补写（登记系统恢复后）
        if not outcome.ok:
            registry_double.post_status_code = 0
            replay = writeback.replay(event.event_id)
            print(
                f"[⑥ 补写] {'成功' if replay.ok else '失败'} · 台账状态 {replay.status} · "
                f"登记系统收到的 POST 次数 {len(registry_double.calls)}"
            )
        return 0
    finally:
        store.close()
        roster_double.stop()
        wecom_double.stop()
        registry_double.stop()


if __name__ == "__main__":
    raise SystemExit(main())
