"""单位换算表（院内可维护 · **缺换算即不判定**）。

红线：**查不到换算关系就不判定**。换算表查不到时返回 ``unit_unknown``，
交人工处理，绝不按经验猜一个系数 —— 猜错就是把 2.8 mmol/L 当成 2.8 mg/dL，
结果是漏报或误报。

换算分两类：

* **通用换算**（与被测物无关）：mg/dL ↔ g/L、g/L ↔ g/dL、10^9/L ↔ 10^3/uL、
  IU/L ↔ U/L；
* **物质相关换算**（依赖摩尔质量，必须知道是哪个项目）：mmol/L ↔ mg/dL、
  µmol/L ↔ mg/dL。缺项目上下文时**不给**换算。

## 换算精度（为什么要有 ``precision``）

换算一律用 **十进制精确运算**（``Decimal``），不用二进制浮点 —— 这是「不把临界值
判反」的第一道保险。浮点世界里 ``2900 * 0.01`` 会得到 ``28.999999999999996``：
本该「恰好等于阈值 29」的值被算成「略低于阈值」，于是一条**不是**危急值的结果
被判成危急。``Decimal`` 下结果是精确的 ``29``。

``precision`` 记的是**目标单位有意义的小数位**（血红蛋白 g/L 2 位、钾 mmol/L 3 位、
肌酐 µmol/L 1 位）。它有两个用途：

* 换算结果按该精度给出「上报值」（``reported_value``），核对同一台仪器换个单位
  报出来是不是同一个数；
* 判定时用它判断「换算后的值是不是恰好落在阈值的一个精度步长之内」——
  落在里面就在判定记录里标**阈值附近**，提示人工复核。物质相关换算（摩尔质量
  系数取整）本身带小数位误差，这种「刚好压线」的值只有靠精度步长才看得出来。

比较本身始终用**精确换算值** ``value``，不拿上报值去比 —— 宁可多标一条待复核，
也不拿取整后的值改判危急结论。

## 换算表来源

* **代码内默认表**（``UnitTable.default()``）—— 兜底，永远可用；
* **外部覆盖文件**（默认 ``data/units.csv``）—— 院内自行维护，可覆盖同名项、
  追加新项。文件不存在时用默认表；文件存在但读不了 / 一行都不可用时
  **回退默认表并告警**（不静默用一个半对的表去判危急）。

CSV 列定义（表头必须一致，顺序不限）：

``from_unit,to_unit,factor,offset,precision,label,item_code,note``

* ``from_unit`` / ``to_unit`` —— 单位写法，会先经 ``normalize_unit`` 归一；
* ``factor`` / ``offset`` —— ``目标值 = 原值 * factor + offset``；
* ``precision`` —— 目标单位保留的小数位，留空按 ``UNIT_PRECISION`` 默认值；
* ``label`` —— 换算类别标签（如「质量浓度换算」）；
* ``item_code`` —— 留空表示通用换算，填项目码表示该项目专用换算；
* ``note`` —— 自由备注。
"""

from __future__ import annotations

import csv
import logging
import re
import threading
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Optional

__all__ = [
    "Conversion",
    "ConversionResult",
    "UNIT_PRECISION",
    "UNIT_TABLE_CSV",
    "UnitTable",
    "convert",
    "current_unit_table",
    "load_unit_table",
    "normalize_unit",
    "reload_unit_table",
    "reset_unit_table",
]

logger = logging.getLogger(__name__)

#: 外部覆盖文件默认路径（相对工作目录）。
UNIT_TABLE_CSV = Path("data/units.csv")

#: 各规范单位「有意义的小数位」。换算结果按**目标单位**的精度取整。
UNIT_PRECISION: dict[str, int] = {
    "g/L": 2,
    "mg/dL": 2,
    "g/dL": 3,
    "mg/L": 2,
    "mmol/L": 3,
    "µmol/L": 1,
    "IU/L": 0,
    "U/L": 0,
    "10^9/L": 2,
    "10^3/uL": 2,
    "10^12/L": 2,
    "%": 1,
}

_SPACE = re.compile(r"\s+")


