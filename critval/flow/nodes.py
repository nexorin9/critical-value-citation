"""内置处置链节点：把六个业务环节接成一条真跑得起来的链。

**这些节点是薄适配层** —— 判定、出卡、推送、回写的业务逻辑各自已经在
``judge/`` / ``card/`` / ``notify/`` / ``writeback/`` 里实现过一遍，
本模块**不重写**它们，只做三件事：

1. 把节点的产出写进共享上下文（``ctx["judgement"]`` / ``ctx["card"]`` …），
   让后面的节点读得到；
2. 把「这一步做完了」翻译成**业务状态跃迁**（``judged`` / ``card_built`` /
   ``notified`` / ``acknowledged`` / ``registered``）；
3. 在**不该往下走**的地方叫停（不危急、判定不成立要转人工）。

**为什么要状态跃迁放在节点里**：状态机（``critval/ledger/states.py``）守的
是「已生效事实」的边界 —— 从 ``notified`` 走到 ``acknowledged`` 只有一个
入口：**值班人点确认**。如果状态跃迁散落在调用方的 ``if`` 里，这条线就
守不住了；放进节点里，每种跃迁都有且只有一个发生点。

**节点的四种收场**：

======================  ====================================================
收场                    含义
======================  ====================================================
正常返回                这一步做完了，继续往下走
``FlowStop``            **正常止步**（不危急 / 判定不成立转人工）—— 不是错误
抛异常                  **异常** —— 由执行器回落「转人工」出口并写审计
``stop=True`` 的返回     等价于 ``FlowStop``（给不想抛异常的节点用）
======================  ====================================================
"""

from __future__ import annotations

import logging
from typing import Any, MutableMapping, Optional

from critval.flow.contract import (
    WHERE_CONTEXT,
    WHERE_EITHER,
    FieldSpec,
    NodeContract,
)
from critval.flow.dsl import DEFAULT_FALLBACK_COMPONENT, FlowDSL, NodeParam, NodeRegistry
from critval.flow.pipeline import FlowStop
from critval.models import CriticalEvent

__all__ = [
    "AckNode",
    "CardNode",
    "FlowNode",
    "JudgeNode",
    "NotifyNode",
    "RetrieveNode",
    "StageNode",
    "TransferToHumanNode",
    "WritebackNode",
    "build_default_registry",
    "standard_chain",
]

logger = logging.getLogger("critval.flow.nodes")


class FlowNode:
    """处置链节点基类。

    子类实现 ``run(context)``；``context`` 整份共享，节点可读可写。

    子类还可以声明 ``contract()`` —— 这个节点对外承诺的**输入形状 /
    输出形状 / 可重试性**（见 ``critval/flow/contract.py``）。装载时
    契约挂到节点参数上，``param.check()`` 与产出校验都按它执行。
    """

    #: 组件名（注册表里的键）。
    component_name = ""

    def __init__(self, name: str, param: NodeParam) -> None:
        self.name = name
        self.param = param
        #: 执行器把异常交给兜底出口时会调 ``set_exception``。
        self.exception: Optional[BaseException] = None

    def set_exception(self, exc: BaseException) -> None:
        """接收上游节点抛出的异常（兜底出口用）。"""
        self.exception = exc

    def contract(self) -> NodeContract:
        """本节点的契约 —— 默认没有契约（自定义节点可不声明）。"""
        return NodeContract(name=self.component_name)

    def run(self, context: MutableMapping[str, Any]) -> Any:  # pragma: no cover
        raise NotImplementedError


