"""双路召回差异归因 —— 口径冲突 / 版本冲突 / 范围冲突（任务 id 33）。

## 为什么要归因，而不是挑一个

结构化精确路给出的是**阈值行**（数值口径：危急阈值是多少），词法 BM25 路给出
的是**制度条款**（流程口径：几分钟内上报、通知谁、记到哪里）。两路对**同一个
项目**给出的说法可能对不上 —— 这是院内真实的口径冲突：

* 制度里两处条款对同一个项目写了不同的上报时限（细则改了，总则没跟着改）；
* 制度正文写的危急阈值与阈值表那一行不一致（材料没同步）；
* 词法路按文字捞上来一条条款，那条条款点名的却是**别的项目**（范围不匹配）；
* 词法路捞到了**已失效**版本的制度条款（版本没跟着改）。

系统**不替值班人挑一个**：挑错一次就是按错时限处置一条危急值。归因层只做
三件事 —— 找出两方说法、说清差在哪、把两方出处都留着，交给值班人在确认页裁决。

## 四类归因

====================  ==========================================================
归因                  判定口径
====================  ==========================================================
``caliber`` 口径冲突  两路对**同一口径字段**给的取值不一致（时限 / 通知对象 /
                    记录去向 / 阈值数值）
``version`` 版本冲突  引用的依据**不是当前生效版本**
``scope``    范围冲突  条款点名的适用范围与本次项目不匹配
``none``     无冲突    两路说法一致（或只有一路有说法）
====================  ==========================================================

整体归因取**最严重**的一类，优先级 ``caliber > version > scope`` —— 口径冲突
最直接地决定"按哪个处置"，版本冲突让依据本身失效，范围冲突说明这条依据根本
不是给这个项目写的。

## 冲突必须有两方

只有**一条依据**有说法时**不构成冲突**（没有对立面，也就没有分歧）。冲突
成立需要**两条不同的依据**对同一口径给了不同取值。

两侧可能来自**不同的召回路径**（结构化路按项目码精确命中、词法路按文字命中），
也可能**都来自词法路** —— 制度里两处条款各写各的时限，是院内最常见的口径
打架。同一份依据被两路命中时只算**一方**（它没有对立面）。

## 冲突要落到值班台

归因结果挂到处置卡的**待人工裁决区块**（带两方出处行号），并写一条审计留痕。
冲突未裁决时，相关要素**不标「已解决」** —— 依据齐备不等于口径没争议。

## 数字口径怎么比

阈值表行给出的是**数值 + 开闭区间语义**（``< 30`` 与 ``≤ 30`` 不是一回事）；
制度条款正文写的是"低于 30 或高于 200 判定为危急值"。两边的**数值**与
**开闭语义**都要对上，才算同一口径。只比数值会把 ``< 30`` 与 ``≤ 30`` 当成
一致 —— 那正是最容易出事的临界点。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Optional, Sequence

from critval.models import Citation, ConflictItem, ConflictSide, PolicyClause, ThresholdRow

__all__ = [
    "AUDIT_CONFLICT_DETECTED",
    "CONFLICT_CALIBER",
    "CONFLICT_FIELDS",
    "CONFLICT_FIELD_LABELS",
    "CONFLICT_KIND_LABELS",
    "CONFLICT_KIND_PRIORITY",
    "CONFLICT_NONE",
    "CONFLICT_SCOPE",
    "CONFLICT_VERSION",
    "FIELD_DEADLINE",
    "FIELD_NOTIFY_TARGET",
    "FIELD_RECORD_DESTINATION",
    "FIELD_THRESHOLD_VALUE",
    "PATH_LEXICAL",
    "PATH_STRUCTURED",
    "ROLE_POLICY_CLAUSE",
    "ROLE_THRESHOLD_ROW",
    "ConflictEvidence",
    "attribute_conflicts",
    "classify_conflicts",
    "clause_evidence",
    "describe_conflict",
    "parse_policy_threshold",
    "threshold_evidence",
    "write_conflict_audit",
]

# —— 归因分类 ——
CONFLICT_CALIBER = "caliber"
CONFLICT_VERSION = "version"
CONFLICT_SCOPE = "scope"
CONFLICT_NONE = "none"

#: 归因分类 → 人读标签（值班台与 CLI 共用一份措辞）。
CONFLICT_KIND_LABELS: Mapping[str, str] = {
    CONFLICT_CALIBER: "口径冲突",
    CONFLICT_VERSION: "版本冲突",
    CONFLICT_SCOPE: "范围冲突",
    CONFLICT_NONE: "无冲突",
}

#: 整体归因的严重度顺序（取最靠前的一类作为整体结论）。
CONFLICT_KIND_PRIORITY: tuple[str, ...] = (CONFLICT_CALIBER, CONFLICT_VERSION, CONFLICT_SCOPE)

# —— 口径字段 ——
FIELD_THRESHOLD_VALUE = "threshold_value"
FIELD_DEADLINE = "deadline"
FIELD_NOTIFY_TARGET = "notify_target"
FIELD_RECORD_DESTINATION = "record_destination"

#: 参与冲突比对的口径字段（顺序即比对顺序）。
CONFLICT_FIELDS: tuple[str, ...] = (
    FIELD_THRESHOLD_VALUE,
    FIELD_DEADLINE,
    FIELD_NOTIFY_TARGET,
    FIELD_RECORD_DESTINATION,
)

#: 口径字段 → 人读标签。
CONFLICT_FIELD_LABELS: Mapping[str, str] = {
    FIELD_THRESHOLD_VALUE: "危急阈值",
    FIELD_DEADLINE: "上报时限",
    FIELD_NOTIFY_TARGET: "通知对象",
    FIELD_RECORD_DESTINATION: "记录去向",
}

# —— 召回路径与依据角色 ——
PATH_STRUCTURED = "structured"
PATH_LEXICAL = "lexical"

ROLE_THRESHOLD_ROW = "threshold_row"
ROLE_POLICY_CLAUSE = "policy_clause"

#: 冲突落库时的审计动作名。
AUDIT_CONFLICT_DETECTED = "recall_conflict_detected"


# ---------------------------------------------------------------------------
# 制度正文里的数值口径
# ---------------------------------------------------------------------------

#: 制度正文写"低于 X / 小于 X"（严格小于）。
_LOW_PATTERN = re.compile(r"(?:低于|小于)\s*([0-9]+(?:\.[0-9]+)?)")
#: 制度正文写"不高于 X / 小于等于 X"（含等号）。
_LOW_INCLUSIVE_PATTERN = re.compile(r"(?:不高于|不大于|小于等于|≤|<=)\s*([0-9]+(?:\.[0-9]+)?)")
#: 制度正文写"高于 X / 大于 X"（严格大于）。
_HIGH_PATTERN = re.compile(r"(?:高于|大于)\s*([0-9]+(?:\.[0-9]+)?)")
#: 制度正文写"不低于 X / 大于等于 X"（含等号）。
_HIGH_INCLUSIVE_PATTERN = re.compile(r"(?:不低于|不小于|大于等于|≥|>=)\s*([0-9]+(?:\.[0-9]+)?)")
#: 制度正文写"单位 g/L"——数值口径要带上单位，两方才可比。
_UNIT_PATTERN = re.compile(r"单位\s*([^\s，,；;。]+)")


@dataclass(frozen=True)
class PolicyThreshold:
    """制度正文里写出的危急阈值（数值 + 开闭语义 + 单位）。"""

    low: Optional[Decimal] = None
    high: Optional[Decimal] = None
    low_inclusive: bool = False
    high_inclusive: bool = False
    unit: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "low": str(self.low) if self.low is not None else None,
            "high": str(self.high) if self.high is not None else None,
            "low_inclusive": self.low_inclusive,
            "high_inclusive": self.high_inclusive,
            "unit": self.unit,
        }


def parse_policy_threshold(body: str, unit: str = "") -> PolicyThreshold:
    """从制度条款正文里解析出危急阈值口径。

    只认「低于 / 小于 / 不高于 / 高于 / 大于 / 不低于」这几种**成对出现**的
    危急表述 —— 参考区间（"115-150"）没有比较词，不会被误读成危急阈值。
    解析不出来就返回空的 ``PolicyThreshold``（不猜数）。
    """
    text = body or ""
    low: Optional[Decimal] = None
    high: Optional[Decimal] = None
    low_inclusive = False
    high_inclusive = False

    match = _LOW_PATTERN.search(text)
    if match:
        low = _to_decimal(match.group(1))
    else:
        match = _LOW_INCLUSIVE_PATTERN.search(text)
        if match:
            low = _to_decimal(match.group(1))
            low_inclusive = True

    match = _HIGH_PATTERN.search(text)
    if match:
        high = _to_decimal(match.group(1))
    else:
        match = _HIGH_INCLUSIVE_PATTERN.search(text)
        if match:
            high = _to_decimal(match.group(1))
            high_inclusive = True

    if low is None and high is None:
        return PolicyThreshold(unit=unit)
    stated_unit = ""
    unit_match = _UNIT_PATTERN.search(text)
    if unit_match:
        stated_unit = unit_match.group(1).strip()
    return PolicyThreshold(
        low=low,
        high=high,
        low_inclusive=low_inclusive,
        high_inclusive=high_inclusive,
        unit=stated_unit or unit,
    )


def _to_decimal(text: str) -> Optional[Decimal]:
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError):  # pragma: no cover - 正则已保证可解析
        return None


# ---------------------------------------------------------------------------
# 依据侧（冲突的一方）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConflictEvidence:
    """一路召回给出的**依据条目**（归因时只看它带的这些事实）。

    ``path`` 记这条路（``structured`` / ``lexical``），``role`` 记角色
    （``threshold_row`` / ``policy_clause``）。取值字段（阈值数值 / 上报时限 /
    通知对象 / 记录去向）按需填 —— 某一路没这个事实，就留空，**不编**。
    """

    path: str = ""
    role: str = ""
    stable_key: str = ""
    locator: str = ""
    citation: Optional[Citation] = None
    version: str = ""
    item_codes: tuple[str, ...] = ()
    item_names: tuple[str, ...] = ()
    body: str = ""
    threshold_low: Optional[Decimal] = None
    threshold_high: Optional[Decimal] = None
    threshold_unit: str = ""
    low_inclusive: bool = False
    high_inclusive: bool = False
    deadline_text: str = ""
    deadline_minutes: Optional[int] = None
    notify_target: str = ""
    record_destination: str = ""

    def value_for(self, field: str) -> str:
        """取某个口径字段的取值（没有就返回空串）。"""
        if field == FIELD_DEADLINE:
            return self.deadline_text.strip()
        if field == FIELD_NOTIFY_TARGET:
            return self.notify_target.strip()
        if field == FIELD_RECORD_DESTINATION:
            return self.record_destination.strip()
        if field == FIELD_THRESHOLD_VALUE:
            return self.threshold_text()
        return ""

    def threshold_text(self) -> str:
        """阈值口径的人话（与阈值表行的写法一致，便于两方对照）。"""
        low_op = "≤" if self.low_inclusive else "<"
        high_op = "≥" if self.high_inclusive else ">"
        unit = self.threshold_unit
        if self.threshold_low is not None and self.threshold_high is not None:
            return f"{low_op} {self.threshold_low} 或 {high_op} {self.threshold_high} {unit}".strip()
        if self.threshold_low is not None:
            return f"{low_op} {self.threshold_low} {unit}".strip()
        if self.threshold_high is not None:
            return f"{high_op} {self.threshold_high} {unit}".strip()
        return ""

    def to_side(self, value: str = "") -> ConflictSide:
        """转成冲突的一方（带出处）。"""
        return ConflictSide(
            path=self.path,
            role=self.role,
            stable_key=self.stable_key,
            locator=self.locator,
            value=value if value else self.value_for(""),
            citation=self.citation,
            version=self.version,
            item_codes=self.item_codes,
        )


def threshold_evidence(
    row: ThresholdRow,
    path: str = PATH_STRUCTURED,
    stable_key: str = "",
) -> ConflictEvidence:
    """阈值表行 → 依据侧（结构化精确路的主依据）。"""
    return ConflictEvidence(
        path=path,
        role=ROLE_THRESHOLD_ROW,
        stable_key=stable_key or f"threshold:{row.citation.source_name}:{row.citation.line_no}",
        locator=row.citation.locator,
        citation=row.citation,
        version=row.version,
        item_codes=(row.item_code,) if row.item_code else (),
        item_names=(row.item_name,) if row.item_name else (),
        threshold_low=row.low_critical,
        threshold_high=row.high_critical,
        threshold_unit=row.unit,
        low_inclusive=row.low_inclusive,
        high_inclusive=row.high_inclusive,
    )


def clause_evidence(
    clause: PolicyClause,
    path: str = PATH_LEXICAL,
    stable_key: str = "",
) -> ConflictEvidence:
    """制度条款 → 依据侧（词法路的主依据）。"""
    citation = clause.citation
    locator = citation.paragraph_no if citation.paragraph_no is not None else citation.line_no
    return ConflictEvidence(
        path=path,
        role=ROLE_POLICY_CLAUSE,
        stable_key=stable_key or f"policy:{citation.source_sha256}:{locator}",
        locator=citation.locator,
        citation=citation,
        version=citation.file_version or "",
        item_codes=clause.item_codes,
        item_names=clause.item_names,
        body=clause.body,
        deadline_text=clause.deadline_text,
        deadline_minutes=clause.deadline_minutes,
        notify_target=clause.notify_target,
        record_destination=clause.record_destination,
    )


# ---------------------------------------------------------------------------
# 归因
# ---------------------------------------------------------------------------


def _normalize_value(field: str, value: str) -> str:
    """口径取值归一（去掉空白与标点差异，"10 分钟内" 与 "10分钟内" 视为同一）。"""
    text = (value or "").strip()
    if not text:
        return ""
    if field == FIELD_DEADLINE:
        minutes = re.search(r"([0-9]+)\s*分钟", text)
        if minutes:
            return f"{int(minutes.group(1))}分钟"
    if field in (FIELD_NOTIFY_TARGET, FIELD_RECORD_DESTINATION):
        text = re.sub(r"[\s，,、；;：:。．\.]+", "", text)
    return text


#: 时限口径里"算得出来时长"的写法（含分钟 / 小时 / 天）。
_DURATION_PATTERN = re.compile(r"[0-9]+\s*(?:分钟|小时|天|秒)")


def _is_duration(value: str) -> bool:
    """这条时限是不是一个**时长**（"10 分钟内"算，"每日核对"不算）。"""
    return bool(_DURATION_PATTERN.search(value or ""))


def _conflict_id(kind: str, field: str, item_code: str, keys: Sequence[str]) -> str:
    basis = "|".join([kind, field, item_code, *sorted(keys)])
    return f"cf-{kind}-{hashlib.sha256(basis.encode('utf-8')).hexdigest()[:12]}"


def _describe(kind: str, field: str, item_code: str, sides: Sequence[ConflictSide]) -> str:
    """一句人话说明差在哪（各方取值与出处都写进去）。"""
    label = CONFLICT_KIND_LABELS.get(kind, kind)
    field_label = CONFLICT_FIELD_LABELS.get(field, field)
    parts = []
    for side in sides:
        who = "结构化精确路" if side.path == PATH_STRUCTURED else "词法 BM25 路"
        where = side.locator or side.stable_key
        section = ""
        if side.citation is not None and side.citation.section_path:
            section = "，" + " / ".join(side.citation.section_path)
        parts.append(f"{who}给的是「{side.value}」（{where}{section}）")
    joined = "；".join(parts)
    if kind == CONFLICT_CALIBER:
        return f"{field_label}口径不一致（项目 {item_code}）：{joined}"
    if kind == CONFLICT_VERSION:
        return f"引用了非生效版本（项目 {item_code}）：{joined}"
    if kind == CONFLICT_SCOPE:
        return f"条款适用范围与项目不匹配（项目 {item_code}）：{joined}"
    return f"{label}（项目 {item_code}）：{joined}"


def _make_conflict(
    kind: str,
    field: str,
    item_code: str,
    sides: Sequence[ConflictSide],
) -> ConflictItem:
    keys = [side.stable_key for side in sides]
    return ConflictItem(
        conflict_id=_conflict_id(kind, field, item_code, keys),
        kind=kind,
        field=field,
        item_code=item_code,
        description=_describe(kind, field, item_code, sides),
        sides=tuple(sides),
        status="pending",
    )


def _threshold_mismatch(
    left: ConflictEvidence,
    right: ConflictEvidence,
) -> bool:
    """两方的阈值口径是否真的不一致（数值 + 开闭语义都要比）。"""
    left_value = (
        left.threshold_low,
        left.threshold_high,
        left.low_inclusive,
        left.high_inclusive,
    )
    right_value = (
        right.threshold_low,
        right.threshold_high,
        right.low_inclusive,
        right.high_inclusive,
    )
    if left_value[0] is None and left_value[1] is None:
        return False
    if right_value[0] is None and right_value[1] is None:
        return False
    return left_value != right_value


def attribute_conflicts(
    item_code: str,
    structured: Sequence[ConflictEvidence] = (),
    lexical: Sequence[ConflictEvidence] = (),
    *,
    effective_threshold_version: str = "",
    effective_policy_version: str = "",
) -> list[ConflictItem]:
    """两路召回差异归因：产出待人工裁决的冲突清单。

    ``structured`` / ``lexical`` 分别是两路给出的依据条目。冲突成立的必要条件
    是**两路都开口且说法不同** —— 只有一路有说法不算冲突。

    产出三类冲突：

    * ``caliber`` —— 同一口径字段两路取值不一致（时限 / 通知对象 / 记录去向），
      以及阈值数值口径不一致（阈值表行 vs 制度正文写的危急阈值）；
    * ``version`` —— 引用了非生效版本的依据（版本号与院内声明的生效版本不符）；
    * ``scope`` —— 条款点名的适用范围与本次项目不匹配。

    返回顺序确定（版本 → 范围 → 口径；同类按口径字段顺序），便于逐位复现。
    """
    conflicts: list[ConflictItem] = []
    seen: set[str] = set()

    def _push(conflict: ConflictItem) -> None:
        if conflict.conflict_id in seen:
            return
        seen.add(conflict.conflict_id)
        conflicts.append(conflict)

    all_evidence = list(structured) + list(lexical)

    # —— 版本冲突：引用了非生效版本 ——
    for evidence in all_evidence:
        expected = ""
        if evidence.role == ROLE_THRESHOLD_ROW:
            expected = effective_threshold_version
        elif evidence.role == ROLE_POLICY_CLAUSE:
            expected = effective_policy_version
        if not expected or not evidence.version:
            continue
        if evidence.version == expected:
            continue
        side = ConflictSide(
            path=evidence.path,
            role=evidence.role,
            stable_key=evidence.stable_key,
            locator=evidence.locator,
            value=f"{evidence.version}（当前生效版本：{expected}）",
            citation=evidence.citation,
            version=evidence.version,
            item_codes=evidence.item_codes,
        )
        _push(_make_conflict(CONFLICT_VERSION, "version", item_code, [side]))

    # —— 范围冲突：条款点名的项目不是本次项目 ——
    threshold_evidences = [
        evidence
        for evidence in all_evidence
        if evidence.role == ROLE_THRESHOLD_ROW and item_code in evidence.item_codes
    ]
    for evidence in all_evidence:
        if evidence.role != ROLE_POLICY_CLAUSE:
            continue
        if not evidence.item_codes or item_code in evidence.item_codes:
            continue
        if not threshold_evidences:
            # 没有本次项目的权威范围依据，构不成"不匹配"，不硬凑一条冲突。
            continue
        counter = threshold_evidences[0]
        sides = [
            ConflictSide(
                path=evidence.path,
                role=evidence.role,
                stable_key=evidence.stable_key,
                locator=evidence.locator,
                value="、".join(evidence.item_codes),
                citation=evidence.citation,
                version=evidence.version,
                item_codes=evidence.item_codes,
            ),
            ConflictSide(
                path=counter.path,
                role=counter.role,
                stable_key=counter.stable_key,
                locator=counter.locator,
                value=item_code,
                citation=counter.citation,
                version=counter.version,
                item_codes=counter.item_codes,
            ),
        ]
        _push(_make_conflict(CONFLICT_SCOPE, "scope", item_code, sides))

    # —— 口径冲突：时限 / 通知对象 / 记录去向，同一项目下说法不一致 ——
    #
    # 对齐口径是「项目 + 口径字段」（口径字段即条款类型：时限 / 通知对象 /
    # 记录去向）。两侧可能来自不同的召回路径（结构化路按项目码精确命中、
    # 词法路按文字命中），也可能都来自词法路（制度里两处条款各写各的）。
    # 无论来自哪条路，只要**两条不同的依据**对同一口径给了不同取值，就是
    # 一条冲突 —— 两方（或多方）的取值与出处全部留着，不替值班人挑一个。
    for field in (FIELD_DEADLINE, FIELD_NOTIFY_TARGET, FIELD_RECORD_DESTINATION):
        sides: list[ConflictSide] = []
        for evidence in all_evidence:
            value = evidence.value_for(field)
            if not value:
                continue
            if field == FIELD_DEADLINE and not _is_duration(value):
                # "每日核对"这类是**复核周期**不是**上报时限**，不参与时限口径
                # 比较（否则制度里哪条都算时限，冲突清单会全是噪声）。
                continue
            sides.append(
                ConflictSide(
                    path=evidence.path,
                    role=evidence.role,
                    stable_key=evidence.stable_key,
                    locator=evidence.locator,
                    value=value,
                    citation=evidence.citation,
                    version=evidence.version,
                    item_codes=evidence.item_codes,
                )
            )
        # 同一条依据只算一次（两路都命中的条款不算"两方"）。
        unique: dict[str, ConflictSide] = {}
        for side in sides:
            unique.setdefault(side.stable_key, side)
        distinct = {
            _normalize_value(field, side.value) for side in unique.values()
        }
        if len(distinct) <= 1:
            continue
        _push(_make_conflict(CONFLICT_CALIBER, field, item_code, list(unique.values())))

    # —— 口径冲突：阈值数值（阈值表行 vs 制度正文写的危急阈值）——
    for left in structured:
        if left.role != ROLE_THRESHOLD_ROW:
            continue
        for right in lexical:
            if right.role != ROLE_POLICY_CLAUSE:
                continue
            if not _clause_covers(right, item_code):
                continue
            policy_threshold = parse_policy_threshold(right.body, right.threshold_unit)
            if policy_threshold.low is None and policy_threshold.high is None:
                continue
            policy_unit = policy_threshold.unit or right.threshold_unit or left.threshold_unit
            policy_evidence = ConflictEvidence(
                path=right.path,
                role=right.role,
                stable_key=right.stable_key,
                locator=right.locator,
                citation=right.citation,
                version=right.version,
                item_codes=right.item_codes,
                item_names=right.item_names,
                body=right.body,
                threshold_low=policy_threshold.low,
                threshold_high=policy_threshold.high,
                threshold_unit=policy_unit,
                low_inclusive=policy_threshold.low_inclusive,
                high_inclusive=policy_threshold.high_inclusive,
            )
            unit_mismatch = bool(
                left.threshold_unit and policy_unit and left.threshold_unit != policy_unit
            )
            if not unit_mismatch and not _threshold_mismatch(left, policy_evidence):
                continue
            sides = [
                ConflictSide(
                    path=left.path,
                    role=left.role,
                    stable_key=left.stable_key,
                    locator=left.locator,
                    value=left.threshold_text(),
                    citation=left.citation,
                    version=left.version,
                    item_codes=left.item_codes,
                ),
                ConflictSide(
                    path=right.path,
                    role=right.role,
                    stable_key=right.stable_key,
                    locator=right.locator,
                    value=policy_evidence.threshold_text(),
                    citation=right.citation,
                    version=right.version,
                    item_codes=right.item_codes,
                ),
            ]
            _push(_make_conflict(CONFLICT_CALIBER, FIELD_THRESHOLD_VALUE, item_code, sides))

    return conflicts


def _clause_covers(evidence: ConflictEvidence, item_code: str) -> bool:
    """这条条款说的是不是**本次**项目。

    判据是**本次项目码**出现在条款的点名项目码里或正文里 —— 不能拿条款
    自己的项目名去比（条款说"钾"不代表它管的是本次要查的项目）。
    """
    if item_code in evidence.item_codes:
        return True
    return bool(item_code) and item_code in (evidence.body or "")


def classify_conflicts(conflicts: Sequence[ConflictItem]) -> str:
    """整体归因分类（取最严重的一类；没有冲突 → ``none``）。"""
    kinds = {conflict.kind for conflict in conflicts if conflict.kind}
    for kind in CONFLICT_KIND_PRIORITY:
        if kind in kinds:
            return kind
    return CONFLICT_NONE


def describe_conflict(conflict: ConflictItem) -> str:
    """一条冲突的人话摘要（CLI 与审计详情共用）。"""
    label = CONFLICT_KIND_LABELS.get(conflict.kind, conflict.kind)
    field_label = CONFLICT_FIELD_LABELS.get(conflict.field, conflict.field)
    sides = " vs ".join(
        f"{'结构化路' if side.path == PATH_STRUCTURED else '词法路'}「{side.value}」"
        f"（{side.locator or side.stable_key}）"
        for side in conflict.sides
    )
    return f"[{label}] {field_label}：{sides}"


# ---------------------------------------------------------------------------
# 审计留痕
# ---------------------------------------------------------------------------


def write_conflict_audit(
    store: Any,
    conflicts: Sequence[ConflictItem],
    *,
    event_id: str = "",
    item_code: str = "",
    specimen_no: str = "",
    message_control_id: str = "",
    dept_code: str = "",
    actor: str = "system",
) -> list[int]:
    """把冲突写进审计流水，返回流水号列表。

    审计只落**标本号 / 项目码 / 消息控制号 / 科室 / 冲突摘要**，不落患者明细；
    两方出处（文件名 + 行号）写进 ``detail``，事后复核能追到具体哪两行打架。
    """
    written: list[int] = []
    for conflict in conflicts:
        detail = describe_conflict(conflict)
        for side in conflict.sides:
            if side.citation is not None:
                detail += f" ｜ 出处 {side.locator}"
        written.append(
            store.append_audit(
                AUDIT_CONFLICT_DETECTED,
                event_id=event_id,
                actor=actor,
                dept_code=dept_code,
                specimen_no=specimen_no,
                item_code=item_code or conflict.item_code,
                message_control_id=message_control_id,
                detail=detail,
            )
        )
    return written
