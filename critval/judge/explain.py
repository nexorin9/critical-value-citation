"""判定可解释与判定回放（任务 id 36）。

## 为什么要有这一层

值班人拿着一张危急值处置卡被问「凭什么是危急值」，他要能在**一分钟内**
把判定过程复算一遍 —— 不是看一句「判定为危急值：低于低危急值 30」就完事，
而是逐步看：上报原值是多少 → 换算到阈值单位是多少 → 命中了哪条规则 →
阈值行是哪份文件第几行 → 和 LIS 异常标志口径对不对得上 → 最后为什么是这个结论。

## 判定链 = 一步步可复算的记录

一条判定记录（:class:`JudgementRecord`）由七段组成，每段带
**输入 / 输出 / 依据 / 规则 ID / 来源行号**：

===========================  ==================================================
阶段                          这段回答什么
===========================  ==================================================
``raw_value``                LIS 报上来的原值是多少（换算前）
``rule_selection``           命中了哪条规则（规则 ID + 适用范围 + 出处）
``unit_conversion``          换算到阈值单位是多少、精度多少、为什么
``reference_band``           参考区间分档是哪一档、把握多大
``threshold_compare``        与哪一行阈值比、低侧高侧哪一边触发
``flag_crosscheck``          LIS 异常标志与阈值表口径是否一致（分歧不改判）
``conclusion``               最终结论、理由、要不要转人工
===========================  ==================================================

记录随事件落库（``judgement_records`` 表），可按事件 ID 取回；
``judge-replay`` 命令按事件 ID **重跑一遍**判定，把新算出来的记录与落库记录
**逐字段比对** —— 一致说明「这条结论用同一份材料能复算出来」；不一致就逐条
列出差在哪个字段（材料换了？规则改了？还是记录本身坏了）。

## 两条红线

1. **不落患者可识别明细**：判定记录里没有姓名 / 身份证 / 住院号，连不透明的
   ``patient_ref`` 也不写 —— 判定过程用不到它。:func:`assert_no_patient_detail`
   在每次落盘前扫一遍，命中就抛 :class:`PatientDetailLeak`。
2. **不改写历史事实**：回放是**只读**的 —— 重跑判定不写台账、不改状态、
   不重推处置卡，它只回答「当初那条结论能不能复算出来」。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Mapping, Optional

from critval.judge.critical import requires_human_review
from critval.models import CriticalEvent, Judgement, ThresholdRow, to_jsonable

__all__ = [
    "ExplainStep",
    "JudgementRecord",
    "PATIENT_DETAIL_KEYS",
    "PatientDetailLeak",
    "REPLAY_COMPARE_FIELDS",
    "STAGE_CONCLUSION",
    "STAGE_FLAG_CROSSCHECK",
    "STAGE_LABELS",
    "STAGE_ORDER",
    "STAGE_RAW_VALUE",
    "STAGE_REFERENCE_BAND",
    "STAGE_RULE_SELECTION",
    "STAGE_THRESHOLD_COMPARE",
    "STAGE_UNIT_CONVERSION",
    "assert_no_patient_detail",
    "build_judgement_record",
    "diff_records",
    "event_from_row",
    "normalize_reason",
    "replay_supersede_reason",
]

# ---------------------------------------------------------------------------
# 阶段
# ---------------------------------------------------------------------------

#: LIS 报上来的原值（未换算）。
STAGE_RAW_VALUE = "raw_value"
#: 命中哪条规则（项目码 + 生效期 + 人群分档）。
STAGE_RULE_SELECTION = "rule_selection"
#: 单位换算。
STAGE_UNIT_CONVERSION = "unit_conversion"
#: 参考区间分档。
STAGE_REFERENCE_BAND = "reference_band"
#: 与阈值行比较。
STAGE_THRESHOLD_COMPARE = "threshold_compare"
#: 与 LIS 异常标志（``OBX-8``）交叉校验。
STAGE_FLAG_CROSSCHECK = "flag_crosscheck"
#: 最终结论。
STAGE_CONCLUSION = "conclusion"

#: 判定链的固定段序 —— 回放时按这个顺序打印，不按运行时拼接顺序。
STAGE_ORDER: tuple[str, ...] = (
    STAGE_RAW_VALUE,
    STAGE_RULE_SELECTION,
    STAGE_UNIT_CONVERSION,
    STAGE_REFERENCE_BAND,
    STAGE_THRESHOLD_COMPARE,
    STAGE_FLAG_CROSSCHECK,
    STAGE_CONCLUSION,
)

#: 阶段的人话标签。
STAGE_LABELS: dict[str, str] = {
    STAGE_RAW_VALUE: "原始值",
    STAGE_RULE_SELECTION: "命中规则",
    STAGE_UNIT_CONVERSION: "单位换算",
    STAGE_REFERENCE_BAND: "参考区间分档",
    STAGE_THRESHOLD_COMPARE: "阈值比较",
    STAGE_FLAG_CROSSCHECK: "异常标志交叉校验",
    STAGE_CONCLUSION: "结论",
}

#: 判定记录里**不得出现**的字段名（患者可识别明细）。
PATIENT_DETAIL_KEYS = frozenset(
    {
        "patient_ref",
        "patient_name",
        "patient_id",
        "patient_name_zh",
        "id_card",
        "id_number",
        "mpi_no",
        "住院号",
        "姓名",
        "身份证号",
        "身份证",
    }
)

#: 回放比对时逐字段核对的字段（版本号类字段单独列在「版本对照」里，
#: 因为它们随进程与热载入次数变化，不影响结论本身）。
REPLAY_COMPARE_FIELDS: tuple[str, ...] = (
    "decision",
    "reason",
    "item_code",
    "value",
    "unit",
    "converted_value",
    "converted_unit",
    "rule_id",
    "threshold_locator",
    "source_name",
    "source_sha256",
    "source_line",
    "file_version",
    "threshold_source_sha256",
    "reference_band_kind",
    "reference_band_confidence",
    "reference_band_note",
    "obx_flag_disagreement",
    "flag_disagreement_note",
    "near_boundary",
    "boundary_note",
)


class PatientDetailLeak(RuntimeError):
    """判定记录里出现了患者可识别明细 —— 落盘前必须拦下。"""


def assert_no_patient_detail(payload: Any, *, where: str = "判定记录") -> None:
    """递归检查载荷里没有患者可识别字段。

    键名命中 :data:`PATIENT_DETAIL_KEYS` 即抛 :class:`PatientDetailLeak`。
    """
    if isinstance(payload, Mapping):
        for key, value in payload.items():
            if str(key) in PATIENT_DETAIL_KEYS:
                raise PatientDetailLeak(
                    f"{where}出现了患者可识别字段 {key!r} —— 判定记录不得落患者明细"
                )
            assert_no_patient_detail(value, where=where)
    elif isinstance(payload, (list, tuple)):
        for item in payload:
            assert_no_patient_detail(item, where=where)


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExplainStep:
    """判定链上的一段。

    每段都带**输入**与**输出**（机器可核对），外加人话**依据**、
    命中的**规则 ID**与**来源行号**（值班人一眼能跳回原始材料）。
    """

    step_no: int
    stage: str
    title: str = ""
    input: dict[str, Any] = field(default_factory=dict)
    output: dict[str, Any] = field(default_factory=dict)
    basis: str = ""
    rule_id: str = ""
    source_name: str = ""
    source_line: Optional[int] = None
    source_sha256: str = ""
    file_version: str = ""
    note: str = ""

    @property
    def stage_label(self) -> str:
        return STAGE_LABELS.get(self.stage, self.stage)

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_no": self.step_no,
            "stage": self.stage,
            "stage_label": self.stage_label,
            "title": self.title,
            "input": to_jsonable(self.input),
            "output": to_jsonable(self.output),
            "basis": self.basis,
            "rule_id": self.rule_id,
            "source_name": self.source_name,
            "source_line": self.source_line,
            "source_sha256": self.source_sha256,
            "file_version": self.file_version,
            "note": self.note,
        }

    def summary(self) -> str:
        """人读的一行摘要（回放终端输出用）。

        带上**阶段标识**（``raw_value`` 这种机器可读的短名）—— 终端回显
        也能被 grep / 脚本按阶段定位。
        """
        head = f"{self.step_no}/{len(STAGE_ORDER)} {self.stage_label}（{self.stage}）"
        if self.title:
            head += f" · {self.title}"
        return head


@dataclass(frozen=True)
class JudgementRecord:
    """一条事件的**判定记录** —— 结论 + 依据 + 完整判定链。

    这条记录是「为什么这条被判为危急」的**可复核凭据**：
    落库后任何人都能按事件 ID 取回，逐步复算，不必再问「当时谁说的」。
    """

    event_id: str
    decision: str
    reason: str
    item_code: str
    item_name: str = ""
    value: str = ""
    unit: str = ""
    converted_value: str = ""
    converted_unit: str = ""
    rule_id: str = ""
    threshold_locator: str = ""
    source_name: str = ""
    source_sha256: str = ""
    source_line: Optional[int] = None
    file_version: str = ""
    #: 阈值表**材料指纹** —— 材料换没换，看这一位（规则集快照 ID 随进程变化，
    #: 不能拿来判「结论能不能复算」）。
    threshold_source_sha256: str = ""
    reference_band_kind: str = ""
    reference_band_confidence: str = ""
    reference_band_note: str = ""
    obx_flag_disagreement: bool = False
    flag_disagreement_note: str = ""
    near_boundary: bool = False
    boundary_note: str = ""
    ruleset_snapshot_id: str = ""
    config_version: int = 0
    observed_at: str = ""
    specimen_no: str = ""
    dept_code: str = ""
    steps: tuple[ExplainStep, ...] = ()
    recorded_at: str = ""

    # -- 取值 ---------------------------------------------------------------
    def step(self, stage: str) -> Optional[ExplainStep]:
        """按阶段取那一段。"""
        for item in self.steps:
            if item.stage == stage:
                return item
        return None

    @property
    def requires_human_review(self) -> bool:
        return requires_human_review(self.decision)

    @property
    def original_value(self) -> str:
        """原始值（含单位），人读用。"""
        unit = self.unit or ""
        return f"{self.value} {unit}".strip()

    @property
    def converted_text(self) -> str:
        """换算值（含单位）；没换算时给空串。"""
        if not self.converted_value:
            return ""
        return f"{self.converted_value} {self.converted_unit}".strip()

    def compare_fields(self) -> dict[str, Any]:
        """参与「回放 vs 落库」逐字段比对的字段视图。"""
        return {name: getattr(self, name) for name in REPLAY_COMPARE_FIELDS}

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "event_id": self.event_id,
            "decision": self.decision,
            "reason": self.reason,
            "item_code": self.item_code,
            "item_name": self.item_name,
            "value": self.value,
            "unit": self.unit,
            "converted_value": self.converted_value,
            "converted_unit": self.converted_unit,
            "rule_id": self.rule_id,
            "threshold_locator": self.threshold_locator,
            "source_name": self.source_name,
            "source_sha256": self.source_sha256,
            "source_line": self.source_line,
            "file_version": self.file_version,
            "threshold_source_sha256": self.threshold_source_sha256,
            "reference_band_kind": self.reference_band_kind,
            "reference_band_confidence": self.reference_band_confidence,
            "reference_band_note": self.reference_band_note,
            "obx_flag_disagreement": self.obx_flag_disagreement,
            "flag_disagreement_note": self.flag_disagreement_note,
            "near_boundary": self.near_boundary,
            "boundary_note": self.boundary_note,
            "ruleset_snapshot_id": self.ruleset_snapshot_id,
            "config_version": self.config_version,
            "observed_at": self.observed_at,
            "specimen_no": self.specimen_no,
            "dept_code": self.dept_code,
            "recorded_at": self.recorded_at,
            "steps": [step.to_dict() for step in self.steps],
        }
        assert_no_patient_detail(payload)
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "JudgementRecord":
        """从落库的 JSON 载荷还原（回放比对用）。"""
        steps = tuple(
            ExplainStep(
                step_no=int(item.get("step_no", 0)),
                stage=str(item.get("stage", "")),
                title=str(item.get("title", "")),
                input=dict(item.get("input", {}) or {}),
                output=dict(item.get("output", {}) or {}),
                basis=str(item.get("basis", "")),
                rule_id=str(item.get("rule_id", "")),
                source_name=str(item.get("source_name", "")),
                source_line=item.get("source_line"),
                source_sha256=str(item.get("source_sha256", "")),
                file_version=str(item.get("file_version", "")),
                note=str(item.get("note", "")),
            )
            for item in payload.get("steps", []) or []
        )
        return cls(
            event_id=str(payload.get("event_id", "")),
            decision=str(payload.get("decision", "")),
            reason=str(payload.get("reason", "")),
            item_code=str(payload.get("item_code", "")),
            item_name=str(payload.get("item_name", "")),
            value=str(payload.get("value", "")),
            unit=str(payload.get("unit", "")),
            converted_value=str(payload.get("converted_value", "")),
            converted_unit=str(payload.get("converted_unit", "")),
            rule_id=str(payload.get("rule_id", "")),
            threshold_locator=str(payload.get("threshold_locator", "")),
            source_name=str(payload.get("source_name", "")),
            source_sha256=str(payload.get("source_sha256", "")),
            source_line=payload.get("source_line"),
            file_version=str(payload.get("file_version", "")),
            threshold_source_sha256=str(payload.get("threshold_source_sha256", "")),
            reference_band_kind=str(payload.get("reference_band_kind", "")),
            reference_band_confidence=str(payload.get("reference_band_confidence", "")),
            reference_band_note=str(payload.get("reference_band_note", "")),
            obx_flag_disagreement=bool(payload.get("obx_flag_disagreement", False)),
            flag_disagreement_note=str(payload.get("flag_disagreement_note", "")),
            near_boundary=bool(payload.get("near_boundary", False)),
            boundary_note=str(payload.get("boundary_note", "")),
            ruleset_snapshot_id=str(payload.get("ruleset_snapshot_id", "")),
            config_version=int(payload.get("config_version", 0) or 0),
            observed_at=str(payload.get("observed_at", "")),
            specimen_no=str(payload.get("specimen_no", "")),
            dept_code=str(payload.get("dept_code", "")),
            steps=steps,
            recorded_at=str(payload.get("recorded_at", "")),
        )


# ---------------------------------------------------------------------------
# 生成
# ---------------------------------------------------------------------------


def _dec(value: Any) -> str:
    """Decimal → 文本（保精度，不做科学计数）。"""
    if value is None:
        return ""
    if isinstance(value, Decimal):
        return format(value, "f")
    return str(value)


def _scope_text(row: ThresholdRow) -> str:
    """规则适用人群的人话描述。"""
    parts: list[str] = []
    if row.applies_sex:
        parts.append(f"性别 {row.applies_sex}")
    if row.applies_age_label:
        parts.append(f"年龄段 {row.applies_age_label}")
    elif row.applies_age_min is not None or row.applies_age_max is not None:
        low = row.applies_age_min if row.applies_age_min is not None else "不限"
        high = row.applies_age_max if row.applies_age_max is not None else "不限"
        parts.append(f"年龄 {low}~{high} 岁")
    return "、".join(parts) or "通用（无人群分档）"


def _threshold_hits(row: ThresholdRow, compared: Decimal) -> tuple[bool, bool]:
    """低侧 / 高侧是否触发（闭开区间语义与判定引擎一致）。"""
    low_hit = row.low_critical is not None and (
        compared <= row.low_critical if row.low_inclusive else compared < row.low_critical
    )
    high_hit = row.high_critical is not None and (
        compared >= row.high_critical if row.high_inclusive else compared > row.high_critical
    )
    return low_hit, high_hit


def build_judgement_record(
    event: CriticalEvent,
    judgement: Judgement,
    *,
    threshold_source_sha256: str = "",
) -> JudgementRecord:
    """把一次判定展开成**可逐步复算**的判定记录。

    记录里的每一段都只由 ``event`` 与 ``judgement`` 两样事实推导出来 ——
    不再回头查材料、也不重跑判定，所以「落库的记录」与「回放重算的记录」
    可以逐字段对齐。
    """
    row = judgement.threshold_row
    citation = row.citation if row is not None else None
    compared = (
        judgement.converted_value
        if judgement.converted_value is not None
        else judgement.value
    )
    steps: list[ExplainStep] = []

    # 1 · 原始值 ---------------------------------------------------------
    steps.append(
        ExplainStep(
            step_no=1,
            stage=STAGE_RAW_VALUE,
            title=f"{event.item_code} {event.item_name}".strip(),
            input={
                "item_code": event.item_code,
                "item_name": event.item_name,
                "value": _dec(event.value),
                "unit": event.unit,
                "observed_at": event.observed_at.isoformat()
                if hasattr(event.observed_at, "isoformat")
                else str(event.observed_at),
                "obx_flag": event.obx_flag,
                "source_channel": event.source_channel,
                "result_status": event.result_status,
            },
            output={
                "原始值": _dec(event.value),
                "单位": event.unit,
                "观察时刻": event.observed_at.isoformat()
                if hasattr(event.observed_at, "isoformat")
                else str(event.observed_at),
            },
            basis=(
                f"LIS {event.source_channel} 通道上报的原值（换算前）"
                + ("；本条为修正结果（OBR-25=C）" if event.result_status == "C" else "")
            ),
        )
    )

    # 2 · 命中规则 --------------------------------------------------------
    if row is not None:
        steps.append(
            ExplainStep(
                step_no=2,
                stage=STAGE_RULE_SELECTION,
                title=judgement.rule_id or "",
                input={
                    "item_code": event.item_code,
                    "observed_at": event.observed_at.isoformat()
                    if hasattr(event.observed_at, "isoformat")
                    else str(event.observed_at),
                },
                output={
                    "命中规则": judgement.rule_id or "",
                    "危急方向": row.direction,
                    "适用范围": _scope_text(row),
                    "阈值表版本": row.version,
                    "生效期": str(row.effective_from or ""),
                },
                basis=citation.locator if citation is not None else "",
                rule_id=judgement.rule_id or "",
                source_name=citation.source_name if citation is not None else "",
                source_line=citation.line_no if citation is not None else None,
                source_sha256=citation.source_sha256 if citation is not None else "",
                file_version=(citation.file_version or "") if citation is not None else "",
            )
        )
    else:
        steps.append(
            ExplainStep(
                step_no=2,
                stage=STAGE_RULE_SELECTION,
                title="未命中规则",
                input={"item_code": event.item_code},
                output={"命中规则": "", "selected": False},
                basis=judgement.reason,
                note="没有可用规则 —— 判定结论为不判定并转人工",
            )
        )

    # 3 · 单位换算 --------------------------------------------------------
    threshold_unit = row.unit if row is not None else ""
    if judgement.converted_value is not None:
        step = Decimal(1).scaleb(-judgement.converted_precision) if (
            judgement.converted_precision is not None
        ) else None
        same_unit = bool(threshold_unit) and threshold_unit == event.unit
        steps.append(
            ExplainStep(
                step_no=3,
                stage=STAGE_UNIT_CONVERSION,
                title=f"{_dec(judgement.value)} {event.unit} → "
                f"{_dec(judgement.converted_value)} {judgement.converted_unit}",
                input={
                    "原始值": _dec(judgement.value),
                    "原始单位": event.unit,
                    "阈值单位": threshold_unit,
                },
                output={
                    "换算值": _dec(judgement.converted_value),
                    "换算单位": judgement.converted_unit or "",
                    "精度位数": judgement.converted_precision,
                    "精度步长": _dec(step) if step is not None else "",
                    "换算方式": "同单位比较" if same_unit else "查表换算",
                },
                basis=judgement.conversion_note or "按院内单位换算表换算",
            )
        )
    else:
        steps.append(
            ExplainStep(
                step_no=3,
                stage=STAGE_UNIT_CONVERSION,
                title="未换算",
                input={
                    "原始值": _dec(judgement.value),
                    "原始单位": event.unit,
                    "阈值单位": threshold_unit,
                },
                output={"换算值": "", "换算方式": "未换算"},
                basis=(
                    judgement.conversion_note
                    or (
                        "上报单位与阈值单位一致，无需换算"
                        if threshold_unit and event.unit
                        else "单位信息不全，无法换算"
                    )
                ),
                note=(
                    "单位不可换算 → 不判定（转人工）"
                    if judgement.decision == "unit_unknown"
                    else ""
                ),
            )
        )

    # 4 · 参考区间分档 ----------------------------------------------------
    band = judgement.reference_band
    band_output: dict[str, Any] = {
        "参考区间原文": (band.raw if band is not None else (
            row.reference_range_raw if row is not None else ""
        )),
        "分档类型": (band.kind if band is not None else ""),
        "置信度": judgement.reference_band_confidence,
        "说明": judgement.reference_band_note,
    }
    if band is not None and band.kind == "numeric":
        band_output["换算值是否落在区间内"] = band.contains(compared)
    steps.append(
        ExplainStep(
            step_no=4,
            stage=STAGE_REFERENCE_BAND,
            title=(band.label or band.raw) if band is not None else "无参考区间",
            input={
                "参考区间原文": (band.raw if band is not None else (
                    row.reference_range_raw if row is not None else ""
                )),
            },
            output=band_output,
            basis=(
                "参考区间按性别 / 年龄段分档选取；参考区间只作补充信息，判定权威仍是阈值表"
            ),
        )
    )

    # 5 · 阈值比较 --------------------------------------------------------
    if row is not None:
        low_hit, high_hit = _threshold_hits(row, compared)
        steps.append(
            ExplainStep(
                step_no=5,
                stage=STAGE_THRESHOLD_COMPARE,
                title=row.threshold_text(),
                input={
                    "比较值": _dec(compared),
                    "比较单位": judgement.converted_unit or event.unit,
                    "低危急值": _dec(row.low_critical),
                    "高危急值": _dec(row.high_critical),
                    "低侧闭区间": row.low_inclusive,
                    "高侧闭区间": row.high_inclusive,
                },
                output={
                    "低侧触发": low_hit,
                    "高侧触发": high_hit,
                    "是否触发危急阈值": low_hit or high_hit,
                    "阈值行": citation.locator if citation is not None else "",
                },
                basis=(
                    f"阈值行 {citation.locator}"
                    if citation is not None
                    else "阈值行"
                ),
                rule_id=judgement.rule_id or "",
                source_name=citation.source_name if citation is not None else "",
                source_line=citation.line_no if citation is not None else None,
                source_sha256=citation.source_sha256 if citation is not None else "",
                file_version=(citation.file_version or "") if citation is not None else "",
                note=judgement.boundary_note,
            )
        )
    else:
        steps.append(
            ExplainStep(
                step_no=5,
                stage=STAGE_THRESHOLD_COMPARE,
                title="未比较",
                input={"比较值": _dec(compared)},
                output={"低侧触发": False, "高侧触发": False, "是否触发危急阈值": False},
                basis="没有可用阈值行，本次未做阈值比较",
            )
        )

    # 6 · 异常标志交叉校验 ------------------------------------------------
    steps.append(
        ExplainStep(
            step_no=6,
            stage=STAGE_FLAG_CROSSCHECK,
            title=f"OBX-8 = {event.obx_flag or '（空）'}",
            input={"异常标志": event.obx_flag, "判定结论": judgement.decision},
            output={
                "口径分歧": judgement.obx_flag_disagreement,
                "分歧说明": judgement.flag_disagreement_note,
            },
            basis=(
                "LIS 异常标志标的是「偏离参考区间」，不是「到没到危急值阈值」；"
                "两边对不上时以院内阈值表为准，分歧只记录、不改判"
            ),
        )
    )

    # 7 · 结论 ------------------------------------------------------------
    steps.append(
        ExplainStep(
            step_no=7,
            stage=STAGE_CONCLUSION,
            title=judgement.decision,
            input={
                "是否触发危急阈值": (
                    steps[4].output.get("是否触发危急阈值") if len(steps) > 4 else False
                ),
                "口径分歧": judgement.obx_flag_disagreement,
            },
            output={
                "结论": judgement.decision,
                "理由": judgement.reason,
                "需转人工": requires_human_review(judgement.decision),
            },
            basis="判定权威 = 院内阈值表；结论后需人工确认才成为已生效事实",
        )
    )

    record = JudgementRecord(
        event_id=event.event_id,
        decision=judgement.decision,
        reason=judgement.reason,
        item_code=judgement.item_code,
        item_name=event.item_name,
        value=_dec(judgement.value),
        unit=judgement.unit,
        converted_value=_dec(judgement.converted_value),
        converted_unit=judgement.converted_unit or "",
        rule_id=judgement.rule_id or "",
        threshold_locator=citation.locator if citation is not None else "",
        source_name=citation.source_name if citation is not None else "",
        source_sha256=citation.source_sha256 if citation is not None else "",
        source_line=citation.line_no if citation is not None else None,
        file_version=(citation.file_version or "") if citation is not None else "",
        threshold_source_sha256=threshold_source_sha256
        or (citation.source_sha256 if citation is not None else ""),
        reference_band_kind=band.kind if band is not None else "",
        reference_band_confidence=judgement.reference_band_confidence,
        reference_band_note=judgement.reference_band_note,
        obx_flag_disagreement=judgement.obx_flag_disagreement,
        flag_disagreement_note=judgement.flag_disagreement_note,
        near_boundary=judgement.near_boundary,
        boundary_note=judgement.boundary_note,
        ruleset_snapshot_id=judgement.ruleset_snapshot_id,
        config_version=judgement.config_version,
        observed_at=event.observed_at.isoformat()
        if hasattr(event.observed_at, "isoformat")
        else str(event.observed_at),
        specimen_no=event.specimen_no,
        dept_code=event.dept_code,
        steps=tuple(steps),
        recorded_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    # 判定记录里绝不能有患者可识别明细 —— 落盘前再扫一遍（含事件的可选字段）。
    payload = record.to_dict()
    assert_no_patient_detail(payload)
    return record


# ---------------------------------------------------------------------------
# 回放辅助
# ---------------------------------------------------------------------------


def event_from_row(row: Mapping[str, Any]) -> CriticalEvent:
    """把台账行还原成规范化事件（回放要用同一份入参重跑判定）。"""
    observed_raw = str(row.get("observed_at", "") or "")
    try:
        observed_at = datetime.fromisoformat(observed_raw)
    except ValueError:
        observed_at = datetime.now(timezone.utc)
    try:
        value = Decimal(str(row.get("value", "") or "0"))
    except Exception:  # noqa: BLE001 - 非数值结果原样留给判定层处理
        value = Decimal(0)
    return CriticalEvent(
        event_id=str(row.get("event_id", "")),
        message_control_id=str(row.get("message_control_id", "") or ""),
        specimen_no=str(row.get("specimen_no", "") or ""),
        item_code=str(row.get("item_code", "") or ""),
        item_name=str(row.get("item_name", "") or ""),
        value=value,
        unit=str(row.get("unit", "") or ""),
        observed_at=observed_at,
        dept_code=str(row.get("dept_code", "") or ""),
        obx_flag=str(row.get("obx_flag", "") or ""),
        result_status=str(row.get("result_status", "") or ""),
        patient_ref=str(row.get("patient_ref", "") or ""),
        source_channel=str(row.get("source_channel", "hl7") or "hl7"),
        item_code_source=str(row.get("item_code_source", "") or ""),
        item_code_confidence=str(row.get("item_code_confidence", "high") or "high"),
        supersedes=str(row.get("supersedes", "") or ""),
    )


#: 生产链路在结论理由后追加的「修正结果与既成事实冲突」说明的起始标记。
SUPERSEDE_REASON_MARKER = "修正结果 "


def normalize_reason(reason: str) -> str:
    """去掉生产链路追加的「修正结果冲突」后缀（回放比对用）。

    那段说明来自**事件上下文**（这条修正结果撞上了已被人工处置的旧事件），
    不是判定本身 —— 复核判定口径时不该把它算进「结论变了」。
    """
    index = reason.find(SUPERSEDE_REASON_MARKER)
    if index < 0:
        return reason
    return reason[:index].rstrip("；")


def replay_supersede_reason(store: Any, event: CriticalEvent) -> str:
    """重放时**只读**地复现「修正结果与既成事实冲突」那段理由。

    ``critval.judge.supersede.apply_supersede`` 会写台账（改旧事件状态、落审计），
    回放不能改历史事实，所以这里只**读**旧事件状态，把当初那段冲突说明
    原样复现出来 —— 否则回放会误报「结论理由不一致」。
    """
    from critval.judge.supersede import (
        SUPERSEDEABLE_STATUSES,
        find_superseded_candidate,
        is_correction_result,
    )

    if not is_correction_result(event):
        return ""
    old_event_id = find_superseded_candidate(store, event)
    if not old_event_id:
        return ""
    old_status = store.get_status(old_event_id)
    if not old_status or old_status in SUPERSEDEABLE_STATUSES:
        return ""
    return (
        f"修正结果 {event.event_id} 想取代旧事件 {old_event_id}，"
        f"但该事件已处于 {old_status}（值班人已处置或已归档），"
        f"不改写既成事实；本条修正结果照常落台账并转人工裁决"
    )


def diff_records(
    stored: JudgementRecord, replayed: JudgementRecord
) -> list[dict[str, Any]]:
    """逐字段比对「落库记录」与「回放重算记录」，返回差异清单。

    差异项形如 ``{"field": "decision", "stored": "critical", "replayed": "not_critical"}``。
    """
    differences: list[dict[str, Any]] = []
    left = stored.compare_fields()
    right = replayed.compare_fields()
    for name in REPLAY_COMPARE_FIELDS:
        if name == "reason":
            if normalize_reason(str(left.get(name) or "")) != normalize_reason(
                str(right.get(name) or "")
            ):
                differences.append(
                    {
                        "field": name,
                        "stored": left.get(name),
                        "replayed": right.get(name),
                    }
                )
            continue
        if left.get(name) != right.get(name):
            differences.append(
                {"field": name, "stored": left.get(name), "replayed": right.get(name)}
            )
    # 判定链各段的输出也逐段比 —— 结论一样但过程不一样，也要报出来。
    for stage in STAGE_ORDER:
        left_step = stored.step(stage)
        right_step = replayed.step(stage)
        if left_step is None or right_step is None:
            differences.append(
                {
                    "field": f"step.{stage}",
                    "stored": left_step.output if left_step else "(缺段)",
                    "replayed": right_step.output if right_step else "(缺段)",
                }
            )
            continue
        if left_step.output != right_step.output:
            differences.append(
                {
                    "field": f"step.{stage}",
                    "stored": left_step.output,
                    "replayed": right_step.output,
                }
            )
    return differences
