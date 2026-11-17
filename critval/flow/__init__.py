"""处置链层：声明式 DSL、节点契约、回路保护与轨迹回放。

**这一层在产品里的位置**：台账（``ledger``）守着「已生效事实」的边界，
判定（``judge``）与召回（``retrieve``）给出「建议」，处置卡（``card``）
把建议装成人能执行的一页纸 —— 而**处置链（``flow``）**是把这几段串起来的
那条线：谁接谁的活、走到哪一步停、出了岔子往哪儿落。

同源参考 ``github_ref/RAGFlow/agent/canvas.py``：源产品的 ``Canvas``
用一张 DSL 图描述「组件 + 上下游」，``run()`` 沿 ``downstream`` 推进、
``_find_loop()`` 防回路、异常时倒序遍历执行路径把异常交给兜底节点。
本项目把「兜底 = Answer」换成**「兜底 = 转人工」**：机器处理不了的事
交给人，事件不丢、有人接。
"""

from __future__ import annotations

from critval.flow.contract import (
    FieldSpec,
    NodeContract,
    NodeContractError,
    NodeOutputError,
    NodeParamError,
)
from critval.flow.dsl import (
    DEFAULT_FALLBACK_COMPONENT,
    Component,
    DSLValidationError,
    FlowDSL,
    NodeParam,
    NodeRegistry,
)
from critval.flow.nodes import (
    AckNode,
    CardNode,
    FlowNode,
    JudgeNode,
    NotifyNode,
    RetrieveNode,
    StageNode,
    TransferToHumanNode,
    WritebackNode,
    build_default_registry,
    standard_chain,
)
from critval.flow.pipeline import (
    DEFAULT_MAX_LOOPS,
    FlowLoopError,
    FlowRunResult,
    FlowStop,
    Pipeline,
)
from critval.flow.trace import (
    REDACTED,
    STATUS_FALLBACK,
    STATUS_FAILED,
    STATUS_LABELS,
    STATUS_SKIPPED,
    STATUS_STOPPED,
    STATUS_SUCCESS,
    NodeTrace,
    TraceRecorder,
    redact_text,
    render_trace_text,
)

__all__ = [
    "DEFAULT_FALLBACK_COMPONENT",
    "DEFAULT_MAX_LOOPS",
    "REDACTED",
    "STATUS_FALLBACK",
    "STATUS_FAILED",
    "STATUS_LABELS",
    "STATUS_SKIPPED",
    "STATUS_STOPPED",
    "STATUS_SUCCESS",
    "AckNode",
    "CardNode",
    "Component",
    "DSLValidationError",
    "FieldSpec",
    "FlowDSL",
    "FlowLoopError",
    "FlowNode",
    "FlowRunResult",
    "FlowStop",
    "JudgeNode",
    "NodeContract",
    "NodeContractError",
    "NodeOutputError",
    "NodeParam",
    "NodeParamError",
    "NodeRegistry",
    "NodeTrace",
    "NotifyNode",
    "Pipeline",
    "RetrieveNode",
    "StageNode",
    "TraceRecorder",
    "TransferToHumanNode",
    "WritebackNode",
    "build_default_registry",
    "redact_text",
    "render_trace_text",
    "standard_chain",
]
