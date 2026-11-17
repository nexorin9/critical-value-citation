"""阈值表 / 制度的字段规范：列名别名映射 + 取值类型。

院内表格的列名各家写法不一（"项目码" / "检验项目代码" / "ITEM_CODE"），
本模块把这些写法收敛到**规范字段名**，其余列名进入解析报告的
``unrecognized_columns``，由人工确认（不做静默丢弃）。

字段规范同时是 ``docs/material_spec.md`` 的唯一真源 —— 交付文档里的
列名要求由这里的别名表生成，不会与代码脱节。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

__all__ = [
    "AgeScope",
    "FieldSpec",
    "POLICY_LABELS",
    "THRESHOLD_FIELDS",
    "normalize_header",
    "parse_applies_age",
    "parse_applies_sex",
    "parse_date",
    "parse_decimal",
    "parse_threshold_value",
    "resolve_threshold_columns",
]


#: 表头归一化时剔除的字符（空格、全角空格、括号、连接符等）。
_STRIP_CHARS = re.compile(r"[\s　()（）\[\]【】_.\-、,，:：;；/\\]+")

_NUM_PATTERN = re.compile(r"^[<>≤≥＜＞]?\s*([+-]?\d+(?:\.\d+)?)")

#: 带比较符的阈值写法。``<=`` / ``≤`` 落取等号语义（等于阈值也算危急），
#: 裸数字按院内习惯取**严格**不等（等于阈值不算危急）。
_COMPARATOR_PATTERN = re.compile(
    r"^\s*(<=|>=|≤|≥|＜|＞|<|>|=)\s*([+-]?\d+(?:\.\d+)?)"
)


def normalize_header(text: Any) -> str:
    """把表头归一化成可比对的键：小写、去空格与标点。"""
    if text is None:
        return ""
    raw = str(text).strip()
    if not raw:
        return ""
    return _STRIP_CHARS.sub("", raw).lower()


@dataclass(frozen=True)
class FieldSpec:
    """一个规范字段。"""

    canonical: str
    label: str
    aliases: tuple[str, ...]
    required: bool = False
    kind: str = "text"  # text | number | date
    description: str = ""


@dataclass(frozen=True)
class AgeScope:
    """年龄段分档。

    ``specified`` = 表里是否写了年龄段；``parsed`` = 写了但看不懂
    （``parsed=False`` 时该行不得进入判定集，只能转人工）。
    """

    age_min: Optional[int] = None
    age_max: Optional[int] = None
    label: str = ""
    specified: bool = False
    parsed: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "age_min": self.age_min,
            "age_max": self.age_max,
            "label": self.label,
            "specified": self.specified,
            "parsed": self.parsed,
        }


#: 阈值表规范字段（顺序即建议的表头顺序）。
THRESHOLD_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec(
        "item_code",
        "项目码",
        ("项目码", "项目编码", "项目代码", "检验项目代码", "检验代码", "项目编号", "itemcode", "itemcode"),
        required=True,
        kind="text",
        description="检验项目码，阈值表主键；与 OBX-3 对应",
    ),
    FieldSpec(
        "item_name",
        "项目名",
        ("项目名", "项目名称", "检验项目名称", "项目中文名", "名称", "itemname"),
        kind="text",
        description="检验项目中文名，用于展示与关键词扩展",
    ),
    FieldSpec(
        "unit",
        "单位",
        ("单位", "计量单位", "结果单位", "unit"),
        kind="text",
        description="阈值所用单位；与 LIS 上报单位不一致时按换算表换算",
    ),
    FieldSpec(
        "low_critical",
        "低危急值",
        ("低危急值", "低危急值下限", "低危值", "低危急界限", "低值危急界限", "lowcritical", "lowercritical"),
        kind="number",
        description="低于该值判为危急；缺该列表示该项目无低危急方向",
    ),
    FieldSpec(
        "high_critical",
        "高危急值",
        ("高危急值", "高危急值上限", "高危值", "高危急界限", "高值危急界限", "highcritical", "uppercritical"),
        kind="number",
        description="高于该值判为危急；缺该列表示该项目无高危急方向",
    ),
    FieldSpec(
        "reference_range",
        "参考区间",
        ("参考区间", "参考范围", "参考值", "正常范围", "referencerange", "refrange"),
        kind="text",
        description="参考区间文本，可分性别 / 年龄段；只作分档与展示，不作判定权威",
    ),
    FieldSpec(
        "effective_from",
        "生效日期",
        ("生效日期", "生效时间", "生效期", "启用日期", "effectivefrom", "effectivedate"),
        kind="date",
        description="该版本生效日期；用于按观察时刻选生效版本",
    ),
    FieldSpec(
        "version",
        "版本号",
        ("版本号", "版本", "版本标识", "文件版本", "version", "ver"),
        kind="text",
        description="阈值表版本号；处置卡上必须显示",
    ),
    FieldSpec(
        "effective_to",
        "失效日期",
        ("失效日期", "失效期", "有效期至", "停用日期", "effective_to", "effectiveto"),
        kind="date",
        description="该版本失效日期；观察时刻落在生效期与失效日期之间即版本空档，转人工",
    ),
    FieldSpec(
        "applies_sex",
        "适用性别",
        ("适用性别", "性别", "性别限定", "适用人群性别", "sex", "gender"),
        kind="text",
        description="该行阈值适用的人群（男 / 女 / 通用）；留空表示不按性别分档",
    ),
    FieldSpec(
        "applies_age",
        "适用年龄段",
        ("适用年龄段", "年龄段", "适用年龄", "年龄范围", "年龄", "age", "agerange"),
        kind="text",
        description="该行阈值适用的年龄段（如 18-60 / ≥65 / 儿童 / 成人）；留空表示不按年龄分档",
    ),
)

#: 制度条款里的事实字段标签（docx / pdf 抽取用）。
POLICY_LABELS: dict[str, tuple[str, ...]] = {
    "notify_target": ("通知对象", "报告对象", "接收对象", "告知对象"),
    "deadline": ("上报时限", "报告时限", "响应时限", "时限"),
    "record_destination": ("记录去向", "登记去向", "记录位置", "记录去处"),
}


def _alias_index() -> dict[str, str]:
    index: dict[str, str] = {}
    for spec in THRESHOLD_FIELDS:
        for alias in (spec.label, *spec.aliases):
            key = normalize_header(alias)
            if key:
                index.setdefault(key, spec.canonical)
    return index


_ALIAS_INDEX = _alias_index()


def resolve_threshold_columns(header_cells: list[Any]) -> tuple[dict[str, int], list[str]]:
    """把表头单元格解析成 ``规范字段名 → 列下标``。

    返回 ``(映射, 未识别列名列表)``。未识别列不报错，但必须出现在解析报告里。
    """
    mapping: dict[str, int] = {}
    unrecognized: list[str] = []
    for index, cell in enumerate(header_cells):
        key = normalize_header(cell)
        if not key:
            continue
        canonical = _ALIAS_INDEX.get(key)
        if canonical is None:
            label = str(cell).strip()
            if label and label not in unrecognized:
                unrecognized.append(label)
            continue
        if canonical not in mapping:  # 同名列取第一列，重复列进未识别清单
            mapping[canonical] = index
        else:
            label = str(cell).strip()
            if label and label not in unrecognized:
                unrecognized.append(f"{label}（重复列）")
    return mapping, unrecognized


def parse_decimal(value: Any) -> Optional[Decimal]:
    """把单元格解析成数值。

    容忍 ``"＜30"`` / ``"<30"`` / ``" 30 "`` / ``"30 g/L"`` 等写法；
    解析不出来返回 ``None``（由调用方把该行标为「字段缺失」，不静默当 0）。
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal)):
        return Decimal(str(value))
    text = str(value).strip()
    if not text:
        return None
    match = _NUM_PATTERN.match(text)
    if not match:
        return None
    try:
        return Decimal(match.group(1))
    except InvalidOperation:
        return None


