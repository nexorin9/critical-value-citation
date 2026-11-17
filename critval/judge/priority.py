"""危急优先级：项目紧急度 × 危急方向权重 × 偏离系数（任务 id 34）。

一条标本可能同时触发多个危急项目（血钾 6.9、血钠 118 一起越限）。
值班人关心的不是「有几项危急」，而是「**先处理哪个**」。本模块给出这条排序：

* **项目级紧急度** —— 院内可维护的权重表（``data/urgency.csv``）。钾 1.20、
  钠 1.15、肌钙蛋白 1.15 …… 同一张卡里钾比血小板更要紧。**示例默认值只作
  回退**，院内上线必须按本院危急值处置制度维护这张表。
* **危急方向权重** —— 高危方向（钾 6.5、钠 160 这类"再高一步就要出事"）
  略高于低危方向：低危值多是慢性风险，高危值多是即刻风险。
* **偏离系数** —— 观察值越过危急阈值越远越优先（``1 + 相对偏离幅度``）。
  相对偏离幅度的分母优先取**阈值余量**（危急阈值到参考区间边界的那段距离），
  语义是「从正常区间走到危急值这条路上，已经走完了百分之多少」。参考区间
  不可用时退化为「相当于阈值的多少倍」，阈值恰为 0 时退化为绝对差。

分值 = 三者相乘，落到四档人话标签（最优先处置 / 优先处置 / 常规处置 /
刚过阈值，尽快处置）。**标签只描述处置顺序，不改判** —— 判不判危急永远
由阈值表说了算。

**排序可复现**：同分按项目码升序 —— 同一批结果每次排出来的顺序一样，
不随字典顺序或运行顺序漂移（值班台上的"第 1 项"必须是稳定的）。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

from critval.models import Judgement, PriorityScore

__all__ = [
    "BAND_BORDERLINE",
    "BAND_EMERGENCY",
    "BAND_PRIORITY",
    "BAND_ROUTINE",
    "BAND_THRESHOLDS",
    "DEFAULT_URGENCY_WEIGHTS",
    "DEFAULT_URGENCY_WEIGHT",
    "DIRECTION_WEIGHTS",
    "MAX_DEVIATION_RATIO",
    "UrgencyTable",
    "compute_priority",
    "priority_band",
    "priority_sort_key",
]

#: 危急方向权重。高危方向 > 双向 > 低危方向（差异温和 —— 真正拉开距离的是
#: 项目紧急度与偏离幅度）。
DIRECTION_WEIGHTS: dict[str, Decimal] = {
    "high": Decimal("1.00"),
    "both": Decimal("0.98"),
    "low": Decimal("0.95"),
    "none": Decimal("0.90"),
}

#: 优先级分档（人话标签）。
BAND_EMERGENCY = "最优先处置"
BAND_PRIORITY = "优先处置"
BAND_ROUTINE = "常规处置"
BAND_BORDERLINE = "刚过阈值，尽快处置"

#: 分档阈值（按最终优先级分值，从高到低匹配）。
BAND_THRESHOLDS: tuple[tuple[Decimal, str], ...] = (
    (Decimal("1.50"), BAND_EMERGENCY),
    (Decimal("1.10"), BAND_PRIORITY),
    (Decimal("0.90"), BAND_ROUTINE),
    (Decimal("0"), BAND_BORDERLINE),
)

#: 相对偏离幅度的上限 —— 极端离群值不至于把其他项目挤出排序（封顶只影响
#: 分值的放大倍数，**不改判**）。
MAX_DEVIATION_RATIO = Decimal("3")

#: 项目级紧急度的**示例**默认权重（项目码 → (权重, 说明)）。
#: 院内应按本院危急值处置制度维护 ``data/urgency.csv``；这里只作回退。
DEFAULT_URGENCY_WEIGHTS: dict[str, tuple[Decimal, str]] = {
    "2823-3": (Decimal("1.20"), "钾：可致心律失常"),
    "2951-2": (Decimal("1.15"), "钠：重度紊乱可致抽搐"),
    "10841-5": (Decimal("1.15"), "肌钙蛋白I：急性心肌损伤指标"),
    "2345-7": (Decimal("1.10"), "葡萄糖：过高或过低均可致意识障碍"),
    "2160-0": (Decimal("1.05"), "肌酐：肾功能恶化指标"),
    "718-7": (Decimal("1.00"), "血红蛋白：携氧能力"),
    "777-3": (Decimal("1.00"), "血小板计数：出血风险"),
    "6690-2": (Decimal("0.95"), "白细胞计数：感染指标"),
}

#: 未在紧急度表里收录的项目用这个权重（中性，不奖不罚）。
DEFAULT_URGENCY_WEIGHT = Decimal("1.00")

#: 分值保留 4 位小数 —— 排序按分值走，量化后才不会因尾数差异抖动。
_QUANT = Decimal("0.0001")


@dataclass(frozen=True)
class UrgencyTable:
    """项目级紧急度表。

    ``source`` 说明这张表从哪来：``builtin``（内置示例）/ ``file``（院内维护的
    CSV）/ ``builtin+file``（文件叠加在示例之上）。``warnings`` 记加载过程中的
    问题（损坏行被跳过等），**不静默丢弃**。
    """

    weights: Mapping[str, tuple[Decimal, str]]
    source: str = "builtin"
    warnings: tuple[str, ...] = ()

    @classmethod
    def builtin(cls) -> "UrgencyTable":
        """内置示例紧急度表。"""
        return cls(weights=dict(DEFAULT_URGENCY_WEIGHTS), source="builtin")

    @classmethod
    def load(cls, path: str | Path) -> "UrgencyTable":
        """从 CSV 加载紧急度表（``item_code,urgency_weight,note``）。

        * 文件不存在 → 用内置示例表，**零告警**（没配就是没配，不是出错）；
        * 权重列不是数值 → **只跳过该行**并留告警，其余行照常采用；
        * 一行都没读到 → 回退内置示例表并告警（不让一张坏表把排序打空）。
        """
        table_path = Path(path)
        if not table_path.exists():
            return cls.builtin()

        weights: dict[str, tuple[Decimal, str]] = {}
        warnings: list[str] = []
        try:
            raw = table_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            warnings.append(f"紧急度表读取失败，回退默认表：{exc}")
            return cls(weights=dict(DEFAULT_URGENCY_WEIGHTS), source="builtin", warnings=tuple(warnings))

        for line_no, raw_line in enumerate(raw.splitlines(), start=1):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            parts = [part.strip() for part in line.split(",")]
            item_code = parts[0] if parts else ""
            if not item_code:
                warnings.append(f"紧急度表第 {line_no} 行未采用：缺项目码")
                continue
            if len(parts) < 2:
                warnings.append(f"紧急度表第 {line_no} 行未采用：缺紧急度权重")
                continue
            try:
                weight = Decimal(parts[1])
            except (InvalidOperation, ValueError):
                warnings.append(f"紧急度表第 {line_no} 行未采用：紧急度权重 {parts[1]!r} 不是数值")
                continue
            if weight <= 0:
                warnings.append(f"紧急度表第 {line_no} 行未采用：紧急度权重必须为正")
                continue
            note = parts[2] if len(parts) > 2 else ""
            weights[item_code] = (weight, note)

        if not weights:
            warnings.append("紧急度表没有任何可用行，回退默认表")
            return cls(
                weights=dict(DEFAULT_URGENCY_WEIGHTS),
                source="builtin",
                warnings=tuple(warnings),
            )
        return cls(weights=weights, source="file", warnings=tuple(warnings))

    def weight_for(self, item_code: str) -> Decimal:
        """项目紧急度权重（未收录取中性权重）。"""
        entry = self.weights.get(item_code)
        if entry is None:
            return DEFAULT_URGENCY_WEIGHT
        return entry[0]

    def note_for(self, item_code: str) -> str:
        """项目紧急度说明（未收录返回空串）。"""
        entry = self.weights.get(item_code)
        return entry[1] if entry else ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "item_count": len(self.weights),
            "warnings": list(self.warnings),
            "weights": {
                code: {"weight": str(weight), "note": note}
                for code, (weight, note) in sorted(self.weights.items())
            },
        }


def priority_band(score: Decimal) -> str:
    """分值 → 人话分档。"""
    for threshold, band in BAND_THRESHOLDS:
        if score >= threshold:
            return band
    return BAND_BORDERLINE


def _hit_direction(row: Any, value: Decimal) -> tuple[str, Optional[Decimal]]:
    """判断越过的是哪一侧阈值，返回 ``(方向, 阈值)``。

    闭开区间语义沿用判定引擎（``low_inclusive`` / ``high_inclusive``）：
    源表写 ``≤`` / ``≥`` 时取等号也算危急。
    """
    high = getattr(row, "high_critical", None)
    low = getattr(row, "low_critical", None)
    high_inclusive = bool(getattr(row, "high_inclusive", False))
    low_inclusive = bool(getattr(row, "low_inclusive", False))

    high_hit = high is not None and (value >= high if high_inclusive else value > high)
    low_hit = low is not None and (value <= low if low_inclusive else value < low)

    if low_hit and high_hit:
        return "both", high
    if low_hit:
        return "low", low
    if high_hit:
        return "high", high
    return "none", None


def _threshold_margin(
    direction: str,
    threshold: Decimal,
    band: Any,
) -> Optional[Decimal]:
    """阈值余量：从参考区间边界走到危急阈值的那段距离。

    高危方向看区间上界（``危急阈值 − 区间上界``），低危方向看区间下界
    （``区间下界 − 危急阈值``）。参考区间不可用、或阈值落在区间之内时
    返回 ``None``（调用方退回别的分母口径）。
    """
    if band is None or getattr(band, "kind", "") != "numeric":
        return None
    if direction == "high":
        high = getattr(band, "high", None)
        if high is not None and high < threshold:
            return threshold - high
        return None
    if direction == "low":
        low = getattr(band, "low", None)
        if low is not None and low > threshold:
            return low - threshold
        return None
    return None


def _deviation(judgement: Judgement) -> tuple[Decimal, str, str, str]:
    """算相对偏离幅度。

    返回 ``(幅度, 方向, 说明, 分母口径)``。判定不成立的（没命中阈值行 /
    不是危急）幅度记 0，方向 ``none``。
    """
    row = judgement.threshold_row
    if row is None:
        return Decimal("0"), "none", "未命中阈值行，无法计算偏离幅度", "none"

    value = judgement.converted_value if judgement.converted_value is not None else judgement.value
    direction, threshold = _hit_direction(row, value)
    if direction == "none" or threshold is None:
        return Decimal("0"), "none", "未越过危急阈值", "none"

    delta = value - threshold
    if direction == "low":
        delta = -delta  # 低危方向：偏离幅度取正
    deviation = abs(delta)

    margin = _threshold_margin(direction, threshold, judgement.reference_band)
    if margin is not None and margin > 0:
        ratio = deviation / margin
        basis = "阈值余量"
        basis_note = (
            f"阈值余量 {margin}，已越过 {(ratio * 100).quantize(Decimal('0.01'))}%"
        )
    elif threshold != 0:
        ratio = deviation / abs(threshold)
        basis = "阈值倍数"
        basis_note = f"相当于阈值的 {ratio.quantize(_QUANT)} 倍"
    else:
        ratio = deviation
        basis = "绝对差"
        basis_note = f"绝对差 {deviation}"

    unit = getattr(row, "unit", "") or judgement.unit
    side = "高" if direction == "high" else ("低" if direction == "low" else "双向")
    note = f"越过{side}危急值 {threshold} {unit} 共 {deviation}（{basis_note}）"
    return ratio.quantize(_QUANT), direction, note, basis


def compute_priority(
    judgement: Judgement,
    urgency_table: Optional[UrgencyTable] = None,
) -> PriorityScore:
    """算一条判定结论的优先级。

    分值 = 项目紧急度 × 方向权重 × 偏离系数（``1 + 相对偏离幅度``）。
    ``judgement`` 必须是**已判定**的结论；不是危急（``not_critical``）时
    偏离幅度记 0，排序时自然沉到最后。
    """
    table = urgency_table or UrgencyTable.builtin()
    ratio, direction, note, _basis = _deviation(judgement)
    capped = min(ratio, MAX_DEVIATION_RATIO)
    direction_weight = DIRECTION_WEIGHTS.get(direction, DIRECTION_WEIGHTS["none"])
    urgency_weight = table.weight_for(judgement.item_code)
    urgency_note = table.note_for(judgement.item_code)
    score = (urgency_weight * direction_weight * (Decimal("1") + capped)).quantize(_QUANT)
    return PriorityScore(
        score=score,
        band=priority_band(score),
        deviation_ratio=ratio,
        deviation_note=note,
        direction=direction,
        direction_weight=direction_weight,
        urgency_weight=urgency_weight,
        urgency_note=urgency_note,
    )


def priority_sort_key(score: PriorityScore, item_code: str) -> tuple[Decimal, str]:
    """排序键：分值降序；同分按项目码升序。

    同分按项目码排（而不是保持入参顺序）—— 同一批结果每次排出来的顺序
    必须一样，否则值班台上的"第 1 项"会随运行漂移。
    """
    return (-score.score, item_code)


def sort_by_priority(items: Iterable[Any]) -> list[Any]:
    """按优先级降序排一组带 ``priority`` / ``item_code`` 的对象。"""
    return sorted(
        items,
        key=lambda item: priority_sort_key(item.priority, item.item_code),
    )


def summarize_priorities(scores: Sequence[PriorityScore]) -> dict[str, Any]:
    """一批优先级分值的摘要（分档计数 + 最高分）。"""
    counts: dict[str, int] = {}
    for score in scores:
        counts[score.band] = counts.get(score.band, 0) + 1
    top = max((score.score for score in scores), default=Decimal("0"))
    return {
        "total": len(scores),
        "bands": counts,
        "top_score": str(top),
    }
