"""处置卡渲染：企微 Markdown / 值班台 HTML / 离线纯文本 + 字段白名单过滤。

三渠道共用**同一份要素数据**（``build_card_view``）—— 同一张卡在值班群、
在值班台、在打印纸上说的是同一件事，只是排版不同。排版一旦分叉，「群里
看到的」和「事后复核看到的」就可能对不上，所以要素数据只装配一次。

* ``render_markdown`` —— 企业微信群机器人。群机器人正文有长度上限，
  超长时**按要素截断**并标注「已截断，完整内容见值班台」，不静默丢字段；
* ``render_html`` —— 值班台页面片段。出处行号与阈值行可展开、版本与生效期
  可见、四要素状态徽标（``resolved`` / ``degraded`` / ``missing``）；
* ``render_text`` —— 离线纯文本。无 Markdown 无 HTML 标记，值班台不可用时
  打印或粘进工作群。

三道闸门叠在一起，任何一道不过就直接抛错，而不是靠人记得别写：

* ``assert_payload_whitelisted`` —— 载荷里出现白名单外的字段即抛错；
* ``assert_payload_patient_free`` —— 载荷任意字符串值出现患者可识别明细即抛错；
* ``assert_patient_free`` —— 渲染文本出现患者可识别明细即抛错。

四要素的 ``status``（``resolved`` / ``missing`` / ``degraded``）与 ``reason``
原因码随载荷一起给出，值班台与台账按它分流，不必从文案里猜。
"""

from __future__ import annotations

import html as html_lib
import unicodedata
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Iterable, Iterator, Mapping, Optional

from critval.card.provenance import CLEAN_ORIGINS, describe_provenance
from critval.models import CardElement, ConflictItem, DispositionCard

__all__ = [
    "CARD_ELEMENT_KEYS",
    "CARD_PAYLOAD_WHITELIST",
    "CITATION_RENDER_FIELDS",
    "CHANNELS",
    "CardView",
    "ConflictSideView",
    "ConflictView",
    "ElementView",
    "EVENT_RENDER_FIELDS",
    "JUDGEMENT_RENDER_FIELDS",
    "PATIENT_FORBIDDEN_PATTERNS",
    "PUSH_PAYLOAD_WHITELIST",
    "PayloadFieldLeak",
    "PayloadLeak",
    "PatientIdentifierLeak",
    "STATUS_LABELS",
    "TRUNCATION_MARK",
    "WECOM_FIELD_MAX_CHARS",
    "WECOM_MARKDOWN_MAX_CHARS",
    "assert_patient_free",
    "assert_payload_patient_free",
    "assert_payload_whitelisted",
    "build_card_view",
    "build_push_payload",
    "build_render_payload",
    "render_card",
    "render_html",
    "render_markdown",
    "render_text",
]

#: 处置卡上允许渲染的四要素（顺序即渲染顺序）。
CARD_ELEMENT_KEYS: tuple[str, ...] = (
    "threshold_basis",
    "notify_target",
    "deadline",
    "record_destination",
)

#: 事件侧允许渲染的字段白名单。
EVENT_RENDER_FIELDS: tuple[str, ...] = (
    "item_name",
    "item_code",
    "value",
    "unit",
    "specimen_no",
    "dept_code",
    "observed_at",
)

#: 判定结论侧允许渲染的字段白名单。
JUDGEMENT_RENDER_FIELDS: tuple[str, ...] = (
    "decision",
    "reason",
    "converted_value",
    "converted_unit",
    "rule_id",
    "ruleset_snapshot_id",
    "near_boundary",
    "boundary_note",
    "reference_band_confidence",
    "obx_flag_disagreement",
)

#: 要素条目的字段白名单。
ELEMENT_RENDER_FIELDS: tuple[str, ...] = (
    "key",
    "label",
    "value",
    "status",
    "reason",
    "locator",
    "note",
    "source",
)

#: 出处条目的字段白名单（``elements[].source`` 与根层 ``citations`` 共用）。
CITATION_RENDER_FIELDS: tuple[str, ...] = (
    "source_name",
    "source_sha256",
    "kind",
    "paragraph_no",
    "section_path",
    "line_no",
    "file_version",
    "locator",
    # 召回降级层级（任务 id 32）：0 原样 / 1 关键词扩展 / 2 章节级兜底。
    # 值班人据此看得出这条依据是直接命中的还是降级捞到的。
    "level",
)