#: 性别列的写法（大小写与全半角都归一到「男」/「女」）。
_SEX_ALIASES: dict[str, str] = {
    "男": "男",
    "男性": "男",
    "male": "男",
    "m": "男",
    "女": "女",
    "女性": "女",
    "female": "女",
    "f": "女",
}

#: 性别列里表示「不限 / 通用」的写法。
_SEX_GENERIC: frozenset[str] = frozenset(
    {"", "通用", "全部", "不限", "所有", "男女", "通用值", "any", "all", "none", "-", "/"}
)

_AGE_RANGE = re.compile(r"^\s*(\d+)\s*[-~—–至]\s*(\d+)\s*$")
_AGE_MIN_ONLY = re.compile(r"^\s*(?:≥|>=)\s*(\d+)")
_AGE_MAX_ONLY = re.compile(r"^\s*(?:<|<=|＜|≤)\s*(\d+)")

#: 年龄段标签 → ``(下限, 上限)``，口径与 ``critval.judge.ranges.AGE_BANDS`` 一致。
_AGE_LABELS: dict[str, tuple[Optional[int], Optional[int]]] = {
    "新生儿": (0, 1),
    "小儿": (0, 14),
    "儿童": (0, 18),
    "成人": (18, 60),
    "老年": (60, None),
}


