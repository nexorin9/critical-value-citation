"""处置链 DSL 的单元测试：节点注册、上下游关系、参数校验、拓扑自检。

对应 task id 12 的「写 ``tests/test_flow_dsl.py``（节点注册与上下游关系）」。
"""

from __future__ import annotations

import pytest

from critval.flow.dsl import (
    DEFAULT_FALLBACK_COMPONENT,
    DSLValidationError,
    FlowDSL,
    NodeParam,
    NodeParamError,
    NodeRegistry,
)


# ---------------------------------------------------------------------------
# 测试用的迷你节点
# ---------------------------------------------------------------------------
class EchoNode:
    """把参数原样吐出来的节点。"""

    def __init__(self, name: str, param: NodeParam) -> None:
        self.name = name
        self.param = param

    def run(self, context):
        return {"name": self.name, "params": dict(self.param.values)}


# ---------------------------------------------------------------------------
# 节点注册表
# ---------------------------------------------------------------------------
class TestNodeRegistry:
    def test_注册后可按组件名造节点(self):
        registry = NodeRegistry()
        registry.register("echo", EchoNode)

        node = registry.create("echo", "echo_0", NodeParam(values={"a": 1}))

        assert isinstance(node, EchoNode)
        assert node.name == "echo_0"
        assert node.param.get("a") == 1

    def test_未注册的组件名报错并列出已注册组件(self):
        registry = NodeRegistry()
        registry.register("echo", EchoNode)

        with pytest.raises(DSLValidationError) as excinfo:
            registry.create("judge", "judge_0", NodeParam())

        assert "judge" in str(excinfo.value)
        assert "echo" in str(excinfo.value)

    def test_组件名重复注册被拒(self):
        registry = NodeRegistry()
        registry.register("echo", EchoNode)

        with pytest.raises(DSLValidationError):
            registry.register("echo", EchoNode)

    def test_空组件名注册被拒(self):
        registry = NodeRegistry()

        with pytest.raises(DSLValidationError):
            registry.register("", EchoNode)

    def test_已注册组件名清单可列出(self):
        registry = NodeRegistry()
        registry.register("echo", EchoNode)
        registry.register("judge", EchoNode)

        assert registry.registered() == ("echo", "judge")
        assert registry.has("echo") is True
        assert registry.has("card") is False


# ---------------------------------------------------------------------------
# 节点参数
# ---------------------------------------------------------------------------
class TestNodeParam:
    def test_update覆盖写入参数(self):
        param = NodeParam(values={"a": 1})
        param.update({"a": 2, "b": 3})

        assert param.get("a") == 2
        assert param.get("b") == 3
        assert "b" in param

    def test_缺必填参数时报出节点名与字段名(self):
        param = NodeParam(values={}, required=("source",))

        with pytest.raises(NodeParamError) as excinfo:
            param.check(node="judge_0")

        assert excinfo.value.node == "judge_0"
        assert excinfo.value.field == "source"
        assert "judge_0" in str(excinfo.value)

    def test_空字符串与None算缺字段_但0不算(self):
        param = NodeParam(values={"a": "", "b": None, "c": 0}, required=("a", "b", "c"))

        assert param.missing_fields() == ("a", "b")

    def test_类型不符时报出字段名(self):
        param = NodeParam(values={"rows": "不是列表"}, types={"rows": list})

        with pytest.raises(NodeParamError) as excinfo:
            param.check(node="judge_0")

        assert excinfo.value.field == "rows"
        assert "list" in str(excinfo.value)

    def test_自定义校验不过时报出校验给的说明(self):
        def _not_empty(value):
            return None if value else "不能为空"

        param = NodeParam(values={"source": ""}, validators={"source": _not_empty})

        with pytest.raises(NodeParamError) as excinfo:
            param.check(node="judge_0")

        assert excinfo.value.reason == "不能为空"

    def test_必填不过时不再看类型(self):
        param = NodeParam(values={}, required=("rows",), types={"rows": list})

        with pytest.raises(NodeParamError) as excinfo:
            param.check(node="judge_0")

        assert excinfo.value.field == "rows"
        assert "必填" in str(excinfo.value)

    def test_全部合格时安静通过(self):
        param = NodeParam(
            values={"rows": [1], "source": "阈值表"},
            required=("rows", "source"),
            types={"rows": list},
        )

        param.check(node="judge_0")  # 不抛异常即通过


