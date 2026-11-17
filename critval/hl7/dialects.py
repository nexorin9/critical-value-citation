"""HL7 厂商方言兼容矩阵：字段映射 · 段差异 · 日期格式 · 空值语义。

国内各 LIS 厂商的 ``ORU^R01`` 写法差异很大，院内换一家厂商就换一套写法：

* 项目码在 ``OBX-3`` 的第 1 个组件，还是厂商私有 ``Z`` 段？
* ``OBX-3`` 只给中文项目名、没给 LOINC 码，怎么办？
* 标本号在 ``OBR-3``，还是在标本段 ``SPM-2``？
* ``OBX-8`` 异常标志用 ``HH``/``LL`` 还是 ``A``/``N``？有没有厂商往这里塞
  完全无关的内容？
* 日期写 ``20260102083000``、``2026/01/02 08:30`` 还是 ``02/01/2026 08:30``？
* 复合字段用 ``^`` 分组件，还是用 ``:`` 分？
* 报文里混着全空的 ``OBX`` 分组，是跳过还是留痕？

本模块是**方言收口的唯一入口**：字段取哪里、命中后的置信度多少、日期怎么
解析、哪些字面量算空值，全部由一份**可外置的方言配置**说了算。院内换厂商
改一份 JSON（``LIS_FIELD_MAP``），**不改代码**；配置缺失或格式非法时回落到
默认方言并告警，不让配置问题打断入站。

**几条硬规则**（与本项目其余部分一致）：

1. **OBX-8 永不作判定权威** —— 它只是筛选与交叉校验用的异常标志（见
   ``spec.md`` 最小字段集）。方言只能决定「读不读、怎么归一」，不能决定
   「危不危急」；判定权威永远是院内阈值表。
2. **兜底必须标低置信** —— 项目码走 ``OBR-4`` 兜底、科室走 ``PV1-3`` 回落、
   观察时刻回落消息时间，一律标 ``low`` 并写明来源，值班人能看到这条依据
   需要人工核对。
3. **不认识就不猜** —— 日期格式不在配置集合内、字段号越界、值落在配置的
   空值字面量集合里，一律当缺，不做「大概是这个意思」的补齐。
4. **空 OBX 分组不静默丢弃** —— 按方言策略跳过或保留，但都要在消息
   ``notes`` 里留痕。

**源码溯源**：本模块在源参考仓库中**没有对应实现**（源项目不含任何医疗
协议，见 ``spec.md`` → ``## 开源对照`` 的「无直接溯源（新建）」行），机制
上参考其 ``config`` 单例 + 运行期整体切换的做法：方言配置集中在一处、
可外置加载、加载失败回退默认值并告警。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence

from critval.hl7.er7 import (
    Encoding,
    HL7Message,
    Segment,
    parse_message,
    parse_timestamp,
)
from critval.hl7.primitives import decode_frame, patient_reference

LOGGER = logging.getLogger(__name__)

__all__ = [
    "BUILTIN_DIALECTS",
    "DEFAULT_DIALECT",
    "Dialect",
    "DialectError",
    "DialectObservation",
    "DialectOruMessage",
    "FieldHit",
    "FieldRef",
    "MappedField",
    "ZSegmentRule",
    "dialect_from_env",
    "load_dialect",
    "parse_oru_r01",
    "resolve_dialect",
]


class DialectError(ValueError):
    """方言配置本身有问题（JSON 非法 / 字段结构不认识）。"""


# ---------------------------------------------------------------------------
# 字段引用
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FieldRef:
    """一个字段引用：段名 + 字段号 + 组件号。

    ``component=0`` 表示取整个字段原样（不分组件）；``component=n`` 表示取
    第 n 个组件（1 起）。``label`` 缺省时自动生成 ``段-字段.组件`` 形式的
    可读来源标签 —— 这个标签会跟着事件落库，是「这条依据从哪来」的人话
    版本。
    """

    segment: str
    field: int
    component: int = 0
    label: str = ""

    @property
    def source_label(self) -> str:
        if self.label:
            return self.label
        if self.component:
            return f"{self.segment}-{self.field}.{self.component}"
        return f"{self.segment}-{self.field}"

    def to_dict(self) -> dict[str, object]:
        return {
            "segment": self.segment,
            "field": self.field,
            "component": self.component,
            "label": self.source_label,
        }

    @classmethod
    def parse(cls, text: str) -> "FieldRef":
        """解析紧凑写法 ``OBX-3.1`` / ``MSH-10`` / ``ZLR-2``。"""
        raw = (text or "").strip()
        if not raw or "-" not in raw:
            raise DialectError(f"字段引用写法不认识：{text!r}（应为 段-字段[.组件]）")
        segment, _, rest = raw.partition("-")
        segment = segment.strip().upper()
        if not segment:
            raise DialectError(f"字段引用缺少段名：{text!r}")
        component = 0
        if "." in rest:
            rest, _, comp_text = rest.partition(".")
            try:
                component = int(comp_text)
            except ValueError as exc:  # pragma: no cover - 由上层校验兜住
                raise DialectError(f"组件号不是整数：{text!r}") from exc
        try:
            field_no = int(rest)
        except ValueError as exc:
            raise DialectError(f"字段号不是整数：{text!r}") from exc
        if field_no < 1 or component < 0:
            raise DialectError(f"字段号 / 组件号越界：{text!r}")
        return cls(segment=segment, field=field_no, component=component)


@dataclass(frozen=True)
class MappedField:
    """字段映射链上的一环：取哪个字段 + 取到后的置信度 + 值的性质。

    ``confidence`` 取 ``high`` / ``medium`` / ``low``：

    * ``high`` —— 标准字段精确命中；
    * ``medium`` —— 厂商私有段显式承载（非标准位置，但确实是这个字段）；
    * ``low`` —— 兜底与文本匹配，**需人工确认**。

    ``kind`` 说明这个位置上的值是什么性质：

    * ``code`` —— 必须是编码（含数字）；纯文本（中文项目名）不当码用，
      否则会把阈值表匹配到错误的行；
    * ``text`` —— 只作名称，不作码；
    * ``any`` —— 原样接受。
    """

    ref: FieldRef
    confidence: str = "high"
    kind: str = "any"

    @property
    def source_label(self) -> str:
        return self.ref.source_label

    def to_dict(self) -> dict[str, object]:
        return {
            "ref": self.ref.source_label,
            "segment": self.ref.segment,
            "field": self.ref.field,
            "component": self.ref.component,
            "confidence": self.confidence,
            "kind": self.kind,
        }

    @classmethod
    def from_dict(cls, data: object) -> "MappedField":
        if isinstance(data, str):
            return cls(ref=FieldRef.parse(data))
        if not isinstance(data, Mapping):
            raise DialectError(f"字段映射项不认识：{data!r}")
        raw_ref = data.get("ref")
        if not raw_ref:
            raise DialectError(f"字段映射项缺少 ref：{data!r}")
        ref = FieldRef.parse(str(raw_ref)) if isinstance(raw_ref, str) else _ref_from_dict(raw_ref)
        confidence = str(data.get("confidence", "high"))
        if confidence not in {"high", "medium", "low"}:
            raise DialectError(f"置信度取值不认识：{confidence!r}")
        kind = str(data.get("kind", "any"))
        if kind not in {"code", "text", "any"}:
            raise DialectError(f"字段性质取值不认识：{kind!r}")
        return cls(ref=ref, confidence=confidence, kind=kind)


def _ref_from_dict(data: Mapping[str, object]) -> FieldRef:
    segment = str(data.get("segment", "")).strip().upper()
    if not segment:
        raise DialectError(f"字段映射项缺少 segment：{data!r}")
    try:
        field_no = int(data.get("field", 0))
        component = int(data.get("component", 0))
    except (TypeError, ValueError) as exc:
        raise DialectError(f"字段映射项字段号 / 组件号不是整数：{data!r}") from exc
    if field_no < 1 or component < 0:
        raise DialectError(f"字段映射项字段号 / 组件号越界：{data!r}")
    return FieldRef(
        segment=segment,
        field=field_no,
        component=component,
        label=str(data.get("label", "")),
    )


#: 默认的项目码形态：标识符（字母数字与 ``.`` ``_`` ``-``）。
#: 「血红蛋白」这种中文项目名不符合 —— 把它当码用，阈值表要么匹配不上、
#: 要么匹配到别的项目行。院内若用中文当项目码，把 ``item_code_pattern``
#: 调宽即可（方言配置项，不改代码）。
DEFAULT_ITEM_CODE_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._\-]*$"

_PATTERN_CACHE: dict[str, "re.Pattern[str]"] = {}


def _compiled_pattern(pattern: str) -> "re.Pattern[str]":
    cached = _PATTERN_CACHE.get(pattern)
    if cached is None:
        try:
            cached = re.compile(pattern)
        except re.error as exc:
            raise DialectError(f"项目码形态正则不合法：{pattern!r}（{exc}）") from exc
        _PATTERN_CACHE[pattern] = cached
    return cached


def _looks_like_code(value: str, dialect: "Dialect") -> bool:
    """判断这个值是不是「像样的项目码」（按方言配置的形态正则）。"""
    if not value:
        return False
    return bool(_compiled_pattern(dialect.item_code_pattern).match(value))


@dataclass(frozen=True)
class ZSegmentRule:
    """厂商私有 ``Z`` 段的取值规则。

    不少厂商把检验项目码塞在自定义段里（如 ``ZLR|1|718-7|血红蛋白``）。
    ``set_id_field`` 指向的字段与 ``OBX-1`` 的 SET-ID 对齐，用来把 Z 段与
    对应的 OBX 配对；配不上时只有在「只有一个 Z 段 + 只有一个 OBX」时
    才按唯一配对处理，并在 notes 里留痕。
    """

    segment: str = "ZLR"
    set_id_field: int = 1
    item_code_field: int = 2
    item_name_field: int = 3

    def to_dict(self) -> dict[str, object]:
        return {
            "segment": self.segment,
            "set_id_field": self.set_id_field,
            "item_code_field": self.item_code_field,
            "item_name_field": self.item_name_field,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "ZSegmentRule":
        def _int(key: str, default: int) -> int:
            try:
                return int(data.get(key, default))
            except (TypeError, ValueError) as exc:
                raise DialectError(f"Z 段规则的 {key} 不是整数：{data!r}") from exc

        segment = str(data.get("segment", "ZLR")).strip().upper()
        if not segment:
            raise DialectError(f"Z 段规则的 segment 为空：{data!r}")
        return cls(
            segment=segment,
            set_id_field=_int("set_id_field", 1),
            item_code_field=_int("item_code_field", 2),
            item_name_field=_int("item_name_field", 3),
        )


@dataclass(frozen=True)
class FieldHit:
    """一次字段命中：值 + 来源 + 置信度。"""

    value: str
    source: str
    confidence: str


# ---------------------------------------------------------------------------
# 方言配置
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Dialect:
    """一份厂商方言配置。

    每个业务字段都有**两级链**：主链与兜底链。取值顺序是
    「主链 → 厂商私有 Z 段 → 兜底链」，兜底链命中一律标 ``low`` 置信度。
    """

    name: str
    description: str = ""
    message_control_id: tuple[MappedField, ...] = ()
    specimen_no: tuple[MappedField, ...] = ()
    specimen_no_fallback: tuple[MappedField, ...] = ()
    item_code: tuple[MappedField, ...] = ()
    item_code_fallback: tuple[MappedField, ...] = ()
    item_name: tuple[MappedField, ...] = ()
    item_name_fallback: tuple[MappedField, ...] = ()
    dept_code: tuple[MappedField, ...] = ()
    result_status: tuple[MappedField, ...] = ()
    observed_at: tuple[MappedField, ...] = ()
    z_item_code: Optional[ZSegmentRule] = None
    #: 异常标志（OBX-8）取哪个字段。默认取第 1 个组件 —— 标准里 OBX-8 是
    #: 编码型字段（``A^Abnormal^HL70078``），取整段会把码表一起带进来。
    obx8: tuple[MappedField, ...] = ()
    #: ``advisory`` = 读 OBX-8 并按词表归一（仅供交叉校验）；``ignore`` = 该
    #: 方言的 OBX-8 不可用，整段丢弃。
    obx8_mode: str = "advisory"
    #: 兜底链的适用范围：``single_obx`` = 只在「一条消息一条结果」时用
    #: 订单级通用标识兜底；``all`` = 每条 OBX 都兜底。
    item_code_fallback_scope: str = "single_obx"
    #: 项目码形态正则：不符合这个形态的值不当项目码用（当项目名处理）。
    item_code_pattern: str = DEFAULT_ITEM_CODE_PATTERN
    #: 厂商异常标志词表 → 归一后的标准写法（键大写）。
    obx8_vocabulary: Mapping[str, str] = field(default_factory=dict)
    #: 方言自有的日期格式（``strptime`` 格式串）；标准 HL7 TS 不在此列。
    date_formats: tuple[str, ...] = ()
    #: 视为「空值」的字面量（大写比较）。
    empty_values: tuple[str, ...] = ("", "NULL", "NONE", "N/A", "-")
    #: 除 ``^`` 之外还认的组件分隔符（部分厂商用 ``:`` 或 ``,``）。
    extra_component_separators: tuple[str, ...] = ()
    #: 空 OBX 分组策略：``skip`` 跳过并留痕；``keep`` 保留（后续落 unparsed）。
    empty_obx_policy: str = "skip"

    # -- 便捷读取 ---------------------------------------------------------
    def empty_value_set(self) -> frozenset[str]:
        return frozenset(value.strip().upper() for value in self.empty_values if value.strip())

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "description": self.description,
            "message_control_id": [m.to_dict() for m in self.message_control_id],
            "specimen_no": [m.to_dict() for m in self.specimen_no],
            "specimen_no_fallback": [m.to_dict() for m in self.specimen_no_fallback],
            "item_code": [m.to_dict() for m in self.item_code],
            "item_code_fallback": [m.to_dict() for m in self.item_code_fallback],
            "item_name": [m.to_dict() for m in self.item_name],
            "item_name_fallback": [m.to_dict() for m in self.item_name_fallback],
            "dept_code": [m.to_dict() for m in self.dept_code],
            "result_status": [m.to_dict() for m in self.result_status],
            "observed_at": [m.to_dict() for m in self.observed_at],
            "z_item_code": self.z_item_code.to_dict() if self.z_item_code else None,
            "obx8": [m.to_dict() for m in self.obx8],
            "obx8_mode": self.obx8_mode,
            "item_code_fallback_scope": self.item_code_fallback_scope,
            "item_code_pattern": self.item_code_pattern,
            "obx8_vocabulary": dict(self.obx8_vocabulary),
            "date_formats": list(self.date_formats),
            "empty_values": list(self.empty_values),
            "extra_component_separators": list(self.extra_component_separators),
            "empty_obx_policy": self.empty_obx_policy,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "Dialect":
        """从 JSON 对象装配一份方言配置。"""

        def _chain(key: str) -> tuple[MappedField, ...]:
            raw = data.get(key, [])
            if raw in (None, ""):
                return ()
            if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
                raise DialectError(f"字段映射链 {key} 应为数组：{raw!r}")
            return tuple(MappedField.from_dict(item) for item in raw)

        name = str(data.get("name", "")).strip()
        if not name:
            raise DialectError("方言配置缺少 name")

        obx8_mode = str(data.get("obx8_mode", "advisory"))
        if obx8_mode not in {"advisory", "ignore"}:
            raise DialectError(f"obx8_mode 取值不认识：{obx8_mode!r}")

        empty_obx_policy = str(data.get("empty_obx_policy", "skip"))
        if empty_obx_policy not in {"skip", "keep"}:
            raise DialectError(f"empty_obx_policy 取值不认识：{empty_obx_policy!r}")

        scope = str(data.get("item_code_fallback_scope", "single_obx"))
        if scope not in {"single_obx", "all"}:
            raise DialectError(f"item_code_fallback_scope 取值不认识：{scope!r}")

        pattern = str(data.get("item_code_pattern", DEFAULT_ITEM_CODE_PATTERN))
        _compiled_pattern(pattern)  # 形态不合法要当场报，不等到解析时

        raw_vocab = data.get("obx8_vocabulary", {}) or {}
        if not isinstance(raw_vocab, Mapping):
            raise DialectError("obx8_vocabulary 应为对象")
        vocabulary = {str(k).strip().upper(): str(v).strip().upper() for k, v in raw_vocab.items()}

        z_rule: Optional[ZSegmentRule] = None
        raw_z = data.get("z_item_code")
        if raw_z:
            if not isinstance(raw_z, Mapping):
                raise DialectError("z_item_code 应为对象")
            z_rule = ZSegmentRule.from_dict(raw_z)

        return cls(
            name=name,
            description=str(data.get("description", "")),
            message_control_id=_chain("message_control_id"),
            specimen_no=_chain("specimen_no"),
            specimen_no_fallback=_chain("specimen_no_fallback"),
            item_code=_chain("item_code"),
            item_code_fallback=_chain("item_code_fallback"),
            item_name=_chain("item_name"),
            item_name_fallback=_chain("item_name_fallback"),
            dept_code=_chain("dept_code"),
            result_status=_chain("result_status"),
            observed_at=_chain("observed_at"),
            z_item_code=z_rule,
            obx8=_chain("obx8"),
            obx8_mode=obx8_mode,
            item_code_fallback_scope=scope,
            item_code_pattern=pattern,
            obx8_vocabulary=vocabulary,
            date_formats=tuple(str(item) for item in (data.get("date_formats") or ())),
            empty_values=tuple(str(item) for item in (data.get("empty_values") or ())),
            extra_component_separators=tuple(
                str(item) for item in (data.get("extra_component_separators") or ())
            ),
            empty_obx_policy=empty_obx_policy,
        )


# ---------------------------------------------------------------------------
# 内置方言
# ---------------------------------------------------------------------------


def _chain(*entries: tuple[str, str, str]) -> tuple[MappedField, ...]:
    """构造字段映射链：``("OBX-3.1", "high", "code")`` → ``MappedField``。"""
    return tuple(
        MappedField(ref=FieldRef.parse(ref), confidence=confidence, kind=kind)
        for ref, confidence, kind in entries
    )


#: 默认方言：标准 HL7 v2 ``ORU^R01``。
DEFAULT_DIALECT = Dialect(
    name="default",
    description="标准 HL7 v2 ORU^R01（字段按规范取值）",
    message_control_id=_chain(("MSH-10", "high", "any")),
    specimen_no=_chain(("OBR-3.1", "high", "any")),
    specimen_no_fallback=_chain(("SPM-2.1", "low", "any")),
    item_code=_chain(("OBX-3.1", "high", "code")),
    item_code_fallback=_chain(("OBR-4.1", "low", "code")),
    item_name=_chain(("OBX-3.2", "high", "any")),
    item_name_fallback=_chain(("OBR-4.2", "low", "any")),
    dept_code=_chain(("PV1-3.1", "high", "any"), ("PV1-7.1", "low", "any")),
    result_status=_chain(("OBR-25", "high", "any"), ("OBX-11", "high", "any")),
    observed_at=_chain(("OBX-14", "high", "any"), ("MSH-7", "low", "any")),
    obx8=_chain(("OBX-8.1", "high", "any")),
    z_item_code=None,
    obx8_mode="advisory",
    obx8_vocabulary={},
    date_formats=(),
    empty_values=("", "NULL", "NONE", "N/A", "-"),
    extra_component_separators=(),
    empty_obx_policy="skip",
)

#: 厂商 A：项目码由私有 ``ZLR`` 段承载，``OBX-3`` 常为空。
_VENDOR_ZSEG = Dialect(
    name="vendor_zseg",
    description="项目码由厂商私有 Z 段（ZLR）承载，OBX-3 可能为空",
    message_control_id=_chain(("MSH-10", "high", "any")),
    specimen_no=_chain(("OBR-3.1", "high", "any")),
    specimen_no_fallback=_chain(("SPM-2.1", "low", "any")),
    item_code=_chain(("OBX-3.1", "high", "code")),
    item_code_fallback=_chain(("OBR-4.1", "low", "code")),
    item_name=_chain(("OBX-3.2", "high", "any")),
    item_name_fallback=_chain(("OBR-4.2", "low", "any")),
    dept_code=_chain(("PV1-3.1", "high", "any"), ("PV1-7.1", "low", "any")),
    result_status=_chain(("OBR-25", "high", "any"), ("OBX-11", "high", "any")),
    observed_at=_chain(("OBX-14", "high", "any"), ("MSH-7", "low", "any")),
    obx8=_chain(("OBX-8.1", "high", "any")),
    z_item_code=ZSegmentRule(segment="ZLR", set_id_field=1, item_code_field=2, item_name_field=3),
    obx8_mode="advisory",
    obx8_vocabulary={},
    date_formats=(),
    empty_values=("", "NULL", "NONE", "N/A", "-"),
    extra_component_separators=(),
    empty_obx_policy="skip",
)

#: 厂商 B：标本号在标本段 ``SPM-2``，``OBR-3`` 留空。
_VENDOR_SPM = Dialect(
    name="vendor_spm",
    description="标本号在标本段 SPM-2，OBR-3 留空",
    message_control_id=_chain(("MSH-10", "high", "any")),
    specimen_no=_chain(("SPM-2.1", "high", "any")),
    specimen_no_fallback=_chain(("OBR-3.1", "low", "any")),
    item_code=_chain(("OBX-3.1", "high", "code")),
    item_code_fallback=_chain(("OBR-4.1", "low", "code")),
    item_name=_chain(("OBX-3.2", "high", "any")),
    item_name_fallback=_chain(("OBR-4.2", "low", "any")),
    dept_code=_chain(("PV1-3.1", "high", "any"), ("PV1-7.1", "low", "any")),
    result_status=_chain(("OBR-25", "high", "any"), ("OBX-11", "high", "any")),
    observed_at=_chain(("OBX-14", "high", "any"), ("MSH-7", "low", "any")),
    obx8=_chain(("OBX-8.1", "high", "any")),
    z_item_code=None,
    obx8_mode="advisory",
    obx8_vocabulary={},
    date_formats=(),
    empty_values=("", "NULL", "NONE", "N/A", "-"),
    extra_component_separators=(),
    empty_obx_policy="skip",
)

#: 厂商 C：日期写 ``2026/01/02 08:30`` 一类自有格式，空值写 ``NULL``。
_VENDOR_LEGACY_DATE = Dialect(
    name="vendor_legacy_date",
    description="日期用厂商自有格式（斜杠 / 横杠 / 日在前），空值字面量含 NULL 与「空」",
    message_control_id=_chain(("MSH-10", "high", "any")),
    specimen_no=_chain(("OBR-3.1", "high", "any")),
    specimen_no_fallback=_chain(("SPM-2.1", "low", "any")),
    item_code=_chain(("OBX-3.1", "high", "code")),
    item_code_fallback=_chain(("OBR-4.1", "low", "code")),
    item_name=_chain(("OBX-3.2", "high", "any")),
    item_name_fallback=_chain(("OBR-4.2", "low", "any")),
    dept_code=_chain(("PV1-3.1", "high", "any"), ("PV1-7.1", "low", "any")),
    result_status=_chain(("OBR-25", "high", "any"), ("OBX-11", "high", "any")),
    observed_at=_chain(("OBX-14", "high", "any"), ("MSH-7", "low", "any")),
    obx8=_chain(("OBX-8.1", "high", "any")),
    z_item_code=None,
    obx8_mode="advisory",
    obx8_vocabulary={},
    date_formats=("%Y/%m/%d %H:%M", "%Y-%m-%d %H:%M:%S", "%d/%m/%Y %H:%M"),
    empty_values=("", "NULL", "NONE", "N/A", "-", "空"),
    extra_component_separators=(),
    empty_obx_policy="skip",
)

#: 厂商 D：复合字段用冒号分隔（``718-7:血红蛋白``）。
_VENDOR_ALT_COMPONENT = Dialect(
    name="vendor_alt_component",
    description="复合字段用冒号分隔组件，不是标准的 ^ 分隔",
    message_control_id=_chain(("MSH-10", "high", "any")),
    specimen_no=_chain(("OBR-3.1", "high", "any")),
    specimen_no_fallback=_chain(("SPM-2.1", "low", "any")),
    item_code=_chain(("OBX-3.1", "high", "code")),
    item_code_fallback=_chain(("OBR-4.1", "low", "code")),
    item_name=_chain(("OBX-3.2", "high", "any")),
    item_name_fallback=_chain(("OBR-4.2", "low", "any")),
    dept_code=_chain(("PV1-3.1", "high", "any"), ("PV1-7.1", "low", "any")),
    result_status=_chain(("OBR-25", "high", "any"), ("OBX-11", "high", "any")),
    observed_at=_chain(("OBX-14", "high", "any"), ("MSH-7", "low", "any")),
    obx8=_chain(("OBX-8.1", "high", "any")),
    z_item_code=None,
    obx8_mode="advisory",
    obx8_vocabulary={},
    date_formats=(),
    empty_values=("", "NULL", "NONE", "N/A", "-"),
    extra_component_separators=(":",),
    empty_obx_policy="skip",
)

#: 厂商 E：``OBX-8`` 里塞的是与检验无关的内容，整段丢弃。
_VENDOR_OBX8_IGNORE = Dialect(
    name="vendor_obx8_ignore",
    description="厂商在 OBX-8 填的是非检验内容，异常标志整体忽略",
    message_control_id=_chain(("MSH-10", "high", "any")),
    specimen_no=_chain(("OBR-3.1", "high", "any")),
    specimen_no_fallback=_chain(("SPM-2.1", "low", "any")),
    item_code=_chain(("OBX-3.1", "high", "code")),
    item_code_fallback=_chain(("OBR-4.1", "low", "code")),
    item_name=_chain(("OBX-3.2", "high", "any")),
    item_name_fallback=_chain(("OBR-4.2", "low", "any")),
    dept_code=_chain(("PV1-3.1", "high", "any"), ("PV1-7.1", "low", "any")),
    result_status=_chain(("OBR-25", "high", "any"), ("OBX-11", "high", "any")),
    observed_at=_chain(("OBX-14", "high", "any"), ("MSH-7", "low", "any")),
    obx8=_chain(("OBX-8.1", "high", "any")),
    z_item_code=None,
    obx8_mode="ignore",
    obx8_vocabulary={},
    date_formats=(),
    empty_values=("", "NULL", "NONE", "N/A", "-"),
    extra_component_separators=(),
    empty_obx_policy="skip",
)

#: 厂商 F：异常标志用 ``HH`` / ``LL`` 词表，需要归一到 ``H`` / ``L``。
_VENDOR_OBX8_VOCAB = Dialect(
    name="vendor_obx8_vocab",
    description="异常标志用 HH/LL 词表，需归一到 H/L",
    message_control_id=_chain(("MSH-10", "high", "any")),
    specimen_no=_chain(("OBR-3.1", "high", "any")),
    specimen_no_fallback=_chain(("SPM-2.1", "low", "any")),
    item_code=_chain(("OBX-3.1", "high", "code")),
    item_code_fallback=_chain(("OBR-4.1", "low", "code")),
    item_name=_chain(("OBX-3.2", "high", "any")),
    item_name_fallback=_chain(("OBR-4.2", "low", "any")),
    dept_code=_chain(("PV1-3.1", "high", "any"), ("PV1-7.1", "low", "any")),
    result_status=_chain(("OBR-25", "high", "any"), ("OBX-11", "high", "any")),
    observed_at=_chain(("OBX-14", "high", "any"), ("MSH-7", "low", "any")),
    obx8=_chain(("OBX-8.1", "high", "any")),
    z_item_code=None,
    obx8_mode="advisory",
    obx8_vocabulary={"HH": "H", "LL": "L", "H": "H", "L": "L", "N": "N", "A": "A"},
    date_formats=(),
    empty_values=("", "NULL", "NONE", "N/A", "-"),
    extra_component_separators=(),
    empty_obx_policy="skip",
)

#: 厂商 G：报文里混着全空的 ``OBX`` 分组，要留痕而不是静默跳过。
_VENDOR_EMPTY_OBX = Dialect(
    name="vendor_empty_obx",
    description="报文含全空 OBX 分组，保留并落 unparsed（不静默丢弃）",
    message_control_id=_chain(("MSH-10", "high", "any")),
    specimen_no=_chain(("OBR-3.1", "high", "any")),
    specimen_no_fallback=_chain(("SPM-2.1", "low", "any")),
    item_code=_chain(("OBX-3.1", "high", "code")),
    item_code_fallback=_chain(("OBR-4.1", "low", "code")),
    item_name=_chain(("OBX-3.2", "high", "any")),
    item_name_fallback=_chain(("OBR-4.2", "low", "any")),
    dept_code=_chain(("PV1-3.1", "high", "any"), ("PV1-7.1", "low", "any")),
    result_status=_chain(("OBR-25", "high", "any"), ("OBX-11", "high", "any")),
    observed_at=_chain(("OBX-14", "high", "any"), ("MSH-7", "low", "any")),
    obx8=_chain(("OBX-8.1", "high", "any")),
    z_item_code=None,
    obx8_mode="advisory",
    obx8_vocabulary={},
    date_formats=(),
    empty_values=("", "NULL", "NONE", "N/A", "-"),
    extra_component_separators=(),
    empty_obx_policy="keep",
)

#: 内置方言表：名字 → 配置。
BUILTIN_DIALECTS: dict[str, Dialect] = {
    dialect.name: dialect
    for dialect in (
        DEFAULT_DIALECT,
        _VENDOR_ZSEG,
        _VENDOR_SPM,
        _VENDOR_LEGACY_DATE,
        _VENDOR_ALT_COMPONENT,
        _VENDOR_OBX8_IGNORE,
        _VENDOR_OBX8_VOCAB,
        _VENDOR_EMPTY_OBX,
    )
}


# ---------------------------------------------------------------------------
# 加载
# ---------------------------------------------------------------------------


def load_dialect(path: str | Path) -> Dialect:
    """从 JSON 文件加载一份方言配置。

    文件不存在或内容非法时抛 :class:`DialectError` —— 由
    :func:`resolve_dialect` 决定是否回落到默认方言。
    """
    file_path = Path(path)
    try:
        raw = file_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise DialectError(f"方言配置文件读不了：{file_path}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DialectError(f"方言配置文件不是合法 JSON：{file_path}（{exc}）") from exc
    if not isinstance(data, Mapping):
        raise DialectError(f"方言配置文件顶层应为对象：{file_path}")
    return Dialect.from_dict(data)


def resolve_dialect(spec: str | Path | None) -> Dialect:
    """按名字、路径或内联 JSON 取一份方言配置。

    顺序：内置名字 → 内联 JSON → 磁盘路径。都不中或加载失败时**回落到默认
    方言并告警** —— 配置问题不该打断入站，但一定要在日志里说清楚。

    这正是 ``LIS_FIELD_MAP`` 配置位认的三种写法（任务 id 4）：院内换厂商时
    只改这一个环境变量 —— 写内置方言名、写方言 JSON 文件路径，或直接写一
    段内联 JSON —— 不改代码。
    """
    if spec is None or (isinstance(spec, str) and not str(spec).strip()):
        return DEFAULT_DIALECT
    key = str(spec).strip()
    builtin = BUILTIN_DIALECTS.get(key)
    if builtin is not None:
        return builtin
    if key.startswith("{"):
        try:
            data = json.loads(key)
        except json.JSONDecodeError as exc:
            LOGGER.warning("内联方言 JSON 解析失败，回落到默认方言：%s", exc)
            return DEFAULT_DIALECT
        if not isinstance(data, Mapping):
            LOGGER.warning("内联方言 JSON 顶层应为对象，回落到默认方言")
            return DEFAULT_DIALECT
        try:
            return Dialect.from_dict(data)
        except DialectError as exc:
            LOGGER.warning("内联方言 JSON 不合法，回落到默认方言：%s", exc)
            return DEFAULT_DIALECT
    try:
        return load_dialect(key)
    except DialectError as exc:
        LOGGER.warning("方言配置 %r 加载失败，回落到默认方言：%s", key, exc)
        return DEFAULT_DIALECT


def dialect_from_env(getter: Optional[Callable[[str], Optional[str]]] = None) -> Dialect:
    """按 ``LIS_FIELD_MAP`` 配置位取一份方言（任务 id 4）。

    ``getter`` 缺省读 ``critval.config.Config.get_env`` —— 配置面统一管环境
    变量，业务代码不直接读 ``os.environ``。
    """
    if getter is None:
        from critval.config import Config

        getter = Config.get_env
    try:
        spec = getter("LIS_FIELD_MAP")
    except Exception:  # noqa: BLE001 —— 配置面异常不该打断入站
        LOGGER.warning("读取 LIS_FIELD_MAP 失败，回落到默认方言", exc_info=True)
        return DEFAULT_DIALECT
    return resolve_dialect(spec)


# ---------------------------------------------------------------------------
# 取值
# ---------------------------------------------------------------------------


def _split_components(raw: str, separators: Sequence[str]) -> tuple[str, ...]:
    """按给定分隔符切组件；一个都不命中时整段算一个组件。"""
    if not raw:
        return ()
    for separator in separators:
        if separator and separator in raw:
            return tuple(part.strip() for part in raw.split(separator))
    return (raw.strip(),)


def _clean(value: str, dialect: Dialect) -> str:
    """去空白 + 空值字面量归一。"""
    text = (value or "").strip()
    if not text:
        return ""
    if text.upper() in dialect.empty_value_set():
        return ""
    return text


def _read_ref(
    message: HL7Message,
    ref: FieldRef,
    encoding: Encoding,
    dialect: Dialect,
    current: Optional[Segment] = None,
) -> str:
    """按字段引用取值（空值字面量归空，组件按方言分隔符切）。

    **同名段多实例时逐个找第一个非空值** —— 官方样例报文里同一条消息带两个
    ``OBR``：前一个是空壳（``OBR|1||||||...``），后一个才带标本号与项目。
    只看第一个 ``OBR`` 会让整条消息因「缺标本号」落 unparsed。
    """
    if current is not None and ref.segment == current.name:
        segments: Sequence[Segment] = (current,)
    else:
        segments = message.find_all(ref.segment)
    for segment in segments:
        raw = segment.field(ref.field)
        if not raw:
            continue
        if ref.component:
            parts = _split_components(
                raw, (encoding.component, *dialect.extra_component_separators)
            )
            if len(parts) < ref.component:
                continue
            value = _clean(parts[ref.component - 1], dialect)
        else:
            value = _clean(raw, dialect)
        if value:
            return value
    return ""


def _resolve_chain(
    message: HL7Message,
    chain: Sequence[MappedField],
    encoding: Encoding,
    dialect: Dialect,
    current: Optional[Segment] = None,
) -> Optional[FieldHit]:
    """按链取值，命中第一个非空值即返回。"""
    for mapped in chain:
        value = _read_ref(message, mapped.ref, encoding, dialect, current=current)
        if value:
            return FieldHit(
                value=value,
                source=mapped.ref.source_label,
                confidence=mapped.confidence,
            )
    return None


def _parse_time(text: str, dialect: Dialect) -> Optional[datetime]:
    """按方言配置的日期格式解析时刻；不认识返回 ``None``（不猜）。

    **统一归到「发送方本地墙钟时间」**：带时区偏移的时间戳去掉偏移、保留
    墙钟读数。一条报文里 ``OBX-14``（无偏移）与 ``MSH-7``（带偏移）会同时
    出现，若一部分带时区、一部分不带，判定规则按生效期比较时会抛
    ``TypeError``（naive 与 aware 不能比）。
    """
    value = (text or "").strip()
    if not value:
        return None
    standard = parse_timestamp(value)
    if standard is not None:
        return standard.replace(tzinfo=None)
    for fmt in dialect.date_formats:
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def _resolve_z_item(
    message: HL7Message,
    obx: Segment,
    dialect: Dialect,
) -> tuple[Optional[FieldHit], str]:
    """厂商私有 Z 段里的项目码与项目名（按 SET-ID 与 OBX 配对）。

    返回 ``(项目码命中, 项目名)``；没有配上的 Z 段时两者都为空。
    """
    rule = dialect.z_item_code
    if rule is None:
        return None, ""
    candidates = [seg for seg in message.segments if seg.name == rule.segment]
    if not candidates:
        return None, ""
    set_id = obx.field(1)
    chosen: Optional[Segment] = None
    for segment in candidates:
        if set_id and segment.field(rule.set_id_field) == set_id:
            chosen = segment
            break
    if chosen is None:
        if len(candidates) == 1 and len(message.find_all("OBX")) == 1:
            chosen = candidates[0]
    if chosen is None:
        return None, ""
    name = _clean(chosen.field(rule.item_name_field), dialect)
    code = _clean(chosen.field(rule.item_code_field), dialect)
    if not code:
        return None, name
    return (
        FieldHit(
            value=code,
            source=f"{rule.segment}-{rule.item_code_field}",
            confidence="medium",
        ),
        name,
    )


def _fallback_allowed(message: HL7Message, dialect: Dialect) -> bool:
    """订单级通用标识（``OBR-4``）能不能用来兜底这条结果的项目码。

    默认只在「一条消息一条结果」时兜底：危急值推送的常态是一条消息一条
    结果；一条消息里塞了多条结果时，订单级标识归属不到具体某一条（官方
    样例报文里 ``OBR-4`` 是过敏原组合标识，同一条消息里还跟着心率、血氧
    等生命体征），硬兜底会把组合标识安到不相干的结果上。宁可落 unparsed
    转人工，也不猜。院内确认本院报文形态后可把范围改成 ``all``。
    """
    if dialect.item_code_fallback_scope == "all":
        return True
    return len(message.find_all("OBX")) <= 1


def _is_empty_obx(obx: Segment, dialect: Dialect, encoding: Encoding) -> bool:
    """全空 OBX 分组：项目码与观察值都没有内容。"""
    holder = HL7Message(segments=(obx,), encoding=encoding)
    for ref in (FieldRef("OBX", 3, 1), FieldRef("OBX", 5)):
        if _read_ref(holder, ref, encoding, dialect, current=obx):
            return False
    return True


# ---------------------------------------------------------------------------
# 解析结果
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DialectObservation:
    """一条 OBX（按方言配置解析后的结果）。"""

    item_code: str
    item_name: str
    item_code_source: str
    item_code_confidence: str
    value_raw: str
    unit: str
    reference_range: str
    abnormal_flag: str
    result_status: str
    observed_at_raw: str
    observed_at: Optional[datetime]
    set_id: str = ""
    notes: tuple[str, ...] = ()
    #: 项目码的**文本候选**（厂商只给了名字没给码时的兜底素材）。
    #: 上层拿它去阈值表的项目名里匹配（``critval.hl7.textmatch``）。
    item_code_text: str = ""
    item_code_text_source: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "item_code": self.item_code,
            "item_name": self.item_name,
            "item_code_source": self.item_code_source,
            "item_code_confidence": self.item_code_confidence,
            "item_code_text": self.item_code_text,
            "item_code_text_source": self.item_code_text_source,
            "value_raw": self.value_raw,
            "unit": self.unit,
            "reference_range": self.reference_range,
            "abnormal_flag": self.abnormal_flag,
            "result_status": self.result_status,
            "observed_at_raw": self.observed_at_raw,
            "set_id": self.set_id,
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class DialectOruMessage:
    """一条 ``ORU^R01`` 报文按方言解析后的结构。"""

    message_type: str
    message_control_id: str
    message_datetime: Optional[datetime]
    specimen_no: str
    specimen_no_source: str
    specimen_no_confidence: str
    dept_code: str
    patient_ref: str
    result_status: str
    observations: tuple[DialectObservation, ...] = ()
    notes: tuple[str, ...] = ()
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
# 主解析
# ---------------------------------------------------------------------------


def parse_oru_r01(raw: str | bytes, dialect: Dialect = DEFAULT_DIALECT) -> DialectOruMessage:
    """按给定方言把一条 ``ORU^R01`` 报文解析成结构化消息。

    这是入站侧的**唯一取字段入口**：``normalize.py`` 只负责把这里的结构
    转成事件模型，不再自己取字段 —— 方言差异全部收口在本模块。
    """
    text, _encoding_name = decode_frame(raw)
    message = parse_message(text)
    encoding = message.encoding
    notes: list[str] = []

    message_type = message.message_type
    if message_type and not message_type.startswith("ORU^R01"):
        notes.append(f"消息类型为 {message_type}，非 ORU^R01，按检验结果消息处理")

    mcid_hit = _resolve_chain(
        message, dialect.message_control_id, encoding, dialect
    )
    message_control_id = mcid_hit.value if mcid_hit else ""

    message_datetime = _parse_time(
        message.first("MSH").field(7) if message.first("MSH") else "", dialect
    )

    specimen_hit = _resolve_chain(message, dialect.specimen_no, encoding, dialect)
    if specimen_hit is None:
        specimen_hit = _resolve_chain(
            message, dialect.specimen_no_fallback, encoding, dialect
        )
    specimen_no = specimen_hit.value if specimen_hit else ""
    specimen_source = specimen_hit.source if specimen_hit else ""
    specimen_confidence = specimen_hit.confidence if specimen_hit else ""

    dept_hit = _resolve_chain(message, dialect.dept_code, encoding, dialect)
    dept_code = dept_hit.value if dept_hit else ""

    pid = message.first("PID")
    patient_ref = (
        patient_reference(pid.component(3, 1, encoding)) if pid is not None else ""
    )

    observations: list[DialectObservation] = []
    for obx in message.find_all("OBX"):
        if _is_empty_obx(obx, dialect, encoding):
            if dialect.empty_obx_policy == "skip":
                notes.append("空 OBX 分组（项目码与观察值均为空）已按方言策略跳过")
                continue
            notes.append("空 OBX 分组（项目码与观察值均为空）按方言策略保留，将落 unparsed 记录")

        obs_notes: list[str] = []

        # --- 项目码：主链 → 厂商 Z 段 → 兜底链 -------------------------
        # ``text_candidate``：项目**名**的候选（OBX-3 里写的是名字时）。
        # ``code_text_candidate``：项目**码**的文本候选 —— 厂商只给了名字没给码
        # 时，上层拿它去阈值表的项目名里匹配（任务 id 4 的文本兜底）。
        text_candidate = ""
        code_text_candidate = ""
        code_text_source = ""
        code_hit: Optional[FieldHit] = None
        for mapped in dialect.item_code:
            value = _read_ref(message, mapped.ref, encoding, dialect, current=obx)
            if not value:
                continue
            if mapped.kind == "code" and not _looks_like_code(value, dialect):
                # 纯文本（中文项目名）不当码用 —— 否则会把阈值表匹配到错的行
                if not text_candidate:
                    text_candidate = value
                if not code_text_candidate:
                    code_text_candidate = value
                    code_text_source = mapped.ref.source_label
                continue
            code_hit = FieldHit(
                value=value,
                source=mapped.ref.source_label,
                confidence=mapped.confidence,
            )
            break
        z_name = ""
        if code_hit is None:
            code_hit, z_name = _resolve_z_item(message, obx, dialect)
        if code_hit is None and _fallback_allowed(message, dialect):
            fallback_hit = _resolve_chain(
                message, dialect.item_code_fallback, encoding, dialect, current=obx
            )
            if fallback_hit is not None:
                if _looks_like_code(fallback_hit.value, dialect):
                    code_hit = fallback_hit
                elif not code_text_candidate:
                    # 兜底链给的是**项目名**而不是项目码 —— 不能拿它当码用
                    # （阈值表要么匹配不上，要么匹配到别的项目行）。留一份
                    # 文本候选，上层拿它去阈值表的项目名里匹配（任务 id 4）；
                    # 匹配不上就落 unparsed 转人工，**不猜**。
                    code_text_candidate = fallback_hit.value
                    code_text_source = fallback_hit.source
        if code_hit is not None and code_hit.confidence == "low":
            obs_notes.append(
                f"项目码来自 {code_hit.source} 兜底匹配（低置信，需人工核对）"
            )

        item_code = code_hit.value if code_hit else ""
        item_code_source = code_hit.source if code_hit else ""
        item_code_confidence = code_hit.confidence if code_hit else ""

        # --- 项目名 ------------------------------------------------------
        # 顺序要紧：**OBX-3 里的原文优先**。官方样例报文里 OBR-4 是过敏原
        # 组合标识，同一条消息里还跟着心率等生命体征；若先取 OBR-4.2，
        # 心率那条结果会被安上「Grass Pollen Mix」这个名字。
        name_hit = _resolve_chain(
            message, dialect.item_name, encoding, dialect, current=obx
        )
        if text_candidate:
            item_name = text_candidate
        elif name_hit is not None:
            item_name = name_hit.value
        else:
            item_name = ""
            if _fallback_allowed(message, dialect):
                fallback_name = _resolve_chain(
                    message, dialect.item_name_fallback, encoding, dialect, current=obx
                )
                if fallback_name is not None:
                    item_name = fallback_name.value
            if not item_name:
                # 厂商私有 Z 段里的项目名
                item_name = z_name
            if not item_name:
                # 连兜底都不适用时，宁可把 OBX-3 的原文当名称，也不要挂上
                # 订单级的名字（那是别的项目的名字）。
                raw_name = _read_ref(
                    message, FieldRef("OBX", 3, 1), encoding, dialect, current=obx
                )
                item_name = raw_name

        # --- 观察值 / 单位 / 参考区间 ------------------------------------
        value_raw = _read_ref(message, FieldRef("OBX", 5), encoding, dialect, current=obx)
        repetitions = obx.repetitions(5, encoding)
        if len(repetitions) > 1 and value_raw:
            obs_notes.append("观察值含重复分隔（~），取首个重复值")
        unit = _read_ref(message, FieldRef("OBX", 6), encoding, dialect, current=obx)
        reference_range = _read_ref(
            message, FieldRef("OBX", 7), encoding, dialect, current=obx
        )

        # --- 异常标志（只作交叉校验，不作判定权威） ----------------------
        if dialect.obx8_mode == "ignore":
            abnormal_flag = ""
        else:
            flag_hit = _resolve_chain(
                message, dialect.obx8, encoding, dialect, current=obx
            )
            raw_flag = flag_hit.value.upper() if flag_hit else ""
            abnormal_flag = (
                dialect.obx8_vocabulary.get(raw_flag, raw_flag) if raw_flag else ""
            )

        # --- 结果状态 ---------------------------------------------------
        status_hit = _resolve_chain(
            message, dialect.result_status, encoding, dialect, current=obx
        )
        result_status = status_hit.value if status_hit else ""

        # --- 观察时刻 ---------------------------------------------------
        observed_at_raw = ""
        observed_at: Optional[datetime] = None
        for mapped in dialect.observed_at:
            raw = _read_ref(message, mapped.ref, encoding, dialect, current=obx)
            if not raw:
                continue
            moment = _parse_time(raw, dialect)
            if moment is None:
                obs_notes.append(
                    f"观察时刻「{raw}」不在方言配置的日期格式集合内，未解析"
                )
                continue
            observed_at_raw = raw
            observed_at = moment
            if mapped.confidence == "low":
                obs_notes.append("观察时刻缺失（OBX-14），已回落消息时间（MSH-7）")
            break

        observations.append(
            DialectObservation(
                item_code=item_code,
                item_name=item_name,
                item_code_source=item_code_source,
                item_code_confidence=item_code_confidence,
                item_code_text=code_text_candidate,
                item_code_text_source=code_text_source,
                value_raw=value_raw,
                unit=unit,
                reference_range=reference_range,
                abnormal_flag=abnormal_flag,
                result_status=result_status,
                observed_at_raw=observed_at_raw,
                observed_at=observed_at,
                set_id=obx.field(1),
                notes=tuple(obs_notes),
            )
        )

    result_status = ""
    for segment in message.find_all("OBR"):
        result_status = segment.field(25)
        if result_status:
            break

    return DialectOruMessage(
        message_type=message_type,
        message_control_id=message_control_id,
        message_datetime=message_datetime,
        specimen_no=specimen_no,
        specimen_no_source=specimen_source,
        specimen_no_confidence=specimen_confidence,
        dept_code=dept_code,
        patient_ref=patient_ref,
        result_status=result_status,
        observations=tuple(observations),
        notes=tuple(notes),
        dialect=dialect.name,
    )