#: 来源标注条目的字段白名单。
PROVENANCE_RENDER_FIELDS: tuple[str, ...] = (
    "source",
    "origin",
    "detail",
    "as_of",
    "level",
)

#: 冲突条目的字段白名单（待人工裁决区块）。
CONFLICT_RENDER_FIELDS: tuple[str, ...] = (
    "conflict_id",
    "kind",
    "kind_label",
    "field",
    "field_label",
    "item_code",
    "description",
    "status",
    "sides",
)

#: 冲突一方的字段白名单。
CONFLICT_SIDE_RENDER_FIELDS: tuple[str, ...] = (
    "path",
    "path_label",
    "role",
    "locator",
    "value",
    "version",
)

#: 卡片渲染载荷的白名单（按层级；``""`` 为根层）。
CARD_PAYLOAD_WHITELIST: dict[str, frozenset[str]] = {
    "": frozenset(
        {
            "card_id",
            "config_version",
            "event",
            "judgement",
            "elements",
            "citations",
            "provenance",
            "element_status",
            "missing_elements",
            "degraded_elements",
            "conflicts",
        }
    ),
    "event": frozenset(EVENT_RENDER_FIELDS),
    "judgement": frozenset(JUDGEMENT_RENDER_FIELDS),
    "elements": frozenset(ELEMENT_RENDER_FIELDS),
    "citations": frozenset(CITATION_RENDER_FIELDS),
    "source": frozenset(CITATION_RENDER_FIELDS),
    "provenance": frozenset(PROVENANCE_RENDER_FIELDS),
    "element_status": frozenset(CARD_ELEMENT_KEYS),
    "conflicts": frozenset(CONFLICT_RENDER_FIELDS),
    "sides": frozenset(CONFLICT_SIDE_RENDER_FIELDS),
}

#: 推送载荷（企业微信群机器人）的白名单。
PUSH_PAYLOAD_WHITELIST: dict[str, frozenset[str]] = {
    "": frozenset({"msgtype", "markdown"}),
    "markdown": frozenset({"content"}),
}

