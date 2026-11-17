"""处置链执行器：沿 ``downstream`` 推进、回路保护、异常回落转人工。

**执行器的三条硬规矩**（每一条都对应一个真实的坏处）：

1. **参数不过即停** —— 节点跑之前先 ``param.check()``。不合格就抛
   :class:`~critval.flow.dsl.NodeParamError`（带节点名与字段名），
   **链在这一步即停**，后面的节点一个都不跑。带着坏参数继续跑 =
   把「配置写错了」放大成「静默错判」。
2. **契约不过即停** —— 节点跑之前按**节点契约**核对该拿的字段
   （输入形状）拿没拿到；跑完再核对**产出形状**（少交字段、类型不对）。
   两次都不通过就停在那一环，**不把半个结果顺着链传下去**。
3. **回路超限抛错** —— 同一条链把同一个节点反复走过（配置写错导致
   ``a → b → a``）时，按 ``max_loops`` 计数，超限就抛
   :class:`FlowLoopError`。不限的话，链会一直绕圈，直到值班人发现
   「这条事件怎么一直在转」。
4. **节点异常回落到「转人工」** —— 链上任何节点抛异常，**不许静默吞掉**：
   倒序沿执行路径找到兜底出口（``transfer_to_human``），把异常交给它，
   同时由执行器把事件状态置为 ``transferred_to_human`` 并写审计。
   机器处理不了的事交给人 —— 这条链的最终出口是**人**，不是沉默。

**同源参考**：``github_ref/RAGFlow/agent/canvas.py`` —— ``Canvas.run()``
沿 ``downstream`` 推进、``_find_loop(max_loops=6)`` 做回路保护、异常分支里
倒序遍历 ``self.path`` 找到 ``Answer`` 节点把异常交给它。本项目把源产品的
「Answer」换成**转人工出口**：源产品里答不出来就打印栈并停下，本项目里
停下之前先把事件交给人，事件不丢、有人接。

**参数校验在跑之前，不在装载时**：源产品在 ``load()`` 里逐节点
``param.check()``（整条链一次性校验）。本项目改成**逐节点、走之前**校验 ——
这样「某个节点缺参数」时，前面已经跑完的环节保留其结果（判定结论、
处置卡都还在台账里），链停在那一步，而不是整条链从头就不跑。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from time import perf_counter
from typing import Any, Mapping, Optional, Sequence

from critval.flow.contract import NodeContractError
from critval.flow.dsl import (
    DEFAULT_FALLBACK_COMPONENT,
    Component,
    FlowDSL,
    NodeParamError,
    NodeRegistry,
)
from critval.flow.trace import (
    DEFAULT_INPUT_KEYS,
    STATUS_FAILED,
    STATUS_FALLBACK,
    STATUS_SKIPPED,
    STATUS_STOPPED,
    STATUS_SUCCESS,
    NodeTrace,
    TraceRecorder,
    summarize_inputs,
    summarize_output,
)
from critval.ledger.states import STATUS_TRANSFERRED_TO_HUMAN

__all__ = [
    "DEFAULT_MAX_LOOPS",
    "FlowLoopError",
    "FlowRunResult",
    "FlowStop",
    "Pipeline",
]

logger = logging.getLogger("critval.flow.pipeline")

#: 同一个节点最多允许被走过多少次（超出即判为回路）。
#:
#: 取 6 与源产品 ``_find_loop(max_loops=6)`` 同口径 —— 处置链里正常的
#: 重入（同一项目多条结果合并）远少于 6 次。
DEFAULT_MAX_LOOPS = 6


class FlowLoopError(OverflowError):
    """回路超限 —— 链在同一节点上绕了太多圈。

    继承 ``OverflowError``：源产品在 ``canvas.py`` 里用的就是
    ``raise OverflowError(f"Too much loops: {loop}")``，本项目保留这个
    异常层级，调用方按 ``OverflowError`` 捕获也不会漏。
    """

    def __init__(self, pattern: str, max_loops: int) -> None:
        self.pattern = pattern
        self.max_loops = max_loops
        super().__init__(f"处置链回路超限（同一节点被走过超过 {max_loops} 次）：{pattern}")


class FlowStop(Exception):
    """**正常**止步 —— 不是错误。

    例：判定结论是「不危急」，后面的出卡 / 推送环节就不该跑了。
    这时链正常结束，事件状态是 ``not_critical``，不是「失败」。
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass
class FlowRunResult:
    """一轮处置链跑完的结果。"""

    #: ``completed`` —— 走到链尾；``stopped`` —— 正常止步（不危急等）；
    #: ``transferred_to_human`` —— 有节点抛异常，已回落转人工。
    status: str = "completed"
    #: 实际跑过的节点（按时间顺序）。
    executed: list[str] = field(default_factory=list)
    #: 每一步跑了哪些节点。
    path: list[list[str]] = field(default_factory=list)
    #: 每个节点跑出了什么。
    history: list[tuple[str, Any]] = field(default_factory=list)
    #: 异常信息（转人工时说明为什么）。
    error: str = ""
    #: 抛异常的节点名。
    failed_node: str = ""
    #: 兜底出口是不是用上了。
    fallback_used: bool = False
    #: 停下那一环**重跑有没有意义**（按节点契约声明）。
    #:
    #: 纯计算环节（判定 / 召回 / 出卡）重跑还是同一个答案 —— 重试没用；
    #: 推送与回写是幂等的 —— 重跑会换结果，值得重试。
    failed_node_retryable: bool = False
    #: 节点级轨迹（每一步的耗时 / 输入摘要 / 输出摘要 / 结论）。
    traces: list[NodeTrace] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """链是不是**正常**跑完的（没转人工）。"""
        return self.status in ("completed", "stopped")

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "ok": self.ok,
            "executed": list(self.executed),
            "path": [list(step) for step in self.path],
            "history": [[name, out] for name, out in self.history],
            "error": self.error,
            "failed_node": self.failed_node,
            "fallback_used": self.fallback_used,
            "failed_node_retryable": self.failed_node_retryable,
            "traces": [trace.to_dict() for trace in self.traces],
        }