@dataclass(frozen=True)
class Conversion:
    """一条换算关系：``to = value * factor + offset``。

    ``precision`` 是**目标单位**保留的小数位（``None`` 表示按
    ``UNIT_PRECISION`` 取默认值，再缺则保留全部位数）。
    """

    from_unit: str
    to_unit: str
    factor: Decimal
    offset: Decimal = Decimal(0)
    precision: Optional[int] = None
    label: str = ""
    note: str = ""
    scope: str = ""  # "" = 通用换算；否则为项目码（物质相关换算）

    @property
    def key(self) -> tuple[str, str, str]:
        """唯一键：``(from, to, scope)``。外部覆盖按这个键合并。"""
        return (self.from_unit, self.to_unit, self.scope)

    def effective_precision(self) -> Optional[int]:
        """实际生效的小数位：显式指定优先，否则取单位默认。"""
        if self.precision is not None:
            return self.precision
        return UNIT_PRECISION.get(self.to_unit)

    def invert(self) -> "Conversion":
        """逆换算：``from = (to - offset) / factor``。

        逆换算的精度按**逆换算的目标单位**（即原 from_unit）取。
        """
        if self.factor == 0:
            raise ValueError("换算系数为 0，无法求逆")
        factor = Decimal(1) / self.factor
        offset = -self.offset / self.factor
        return Conversion(
            from_unit=self.to_unit,
            to_unit=self.from_unit,
            factor=factor,
            offset=offset,
            precision=UNIT_PRECISION.get(self.from_unit),
            label=self.label,
            note=f"{self.note}（逆向）" if self.note else "逆向换算",
            scope=self.scope,
        )


@dataclass(frozen=True)
class ConversionResult:
    """换算结果。``ok=False`` 时 ``value`` 为 ``None``，``error`` 说明为什么。

    * ``value`` —— **精确换算值**（十进制精确运算），判定比较用这个；
    * ``reported_value`` —— 按目标单位精度上报的值（``value`` 的取舍版），
      用来核对「这台仪器会把同一个结果显示成什么」；
    * ``precision`` / ``step`` —— 目标单位有意义的小数位与步长，
      判定时用它判断「值是否恰好落在阈值的一个精度步长之内」。
    """

    ok: bool
    value: Optional[Decimal]
    from_unit: str
    to_unit: str
    factor: Optional[Decimal] = None
    #: 实际生效的小数位（换算精度）。
    precision: Optional[int] = None
    #: 精度步长（如 0.01），便于复核「值与阈值差了多少个精度步」。
    step: Optional[Decimal] = None
    #: 按精度上报的值（``value`` 取整后）。
    reported_value: Optional[Decimal] = None
    #: 上报值与精确值是否不同（说明精确值在小数位上比该单位能显示的更细）。
    rounded: bool = False
    label: str = ""
    note: str = ""
    error: str = ""

    def __bool__(self) -> bool:  # pragma: no cover - 便利写法
        return self.ok


#: 单位别名 → 规范写法。
_UNIT_ALIASES: dict[str, str] = {
    "g/l": "g/L",
    "g/dl": "g/dL",
    "mg/dl": "mg/dL",
    "mg/l": "mg/L",
    "mmol/l": "mmol/L",
    "umol/l": "µmol/L",
    "μmol/l": "µmol/L",
    "iu/l": "IU/L",
    "u/l": "U/L",
    "10*9/l": "10^9/L",
    "10^9/l": "10^9/L",
    "g/l(10^9/l)": "10^9/L",
    "10*3/ul": "10^3/uL",
    "10^3/ul": "10^3/uL",
    "k/ul": "10^3/uL",
    "10*12/l": "10^12/L",
    "10^12/l": "10^12/L",
    "t/l": "10^12/L",
    "%": "%",
    "percent": "%",
}


def normalize_unit(text: str | None) -> str:
    """把单位写法归一：去空格、统一 µ、大小写、``10*9/L`` → ``10^9/L``。"""
    if not text:
        return ""
    raw = _SPACE.sub("", str(text)).replace("μ", "µ")
    lowered = raw.lower()
    if lowered in _UNIT_ALIASES:
        return _UNIT_ALIASES[lowered]
    # "10*9/L" 之类：去掉 * 号再试
    simplified = raw.replace("*", "^")
    lowered = simplified.lower()
    if lowered in _UNIT_ALIASES:
        return _UNIT_ALIASES[lowered]
    return raw