class StageNode(FlowNode):
    """阶段占位节点 —— 标准链的两端用它。

    * ``received`` —— 事件刚收下：**把事件收进台账**（幂等键挡重复）；
    * ``archived`` —— 事实定格归档：状态 → ``archived``。

    中间这两个环节没有额外动作，但**要在图上占位** —— 否则「这条事件
    走到哪一步」在链上看不出来。
    """

    def __init__(self, name: str, param: NodeParam, stage: str = "") -> None:
        super().__init__(name, param)
        self.stage = stage or name

    def contract(self) -> NodeContract:
        return NodeContract(
            name=self.stage,
            inputs=(
                FieldSpec(
                    "event",
                    WHERE_CONTEXT,
                    types=(CriticalEvent,),
                    note="待处置的事件",
                ),
            ),
            outputs=(FieldSpec("stage", WHERE_EITHER, types=(str,)),),
            retryable=False,
            description="处置链的阶段占位节点（收到 / 归档）",
        )

    def run(self, context: MutableMapping[str, Any]) -> Any:
        ctx = context  # 共享上下文：节点的产出写回去，后面的节点读得到
        store = ctx.get("store")
        event = ctx.get("event")
        event_id = getattr(event, "event_id", "")
        if self.stage == "received" and store is not None and event is not None:
            store.record_event(event)
        elif self.stage == "archived" and store is not None and event_id:
            store.set_status(
                event_id,
                "archived",
                actor="flow",
                detail="处置链走完，事实定格归档",
            )
        return {"stage": self.stage}


# ---------------------------------------------------------------------------
# 判定
# ---------------------------------------------------------------------------
class JudgeNode(FlowNode):
    """判定节点：阈值表 → 危急值结论。

    三种收场：

    * **危急** → 状态 ``judged``，继续往下走出卡；
    * **不危急** → 状态 ``not_critical``，**正常止步**（不出卡、不推送）；
    * **判定不成立**（单位换不了 / 阈值表没收录 / 口径互斥）→ 状态
      ``transferred_to_human``，**正常止步**（转人工，不出卡）。
    """

    component_name = "judge"

    def contract(self) -> NodeContract:
        return NodeContract(
            name=self.component_name,
            inputs=(
                FieldSpec(
                    "event",
                    WHERE_CONTEXT,
                    types=(CriticalEvent,),
                    note="待判定的事件",
                ),
                FieldSpec(
                    "rows",
                    WHERE_EITHER,
                    note="院内阈值行；没有阈值行就没法判定",
                ),
            ),
            outputs=(
                FieldSpec("decision", WHERE_EITHER, types=(str,)),
                FieldSpec("item_code", WHERE_EITHER, types=(str,)),
            ),
            retryable=False,
            description="按院内阈值表判定危急（纯计算，重跑还是同一个答案）",
        )

    def run(self, context: MutableMapping[str, Any]) -> Any:
        from critval.judge.critical import (
            DECISION_CRITICAL,
            DECISION_NOT_CRITICAL,
            DECISION_STATUS,
            judge_event,
            requires_human_review,
        )

        ctx = context  # 共享上下文：节点的产出写回去，后面的节点读得到
        event = ctx.get("event")
        if event is None:
            raise RuntimeError("判定节点拿不到事件")

        rows = self.param.get("rows")
        if rows is None:
            rows = ctx.get("rows") or []
        ruleset = self.param.get("ruleset") or ctx.get("ruleset")
        sex = self.param.get("sex") or ctx.get("sex")
        age = self.param.get("age") or ctx.get("age")

        judgement = judge_event(event, rows, sex, age, ruleset=ruleset)
        ctx["judgement"] = judgement

        store = ctx.get("store")
        if store is not None:
            store.record_judgement(event.event_id, judgement)
            store.set_status(
                event.event_id,
                DECISION_STATUS.get(judgement.decision, "judged"),
                actor="judge",
                detail=judgement.reason,
            )

        if judgement.decision == DECISION_CRITICAL:
            return {
                "decision": judgement.decision,
                "item_code": judgement.item_code,
                "rule_id": judgement.rule_id,
            }

        if judgement.decision == DECISION_NOT_CRITICAL:
            raise FlowStop("判定结论为不危急，只入台账，不出卡不推送")

        if requires_human_review(judgement.decision):
            reason = f"判定不成立（{judgement.decision}）：{judgement.reason}"
            if store is not None:
                store.append_audit(
                    "flow_transferred_to_human",
                    event_id=event.event_id,
                    actor="judge",
                    dept_code=event.dept_code,
                    specimen_no=event.specimen_no,
                    item_code=event.item_code,
                    message_control_id=event.message_control_id,
                    detail=reason,
                )
            raise FlowStop(reason)

        return {"decision": judgement.decision}


