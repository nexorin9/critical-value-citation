"""参考区间解析与分档。

参考区间**不是判定权威**（判定权威是院内阈值表），它有两个用途：

1. 给判定结果补一句「该值相对参考区间落在什么位置」，方便值班人一眼看出偏离程度；
2. 按性别 / 年龄段选出**该患者适用的那一条**区间 —— 用错档会把正常值说成异常。

区间文本形态很杂，本模块覆盖：``3.5-9.5`` / ``<3.5`` / ``>40`` / ``<=5`` /
``>=2.5`` / ``3.5~9.5`` / ``3.5—9.5`` / ``阴性`` / ``阳性``，
以及带分档标签的 ``男 115-150；女 120-150``。

两条硬纪律：

* **解析失败不静默跳过**：看不懂的写法落成 ``kind="missing"`` 的缺失标记
  （保留原文与原因），**不参与分档**，但会进判定记录与解析报告；
* **分档信息不足要标低置信**：拿不准用哪条区间时，取默认区间并把
  ``confidence`` 置为 ``low`` + 写明原因，低置信随判定记录落库，
  复核时能看出「这条区间是猜的」。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from typing import Optional, Sequence

from critval.models import ReferenceBand

__all__ = [
    "AGE_BANDS",
    "BandScope",
    "missing_markers",
    "parse_reference_ranges",
    "select_band",
]

#: 年龄段口径（与院内常用分档一致；改口径只改这里）。
AGE_BANDS: tuple[tuple[str, Optional[int], Optional[int]], ...] = (
    ("儿童", None, 18),
    ("成人", 18, 60),
    ("老年", 60, None),
)

#: 具名年龄段 → ``(下限, 上限)``（用于阈值表人群与参考区间分档的区间比对）。
_NAMED_AGE_RANGES: dict[str, tuple[Optional[int], Optional[int]]] = {
    "新生儿": (0, 1),
    "小儿": (0, 14),
    "儿童": (0, 18),
    "成人": (18, 60),
    "老年": (60, None),
}

_COMPARATOR_ONLY = re.compile(r"^(<=|>=|<|>|≤|≥|＜|＞)\s*(\d+(?:\.\d+)?)$")
_PAIR = re.compile(r"^(\d+(?:\.\d+)?)\s*[-~—–至]\s*(\d+(?:\.\d+)?)$")
_AGE_BOUND = re.compile(r"(≥|>=|<=|＜|＞|<)\s*(\d+)\s*岁")
_TEXT_VALUES = ("阴性", "阳性", "未检出", "未见", "未见异常")

_SEGMENT_SPLIT = re.compile(r"[;；|]|\n")


@dataclass(frozen=True)
class BandScope:
    """阈值行给出的适用人群 —— 选参考区间分档时**优先于**患者上下文。

    阈值表把同一项目拆成「男一行、女一行」时，行上的「适用性别」列已经
    说明了这条阈值是给谁的；此时按患者信息再挑一次反而可能挑错。
    """

    sex: str = ""
    age_min: Optional[int] = None
    age_max: Optional[int] = None
    age_label: str = ""

    @classmethod
    def from_row(cls, row) -> "BandScope":
        """从阈值行取适用人群（``row`` 为 :class:`critval.models.ThresholdRow`）。"""
        return cls(
            sex=getattr(row, "applies_sex", "") or "",
            age_min=getattr(row, "applies_age_min", None),
            age_max=getattr(row, "applies_age_max", None),
            age_label=getattr(row, "applies_age_label", "") or "",
        )

    @property
    def has_scope(self) -> bool:
        return bool(self.sex or self.age_min is not None or self.age_max is not None or self.age_label)


def _to_decimal(text: str) -> Optional[Decimal]:
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def _split_label(segment: str) -> tuple[Optional[str], str]:
    """拆出 ``(标签, 区间表达式)``。

    标签可能是「男」「女」「儿童」「成人」「老年」「≥65岁」等。
    """
    label: Optional[str] = None
    remainder = segment.strip()

    age_match = _AGE_BOUND.search(remainder)
    if age_match:
        label = age_match.group(0)
        remainder = (remainder[: age_match.start()] + remainder[age_match.end() :]).strip()

    for keyword in ("新生儿", "儿童", "成人", "老年"):
        if keyword in remainder:
            label = label or keyword
            remainder = remainder.replace(keyword, "", 1).strip()
            break

    for keyword, tag in (("男性", "男"), ("女性", "女"), ("男", "男"), ("女", "女")):
        if keyword in remainder:
            label = label or tag
            remainder = remainder.replace(keyword, "", 1).strip()
            break

    remainder = remainder.lstrip(":：,， ").rstrip("；; ")
    return label, remainder


def _parse_expression(expression: str) -> Optional[ReferenceBand]:
    """解析单个区间表达式。解析不出来返回 ``None``（由调用方落缺失标记）。"""
    text = expression.strip()
    if not text:
        return None

    for keyword in _TEXT_VALUES:
        if text.startswith(keyword):
            return ReferenceBand(raw=text, kind="text", text_value=keyword)

    comparator = _COMPARATOR_ONLY.match(text)
    if comparator:
        op = comparator.group(1)
        number = _to_decimal(comparator.group(2))
        if number is None:
            return None
        if op in ("<", "＜"):
            return ReferenceBand(raw=text, high=number, high_inclusive=False)
        if op in ("<=", "≤"):
            return ReferenceBand(raw=text, high=number, high_inclusive=True)
        if op in (">", "＞"):
            return ReferenceBand(raw=text, low=number, low_inclusive=False)
        return ReferenceBand(raw=text, low=number, low_inclusive=True)

    pair = _PAIR.match(text)
    if pair:
        low = _to_decimal(pair.group(1))
        high = _to_decimal(pair.group(2))
        if low is None or high is None:
            return None
        if low > high:
            # 区间反写：宁可标成无效，也不擅自调换（调换等于换语义）
            return ReferenceBand(raw=text, kind="invalid", note="参考区间上下限反写")
        return ReferenceBand(raw=text, low=low, high=high)

    return None


def parse_reference_ranges(text: str | None) -> list[ReferenceBand]:
    """解析参考区间文本 → 分档列表。

    **解析失败的片段不会被静默跳过**：落成 ``kind="missing"`` 的缺失标记
    （保留原文与原因），它不参与分档，但会出现在判定记录里。
    """
    if not text:
        return []
    bands: list[ReferenceBand] = []
    for segment in _SEGMENT_SPLIT.split(str(text)):
        original = segment.strip()
        if not original:
            continue
        label, expression = _split_label(segment)
        band = _parse_expression(expression)
        if band is None:
            band = ReferenceBand(
                raw=original,
                kind="missing",
                note=f"参考区间写法无法解析：{original}",
            )
        elif band.kind == "invalid":
            band = replace(band, note="参考区间上下限反写，不参与分档")
        if label:
            band = replace(band, label=label)
        bands.append(band)
    return bands


def missing_markers(bands: Sequence[ReferenceBand]) -> list[str]:
    """取出解析失败 / 反写的区间说明（供判定记录与解析报告引用）。"""
    notes: list[str] = []
    for band in bands:
        if band.kind in ("missing", "invalid") and band.note:
            notes.append(band.note)
    return notes


def _age_label_matches(label: Optional[str], age: Optional[int]) -> bool:
    if label is None or age is None:
        return False
    age_match = _AGE_BOUND.search(label)
    if age_match:
        op = age_match.group(1)
        bound = int(age_match.group(2))
        if op in ("≥", ">="):
            return age >= bound
        if op in ("<", "＜"):
            return age < bound
        if op in ("<=", "≤"):
            return age <= bound
        return False
    for name, lower, upper in AGE_BANDS:
        if label == name:
            if lower is not None and age < lower:
                return False
            if upper is not None and age >= upper:
                return False
            return True
    return False


def _sex_label_matches(label: Optional[str], sex: Optional[str]) -> bool:
    if label is None or sex is None:
        return False
    if sex in ("男", "M", "male"):
        return "男" in label and "女" not in label
    if sex in ("女", "F", "female"):
        return "女" in label
    return False


def _sex_label_conflicts(label: Optional[str], sex: Optional[str]) -> bool:
    """区间标签的性别与已知性别**冲突**（如区间写「女」而患者是男）。"""
    if label is None or sex is None:
        return False
    if sex in ("男", "M", "male"):
        return "女" in label and "男" not in label
    if sex in ("女", "F", "female"):
        return "男" in label and "女" not in label
    return False


def _age_range_of_label(label: Optional[str]) -> Optional[tuple[Optional[int], Optional[int]]]:
    """标签 → 年龄区间（具名档或 ``≥N岁`` / ``<N岁`` 写法）。"""
    if not label:
        return None
    if label in _NAMED_AGE_RANGES:
        return _NAMED_AGE_RANGES[label]
    age_match = _AGE_BOUND.search(label)
    if not age_match:
        return None
    op = age_match.group(1)
    bound = int(age_match.group(2))
    if op in ("≥", ">="):
        return (bound, None)
    if op in ("<", "＜"):
        return (None, bound)
    return (None, None)


def _age_overlaps_scope(label: Optional[str], scope: BandScope) -> bool:
    """区间标签的年龄段与阈值行人群是否重叠。"""
    band_range = _age_range_of_label(label)
    if band_range is None:
        return False
    band_min, band_max = band_range
    scope_min, scope_max = scope.age_min, scope.age_max
    if band_min is not None and scope_max is not None and band_min >= scope_max:
        return False
    if band_max is not None and scope_min is not None and band_max <= scope_min:
        return False
    return True


def _mark(band: ReferenceBand, confidence: str, note: str) -> ReferenceBand:
    """给选中的区间打上置信度与说明。"""
    if confidence == band.confidence and note == band.note:
        return band
    return replace(band, confidence=confidence, note=note)


def _pick_by_scope(usable: list[ReferenceBand], scope: BandScope) -> Optional[ReferenceBand]:
    """按阈值行给出的适用人群选区间。"""
    candidates = usable
    if scope.sex:
        sex_matched = [b for b in usable if _sex_label_matches(b.label, scope.sex)]
        if sex_matched:
            candidates = sex_matched
    if scope.age_min is not None or scope.age_max is not None:
        age_matched = [b for b in candidates if _age_overlaps_scope(b.label, scope)]
        if age_matched:
            candidates = age_matched
    if not candidates:
        return None
    if len(candidates) == 1:
        return _mark(candidates[0], "high", "")
    unlabeled = [b for b in candidates if b.label is None]
    if len(unlabeled) == 1:
        return _mark(unlabeled[0], "high", "")
    return _mark(
        candidates[0],
        "low",
        f"阈值表人群（{scope.sex or scope.age_label or '未标注'}）未能唯一确定参考区间，采用默认区间",
    )


def _pick_by_context(
    usable: list[ReferenceBand],
    sex: Optional[str],
    age: Optional[int],
) -> Optional[ReferenceBand]:
    """按患者上下文（性别 / 年龄）选区间。"""
    if not sex and age is None:
        return None
    if sex:
        sex_matched = [b for b in usable if _sex_label_matches(b.label, sex)]
        if sex_matched:
            if age is not None:
                age_matched = [b for b in sex_matched if _age_label_matches(b.label, age)]
                if age_matched:
                    return _mark(age_matched[0], "high", "")
            if len(sex_matched) == 1:
                return _mark(sex_matched[0], "high", "")
            return _mark(
                sex_matched[0],
                "low",
                f"同性别（{sex}）有多条参考区间分档，采用默认区间",
            )
    if age is not None:
        age_matched = [b for b in usable if _age_label_matches(b.label, age)]
        if age_matched:
            return _mark(age_matched[0], "high", "")
    return None


def _fallback(
    usable: list[ReferenceBand],
    sex: Optional[str],
    age: Optional[int],
) -> Optional[ReferenceBand]:
    """兜底：取默认区间并**标低置信** + 写明原因。"""
    unlabeled = [b for b in usable if b.label is None]
    if len(unlabeled) == 1:
        # 唯一一条且没有分档标签 —— 不存在「挑错档」的可能
        return _mark(unlabeled[0], "high", "")
    base = unlabeled[0] if unlabeled else usable[0]
    notes: list[str] = []
    if sex:
        conflicting = [b for b in usable if _sex_label_conflicts(b.label, sex)]
        if conflicting:
            notes.append(f"参考区间按性别分档（{conflicting[0].label}），与上报性别（{sex}）不符")
    if age is not None:
        conflicting = [b for b in usable if b.label is not None and not _age_label_matches(b.label, age)]
        if conflicting:
            notes.append(f"参考区间按年龄段分档（{conflicting[0].label}），与上报年龄（{age} 岁）不符")
    if not notes:
        notes.append("缺少性别/年龄分档信息，采用默认区间")
    return _mark(base, "low", "；".join(notes))


def select_band(
    bands: list[ReferenceBand],
    sex: Optional[str] = None,
    age: Optional[int] = None,
    scope: Optional[BandScope] = None,
) -> Optional[ReferenceBand]:
    """选出适用的那一条参考区间。

    选取顺序：

    1. **阈值表人群优先**（``scope``）—— 表里说这条阈值给谁，就取谁的那条区间；
    2. **患者上下文**（性别 / 年龄）；
    3. **兜底**：取默认区间并标 ``confidence="low"`` + 写明原因。

    ``missing`` / ``invalid`` 的区间**不参与分档**；全都不可用时返回 ``None``，
    由调用方记「参考区间缺失」。
    """
    usable = [b for b in bands if b.kind not in ("invalid", "missing")]
    if not usable:
        return None

    if scope is not None and scope.has_scope:
        picked = _pick_by_scope(usable, scope)
        if picked is not None:
            return picked

    picked = _pick_by_context(usable, sex, age)
    if picked is not None:
        return picked

    return _fallback(usable, sex, age)
