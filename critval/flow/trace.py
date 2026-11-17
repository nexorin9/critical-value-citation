"""处置链节点级轨迹：这条事件**在哪一步停下、为什么**。

**为什么要有这一层**：值班台被问得最多的一句话就是「这条怎么停在这儿」。
没有轨迹时只能翻日志、按时间戳猜；有了轨迹，一行就是答案 —— 第几步、
哪个节点、花了多久、读到了什么、结论是什么、为什么停。

**同源参考** ``github_ref/RAGFlow/agent/canvas.py``：

* ``canvas.py:202`` ``self.path[-1].append(c)`` —— 每跑过一个节点就把节点
  **追加**进当前这一步的轨迹（只增不改）；
* ``canvas.py:178`` ``self.history.append(("assistant", ans.to_dict("records")))``
  —— 每跑过一个节点就把**产出**接进历史；
* ``canvas.py:131-153`` ``Canvas.__str__`` —— 把 ``path`` / ``history`` /
  ``answer`` / ``reference`` **整条链序列化落盘**，事后可回看。

源产品留的是「跑过哪些节点」与「节点吐了什么」两样；本产品在这两样之上
再加三样：**这一步花了多久**、**这一步读到了什么（输入摘要）**、
**这一步的结论与停下的原因** —— 也就是「事件在哪一步停下、为什么」。

**摘要一律脱敏**：轨迹落的是**给值班人看的一句话**，不是原始报文。
摘要只取白名单里的字段与标量；患者标识层字段（姓名 / 身份证 / 住院号 /
病案号 / PID-3 明细）既不进摘要，也不留在库里 —— 轨迹身会随事件长期
留在台账里，是**最容易漏掉患者明细**的地方。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
import unicodedata
from typing import Any, Mapping, Optional, Sequence

__all__ = [
    "MAX_SUMMARY_CHARS",
    "PATIENT_LABELS",
    "REDACTED",
    "STATUS_FAILED",
    "STATUS_FALLBACK",
    "STATUS_LABELS",
    "STATUS_SKIPPED",
    "STATUS_STOPPED",
    "STATUS_SUCCESS",
    "NodeTrace",
    "TraceRecorder",
    "forbidden_key",
    "redact_text",
    "render_trace_text",
    "summarize_context_value",
    "summarize_output",
]


# ---------------------------------------------------------------------------
# 状态
# ---------------------------------------------------------------------------
#: 这一环跑完了，产出正常。
STATUS_SUCCESS = "success"
#: 这一环**没跑** —— 链在上一环就停下了，它只是排在后面。
STATUS_SKIPPED = "skipped"
#: 这一环**正常止步** —— 不是错误（不危急、判定不成立要转人工）。
STATUS_STOPPED = "stopped"
#: 这一环**失败** —— 抛了异常或契约不满足。
STATUS_FAILED = "failed"
#: 这一环是**回落转人工**出口。
STATUS_FALLBACK = "fallback"

#: 状态 → 人话标签（回放输出与值班台用同一份措辞）。
STATUS_LABELS: dict[str, str] = {
    STATUS_SUCCESS: "成功",
    STATUS_SKIPPED: "跳过",
    STATUS_STOPPED: "止步",
    STATUS_FAILED: "失败",
    STATUS_FALLBACK: "回落转人工",
}


# ---------------------------------------------------------------------------
# 脱敏
# ---------------------------------------------------------------------------
#: 脱敏后写进摘要的占位文字。
REDACTED = "（已脱敏）"

#: 患者标识层字段名 —— 出现即**不进摘要**（与卡片渲染同一份口径）。
FORBIDDEN_KEYS: frozenset[str] = frozenset(
    {
        "patient_ref",
        "patient_name",
        "patient_id",
        "name",
        "id_card",
        "id_no",
        "id_number",
        "admission_no",
        "inpatient_no",
        "medical_record_no",
        "姓名",
        "身份证",
        "住院号",
        "病案号",
        "门诊号",
    }
)

#: 患者可识别明细的标签 —— 文本里出现即**整段脱敏**。
#:
#: 整段而不是逐个替换：原文里「姓名 张三」后面还跟着别的字段，
#: 局部替换容易漏下一个；整段丢反而更安全 —— 摘要少一句话不要紧，
#: 留一份患者明细在轨迹里才是事故。
PATIENT_LABELS: tuple[str, ...] = (
    "姓名",
    "身份证",
    "住院号",
    "病案号",
    "门诊号",
    "住院流水号",
    "PID-3",
    "PID3",
    "MRN",
)

#: 单个值的摘要上限（字符）。
MAX_VALUE_CHARS = 60

#: 整条输入 / 输出摘要的上限（字符）。
MAX_SUMMARY_CHARS = 200

#: 耗时最小记到多少毫秒 —— 快节点被四舍五入成 0 就看不出「跑过」。
MIN_ELAPSED_MS = 0.001


def forbidden_key(key: Any) -> bool:
    """这个字段名是不是患者标识层字段。"""
    return str(key or "").strip().lower() in FORBIDDEN_KEYS


def redact_text(text: Any) -> str:
    """含患者可识别明细的文本整段脱敏；其余原样返回。"""
    raw = "" if text is None else str(text)
    if not raw:
        return ""
    for label in PATIENT_LABELS:
        if label in raw:
            return REDACTED
    return raw


def _truncate(text: str, limit: int = MAX_VALUE_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


def _scalar(value: Any) -> bool:
    return value is None or isinstance(value, (str, int, float, bool, Decimal))


# ---------------------------------------------------------------------------
# 摘要
# ---------------------------------------------------------------------------
def _pairs(mapping: Mapping[str, Any], *, limit: int = MAX_VALUE_CHARS) -> str:
    """把若干「标签 → 值」拼成一句摘要；空值直接不写。"""
    parts: list[str] = []
    for label, value in mapping.items():
        if value is None:
            continue
        text = redact_text(value)
        if not text:
            continue
        parts.append(f"{label}={_truncate(text, limit)}")
    return " · ".join(parts)


def summarize_mapping(mapping: Mapping[str, Any]) -> str:
    """通用映射摘要 —— 只取标量，禁掉患者标识层字段，其余脱敏后截断。"""
    parts: list[str] = []
    for key, value in mapping.items():
        if forbidden_key(key):
            continue
        if not _scalar(value):
            continue
        text = redact_text(value)
        if not text:
            continue
        parts.append(f"{key}={_truncate(text)}")
    return " · ".join(parts)


def summarize_event(event: Any) -> str:
    """事件摘要 —— 只取业务字段，**不取** ``patient_ref``。"""
    if event is None:
        return ""
    return _pairs(
        {
            "事件": getattr(event, "event_id", ""),
            "项目": getattr(event, "item_code", ""),
            "结果": getattr(event, "value", ""),
            "单位": getattr(event, "unit", ""),
            "科室": getattr(event, "dept_code", ""),
            "时刻": getattr(event, "observed_at", ""),
        }
    )


def summarize_judgement(judgement: Any) -> str:
    """判定结论摘要。"""
    if judgement is None:
        return ""
    return _pairs(
        {
            "结论": getattr(judgement, "decision", ""),
            "项目": getattr(judgement, "item_code", ""),
            "规则": getattr(judgement, "rule_id", ""),
            "理由": getattr(judgement, "reason", ""),
        }
    )


def summarize_card(card: Any) -> str:
    """处置卡摘要 —— 卡号与缺项数就够回答「出卡了没有」。"""
    if card is None:
        return ""
    try:
        missing = len(card.missing_elements())
    except Exception:  # noqa: BLE001 —— 摘要不该把主流程带崩
        missing = 0
    return _pairs(
        {
            "处置卡": getattr(card, "card_id", ""),
            "缺项": missing,
        }
    )


def summarize_clause(clause: Any) -> str:
    """制度条款摘要。"""
    if clause is None:
        return ""
    return _pairs(
        {
            "条款": getattr(clause, "clause_no", "") or getattr(clause, "section_title", ""),
            "通知对象": getattr(clause, "notify_target", ""),
            "时限": getattr(clause, "deadline_text", ""),
            "记录去向": getattr(clause, "record_destination", ""),
        }
    )


def summarize_duty_person(person: Any) -> str:
    """当班人摘要 —— 只取姓名与角色（值班人是谁是回放要答的问题）。"""
    if person is None:
        return ""
    if isinstance(person, str):
        return redact_text(person)
    return _pairs(
        {
            "当班": getattr(person, "name", ""),
            "角色": getattr(person, "role", ""),
        }
    )


def summarize_context_value(key: str, value: Any) -> str:
    """按上下文键的语义给出摘要。

    认识的键按业务含义摘；不认识的键若是映射/标量就按通用口径摘，
    若是别的对象就只留类型名（**不猜**对象里有什么）。
    """
    if key == "event":
        return summarize_event(value)
    if key == "judgement":
        return summarize_judgement(value)
    if key == "card":
        return summarize_card(value)
    if key == "policy_clause":
        return summarize_clause(value)
    if key == "duty_person":
        return summarize_duty_person(value)
    if key == "citations":
        try:
            return f"{len(value)} 条出处"
        except TypeError:
            return ""
    if key == "rows":
        try:
            return f"{len(value)} 条阈值行"
        except TypeError:
            return ""
    if isinstance(value, Mapping):
        return summarize_mapping(value)
    if _scalar(value):
        return _truncate(redact_text(value))
    if isinstance(value, (list, tuple)):
        return f"{len(value)} 项"
    return type(value).__name__


def summarize_output(output: Any) -> str:
    """节点产出摘要。"""
    if output is None:
        return ""
    if isinstance(output, Mapping):
        return summarize_mapping(output)
    if _scalar(output):
        return _truncate(redact_text(output))
    return type(output).__name__


def summarize_inputs(context: Mapping[str, Any], *, keys: Optional[Sequence[str]] = None) -> str:
    """输入摘要 —— 按给定的键顺序摘，拼成一句。

    ``keys`` 给了就只摘这些键（节点契约的输入字段）；没给就用默认的
    一组上下文键。
    """
    ctx = context or {}
    parts: list[str] = []
    for key in keys or ():
        if key not in ctx:
            continue
        text = summarize_context_value(key, ctx[key])
        if text:
            parts.append(text)
    summary = " · ".join(parts)
    return summary[:MAX_SUMMARY_CHARS]


#: 节点没有契约时，摘要默认看这几个上下文键。
DEFAULT_INPUT_KEYS: tuple[str, ...] = (
    "event",
    "judgement",
    "card",
    "policy_clause",
    "citations",
    "duty_person",
    "notify_target",
    "due_minutes",
)


# ---------------------------------------------------------------------------
# 轨迹
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class NodeTrace:
    """一个节点的执行轨迹。

    * ``seq`` —— 第几步（从 1 起）；
    * ``node`` —— 节点实例名；
    * ``component`` —— 组件名（环节名）；
    * ``status`` —— 这一环的收场（见 :data:`STATUS_SUCCESS` 等常量）；
    * ``conclusion`` —— 人话结论（做了什么 / 为什么停下）；
    * ``elapsed_ms`` —— 这一环花了多久（毫秒）；
    * ``input_summary`` / ``output_summary`` —— 输入 / 产出摘要（**已脱敏**）；
    * ``error`` —— 失败时的报错文案；
    * ``retryable`` —— 这一环失败后**重跑有没有意义**（按节点契约声明）；
    * ``at`` —— 记录时刻（ISO）。
    """

    seq: int
    node: str
    component: str
    status: str
    conclusion: str = ""
    elapsed_ms: float = 0.0
    input_summary: str = ""
    output_summary: str = ""
    error: str = ""
    retryable: bool = False
    at: str = ""

    @property
    def status_label(self) -> str:
        return STATUS_LABELS.get(self.status, self.status)

    @property
    def ok(self) -> bool:
        """这一环是不是正常收场（成功 / 正常止步）。"""
        return self.status in (STATUS_SUCCESS, STATUS_STOPPED)

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "node": self.node,
            "component": self.component,
            "status": self.status,
            "status_label": self.status_label,
            "conclusion": self.conclusion,
            "elapsed_ms": self.elapsed_ms,
            "input_summary": self.input_summary,
            "output_summary": self.output_summary,
            "error": self.error,
            "retryable": self.retryable,
            "at": self.at,
        }


class TraceRecorder:
    """一条事件的节点级轨迹收集器。

    用法：处置链每跑一个节点就 ``record()`` 一次；跑完后
    :meth:`traces` 就是这条事件的完整处置链路。
    """

    def __init__(self) -> None:
        self._traces: list[NodeTrace] = []

    # ------------------------------------------------------------------
    # 收集
    # ------------------------------------------------------------------
    def reset(self) -> None:
        """清空（每轮处置重新记）。"""
        self._traces = []

    def record(
        self,
        *,
        node: str,
        component: str = "",
        status: str = STATUS_SUCCESS,
        conclusion: str = "",
        elapsed_ms: float = 0.0,
        input_summary: str = "",
        output_summary: str = "",
        error: str = "",
        retryable: bool = False,
        at: str = "",
    ) -> NodeTrace:
        """记一条节点轨迹，返回它。"""
        trace = NodeTrace(
            seq=len(self._traces) + 1,
            node=node,
            component=component or node,
            status=status,
            conclusion=conclusion,
            elapsed_ms=max(round(float(elapsed_ms), 3), MIN_ELAPSED_MS),
            input_summary=input_summary[:MAX_SUMMARY_CHARS],
            output_summary=output_summary[:MAX_SUMMARY_CHARS],
            error=error,
            retryable=bool(retryable),
            at=at or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        self._traces.append(trace)
        return trace

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def traces(self) -> list[NodeTrace]:
        """按顺序的轨迹列表。"""
        return list(self._traces)

    def stopped_at(self) -> Optional[NodeTrace]:
        """第一个**没正常收场**的节点 —— 「事件停在哪一步」。"""
        for trace in self._traces:
            if trace.status in (STATUS_FAILED, STATUS_STOPPED):
                return trace
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "traces": [trace.to_dict() for trace in self._traces],
            "stopped_at": (self.stopped_at().node if self.stopped_at() else ""),
        }


# ---------------------------------------------------------------------------
# 回放
# ---------------------------------------------------------------------------
def _display_width(text: str) -> int:
    """文本的**显示宽度** —— 中日韩字符占两列。

    按 ``len()`` 算宽度会让中文列错位：终端里一个汉字两列，``len()``
    只算一个字符，表格就会参差不齐。
    """
    width = 0
    for char in str(text or ""):
        width += 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
    return width


def _pad(text: str, width: int) -> str:
    """按显示宽度左对齐补空格。"""
    raw = str(text or "")
    gap = width - _display_width(raw)
    return raw + (" " * gap if gap > 0 else "")


def _as_dict(item: Any) -> dict[str, Any]:
    """把 :class:`NodeTrace` 或台账行统一成字典。"""
    if isinstance(item, NodeTrace):
        return item.to_dict()
    if isinstance(item, Mapping):
        return dict(item)
    return {
        "seq": getattr(item, "seq", 0),
        "node": getattr(item, "node", ""),
        "component": getattr(item, "component", ""),
        "status": getattr(item, "status", ""),
        "conclusion": getattr(item, "conclusion", ""),
        "elapsed_ms": getattr(item, "elapsed_ms", 0.0),
        "input_summary": getattr(item, "input_summary", ""),
        "output_summary": getattr(item, "output_summary", ""),
        "error": getattr(item, "error", ""),
        "retryable": getattr(item, "retryable", False),
        "at": getattr(item, "at", ""),
    }


def render_trace_text(
    traces: Sequence[Any],
    *,
    event_id: str = "",
    status: str = "",
    retryable: Optional[bool] = None,
) -> str:
    """把轨迹渲染成人能读的**回放**文本。

    输出要能回答四件事：**走过哪些环节**、**停在哪一步**、
    **为什么停**、**这一环重跑有没有意义**。
    """
    rows = [_as_dict(item) for item in traces]
    lines: list[str] = []
    header = f"事件 {event_id or '（未指定）'} · 处置链轨迹（节点级）"
    if status:
        header += f" · 当前状态 {status}"
    lines.append(header)
    lines.append("─" * 68)

    for row in rows:
        seq = int(row.get("seq") or 0)
        node = str(row.get("node") or "")
        label = STATUS_LABELS.get(str(row.get("status") or ""), str(row.get("status") or ""))
        elapsed = float(row.get("elapsed_ms") or 0.0)
        elapsed_text = "—" if row.get("status") == STATUS_SKIPPED else f"{elapsed:.1f}ms"
        lines.append(
            f" {seq:>2}. {_pad(node, 20)}{_pad(label, 8)}{elapsed_text:>9}"
        )
        conclusion = str(row.get("conclusion") or "")
        if conclusion:
            lines.append(f"      结论 · {conclusion}")
        if row.get("input_summary"):
            lines.append(f"      输入 · {row['input_summary']}")
        if row.get("output_summary"):
            lines.append(f"      产出 · {row['output_summary']}")
        if row.get("error"):
            lines.append(f"      报错 · {row['error']}")

    if not rows:
        lines.append("（没有轨迹记录）")

    lines.append("─" * 68)

    stop = None
    for row in rows:
        if str(row.get("status")) in (STATUS_FAILED, STATUS_STOPPED):
            stop = row
            break

    if stop is not None:
        lines.append(f"停在哪一步：{stop.get('node')}（第 {stop.get('seq')} 步）")
        reason = str(stop.get("error") or stop.get("conclusion") or "")
        lines.append(f"为什么      ：{reason or '（未记录原因）'}")
        # 「能不能重跑」优先用这一环自己记的可重试性；没有就退回调用方给的。
        step_retryable = stop.get("retryable")
        if step_retryable is None:
            step_retryable = retryable
        if step_retryable is not None:
            step_retryable = bool(step_retryable)
        if step_retryable:
            lines.append("这一环能不能重跑：可以（幂等，重跑安全）")
        elif step_retryable is False:
            lines.append("这一环能不能重跑：不能（重跑还是同一个结果）")
    else:
        lines.append("停在哪一步：没有停在半路（整条链走完了）")

    fallback = next(
        (row for row in rows if str(row.get("status")) == STATUS_FALLBACK),
        None,
    )
    if fallback is not None:
        lines.append("兜底出口    ：已回落转人工（事件留在台账里等人接）")

    return "\n".join(lines)
