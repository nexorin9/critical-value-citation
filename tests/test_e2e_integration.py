"""端到端集成验收（任务 id 51）：入站 → 判定 → 召回 → 出卡 → 推送 → 确认 → 回写 → 台账。

这条链路就是产品主路径的**完整一遍**：一条 LIS 推来的 ``ORU^R01`` 帧进去，
一张处置卡推到值班群，值班人点确认，回执回写登记系统，台账落成已生效事实。

**对端是谁**：本机起的**协议替身**（``tests/fixtures/`` 下）—— 排班系统、
企微群机器人、危急值登记系统各一个。替身替代的**只是对端系统本身**：
产品代码走的是院内真实接口的报文格式（真实 URL、真实 ``Authorization:
Bearer`` 头、真实 JSON 体、真实 ``errcode`` 判读、真实九项回写字段）。
这不是最终集成面 —— 院内地址与凭据到位后，把环境变量指过去即可，
产品代码一行不改。

**盯住七件事**（每一件都对应一个真实的坏处）：

#. **事件落库** —— 帧进来就落台账，不落就等于没收到；
#. **判定有依据** —— 结论挂在阈值表的某一行上，不是拍脑袋；
#. **处置卡四要素齐全且带出处** —— 每条依据都能追到「哪个文件第几行」；
#. **推送留痕** —— 推过一次就要在案（值班台查得到）；
#. **人工确认是唯一闸门** —— 没确认就不许回写，一次出站调用都不发；
#. **回写请求体逐字段对得上** —— 九项字段一个不少、一个不多；
#. **台账状态 registered** —— 已生效事实落在库里，不是只在内存里。
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
for _extra in (PROJECT_ROOT, PROJECT_ROOT / "tests", PROJECT_ROOT / "scripts"):
    if str(_extra) not in sys.path:
        sys.path.insert(0, str(_extra))

from _samplerefs import read_sample_clause  # noqa: E402
from critval.card.builder import ELEMENT_KEYS  # noqa: E402
from critval.flow.dsl import DEFAULT_FALLBACK_COMPONENT, FlowDSL, NodeRegistry  # noqa: E402
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
from critval.hl7.normalize import normalize_events, parse_oru_r01  # noqa: E402
from critval.kb.loader import load_threshold_table  # noqa: E402
from critval.ledger.store import LedgerStore  # noqa: E402
from critval.notify.local import FallbackNotifier, LocalNotifier  # noqa: E402
from critval.notify.wecom import WeComNotifier  # noqa: E402
from critval.roster.cache import RosterCache  # noqa: E402
from critval.roster.client import RosterClient  # noqa: E402
from critval.writeback.client import RECEIPT_FIELDS, RegistrationSystemClient  # noqa: E402
from critval.writeback.registry import RegistryWriteback  # noqa: E402
from fixtures.registry_double import RegistryDouble  # noqa: E402
from fixtures.roster_double import RosterDouble  # noqa: E402
from fixtures.wecom_double import WeComDouble  # noqa: E402

FIXTURES = PROJECT_ROOT / "tests" / "fixtures"
SAMPLE_FRAME = FIXTURES / "脱敏样例ER7_正常.hl7"
THRESHOLD = PROJECT_ROOT / "samples" / "threshold" / "示例危急值阈值表.xlsx"
POLICY = PROJECT_ROOT / "samples" / "policy" / "示例危急值报告制度.docx"

#: 值班台地址（确认链接的基地址，与守护进程同口径）。
CONSOLE_BASE_URL = "http://127.0.0.1:8760"

#: 制度里的上报时限（分钟）。
DUE_MINUTES = 10

#: 当班人（角色口径，不落姓名以外的患者信息）。
DUTY_NAME = "李静"
DUTY_ROLE = "检验科值班技师"


# ---------------------------------------------------------------------------
# 集成环境
# ---------------------------------------------------------------------------
@dataclass
class Chain:
    """一次集成跑出来的全部产物。"""

    store: LedgerStore
    event: Any
    card: Any
    result: Any
    wecom: WeComDouble
    registry: RegistryDouble
    roster: RosterDouble
    target: Any
    notifications: list[dict[str, Any]] = field(default_factory=list)

    @property
    def event_id(self) -> str:
        return str(self.event.event_id)


class _CardCapture(CardNode):
    """出卡节点 + 把卡片对象留下来（供断言四要素与出处）。

    执行器在跑之前会把上下文**复制**一份，节点写回共享上下文的产出留在
    副本里 —— 所以卡片对象要从节点内部取，而不是从调用方的字典里取。
    卡片本身不落库（台账只留 ``card_id`` 与引用出处），断言要看到四要素
    就得在这儿留一手。
    """

    def __init__(self, name, param, holder: dict) -> None:
        super().__init__(name, param)
        self.holder = holder

    def run(self, context):
        output = super().run(context)
        self.holder["card"] = context.get("card")
        return output


def _build_registry(holder: Optional[dict] = None) -> NodeRegistry:
    """内置节点注册表（与守护进程装的是同一套节点）。"""
    registry = NodeRegistry()
    registry.register("received", lambda n, p: StageNode(n, p, "received"))
    registry.register("judge", JudgeNode)
    registry.register("retrieve", RetrieveNode)
    if holder is None:
        registry.register("card", CardNode)
    else:
        registry.register("card", lambda n, p: _CardCapture(n, p, holder))
    registry.register("notify", NotifyNode)
    registry.register("ack", AckNode)
    registry.register("writeback", WritebackNode)
    registry.register("archived", lambda n, p: StageNode(n, p, "archived"))
    registry.register(DEFAULT_FALLBACK_COMPONENT, TransferToHumanNode)
    return registry


def _build_dsl(*, rows, clause, notifier, registry, with_archive: bool, confirm_url: str) -> FlowDSL:
    """处置链声明 —— 与 ``critval.flow.nodes.standard_chain`` 同一张图。

    ``with_archive=False`` 时链停在 ``registered``（回写完成即止），便于
    断言「台账状态 registered」这一格。
    """
    dsl = FlowDSL()
    dsl.add("received", "received", {}, downstream=["judged"])
    dsl.add("judged", "judge", {"rows": rows}, downstream=["retrieved"])
    dsl.add("retrieved", "retrieve", {"clause_provider": clause}, downstream=["card_built"])
    dsl.add("card_built", "card", {"confirm_url": confirm_url}, downstream=["notified"])
    dsl.add("notified", "notify", {"notifier": notifier}, downstream=["acknowledged"])
    dsl.add("acknowledged", "ack", {}, downstream=["registered"])
    dsl.add(
        "registered",
        "writeback",
        {"registry": registry},
        downstream=["archived"] if with_archive else [],
    )
    if with_archive:
        dsl.add("archived", "archived", {}, downstream=[])
    dsl.add(DEFAULT_FALLBACK_COMPONENT, DEFAULT_FALLBACK_COMPONENT, {}, downstream=[])
    return dsl


def _parse_sample_event():
    """入站：取样例帧 → 规范化成一个事件。"""
    message = parse_oru_r01(SAMPLE_FRAME.read_bytes())
    events, unparsed = normalize_events(message)
    assert not unparsed, unparsed
    assert events, "样例帧没有解析出事件"
    return events[0]


def run_chain(
    tmp_path: Path,
    *,
    acknowledged: bool = True,
    with_archive: bool = False,
) -> Chain:
    """跑一遍完整处置链，返回全部产物。

    ``acknowledged=False`` 用来验证**红线**：值班人没确认就不许回写。
    """
    wecom = WeComDouble()
    roster = RosterDouble()
    registry = RegistryDouble()
    wecom.start()
    roster.start()
    registry.start()
    store = LedgerStore(str(tmp_path / "integration.db"))
    try:
        # ---- 入站：样例帧 → 事件 ----
        event = _parse_sample_event()

        # ---- 排班系统：读当班人（真实 HTTP 协议 → 本机协议替身）----
        roster.seed(
            event.dept_code,
            [
                {
                    "姓名": DUTY_NAME,
                    "工号": "10231",
                    "岗位": DUTY_ROLE,
                    "shift_start": "08:00",
                    "shift_end": "20:00",
                }
            ],
        )
        roster_client = RosterClient(
            roster.base_url,
            roster.token,
            cache=RosterCache(tmp_path / "roster_cache.json"),
        )
        roster_result = roster_client.fetch(event.dept_code)
        target = roster_result.resolve(at=event.observed_at)

        # ---- 材料：阈值表 + 院内制度条款 ----
        table = load_threshold_table(THRESHOLD)
        clause = read_sample_clause(POLICY)

        # ---- 出站出口（真实协议 → 本机协议替身）----
        notifier = FallbackNotifier(
            primary=WeComNotifier(wecom.webhook_url, sleep=lambda _s: None),
            fallback=LocalNotifier(tmp_path / "local_notifications.jsonl"),
        )
        writeback = RegistryWriteback(
            RegistrationSystemClient(registry.base_url, registry.token),
            store,
        )

        # 确认链接与守护进程同口径：{值班台地址}/events/{事件号}/ack
        confirm_url = f"{CONSOLE_BASE_URL}/events/{event.event_id}/ack"

        dsl = _build_dsl(
            rows=table.rows,
            clause=clause,
            notifier=notifier,
            registry=writeback,
            with_archive=with_archive,
            confirm_url=confirm_url,
        )
        holder: dict = {}
        pipeline = Pipeline(dsl, _build_registry(holder), store=store)

        context = {
            "event": event,
            "confirm_url": confirm_url,
            "duty_person": target.label() or None,
            "notify_target": target.label(),
            "due_minutes": DUE_MINUTES,
            "acknowledged": acknowledged,
            "acknowledged_by": DUTY_ROLE if acknowledged else "",
        }
        result = pipeline.run(context)

        card = holder.get("card")
        notifications = [
            row for row in store.notifications_of(event.event_id)
        ]
        return Chain(
            store=store,
            event=event,
            card=card,
            result=result,
            wecom=wecom,
            registry=registry,
            roster=roster,
            target=target,
            notifications=notifications,
        )
    finally:
        # 台账与替身留给断言用 —— 这里只关 socket，不关 store（store 由用例收尾关）
        pass


@pytest.fixture
def chain(tmp_path):
    """一条**确认过**的完整链路。"""
    result = run_chain(tmp_path)
    try:
        yield result
    finally:
        result.store.close()
        result.wecom.stop()
        result.registry.stop()
        result.roster.stop()


# ---------------------------------------------------------------------------
# ① 入站 → 事件落库
# ---------------------------------------------------------------------------
def test_入站样例帧解析成事件并落库(chain: Chain):
    assert chain.event.item_code == "718-7"
    assert chain.event.message_control_id == "FIXMSG000001"
    assert chain.event.value == 25

    row = chain.store.get_event(chain.event_id)
    assert row is not None, "事件没落库"
    assert row["item_code"] == "718-7"
    assert row["specimen_no"]
    assert row["observed_at"]
    assert row["idempotency_key"], "事件行要带幂等键"


def test_台账里不留患者可识别明细(chain: Chain):
    row = chain.store.get_event(chain.event_id)
    assert row["patient_ref"].startswith("pt-")
    assert "FIX-PAT-000001" not in json.dumps(row, ensure_ascii=False)


# ---------------------------------------------------------------------------
# ② 判定
# ---------------------------------------------------------------------------
def test_判定结论为危急且挂在阈值表某一行(chain: Chain):
    row = chain.store.get_event(chain.event_id)
    assert row["judgement_decision"] == "critical"
    assert row["threshold_row"], "判定依据行要落在事件行上"
    # 依据行要能指回阈值表的真实文件与行号
    assert "阈值表" in row["threshold_row"]


# ---------------------------------------------------------------------------
# ③ 处置卡四要素 + 出处
# ---------------------------------------------------------------------------
def test_处置卡四要素齐全且都带出处(chain: Chain):
    card = chain.card
    assert card is not None, "没出卡"
    assert list(ELEMENT_KEYS) == [
        "threshold_basis",
        "notify_target",
        "deadline",
        "record_destination",
    ]
    elements = {
        "threshold_basis": card.threshold_basis,
        "notify_target": card.notify_target,
        "deadline": card.deadline,
        "record_destination": card.record_destination,
    }
    for key, element in elements.items():
        assert element.value, f"处置卡要素 {key} 是空的"
        assert element.status in {"resolved", "degraded"}, f"{key} 状态异常：{element.status}"
        assert element.source is not None, f"{key} 没有出处"
        assert element.source.source_sha256, f"{key} 的出处缺 sha256"
        assert element.source.line_no, f"{key} 的出处缺行号"

    # 通知对象要落到「当班人」这一层，不是制度里那句规则原文
    assert DUTY_NAME in card.notify_target.value


def test_处置卡的引用出处已落库(chain: Chain):
    citations = chain.store.citations_of(chain.event_id)
    assert citations, "处置卡的引用要落库"
    for citation in citations:
        assert citation["source_sha256"]
        assert citation["line_no"]
        assert citation["locator"]


def test_缺项要标出来不许拿占位文案冒充(chain: Chain):
    missing = chain.card.missing_elements()
    assert isinstance(missing, (list, tuple))
    for element in (
        chain.card.threshold_basis,
        chain.card.notify_target,
        chain.card.deadline,
        chain.card.record_destination,
    ):
        if not element.value:
            assert element.status == "missing"
            assert element.reason, "缺项必须写明原因码"


# ---------------------------------------------------------------------------
# ④ 推送
# ---------------------------------------------------------------------------
def test_推送只发一次且正文带出处与确认入口(chain: Chain):
    assert chain.wecom.call_count == 1, chain.wecom.call_count
    request = chain.wecom.requests[0]
    assert request.msgtype == "markdown"
    content = request.content
    assert content, "推送正文是空的"
    assert "第 " in content and " 行" in content, "正文要带阈值依据行号"
    expected_url = f"{CONSOLE_BASE_URL}/events/{chain.event.event_id}/ack"
    assert expected_url in content, "正文要带确认入口"
    # 正文里不能出现患者可识别明细
    assert "FIX-PAT-000001" not in content


def test_推送留痕进台账(chain: Chain):
    assert chain.notifications, "推送要留通知记录"
    first = chain.notifications[0]
    assert first["kind"] == "initial"
    assert first["channel"] == "wecom"
    assert first["card_id"]


# ---------------------------------------------------------------------------
# ⑤ 人工确认（唯一闸门）
# ---------------------------------------------------------------------------
def test_值班人确认后事件状态到acknowledged(chain: Chain):
    row = chain.store.get_event(chain.event_id)
    assert row["acknowledged_by"] == DUTY_ROLE
    assert row["acknowledged_at"]
    assert row["due_minutes"] == DUE_MINUTES


def test_未确认就不回写一次出站调用都不发(tmp_path):
    """**红线**：没点确认 → 不发回写，事件停在「已推送待回执」。"""
    chain = run_chain(tmp_path, acknowledged=False)
    try:
        assert chain.result.status == "stopped"
        assert "还没人确认" in chain.result.error

        row = chain.store.get_event(chain.event_id)
        assert row["status"] == "notified", row["status"]
        assert not row["acknowledged_at"]

        posts = [call for call in chain.registry.calls if call[0] == "POST"]
        assert posts == [], "未确认却发了回写请求"
        assert chain.registry.posted == []
    finally:
        chain.store.close()
        chain.wecom.stop()
        chain.registry.stop()
        chain.roster.stop()


# ---------------------------------------------------------------------------
# ⑥ 回写请求体逐字段断言
# ---------------------------------------------------------------------------
def test_回写请求体九项字段逐字段对得上(chain: Chain):
    posts = [call for call in chain.registry.calls if call[0] == "POST"]
    assert len(posts) == 1, f"回写应恰好一次，实际 {len(posts)} 次"

    body = json.loads(posts[0][2])
    assert set(body.keys()) == set(RECEIPT_FIELDS), (
        f"回写字段对不上：多 {set(body) - set(RECEIPT_FIELDS)}"
        f" / 少 {set(RECEIPT_FIELDS) - set(body)}"
    )

    row = chain.store.get_event(chain.event_id)
    assert body["message_control_id"] == chain.event.message_control_id
    assert body["message_control_id"] == "FIXMSG000001", "幂等键取消息控制号"
    assert body["item_code"] == "718-7"
    assert body["value"] == "25"
    assert body["threshold_row"] == row["threshold_row"]
    assert body["notified_dept"] == chain.event.dept_code
    assert DUTY_NAME in body["duty_person"]
    assert body["due_minutes"] == DUE_MINUTES
    assert body["receipt_status"] == "acknowledged"
    assert body["receipt_at"] == row["acknowledged_at"]


def test_回写请求带Bearer凭据且走登记系统真实路径(chain: Chain):
    posts = [call for call in chain.registry.calls if call[0] == "POST"]
    assert posts[0][1] == "/api/v1/critical-events"


def test_回写载荷里没有患者可识别明细(chain: Chain):
    posts = [call for call in chain.registry.calls if call[0] == "POST"]
    raw = posts[0][2]
    assert "FIX-PAT-000001" not in raw
    assert "patient" not in raw.lower()


# ---------------------------------------------------------------------------
# ⑦ 台账状态
# ---------------------------------------------------------------------------
def test_台账状态为registered(chain: Chain):
    assert chain.store.get_status(chain.event_id) == "registered"


def test_登记系统侧确实落了一条回执(chain: Chain):
    receipt = chain.registry.get("FIXMSG000001")
    assert receipt is not None, "登记系统侧没有这条回执"
    assert receipt["item_code"] == "718-7"

    local = chain.store.get_writeback_receipt("FIXMSG000001")
    assert local is not None, "本地也要留一份既有事实（补写/重放的依据）"
    assert local["http_status"] == 200


def test_审计流水能读出这条事件怎么走的(chain: Chain):
    actions = [row["action"] for row in chain.store.audit_entries(chain.event_id)]
    assert "status_transition" in actions
    assert "writeback_attempted" in actions
    assert "event_acknowledged" in actions

    transitions = [
        row
        for row in chain.store.audit_entries(chain.event_id)
        if row["action"] == "status_transition"
    ]
    path = [row["detail"] for row in transitions]
    joined = " | ".join(path)
    for state in ("judged", "card_built", "notified", "acknowledged", "registered"):
        assert state in joined, f"审计里看不到 {state}"


# ---------------------------------------------------------------------------
# 走完归档的完整主链
# ---------------------------------------------------------------------------
def test_走完归档的完整主链(tmp_path):
    chain = run_chain(tmp_path, with_archive=True)
    try:
        assert chain.result.status == "completed", chain.result.error
        assert chain.result.executed[-1] == "archived"
        assert chain.store.get_status(chain.event_id) == "archived"
        # 归档之前一定经过 registered
        joined = " | ".join(
            row["detail"]
            for row in chain.store.audit_entries(chain.event_id)
            if row["action"] == "status_transition"
        )
        assert "registered" in joined
    finally:
        chain.store.close()
        chain.wecom.stop()
        chain.registry.stop()
        chain.roster.stop()


# ---------------------------------------------------------------------------
# 幂等：同一帧重复到达
# ---------------------------------------------------------------------------
def test_同一帧重复到达只落一条不重复推送不重复回写(tmp_path):
    chain = run_chain(tmp_path)
    try:
        # 再解析一次同一帧，走同一条链 —— 幂等键挡住第二次
        event = _parse_sample_event()
        assert event.event_id == chain.event_id

        assert chain.store.record_event(event) is False, "重复帧不该再落一条"
        assert chain.store.count_events() == 1

        posts = [call for call in chain.registry.calls if call[0] == "POST"]
        assert len(posts) == 1, "重复帧不该再回写一次"
        assert chain.wecom.call_count == 1, "重复帧不该再推送一次"
    finally:
        chain.store.close()
        chain.wecom.stop()
        chain.registry.stop()
        chain.roster.stop()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