#: 患者标识层字段名 —— 出现即视为越界（结构上就拦住，不依赖文案检查）。
FORBIDDEN_PAYLOAD_KEYS: frozenset[str] = frozenset(
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

#: 患者可识别明细的禁用标签 —— 出现即视为渲染越界。
PATIENT_FORBIDDEN_PATTERNS: tuple[str, ...] = (
    "姓名",
    "身份证",
    "住院号",
    "病案号",
    "门诊号",
    "住院流水号",
)

#: 三种渲染渠道。
CHANNELS: tuple[str, ...] = ("markdown", "html", "text")

#: 企微群机器人正文长度上限（字符；官方限制为 4096 字节，按字符保守取齐）。
WECOM_MARKDOWN_MAX_CHARS = 4096

#: 企微正文里单个要素值的显示上限 —— 超出即按要素截断并标注。
WECOM_FIELD_MAX_CHARS = 180

#: 截断标注（三个渠道共用一句，避免各处措辞不一）。
TRUNCATION_MARK = "（已截断，完整内容见值班台）"

#: 四要素状态 → 人话标签。
STATUS_LABELS: dict[str, str] = {
    "resolved": "依据齐备",
    "degraded": "降级依据",
    "missing": "依据缺失",
}

#: 召回路径 → 人话标签（冲突区块要说清"是谁说的"）。
PATH_LABELS: dict[str, str] = {
    "structured": "结构化精确路",
    "lexical": "词法 BM25 路",
}

#: 归因分类 → 人话标签（与召回层同一份措辞，不两处各写）。
CONFLICT_KIND_LABELS: dict[str, str] = {
    "caliber": "口径冲突",
    "version": "版本冲突",
    "scope": "范围冲突",
    "none": "无冲突",
}

#: 口径字段 → 人话标签（与召回层同一份措辞）。
CONFLICT_FIELD_LABELS: dict[str, str] = {
    "threshold_value": "危急阈值",
    "deadline": "上报时限",
    "notify_target": "通知对象",
    "record_destination": "记录去向",
}


class PayloadLeak(RuntimeError):
    """载荷越界（字段不在白名单内，或含患者标识层字段）。"""


class PatientIdentifierLeak(PayloadLeak):
    """渲染产物里出现了患者可识别明细。"""


class PayloadFieldLeak(PayloadLeak):
    """载荷里出现了白名单之外的字段。"""


def assert_patient_free(text: str, extra_values: Iterable[str] = ()) -> None:
    """检查渲染文本里没有患者可识别明细。"""
    for pattern in PATIENT_FORBIDDEN_PATTERNS:
        if pattern in text:
            raise PatientIdentifierLeak(f"渲染产物出现患者可识别字段「{pattern}」")
    for value in extra_values:
        if value and len(value) >= 4 and value in text:
            raise PatientIdentifierLeak("渲染产物出现事件标识层的患者引用")


def _iter_strings(payload: Any) -> Iterator[str]:
    """递归取出载荷里的全部字符串值。"""
    if isinstance(payload, str):
        yield payload
    elif isinstance(payload, dict):
        for value in payload.values():
            yield from _iter_strings(value)
    elif isinstance(payload, (list, tuple)):
        for item in payload:
            yield from _iter_strings(item)


def assert_payload_patient_free(payload: Any, extra_values: Iterable[str] = ()) -> None:
    """载荷的任意字符串值都不许含患者可识别明细。"""
    for text in _iter_strings(payload):
        assert_patient_free(text, extra_values=extra_values)


def assert_payload_whitelisted(
    payload: Any,
    whitelist: dict[str, frozenset[str]] | None = None,
    _path: str = "",
) -> None:
    """载荷只允许白名单字段。

    白名单按层级给：根层用 ``""``，嵌套字典用其键名。出现白名单外的字段
    即抛 ``PayloadFieldLeak``；出现患者标识层字段名即抛
    ``PatientIdentifierLeak``（结构上就拦住，不等渲染时才发现）。
    """
    allowed_by_level = CARD_PAYLOAD_WHITELIST if whitelist is None else whitelist
    if isinstance(payload, dict):
        allowed = allowed_by_level.get(_path)
        for key, value in payload.items():
            if key in FORBIDDEN_PAYLOAD_KEYS:
                raise PatientIdentifierLeak(f"载荷含患者标识层字段「{key}」")
            if allowed is not None and key not in allowed:
                raise PayloadFieldLeak(f"载荷字段「{key}」不在白名单内（层级 {_path or '根'}）")
            assert_payload_whitelisted(value, allowed_by_level, key)
    elif isinstance(payload, (list, tuple)):
        for item in payload:
            assert_payload_whitelisted(item, allowed_by_level, _path)


def build_render_payload(card: DispositionCard) -> dict[str, Any]:
    """按白名单组装渲染载荷（未列入白名单的字段一律不进）。

    载荷是三渠道与卡片快照的**唯一数据源**：谁都不许绕过它另取一份数据，
    否则「群里看到的」与「快照里存的」会分叉。
    """
    event = card.event
    payload: dict[str, Any] = {
        "card_id": card.card_id,
        "config_version": card.config_version,
        "event": {key: getattr(event, key) for key in EVENT_RENDER_FIELDS},
        "judgement": {key: getattr(card.judgement, key) for key in JUDGEMENT_RENDER_FIELDS},
        "elements": [],
        "citations": [citation.to_dict() for citation in card.citations],
        "provenance": [],
        "element_status": {},
        "missing_elements": [],
        "degraded_elements": [],
    }
    for key in CARD_ELEMENT_KEYS:
        element: CardElement = getattr(card, key)
        payload["elements"].append(
            {
                "key": element.key,
                "label": element.label,
                "value": element.value,
                "status": element.status,
                "reason": element.reason,
                "locator": element.source.locator if element.source else "",
                "note": element.note,
                "source": element.source.to_dict() if element.source else None,
            }
        )
        payload["element_status"][element.key] = element.status
        if element.status == "missing":
            payload["missing_elements"].append(element.label)
        elif element.status == "degraded":
            payload["degraded_elements"].append(element.label)
    for provenance in card.provenance.entries():
        payload["provenance"].append(
            {
                "source": provenance.source,
                "origin": provenance.origin,
                "detail": provenance.detail,
                "as_of": provenance.as_of,
                "level": provenance.level,
            }
        )
    payload["conflicts"] = [_conflict_payload(conflict) for conflict in card.conflicts]
    assert_payload_whitelisted(payload)
    assert_payload_patient_free(payload, extra_values=(card.event.patient_ref,))
    return payload


def _conflict_payload(conflict: ConflictItem) -> dict[str, Any]:
    """一条冲突的渲染载荷（只带白名单字段）。"""
    return {
        "conflict_id": conflict.conflict_id,
        "kind": conflict.kind,
        "kind_label": CONFLICT_KIND_LABELS.get(conflict.kind, conflict.kind),
        "field": conflict.field,
        "field_label": CONFLICT_FIELD_LABELS.get(conflict.field, conflict.field),
        "item_code": conflict.item_code,
        "description": conflict.description,
        "status": conflict.status,
        "sides": [
            {
                "path": side.path,
                "path_label": PATH_LABELS.get(side.path, side.path),
                "role": side.role,
                "locator": side.locator,
                "value": side.value,
                "version": side.version,
            }
            for side in conflict.sides
        ],
    }


def build_push_payload(card: DispositionCard, confirm_url: str = "") -> dict[str, Any]:
    """推送载荷（企业微信群机器人 body）。

    载荷里只有 Markdown 正文 —— 事件字段不直接外发，避免白名单外的
    内容被顺带推出去。
    """
    content = render_markdown(card, confirm_url)
    payload: dict[str, Any] = {"msgtype": "markdown", "markdown": {"content": content}}
    assert_payload_whitelisted(payload, PUSH_PAYLOAD_WHITELIST)
    assert_payload_patient_free(payload, extra_values=(card.event.patient_ref,))
    return payload


def _format_value(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def _same_number(left: Any, right: Any) -> bool:
    """数值比较（换算值可能是 Decimal 也可能是字符串）。"""
    if isinstance(left, Decimal) and isinstance(right, Decimal):
        return left == right
    try:
        return Decimal(str(left)) == Decimal(str(right))
    except Exception:  # pragma: no cover - 非数值一律按不等处理
        return False


# ---------------------------------------------------------------------------
# 三渠道共用的要素视图
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ElementView:
    """一个要素在三渠道里的共同视图。"""

    key: str
    label: str
    value: str
    status: str
    reason: str
    note: str
    locator: str
    citation: Optional[Mapping[str, Any]] = None

    @property
    def status_label(self) -> str:
        return STATUS_LABELS.get(self.status, self.status)


@dataclass(frozen=True)
class ConflictSideView:
    """冲突一方的视图（谁说的、说了什么、依据在哪一行）。"""

    path_label: str
    role: str
    locator: str
    value: str
    version: str


@dataclass(frozen=True)
class ConflictView:
    """一条待人工裁决的冲突（三渠道共用同一份视图）。"""

    conflict_id: str
    kind: str
    kind_label: str
    field: str
    field_label: str
    description: str
    status: str
    sides: tuple[ConflictSideView, ...]

    @property
    def side_summary(self) -> str:
        """两方说法的单行摘要。"""
        return " vs ".join(
            f"{side.path_label}「{side.value}」" for side in self.sides
        )


@dataclass(frozen=True)
class CardView:
    """一张处置卡的**渠道无关**视图。

    三个渲染器都只读这份视图 —— 谁都不许自己去 card 上另取字段，
    否则同一张卡在三个渠道里会出现三种说法。
    """

    card_id: str
    title: str
    decision: str
    fields: tuple[tuple[str, str], ...]
    elements: tuple[ElementView, ...]
    header_notes: tuple[str, ...]
    confirm_url: str
    versions: tuple[tuple[str, str], ...]
    config_version: int
    ruleset_snapshot_id: str
    #: 待人工裁决的口径冲突（任务 id 33）。
    conflicts: tuple[ConflictView, ...] = ()


def _result_text(payload: Mapping[str, Any], card: DispositionCard) -> str:
    """结果行（单位写法不同但数值没变时不啰嗦）。"""
    event = payload["event"]
    judgement = payload["judgement"]
    result_text = _format_value(event["value"])
    converted = judgement["converted_value"]
    if converted is not None and not _same_number(converted, card.judgement.value):
        return f"{result_text} {event['unit']} → 换算 {converted} {judgement['converted_unit']}"
    return f"{result_text} {event['unit']}"


def _threshold_versions(card: DispositionCard) -> list[tuple[str, str]]:
    """阈值表版本与生效期（供值班台 HTML 的「版本与生效期」块）。"""
    row = card.judgement.threshold_row
    if row is None:
        return []
    out: list[tuple[str, str]] = []
    if row.version:
        out.append(("阈值表版本", row.version))
    if row.effective_from or row.effective_to:
        start = row.effective_from.isoformat() if row.effective_from else "未标注"
        end = row.effective_to.isoformat() if row.effective_to else "至今有效"
        out.append(("阈值表生效期", f"{start} ~ {end}"))
    return out


def _policy_version(card: DispositionCard) -> str:
    """制度版本（优先取制度条款出处上的版本号）。"""
    for citation in card.extra_citations:
        if citation.kind == "policy_clause" and citation.file_version:
            return citation.file_version
    for element in card.elements:
        if element.source is not None and element.source.file_version:
            return element.source.file_version
    return ""


def build_card_view(card: DispositionCard, confirm_url: str = "") -> CardView:
    """把处置卡装配成渠道无关的视图（三渠道共用同一份数据）。"""
    payload = build_render_payload(card)
    event = payload["event"]
    judgement = payload["judgement"]

    title = "危急值处置卡 · 待确认"
    if judgement["decision"] == "not_critical":
        title = "危急值处置卡 · 未触发危急阈值"

    fields: list[tuple[str, str]] = [
        ("项目", f"{event['item_name'] or '（未命名项目）'}（{event['item_code']}）"),
        ("结果", _result_text(payload, card)),
        ("标本号", event["specimen_no"]),
        ("科室", event["dept_code"] or "（消息未带科室）"),
        ("观察时间", event["observed_at"]),
        ("判定", judgement["reason"]),
    ]

    elements: list[ElementView] = []
    for item in payload["elements"]:
        elements.append(
            ElementView(
                key=item["key"],
                label=item["label"],
                value=item["value"],
                status=item["status"],
                reason=item["reason"],
                note=item["note"],
                locator=item["locator"],
                citation=item["source"],
            )
        )

    header_notes = tuple(
        describe_provenance_from_payload(item)
        for item in payload["provenance"]
        if item["origin"] not in CLEAN_ORIGINS
    )

    versions: list[tuple[str, str]] = _threshold_versions(card)
    policy_version = _policy_version(card)
    if policy_version:
        versions.append(("制度版本", policy_version))
    versions.append(("配置版本", str(card.config_version)))
    if card.judgement.ruleset_snapshot_id:
        versions.append(("规则集快照", card.judgement.ruleset_snapshot_id))

    conflicts = tuple(
        ConflictView(
            conflict_id=item["conflict_id"],
            kind=item["kind"],
            kind_label=item["kind_label"],
            field=item["field"],
            field_label=item["field_label"],
            description=item["description"],
            status=item["status"],
            sides=tuple(
                ConflictSideView(
                    path_label=side["path_label"],
                    role=side["role"],
                    locator=side["locator"],
                    value=side["value"],
                    version=side["version"],
                )
                for side in item["sides"]
            ),
        )
        for item in payload["conflicts"]
    )

    return CardView(
        card_id=card.card_id,
        title=title,
        decision=judgement["decision"],
        fields=tuple(fields),
        elements=tuple(elements),
        header_notes=header_notes,
        confirm_url=confirm_url,
        versions=tuple(versions),
        config_version=card.config_version,
        ruleset_snapshot_id=card.judgement.ruleset_snapshot_id,
        conflicts=conflicts,
    )


# ---------------------------------------------------------------------------
# 渠道一：企业微信群机器人 Markdown
# ---------------------------------------------------------------------------


def _truncate(text: Any, limit: int) -> tuple[str, bool]:
    """按长度截断；返回 ``(文本, 是否被截断)``。"""
    text = _format_value(text)
    if limit <= 0 or len(text) <= limit:
        return text, False
    return text[:limit].rstrip() + "…" + TRUNCATION_MARK, True


def _element_lines(view: CardView, field_limit: int) -> list[str]:
    """四要素渲染成 Markdown 行（``field_limit`` 控制单要素显示长度）。"""
    lines: list[str] = []
    for index, element in enumerate(view.elements, start=1):
        if element.status == "resolved":
            value, _ = _truncate(element.value, field_limit)
            lines.append(f"{index}. **{element.label}**：{value}")
            if element.locator:
                lines.append(f"   - 出处：{element.locator}")
            if element.note:
                lines.append(f"   - 备注：{element.note}")
        elif element.status == "degraded":
            value, _ = _truncate(element.value, field_limit)
            lines.append(f"{index}. **{element.label}**：{value}【降级依据】{element.note}")
            if element.locator:
                lines.append(f"   - 出处：{element.locator}")
        else:
            lines.append(f"{index}. **{element.label}**：【依据缺失】{element.note or '原因未记录'}")
    return lines


def _markdown_parts(view: CardView, field_limit: int) -> tuple[str, str, str]:
    """拆成 ``(头部, 正文, 尾部)`` —— 尾部（确认回执）永远不参与截断。"""
    header: list[str] = [f"**{view.title}**"]
    if view.header_notes:
        header.append("")
        header.append("**依据来源标注**")
        for text in view.header_notes:
            header.append(f"- {text}")

    body: list[str] = []
    for label, value in view.fields:
        shown, _ = _truncate(value, field_limit)
        body.append(f"- {label}：{shown}")
    body.append("")
    body.append("**处置依据（四要素）**")
    body.append("")
    body.extend(_element_lines(view, field_limit))

    if view.conflicts:
        body.append("")
        body.append(f"**待人工裁决（口径冲突 {len(view.conflicts)} 条）**")
        body.append("")
        for number, conflict in enumerate(view.conflicts, start=1):
            body.append(f"{number}. 【{conflict.kind_label}】{conflict.field_label}：{conflict.side_summary}")
            for side in conflict.sides:
                if side.locator:
                    body.append(f"   - {side.path_label}出处：{side.locator}")

    if view.confirm_url:
        footer = f"确认回执：{view.confirm_url}"
    else:
        footer = "确认回执：请在值班台确认本条处置"

    return "\n".join(header), "\n".join(body), footer


def render_markdown(card: DispositionCard, confirm_url: str = "") -> str:
    """渲染处置卡 Markdown（企业微信群机器人）。

    群机器人正文有长度上限：超长时**按要素截断**并标注「已截断，完整内容
    见值班台」—— 截断的是显示长度，不是依据本身，也不静默丢字段。
    """
    view = build_card_view(card, confirm_url)

    limit = WECOM_FIELD_MAX_CHARS
    header, body, footer = _markdown_parts(view, limit)
    text = f"{header}\n\n{body}\n\n{footer}"
    if len(text) <= WECOM_MARKDOWN_MAX_CHARS:
        assert_patient_free(text, extra_values=(card.event.patient_ref,))
        return text

    # 逐级收紧单要素显示长度，直到整卡落进企微正文上限。
    while limit > 40:
        limit //= 2
        header, body, footer = _markdown_parts(view, limit)
        text = f"{header}\n\n{body}\n\n{footer}"
        if len(text) <= WECOM_MARKDOWN_MAX_CHARS:
            break
    else:
        # 仍然超长：硬截正文，但**保留尾部确认回执**（值班人得点它）。
        budget = WECOM_MARKDOWN_MAX_CHARS - len(header) - len(footer) - 8
        if budget > 0 and len(body) > budget:
            body = body[:budget].rstrip() + "…（正文已截断，完整内容见值班台）"
        text = f"{header}\n\n{body}\n\n{footer}"

    assert_patient_free(text, extra_values=(card.event.patient_ref,))
    return text


# ---------------------------------------------------------------------------
# 渠道二：值班台 HTML
# ---------------------------------------------------------------------------


def _esc(value: Any) -> str:
    return html_lib.escape(_format_value(value), quote=True)


def _citation_items(citation: Optional[Mapping[str, Any]]) -> list[str]:
    """出处明细（展开后看到的字段）。"""
    if not citation:
        return []
    items: list[str] = []
    if citation.get("source_name"):
        items.append(f"文件：{citation['source_name']}")
    if citation.get("source_sha256"):
        items.append(f"内容指纹：{citation['source_sha256']}")
    if citation.get("line_no") is not None:
        items.append(f"行号：第 {citation['line_no']} 行")
    if citation.get("paragraph_no") is not None:
        items.append(f"段落序号：{citation['paragraph_no']}")
    section = citation.get("section_path") or []
    if section:
        items.append("章节路径：" + " / ".join(str(part) for part in section))
    if citation.get("file_version"):
        items.append(f"版本：{citation['file_version']}")
    return items


def render_html(card: DispositionCard, confirm_url: str = "") -> str:
    """渲染值班台 HTML 片段。

    出处行号与阈值行放在 ``<details>`` 里可展开；版本与生效期单列一块；
    四要素状态用徽标呈现（``resolved`` / ``degraded`` / ``missing``）。

    这里只出**结构与类名**，样式由值班台的设计令牌统一给（不在渲染层
    写死颜色，避免两处各有一套样式）。
    """
    view = build_card_view(card)
    parts: list[str] = []

    parts.append(
        f'<article class="critval-card" data-card-id="{_esc(view.card_id)}" '
        f'data-decision="{_esc(view.decision)}">'
    )
    parts.append('  <header class="critval-card__header">')
    parts.append(f'    <h2 class="critval-card__title">{_esc(view.title)}</h2>')
    meta = f"卡片编号 {view.card_id}"
    if view.ruleset_snapshot_id:
        meta += f" · 规则集快照 {view.ruleset_snapshot_id}"
    parts.append(f'    <p class="critval-card__meta">{_esc(meta)}</p>')
    parts.append("  </header>")

    if view.header_notes:
        parts.append('  <section class="critval-card__notes">')
        for text in view.header_notes:
            parts.append(
                '    <p class="critval-card__note critval-card__note--degraded">'
                f"{_esc(text)}</p>"
            )
        parts.append("  </section>")

    parts.append('  <dl class="critval-card__fields">')
    for label, value in view.fields:
        parts.append(
            '    <div class="critval-card__field">'
            f"<dt>{_esc(label)}</dt><dd>{_esc(value)}</dd></div>"
        )
    parts.append("  </dl>")

    parts.append('  <section class="critval-card__elements">')
    parts.append('    <h3 class="critval-card__section-title">处置依据（四要素）</h3>')
    parts.append('    <ol class="critval-card__element-list">')
    for element in view.elements:
        parts.append(
            f'      <li class="critval-card__element critval-card__element--{_esc(element.status)}" '
            f'data-key="{_esc(element.key)}">'
        )
        parts.append('        <div class="critval-card__element-head">')
        parts.append(
            f'          <span class="critval-card__badge critval-card__badge--{_esc(element.status)}">'
            f"{_esc(element.status_label)}</span>"
        )
        parts.append(
            f'          <span class="critval-card__element-label">{_esc(element.label)}</span>'
        )
        parts.append("        </div>")
        if element.status == "missing":
            parts.append(
                '        <p class="critval-card__element-value critval-card__element-value--missing">'
                f"【依据缺失】{_esc(element.note or '原因未记录')}</p>"
            )
        else:
            parts.append(
                '        <p class="critval-card__element-value">'
                f"{_esc(element.value)}"
                + ("【降级依据】" + _esc(element.note) if element.status == "degraded" else "")
                + "</p>"
            )
        if element.locator:
            parts.append('        <details class="critval-card__citation">')
            parts.append(f"          <summary>出处：{_esc(element.locator)}</summary>")
            items = _citation_items(element.citation)
            if items:
                parts.append('          <ul class="critval-card__citation-list">')
                for item in items:
                    parts.append(f"            <li>{_esc(item)}</li>")
                parts.append("          </ul>")
            parts.append("        </details>")
        if element.note and element.status != "missing":
            parts.append(
                f'        <p class="critval-card__element-note">备注：{_esc(element.note)}</p>'
            )
        parts.append("      </li>")
    parts.append("    </ol>")
    parts.append("  </section>")

    if view.versions:
        parts.append('  <section class="critval-card__versions">')
        parts.append('    <h3 class="critval-card__section-title">版本与生效期</h3>')
        parts.append('    <dl class="critval-card__version-list">')
        for label, value in view.versions:
            parts.append(
                f'      <div class="critval-card__version"><dt>{_esc(label)}</dt>'
                f"<dd>{_esc(value)}</dd></div>"
            )
        parts.append("    </dl>")
        parts.append("  </section>")

    if view.conflicts:
        parts.append('  <section class="critval-card__conflicts">')
        parts.append(
            '    <h3 class="critval-card__section-title">'
            f"待人工裁决（口径冲突 {len(view.conflicts)} 条）</h3>"
        )
        parts.append('    <ul class="critval-card__conflict-list">')
        for conflict in view.conflicts:
            parts.append(
                f'      <li class="critval-card__conflict critval-card__conflict--{_esc(conflict.kind)}" '
                f'data-conflict-id="{_esc(conflict.conflict_id)}" '
                f'data-field="{_esc(conflict.field)}">'
            )
            parts.append(
                f'        <span class="critval-card__badge critval-card__badge--degraded">'
                f"{_esc(conflict.kind_label)}</span>"
            )
            parts.append(
                f'        <span class="critval-card__element-label">{_esc(conflict.field_label)}</span>'
            )
            parts.append(
                f'        <p class="critval-card__conflict-desc">{_esc(conflict.description)}</p>'
            )
            parts.append('        <ul class="critval-card__conflict-sides">')
            for side in conflict.sides:
                parts.append(
                    f'          <li class="critval-card__conflict-side">'
                    f"{_esc(side.path_label)}：{_esc(side.value)}"
                    f"（{_esc(side.locator or side.role)}）</li>"
                )
            parts.append("        </ul>")
            parts.append("      </li>")
        parts.append("    </ul>")
        parts.append("  </section>")

    parts.append('  <footer class="critval-card__footer">')
    if confirm_url:
        parts.append(
            f'    <a class="critval-card__confirm" href="{_esc(confirm_url)}">确认回执</a>'
        )
    else:
        parts.append('    <span class="critval-card__confirm-hint">请在值班台确认本条处置</span>')
    parts.append("  </footer>")
    parts.append("</article>")

    text = "\n".join(parts)
    assert_patient_free(text, extra_values=(card.event.patient_ref,))
    return text


# ---------------------------------------------------------------------------
# 渠道三：离线纯文本
# ---------------------------------------------------------------------------

_TEXT_WIDTH = 64


def _display_width(text: str) -> int:
    """显示宽度（中文按两列算）—— 纯文本版要对齐得整齐。"""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def _pad(label: str, width: int) -> str:
    return label + " " * max(0, width - _display_width(label))


def _text_line(left: str, right: str) -> str:
    """``标签 : 值`` 一行（标签左对齐到固定宽度）。"""
    return f"{_pad(left, 10)}: {right}"


def render_text(card: DispositionCard, confirm_url: str = "") -> str:
    """渲染离线纯文本处置卡。

    无 Markdown、无 HTML 标记 —— 值班台不可用时打印出来贴在值班台，
    或粘进工作群。出处行号与确认链接都在里面。
    """
    view = build_card_view(card)
    rule = "═" * _TEXT_WIDTH
    thin = "─" * _TEXT_WIDTH
    lines: list[str] = [view.title, rule]

    for label, value in view.fields:
        lines.append(_text_line(label, value))

    if view.header_notes:
        lines.append(thin)
        lines.append("依据来源标注")
        for text in view.header_notes:
            lines.append(f"  - {text}")

    lines.append(thin)
    lines.append("处置依据（四要素）")
    for index, element in enumerate(view.elements, start=1):
        if element.status == "missing":
            lines.append(f"{index}. {element.label}  [{element.status_label}]")
            lines.append(f"   说明    : 【依据缺失】{element.note or '原因未记录'}")
            continue
        lines.append(f"{index}. {element.label}  [{element.status_label}]")
        lines.append(f"   值      : {element.value}")
        if element.locator:
            lines.append(f"   出处    : {element.locator}")
        if element.note:
            lines.append(f"   备注    : {element.note}")

    if view.versions:
        lines.append(thin)
        lines.append("版本与生效期")
        for label, value in view.versions:
            lines.append(f"   {_pad(label, 10)}: {value}")

    if view.conflicts:
        lines.append(thin)
        lines.append(f"待人工裁决（口径冲突 {len(view.conflicts)} 条）")
        for number, conflict in enumerate(view.conflicts, start=1):
            lines.append(f"{number}. [{conflict.kind_label}] {conflict.field_label}")
            for side in conflict.sides:
                lines.append(f"   {side.path_label} : {side.value}")
                if side.locator:
                    lines.append(f"   出处        : {side.locator}")

    lines.append(thin)
    if confirm_url:
        lines.append(f"确认回执 : {confirm_url}")
    else:
        lines.append("确认回执 : 请在值班台确认本条处置")

    text = "\n".join(lines)
    assert_patient_free(text, extra_values=(card.event.patient_ref,))
    return text


def render_card(card: DispositionCard, channel: str = "markdown", confirm_url: str = "") -> str:
    """按渠道名渲染（``markdown`` / ``html`` / ``text``）。"""
    if channel == "markdown":
        return render_markdown(card, confirm_url)
    if channel == "html":
        return render_html(card, confirm_url)
    if channel == "text":
        return render_text(card, confirm_url)
    raise ValueError(f"未知渲染渠道：{channel!r}（可选：{', '.join(CHANNELS)}）")


def describe_provenance_from_payload(item: dict[str, Any]) -> str:
    """把载荷里的来源条目翻译成一句人话。"""
    from critval.models import Provenance

    provenance = Provenance(
        source=item["source"],
        origin=item["origin"],
        detail=item["detail"],
        as_of=item["as_of"],
        level=item["level"],
    )
    return describe_provenance(provenance)