# ---------------------------------------------------------------------------
# 召回
# ---------------------------------------------------------------------------
class RetrieveNode(FlowNode):
    """召回节点：把院内危急值制度条款召回出来（带出处）。

    条款来源走**注入的 ``clause_provider``**（与入站处理器同一道缝）——
    召回引擎（``critval/retrieve/``）已经实现了带出处的混合召回，
    这里只负责把它接到链上，并把召回到的出处挂到上下文里。

    **召回不到不编造**：条款为 ``None`` 时照常往下走，但处置卡上
    「制度依据」这一项会标成缺失（由 ``card/builder.py`` 负责），
    值班人至少能看到「依据没查到，需要人工找制度」。
    """

    component_name = "retrieve"

    def contract(self) -> NodeContract:
        return NodeContract(
            name=self.component_name,
            inputs=(
                FieldSpec(
                    "event",
                    WHERE_CONTEXT,
                    types=(CriticalEvent,),
                    note="要召回依据的事件",
                ),
            ),
            outputs=(
                FieldSpec("clause_found", WHERE_EITHER, types=(bool,)),
                FieldSpec("citations", WHERE_EITHER, types=(int,)),
            ),
            retryable=False,
            description="召回院内危急值制度条款（召回不到不编造，只标缺失）",
        )

    def run(self, context: MutableMapping[str, Any]) -> Any:
        ctx = context  # 共享上下文：节点的产出写回去，后面的节点读得到
        event = ctx.get("event")
        provider = self.param.get("clause_provider") or ctx.get("clause_provider")
        clause = None
        if callable(provider):
            clause = provider(event)
        elif provider is not None:
            clause = provider
        ctx["policy_clause"] = clause

        citations = []
        if clause is not None and getattr(clause, "citation", None) is not None:
            citations = [clause.citation]
        retriever = self.param.get("retriever") or ctx.get("retriever")
        if retriever is not None and event is not None:
            try:
                result = retriever.retrieve(
                    query=str(self.param.get("query") or ""),
                    filters={"item_code": event.item_code},
                )
                citations.extend(result.citations())
            except Exception:  # noqa: BLE001 —— 召回失败不该让整条链断掉
                logger.exception("召回制度条款失败，按「未召回」继续")
        ctx["citations"] = citations
        return {
            "clause_found": clause is not None,
            "citations": len(citations),
        }


# ---------------------------------------------------------------------------
# 出卡
# ---------------------------------------------------------------------------
class CardNode(FlowNode):
    """出卡节点：四要素处置卡 + 落库 + 不可变快照。

    出卡前的**版本闸门**由 ``card/builder.py`` 负责（同类材料混版、
    与院内声明的版本绑定不符 → 拒绝出卡）。这里把异常往上抛，
    由执行器回落「转人工」—— 依据口径自相矛盾的卡，比没有卡更危险。
    """

    component_name = "card"

    def contract(self) -> NodeContract:
        return NodeContract(
            name=self.component_name,
            inputs=(
                FieldSpec(
                    "event",
                    WHERE_CONTEXT,
                    types=(CriticalEvent,),
                    note="要出卡的事件",
                ),
                FieldSpec(
                    "judgement",
                    WHERE_CONTEXT,
                    note="判定结论；没有结论出不了卡",
                ),
            ),
            outputs=(
                FieldSpec("card_id", WHERE_EITHER, types=(str,)),
                FieldSpec("missing_elements", WHERE_EITHER, types=(list, tuple)),
            ),
            retryable=False,
            description="装配四要素处置卡（缺项标注、不编造）",
        )

    def run(self, context: MutableMapping[str, Any]) -> Any:
        from critval.card.builder import build_card

        ctx = context  # 共享上下文：节点的产出写回去，后面的节点读得到
        event = ctx.get("event")
        judgement = ctx.get("judgement")
        if event is None or judgement is None:
            raise RuntimeError("出卡节点拿不到事件或判定结论")

        card = build_card(
            event,
            judgement,
            policy_clause=ctx.get("policy_clause"),
            duty_person=ctx.get("duty_person"),
            confirm_url=self.param.get("confirm_url") or ctx.get("confirm_url"),
        )
        ctx["card"] = card

        store = ctx.get("store")
        if store is not None:
            store.record_card(card)
            store.set_status(
                event.event_id,
                "card_built",
                actor="card",
                detail=f"处置卡 {card.card_id} 已装配",
            )
        return {
            "card_id": card.card_id,
            "missing_elements": list(card.missing_elements()),
        }