# ---------------------------------------------------------------------------
# 拓扑
# ---------------------------------------------------------------------------
class TestFlowDSLTopology:
    def test_声明下游会自动镜像到上游(self):
        dsl = FlowDSL()
        dsl.add("judge_0", "judge", {}, downstream=["card_0"])
        dsl.add("card_0", "card", {})

        assert dsl.components["judge_0"].downstream == ["card_0"]
        assert dsl.components["card_0"].upstream == ["judge_0"]

    def test_声明上游会自动镜像到下游(self):
        dsl = FlowDSL()
        dsl.add("card_0", "card", {}, upstream=["judge_0"])
        dsl.add("judge_0", "judge", {})

        assert dsl.components["judge_0"].downstream == ["card_0"]
        assert dsl.components["card_0"].upstream == ["judge_0"]

    def test_上下游不对称时报出具体节点(self):
        dsl = FlowDSL()
        dsl.add("judge_0", "judge", {}, downstream=["card_0"])
        dsl.add("card_0", "card", {}, upstream=["notify_0"])

        with pytest.raises(DSLValidationError) as excinfo:
            dsl.validate()

        assert "card_0" in str(excinfo.value)

    def test_下游指向不存在的节点时报错(self):
        dsl = FlowDSL()
        dsl.add("judge_0", "judge", {}, downstream=["不存在的节点"])

        with pytest.raises(DSLValidationError) as excinfo:
            dsl.validate()

        assert "不存在" in str(excinfo.value)

    def test_入口节点是没有上游的那个(self):
        dsl = FlowDSL()
        dsl.add("judge_0", "judge", {}, downstream=["card_0"])
        dsl.add("card_0", "card", {})

        assert dsl.entry() == "judge_0"

    def test_兜底出口不算入口(self):
        dsl = FlowDSL()
        dsl.add("judge_0", "judge", {}, downstream=["card_0"])
        dsl.add("card_0", "card", {})
        dsl.add("transfer_to_human", DEFAULT_FALLBACK_COMPONENT, {})

        assert dsl.entry() == "judge_0"

    def test_入口不唯一时报错并列出候选(self):
        dsl = FlowDSL()
        dsl.add("judge_0", "judge", {})
        dsl.add("card_0", "card", {})

        with pytest.raises(DSLValidationError) as excinfo:
            dsl.entry()

        assert "card_0" in str(excinfo.value)
        assert "judge_0" in str(excinfo.value)

    def test_节点重名被拒(self):
        dsl = FlowDSL()
        dsl.add("judge_0", "judge", {})

        with pytest.raises(DSLValidationError):
            dsl.add("judge_0", "card", {})

    def test_取不到节点时报明确错误而不是KeyError(self):
        dsl = FlowDSL()

        with pytest.raises(DSLValidationError):
            dsl.node("judge_0")

    def test_空节点名被拒(self):
        dsl = FlowDSL()

        with pytest.raises(DSLValidationError):
            dsl.add("", "judge", {})


# ---------------------------------------------------------------------------
# 兜底出口查找
# ---------------------------------------------------------------------------
class TestFallbackLookup:
    def test_链上没有兜底出口时返回None(self):
        dsl = FlowDSL()
        dsl.add("judge_0", "judge", {})

        assert dsl.fallback_component() is None

    def test_优先挑链上已经走过的兜底出口(self):
        dsl = FlowDSL()
        dsl.add("judge_0", "judge", {})
        dsl.add("transfer_to_human", DEFAULT_FALLBACK_COMPONENT, {})
        dsl.path = [["judge_0", "transfer_to_human"]]

        found = dsl.fallback_component()

        assert found is not None
        assert found.name == "transfer_to_human"

    def test_没走过时从链上定义里找(self):
        dsl = FlowDSL()
        dsl.add("judge_0", "judge", {})
        dsl.add("transfer_to_human", DEFAULT_FALLBACK_COMPONENT, {})

        found = dsl.fallback_component()

        assert found is not None
        assert found.component_name == DEFAULT_FALLBACK_COMPONENT


# ---------------------------------------------------------------------------
# 序列化
# ---------------------------------------------------------------------------
class TestSerialization:
    def test_to_dict含组件_路径_历史_出口(self):
        dsl = FlowDSL()
        dsl.add("judge_0", "judge", {"source": "阈值表"}, downstream=["card_0"])
        dsl.add("card_0", "card", {})
        dsl.path = [["judge_0"], ["card_0"]]
        dsl.history = [("judge_0", {"decision": "critical"})]

        data = dsl.to_dict()

        assert set(data) >= {"components", "path", "history", "answer", "reference"}
        assert data["components"]["judge_0"]["component_name"] == "judge"
        assert data["components"]["judge_0"]["params"] == {"source": "阈值表"}
        assert data["path"] == [["judge_0"], ["card_0"]]

    def test_to_json是合法JSON(self):
        import json

        dsl = FlowDSL()
        dsl.add("judge_0", "judge", {"source": "阈值表"})

        payload = json.loads(dsl.to_json())

        assert payload["components"]["judge_0"]["component_name"] == "judge"

    def test_跑过的节点按顺序展开(self):
        dsl = FlowDSL()
        dsl.add("judge_0", "judge", {})
        dsl.path = [["judge_0"], ["card_0", "notify_0"]]

        assert dsl.executed() == ["judge_0", "card_0", "notify_0"]