def parse_applies_sex(value: Any) -> tuple[str, bool]:
    """解析「适用性别」列 → ``(性别, 是否可解析)``。

    返回的性别取值只有 ``""``（通用）、``"男"``、``"女"`` 三种；
    写了但看不懂时返回 ``("", False)`` —— 该行不得进判定集。
    """
    text = "" if value is None else str(value).strip()
    if not text:
        return "", True
    key = text.lower()
    if key in _SEX_GENERIC:
        return "", True
    if key in _SEX_ALIASES:
        return _SEX_ALIASES[key], True
    return "", False


def parse_applies_age(value: Any) -> AgeScope:
    """解析「适用年龄段」列。

    支持 ``18-60`` / ``18-`` / ``-60`` / ``≥65`` / ``<18`` / ``儿童`` / ``成人`` /
    ``老年`` 等写法。区间语义：下限含、上限不含（与参考区间分档口径一致）。
    """
    text = "" if value is None else str(value).strip()
    if not text:
        return AgeScope()

    match = _AGE_RANGE.match(text)
    if match:
        low, high = int(match.group(1)), int(match.group(2))
        if low >= high:
            # 区间反写：宁可标成不可解析，也不擅自调换（调换等于换语义）
            return AgeScope(label=text, specified=True, parsed=False)
        return AgeScope(age_min=low, age_max=high, label=text, specified=True)

    match = _AGE_MIN_ONLY.match(text)
    if match:
        return AgeScope(age_min=int(match.group(1)), label=text, specified=True)

    match = _AGE_MAX_ONLY.match(text)
    if match:
        return AgeScope(age_max=int(match.group(1)), label=text, specified=True)

    if text in _AGE_LABELS:
        low, high = _AGE_LABELS[text]
        return AgeScope(age_min=low, age_max=high, label=text, specified=True)

    return AgeScope(label=text, specified=True, parsed=False)


def parse_threshold_value(value: Any) -> tuple[Optional[Decimal], bool]:
    """解析一个阈值单元格 → ``(数值, 是否含等号)``。

    院内表格里阈值列常见三种写法：

    * ``30`` —— 裸数字，按院内习惯取**严格**不等（等于 30 不算危急）；
    * ``<30`` / ``＞30`` —— 严格不等；
    * ``<=30`` / ``≤30`` / ``>=200`` —— **含等号**（等于 30 即判危急）。

    解析不出来返回 ``(None, False)`` —— 由调用方把该行标为「阈值非数值」，
    绝不按 0 或经验值顶替。
    """
    if value is None:
        return None, False
    if isinstance(value, bool):
        return None, False
    if isinstance(value, (int, float, Decimal)):
        return Decimal(str(value)), False

    text = str(value).strip()
    if not text:
        return None, False

    comparator = _COMPARATOR_PATTERN.match(text)
    if comparator:
        op = comparator.group(1)
        try:
            number = Decimal(comparator.group(2))
        except InvalidOperation:
            return None, False
        return number, op in ("<=", ">=", "≤", "≥", "=")

    plain = _NUM_PATTERN.match(text)
    if plain:
        try:
            return Decimal(plain.group(1)), False
        except InvalidOperation:
            return None, False
    return None, False


def parse_date(value: Any) -> Optional[date]:
    """把单元格解析成日期；解析不出来返回 ``None``。"""
    """把单元格解析成日期；解析不出来返回 ``None``。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%Y年%m月%d日", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None