# ---------------------------------------------------------------------------
# 推送
# ---------------------------------------------------------------------------
class NotifyNode(FlowNode):
    """推送节点：把处置卡推进值班群。

    推送失败**不算成功** —— 节点抛异常，由执行器回落转人工，事件留在
    台账里等人处理。绝不假装推出去了。
    """

    component_name = "notify"

    def contract(self) -> NodeContract:
        return NodeContract(
            name=self.component_name,
            inputs=(
                FieldSpec(
                    "card",
                    WHERE_CONTEXT,
                    note="要推送的处置卡",
                ),
                FieldSpec(
                    "notifier",
                    WHERE_EITHER,
                    note="通知出口（企业微信群机器人）",
                ),
            ),
            outputs=(
                FieldSpec("channel", WHERE_EITHER, types=(str,)),
                FieldSpec("attempts", WHERE_EITHER, types=(int,)),
            ),
            # 推送失败可以重试（通道自带退避重试），但重复推送要靠幂等抑制。
            retryable=True,
            description="把处置卡推进值班群（推不出去就算失败，不假装成功）",
        )

    def run(self, context: MutableMapping[str, Any]) -> Any:
        ctx = context  # 共享上下文：节点的产出写回去，后面的节点读得到
        card = ctx.get("card")
        event = ctx.get("event")
        if card is None:
            raise RuntimeError("推送节点拿不到处置卡")

        notifier = self.param.get("notifier") or ctx.get("notifier")
        if notifier is None:
            raise RuntimeError("推送节点没有拿到通知出口")

        outcome = notifier.send_card(
            card,
            confirm_url=str(self.param.get("confirm_url") or ctx.get("confirm_url") or ""),
            event_id=getattr(event, "event_id", ""),
            notify_target=str(ctx.get("notify_target") or ""),
            due_minutes=ctx.get("due_minutes"),
        )
        if not getattr(outcome, "ok", False):
            reason = getattr(outcome, "error", "") or "推送未成功"
            raise RuntimeError(f"处置卡推送失败：{reason}")

        store = ctx.get("store")
        if store is not None:
            store.mark_notified(
                getattr(event, "event_id", ""),
                notify_target=str(ctx.get("notify_target") or ""),
                due_minutes=ctx.get("due_minutes"),
                channel=getattr(outcome, "channel", ""),
                card_id=card.card_id,
                actor="notify",
            )
        return {
            "channel": getattr(outcome, "channel", ""),
            "attempts": getattr(outcome, "attempts", 0),
        }


