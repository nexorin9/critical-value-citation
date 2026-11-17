"""回写请求体契约（任务 id 43）。

回写是「建议 → 已生效事实」的**唯一通道** —— 台账里的一条回执只有写进
登记系统，才算有人真的处置了。这条通道上多一道校验、少一次覆盖，代价都
落在患者身上，所以**契约要在发出前拦一次**。

**为什么要有这一层**：登记系统那边的回执表窄得可爱 —— 消息控制号、项目
码、结果值、上报时限这几列多半是定长或枚举。发过去一条超长科室名、一个
拼错的状态词，对端可能**截断**、**丢列**、或者**整条拒收**。截断的话，
登记系统里那条记录看着「有」，其实字段是错的 —— 复核时没人说得清。所以
本模块做三件事：

1. **必填 / 类型 / 枚举 / 长度上限** —— 逐字段校验，给出**字段级**原因；
2. **发出前拦截** —— 校验不过就**一个字节都不发**，不拿对端当校验器；
3. **内容指纹** —— 把九个字段归一化后算一个 sha256，供幂等冲突检测比对
   （同一 ``message_control_id`` 但内容不同 → 报冲突，不覆盖既有事实）。

**字段规范**（与 ``critval.writeback.client.RECEIPT_FIELDS`` 一致）：

==================  ======  ==========================================
字段                必填    约束
==================  ======  ==========================================
``message_control_id``  是  幂等键，非空，≤128，单行
``item_code``       是    检验项目码，非空，≤64，单行
``value``           是    观察值，非空，≤128，单行
``threshold_row``   是    阈值依据行定位串，非空，≤512，单行
``notified_dept``   是    已通知科室，非空，≤64，单行
``duty_person``     是    通知对象，非空，≤128，单行
``due_minutes``     是    整数，1..1440（上报时限，分钟）
``receipt_status``  是    枚举：acknowledged / accepted / rejected / voided
``receipt_at``      否    ISO-8601 时刻；为空表示「拿不到原确认时刻」
==================  ======  ==========================================

**``receipt_status`` 的两个口径**：``acknowledged`` 是本产品的院内台账口径
（值班人已确认）；``accepted`` / ``rejected`` / ``voided`` 是登记系统侧的
回执状态词汇 —— 对端自己改状态时用得上。四个都在契约内；**拼错的、
没见过的值会被拦下**，因为登记系统那边多半是枚举列。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Optional

__all__ = [
    "CONTRACT_FIELDS",
    "ContractResult",
    "FAILURE_CODES",
    "FieldSpec",
    "FieldIssue",
    "IDENTITY_FIELDS",
    "MAX_DUE_MINUTES",
    "RECEIPT_STATUS_VALUES",
    "diff_payloads",
    "field_spec",
    "identity_fingerprint",
    "payload_fingerprint",
    "validate_payload",
]


#: 字符串字段里的控制字符（换行、制表符等）会让下游系统解析错列 —— 拦下。
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")

#: 校验失败的原因码（机器可读，便于台账统计与值班台筛选）。
FAILURE_CODES: tuple[str, ...] = (
    "missing",
    "wrong_type",
    "not_in_enum",
    "too_long",
    "out_of_range",
    "bad_format",
    "control_char",
)

#: 上报时限上限（分钟）—— 一天。超过一天的时限说明制度口径写错了。
MAX_DUE_MINUTES = 1440

#: ``receipt_status`` 的合法取值。
RECEIPT_STATUS_VALUES: tuple[str, ...] = (
    "acknowledged",
    "accepted",
    "rejected",
    "voided",
)


@dataclass(frozen=True)
class FieldSpec:
    """一个字段的契约规则。"""

    name: str
    kind: str  # str | int
    label: str
    required: bool = True
    max_length: int = 0
    enum: tuple[str, ...] = ()
    min_value: int = 0
    max_value: int = 0
    iso_datetime: bool = False
    single_line: bool = True

    def describe(self) -> str:
        """这条规则的人话描述（写进校验原因里）。"""
        bits: list[str] = []
        if self.required:
            bits.append("必填")
        if self.kind == "int":
            bits.append(f"整数（{self.min_value}~{self.max_value}）")
        elif self.max_length:
            bits.append(f"≤{self.max_length} 字")
        if self.enum:
            bits.append("取值 " + " / ".join(self.enum))
        if self.iso_datetime:
            bits.append("ISO-8601 时刻")
        return "，".join(bits) or "非空"


#: 九个字段的契约规则（顺序 = 回执字段规范顺序）。
FIELD_SPECS: tuple[FieldSpec, ...] = (
    FieldSpec(
        "message_control_id",
        "str",
        "消息控制号（幂等键）",
        max_length=128,
    ),
    FieldSpec("item_code", "str", "检验项目码", max_length=64),
    FieldSpec("value", "str", "观察值", max_length=128),
    FieldSpec("threshold_row", "str", "阈值依据行", max_length=512),
    FieldSpec("notified_dept", "str", "已通知科室", max_length=64),
    FieldSpec("duty_person", "str", "通知对象（当班人）", max_length=128),
    FieldSpec(
        "due_minutes",
        "int",
        "上报时限（分钟）",
        min_value=1,
        max_value=MAX_DUE_MINUTES,
    ),
    FieldSpec(
        "receipt_status",
        "str",
        "回执状态",
        max_length=32,
        enum=RECEIPT_STATUS_VALUES,
    ),
    FieldSpec(
        "receipt_at",
        "str",
        "回执时刻",
        required=False,
        max_length=64,
        iso_datetime=True,
    ),
)

#: 契约覆盖的字段名（顺序与 :data:`FIELD_SPECS` 一致）。
CONTRACT_FIELDS: tuple[str, ...] = tuple(spec.name for spec in FIELD_SPECS)

_SPEC_INDEX: dict[str, FieldSpec] = {spec.name: spec for spec in FIELD_SPECS}


def field_spec(name: str) -> Optional[FieldSpec]:
    """按字段名取契约规则。"""
    return _SPEC_INDEX.get(str(name or "").strip())


@dataclass(frozen=True)
class FieldIssue:
    """一个字段的校验问题（**字段级**原因，不是笼统一句「载荷不合法」）。"""

    field: str
    code: str
    message: str
    value: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "code": self.code,
            "message": self.message,
            "value": self.value,
        }


@dataclass(frozen=True)
class ContractResult:
    """一次契约校验的结论。"""

    ok: bool
    payload: Mapping[str, Any] = field(default_factory=dict)
    issues: tuple[FieldIssue, ...] = ()

    @property
    def fields(self) -> tuple[str, ...]:
        """有问题的字段名。"""
        return tuple(issue.field for issue in self.issues)

    def summary(self) -> str:
        """人话摘要（写进审计与 CLI 输出）。"""
        if self.ok:
            return "契约校验通过（9 个字段齐备）"
        return "；".join(f"{issue.field}：{issue.message}" for issue in self.issues)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "fields": list(self.fields),
            "issues": [issue.to_dict() for issue in self.issues],
            "summary": self.summary(),
        }


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def validate_payload(payload: Mapping[str, Any]) -> ContractResult:
    """校验回写请求体（**逐字段**给原因）。

    返回的 :class:`ContractResult` 里 ``issues`` 是字段级问题清单；
    ``ok=False`` 时调用方**不应该发请求** —— 先把数据改对。
    """
    data = dict(payload or {})
    issues: list[FieldIssue] = []

    for spec in FIELD_SPECS:
        raw = data.get(spec.name)

        # ---- 必填 ----
        if raw is None or (spec.kind == "str" and not _text(raw)):
            if spec.required:
                issues.append(
                    FieldIssue(
                        field=spec.name,
                        code="missing",
                        message=f"{spec.label} 缺失或为空（{spec.describe()}）",
                        value="",
                    )
                )
            continue

        # ---- 类型 ----
        if spec.kind == "int":
            if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                issues.append(
                    FieldIssue(
                        field=spec.name,
                        code="wrong_type",
                        message=f"{spec.label} 必须是整数（当前 {type(raw).__name__}）",
                        value=_text(raw),
                    )
                )
                continue
            number = int(raw)
            if number < spec.min_value or number > spec.max_value:
                issues.append(
                    FieldIssue(
                        field=spec.name,
                        code="out_of_range",
                        message=(
                            f"{spec.label} 超出允许范围"
                            f"（{spec.min_value}~{spec.max_value}，当前 {number}）"
                        ),
                        value=str(number),
                    )
                )
            continue

        text = _text(raw)
        if spec.enum and text not in spec.enum:
            issues.append(
                FieldIssue(
                    field=spec.name,
                    code="not_in_enum",
                    message=(
                        f"{spec.label} 取值不在契约内"
                        f"（允许 {' / '.join(spec.enum)}，当前 {text}）"
                    ),
                    value=text,
                )
            )
            continue
        if spec.max_length and len(text) > spec.max_length:
            issues.append(
                FieldIssue(
                    field=spec.name,
                    code="too_long",
                    message=(
                        f"{spec.label} 超长（上限 {spec.max_length} 字，"
                        f"当前 {len(text)} 字）"
                    ),
                    value=text[:64],
                )
            )
            continue
        if spec.single_line and _CONTROL_CHARS.search(text):
            issues.append(
                FieldIssue(
                    field=spec.name,
                    code="control_char",
                    message=f"{spec.label} 含控制字符（换行 / 制表符会让下游解析错列）",
                    value=text[:64],
                )
            )
            continue
        if spec.iso_datetime and text:
            try:
                datetime.fromisoformat(text)
            except ValueError:
                issues.append(
                    FieldIssue(
                        field=spec.name,
                        code="bad_format",
                        message=f"{spec.label} 不是 ISO-8601 时刻（当前 {text}）",
                        value=text,
                    )
                )

    return ContractResult(
        ok=not issues,
        payload=data,
        issues=tuple(issues),
    )


def _normalized(payload: Mapping[str, Any]) -> dict[str, Any]:
    """把载荷归一化成可比较的形式。

    * 字符串 → 去空白；
    * ``due_minutes`` → 整数（``10`` 与 ``10.0`` 视为同一件事）；
    * 其余字段 → 原样字符串。
    """
    data = dict(payload or {})
    out: dict[str, Any] = {}
    for spec in FIELD_SPECS:
        raw = data.get(spec.name)
        if spec.kind == "int":
            if isinstance(raw, bool):
                out[spec.name] = str(raw)
            elif isinstance(raw, (int, float)):
                out[spec.name] = int(raw)
            else:
                out[spec.name] = _text(raw)
        else:
            out[spec.name] = _text(raw)
    return out


def payload_fingerprint(payload: Mapping[str, Any]) -> str:
    """载荷内容指纹（sha256 前 16 位）。

    同一 ``message_control_id`` 的两次回写，指纹**相同** = 内容一样
    （重放安全）；指纹**不同** = 内容变了（幂等冲突，不得覆盖既有事实）。
    """
    canonical = json.dumps(
        _normalized(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def diff_payloads(left: Mapping[str, Any], right: Mapping[str, Any]) -> tuple[str, ...]:
    """两个载荷哪些字段不一样（只报契约覆盖的九个字段）。"""
    a = _normalized(left)
    b = _normalized(right)
    return tuple(
        spec.name for spec in FIELD_SPECS if a.get(spec.name) != b.get(spec.name)
    )


#: 「实质内容」字段 —— 判定「是不是同一条危急值处置」时只看这六个。
#:
#: ``receipt_status`` 与 ``receipt_at`` **不算实质内容**：登记系统可能自己
#: 改状态词、自己补受理时刻（本地发的是 ``acknowledged``，对端存成
#: ``accepted`` 是常态）。拿它们比指纹会把「对端正常归一」误判成冲突。
IDENTITY_FIELDS: tuple[str, ...] = (
    "item_code",
    "value",
    "threshold_row",
    "notified_dept",
    "duty_person",
    "due_minutes",
)


def identity_fingerprint(payload: Mapping[str, Any]) -> str:
    """载荷的**实质内容**指纹（只看 :data:`IDENTITY_FIELDS` 六个字段）。

    本地比「同一份载荷」用 :func:`payload_fingerprint`（九个字段全算）；
    跟**登记系统**比用这个 —— 对端归一状态词与时刻是正常的，不该因此
    报冲突。
    """
    normalized = _normalized(payload)
    subset = {name: normalized.get(name) for name in IDENTITY_FIELDS}
    canonical = json.dumps(
        subset, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
