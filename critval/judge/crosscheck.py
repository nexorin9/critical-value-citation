"""OBX-8 异常标志与院内阈值表的交叉校验，以及「只看 OBX-8」的朴素基线。

## 为什么要有这一层

LIS 报文里的 ``OBX-8``（异常标志）标的是**这条结果偏没偏离参考区间**，
不是**到没到危急值阈值**。这是两件事：

* 参考区间是「正常范围」，越出它只说明「异常」；
* 危急值阈值是「必须立刻找人」的那条线，通常比参考区间宽得多。

检验科值班技师如果按「LIS 说异常就报危急值」处理，会把大量
「偏低但不到危急值」的结果当成危急值推进值班群 —— 群里一天几十条，
真正要命的那一条被淹没在里面。**这就是告警疲劳**，也是院内反复踩的坑。

所以本模块给出两样东西：

1. :func:`crosscheck_flag` —— 事件判定完成后，把 ``OBX-8`` 与阈值表结论
   对一遍；**不一致时以阈值表为准**（判定权威不变），并把分歧落成一条
   可复核的记录（``Judgement.obx_flag_disagreement`` + 说明），让值班人
   知道「LIS 的异常标志和咱们院的阈值表口径对不上」。
2. :func:`naive_flag_decision` —— 「只看 OBX-8」的朴素基线。任务 id 9 拿它
   和判定引擎在**同一批样例**上跑，比口径分歧检出数，产出一张可复算的
   对比表（``critval/judge/baseline_compare.py``）。

## 朴素基线有多朴素

朴素基线的口径就一条：**``OBX-8`` 非空且不是 ``N`` → 危急值**。
不查阈值表、不换算单位、不看危急方向 —— 值班技师最原始的判断方式。
缺标志（``OBX-8`` 为空）也算「没说不危急」，按不危急处理。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

__all__ = [
    "BASELINE_CRITICAL",
    "BASELINE_NOT_CRITICAL",
    "DIRECTION_OVER_CALL",
    "DIRECTION_UNDER_CALL",
    "FLAG_CLASS_ABNORMAL",
    "FLAG_CLASS_ABOVE",
    "FLAG_CLASS_BELOW",
    "FLAG_CLASS_MISSING",
    "FLAG_CLASS_NORMAL",
    "FLAG_CLASS_OTHER",
    "FLAG_CLASS_PANIC_HIGH",
    "FLAG_CLASS_PANIC_LOW",
    "FlagDisagreement",
    "classify_flag",
    "crosscheck_flag",
    "naive_flag_decision",
]

#: 朴素基线的两种结论。
BASELINE_CRITICAL = "critical"
BASELINE_NOT_CRITICAL = "not_critical"

#: 分歧方向。
#:
#: * ``baseline_over_call`` —— 朴素基线判危急、阈值表判不危急（**误报**，
#:   告警疲劳的主要来源）；
#: * ``baseline_under_call`` —— 朴素基线判不危急、阈值表判危急（**漏报**，
#:   真正要命的被放过）。
DIRECTION_OVER_CALL = "baseline_over_call"
DIRECTION_UNDER_CALL = "baseline_under_call"

#: 异常标志分类（HL7 v2 Table 0078 的常见取值）。
FLAG_CLASS_NORMAL = "normal"
FLAG_CLASS_ABOVE = "above_normal"
FLAG_CLASS_BELOW = "below_normal"
FLAG_CLASS_PANIC_HIGH = "panic_high"
FLAG_CLASS_PANIC_LOW = "panic_low"
FLAG_CLASS_ABNORMAL = "abnormal"
FLAG_CLASS_MISSING = "missing"
FLAG_CLASS_OTHER = "other"

_FLAG_CLASS_BY_FLAG: dict[str, str] = {
    "N": FLAG_CLASS_NORMAL,
    "H": FLAG_CLASS_ABOVE,
    ">": FLAG_CLASS_ABOVE,
    "I": FLAG_CLASS_ABOVE,
    "L": FLAG_CLASS_BELOW,
    "<": FLAG_CLASS_BELOW,
    "B": FLAG_CLASS_BELOW,
    "HH": FLAG_CLASS_PANIC_HIGH,
    "LL": FLAG_CLASS_PANIC_LOW,
    "A": FLAG_CLASS_ABNORMAL,
    "AA": FLAG_CLASS_ABNORMAL,
    "U": FLAG_CLASS_ABNORMAL,
    "D": FLAG_CLASS_ABNORMAL,
}

_FLAG_CLASS_LABELS: dict[str, str] = {
    FLAG_CLASS_NORMAL: "正常",
    FLAG_CLASS_ABOVE: "高于正常",
    FLAG_CLASS_BELOW: "低于正常",
    FLAG_CLASS_PANIC_HIGH: "高于危急（panic）",
    FLAG_CLASS_PANIC_LOW: "低于危急（panic）",
    FLAG_CLASS_ABNORMAL: "异常",
    FLAG_CLASS_MISSING: "未给异常标志",
    FLAG_CLASS_OTHER: "未识别标志",
}


@dataclass(frozen=True)
class FlagDisagreement:
    """一条 ``OBX-8`` 与阈值表的口径分歧记录。

    ``direction`` 说明分歧朝哪边歪：

    * ``baseline_over_call`` —— 朴素口径会误报（按阈值表其实不危急）；
    * ``baseline_under_call`` —— 朴素口径会漏报（按阈值表其实是危急）。

    记录本身**不改判**：判定权威永远是院内阈值表。
    """

    obx_flag: str
    flag_class: str
    baseline_decision: str
    engine_decision: str
    direction: str
    note: str = ""

    @property
    def is_over_call(self) -> bool:
        return self.direction == DIRECTION_OVER_CALL

    @property
    def is_under_call(self) -> bool:
        return self.direction == DIRECTION_UNDER_CALL

    def to_dict(self) -> dict[str, Any]:
        return {
            "obx_flag": self.obx_flag,
            "flag_class": self.flag_class,
            "flag_class_label": _FLAG_CLASS_LABELS.get(self.flag_class, self.flag_class),
            "baseline_decision": self.baseline_decision,
            "engine_decision": self.engine_decision,
            "direction": self.direction,
            "note": self.note,
        }


def classify_flag(flag: str) -> str:
    """把 ``OBX-8`` 归到一类（正常 / 高于正常 / 低于正常 / panic / 异常 …）。"""
    text = (flag or "").strip().upper()
    if not text:
        return FLAG_CLASS_MISSING
    return _FLAG_CLASS_BY_FLAG.get(text, FLAG_CLASS_OTHER)


def naive_flag_decision(flag: str) -> str:
    """朴素基线：**只看 ``OBX-8``** 判不判危急值。

    ``OBX-8`` 为空或为 ``N`` → 不危急；其余一律判危急。
    这是值班技师最原始的判断方式，也是本项目的对照基线。
    """
    text = (flag or "").strip().upper()
    if not text or text == "N":
        return BASELINE_NOT_CRITICAL
    return BASELINE_CRITICAL


def _engine_locator(judgement: Any) -> str:
    """取判定命中的阈值行出处（可读行号 + 版本）。"""
    row = getattr(judgement, "threshold_row", None)
    if row is None:
        return ""
    citation = getattr(row, "citation", None)
    locator = getattr(citation, "locator", "") if citation is not None else ""
    return locator or ""


def crosscheck_flag(obx_flag: str, judgement: Any) -> Optional[FlagDisagreement]:
    """把 ``OBX-8`` 与判定结论对一遍。

    * 判定本身**没结论**（转人工的各种情况）→ 不比对，返回 ``None``；
    * 两侧一致 → 不比对，返回 ``None``；
    * 两侧不一致 → 返回一条 :class:`FlagDisagreement`（**以阈值表为准**）。

    参数 ``judgement`` 只要求有 ``decision`` 属性（duck typing），
    这样判定层与评测层都能调它。
    """
    decision = getattr(judgement, "decision", "")
    if decision not in ("critical", "not_critical"):
        return None

    baseline = naive_flag_decision(obx_flag)
    if baseline == decision:
        return None

    flag_class = classify_flag(obx_flag)
    flag_text = (obx_flag or "").strip() or "（空）"
    class_label = _FLAG_CLASS_LABELS.get(flag_class, flag_class)
    locator = _engine_locator(judgement)
    basis = f"（依据 {locator}）" if locator else ""

    if baseline == BASELINE_CRITICAL:
        direction = DIRECTION_OVER_CALL
        detail = (
            f"LIS 异常标志 {flag_text}（{class_label}）按朴素口径会被当成危急值，"
            f"但按院内阈值表{basis}判定为未达危急阈值"
        )
    else:
        direction = DIRECTION_UNDER_CALL
        detail = (
            f"LIS 异常标志 {flag_text}（{class_label}）按朴素口径不会被当成危急值，"
            f"但按院内阈值表{basis}判定为危急值"
        )

    note = f"{detail}；以院内阈值表为准。"
    return FlagDisagreement(
        obx_flag=(obx_flag or "").strip(),
        flag_class=flag_class,
        baseline_decision=baseline,
        engine_decision=decision,
        direction=direction,
        note=note,
    )