# ---------------------------------------------------------------------------
# 确认
# ---------------------------------------------------------------------------
class AckNode(FlowNode):
    """确认节点：**人工确认点**。

    这是整条链上唯一能把事件从「建议」推进到「已生效事实」之前的闸门。
    节点本身不做判定 —— 它只承认「值班人已经点了确认」这件事，
    而且**只认值班人给的确认**：上下文里没有 ``acknowledged=True``
    就**不放行**，宁可停在 ``notified`` 等人回来点。
    """

    component_name = "ack"

    def contract(self) -> NodeContract:
        return NodeContract(
            name=self.component_name,
            inputs=(
                FieldSpec(
                    "event",
                    WHERE_CONTEXT,
                    types=(CriticalEvent,),
                    note="被确认的事件",
                ),
            ),
            outputs=(FieldSpec("acknowledged", WHERE_EITHER, types=(bool,)),),
            # 人工确认不可重试 —— 没点确认就是没点，重跑不会替人点。
            retryable=False,
            description="人工确认点（唯一能把建议变成事实的闸门）",
        )

    def run(self, context: MutableMapping[str, Any]) -> Any:
        ctx = context  # 共享上下文：节点的产出写回去，后面的节点读得到
        event = ctx.get("event")
        acknowledged = bool(ctx.get("acknowledged"))
        handler = str(ctx.get("acknowledged_by") or ctx.get("handler") or "")

        if not acknowledged:
            raise FlowStop("还没人确认，事件停在已推送待回执")

        store = ctx.get("store")
        if store is not None:
            store.mark_acknowledged(
                getattr(event, "event_id", ""),
                actor=handler or "duty",
                detail="值班人已确认处置",
            )
        return {"acknowledged": True, "acknowledged_by": handler}


# ---------------------------------------------------------------------------
# 回写
# ---------------------------------------------------------------------------
class WritebackNode(FlowNode):
    """回写节点：把**已确认**的回执写回院内危急值登记系统。

    「已生效事实」只从这里产生。未确认的事件回写器会自己挡下
    （一次出站调用都不发），这里再兜一层：没确认就**停**，不往下走。
    """

    component_name = "writeback"

    def contract(self) -> NodeContract:
        return NodeContract(
            name=self.component_name,
            inputs=(
                FieldSpec(
                    "registry",
                    WHERE_EITHER,
                    note="危急值登记系统回写器",
                ),
            ),
            outputs=(
                FieldSpec("ok", WHERE_EITHER, types=(bool,)),
                FieldSpec("attempts", WHERE_EITHER, types=(int,)),
                FieldSpec("status", WHERE_EITHER, types=(str,)),
            ),
            # 回写是幂等的（按 message_control_id 去重），重跑安全。
            retryable=True,
            description="把已确认的回执幂等回写院内登记系统",
        )

    def run(self, context: MutableMapping[str, Any]) -> Any:
        ctx = context  # 共享上下文：节点的产出写回去，后面的节点读得到
        event = ctx.get("event")
        registry = self.param.get("registry") or ctx.get("writeback_registry")
        if registry is None:
            raise RuntimeError("回写节点没有拿到回写登记系统")

        outcome = registry.write_back(
            getattr(event, "event_id", ""),
            receipt_status=str(self.param.get("receipt_status") or "acknowledged"),
            receipt_at=str(ctx.get("acknowledged_at") or ""),
        )
        if getattr(outcome, "skipped", False):
            reason = getattr(outcome, "reason", "") or "回写被跳过"
            raise RuntimeError(f"回写被跳过：{reason}")
        if not getattr(outcome, "ok", False):
            reason = getattr(outcome, "error", "") or getattr(outcome, "reason", "")
            raise RuntimeError(f"回写失败：{reason}")
        return {
            "ok": True,
            "attempts": getattr(outcome, "attempts", 0),
            "status": getattr(outcome, "status", ""),
        }