#: 通用换算（与被测物无关）。
GLOBAL_CONVERSIONS: tuple[Conversion, ...] = (
    Conversion("g/L", "mg/dL", Decimal(100), precision=2, label="质量浓度换算", note="1 g/L = 100 mg/dL"),
    Conversion("mg/dL", "g/L", Decimal("0.01"), precision=2, label="质量浓度换算", note="1 mg/dL = 0.01 g/L"),
    Conversion("g/L", "g/dL", Decimal("0.1"), precision=3, label="质量浓度换算", note="1 g/L = 0.1 g/dL"),
    Conversion("g/dL", "g/L", Decimal(10), precision=2, label="质量浓度换算", note="1 g/dL = 10 g/L"),
    Conversion("mg/L", "g/L", Decimal("0.001"), precision=2, label="质量浓度换算", note="1 mg/L = 0.001 g/L"),
    Conversion("10^9/L", "10^3/uL", Decimal(1), precision=2, label="细胞计数换算", note="临床常规记法：10^9/L 与 K/uL 数值等价"),
    Conversion("10^3/uL", "10^9/L", Decimal(1), precision=2, label="细胞计数换算", note="临床常规记法：K/uL 与 10^9/L 数值等价"),
    Conversion("10^12/L", "10^9/L", Decimal("0.001"), precision=2, label="细胞计数换算", note="1 T/L = 0.001 ×10^9/L"),
    Conversion("10^9/L", "10^12/L", Decimal(1000), precision=2, label="细胞计数换算", note="1 ×10^9/L = 0.001 T/L"),
    Conversion("IU/L", "U/L", Decimal(1), precision=0, label="酶活单位换算", note="以 IU 校准的酶活单位与 U/L 等价（个别项目另行覆盖）"),
    Conversion("U/L", "IU/L", Decimal(1), precision=0, label="酶活单位换算", note="以 IU 校准的酶活单位与 U/L 等价（个别项目另行覆盖）"),
)

#: 物质相关换算：系数来自摩尔质量（mg/dL = mmol/L × MW / 10）。
SUBSTANCE_CONVERSIONS: tuple[Conversion, ...] = (
    Conversion("mmol/L", "mg/dL", Decimal("18.016"), precision=2, label="摩尔浓度换算（葡萄糖）", note="葡萄糖摩尔质量 180.16 g/mol", scope="2345-7"),
    Conversion("mg/dL", "mmol/L", Decimal("0.0555"), precision=3, label="摩尔浓度换算（葡萄糖）", note="葡萄糖摩尔质量 180.16 g/mol", scope="2345-7"),
    Conversion("mmol/L", "mg/dL", Decimal("6.006"), precision=2, label="摩尔浓度换算（尿素）", note="尿素摩尔质量 60.06 g/mol", scope="3094-0"),
    Conversion("mg/dL", "mmol/L", Decimal("0.1665"), precision=3, label="摩尔浓度换算（尿素）", note="尿素摩尔质量 60.06 g/mol", scope="3094-0"),
    Conversion("mmol/L", "mg/dL", Decimal("38.665"), precision=2, label="摩尔浓度换算（总胆固醇）", note="总胆固醇摩尔质量 386.65 g/mol", scope="2093-3"),
    Conversion("mg/dL", "mmol/L", Decimal("0.02586"), precision=3, label="摩尔浓度换算（总胆固醇）", note="总胆固醇摩尔质量 386.65 g/mol", scope="2093-3"),
    Conversion("µmol/L", "mg/dL", Decimal("0.011312"), precision=2, label="摩尔浓度换算（肌酐）", note="肌酐摩尔质量 88.4 g/mol", scope="2160-0"),
    Conversion("mg/dL", "µmol/L", Decimal("88.4"), precision=1, label="摩尔浓度换算（肌酐）", note="肌酐摩尔质量 88.4 g/mol", scope="2160-0"),
    Conversion("mmol/L", "mg/dL", Decimal("8.84"), precision=2, label="摩尔浓度换算（肌酐）", note="肌酐摩尔质量 88.4 g/mol", scope="2160-0"),
    Conversion("mg/dL", "mmol/L", Decimal("0.11312"), precision=3, label="摩尔浓度换算（肌酐）", note="肌酐摩尔质量 88.4 g/mol", scope="2160-0"),
    Conversion("µmol/L", "mg/dL", Decimal("0.058435"), precision=2, label="摩尔浓度换算（总胆红素）", note="总胆红素摩尔质量 584.35 g/mol", scope="1975-2"),
    Conversion("mg/dL", "µmol/L", Decimal("17.111"), precision=1, label="摩尔浓度换算（总胆红素）", note="总胆红素摩尔质量 584.35 g/mol", scope="1975-2"),
    Conversion("µmol/L", "mg/dL", Decimal("0.040078"), precision=2, label="摩尔浓度换算（钙）", note="钙摩尔质量 40.078 g/mol", scope="17861-6"),
    Conversion("mg/dL", "µmol/L", Decimal("24.953"), precision=1, label="摩尔浓度换算（钙）", note="钙摩尔质量 40.078 g/mol", scope="17861-6"),
)


