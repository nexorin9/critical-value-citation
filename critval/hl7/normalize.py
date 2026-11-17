"""``ORU^R01`` → 规范化危急值事件（厂商方言容错）。

**最小必需字段集**（缺任一 → 落 ``unparsed`` 记录 + 写审计，**不静默丢弃**）：

===========================  ==========================================
字段                          缺了会怎样
===========================  ==========================================
``MSH-10`` 消息控制号          整条消息无法定幂等键 → 整条落 unparsed
``OBR-3`` 标本号              无法把结果绑到标本 → 该条落 unparsed
``OBX-3`` 检验项目码           无法映射阈值表 → 该条落 unparsed
``OBX-5`` 观察值              没有判定入参 → 该条落 unparsed
===========================  ==========================================

``OBX-14`` 观察时刻缺失时回落消息时间 ``MSH-7``，并在事件 notes 里写明
「回落自消息时间」；两者都缺才是不可用。

**字段取哪里由方言兼容矩阵决定**：本模块**不再自己取字段**，而是调用
``critval/hl7/dialects.py`` 的 ``parse_oru_r01``（任务 id 23）—— 字段映射
（``LIS_FIELD_MAP``）、``OBR-4`` 兜底、厂商私有 ``Z`` 段、日期格式集、空值
语义全部收口在那里，院内换厂商改一份 JSON 即可。本模块只做「结构 → 事件
模型」的语义活：最小必需字段校验、幂等键、事件构造。

**患者标识**：`PID-3`` 只进事件标识层，落库存的是**不可逆短哈希**
（``pt-<12 位>``），不落住院号 / 身份证明文 —— 事件之间要能关联到同一
患者，但台账里不留可识别明细。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Mapping, Optional

from critval.hl7.dialects import (
    DEFAULT_DIALECT,
    Dialect,
    parse_oru_r01 as parse_oru_r01_with_dialect,
)
from critval.hl7.er7 import parse_numeric
from critval.hl7.primitives import decode_frame, patient_reference
from critval.models import CriticalEvent

__all__ = [
    "HL7_REQUIRED_FIELDS",
    "HTTP_REQUIRED_FIELDS",
    "NormalizedFields",
    "Observation",
    "OruMessage",
    "PATIENT_IDENTIFIER_FIELDS",
    "UnparsedRecord",
    "decode_frame",
    "normalize_events",
    "normalize_fields",
    "normalize_http_payload",
    "parse_oru_r01",
    "parse_timestamp",
    "patient_reference",
]


@dataclass(frozen=True)
class Observation:
    """一条 OBX（一次检验结果）。"""

    item_code: str
    item_name: str
    value_raw: str
    unit: str
    reference_range: str
    abnormal_flag: str
    result_status: str
    observed_at_raw: str
    observed_at: Optional[datetime]
    set_id: str = ""
    notes: tuple[str, ...] = ()
    #: 项目码取自哪个字段（``OBX-3.1`` / ``OBR-4.1`` / ``ZLR-2`` …）。
    item_code_source: str = ""
    #: 项目码置信度：``high`` / ``medium`` / ``low``。``low`` 表示是兜底
    #: 匹配来的，值班人需要人工核对。
    item_code_confidence: str = "high"
    #: 项目码的文本候选（厂商只给了项目名没给项目码时的兜底素材）。
    item_code_text: str = ""
    item_code_text_source: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "item_code": self.item_code,
            "item_name": self.item_name,
            "value_raw": self.value_raw,
            "unit": self.unit,
            "reference_range": self.reference_range,
            "abnormal_flag": self.abnormal_flag,
            "result_status": self.result_status,
            "observed_at_raw": self.observed_at_raw,
            "set_id": self.set_id,
            "item_code_source": self.item_code_source,
            "item_code_confidence": self.item_code_confidence,
            "item_code_text": self.item_code_text,
            "item_code_text_source": self.item_code_text_source,
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class UnparsedRecord:
    """**没能规范化**的一条报文（或其中一条结果）。

    缺字段的报文**不丢弃**：落一条记录 + 写审计，值班台能看到「今天有
    几条 LIS 报文没读懂、缺了什么」。
    """

    reason: str
    detail: str
    message_control_id: str = ""
    specimen_no: str = ""
    segment_name: str = ""
    raw: str = ""
    #: 解析这条报文时用的方言名（排查「换个厂商就丢了」用）。
    dialect: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "reason": self.reason,
            "detail": self.detail,
            "message_control_id": self.message_control_id,
            "specimen_no": self.specimen_no,
            "segment_name": self.segment_name,
            "dialect": self.dialect,
        }


@dataclass(frozen=True)
class OruMessage:
    """一条 ``ORU^R01`` 消息解析后的结构。"""

    message_type: str
    message_control_id: str
    message_datetime: Optional[datetime]
    specimen_no: str
    dept_code: str
    patient_ref: str
    result_status: str
    observations: tuple[Observation, ...] = ()
    notes: tuple[str, ...] = ()
    #: 标本号取自哪个字段（``OBR-3.1`` / ``SPM-2.1`` …）。
    specimen_no_source: str = ""
    #: 标本号置信度：``high`` / ``medium`` / ``low``。
    specimen_no_confidence: str = "high"
    #: 解析这条报文时用的方言名。
    dialect: str = "default"

    def to_dict(self) -> dict[str, object]:
        return {
            "message_type": self.message_type,
            "message_control_id": self.message_control_id,
            "specimen_no": self.specimen_no,
            "specimen_no_source": self.specimen_no_source,
            "specimen_no_confidence": self.specimen_no_confidence,
            "dept_code": self.dept_code,
            "patient_ref": self.patient_ref,
            "result_status": self.result_status,
            "dialect": self.dialect,
            "observations": [obs.to_dict() for obs in self.observations],
            "notes": list(self.notes),
        }


# ---------------------------------------------------------------------------
# 两条入站通道共用的规范层（任务 id 24）
# ---------------------------------------------------------------------------
#
# 院内 LIS 推危急值有两条路：一条是 HL7 v2 ``ORU^R01`` 走 MLLP（主通道），
# 一条是直接 HTTP 推送（辅通道）。两条路推的是同一件事 —— 「某标本某项目的
# 某一次结果」—— 所以**最小必需字段校验、幂等键拼法、患者标识处理、事件构造
# 必须只有一份实现**。各写一套的结果是：改一条规则要改两处，改漏一处就出现
# 「同一条结果走 MLLP 判危急、走 HTTP 判不危急」。
#
# 通道差异只体现在两处：**传进来什么字段**、**哪几个字段算必需**。

#: HL7 通道的最小必需字段集（与 spec.md 一致）。
HL7_REQUIRED_FIELDS: frozenset[str] = frozenset(
    {"message_control_id", "specimen_no", "item_code", "value"}
)

#: HTTP 推送通道的最小必需字段集。
#:
#: 与 HL7 的差别只有一处：``specimen_no`` **不是必填** —— LIS 走 HTTP 推送时
#: 常常不带标本号，标本绑定由院内台账另查；其余四项（事件号 / 项目码 / 观察值 /
#: 结果时刻）缺一不可。
HTTP_REQUIRED_FIELDS: frozenset[str] = frozenset(
    {"message_control_id", "item_code", "value", "observed_at"}
)

#: 载荷里出现这些字段名 → 明确拒绝。
#:
#: HTTP 推送通道**不承载患者标识**。厂商对接时把患者信息顺手塞进来的情形很常见，
#: 与其默默丢字段让人以为「系统收了但没存」，不如当场拒绝并说明 —— 这条通道的
#: 边界是「只传结果，不传人」。
PATIENT_IDENTIFIER_FIELDS: frozenset[str] = frozenset(
    {
        "patient_id",
        "patient_name",
        "patient_ref",
        "patient",
        "name",
        "id_card",
        "id_number",
        "idcard",
        "identity_no",
        "inpatient_no",
        "inpatient_number",
        "hospital_no",
        "mrn",
        "住院号",
        "姓名",
        "身份证",
        "病案号",
    }
)

#: 缺字段时给人看的文案：(字段标签, 后半句)。按通道分 —— 两条通道的字段名
#: 不一样，值班人对着报错要能一眼看懂是哪个字段没给。
_MISSING_TEXT: dict[str, dict[str, tuple[str, str]]] = {
    "hl7": {
        "message_control_id": ("MSH-10 消息控制号", "缺失，整条报文无法定位幂等键"),
        "item_code": ("OBX-3 检验项目码", "缺失，无法映射院内阈值表"),
        "specimen_no": ("OBR-3 标本号", "缺失，无法把结果绑定到标本"),
        "value": ("OBX-5 观察值", "缺失（项目 {item_code}）"),
        "observed_at": ("OBX-14 观察时刻", "缺失且 MSH-7 消息时间不可用（项目 {item_code}）"),
    },
    "http": {
        "message_control_id": ("event_id 事件号", "缺失，无法定位事件幂等键"),
        "item_code": ("item_code 检验项目码", "缺失，无法映射院内阈值表"),
        "specimen_no": ("specimen_no 标本号", "缺失（该字段在本通道非必填）"),
        "value": ("value 观察值", "缺失（项目 {item_code}）"),
        "observed_at": ("raised_at 结果产生时刻", "缺失，无法定位这一次结果"),
    },
}

#: HTTP 通道接受的结果时刻写法。
_HTTP_TIME_FORMATS: tuple[str, ...] = (
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
    "%Y%m%d%H%M%S",
    "%Y%m%d%H%M",
    "%Y%m%d",
)

#: 末尾的时区偏移（``+08:00`` / ``+0800`` / ``Z``）。
_TZ_SUFFIX_RE = re.compile(r"^(?P<stamp>.+?)(?P<offset>[+-]\d{2}:?\d{2}|Z)$")


@dataclass(frozen=True)
class NormalizedFields:
    """两条入站通道共用的一组规范字段（结构 → 事件语义层的输入）。

    ``channel`` 决定缺字段时用哪套文案（``hl7`` / ``http``）；
    ``source_channel`` 是写进事件与台账的通道名。
    """

    message_control_id: str = ""
    specimen_no: str = ""
    item_code: str = ""
    item_name: str = ""
    value_raw: str = ""
    unit: str = ""
    observed_at: Optional[datetime] = None
    dept_code: str = ""
    obx_flag: str = ""
    result_status: str = ""
    patient_ref: str = ""
    source_channel: str = "hl7"
    item_code_source: str = ""
    item_code_confidence: str = "high"
    #: 项目码的文本候选：厂商给的是项目名而不是项目码时，留给文本兜底匹配用。
    item_code_text: str = ""
    item_code_text_source: str = ""
    channel: str = "hl7"
    dialect: str = ""


def parse_timestamp(raw: object) -> Optional[datetime]:
    """解析一条结果时刻，**取墙钟读数**。

    带时区偏移的时刻一律去掉偏移取本地墙钟 —— 与 HL7 路径「时刻口径统一」
    同理：一条推送里一部分带偏移一部分不带，按生效期比较时会抛 ``TypeError``。

    解析不出来返回 ``None``（不猜、不用当前时间顶替）。
    """
    text = str(raw or "").strip()
    if not text:
        return None
    normalized = text.replace("/", "-")
    match = _TZ_SUFFIX_RE.match(normalized)
    if match:
        normalized = match.group("stamp")
    for fmt in _HTTP_TIME_FORMATS:
        try:
            return datetime.strptime(normalized, fmt)
        except ValueError:
            continue
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed


def _value_text(value: object) -> str:
    """把载荷里的观察值转成文本（JSON 数字不能按 Python 的 ``repr`` 处理）。"""
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (int, float, Decimal)):
        return format(Decimal(str(value)), "f")
    return str(value).strip()


def normalize_fields(
    fields: NormalizedFields,
    *,
    required: frozenset[str] = HL7_REQUIRED_FIELDS,
    item_matcher: Optional[Any] = None,
) -> tuple[Optional[CriticalEvent], Optional[UnparsedRecord]]:
    """规范字段 → （事件, 未识别记录）。

    **两条入站通道共用这一套规范函数**：最小必需字段校验、观察值解析、
    幂等键拼法、患者标识处理、事件构造都在这里。返回 ``(None, record)``
    表示这条结果读不懂 —— 调用方**必须**把 ``record`` 落库并写审计，
    一条都不静默丢弃。

    ``item_matcher`` 是**文本兜底**用的项目匹配器（``critval.hl7.textmatch``
    的 ``ItemTextMatcher``）：厂商只给了项目名没给项目码时，拿这个名字去
    阈值表的项目名里匹配 —— 匹配上则项目码落成表内的码、置信度 ``low``；
    匹配不上（或没给匹配器）则保持原样，仍按「缺项目码」处理。

    校验顺序（HL7 与 HTTP 一致，报错才长得一样）：

    1. 消息控制号 / 事件号 → 2. 项目码（含文本兜底）→ 3. 标本号（仅当该通道
    必填）→ 4. 观察值缺失 → 5. 观察值非数值 → 6. 结果时刻
    """
    from critval.ledger.idempotency import build_idempotency_key, event_id_for_key

    texts = _MISSING_TEXT.get(fields.channel, _MISSING_TEXT["hl7"])

    def _unparsed(
        reason: str,
        label: str,
        suffix: str,
        segment: str,
        **extra: str,
    ) -> UnparsedRecord:
        return UnparsedRecord(
            reason=reason,
            detail=f"{label}{suffix.format(item_code=fields.item_code, **extra)}",
            message_control_id=fields.message_control_id,
            specimen_no=fields.specimen_no,
            segment_name=segment,
            dialect=fields.dialect,
        )

    segment = "HTTP" if fields.channel == "http" else "OBX"

    if "message_control_id" in required and not fields.message_control_id:
        label, suffix = texts["message_control_id"]
        return None, _unparsed("missing_message_control_id", label, suffix, "MSH")

    item_code = fields.item_code
    item_code_source = fields.item_code_source
    item_code_confidence = fields.item_code_confidence
    if fields.item_code_text and item_matcher is not None:
        # 文本兜底：厂商给的是项目名，用它去阈值表的项目名里找项目码。
        # 匹配不上就**保持原样**（不猜、不改写既有来源标注）。
        match = item_matcher.match_by_name(fields.item_code_text)
        if match is not None:
            item_code = match.item_code
            item_code_source = f"{fields.item_code_text_source}（文本匹配）"
            item_code_confidence = "low"

    if not item_code:
        label, suffix = texts["item_code"]
        return None, _unparsed("missing_item_code", label, suffix, segment)

    if "specimen_no" in required and not fields.specimen_no:
        label, suffix = texts["specimen_no"]
        return None, _unparsed("missing_specimen_no", label, suffix, segment)

    if not fields.value_raw:
        label, suffix = texts["value"]
        return None, _unparsed("missing_value", label, suffix, segment)

    value = parse_numeric(fields.value_raw)
    if value is None:
        label = texts["value"][0]
        return None, _unparsed(
            "value_not_numeric",
            label,
            "「{value_raw}」不是数值，无法与阈值表比较（项目 {item_code}）",
            segment,
            value_raw=fields.value_raw,
        )

    if fields.observed_at is None:
        label, suffix = texts["observed_at"]
        return None, _unparsed("missing_observed_at", label, suffix, segment)

    key = build_idempotency_key(
        fields.message_control_id,
        item_code,
        fields.observed_at,
    )
    event = CriticalEvent(
        event_id=event_id_for_key(key),
        message_control_id=fields.message_control_id,
        specimen_no=fields.specimen_no,
        item_code=item_code,
        item_name=fields.item_name,
        value=value,
        unit=fields.unit,
        observed_at=fields.observed_at,
        dept_code=fields.dept_code,
        obx_flag=fields.obx_flag,
        result_status=fields.result_status,
        patient_ref=fields.patient_ref,
        source_channel=fields.source_channel,
        item_code_source=item_code_source,
        item_code_confidence=item_code_confidence,
    )
    return event, None


def normalize_http_payload(
    payload: Mapping[str, Any],
) -> tuple[Optional[CriticalEvent], Optional[UnparsedRecord]]:
    """HTTP 推送载荷 → （事件, 未识别记录）。

    与 MLLP 路径**共用** ``normalize_fields`` —— 两条通道推同一种事件，
    幂等键口径一致（``event_id + item_code + raised_at``）。

    字段映射：

    ===============  ==========================================
    载荷字段          事件字段
    ===============  ==========================================
    ``event_id``      消息控制号（进入幂等键）
    ``item_code``     检验项目码（阈值表主键）
    ``value``         观察值（判定入参）
    ``unit``          单位（换算入参）
    ``critical_level`` 异常标志 —— **仅作候选筛选与交叉校验，不作判定权威**
    ``raised_at``     结果时刻（幂等键与生效期比较入参）
    ``dept_code``     科室
    ``specimen_no``   标本号（可选）
    ``item_name``     项目名（可选）
    ``result_status`` 结果状态（``C`` = 修正）
    ===============  ==========================================
    """
    for key in payload:
        if str(key).strip().lower() in PATIENT_IDENTIFIER_FIELDS:
            return None, UnparsedRecord(
                reason="patient_identifier_rejected",
                detail=(
                    f"载荷字段「{key}」是患者标识，本通道不承载患者信息"
                    f"（请只传结果字段：event_id / item_code / value / unit / "
                    f"critical_level / raised_at / dept_code）"
                ),
                message_control_id=str(payload.get("event_id") or "").strip(),
                segment_name="HTTP",
            )

    fields = NormalizedFields(
        message_control_id=str(payload.get("event_id") or "").strip(),
        specimen_no=str(payload.get("specimen_no") or "").strip(),
        item_code=str(payload.get("item_code") or "").strip(),
        item_name=str(payload.get("item_name") or "").strip(),
        value_raw=_value_text(payload.get("value")),
        unit=str(payload.get("unit") or "").strip(),
        observed_at=parse_timestamp(payload.get("raised_at")),
        dept_code=str(payload.get("dept_code") or "").strip(),
        obx_flag=str(payload.get("critical_level") or "").strip(),
        result_status=str(payload.get("result_status") or "").strip(),
        patient_ref="",
        source_channel="http",
        item_code_source="http:item_code",
        item_code_confidence="high",
        channel="http",
    )
    return normalize_fields(fields, required=HTTP_REQUIRED_FIELDS)


def parse_oru_r01(raw: str | bytes, dialect: Optional[Dialect] = None) -> OruMessage:
    """把一条 ``ORU^R01`` 报文解析成结构化消息。

    ``dialect`` 缺省用标准方言；传厂商方言时，字段取哪里、日期怎么解析、
    空值怎么算全部按那份配置走（见 ``critval/hl7/dialects.py``）。
    """
    parsed = parse_oru_r01_with_dialect(raw, dialect or DEFAULT_DIALECT)

    observations = tuple(
        Observation(
            item_code=obs.item_code,
            item_name=obs.item_name,
            value_raw=obs.value_raw,
            unit=obs.unit,
            reference_range=obs.reference_range,
            abnormal_flag=obs.abnormal_flag,
            result_status=obs.result_status,
            observed_at_raw=obs.observed_at_raw,
            observed_at=obs.observed_at,
            set_id=obs.set_id,
            notes=obs.notes,
            item_code_source=obs.item_code_source,
            item_code_confidence=obs.item_code_confidence,
            item_code_text=obs.item_code_text,
            item_code_text_source=obs.item_code_text_source,
        )
        for obs in parsed.observations
    )

    return OruMessage(
        message_type=parsed.message_type,
        message_control_id=parsed.message_control_id,
        message_datetime=parsed.message_datetime,
        specimen_no=parsed.specimen_no,
        dept_code=parsed.dept_code,
        patient_ref=parsed.patient_ref,
        result_status=parsed.result_status,
        observations=observations,
        notes=parsed.notes,
        specimen_no_source=parsed.specimen_no_source,
        specimen_no_confidence=parsed.specimen_no_confidence,
        dialect=parsed.dialect,
    )


def normalize_events(
    message: OruMessage,
    *,
    item_matcher: Optional[Any] = None,
) -> tuple[list[CriticalEvent], list[UnparsedRecord]]:
    """把一条消息规范化成事件列表 + 未识别记录列表。

    返回的 ``events`` 里每条都**可以进入判定**；缺最小必需字段的结果
    全部进 ``unparsed``，一条都不静默丢弃。

    每条结果走的是**与 HTTP 推送通道共用**的 ``normalize_fields`` ——
    两条入站通道的最小必需字段校验、幂等键拼法、事件构造只有一份实现。

    ``item_matcher`` 是**文本兜底**用的项目匹配器（``critval.hl7.textmatch``
    的 ``ItemTextMatcher``）：厂商只给了项目名没给项目码时，拿这个名字去
    阈值表的项目名里匹配 —— 匹配上则项目码落成表内的码、置信度 ``low``；
    匹配不上则仍按「缺项目码」落 ``unparsed``，**不猜**。
    """
    unparsed: list[UnparsedRecord] = []
    events: list[CriticalEvent] = []

    if not message.message_control_id:
        unparsed.append(
            UnparsedRecord(
                reason="missing_message_control_id",
                detail="MSH-10 消息控制号缺失，整条报文无法定位幂等键",
                specimen_no=message.specimen_no,
                segment_name="MSH",
                dialect=message.dialect,
            )
        )
        return events, unparsed

    for observation in message.observations:
        event, record = normalize_fields(
            NormalizedFields(
                message_control_id=message.message_control_id,
                specimen_no=message.specimen_no,
                item_code=observation.item_code,
                item_name=observation.item_name,
                value_raw=observation.value_raw,
                unit=observation.unit,
                observed_at=observation.observed_at,
                dept_code=message.dept_code,
                obx_flag=observation.abnormal_flag,
                result_status=observation.result_status,
                patient_ref=message.patient_ref,
                source_channel="hl7",
                item_code_source=observation.item_code_source,
                item_code_confidence=observation.item_code_confidence,
                item_code_text=observation.item_code_text,
                item_code_text_source=observation.item_code_text_source,
                channel="hl7",
                dialect=message.dialect,
            ),
            required=HL7_REQUIRED_FIELDS,
            item_matcher=item_matcher,
        )
        if event is not None:
            events.append(event)
        if record is not None:
            unparsed.append(record)

    return events, unparsed
