"""处置链 DSL：把「一条危急值要走过哪些环节」写成一张声明式的图。

**为什么要有一张图**：危急值的处置不是一串写死在函数里的 ``if``，
而是**六个可插的环节**（判定 / 召回 / 出卡 / 推送 / 确认 / 回写）串成的一条
链。把这六个环节写死在一个函数里，会带来三个具体的坏处：

1. 改一个环节就要动一大段代码 —— 检验科改阈值表口径、医务科改通知对象，
   都要去改流程代码；
2. 出问题时说不清「这条事件走到哪一步停的」—— 只有一个巨大的 try；
3. 没法**演练**。想验证「制度条款召回不到时会发生什么」，只能真的去改
   线上材料。

**这张图长什么样**（结构同源参考 ``agent/canvas.py`` 的 ``dsl``）::

    {
      "components": {
        "judge_0": {
          "obj": {"component_name": "judge", "params": {"source": "阈值表"}},
          "downstream": ["retrieve_0"],
          "upstream": ["received_0"],
        },
        ...
      },
      "path": [["received_0"], ["judge_0"], ...],
      "history": [["judge_0", {...}], ...],
      "answer": [],
      "reference": [],
    }

三个字段的分工：

* ``components`` —— **静态拓扑**：有哪些节点、每个节点连着谁；
* ``path`` —— **动态轨迹**：这一轮实际走过了哪几步（源产品同名字段，
  它用来做回路检测与异常回落）；
* ``history`` —— **逐步结果**：每个节点跑出了什么（源产品同名字段，
  它用来在节点异常时定位上下文）。

**节点参数与校验**：每个节点带一份 :class:`NodeParam`，里面有
``required``（必填字段）、``types``（类型）、``validators``（自定义校验）。
``param.check()`` 在**节点跑之前**逐项过一遍 —— 不合法就抛
:class:`NodeParamError` 并**指明节点名与字段名**，链在这一步**即停**，
不带着坏参数继续往下跑。

带着坏参数继续跑，正是把「配置写错了」放大成「静默错判」的典型路径：
阈值表的行号传错了位，出卡照样出，只是出处的行号是错的 —— 事后没人查得出来。

**节点契约**：参数之外，每个节点还带一份 :class:`NodeContract`
（见 ``critval/flow/contract.py``）—— 声明它要从上下文里**拿到**什么
（输入形状）、跑完要**交出**什么（输出形状）、以及这一环失败后**重跑有没有
意义**（可重试性）。契约在装载时挂到节点参数上，``param.check()`` 一并校验，
节点产出也会按契约的输出形状验一遍。

源产品的 ``param.check()`` 只管**参数**；本项目把「输入形状 / 输出形状」
也写进契约 —— 共享上下文是个口袋，上游忘了写、下游读到空值，这种错
在链上要**当场停住**，而不是传到最后一步才炸。

**上游 / 下游必须对称**：``a.downstream`` 里有 ``b``，则 ``b.upstream``
里必须有 ``a``。源产品里这两份关系是各写各的（写错了没人发现），
本项目在 :meth:`FlowDSL.validate` 里把它当**硬约束** —— 拓扑自相矛盾
的链不该跑起来。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Mapping, Optional, Sequence

from critval.flow.contract import (
    WHERE_CONTEXT,
    WHERE_EITHER,
    WHERE_PARAM,
    FieldSpec,
    NodeContract,
    NodeContractError,
    NodeOutputError,
    NodeParamError,
)

__all__ = [
    "DEFAULT_FALLBACK_COMPONENT",
    "DSLValidationError",
    "Component",
    "FieldSpec",
    "FlowDSL",
    "NodeContract",
    "NodeContractError",
    "NodeOutputError",
    "NodeParam",
    "NodeParamError",
    "NodeRegistry",
]


#: **回落出口**的组件名 —— 节点异常时，链要落到这个出口上。
#:
#: 源产品里对应的���「Answer」节点：节点抛异常时，源产品倒序遍历执行路径，
#: 找到 ``Answer`` 节点把异常塞给它再跑一次（见 ``canvas.py`` 的
#: ``run()`` 异常分支）。本产品把「Answer」换成**转人工** ——
#: 机器处理不了的事，交给人，而不是静默吞掉。
DEFAULT_FALLBACK_COMPONENT = "transfer_to_human"


class DSLValidationError(ValueError):
    """DSL 本身不合法（节点重名、走向指向不存在的节点、上下游不对称）。"""


@dataclass
class NodeParam:
    """节点参数 —— 对应源产品 ``param.update()`` + ``param.check()`` 的语义。

    * ``values`` —— 参数值；
    * ``required`` —— 必填字段名（缺一个都不许跑）；
    * ``types`` —— 字段 → 类型（或类型的元组）；
    * ``validators`` —— 字段 → 校验函数。校验函数返回 ``None`` 表示通过，
      返回一段文字表示不通过（那段文字就是 ``reason``）；
    * ``contract`` —— 节点的**契约**（输入形状 / 输出形状 / 可重试性）。
      由注册表在装载节点时从节点对象上取下来挂上（也可以在
      :meth:`FlowDSL.add` 时显式声明）。
    """

    values: dict[str, Any] = field(default_factory=dict)
    required: tuple[str, ...] = ()
    types: dict[str, Any] = field(default_factory=dict)
    validators: dict[str, Callable[[Any], Optional[str]]] = field(default_factory=dict)
    contract: Optional[NodeContract] = None

    # ------------------------------------------------------------------
    # 写入 / 读取
    # ------------------------------------------------------------------
    def update(self, values: Mapping[str, Any]) -> "NodeParam":
        """覆盖式写入参数（源产品同名方法 ``param.update()``）。"""
        for key, value in (values or {}).items():
            self.values[key] = value
        return self

    def get(self, key: str, default: Any = None) -> Any:
        return self.values.get(key, default)

    def __contains__(self, key: str) -> bool:
        return key in self.values

    # ------------------------------------------------------------------
    # 校验
    # ------------------------------------------------------------------
    def missing_fields(self) -> tuple[str, ...]:
        """必填但没填（或填了空值）的字段名。

        ``0`` 与 ``False`` 算**填了** —— 空字符串与 ``None`` 才算没填。
        """
        missing: list[str] = []
        for key in self.required:
            if key not in self.values:
                missing.append(key)
                continue
            value = self.values[key]
            if value is None or (isinstance(value, str) and not value.strip()):
                missing.append(key)
        return tuple(missing)

    def check(self, *, node: str = "", context: Optional[Mapping[str, Any]] = None) -> None:
        """逐项校验参数；不合法就抛 :class:`NodeParamError`。

        顺序是「先必填、再类型、后自定义」—— 必填不过就没必要看类型，
        报错更贴近真正的原因。

        校验完**本地**声明，接着校验**契约**（:attr:`contract`）：

        * 契约要求的输入字段（参数里或上下文里）拿不到 → 抛
          :class:`~critval.flow.contract.NodeContractError`，**指明节点名
          与字段名**；
        * 拿到的值类型不对 / 自带校验器不过 → 同样抛契约错误。

        ``context`` 传 ``None`` 时只校验能在参数层判定的部分（上线前体检
        用这个口径 —— 那时候还没有事件上下文）。
        """
        label = node or "节点"

        missing = self.missing_fields()
        if missing:
            raise NodeParamError(label, missing[0], "必填参数缺失")

        for key, expected in self.types.items():
            if key not in self.values:
                continue
            value = self.values[key]
            if expected is None:
                continue
            if not isinstance(value, expected):
                wanted = (
                    expected.__name__
                    if isinstance(expected, type)
                    else "/".join(t.__name__ for t in expected)
                )
                raise NodeParamError(
                    label,
                    key,
                    f"类型应为 {wanted}，实际为 {type(value).__name__}",
                )

        for key, validator in self.validators.items():
            if key not in self.values:
                continue
            reason = validator(self.values[key])
            if reason:
                raise NodeParamError(label, key, str(reason))

        if self.contract is not None:
            self._check_contract(self.contract, label=label, context=context)

    def _check_contract(
        self,
        contract: NodeContract,
        *,
        label: str,
        context: Optional[Mapping[str, Any]],
    ) -> None:
        """按契约逐项校验输入字段。"""
        for spec in contract.inputs:
            value, source = self._resolve(spec, context)
            if value is _MISSING:
                if spec.where == WHERE_PARAM:
                    raise NodeContractError(label, spec.name, "契约要求的参数缺失")
                if context is None:
                    # 没有上下文可查（上线前体检）—— 这一项跳过，
                    # 不因为「查不到」就报缺参。
                    continue
                raise NodeContractError(
                    label,
                    spec.name,
                    f"契约要求的输入缺失（参数与上下文都没有）{_note(spec)}",
                )
            if not spec.matches(value):
                raise NodeContractError(
                    label,
                    spec.name,
                    f"类型应为 {spec.expected()}，实际为 {type(value).__name__}",
                )
            if spec.validator is not None:
                reason = spec.validator(value)
                if reason:
                    raise NodeContractError(label, spec.name, str(reason))
            del source

    def _resolve(
        self,
        spec: FieldSpec,
        context: Optional[Mapping[str, Any]],
    ) -> tuple[Any, str]:
        """按契约里的字段声明取值 —— 参数优先，其次上下文。

        返回 ``(值, 来源)``；取不到时值是 :data:`_MISSING`。
        """
        if spec.where in (WHERE_PARAM, WHERE_EITHER) and spec.name in self.values:
            return self.values[spec.name], "param"
        if spec.where in (WHERE_CONTEXT, WHERE_EITHER) and context is not None:
            if spec.name in context:
                return context[spec.name], "context"
        return _MISSING, ""

    def check_output(self, *, node: str = "", output: Any = None) -> None:
        """校验节点产出（契约的输出形状）。

        不符就抛 :class:`~critval.flow.contract.NodeOutputError`，
        指明节点名与缺哪个字段。
        """
        if self.contract is None:
            return
        self.contract.check_output(node or "节点", output)

    def contract_summary(self) -> str:
        """契约的人话摘要（体检命令与文档用）。"""
        if self.contract is None:
            return "（无契约）"
        return self.contract.describe()


#: 「取不到」哨兵 —— 区别于「取到了 None」。
_MISSING = object()


def _note(spec: FieldSpec) -> str:
    """字段说明（写进报错文案，让人知道该补什么）。"""
    return f"：{spec.note}" if spec.note else ""


@dataclass
class Component:
    """处置链上的一个节点。

    * ``name`` —— 节点实例名（链内的唯一键，如 ``judge_0``）；
    * ``component_name`` —— 节点**类型**（注册表里的键，如 ``judge``）；
    * ``params`` —— 节点参数；
    * ``downstream`` / ``upstream`` —— 上下游节点实例名；
    * ``obj`` —— 实例化后的节点对象（由 :class:`NodeRegistry` 造）。
    """

    name: str
    component_name: str
    params: NodeParam = field(default_factory=NodeParam)
    downstream: list[str] = field(default_factory=list)
    upstream: list[str] = field(default_factory=list)
    obj: Any = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "component_name": self.component_name,
            "params": dict(self.params.values),
            "downstream": list(self.downstream),
            "upstream": list(self.upstream),
        }


@dataclass
class FlowDSL:
    """一条处置链的声明式描述。"""

    components: dict[str, Component] = field(default_factory=dict)
    #: 每一轮实际走过的步骤（每步是一个节点名列表）。
    path: list[list[str]] = field(default_factory=list)
    #: 每个节点跑出了什么（节点名 → 结果）。
    history: list[tuple[str, Any]] = field(default_factory=list)
    #: 源产品的 ``answer`` 字段 —— 本项目里放「兜底出口跑出来的结果」。
    answer: list[str] = field(default_factory=list)
    #: 引用（出处）清单。
    reference: list[Any] = field(default_factory=list)
    #: 回落出口的组件名。
    fallback: str = DEFAULT_FALLBACK_COMPONENT
    #: **显式入口**节点名（留空则自动推断）。
    #:
    #: 自动推断的口径是「没有上游的那个节点」—— 正常的链都从它起步。
    #: 但**纯回路**的链（``a → b → a``）每个节点都有上游，推断不出入口；
    #: 这种链必须**显式**指明从哪儿起步（源产品靠一个固定的 ``Begin``
    #: 组件解决同一件事）。指错了就在拓扑自检时��错。
    entry_name: str = ""

    # ------------------------------------------------------------------
    # 组装
    # ------------------------------------------------------------------
    def add(
        self,
        name: str,
        component_name: str,
        params: Optional[Mapping[str, Any]] = None,
        *,
        downstream: Sequence[str] = (),
        upstream: Sequence[str] = (),
        required: Sequence[str] = (),
        types: Optional[Mapping[str, Any]] = None,
        validators: Optional[Mapping[str, Callable[[Any], Optional[str]]]] = None,
        contract: Optional[NodeContract] = None,
    ) -> Component:
        """往链上加一个节点。

        ``contract`` 是**显式声明**的节点契约；不给的话，装载节点时由
        :meth:`NodeRegistry.create` 从节点对象上取（内置节点都带契约）。
        """
        if not name:
            raise DSLValidationError("节点名不能为空")
        if name in self.components:
            raise DSLValidationError(f"节点 {name} 重复定义")
        param = NodeParam(
            values=dict(params or {}),
            required=tuple(required),
            types=dict(types or {}),
            validators=dict(validators or {}),
            contract=contract,
        )
        component = Component(
            name=name,
            component_name=component_name,
            params=param,
            downstream=list(downstream),
            upstream=list(upstream),
        )
        self.components[name] = component
        self._mirror_edges()
        return component

    def _mirror_edges(self) -> None:
        """把调用方声明的边**镜像**到对端。

        声明 ``a.downstream = [b]`` 就补上 ``b.upstream += [a]``；声明
        ``b.upstream = [a]`` 就补上 ``a.downstream += [b]``。

        源产品里这两份关系是各写各的（写错了没人发现，跑起来才发现走错
        边）。本项目保留「两份关系都在」的结构，但让**声明即镜像** ——
        用代码搭链的人不用两边都写；而 :meth:`validate` 仍然按反向表逐条
        对账，手改过的 / 从 JSON 载进来的链照样能查出不一致。

        **自环不镜像**：``a.downstream = [a]`` 是一条回路，不是「a 的上游
        是 a」—— 它由回路保护（``FlowLoopError``）去抓，不该在拓扑自检
        阶段就变成「没有入口节点」。
        """
        for comp in self.components.values():
            for target in comp.downstream:
                if target == comp.name:
                    continue
                other = self.components.get(target)
                if other is not None and comp.name not in other.upstream:
                    other.upstream.append(comp.name)
            for source in comp.upstream:
                if source == comp.name:
                    continue
                other = self.components.get(source)
                if other is not None and comp.name not in other.downstream:
                    other.downstream.append(comp.name)

    def node(self, name: str) -> Component:
        """按实例名取节点；取不到就报明确错误（而不是 ``KeyError``）。"""
        try:
            return self.components[name]
        except KeyError:
            raise DSLValidationError(f"处置链里没有节点 {name}") from None

    def entry(self) -> str:
        """入口节点。

        **显式指定**（:attr:`entry_name`）优先 —— 纯回路的链每个节点都有
        上游，只能显式指明从哪儿起步。

        没显式指定时按「**没有上游**的那个」推断；兜底出口
        （``fallback``）不算入口 —— 它是异常时才落上去的出口，不是链的起点。

        有多个候选时报错（拓扑有歧义，跑起来才知道走哪条更糟）。
        """
        if self.entry_name:
            if self.entry_name not in self.components:
                raise DSLValidationError(
                    f"指定的入口节点 {self.entry_name} 不在处置链里"
                )
            return self.entry_name
        entries = [
            comp.name
            for comp in self.components.values()
            if not comp.upstream and comp.component_name != self.fallback
        ]
        if not entries:
            raise DSLValidationError(
                "处置链没有入口节点（所有节点都有上游）——"
                "纯回路的链要用 entry_name 显式指明起点"
            )
        if len(entries) > 1:
            raise DSLValidationError(
                "处置链入口不唯一：" + "、".join(sorted(entries))
            )
        return entries[0]

    def fallback_component(self) -> Optional[Component]:
        """找兜底出口节点（组件名等于 :attr:`fallback` 的那个）。

        优先挑**链上已经走过**的 —— 回落要沿着刚走过的路径倒着找；
        链上没有就退而求其次找链里定义好的那个。
        """
        executed = [name for step in self.path for name in step]
        for name in reversed(executed):
            comp = self.components.get(name)
            if comp is not None and comp.component_name == self.fallback:
                return comp
        for comp in self.components.values():
            if comp.component_name == self.fallback:
                return comp
        return None

    # ------------------------------------------------------------------
    # 拓扑自检
    # ------------------------------------------------------------------
    def validate(self) -> None:
        """拓扑自检 —— **跑之前**就该把矛盾挑出来。

        查三件事：

        1. 上下游指向的节点**存在**；
        2. 上下游**对称**（按 ``downstream`` 反推出的上游表要和声明的
           ``upstream`` 一致）；
        3. 入口**唯一**。
        """
        for comp in self.components.values():
            for target in comp.downstream:
                if target not in self.components:
                    raise DSLValidationError(
                        f"节点 {comp.name} 的下游 {target} 不存在"
                    )
            for source in comp.upstream:
                if source not in self.components:
                    raise DSLValidationError(
                        f"节点 {comp.name} 的上游 {source} 不存在"
                    )

        reverse: dict[str, list[str]] = {name: [] for name in self.components}
        for comp in self.components.values():
            for target in comp.downstream:
                if target == comp.name:
                    continue
                reverse[target].append(comp.name)
        for comp in self.components.values():
            expected = sorted(reverse[comp.name])
            actual = sorted(comp.upstream)
            if expected != actual:
                raise DSLValidationError(
                    f"上下游不对称：节点 {comp.name} 的上游应为 "
                    f"{expected or '（无）'}，实际声明为 {actual or '（无）'}"
                )
        self.entry()

    # ------------------------------------------------------------------
    # 序列化
    # ------------------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "components": {
                name: comp.to_dict() for name, comp in self.components.items()
            },
            "path": [list(step) for step in self.path],
            "history": [[name, out] for name, out in self.history],
            "answer": list(self.answer),
            "reference": list(self.reference),
            "fallback": self.fallback,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2, default=str)

    def executed(self) -> list[str]:
        """这一轮实际跑过的节点（按时间顺序，跨步展开）。"""
        return [name for step in self.path for name in step]


class NodeRegistry:
    """节点注册表 —— 组件名 → 节点工厂。

    同源参考 ``agent/component/__init__.py`` 的 ``component_class(name)``：
    源产品按组件名**动态装载**组件，本项目保留这个语义（节点可插），
    但把「按字符串猜类名」换成**显式注册** —— 拼错组件名在注册时就报错，
    不会等到运行时才炸。
    """

    def __init__(self) -> None:
        self._factories: dict[str, Callable[[str, NodeParam], Any]] = {}

    def register(
        self,
        component_name: str,
        factory: Callable[[str, NodeParam], Any],
    ) -> None:
        """注册一个节点类型。

        ``factory(name, param)`` 返回节点对象；节点对象需要有
        ``run(context)`` 方法（可选 ``set_exception(exc)``）。
        """
        if not component_name:
            raise DSLValidationError("组件名不能为空")
        if component_name in self._factories:
            raise DSLValidationError(f"组件 {component_name} 重复注册")
        self._factories[component_name] = factory

    def create(self, component_name: str, name: str, param: NodeParam) -> Any:
        """按组件名造一个节点实例。

        节点对象若声明了 ``contract()``，装载时把契约挂到参数上 ——
        这样「这个节点对外承诺什么」跟着节点实例走，链上不必重复声明。
        """
        try:
            factory = self._factories[component_name]
        except KeyError:
            known = "、".join(sorted(self._factories)) or "（空）"
            raise DSLValidationError(
                f"未注册的组件 {component_name}；已注册：{known}"
            ) from None
        obj = factory(name, param)
        if param.contract is None:
            contract_fn = getattr(obj, "contract", None)
            if callable(contract_fn):
                contract = contract_fn()
                if isinstance(contract, NodeContract):
                    # 契约里的组件名以注册表为准（阶段节点两个组件名共用一个类）。
                    if contract.name != component_name:
                        contract = replace(contract, name=component_name)
                    param.contract = contract
        return obj

    def has(self, component_name: str) -> bool:
        return component_name in self._factories

    def registered(self) -> tuple[str, ...]:
        return tuple(sorted(self._factories))