def _default_table() -> "UnitTable":
    return UnitTable(
        global_conversions=GLOBAL_CONVERSIONS,
        substance_conversions=SUBSTANCE_CONVERSIONS,
        source="builtin",
    )


def _to_decimal(text: str) -> Optional[Decimal]:
    try:
        return Decimal(str(text).strip())
    except (InvalidOperation, ValueError, ArithmeticError):
        return None


def _conversion_from_row(row: dict[str, str]) -> tuple[Optional[Conversion], str]:
    """CSV 行 → ``(换算, 错误说明)``。错误行不进入换算表，进告警清单。"""
    from_unit = normalize_unit(row.get("from_unit"))
    to_unit = normalize_unit(row.get("to_unit"))
    if not from_unit or not to_unit:
        return None, "缺少 from_unit / to_unit"
    factor = _to_decimal(row.get("factor", ""))
    if factor is None:
        return None, f"factor 不是数值（{row.get('factor', '')!r}）"
    offset = _to_decimal(row.get("offset", "") or "0")
    if offset is None:
        return None, f"offset 不是数值（{row.get('offset', '')!r}）"
    precision_raw = (row.get("precision") or "").strip()
    precision: Optional[int] = None
    if precision_raw:
        try:
            precision = int(precision_raw)
        except ValueError:
            return None, f"precision 不是整数（{precision_raw!r}）"
        if precision < 0 or precision > 8:
            return None, f"precision 超出合理范围（{precision_raw!r}）"
    scope = (row.get("item_code") or "").strip()
    return (
        Conversion(
            from_unit=from_unit,
            to_unit=to_unit,
            factor=factor,
            offset=offset,
            precision=precision,
            label=(row.get("label") or "").strip(),
            note=(row.get("note") or "").strip(),
            scope=scope,
        ),
        "",
    )