class Pipeline:
    """处置链执行器。

    * ``dsl`` —— 链的声明式描述（拓扑 + 节点参数）；
    * ``registry`` —— 节点注册表（组件名 → 节点工厂）；
    * ``store`` —— 台账（可选）。给了就把状态跃迁与审计写进库，
      事件不会因为链上出错而**留在原地没人管**；
    * ``max_loops`` —— 回路保护阈值。
    """

    def __init__(
        self,
        dsl: FlowDSL,
        registry: NodeRegistry,
        *,
        store: Any = None,
        fallback: str = "",
        start: str = "",
        max_loops: int = DEFAULT_MAX_LOOPS,
        actor: str = "flow",
    ) -> None:
        self.dsl = dsl
        self.registry = registry
        self.store = store
        self.fallback = fallback or dsl.fallback or DEFAULT_FALLBACK_COMPONENT
        self.max_loops = max_loops
        self.actor = actor
        self.dsl.fallback = self.fallback
        if start:
            self.dsl.entry_name = start
        #: 节点级轨迹收集器（任务 id 45）：每跑一个节点记一条。
        self.recorder = TraceRecorder()
        #: 本轮处置的事件 ID（轨迹随事件落库要用）。
        self._current_event_id = ""
        self._instantiate()

    # ------------------------------------------------------------------
    # 装载
    # ------------------------------------------------------------------
    def _instantiate(self) -> None:
        """按组件名把每个节点实例化（源产品 ``load()`` 的语义）。

        只实例化，**不**在这里校验参数 —— 参数校验放在跑之前逐节点做。
        """
        for comp in self.dsl.components.values():
            comp.obj = self.registry.create(comp.component_name, comp.name, comp.params)

    def validate_params(self) -> None:
        """预检：整条链的参数逐节点过一遍。

        给运维用的「上线前体检」入口 —— 不想等到事件真的跑上来才发现
        某个节点缺参数。**不**在装载时自动调用。
        """
        for comp in self.dsl.components.values():
            comp.params.check(node=comp.name)

    def contract_report(self) -> list[dict[str, Any]]:
        """节点契约体检表 —— 每个节点承诺的输入 / 产出 / 可重试性。

        给运维看的一张小表：链上有哪些节点、每个节点要什么、交什么、
        失败后重跑有没有意义。
        """
        report: list[dict[str, Any]] = []
        for comp in self.dsl.components.values():
            contract = comp.params.contract
            entry = {
                "node": comp.name,
                "component": comp.component_name,
                "has_contract": contract is not None,
                "retryable": bool(getattr(contract, "retryable", False)),
                "inputs": list(getattr(contract, "input_names", lambda: ())() or ()),
                "outputs": list(getattr(contract, "output_names", lambda: ())() or ()),
                "summary": comp.params.contract_summary(),
            }
            report.append(entry)
        return report

    # ------------------------------------------------------------------
    # 执行
    # ------------------------------------------------------------------
    def run(self, context: Optional[Mapping[str, Any]] = None) -> FlowRunResult:
        """跑一轮处置链。

        ``context`` 是这一轮共享的上下文（事件、判定结论、处置卡、
        当班人……）。节点从这里读输入，也往里写产出 ——
        后面的节点读得到前面节点写的东西。

        跑完（无论跑完、止步、异常回落）都留下一份**节点级轨迹**：
        每一步的耗时、输入摘要、输出摘要与结论。轨迹随事件落库，
        事后用 ``cli trace --event <id>`` 回放（任务 id 45）。
        """
        ctx: dict[str, Any] = dict(context or {})
        if self.store is not None:
            ctx.setdefault("store", self.store)
        self.dsl.validate()

        self.dsl.path = []
        self.dsl.history = []
        self.dsl.answer = []
        self.recorder.reset()
        self._current_event_id = self._event_id(ctx)

        executed: list[str] = []
        try:
            result = self._run_chain(ctx, executed)
        finally:
            # 链停在哪儿、后面的节点为什么没跑 —— 都要在轨迹里说清楚。
            self._finalize_trace(executed)
        result.traces = self.recorder.traces()
        return result

    def _run_chain(self, ctx: dict[str, Any], executed: list[str]) -> FlowRunResult:
        """沿 ``downstream`` 推进一整条链（``run`` 的主体）。"""
        error = ""
        fallback_used = False

        pending = [self.dsl.entry()]
        while pending:
            step: list[str] = []
            self.dsl.path.append(step)
            upcoming: list[str] = []
            for name in pending:
                # 同一步里不重复跑同一个节点（源产品 ``prepare2run`` 的语义）。
                if step and name == step[-1]:
                    continue
                comp = self.dsl.node(name)
                started = perf_counter()
                try:
                    # 参数不过 → 即停（不推进、不吞）；契约不过 → 落转人工。
                    comp.params.check(node=comp.name, context=ctx)
                    output = self._run_node(comp, ctx)
                except FlowStop as stop:
                    step.append(name)
                    executed.append(name)
                    self.dsl.history.append((name, {"stopped": stop.reason}))
                    self._audit(
                        "flow_stopped",
                        event_id=self._event_id(ctx),
                        detail=f"节点 {name} 正常止步：{stop.reason}",
                    )
                    self._record_trace(
                        comp,
                        STATUS_STOPPED,
                        conclusion=f"正常止步：{stop.reason}",
                        elapsed_ms=(perf_counter() - started) * 1000,
                        input_summary=self._input_summary(comp, ctx),
                        error=stop.reason,
                    )
                    return FlowRunResult(
                        status="stopped",
                        executed=executed,
                        path=[list(s) for s in self.dsl.path],
                        history=list(self.dsl.history),
                        error=stop.reason,
                        failed_node=name,
                    )
                except NodeContractError as exc:
                    # 契约不满足（输入拿不到 / 产出形状不对）—— 链停在这一步，
                    # 事件落转人工：机器处理不了的事交给人，事件不丢。
                    # 这一环**没进执行路径** —— 契约是跑之前查的，
                    # 路径里只放真正跑过的节点。
                    error = f"{type(exc).__name__}: {exc}"
                    logger.warning("处置链节点 %s 契约不满足，落转人工：%s", name, exc)
                    self._record_trace(
                        comp,
                        STATUS_FAILED,
                        conclusion="契约不满足，链停在这一步",
                        elapsed_ms=(perf_counter() - started) * 1000,
                        input_summary=self._input_summary(comp, ctx),
                        error=error,
                    )
                    fallback_used = self._abort(
                        exc,
                        ctx,
                        node_name=name,
                        action="flow_contract_violation",
                    )
                    return FlowRunResult(
                        status="transferred_to_human",
                        executed=executed,
                        path=[list(s) for s in self.dsl.path],
                        history=list(self.dsl.history),
                        error=error,
                        failed_node=name,
                        fallback_used=fallback_used,
                        failed_node_retryable=self._retryable(comp),
                    )
                except NodeParamError as exc:
                    # 参数是跑之前查的 —— 节点没跑，不进执行路径。
                    error = str(exc)
                    self._record_trace(
                        comp,
                        STATUS_FAILED,
                        conclusion="参数不合法，链硬停（配置错误要立刻暴露）",
                        elapsed_ms=(perf_counter() - started) * 1000,
                        input_summary=self._input_summary(comp, ctx),
                        error=error,
                    )
                    self._abort(exc, ctx, node_name=name, action="flow_param_invalid")
                    raise
                except Exception as exc:  # noqa: BLE001 —— 任何节点异常都要落转人工
                    step.append(name)
                    error = f"{type(exc).__name__}: {exc}"
                    logger.exception("处置链节点 %s 抛异常，回落转人工", name)
                    self._record_trace(
                        comp,
                        STATUS_FAILED,
                        conclusion="节点异常，已回落转人工",
                        elapsed_ms=(perf_counter() - started) * 1000,
                        input_summary=self._input_summary(comp, ctx),
                        error=error,
                    )
                    fallback_used = self._abort(
                        exc, ctx, node_name=name, action="flow_node_failed"
                    )
                    return FlowRunResult(
                        status="transferred_to_human",
                        executed=executed,
                        path=[list(s) for s in self.dsl.path],
                        history=list(self.dsl.history),
                        error=error,
                        failed_node=name,
                        fallback_used=fallback_used,
                        failed_node_retryable=self._retryable(comp),
                    )

                step.append(name)
                executed.append(name)
                self.dsl.history.append((name, output))
                if isinstance(output, Mapping) and output.get("stop"):
                    reason = str(output.get("reason") or "节点要求止步")
                    self._audit(
                        "flow_stopped",
                        event_id=self._event_id(ctx),
                        detail=f"节点 {name} 正常止步：{reason}",
                    )
                    self._record_trace(
                        comp,
                        STATUS_STOPPED,
                        conclusion=f"正常止步：{reason}",
                        elapsed_ms=(perf_counter() - started) * 1000,
                        input_summary=self._input_summary(comp, ctx),
                        output_summary=summarize_output(output),
                        error=reason,
                    )
                    return FlowRunResult(
                        status="stopped",
                        executed=executed,
                        path=[list(s) for s in self.dsl.path],
                        history=list(self.dsl.history),
                        error=reason,
                        failed_node=name,
                    )
                self._record_trace(
                    comp,
                    STATUS_SUCCESS,
                    conclusion=self._success_conclusion(comp, output),
                    elapsed_ms=(perf_counter() - started) * 1000,
                    input_summary=self._input_summary(comp, ctx),
                    output_summary=summarize_output(output),
                )
                upcoming.extend(comp.downstream)

            # 回路保护：同一个节点被走过太多次 → 抛错，不许无限绕圈。
            loop = self._find_loop()
            if loop:
                exc = FlowLoopError(loop, self.max_loops)
                self._abort(
                    exc,
                    ctx,
                    node_name=step[-1] if step else "",
                    action="flow_loop_aborted",
                )
                raise exc
            pending = upcoming

        return FlowRunResult(
            status="completed",
            executed=executed,
            path=[list(s) for s in self.dsl.path],
            history=list(self.dsl.history),
        )

    def _run_node(self, comp: Component, ctx: dict[str, Any]) -> Any:
        """跑一个节点。

        节点的 ``run(context)`` 拿整份上下文（可读可写），返回自己的产出；
        产出若是带 ``stop`` 的字典，链正常止步。

        产出还会按**节点契约**验一遍输出形状 —— 少交字段、类型不对，
        都在这里当场停住（:class:`~critval.flow.contract.NodeOutputError`），
        不让半个结果顺着链传下去。
        """
        obj = comp.obj
        if obj is None:
            raise RuntimeError(f"节点 {comp.name} 没有实例化")
        runner = getattr(obj, "run", None)
        if runner is None:
            raise RuntimeError(f"节点 {comp.name} 没有 run(context) 方法")
        output = runner(ctx)
        if not (isinstance(output, Mapping) and output.get("stop")):
            comp.params.check_output(node=comp.name, output=output)
        return output

    def _retryable(self, comp: Component) -> bool:
        """这一环失败后重跑有没有意义（按节点契约声明）。"""
        contract = comp.params.contract
        return bool(getattr(contract, "retryable", False))

    # ------------------------------------------------------------------
    # 节点级轨迹
    # ------------------------------------------------------------------
    def _input_summary(self, comp: Component, ctx: Mapping[str, Any]) -> str:
        """这一环读到了什么的摘要（**已脱敏**）。

        摘哪些键由**节点契约**说了算 —— 契约声明了这个节点该拿哪些字段，
        摘要就只摘那些；节点没声明契约就退回默认的一组上下文键。
        """
        contract = comp.params.contract
        if contract is not None:
            keys = list(contract.input_names())
        else:
            keys = list(DEFAULT_INPUT_KEYS)
        if not keys:
            return ""
        # 参数优先（与契约的取值口径一致），上下文补其余的。
        view: dict[str, Any] = dict(ctx)
        view.update(comp.params.values)
        return summarize_inputs(view, keys=keys)

    def _success_conclusion(self, comp: Component, output: Any) -> str:
        """这一环做完了什么（人话结论）。"""
        if isinstance(output, Mapping):
            for key in ("decision", "card_id", "channel", "ok", "acknowledged"):
                if key in output:
                    value = output[key]
                    if key == "decision":
                        return f"判定结论 {value}"
                    if key == "card_id":
                        return f"处置卡 {value} 已装配"
                    if key == "channel":
                        return f"已推送（渠道 {value}）"
                    if key == "acknowledged":
                        return "值班人已确认"
                    if key == "ok":
                        return "回写完成"
            if "stage" in output:
                return f"阶段 {output['stage']}"
        return f"{comp.component_name} 环节完成"

    def _record_trace(
        self,
        comp: Component,
        status: str,
        *,
        conclusion: str,
        elapsed_ms: float,
        input_summary: str = "",
        output_summary: str = "",
        error: str = "",
    ) -> NodeTrace:
        """记一条节点轨迹并**随事件落库**。"""
        trace = self.recorder.record(
            node=comp.name,
            component=comp.component_name,
            status=status,
            conclusion=conclusion,
            elapsed_ms=elapsed_ms,
            input_summary=input_summary,
            output_summary=output_summary,
            error=error,
            retryable=self._retryable(comp),
        )
        self._persist_trace(trace)
        return trace

    def _persist_trace(self, trace: NodeTrace) -> None:
        """把轨迹写进台账（没台账就跳过；写不进去也不能把主流程带崩）。"""
        if self.store is None or not self._current_event_id:
            return
        try:
            self.store.record_flow_trace(self._current_event_id, trace)
        except Exception:  # noqa: BLE001 —— 轨迹写不进去不能把处置链带崩
            logger.exception("写处置链节点轨迹失败")

    def _finalize_trace(self, executed: Sequence[str]) -> None:
        """链跑完后补齐**没跑到的节点** —— 标成「跳过」并说清为什么。

        「事件在哪一步停下」要能一眼看出来：停下的那一环是失败 / 止步，
        它后面的环节统统标成跳过，注明「未执行（链在第 N 步停下了）」。

        **已经记过轨迹的节点不再补一条** —— 失败的那一环本身就有轨迹，
        再补一条「跳过」会让回放里同一个节点出现两次（一次失败、一次跳过），
        反而看不出它到底停在哪一步。
        """
        done = set(executed)
        recorded = {trace.node for trace in self.recorder.traces()}
        stop = self.recorder.stopped_at()
        for comp in self.dsl.components.values():
            if comp.name in done or comp.name in recorded:
                continue
            if stop is not None:
                conclusion = f"未执行（链在第 {stop.seq} 步 {stop.node} 停下了）"
            elif comp.component_name == self.fallback:
                conclusion = "未执行（本轮没有触发回落）"
            else:
                conclusion = "未执行（这一轮没走到这一环）"
            self._record_trace(
                comp,
                STATUS_SKIPPED,
                conclusion=conclusion,
                elapsed_ms=0.0,
            )

    # ------------------------------------------------------------------
    # 回路检测
    # ------------------------------------------------------------------
    def _find_loop(self) -> str:
        """找出执行路径里的回路；没有回路返回空串。

        口径与源产品 ``_find_loop(max_loops=6)`` 对齐：**同一个节点被走过
        超过 ``max_loops`` 次**即判为回路。返回的是回路片段（``a => b => a``），
        便于运维一眼看出是哪两个节点在互相勾连。
        """
        seq = self.dsl.executed()
        if len(seq) < 2:
            return ""
        counts: dict[str, int] = {}
        for name in seq:
            counts[name] = counts.get(name, 0) + 1
        repeated = [name for name, count in counts.items() if count > self.max_loops]
        if not repeated:
            return ""
        fragment = seq[-(self.max_loops + 1):]
        return " => ".join(fragment)

    # ------------------------------------------------------------------
    # 回落转人工
    # ------------------------------------------------------------------
    def _abort(
        self,
        exc: BaseException,
        ctx: Mapping[str, Any],
        *,
        node_name: str,
        action: str,
    ) -> bool:
        """异常落定：写审计 + 置转人工 + 交给兜底出口。

        返回值表示**兜底出口是不是用上了**。
        """
        event_id = self._event_id(ctx)
        retry_hint = ""
        comp = self.dsl.components.get(node_name)
        if comp is not None and comp.params.contract is not None:
            retry_hint = (
                "；这一环可重试（幂等，重跑安全）"
                if comp.params.contract.retryable
                else "；这一环不可重试（重跑还是同一个结果，重试没有意义）"
            )
        detail = (
            f"节点 {node_name or '（未知）'} 异常：{type(exc).__name__}: {exc}"
            f"；已回落转人工，事件留在台账里等人处理{retry_hint}"
        )
        self._audit(action, event_id=event_id, detail=detail)

        # 状态落到转人工 —— 机器处理不了的事交给人，事件不许留在原地。
        self._transfer_to_human(event_id, detail=detail)

        comp = self.dsl.fallback_component()
        if comp is None or comp.obj is None:
            logger.warning("处置链里没有兜底出口节点，转人工只落了状态与审计")
            return False
        if comp.name == node_name:
            # 出问题的就是兜底出口本身 —— 不能再跑一遍（会死循环）。
            return False
        try:
            setter = getattr(comp.obj, "set_exception", None)
            if callable(setter):
                setter(exc)
            # 上下文是**共享**的：兜底出口看到的就是链上跑到这儿的样子。
            if isinstance(ctx, dict):
                ctx["exception"] = exc
            started = perf_counter()
            output = comp.obj.run(ctx)
            self.dsl.answer.append(comp.name)
            self.dsl.history.append((comp.name, output))
            self._record_trace(
                comp,
                STATUS_FALLBACK,
                conclusion="已回落转人工：事件留在台账里等值班人接手",
                elapsed_ms=(perf_counter() - started) * 1000,
                input_summary=self._input_summary(comp, ctx),
                output_summary=summarize_output(output),
                error=f"{type(exc).__name__}: {exc}",
            )
            return True
        except Exception:  # noqa: BLE001 —— 兜底出口自己再炸，也不能把事件丢了
            logger.exception("兜底出口节点 %s 自身异常，转人工只落了状态与审计", comp.name)
            return False

    def _transfer_to_human(self, event_id: str, *, detail: str) -> None:
        """把事件状态置为 ``transferred_to_human``（有台账时才做）。"""
        if self.store is None or not event_id:
            return
        try:
            self.store.set_status(
                event_id,
                STATUS_TRANSFERRED_TO_HUMAN,
                actor=self.actor,
                detail=detail,
            )
        except Exception:  # noqa: BLE001 —— 状态机拒绝跃迁也不能吞掉异常本身
            logger.exception("把事件 %s 置为转人工失败", event_id)

    # ------------------------------------------------------------------
    # 审计
    # ------------------------------------------------------------------
    def _event_id(self, ctx: Mapping[str, Any]) -> str:
        event = ctx.get("event")
        event_id = str(getattr(event, "event_id", "") or "")
        return event_id or str(ctx.get("event_id") or "")

    def _audit(self, action: str, *, event_id: str = "", detail: str = "") -> None:
        """写一条审计流水（没给台账就跳过）。"""
        if self.store is None:
            return
        try:
            self.store.append_audit(
                action,
                event_id=event_id,
                actor=self.actor,
                detail=detail,
            )
        except Exception:  # noqa: BLE001 —— 审计写不进去也不能把主流程带崩
            logger.exception("写审计 %s 失败", action)
