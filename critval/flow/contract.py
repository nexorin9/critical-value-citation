"""节点契约：每个处置链节点对外承诺的「输入形状 / 参数合法性 / 输出形状 / 可重试性」。

**为什么要有契约**：处置链上的节点是**可插**的 —— 判定、召回、出卡、推送、
确认、回写，六段各写各的。节点之间靠**共享上下文**传东西，而共享上下文
是「谁想读什么就读什么」的口袋：上游忘了往里写，下游读到的就是 ``None``，
再往下走几步就变成「空值沿着链传下去，最后在某个毫不相干的地方炸掉」。

契约把这条约定**写下来并让它可校验**：

* **输入字段** —— 这个节点要从参数或上下文里拿到哪些东西，缺一个就不许跑；
* **参数校验器** —— 拿到的值合不合法（类型、取值范围）；
* **输出字段** —— 节点跑完必须交出哪些字段，后面的人才知道能读什么；
* **可重试性** —— 这一环失败之后，**重跑一遍有没有意义**（纯计算的判定
  重跑还是同一个答案；推送与回写重跑才会换结果）。

**同源参考**：``github_ref/RAGFlow/agent/component/answer.py`` 的
``AnswerParam.check()`` 与 ``retrieval.py`` 的 ``RetrievalParam.check()`` ——
源产品把「参数合法性」收在每个组件自己的 ``check()`` 里（逐项
``check_empty`` / ``check_positive_number`` / ``check_decimal_float``），
装载时逐节点调一次（``canvas.py:117``）。源产品的 ``check()`` 只管**参数**，
不管**输入形状**与**输出形状** —— 组件读到什么、吐出什么，全靠约定。

本项目把这份约定补成一份**可声明、可校验、可测**的契约：输入字段与输出
字段都进契约，``param.check()`` 逐节点过一遍，**不满足就即停**。

**停在哪、怎么停**：契约不满足分两种，处置方式不同 ——

* **参数不合法**（链上写错了，比如阈值行号写错位）→ 硬停，抛
  :class:`NodeParamError`，链不再往下走（配置错误要立刻暴露）；
* **契约不满足**（节点自己该拿的东西没拿到 / 交出来的东西形状不对）→
  落 **转人工**：事件不丢、有人接，链在这一步停下，后面的节点一个都不跑。

两种情况都**不会**带着坏参数继续往下跑 —— 那正是把「配置写错了」放大成
「静默错判」的典型路径。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional

__all__ = [
    "FieldSpec",
    "NodeContract",
    "NodeContractError",
    "NodeOutputError",
    "NodeParamError",
    "WHERE_CONTEXT",
    "WHERE_EITHER",
    "WHERE_PARAM",
]


#: 字段必须出现在**节点参数**里。
WHERE_PARAM = "param"
#: 字段必须出现在**共享上下文**里。
WHERE_CONTEXT = "context"
#: 字段在参数或上下文里**任一**处有就行（参数优先）。
WHERE_EITHER = "either"


class NodeParamError(ValueError):
    """节点参数不合法 —— 链在这一步**即停**。

    带三个字段，调用方可以直接写进报错文案与审计：

    * ``node`` —— 是哪个节点；
    * ``field`` —— 是哪个字段；
    * ``reason`` —— 为什么不合格（缺字段 / 类型不对 / 自定义校验不过）。
    """

    def __init__(self, node: str, field: str, reason: str) -> None:
        self.node = node
        self.field = field
        self.reason = reason
        super().__init__(f"节点 {node} 的参数 {field} 不合法：{reason}")


class NodeContractError(NodeParamError):
    """节点**契约**不满足 —— 输入形状、参数合法性、输出形状对不上。

    单独一个类型（而不是直接用 :class:`NodeParamError`）：链上「配置写错」
    要硬停（抛出去让人改配置），节点「自己该拿的没拿到」要落转人工
    （事件不丢、有人接）。两者都用 ``node`` / ``field`` / ``reason``
    三个字段，报错文案一致。
    """


class NodeOutputError(NodeContractError):
    """节点**产出形状**不符 —— 契约要求带的字段没带，或类型不对。"""


@dataclass(frozen=True)
class FieldSpec:
    """契约里的一个字段。

    * ``name`` —— 字段名；
    * ``where`` —— 这个字段该出现在哪儿（参数 / 上下文 / 两者任一）；
    * ``types`` —— 允许的类型（元组；空表示不校验类型）；
    * ``validator`` —— 自定义校验函数。返回 ``None`` 表示通过，
      返回一段文字表示不通过（那段文字就是 ``reason``）；
    * ``note`` —— 人话说明（写进体检报告与报错文案）。
    """

    name: str
    where: str = WHERE_EITHER
    types: tuple[type, ...] = ()
    validator: Optional[Callable[[Any], Optional[str]]] = None
    note: str = ""

    def matches(self, value: Any) -> bool:
        """值的类型合不合契约。"""
        if not self.types:
            return True
        return isinstance(value, self.types)

    def expected(self) -> str:
        """类型期望的人话写法（``str`` / ``str 或 int``）。"""
        return " 或 ".join(t.__name__ for t in self.types) if self.types else "（不限）"


@dataclass(frozen=True)
class NodeContract:
    """一个节点的契约。

    * ``name`` —— 组件名（注册表里的键）；
    * ``inputs`` —— 节点要读到的字段（参数里或上下文里）；
    * ``outputs`` —— 节点产出必须带的字段；
    * ``retryable`` —— 这一环失败后**重跑有没有意义**；
    * ``description`` —— 一句话说明这个节点干什么。
    """

    name: str = ""
    inputs: tuple[FieldSpec, ...] = ()
    outputs: tuple[FieldSpec, ...] = ()
    retryable: bool = False
    description: str = ""

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def input_names(self) -> tuple[str, ...]:
        return tuple(spec.name for spec in self.inputs)

    def output_names(self) -> tuple[str, ...]:
        return tuple(spec.name for spec in self.outputs)

    def input_spec(self, name: str) -> Optional[FieldSpec]:
        for spec in self.inputs:
            if spec.name == name:
                return spec
        return None

    def output_spec(self, name: str) -> Optional[FieldSpec]:
        for spec in self.outputs:
            if spec.name == name:
                return spec
        return None

    # ------------------------------------------------------------------
    # 产出形状校验
    # ------------------------------------------------------------------
    def check_output(self, node: str, output: Any) -> None:
        """校验节点产出 —— 不符就抛 :class:`NodeOutputError`。

        查两件事：产出是不是个映射；契约要求的字段在不在、类型对不对。
        """
        if not self.outputs:
            return
        if not isinstance(output, Mapping):
            first = self.outputs[0].name
            raise NodeOutputError(
                node,
                first,
                f"产出应为映射（dict），实际为 {type(output).__name__}",
            )
        for spec in self.outputs:
            if spec.name not in output:
                raise NodeOutputError(node, spec.name, "产出缺少契约要求的字段")
            value = output[spec.name]
            if not spec.matches(value):
                raise NodeOutputError(
                    node,
                    spec.name,
                    f"类型应为 {spec.expected()}，实际为 {type(value).__name__}",
                )
            if spec.validator is not None:
                reason = spec.validator(value)
                if reason:
                    raise NodeOutputError(node, spec.name, str(reason))

    # ------------------------------------------------------------------
    # 体检报告
    # ------------------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        """机器可读视图（体检命令与文档用）。"""

        def _spec(item: FieldSpec) -> dict[str, Any]:
            return {
                "name": item.name,
                "where": item.where,
                "types": [t.__name__ for t in item.types],
                "note": item.note,
            }

        return {
            "name": self.name,
            "description": self.description,
            "retryable": self.retryable,
            "inputs": [_spec(item) for item in self.inputs],
            "outputs": [_spec(item) for item in self.outputs],
        }

    def describe(self) -> str:
        """人话摘要（体检命令与报错文案用）。"""
        ins = "、".join(self.input_names()) or "（无）"
        outs = "、".join(self.output_names()) or "（无）"
        retry = "可重试" if self.retryable else "不可重试"
        return f"{self.name}：输入 {ins}；产出 {outs}；{retry}"