class UnitTable:
    """一份单位换算表。

    ``global_conversions`` 为通用换算，``substance_conversions`` 为项目专用换算
    （``scope`` = 项目码）。查找顺序：**项目专用 → 通用 → 逆向**。
    """

    def __init__(
        self,
        global_conversions: tuple[Conversion, ...] = (),
        substance_conversions: tuple[Conversion, ...] = (),
        source: str = "builtin",
        warnings: tuple[str, ...] = (),
    ) -> None:
        self.global_conversions = tuple(global_conversions)
        self.substance_conversions = tuple(substance_conversions)
        self.source = source
        self.warnings = tuple(warnings)

    # ---- 构造 -----------------------------------------------------------
    @classmethod
    def default(cls) -> "UnitTable":
        """代码内默认换算表（永远可用的兜底）。"""
        return _default_table()

    @classmethod
    def load(cls, path: str | Path = UNIT_TABLE_CSV) -> "UnitTable":
        """从外部文件加载换算表。

        * 文件不存在 → 用默认表（不算故障，院内没配就是用默认）；
        * 文件读不了 / 一行都不可用 → **回退默认表并告警**（不静默使用半对的表）；
        * 文件可用 → 覆盖同名默认项 + 追加新项，逐行告警不可用行。
        """
        target = Path(path)
        if not target.exists():
            return cls.default()
        try:
            rows = list(_read_csv(target))
        except (OSError, UnicodeDecodeError, csv.Error) as exc:
            message = f"单位换算表 {target} 不可读（{exc}），已回退默认换算表"
            logger.warning(message)
            return cls.default()._with_warnings((message,))
        return cls._from_rows(rows, target)

    @classmethod
    def _from_rows(cls, rows: list[dict[str, str]], target: Path) -> "UnitTable":
        warnings: list[str] = []
        overrides: list[Conversion] = []
        for index, row in enumerate(rows, start=2):
            conversion, error = _conversion_from_row(row)
            if error:
                message = f"单位换算表第 {index} 行未采用：{error}"
                warnings.append(message)
                logger.warning("%s（%s）", message, target)
                continue
            assert conversion is not None
            overrides.append(conversion)
        if not overrides:
            message = f"单位换算表 {target} 没有可用行，已回退默认换算表"
            logger.warning(message)
            return cls.default()._with_warnings((message, *warnings))
        return cls._merge(cls.default(), overrides, source=str(target), warnings=tuple(warnings))

    @classmethod
    def _merge(
        cls,
        base: "UnitTable",
        overrides: list[Conversion],
        source: str = "builtin",
        warnings: tuple[str, ...] = (),
    ) -> "UnitTable":
        """外部表覆盖同名默认项，其余保留。"""
        merged: dict[tuple[str, str, str], Conversion] = {}
        for conversion in (*base.global_conversions, *base.substance_conversions):
            merged[conversion.key] = conversion
        for conversion in overrides:
            merged[conversion.key] = conversion
        globals_ = tuple(c for c in merged.values() if not c.scope)
        substances = tuple(c for c in merged.values() if c.scope)
        return cls(
            global_conversions=globals_,
            substance_conversions=substances,
            source=source,
            warnings=warnings,
        )

    def _with_warnings(self, warnings: tuple[str, ...]) -> "UnitTable":
        return UnitTable(
            global_conversions=self.global_conversions,
            substance_conversions=self.substance_conversions,
            source=self.source,
            warnings=warnings,
        )

    # ---- 查询 -----------------------------------------------------------
    def find(
        self,
        from_unit: str,
        to_unit: str,
        item_code: str | None = None,
    ) -> Optional[Conversion]:
        """查换算关系（正向优先，再试逆向）。查不到返回 ``None``。"""
        src = normalize_unit(from_unit)
        dst = normalize_unit(to_unit)
        if not src or not dst:
            return None
        for conversion in self.substance_conversions:
            if conversion.scope == (item_code or "") and conversion.from_unit == src and conversion.to_unit == dst:
                return conversion
        for conversion in self.global_conversions:
            if conversion.from_unit == src and conversion.to_unit == dst:
                return conversion
        # 逆向：表里有 a→b，但要的是 b→a
        for conversion in self.substance_conversions:
            if conversion.scope == (item_code or "") and conversion.from_unit == dst and conversion.to_unit == src:
                return conversion.invert()
        for conversion in self.global_conversions:
            if conversion.from_unit == dst and conversion.to_unit == src:
                return conversion.invert()
        return None

    @property
    def entry_count(self) -> int:
        return len(self.global_conversions) + len(self.substance_conversions)

    def summary(self) -> dict[str, object]:
        return {
            "source": self.source,
            "global_count": len(self.global_conversions),
            "substance_count": len(self.substance_conversions),
            "entry_count": self.entry_count,
            "warnings": list(self.warnings),
        }

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"UnitTable(source={self.source!r}, entries={self.entry_count})"


def _read_csv(path: Path) -> list[dict[str, str]]:
    """读 CSV（``#`` 开头为注释行，跳过）。"""
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        lines = [line for line in handle if not line.lstrip().startswith("#")]
    reader = csv.DictReader(lines)
    return [dict(row) for row in reader]


