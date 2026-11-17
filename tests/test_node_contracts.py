"""处置链**节点契约**的契约测试矩阵。

对应 task id 44 的「写契约测试矩阵：每个节点至少一个缺必填参数用例 +
一个输出形状不符用例，矩阵驱动（参数化测试）」。

**盯住的是两件事**：

1. **缺必填项 → 链停在那一个节点** —— 该节点自己不跑，后面的节点也不跑，
   报错信息里**带着节点名与字段名**（值班的要知道是哪个环节缺了哪样东西，
   而不是「处理失败」四个字）；
2. **产出形状不符 → 同样当场停住** —— 节点少交字段、把 ``str`` 写成
   ``int``，都要在**这一环**被拦住，不让半个结果顺着链传下去。

**为什么参数不合法是「硬停」、契约不满足是「转人工」**：参数写错是
**配置错误**，要立刻暴露出来让人改配置；契约不满足是**这一环拿不到该拿的
东西**，机器处理不了 —— 事件落转人工，交给人。两条路都**不带着坏数据
继续往下跑**。

矩阵是**数据驱动**的：新增一个内置节点，往 :data:`CONTRACT_MATRIX` 里加
一行即可，缺参用例与产出形状用例同时补上。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import pytest

from critval.flow.contract import (
    FieldSpec,
    NodeContract,
    NodeContractError,
    NodeOutputError,
)
from critval.flow.dsl import (
    DEFAULT_FALLBACK_COMPONENT,
    FlowDSL,
    NodeParam,
    NodeParamError,
    NodeRegistry,
)
from critval.flow.nodes import TransferToHumanNode, build_default_registry
from critval.flow.pipeline import Pipeline
from critval.ledger.store import LedgerStore
from critval.models import CriticalEvent


# ---------------------------------------------------------------------------
# 测试用的节点
# ---------------------------------------------------------------------------
class NoopNode:
    """什么都不做的节点 —— 只用来证明「后续节点没被执行」。"""

    def __init__(self, name: str, param: NodeParam) -> None:
        self.name = name
        self.param = param

    def run(self, context):
        return {"noop": True}


class SpyNode:
    """记录自己跑没跑过的节点。"""

    def __init__(self, name: str, param: NodeParam, calls: list) -> None:
        self.name = name
        self.param = param
        self.calls = calls

    def run(self, context):
        self.calls.append(self.name)
        return {"spy": True}


def _event(event_id: str = "evt-contract-0001") -> CriticalEvent:
    return CriticalEvent(
        event_id=event_id,
        message_control_id="MC-CONTRACT-0001",
        specimen_no="SP-CONTRACT-0001",
        item_code="718-7",
        item_name="血红蛋白",
        value=Decimal("25"),
        unit="g/L",
        observed_at=datetime(2026, 1, 2, 8, 30, tzinfo=timezone.utc),
        dept_code="LAB",
        patient_ref="pt-000000000001",
    )


#: 判定结论与处置卡在契约里**不约束类型**（真实类型在各自的模块里保证），
#: 这里用占位对象即可 —— 契约测试只关心「字段在不在、类型对不对」。
_JUDGEMENT = object()
_CARD = object()
_NOTIFIER = object()
_REGISTRY = object()

_EVENT = _event()


@dataclass(frozen=True)
class ContractCase:
    """契约矩阵的一行 —— 一个节点的一组契约用例。

    * ``component`` —— 组件名；
    * ``node`` —— 链上的实例名；
    * ``missing_field`` —— **缺必填项**用例里故意不给的那个字段；
    * ``params`` / ``context`` —— 缺该项时其余字段齐备的取值；
    * ``full_params`` / ``full_context`` —— 字段齐备时的取值（产出形状用例用）；
    * ``bad_output`` —— **产出形状不符**用例里节点交出来的畸形结果；
    * ``expected_outputs`` —— 契约声明的产出字段。
    """

    component: str
    node: str
    missing_field: str
    params: dict[str, Any] = field(default_factory=dict)
    context: dict[str, Any] = field(default_factory=dict)
    full_params: dict[str, Any] = field(default_factory=dict)
    full_context: dict[str, Any] = field(default_factory=dict)
    bad_output: dict[str, Any] = field(default_factory=dict)
    expected_outputs: tuple[str, ...] = ()

    @property
    def case_id(self) -> str:
        return f"{self.component}-{self.missing_field}"


#: **契约测试矩阵** —— 每个内置节点一行。
CONTRACT_MATRIX: tuple[ContractCase, ...] = (
    ContractCase(
        component="received",
        node="received",
        missing_field="event",
        full_context={"event": _EVENT},
        expected_outputs=("stage",),
        bad_output={},
    ),
    ContractCase(
        component="judge",
        node="judged",
        missing_field="event",
        params={"rows": []},
        full_params={"rows": []},
        full_context={"event": _EVENT},
        expected_outputs=("decision", "item_code"),
        bad_output={"decision": "critical"},
    ),
    ContractCase(
        component="judge",
        node="judged",
        missing_field="rows",
        context={"event": _EVENT},
        full_params={"rows": []},
        full_context={"event": _EVENT},
        expected_outputs=("decision", "item_code"),
        bad_output={"decision": "critical"},
    ),
    ContractCase(
        component="retrieve",
        node="retrieved",
        missing_field="event",
        full_context={"event": _EVENT},
        expected_outputs=("clause_found", "citations"),
        bad_output={"clause_found": True},
    ),
    ContractCase(
        component="card",
        node="card_built",
        missing_field="judgement",
        context={"event": _EVENT},
        full_context={"event": _EVENT, "judgement": _JUDGEMENT},
        expected_outputs=("card_id", "missing_elements"),
        bad_output={"card_id": "card-0001"},
    ),
    ContractCase(
        component="notify",
        node="notified",
        missing_field="notifier",
        context={"card": _CARD, "event": _EVENT},
        full_params={"notifier": _NOTIFIER},
        full_context={"card": _CARD, "event": _EVENT},
        expected_outputs=("channel", "attempts"),
        bad_output={"channel": "wecom"},
    ),
    ContractCase(
        component="ack",
        node="acknowledged",
        missing_field="event",
        full_context={"event": _EVENT},
        expected_outputs=("acknowledged",),
        bad_output={},
    ),
    ContractCase(
        component="writeback",
        node="registered",
        missing_field="registry",
        context={"event": _EVENT},
        full_params={"registry": _REGISTRY},
        full_context={"event": _EVENT},
        expected_outputs=("ok", "attempts", "status"),
        bad_output={"ok": True},
    ),
    ContractCase(
        component=DEFAULT_FALLBACK_COMPONENT,
        node=DEFAULT_FALLBACK_COMPONENT,
        missing_field="event",
        full_context={"event": _EVENT},
        expected_outputs=("transferred", "event_id", "next_action"),
        bad_output={},
    ),
)

#: 内置节点清单（矩阵覆盖到的组件）。
BUILTIN_COMPONENTS: tuple[str, ...] = tuple(
    dict.fromkeys(case.component for case in CONTRACT_MATRIX)
)


@pytest.fixture()
def store() -> LedgerStore:
    instance = LedgerStore(":memory:")
    try:
        yield instance
    finally:
        instance.close()


def _registry_with_noop() -> NodeRegistry:
    """默认注册表 + 一个什么也不干的节点（用来证明后续节点没跑）。"""
    registry = build_default_registry()
    registry.register("noop", NoopNode)
    return registry


def _contract_of(component: str) -> NodeContract:
    """取内置节点声明的契约。"""
    node = build_default_registry().create(component, "probe", NodeParam())
    return node.contract()


def _chain_for(case: ContractCase, params: dict[str, Any]) -> FlowDSL:
    """按矩阵行搭一条链：被测节点 → 后续节点（+ 兜底出口）。"""
    dsl = FlowDSL()
    dsl.add(case.node, case.component, params, downstream=["after"])
    dsl.add("after", "noop", {})
    if case.component != DEFAULT_FALLBACK_COMPONENT:
        dsl.add(DEFAULT_FALLBACK_COMPONENT, DEFAULT_FALLBACK_COMPONENT, {})
    return dsl


def _bad_output_registry(case: ContractCase) -> NodeRegistry:
    """注册表：被测组件换成「交出畸形产出」的节点，契约照旧。"""
    contract = _contract_of(case.component)

    class BadOutputNode:
        def __init__(self, name: str, param: NodeParam) -> None:
            self.name = name
            self.param = param

        def contract(self) -> NodeContract:
            return contract

        def run(self, context):
            return dict(case.bad_output)

    registry = NodeRegistry()
    registry.register(case.component, BadOutputNode)
    registry.register("noop", NoopNode)
    if case.component != DEFAULT_FALLBACK_COMPONENT:
        registry.register(DEFAULT_FALLBACK_COMPONENT, TransferToHumanNode)
    return registry


# ---------------------------------------------------------------------------
# 1. 每个内置节点都带契约
# ---------------------------------------------------------------------------
class TestContractRegistration:
    @pytest.mark.parametrize("component", BUILTIN_COMPONENTS)
    def test_内置节点都声明了契约(self, component):
        contract = _contract_of(component)

        assert contract.name == component
        assert contract.inputs, f"{component} 没有声明输入字段"
        assert contract.outputs, f"{component} 没有声明产出字段"
        assert isinstance(contract.retryable, bool)
        assert contract.description

    @pytest.mark.parametrize("component", BUILTIN_COMPONENTS)
    def test_装载时契约挂到节点参数上(self, component):
        pipeline = Pipeline(_chain_for(_case(component), {}), _registry_with_noop())

        comp = pipeline.dsl.components[_case(component).node]
        assert comp.params.contract is not None
        assert comp.params.contract.name == component

    @pytest.mark.parametrize("component", BUILTIN_COMPONENTS)
    def test_契约能出机器可读体检表(self, component):
        contract = _contract_of(component)

        payload = contract.to_dict()
        assert payload["name"] == component
        assert payload["inputs"] and payload["outputs"]
        assert "retryable" in payload
        assert component in contract.describe()


def _case(component: str) -> ContractCase:
    """按组件名取矩阵行（同一组件取第一行）。"""
    for case in CONTRACT_MATRIX:
        if case.component == component:
            return case
    raise AssertionError(f"矩阵里没有组件 {component}")


# ---------------------------------------------------------------------------
# 2. 缺必填项 → 链停在那一个节点
# ---------------------------------------------------------------------------
class TestMissingRequiredField:
    @pytest.mark.parametrize("case", CONTRACT_MATRIX, ids=lambda c: c.case_id)
    def test_缺必填项时链停在那一节点(self, case, store):
        pipeline = Pipeline(
            _chain_for(case, case.params),
            _registry_with_noop(),
            store=store,
            start=case.node,
        )

        result = pipeline.run(case.context)

        # 停在被测节点：它自己没跑，后面的节点也没跑。
        assert result.status == "transferred_to_human"
        assert result.failed_node == case.node
        assert case.node not in result.executed
        assert "after" not in result.executed
        # 报错信息带节点名与字段名 —— 值班要知道缺的是哪样东西。
        assert case.node in result.error
        assert case.missing_field in result.error

    @pytest.mark.parametrize(
        "case",
        [c for c in CONTRACT_MATRIX if c.missing_field != "event"],
        ids=lambda c: c.case_id,
    )
    def test_缺必填项时事件落转人工并留审计(self, case, store):
        event = _event()
        store.record_event(event)
        ctx = dict(case.context)
        ctx.setdefault("event", event)

        pipeline = Pipeline(
            _chain_for(case, case.params),
            _registry_with_noop(),
            store=store,
            start=case.node,
        )
        pipeline.run(ctx)

        assert store.get_status(event.event_id) == "transferred_to_human"
        actions = [row["action"] for row in store.audit_entries(event.event_id)]
        assert "flow_contract_violation" in actions
        details = [
            row["detail"]
            for row in store.audit_entries(event.event_id)
            if row["action"] == "flow_contract_violation"
        ]
        assert details and case.missing_field in details[0]

    @pytest.mark.parametrize(
        "case",
        [c for c in CONTRACT_MATRIX if c.missing_field == "event"],
        ids=lambda c: c.case_id,
    )
    def test_缺事件时事件不丢_仍在台账里等人(self, case, store):
        """连事件都没拿到时无从标记状态 —— 但事件不能因此丢在台账外。"""
        event = _event()
        store.record_event(event)

        pipeline = Pipeline(
            _chain_for(case, case.params),
            _registry_with_noop(),
            store=store,
            start=case.node,
        )
        result = pipeline.run({})

        assert result.status == "transferred_to_human"
        assert store.get_event(event.event_id) is not None
        assert store.get_status(event.event_id) == "received"

    def test_契约校验发生在节点跑之前(self, store):
        """节点自己一次都没被调用 —— 校验在跑之前。"""
        calls: list = []
        registry = NodeRegistry()
        registry.register("spy", lambda name, param: SpyNode(name, param, calls))
        registry.register("noop", NoopNode)
        registry.register(DEFAULT_FALLBACK_COMPONENT, TransferToHumanNode)

        dsl = FlowDSL()
        dsl.add("spy_0", "spy", {}, downstream=["after"])
        dsl.add("after", "noop", {})
        dsl.add(DEFAULT_FALLBACK_COMPONENT, DEFAULT_FALLBACK_COMPONENT, {})

        # 契约里点名要 "thing"，上下文里没有 → 跑之前就该停。
        dsl.components["spy_0"].params.contract = NodeContract(
            name="spy",
            inputs=(FieldSpec("thing"),),
        )

        Pipeline(dsl, registry, store=store, start="spy_0").run({})

        assert calls == []


# ---------------------------------------------------------------------------
# 3. 产出形状不符 → 同样当场停住
# ---------------------------------------------------------------------------
class TestOutputShape:
    @pytest.mark.parametrize("case", CONTRACT_MATRIX, ids=lambda c: c.case_id)
    def test_产出形状不符时链停在那一节点(self, case, store):
        pipeline = Pipeline(
            _chain_for(case, case.full_params),
            _bad_output_registry(case),
            store=store,
            start=case.node,
        )

        result = pipeline.run(case.full_context)

        assert result.status == "transferred_to_human"
        assert result.failed_node == case.node
        assert "after" not in result.executed
        # 报错里能看出**缺哪个字段**。
        missing = [
            name
            for name in case.expected_outputs
            if name not in case.bad_output
        ]
        assert missing, "矩阵里的畸形产出应当少交至少一个字段"
        assert missing[0] in result.error
        assert case.node in result.error

    def test_产出类型不对也算形状不符(self, store):
        """字段在，但类型不对 —— 同样当场停住。"""
        contract = _contract_of("judge")

        class WrongTypeNode:
            def __init__(self, name: str, param: NodeParam) -> None:
                self.name = name
                self.param = param

            def contract(self) -> NodeContract:
                return contract

            def run(self, context):
                return {"decision": 123, "item_code": "718-7"}  # decision 该是 str

        registry = NodeRegistry()
        registry.register("judge", WrongTypeNode)
        registry.register("noop", NoopNode)
        registry.register(DEFAULT_FALLBACK_COMPONENT, TransferToHumanNode)

        dsl = FlowDSL()
        dsl.add("judged", "judge", {"rows": []}, downstream=["after"])
        dsl.add("after", "noop", {})
        dsl.add(DEFAULT_FALLBACK_COMPONENT, DEFAULT_FALLBACK_COMPONENT, {})

        result = Pipeline(dsl, registry, store=store, start="judged").run(
            {"event": _event()}
        )

        assert result.status == "transferred_to_human"
        assert "decision" in result.error
        assert "str" in result.error

    def test_产出不是映射也算形状不符(self, store):
        contract = _contract_of("judge")

        class NotMappingNode:
            def __init__(self, name: str, param: NodeParam) -> None:
                self.name = name
                self.param = param

            def contract(self) -> NodeContract:
                return contract

            def run(self, context):
                return ["不是", "映射"]

        registry = NodeRegistry()
        registry.register("judge", NotMappingNode)
        registry.register("noop", NoopNode)
        registry.register(DEFAULT_FALLBACK_COMPONENT, TransferToHumanNode)

        dsl = FlowDSL()
        dsl.add("judged", "judge", {"rows": []}, downstream=["after"])
        dsl.add("after", "noop", {})
        dsl.add(DEFAULT_FALLBACK_COMPONENT, DEFAULT_FALLBACK_COMPONENT, {})

        result = Pipeline(dsl, registry, store=store, start="judged").run(
            {"event": _event()}
        )

        assert result.status == "transferred_to_human"
        assert "映射" in result.error


# ---------------------------------------------------------------------------
# 4. 可重试性声明
# ---------------------------------------------------------------------------
class TestRetryableDeclaration:
    def test_推送环节声明可重试(self, store):
        """推送失败可以重试（通道自带退避重试）—— 结果与审计都写明。"""
        case = _case("notify")
        pipeline = Pipeline(
            _chain_for(case, case.params),
            _registry_with_noop(),
            store=store,
            start=case.node,
        )

        result = pipeline.run(case.context)

        assert result.failed_node == "notified"
        assert result.failed_node_retryable is True

    def test_判定环节声明不可重试(self, store):
        """纯计算环节重跑还是同一个答案 —— 重试没有意义。"""
        case = _case("judge")
        pipeline = Pipeline(
            _chain_for(case, {"rows": []}),
            _registry_with_noop(),
            store=store,
            start=case.node,
        )

        result = pipeline.run({})

        assert result.failed_node == "judged"
        assert result.failed_node_retryable is False

    def test_审计里写明这一环能不能重试(self, store):
        event = _event()
        store.record_event(event)
        case = _case("notify")

        pipeline = Pipeline(
            _chain_for(case, case.params),
            _registry_with_noop(),
            store=store,
            start=case.node,
        )
        pipeline.run({"event": event})

        details = [
            row["detail"] for row in store.audit_entries(event.event_id)
            if row["action"] == "flow_contract_violation"
        ]
        assert details
        assert "可重试" in details[0]


# ---------------------------------------------------------------------------
# 5. 参数不合法仍然硬停（与契约不满足区分开）
# ---------------------------------------------------------------------------
class TestParamErrorStillHardStops:
    def test_链上写错的必填参数仍然抛出去(self, store):
        """参数写错是配置错误 —— 抛出去让人改配置，不吞。"""
        dsl = FlowDSL()
        dsl.add("a", "noop", {}, required=("source",))
        dsl.add(DEFAULT_FALLBACK_COMPONENT, DEFAULT_FALLBACK_COMPONENT, {})

        pipeline = Pipeline(dsl, _registry_with_noop(), store=store, start="a")

        with pytest.raises(NodeParamError) as excinfo:
            pipeline.run({})

        assert excinfo.value.node == "a"
        assert excinfo.value.field == "source"
        # 事件仍然落转人工，不留在原地。
        assert store.get_status("evt-contract-0001") in ("", "transferred_to_human")

    def test_契约错误是参数错误的子类但走转人工(self, store):
        """两类错误都带节点名与字段名，但收场方式不同。"""
        assert issubclass(NodeContractError, NodeParamError)
        assert issubclass(NodeOutputError, NodeContractError)


# ---------------------------------------------------------------------------
# 6. 体检表
# ---------------------------------------------------------------------------
class TestContractReport:
    def test_体检表逐节点列出契约(self):
        from critval.flow.nodes import standard_chain

        pipeline = Pipeline(standard_chain(), build_default_registry())

        report = pipeline.contract_report()
        names = [row["node"] for row in report]

        assert "judged" in names
        assert "card_built" in names
        judge_row = next(row for row in report if row["node"] == "judged")
        assert judge_row["has_contract"] is True
        assert "event" in judge_row["inputs"]
        assert "decision" in judge_row["outputs"]
        assert judge_row["retryable"] is False
