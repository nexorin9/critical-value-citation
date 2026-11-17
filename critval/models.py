"""领域模型：事件 / 阈值行 / 制度条款 / Citation / 处置卡 / 台账记录。

模型只承载数据与最小不变量，**不做 I/O**：解析在 ``kb``、判定在 ``judge``、
装配在 ``card``。所有落库 / 推送字段都从这里出，避免同一语义在多处各写一份。

两条硬红线在这里就地固化：

* **出处不可丢**：``Citation`` 必须带 ``source_sha256`` 与可读行号，
  否则下游无法回答「这条依据是哪份文件第几行」。
* **患者可识别明细不落**：事件只保留不透明的 ``patient_ref``，
  处置卡渲染层另有白名单与守卫。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Optional

__all__ = [
    "Citation",
    "ConflictItem",
    "ConflictSide",
    "CriticalEvent",
    "DispositionCard",
    "Judgement",
    "LedgerRecord",
    "MergedCardItem",
    "MergedDispositionCard",
    "PolicyClause",
    "PriorityScore",
    "Provenance",
    "CardProvenance",
    "ReferenceBand",
    "ThresholdRow",
    "CardElement",
    "CONFLICT_FIELD_ELEMENT_KEYS",
    "to_jsonable",
]


def _jsonable(value: Any) -> Any:
    """把模型里的 Decimal / date / tuple 转成可 JSON 化的值。"""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    return value


def to_jsonable(value: Any) -> Any:
    """公开版 JSON 化（快照落盘、导出等处复用同一套转换）。"""
    return _jsonable(value)


@dataclass(frozen=True)
class Citation:
    """出处三元组 + 可读定位。

    ``line_no`` 是**给人看的行号**（xlsx 行号 / docx 段落行号），
    ``paragraph_no`` 是段落序号（制度条款用），两者都不参与机器匹配。
    """

    source_name: str
    source_sha256: str
    kind: str = "threshold_row"  # threshold_row | policy_clause
    paragraph_no: Optional[int] = None
    section_path: tuple[str, ...] = ()
    line_no: Optional[int] = None
    file_version: Optional[str] = None
    #: 召回降级层级（任务 id 32）：0 = 原样查询，1 = 关键词扩展，
    #: 2 = 章节级兜底，3 = 无依据转人工（此时**不会产出引用**）。
    #: 依据的层级写在引用上，值班人一眼能分出"直接命中"与"降级捞到"。
    level: int = 0

    @property
    def locator(self) -> str:
        """一句话定位：``文件名 第 N 行 · 版本``。"""
        parts = [self.source_name]
        if self.line_no is not None:
            parts.append(f"第 {self.line_no} 行")
        if self.file_version:
            parts.append(self.file_version)
        return " · ".join(parts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_name": self.source_name,
            "source_sha256": self.source_sha256,
            "kind": self.kind,
            "paragraph_no": self.paragraph_no,
            "section_path": list(self.section_path),
            "line_no": self.line_no,
            "file_version": self.file_version,
            "locator": self.locator,
            "level": self.level,
        }


@dataclass(frozen=True)
class ReferenceBand:
    """参考区间的一个分档（按性别 / 年龄段选出的那一条）。

    ``kind`` 取值：

    * ``numeric`` —— 数值区间，正常参与分档；
    * ``text`` —— 定性区间（阴性 / 阳性），不参与数值比较；
    * ``invalid`` —— 写法看得懂但上下限反写（9.5-3.5），不参与分档；
    * ``missing`` —— **写法解析不出来**的缺失标记，不参与分档，
      但保留原文与原因，供解析报告与人工确认使用（不静默跳过）。

    ``confidence`` 记录这条分档被选中的把握：``high`` = 明确命中该人群分档；
    ``low`` = 上下文不足只能取默认区间（或分档与已知信息冲突），须在判定记录里留痕。
    """

    raw: str
    low: Optional[Decimal] = None
    high: Optional[Decimal] = None
    low_inclusive: bool = True
    high_inclusive: bool = True
    label: Optional[str] = None  # 男 / 女 / 儿童 / 成人 …
    kind: str = "numeric"  # numeric | text | invalid | missing
    text_value: Optional[str] = None  # 阴性 / 阳性 等文本型区间
    confidence: str = "high"  # high | low
    note: str = ""  # 低置信原因 / 缺失原因 / 反写原因

    def contains(self, value: Decimal) -> bool:
        if self.kind != "numeric" or value is None:
            return False
        if self.low is not None:
            if value < self.low or (value == self.low and not self.low_inclusive):
                return False
        if self.high is not None:
            if value > self.high or (value == self.high and not self.high_inclusive):
                return False
        return True

    def to_dict(self) -> dict[str, Any]:
        return {
            "raw": self.raw,
            "low": _jsonable(self.low),
            "high": _jsonable(self.high),
            "low_inclusive": self.low_inclusive,
            "high_inclusive": self.high_inclusive,
            "label": self.label,
            "kind": self.kind,
            "text_value": self.text_value,
            "confidence": self.confidence,
            "note": self.note,
        }


@dataclass(frozen=True)
class ThresholdRow:
    """阈值表的一行 —— 危急值判定的权威依据。"""

    item_code: str
    item_name: str
    unit: str
    low_critical: Optional[Decimal]
    high_critical: Optional[Decimal]
    reference_range_raw: str
    effective_from: Optional[date]
    version: str
    citation: Citation
    row_no: Optional[int] = None
    note: str = ""
    # —— 规则编译所需的事实（任务 id 18）——
    # 原始单元格文本：区分「空着」与「写了但解析不出来」，
    # 两者在规则缺口清单里的归因完全不同。
    raw_low: str = ""
    raw_high: str = ""
    # 闭开区间语义：True = 取等号也算危急（源表写 ≤ / ≥），False = 严格不等。
    low_inclusive: bool = False
    high_inclusive: bool = False
    effective_to: Optional[date] = None
    # 适用范围分档：性别 / 年龄段（同一项目可以按人群拆成多行）。
    applies_sex: str = ""
    applies_age_min: Optional[int] = None
    applies_age_max: Optional[int] = None
    applies_age_label: str = ""
    #: 适用人群写法无法识别时的说明（该行不得进入判定集，只能转人工）。
    scope_error: str = ""

    @property
    def direction(self) -> str:
        """危急方向：``low`` / ``high`` / ``both`` / ``none``。"""
        if self.low_critical is not None and self.high_critical is not None:
            return "both"
        if self.low_critical is not None:
            return "low"
        if self.high_critical is not None:
            return "high"
        return "none"

    def threshold_text(self) -> str:
        low_op = "<=" if self.low_inclusive else "<"
        high_op = ">=" if self.high_inclusive else ">"
        if self.direction == "both":
            return f"{low_op} {self.low_critical} 或 {high_op} {self.high_critical} {self.unit}"
        if self.direction == "low":
            return f"{low_op} {self.low_critical} {self.unit}"
        if self.direction == "high":
            return f"{high_op} {self.high_critical} {self.unit}"
        return "未给出阈值"

    def to_dict(self) -> dict[str, Any]:
        return {
            "item_code": self.item_code,
            "item_name": self.item_name,
            "unit": self.unit,
            "low_critical": _jsonable(self.low_critical),
            "high_critical": _jsonable(self.high_critical),
            "reference_range_raw": self.reference_range_raw,
            "effective_from": _jsonable(self.effective_from),
            "effective_to": _jsonable(self.effective_to),
            "version": self.version,
            "row_no": self.row_no,
            "note": self.note,
            "direction": self.direction,
            "raw_low": self.raw_low,
            "raw_high": self.raw_high,
            "low_inclusive": self.low_inclusive,
            "high_inclusive": self.high_inclusive,
            "applies_sex": self.applies_sex,
            "applies_age_min": self.applies_age_min,
            "applies_age_max": self.applies_age_max,
            "applies_age_label": self.applies_age_label,
            "scope_error": self.scope_error,
            "citation": self.citation.to_dict(),
        }


@dataclass(frozen=True)
class PolicyClause:
    """制度里的一条 —— 时限与通知对象的权威依据。

    ``missing_fields`` 记这条条款**缺**哪几项事实（``notify_target`` /
    ``deadline`` / ``record_destination``）：制度只写了一半时，处置卡上
    那一项要标缺项并写明原因，不能拿别的条款的取值顶替。

    ``item_codes`` 是条款正文里点名的检验项目码 —— 绑定表
    （项目码 → 阈值表版本 + 制度版本）靠它把制度条款挂到项目上。

    ``stable_id`` 是**跨版本稳定**的条款标识：制度改版 diff 按它配对，
    「条款改了」与「条款删了」是两回事。
    """

    section_title: str
    body: str
    notify_target: str
    deadline_text: str
    deadline_minutes: Optional[int]
    record_destination: str
    citation: Citation
    clause_no: Optional[str] = None
    #: 条款正文里点名的检验项目码（去重、保序）。
    item_codes: tuple[str, ...] = ()
    #: 条款正文里「项目名（项目码）」的项目名（去重、保序）。
    item_names: tuple[str, ...] = ()
    #: 缺失的事实字段名（解析出来了才有，解析不出来就留空）。
    missing_fields: tuple[str, ...] = ()
    #: 跨版本稳定的条款标识（改版 diff 的配对键）。
    stable_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "section_title": self.section_title,
            "body": self.body,
            "notify_target": self.notify_target,
            "deadline_text": self.deadline_text,
            "deadline_minutes": self.deadline_minutes,
            "record_destination": self.record_destination,
            "clause_no": self.clause_no,
            "item_codes": list(self.item_codes),
            "item_names": list(self.item_names),
            "missing_fields": list(self.missing_fields),
            "stable_id": self.stable_id,
            "citation": self.citation.to_dict(),
        }


@dataclass(frozen=True)
class CriticalEvent:
    """一条危急值事件（规范化后的入站事件模型）。

    ``patient_ref`` 是不透明引用（仅用于事件标识层），**不含**姓名 / 身份证 /
    住院号等可识别明细。
    """

    event_id: str
    message_control_id: str
    specimen_no: str
    item_code: str
    item_name: str
    value: Decimal
    unit: str
    observed_at: datetime
    dept_code: str = ""
    obx_flag: str = ""
    result_status: str = ""  # C = 修正结果
    patient_ref: str = ""
    source_channel: str = "hl7"
    #: 项目码取自哪个字段（``OBX-3.1`` / ``OBR-4.1`` / ``ZLR-2`` …）——
    #: 方言兜底匹配时值班人要看的就是这个。
    item_code_source: str = ""
    #: 项目码置信度：``high`` / ``medium`` / ``low``；``low`` = 兜底匹配来的。
    item_code_confidence: str = "high"
    #: 本事件取代的旧事件 ID（修正结果 ``OBR-25=C`` 才有，任务 id 9）。
    #: 旧事件被置 ``superseded``（终态），其判定与依据原样保留。
    supersedes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "message_control_id": self.message_control_id,
            "specimen_no": self.specimen_no,
            "item_code": self.item_code,
            "item_name": self.item_name,
            "value": _jsonable(self.value),
            "unit": self.unit,
            "observed_at": _jsonable(self.observed_at),
            "dept_code": self.dept_code,
            "obx_flag": self.obx_flag,
            "result_status": self.result_status,
            "patient_ref": self.patient_ref,
            "source_channel": self.source_channel,
            "item_code_source": self.item_code_source,
            "item_code_confidence": self.item_code_confidence,
            "supersedes": self.supersedes,
        }


@dataclass(frozen=True)
class Judgement:
    """危急值判定结论。

    ``decision`` 取值：``critical`` / ``not_critical`` / ``unit_unknown`` /
    ``no_threshold`` / ``ambiguous``。后三者都是**不判定**，须转人工，
    不得当成「不危急」静默放过。
    """

    decision: str
    reason: str
    item_code: str
    value: Decimal
    unit: str
    converted_value: Optional[Decimal] = None
    converted_unit: Optional[str] = None
    #: 换算目标单位保留的小数位（换算精度）；``None`` 表示未换算。
    converted_precision: Optional[int] = None
    #: 换算说明（换算类别 / 取整说明），进判定记录便于复算。
    conversion_note: str = ""
    #: 换算后的值是否恰好落在阈值的一个精度步长之内（压线值）。
    #: 压线不等于判错，但值得人工再看一眼 —— 该提示随判定记录落库。
    near_boundary: bool = False
    #: 压线说明（与哪个阈值差了多少、精度步长多大）。
    boundary_note: str = ""
    threshold_row: Optional[ThresholdRow] = None
    rule_id: Optional[str] = None
    reference_band: Optional[ReferenceBand] = None
    #: 参考区间分档置信度：``high`` / ``low`` / ``missing`` / ``""``（表里没给区间）。
    #: ``low`` 表示上下文不足只能取默认区间，``missing`` 表示写法解析不出来。
    reference_band_confidence: str = ""
    #: 参考区间的低置信原因 / 缺失原因（可被机器解析，进判定记录）。
    reference_band_note: str = ""
    obx_flag_disagreement: bool = False
    #: ``OBX-8`` 与阈值表的口径分歧说明（任务 id 9）。分歧**不改判**：
    #: 判定权威永远是院内阈值表，这段说明只告诉值班人「两边对不上」。
    flag_disagreement_note: str = ""
    #: 本次判定所用的规则集快照 ID（任务 id 18）。事件落库时一并记录，
    #: 事后复核能确定「当时用的是哪一版阈值规则」。
    ruleset_snapshot_id: str = ""
    #: 本次判定生效时**配置快照的版本号**（任务 id 20）。配置会在运行期热载入，
    #: 事后复核要能确定「这条结论是用哪一版配置判出来的」。
    config_version: int = 0

    @property
    def judged(self) -> bool:
        """是否给出了确定结论。"""
        return self.decision in ("critical", "not_critical")

    @property
    def is_critical(self) -> bool:
        return self.decision == "critical"

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "reason": self.reason,
            "item_code": self.item_code,
            "value": _jsonable(self.value),
            "unit": self.unit,
            "converted_value": _jsonable(self.converted_value),
            "converted_unit": self.converted_unit,
            "converted_precision": self.converted_precision,
            "conversion_note": self.conversion_note,
            "near_boundary": self.near_boundary,
            "boundary_note": self.boundary_note,
            "rule_id": self.rule_id,
            "ruleset_snapshot_id": self.ruleset_snapshot_id,
            "config_version": self.config_version,
            "reference_band": _jsonable(self.reference_band.to_dict()) if self.reference_band else None,
            "reference_band_confidence": self.reference_band_confidence,
            "reference_band_note": self.reference_band_note,
            "obx_flag_disagreement": self.obx_flag_disagreement,
            "flag_disagreement_note": self.flag_disagreement_note,
            "threshold_row": self.threshold_row.to_dict() if self.threshold_row else None,
        }


@dataclass(frozen=True)
class Provenance:
    """某一项依据的来源与降级标注（任务 id 21）。

    ``source`` 说明这是哪一环的来源：``duty_roster``（当班名单）/
    ``threshold_table``（阈值行）/ ``policy_retrieval``（制度召回）。

    ``origin`` 说明这个来源是**实时拿到的**还是**降级来的**：

    * ``live`` / ``active`` —— 实时（或当期生效）取到；
    * ``cache`` —— 用了带时间戳的缓存（排班系统不可用时的降级）；
    * ``historical`` —— 用的是历史版本（阈值行改版后按旧版判定）；
    * ``fallback`` —— 走了兜底（科室值班长顶班等）；
    * ``unavailable`` —— 上游根本不可用，该项没有依据。

    ``level`` 是召回降级层级：0 = 原样查询，1 = 关键词扩展，
    2 = 章节级兜底，3 = 无依据转人工。
    """

    source: str
    origin: str
    detail: str = ""
    as_of: str = ""
    level: int = 0
    citation: Optional[Citation] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "origin": self.origin,
            "detail": self.detail,
            "as_of": self.as_of,
            "level": self.level,
        }


@dataclass(frozen=True)
class CardProvenance:
    """一张处置卡上三处来源的合集。"""

    duty_roster: Optional[Provenance] = None
    threshold_table: Optional[Provenance] = None
    policy_retrieval: Optional[Provenance] = None

    def entries(self) -> tuple[Provenance, ...]:
        return tuple(p for p in (self.duty_roster, self.threshold_table, self.policy_retrieval) if p)

    def to_dict(self) -> dict[str, Any]:
        return {
            "duty_roster": self.duty_roster.to_dict() if self.duty_roster else None,
            "threshold_table": self.threshold_table.to_dict() if self.threshold_table else None,
            "policy_retrieval": self.policy_retrieval.to_dict() if self.policy_retrieval else None,
        }


@dataclass(frozen=True)
class CardElement:
    """处置卡的一个要素（阈值依据行 / 通知对象 / 上报时限 / 记录去向）。

    ``status`` 三态：

    * ``resolved`` —— 依据齐备且来源权威；
    * ``degraded`` —— 依据有值，但来源是降级来的（缓存 / 历史版本 /
      降级召回）或要素只取到一部分 —— 必须让人看得出这条依据弱在哪；
    * ``missing`` —— **没有依据**，绝不编造。

    ``reason`` 是可被机器解析的原因码（``resolved`` 时为 ``resolved``），
    ``note`` 是给人看的原因说明。缺项时用占位文案冒充已解决是禁止的。
    """

    key: str
    label: str
    value: str = ""
    status: str = "missing"
    source: Optional[Citation] = None
    note: str = ""
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "value": self.value,
            "status": self.status,
            "reason": self.reason,
            "source": self.source.to_dict() if self.source else None,
            "note": self.note,
        }


#: 冲突字段 → 处置卡要素键（一条冲突牵连哪个要素）。
CONFLICT_FIELD_ELEMENT_KEYS: dict[str, str] = {
    "threshold_value": "threshold_basis",
    "notify_target": "notify_target",
    "deadline": "deadline",
    "record_destination": "record_destination",
}

#: 制度类要素（引用了非生效版本或范围不匹配的制度条款时，这三项一起受牵连）。
CONFLICT_POLICY_ELEMENT_KEYS: tuple[str, ...] = (
    "notify_target",
    "deadline",
    "record_destination",
)


def conflict_element_keys(conflict: "ConflictItem") -> tuple[str, ...]:
    """一条冲突牵连哪些处置卡要素。

    * 口径冲突 → 直接对应的那一项；
    * 版本冲突 → 阈值行侧只牵连「阈值依据行」，制度条款侧牵连三项制度类要素；
    * 范围冲突 → 制度条款侧的三项。
    """
    key = CONFLICT_FIELD_ELEMENT_KEYS.get(conflict.field)
    if key:
        return (key,)
    if conflict.kind in ("version", "scope"):
        roles = {side.role for side in conflict.sides}
        if roles == {"threshold_row"}:
            return ("threshold_basis",)
        return CONFLICT_POLICY_ELEMENT_KEYS
    return ()


@dataclass(frozen=True)
class ConflictSide:
    """口径冲突的**一方**（阈值行侧或制度条款侧）。

    ``path`` 记这条依据是被**哪一路**召回捞到的（``structured`` 结构化精确路 /
    ``lexical`` 词法 BM25 路）—— 归因要看的就是"两路各自的说法"。

    ``role`` 记这条依据的角色（``threshold_row`` 阈值行 / ``policy_clause``
    制度条款）。两方出处一律带 ``citation``，值班人才知道去翻哪份文件第几行。
    """

    path: str = ""
    role: str = ""
    stable_key: str = ""
    locator: str = ""
    value: str = ""
    citation: Optional[Citation] = None
    version: str = ""
    item_codes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "role": self.role,
            "stable_key": self.stable_key,
            "locator": self.locator,
            "value": self.value,
            "version": self.version,
            "item_codes": list(self.item_codes),
            "citation": self.citation.to_dict() if self.citation else None,
        }


@dataclass(frozen=True)
class ConflictItem:
    """一条**待人工裁决**的口径冲突（任务 id 33）。

    ``kind`` 取值：

    * ``caliber`` —— 口径冲突：两路对同一字段给的取值不一致（时限 / 通知对象 /
      记录去向 / 阈值数值）；
    * ``version`` —— 版本冲突：引用了**非生效版本**的依据；
    * ``scope`` —— 范围冲突：条款点名的适用范围与本次项目不匹配。

    ``sides`` 保留**两方（或多方）**的取值与出处。系统**不替值班人选一个** ——
    两方都留着，由值班人在确认页裁决。

    ``status`` 为 ``pending`` 时，相关处置要素不得标成「已解决」。
    """

    conflict_id: str
    kind: str = ""
    field: str = ""
    item_code: str = ""
    description: str = ""
    sides: tuple[ConflictSide, ...] = ()
    status: str = "pending"

    @property
    def structured_side(self) -> Optional[ConflictSide]:
        """结构化精确路那一方的说法。"""
        for side in self.sides:
            if side.path == "structured":
                return side
        return None

    @property
    def lexical_side(self) -> Optional[ConflictSide]:
        """词法 BM25 路那一方的说法。"""
        for side in self.sides:
            if side.path == "lexical":
                return side
        return None

    @property
    def pending(self) -> bool:
        """是否仍待人工裁决。"""
        return self.status == "pending"

    def to_dict(self) -> dict[str, Any]:
        return {
            "conflict_id": self.conflict_id,
            "kind": self.kind,
            "field": self.field,
            "item_code": self.item_code,
            "description": self.description,
            "status": self.status,
            "sides": [side.to_dict() for side in self.sides],
        }


@dataclass(frozen=True)
class DispositionCard:
    """处置卡 —— 值班人看到的全部内容。"""

    card_id: str
    event: CriticalEvent
    judgement: Judgement
    threshold_basis: CardElement
    notify_target: CardElement
    deadline: CardElement
    record_destination: CardElement
    created_at: datetime
    extra_citations: tuple[Citation, ...] = ()
    #: 装配这张卡时生效的配置快照版本号（任务 id 20）。卡片要推给值班群，
    #: 这张卡本身就是推送环节的配置凭证之一。
    config_version: int = 0
    #: 三处来源的降级标注（任务 id 21）：当班名单 / 阈值行 / 制度召回。
    provenance: CardProvenance = field(default_factory=CardProvenance)
    #: **待人工裁决**的口径冲突（任务 id 33）：两路召回对同一项目给出的
    #: 时限 / 通知对象 / 阈值数值不一致，或引用了非生效版本。冲突挂到卡片上，
    #: 由值班人裁决；系统不替它选一个。
    conflicts: tuple[ConflictItem, ...] = ()

    @property
    def elements(self) -> tuple[CardElement, ...]:
        return (
            self.threshold_basis,
            self.notify_target,
            self.deadline,
            self.record_destination,
        )

    @property
    def citations(self) -> tuple[Citation, ...]:
        found = [e.source for e in self.elements if e.source is not None]
        found.extend(self.extra_citations)
        return tuple(found)

    def missing_elements(self) -> tuple[str, ...]:
        """**没有依据**的要素（降级不算缺项，降级另列）。"""
        return tuple(e.label for e in self.elements if e.status == "missing")

    def degraded_elements(self) -> tuple[str, ...]:
        """依据有值但来源降级的要素。"""
        return tuple(e.label for e in self.elements if e.status == "degraded")

    def pending_conflicts(self) -> tuple[ConflictItem, ...]:
        """仍待人工裁决的冲突。"""
        return tuple(c for c in self.conflicts if c.pending)

    def conflicting_elements(self) -> tuple[str, ...]:
        """被冲突牵连的要素标签（去重保序）。"""
        labels: list[str] = []
        for conflict in self.pending_conflicts():
            for key in conflict_element_keys(conflict):
                for element in self.elements:
                    if element.key == key and element.label not in labels:
                        labels.append(element.label)
        return tuple(labels)

    def to_dict(self) -> dict[str, Any]:
        return {
            "card_id": self.card_id,
            "event": self.event.to_dict(),
            "judgement": self.judgement.to_dict(),
            "config_version": self.config_version,
            "created_at": _jsonable(self.created_at),
            "elements": [e.to_dict() for e in self.elements],
            "citations": [c.to_dict() for c in self.citations],
            "provenance": self.provenance.to_dict(),
            "conflicts": [c.to_dict() for c in self.conflicts],
        }


@dataclass(frozen=True)
class PriorityScore:
    """一条危急结果的**优先级**（任务 id 34）。

    分值由三样相乘得到：``deviation_ratio``（相对偏离幅度）×
    ``direction_weight``（危急方向权重）× ``urgency_weight``（项目级紧急度）。

    ``band`` 是给值班人看的一档人话标签（紧急处置 / 优先处置 / 常规处置 /
    贴近阈值，待复核），排序时以 ``score`` 为准 —— **同分按项目码升序**，
    保证同一批结果每次排出来的顺序一样（不随运行漂移）。
    """

    score: Decimal
    band: str
    deviation_ratio: Decimal
    deviation_note: str
    direction: str
    direction_weight: Decimal
    urgency_weight: Decimal
    urgency_note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "score": _jsonable(self.score),
            "band": self.band,
            "deviation_ratio": _jsonable(self.deviation_ratio),
            "deviation_note": self.deviation_note,
            "direction": self.direction,
            "direction_weight": _jsonable(self.direction_weight),
            "urgency_weight": _jsonable(self.urgency_weight),
            "urgency_note": self.urgency_note,
        }


@dataclass(frozen=True)
class MergedCardItem:
    """合并处置卡里的**一个危急项目**。

    每个项目各自带一条 ``threshold_basis``（自己的阈值依据行 + 自己的出处
    行号）—— 合并只合并**通知单元**，不合并依据：值班人仍要能逐项回答
    「这一项按哪一行阈值判的」。
    """

    event_id: str
    item_code: str
    item_name: str
    value: Decimal
    unit: str
    observed_at: datetime
    decision: str
    reason: str
    threshold_basis: CardElement
    priority: PriorityScore
    judgement: Judgement

    @property
    def label(self) -> str:
        """卡内这一项的人话标签（含项目码，缺项时也要能认出是哪一项）。"""
        return f"阈值依据行 · {self.item_name or self.item_code}（{self.item_code}）"

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "item_code": self.item_code,
            "item_name": self.item_name,
            "value": _jsonable(self.value),
            "unit": self.unit,
            "observed_at": _jsonable(self.observed_at),
            "decision": self.decision,
            "reason": self.reason,
            "label": self.label,
            "threshold_basis": self.threshold_basis.to_dict(),
            "priority": self.priority.to_dict(),
        }


@dataclass(frozen=True)
class MergedDispositionCard:
    """**一条标本一张卡**的合并处置卡（任务 id 34）。

    卡片分两层：

    * **逐项**（``items``）—— 每个危急项目一条，各自带阈值依据行与出处；
    * **共享**（``notify_target`` / ``deadline`` / ``record_destination``）——
      通知对象、上报时限、记录去向来自同一份制度条款与同一个当班人，
      一张卡里只说一次。

    ``items`` 已按优先级降序排好（同分按项目码升序），渲染层不再二次排序。
    """

    card_id: str
    specimen_no: str
    dept_code: str
    window_start: datetime
    window_end: datetime
    items: tuple[MergedCardItem, ...]
    notify_target: CardElement
    deadline: CardElement
    record_destination: CardElement
    created_at: datetime
    extra_citations: tuple[Citation, ...] = ()
    config_version: int = 0
    provenance: CardProvenance = field(default_factory=CardProvenance)

    @property
    def shared_elements(self) -> tuple[CardElement, ...]:
        """卡级共享的三要素。"""
        return (self.notify_target, self.deadline, self.record_destination)

    @property
    def elements(self) -> tuple[CardElement, ...]:
        """全部要素（逐项阈值依据行 + 卡级共享三要素）。"""
        return tuple(item.threshold_basis for item in self.items) + self.shared_elements

    @property
    def basis_entries(self) -> tuple[tuple[str, CardElement], ...]:
        """逐项依据条目（标签 + 要素）—— 渲染与验收都按它数「卡内几项依据」。"""
        return tuple((item.label, item.threshold_basis) for item in self.items)

    @property
    def citations(self) -> tuple[Citation, ...]:
        found = [item.threshold_basis.source for item in self.items if item.threshold_basis.source]
        found.extend(e.source for e in self.shared_elements if e.source is not None)
        found.extend(self.extra_citations)
        return tuple(found)

    def missing_elements(self) -> tuple[str, ...]:
        """**没有依据**的要素（逐项按项目标签、共享项按要素标签）。"""
        labels: list[str] = []
        for item in self.items:
            if item.threshold_basis.status == "missing":
                labels.append(item.label)
        labels.extend(e.label for e in self.shared_elements if e.status == "missing")
        return tuple(labels)

    def degraded_elements(self) -> tuple[str, ...]:
        """依据有值但来源降级的要素。"""
        labels: list[str] = []
        for item in self.items:
            if item.threshold_basis.status == "degraded":
                labels.append(item.label)
        labels.extend(e.label for e in self.shared_elements if e.status == "degraded")
        return tuple(labels)

    def to_dict(self) -> dict[str, Any]:
        return {
            "card_id": self.card_id,
            "specimen_no": self.specimen_no,
            "dept_code": self.dept_code,
            "window_start": _jsonable(self.window_start),
            "window_end": _jsonable(self.window_end),
            "config_version": self.config_version,
            "created_at": _jsonable(self.created_at),
            "items": [item.to_dict() for item in self.items],
            "shared_elements": [e.to_dict() for e in self.shared_elements],
            "citations": [c.to_dict() for c in self.citations],
            "provenance": self.provenance.to_dict(),
            "missing_elements": list(self.missing_elements()),
            "degraded_elements": list(self.degraded_elements()),
        }


@dataclass
class LedgerRecord:
    """台账记录（事件在业务状态机上的当前落点）。"""

    event_id: str
    status: str = "received"
    message_control_id: str = ""
    specimen_no: str = ""
    item_code: str = ""
    dept_code: str = ""
    card_id: str = ""
    threshold_row: str = ""
    notify_target: str = ""
    due_minutes: Optional[int] = None
    receipt_status: str = ""
    acknowledged_by: str = ""
    acknowledged_at: Optional[datetime] = None
    registered_at: Optional[datetime] = None
    config_version: int = 0
    ruleset_snapshot_id: str = ""
    created_at: datetime = field(default_factory=datetime.utcnow)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "status": self.status,
            "message_control_id": self.message_control_id,
            "specimen_no": self.specimen_no,
            "item_code": self.item_code,
            "dept_code": self.dept_code,
            "card_id": self.card_id,
            "threshold_row": self.threshold_row,
            "notify_target": self.notify_target,
            "due_minutes": self.due_minutes,
            "receipt_status": self.receipt_status,
            "acknowledged_by": self.acknowledged_by,
            "acknowledged_at": _jsonable(self.acknowledged_at),
            "registered_at": _jsonable(self.registered_at),
            "config_version": self.config_version,
            "ruleset_snapshot_id": self.ruleset_snapshot_id,
            "created_at": _jsonable(self.created_at),
        }