# ---------------------------------------------------------------------------
# 换算表注册表（进程内当前生效的那一份）
# ---------------------------------------------------------------------------

_TABLE_LOCK = threading.RLock()
_ACTIVE_TABLE: Optional[UnitTable] = None


def load_unit_table(path: str | Path = UNIT_TABLE_CSV) -> UnitTable:
    """装载外部覆盖文件并**设为当前生效表**（进程内只此一份）。"""
    global _ACTIVE_TABLE
    table = UnitTable.load(path)
    with _TABLE_LOCK:
        _ACTIVE_TABLE = table
    return table


def reload_unit_table(path: str | Path = UNIT_TABLE_CSV) -> UnitTable:
    """重新装载换算表（材料变更后热切换）。"""
    return load_unit_table(path)


def reset_unit_table() -> UnitTable:
    """丢弃外部覆盖，回到代码内默认表（测试与排障用）。"""
    global _ACTIVE_TABLE
    with _TABLE_LOCK:
        _ACTIVE_TABLE = None
    return UnitTable.default()


def current_unit_table() -> UnitTable:
    """当前生效的换算表。

    首次调用时按默认路径尝试装载外部覆盖文件（文件不存在则用默认表），
    之后一直复用同一份 —— 一次处理内不会看到半程切换。
    """
    global _ACTIVE_TABLE
    with _TABLE_LOCK:
        if _ACTIVE_TABLE is None:
            _ACTIVE_TABLE = UnitTable.load(UNIT_TABLE_CSV)
        return _ACTIVE_TABLE


def _quantize(value: Decimal, precision: Optional[int]) -> tuple[Decimal, bool, Optional[Decimal]]:
    """按精度取整，返回 ``(取整后值, 是否取整过, 精度步长)``。"""
    if precision is None:
        return value, False, None
    step = Decimal(1).scaleb(-precision)
    try:
        quantized = value.quantize(step, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ArithmeticError):
        # 数值过大超出 Decimal 上下文精度时保留原值，不做取整
        return value, False, step
    return quantized, quantized != value, step


def convert(
    value: Decimal,
    from_unit: str | None,
    to_unit: str | None,
    item_code: str | None = None,
    *,
    table: Optional[UnitTable] = None,
) -> ConversionResult:
    """把 ``value`` 从 ``from_unit`` 换算到 ``to_unit``。

    查不到换算关系时返回 ``ok=False``，调用方据此**不判定**（转人工）。
    换算结果按目标单位的精度取整，避免亚精度噪声把临界值判反。
    """
    active = table if table is not None else current_unit_table()
    src = normalize_unit(from_unit)
    dst = normalize_unit(to_unit)
    if not src or not dst:
        return ConversionResult(
            ok=False,
            value=None,
            from_unit=src,
            to_unit=dst,
            error=f"单位信息不全（原始单位 {src or '空'} → 阈值单位 {dst or '空'}）",
        )
    if src == dst:
        precision = UNIT_PRECISION.get(dst)
        reported, rounded, step = _quantize(value, precision)
        return ConversionResult(
            ok=True,
            value=value,
            from_unit=src,
            to_unit=dst,
            factor=Decimal(1),
            precision=precision,
            step=step,
            reported_value=reported,
            rounded=rounded,
            label="同单位比较",
            note="单位一致",
        )

    conversion = active.find(src, dst, item_code)
    if conversion is None:
        return ConversionResult(
            ok=False,
            value=None,
            from_unit=src,
            to_unit=dst,
            error=f"缺少 {src} → {dst} 的换算依据，不做判定（转人工）",
        )

    exact = value * conversion.factor + conversion.offset
    precision = conversion.effective_precision()
    reported, rounded, step = _quantize(exact, precision)
    notes = [conversion.note] if conversion.note else []
    if rounded:
        notes.append(f"按目标单位精度上报为 {reported} {conversion.to_unit}")
    return ConversionResult(
        ok=True,
        value=exact,
        from_unit=src,
        to_unit=dst,
        factor=conversion.factor,
        precision=precision,
        step=step,
        reported_value=reported,
        rounded=rounded,
        label=conversion.label,
        note="；".join(notes),
    )