# ---------------------------------------------------------------------------
# 转人工（兜底出口）
# ---------------------------------------------------------------------------
class TransferToHumanNode(FlowNode):
    """兜底出口：**转人工**。

    源产品里这一环是「Answer」节点 —— 链上任何环节答不出来，就落到它
    把结论说清楚。本项目把它换成转人工：链上任何环节处理不了，
    事件交给**人**，并说清楚「卡在哪一步、为什么」。

    状态跃迁由**执行器**兜底保证（即使这个节点自身炸了，事件也会被
    置为 ``transferred_to_human`` 并写审计）—— 兜底出口自己再炸，
    也不能把事件丢了。
    """

    component_name = DEFAULT_FALLBACK_COMPONENT

    def contract(self) -> NodeContract:
        return NodeContract(
            name=self.component_name,
            inputs=(
                FieldSpec(
                    "event",
                    WHERE_CONTEXT,
                    types=(CriticalEvent,),
                    note="要转人工的事件（转人工必须知道是哪一条）",
                ),
            ),
            outputs=(
                FieldSpec("transferred", WHERE_EITHER, types=(bool,)),
                FieldSpec("event_id", WHERE_EITHER, types=(str,)),
                FieldSpec("next_action", WHERE_EITHER, types=(str,)),
            ),
            # 转人工不可重试 —— 要人来接，不是机器再跑一遍。
            retryable=False,
            description="兜底出口：转人工（事件不丢、有人接）",
        )

    def run(self, context: MutableMapping[str, Any]) -> Any:
        ctx = context  # 共享上下文：节点的产出写回去，后面的节点读得到
        exc = self.exception or ctx.get("exception")
        event = ctx.get("event")
        summary = {
            "transferred": True,
            "event_id": getattr(event, "event_id", ""),
            "error": f"{type(exc).__name__}: {exc}" if exc else "",
            "next_action": "请值班人接手：核对依据与时限，处置后手工回写登记系统",
        }
        store = ctx.get("store")
        if store is not None and getattr(event, "event_id", ""):
            store.append_audit(
                "flow_transferred_to_human",
                event_id=event.event_id,
                actor="flow",
                dept_code=event.dept_code,
                specimen_no=event.specimen_no,
                item_code=event.item_code,
                message_control_id=event.message_control_id,
                detail=summary["next_action"],
            )
        return summary


# ---------------------------------------------------------------------------
# 注册表与标准链
# ---------------------------------------------------------------------------
def build_default_registry() -> NodeRegistry:
    """注册全部内置节点。"""
    registry = NodeRegistry()
    registry.register(JudgeNode.component_name, JudgeNode)
    registry.register(RetrieveNode.component_name, RetrieveNode)
    registry.register(CardNode.component_name, CardNode)
    registry.register(NotifyNode.component_name, NotifyNode)
    registry.register(AckNode.component_name, AckNode)
    registry.register(WritebackNode.component_name, WritebackNode)
    registry.register(TransferToHumanNode.component_name, TransferToHumanNode)
    for stage in ("received", "archived"):
        registry.register(
            stage,
            lambda name, param, _stage=stage: StageNode(name, param, _stage),
        )
    return registry


def standard_chain() -> FlowDSL:
    """标准处置链 —— 业务状态机主链的图形态。

    ::

        received → judged → card_built → notified → acknowledged → registered → archived

    节点实例名用状态名（一眼能对上状态机），组件名用环节名
    （决定这个节点跑什么）。

    ``transfer_to_human`` **不在主路径上** —— 它是兜底出口，
    执行器在节点异常时倒序沿路径找它，找不到才从链上定义里取。
    """
    dsl = FlowDSL()
    dsl.add("received", "received", {}, downstream=["judged"])
    dsl.add("judged", "judge", {}, downstream=["card_built"])
    dsl.add("card_built", "card", {}, downstream=["notified"])
    dsl.add("notified", "notify", {}, downstream=["acknowledged"])
    dsl.add("acknowledged", "ack", {}, downstream=["registered"])
    dsl.add("registered", "writeback", {}, downstream=["archived"])
    dsl.add("archived", "archived", {}, downstream=[])
    dsl.add("transfer_to_human", DEFAULT_FALLBACK_COMPONENT, {}, downstream=[])
    return dsl
