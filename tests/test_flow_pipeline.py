"""处置链执行器的单元测试：推进 / 回路保护 / 异常回落转人工 / 参数即停。

对应 task id 12 的「写 ``tests/test_flow_pipeline.py``（构造带回路的处置链
断言抛错；注入节点异常断言落到 ``transferred_to_human`` 且审计有记录）」。

盯住四件事：

* **参数不过即停** —— 缺参的节点不跑，后面的节点也不跑；
* **回路超限抛错** —— 链在同一节点上绕圈时抛 ``FlowLoopError``，
  不许无限绕；
* **异常回落转人工** —— 节点抛异常时事件状态落到 ``transferred_to_human``，
  审计流水里有记录，**不静默吞掉**；
* **正常止步不是错误** —— 不危急这类业务结论让链正常停下，
  而不是抛异常。
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from critval.flow.dsl import (
    DEFAULT_FALLBACK_COMPONENT,
    DSLValidationError,
    FlowDSL,
    NodeParam,
    NodeParamError,
    NodeRegistry,
)
from critval.flow.nodes import TransferToHumanNode, build_default_registry, standard_chain
from critval.flow.pipeline import FlowLoopError, FlowStop, Pipeline
from critval.ledger.store import LedgerStore
from critval.models import CriticalEvent


def _event(event_id: str = "evt-flow-0001") -> CriticalEvent:
    return CriticalEvent(
        event_id=event_id,
        message_control_id="MC-FLOW-0001",
        specimen_no="SP-FLOW-0001",
        item_code="718-7",
        item_name="血红蛋白",
        value=Decimal("25"),
        unit="g/L",
        observed_at=datetime(2026, 1, 2, 8, 30, tzinfo=timezone.utc),
        dept_code="LAB",
        patient_ref="pt-000000000001",
    )


@pytest.fixture()
def store() -> LedgerStore:
    instance = LedgerStore(":memory:")
    try:
        yield instance
    finally:
        instance.close()


# ---------------------------------------------------------------------------
# 测试用的节点
# ---------------------------------------------------------------------------
class RecordingNode:
    """记录自己跑过几次的节点。"""

    def __init__(self, name: str, param: NodeParam, calls: list) -> None:
        self.name = name
        self.param = param
        self.calls = calls

    def run(self, context):
        self.calls.append(self.name)
        return {"node": self.name}


class BoomNode:
    """一跑就炸的节点。"""

    def __init__(self, name: str, param: NodeParam) -> None:
        self.name = name
        self.param = param
        self.exception = None

    def set_exception(self, exc):
        self.exception = exc

    def run(self, context):
        raise RuntimeError("这个环节炸了")


class StopNode:
    """正常止步的节点（不危急这类业务结论）。"""

    def __init__(self, name: str, param: NodeParam) -> None:
        self.name = name
        self.param = param

    def run(self, context):
        raise FlowStop("判定结论为不危急")


def _registry_with(node_cls, calls: list) -> NodeRegistry:
    registry = NodeRegistry()
    registry.register("step", lambda name, param: node_cls(name, param, calls))
    registry.register("boom", BoomNode)
    registry.register("stop", StopNode)
    registry.register(DEFAULT_FALLBACK_COMPONENT, TransferToHumanNode)
    return registry


# ---------------------------------------------------------------------------
# 1. 沿下游推进
# ---------------------------------------------------------------------------
class TestAdvance:
    def test_沿downstream按顺序推进(self):
        calls: list = []
        dsl = FlowDSL()
        dsl.add("a", "step", {}, downstream=["b"])
        dsl.add("b", "step", {}, downstream=["c"])
        dsl.add("c", "step", {})

        pipeline = Pipeline(dsl, _registry_with(RecordingNode, calls))
        result = pipeline.run({})

        assert calls == ["a", "b", "c"]
        assert result.status == "completed"
        assert result.executed == ["a", "b", "c"]
        assert result.ok is True

    def test_历史里留有每个节点的产出(self):
        calls: list = []
        dsl = FlowDSL()
        dsl.add("a", "step", {}, downstream=["b"])
        dsl.add("b", "step", {})

        pipeline = Pipeline(dsl, _registry_with(RecordingNode, calls))
        result = pipeline.run({})

        assert [name for name, _ in result.history] == ["a", "b"]
        assert result.history[0][1] == {"node": "a"}

    def test_节点产出写进上下文_后面的节点读得到(self):
        seen: list = []

        class WriterNode:
            def __init__(self, name, param):
                self.name = name
                self.param = param

            def run(self, context):
                context["judgement"] = "critical"
                return {"written": True}

        class ReaderNode:
            def __init__(self, name, param):
                self.name = name
                self.param = param

            def run(self, context):
                seen.append(context.get("judgement"))
                return {"read": context.get("judgement")}

        registry = NodeRegistry()
        registry.register("write", WriterNode)
        registry.register("read", ReaderNode)

        dsl = FlowDSL()
        dsl.add("w", "write", {}, downstream=["r"])
        dsl.add("r", "read", {})

        Pipeline(dsl, registry).run({})

        assert seen == ["critical"]


# ---------------------------------------------------------------------------
# 2. 参数不过即停
# ---------------------------------------------------------------------------
class TestParamCheck:
    def test_缺参节点不跑_后续节点也不跑(self):
        calls: list = []
        dsl = FlowDSL()
        dsl.add("a", "step", {}, downstream=["b"])
        dsl.add("b", "step", {}, downstream=["c"], required=("source",))
        dsl.add("c", "step", {})

        pipeline = Pipeline(dsl, _registry_with(RecordingNode, calls))

        with pytest.raises(NodeParamError) as excinfo:
            pipeline.run({})

        # 缺参的节点 b 没跑，它后面的 c 也没跑。
        assert calls == ["a"]
        assert excinfo.value.node == "b"
        assert excinfo.value.field == "source"

    def test_参数不过时链即停_不继续推进(self):
        calls: list = []
        dsl = FlowDSL()
        dsl.add("a", "step", {}, downstream=["b"], required=("source",))
        dsl.add("b", "step", {}, downstream=["c"])
        dsl.add("c", "step", {})

        pipeline = Pipeline(dsl, _registry_with(RecordingNode, calls))

        with pytest.raises(NodeParamError):
            pipeline.run({})

        assert calls == []
        assert pipeline.dsl.executed() == []

    def test_预检入口能提前发现缺参(self):
        dsl = FlowDSL()
        dsl.add("a", "step", {}, required=("source",))

        pipeline = Pipeline(dsl, _registry_with(RecordingNode, []))

        with pytest.raises(NodeParamError) as excinfo:
            pipeline.validate_params()

        assert excinfo.value.node == "a"


# ---------------------------------------------------------------------------
# 3. 回路保护
# ---------------------------------------------------------------------------
class TestLoopGuard:
    def test_构造带回路的链时抛错(self):
        calls: list = []
        dsl = FlowDSL()
        dsl.add("a", "step", {}, downstream=["b"])
        dsl.add("b", "step", {}, downstream=["a"])

        pipeline = Pipeline(dsl, _registry_with(RecordingNode, calls), start="a")

        with pytest.raises(FlowLoopError) as excinfo:
            pipeline.run({})

        assert "回路" in str(excinfo.value)
        assert excinfo.value.max_loops == 6

    def test_回路错误是OverflowError的子类(self):
        dsl = FlowDSL()
        dsl.add("a", "step", {}, downstream=["b"])
        dsl.add("b", "step", {}, downstream=["a"])

        pipeline = Pipeline(dsl, _registry_with(RecordingNode, []), start="a")

        with pytest.raises(OverflowError):
            pipeline.run({})

    def test_回路片段能看出是哪两个节点在勾连(self):
        dsl = FlowDSL()
        dsl.add("a", "step", {}, downstream=["b"])
        dsl.add("b", "step", {}, downstream=["a"])

        pipeline = Pipeline(dsl, _registry_with(RecordingNode, []), start="a")

        with pytest.raises(FlowLoopError) as excinfo:
            pipeline.run({})

        assert "a" in excinfo.value.pattern
        assert "b" in excinfo.value.pattern

    def test_纯回路的链必须显式指明起点(self):
        dsl = FlowDSL()
        dsl.add("a", "step", {}, downstream=["b"])
        dsl.add("b", "step", {}, downstream=["a"])

        pipeline = Pipeline(dsl, _registry_with(RecordingNode, []))

        with pytest.raises(DSLValidationError) as excinfo:
            pipeline.run({})

        assert "入口" in str(excinfo.value)

    def test_自环节点不会无限绕(self):
        dsl = FlowDSL()
        dsl.add("a", "step", {}, downstream=["a"])

        pipeline = Pipeline(dsl, _registry_with(RecordingNode, []))

        with pytest.raises(FlowLoopError):
            pipeline.run({})

    def test_没有回路的链正常跑完(self):
        calls: list = []
        dsl = FlowDSL()
        dsl.add("a", "step", {}, downstream=["b"])
        dsl.add("b", "step", {})

        pipeline = Pipeline(dsl, _registry_with(RecordingNode, calls))
        result = pipeline.run({})

        assert result.status == "completed"
        assert calls == ["a", "b"]


# ---------------------------------------------------------------------------
# 4. 异常回落转人工
# ---------------------------------------------------------------------------
class TestFallbackToHuman:
    def test_节点异常时状态落到转人工(self, store):
        event = _event()
        store.record_event(event)

        dsl = FlowDSL()
        dsl.add("a", "step", {}, downstream=["boom"])
        dsl.add("boom", "boom", {})
        dsl.add(DEFAULT_FALLBACK_COMPONENT, DEFAULT_FALLBACK_COMPONENT, {})

        pipeline = Pipeline(dsl, _registry_with(RecordingNode, []), store=store)
        result = pipeline.run({"event": event})

        assert result.status == "transferred_to_human"
        assert result.ok is False
        assert result.failed_node == "boom"
        assert "这个环节炸了" in result.error
        assert store.get_status(event.event_id) == "transferred_to_human"

    def test_节点异常时审计有记录(self, store):
        event = _event()
        store.record_event(event)

        dsl = FlowDSL()
        dsl.add("a", "step", {}, downstream=["boom"])
        dsl.add("boom", "boom", {})
        dsl.add(DEFAULT_FALLBACK_COMPONENT, DEFAULT_FALLBACK_COMPONENT, {})

        pipeline = Pipeline(dsl, _registry_with(RecordingNode, []), store=store)
        pipeline.run({"event": event})

        actions = [row["action"] for row in store.audit_entries(event.event_id)]
        assert "flow_node_failed" in actions
        # 状态跃迁也留痕。
        assert "status_transition" in actions

    def test_异常时兜底出口真的被调用了(self, store):
        event = _event()
        store.record_event(event)

        dsl = FlowDSL()
        dsl.add("boom", "boom", {})
        dsl.add(DEFAULT_FALLBACK_COMPONENT, DEFAULT_FALLBACK_COMPONENT, {})

        pipeline = Pipeline(dsl, _registry_with(RecordingNode, []), store=store)
        result = pipeline.run({"event": event})

        assert result.fallback_used is True
        assert pipeline.dsl.answer == [DEFAULT_FALLBACK_COMPONENT]

    def test_兜底出口拿得到异常内容(self, store):
        event = _event()
        store.record_event(event)

        seen: list = []

        class SpyFallback:
            def __init__(self, name, param):
                self.name = name
                self.param = param
                self.exception = None

            def set_exception(self, exc):
                self.exception = exc

            def run(self, context):
                seen.append(context.get("exception"))
                return {"ok": False}

        registry = NodeRegistry()
        registry.register("boom", BoomNode)
        registry.register(DEFAULT_FALLBACK_COMPONENT, SpyFallback)

        dsl = FlowDSL()
        dsl.add("boom", "boom", {})
        dsl.add(DEFAULT_FALLBACK_COMPONENT, DEFAULT_FALLBACK_COMPONENT, {})

        Pipeline(dsl, registry, store=store).run({"event": event})

        assert len(seen) == 1
        assert isinstance(seen[0], RuntimeError)
        assert "这个环节炸了" in str(seen[0])

    def test_链上没有兜底出口时事件仍然转人工(self, store):
        event = _event()
        store.record_event(event)

        dsl = FlowDSL()
        dsl.add("boom", "boom", {})

        pipeline = Pipeline(dsl, _registry_with(RecordingNode, []), store=store)
        result = pipeline.run({"event": event})

        assert result.status == "transferred_to_human"
        assert result.fallback_used is False
        assert store.get_status(event.event_id) == "transferred_to_human"

    def test_转人工时事件不丢_仍在台账里(self, store):
        event = _event()
        store.record_event(event)

        dsl = FlowDSL()
        dsl.add("boom", "boom", {})

        Pipeline(dsl, _registry_with(RecordingNode, []), store=store).run({"event": event})

        assert store.get_event(event.event_id) is not None

    def test_兜底出口自己炸了也不吞掉异常_事件仍然转人工(self, store):
        event = _event()
        store.record_event(event)

        class BrokenFallback:
            def __init__(self, name, param):
                self.name = name
                self.param = param

            def run(self, context):
                raise RuntimeError("兜底出口自己也炸了")

        registry = NodeRegistry()
        registry.register("boom", BoomNode)
        registry.register(DEFAULT_FALLBACK_COMPONENT, BrokenFallback)

        dsl = FlowDSL()
        dsl.add("boom", "boom", {})
        dsl.add(DEFAULT_FALLBACK_COMPONENT, DEFAULT_FALLBACK_COMPONENT, {})

        pipeline = Pipeline(dsl, registry, store=store)
        result = pipeline.run({"event": event})

        assert result.status == "transferred_to_human"
        assert result.fallback_used is False
        assert store.get_status(event.event_id) == "transferred_to_human"

    def test_参数不过时事件也落转人工不留在原地(self, store):
        event = _event()
        store.record_event(event)

        dsl = FlowDSL()
        dsl.add("a", "step", {}, required=("source",))

        pipeline = Pipeline(dsl, _registry_with(RecordingNode, []), store=store)

        with pytest.raises(NodeParamError):
            pipeline.run({"event": event})

        assert store.get_status(event.event_id) == "transferred_to_human"
        actions = [row["action"] for row in store.audit_entries(event.event_id)]
        assert "flow_param_invalid" in actions


# ---------------------------------------------------------------------------
# 5. 正常止步
# ---------------------------------------------------------------------------
class TestNormalStop:
    def test_节点要求止步时链正常停下(self):
        dsl = FlowDSL()
        dsl.add("a", "stop", {}, downstream=["b"])
        dsl.add("b", "step", {})

        calls: list = []
        registry = _registry_with(RecordingNode, calls)
        pipeline = Pipeline(dsl, registry)
        result = pipeline.run({})

        assert result.status == "stopped"
        assert result.ok is True
        assert calls == []

    def test_止步不是错误_不写转人工审计(self, store):
        event = _event()
        store.record_event(event)

        dsl = FlowDSL()
        dsl.add("a", "stop", {})

        pipeline = Pipeline(dsl, _registry_with(RecordingNode, []), store=store)
        pipeline.run({"event": event})

        actions = [row["action"] for row in store.audit_entries(event.event_id)]
        assert "flow_node_failed" not in actions
        assert "flow_stopped" in actions


# ---------------------------------------------------------------------------
# 6. 标准链与内置节点
# ---------------------------------------------------------------------------
class TestStandardChain:
    def test_标准链的节点与状态机主链对得上(self):
        dsl = standard_chain()

        assert list(dsl.components) == [
            "received",
            "judged",
            "card_built",
            "notified",
            "acknowledged",
            "registered",
            "archived",
            DEFAULT_FALLBACK_COMPONENT,
        ]
        assert dsl.entry() == "received"

    def test_标准链拓扑自检通过(self):
        standard_chain().validate()

    def test_标准链能用默认注册表装载(self):
        pipeline = Pipeline(standard_chain(), build_default_registry())

        assert pipeline.dsl.components["judged"].obj is not None
        assert pipeline.dsl.components["card_built"].obj is not None

    def test_兜底出口不在主路径上(self):
        dsl = standard_chain()

        assert dsl.components["archived"].downstream == []
        assert DEFAULT_FALLBACK_COMPONENT not in dsl.components["archived"].downstream


# ---------------------------------------------------------------------------
# 7. 内置节点：链真的驱动业务状态机
# ---------------------------------------------------------------------------
SAMPLE_THRESHOLD = (
    Path(__file__).resolve().parent.parent
    / "samples"
    / "threshold"
    / "示例危急值阈值表.xlsx"
)


def _sample_rows():
    from critval.kb.loader import load_threshold_table

    return load_threshold_table(SAMPLE_THRESHOLD).rows


class TestBuiltinNodes:
    """内置节点把「跑完一步」翻译成业务状态跃迁。

    这几条用例盯的是**链与状态机的咬合** —— 判定节点判危急就把状态推到
    ``judged``，出卡节点出完卡就推到 ``card_built``；判不危急的链**止步**
    （不出卡、不推送），判定不成立的链**转人工**。
    """

    def _chain(self, rows):
        dsl = FlowDSL()
        dsl.add("received", "received", {}, downstream=["judged"])
        dsl.add("judged", "judge", {"rows": rows}, downstream=["card_built"])
        dsl.add("card_built", "card", {}, downstream=["notified"])
        dsl.add("notified", "notify", {}, downstream=["acknowledged"])
        dsl.add("acknowledged", "ack", {}, downstream=[])
        dsl.add("transfer_to_human", DEFAULT_FALLBACK_COMPONENT, {}, downstream=[])
        return dsl

    def test_判危急时状态走到已出卡(self, store):
        event = _event()
        dsl = self._chain(_sample_rows())
        pipeline = Pipeline(dsl, build_default_registry(), store=store)

        result = pipeline.run({"event": event})

        # 推送节点没有通知出口 → 链在此异常回落；出卡这一步已经完成。
        assert "card_built" in result.executed
        assert store.get_status(event.event_id) == "transferred_to_human"
        actions = [row["action"] for row in store.audit_entries(event.event_id)]
        assert "status_transition" in actions

    def test_判不危急时链止步且不出卡(self, store):
        event = _event()
        from dataclasses import replace

        # 血红蛋白 120 g/L 落在阈值表给的参考区间里（低危急 30 / 高危急 200）→ 判不危急。
        quiet = replace(event, value=Decimal("120"))
        dsl = self._chain(_sample_rows())
        pipeline = Pipeline(dsl, build_default_registry(), store=store)

        result = pipeline.run({"event": quiet})

        assert result.status == "stopped"
        assert "card_built" not in result.executed
        assert store.get_status(quiet.event_id) == "not_critical"

    def test_阈值表没收录的项目转人工(self, store):
        from dataclasses import replace

        event = replace(_event(), item_code="9999-9", item_name="不存在的项目")
        dsl = self._chain(_sample_rows())
        pipeline = Pipeline(dsl, build_default_registry(), store=store)

        result = pipeline.run({"event": event})

        assert result.status == "stopped"
        assert store.get_status(event.event_id) == "transferred_to_human"

    def test_标准链的内置节点都能装载(self):
        pipeline = Pipeline(standard_chain(), build_default_registry())

        for name, comp in pipeline.dsl.components.items():
            assert comp.obj is not None, f"节点 {name} 没有装载"
