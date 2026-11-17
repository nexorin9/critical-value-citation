"""HL7 v2 ER7 报文解析（只做语法层，不做临床语义判断）。

ER7 的结构：一条消息由若干**段**组成，段之间用 ``\\r`` 分隔；段内用
字段分隔符 ``|`` 分字段，字段内用组件分隔符 ``^`` 分组件，组件内用
``&`` 分子组件；``~`` 是重复分隔符，``\\`` 是转义符。

**MSH 段是特例**：``MSH-1`` 就是字段分隔符本身，``MSH-2`` 才是编码字符。
所以按 ``|`` 切开后，``MSH`` 的第 n 个元素对应 ``MSH-(n+1)``，而其他段
第 n 个元素就是第 n 字段。``Segment.field(n)`` 把这层差异藏起来，
调用方只管按 HL7 字段号取值，不必在每个取字段的地方写 if。

这一层**只管语法**：不认识检验项目、不认识单位、不判断危急不危急。
语义判断在 ``critval/hl7/normalize.py``（规范化）与 ``critval/judge``（判定）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Optional

__all__ = [
    "Encoding",
    "HL7Message",
    "Segment",
    "parse_message",
    "parse_numeric",
    "parse_timestamp",
    "split_segments",
    "strip_frame",
]

#: MLLP 组帧符：帧以 VT 起、以 FS+CR 止。
MLLP_START = "\x0b"
MLLP_END = "\x1c\r"

DEFAULT_FIELD_SEP = "|"
DEFAULT_ENCODING_CHARS = "^~\\&"


@dataclass(frozen=True)
class Encoding:
    """HL7 编码字符（来自 ``MSH-2``）。"""

    field: str = DEFAULT_FIELD_SEP
    component: str = "^"
    repetition: str = "~"
    escape: str = "\\"
    subcomponent: str = "&"

    @classmethod
    def from_msh2(cls, msh2: str) -> "Encoding":
        """从 ``MSH-2`` 取编码字符；写得不标准时回落到默认 ``^~\\&``。"""
        text = (msh2 or "").strip()
        if len(text) < 4:
            return cls()
        return cls(
            field=DEFAULT_FIELD_SEP,
            component=text[0],
            repetition=text[1],
            escape=text[2],
            subcomponent=text[3],
        )


@dataclass(frozen=True)
class Segment:
    """一个 HL7 段。

    ``values`` 是按字段分隔符切开的原始片段，``values[0]`` 恒为段名。
    """

    name: str
    values: tuple[str, ...]
    raw: str = ""

    @property
    def is_msh(self) -> bool:
        return self.name == "MSH"

    def field(self, index: int) -> str:
        """按 HL7 字段号取值（1 起）；越界或缺字段返回空串。"""
        if index < 1:
            return ""
        if self.is_msh:
            if index == 1:
                # MSH-1 就是字段分隔符本身，切分后不在 values 里。
                return DEFAULT_FIELD_SEP
            pos = index - 1
        else:
            pos = index
        if pos < 0 or pos >= len(self.values):
            return ""
        return self.values[pos].strip()

    def components(self, index: int, encoding: Optional[Encoding] = None) -> tuple[str, ...]:
        """取某字段的组件列表（按编码字符拆）。"""
        enc = encoding or Encoding()
        value = self.field(index)
        if not value:
            return ()
        return tuple(part.strip() for part in value.split(enc.component))

    def component(self, index: int, component_index: int, encoding: Optional[Encoding] = None) -> str:
        """取某字段的第 n 个组件（1 起）；缺则返回空串。"""
        parts = self.components(index, encoding)
        if component_index < 1 or component_index > len(parts):
            return ""
        return parts[component_index - 1]

    def repetitions(self, index: int, encoding: Optional[Encoding] = None) -> tuple[str, ...]:
        """取某字段的重复值（按重复分隔符拆）。"""
        enc = encoding or Encoding()
        value = self.field(index)
        if not value:
            return ()
        return tuple(part.strip() for part in value.split(enc.repetition))

    def to_dict(self) -> dict[str, object]:
        return {"name": self.name, "fields": len(self.values) - 1}


@dataclass(frozen=True)
class HL7Message:
    """一条 HL7 v2 消息（按段顺序保存）。"""

    segments: tuple[Segment, ...]
    encoding: Encoding = Encoding()

    def first(self, name: str) -> Optional[Segment]:
        """取第一个同名段。"""
        for segment in self.segments:
            if segment.name == name:
                return segment
        return None

    def find_all(self, name: str) -> tuple[Segment, ...]:
        """取全部同名段（一条消息里可以有多条 OBX）。"""
        return tuple(segment for segment in self.segments if segment.name == name)

    @property
    def message_type(self) -> str:
        msh = self.first("MSH")
        return msh.field(9) if msh else ""

    @property
    def message_control_id(self) -> str:
        msh = self.first("MSH")
        return msh.field(10) if msh else ""

    @property
    def message_datetime(self) -> Optional[datetime]:
        msh = self.first("MSH")
        return parse_timestamp(msh.field(7)) if msh else None


def strip_frame(raw: str) -> str:
    """剥掉 MLLP 组帧符（``\\x0b`` 起、``\\x1c\\r`` 止）与首尾空行。"""
    text = raw or ""
    if MLLP_START in text:
        text = text.split(MLLP_START, 1)[1]
    if MLLP_END in text:
        text = text.split(MLLP_END, 1)[0]
    elif text.endswith("\x1c"):
        text = text[:-1]
    return text.strip("\r\n\x0b")


def split_segments(raw: str) -> list[str]:
    """按段分隔符切段；空段丢弃（尾部换行不算一段）。"""
    normalized = (raw or "").replace("\r\n", "\r").replace("\n", "\r")
    return [chunk for chunk in normalized.split("\r") if chunk.strip()]


def parse_message(raw: str) -> HL7Message:
    """把一段 ER7 文本解析成消息。

    MLLP 组帧符会被自动剥掉，所以 MLLP 收到的原始字节也能直接喂进来。
    """
    chunks = split_segments(strip_frame(raw))
    segments: list[Segment] = []
    encoding = Encoding()
    for chunk in chunks:
        name = chunk.split(DEFAULT_FIELD_SEP, 1)[0].strip()
        if not name:
            continue
        values = tuple(chunk.split(DEFAULT_FIELD_SEP))
        if name == "MSH" and len(values) > 1:
            encoding = Encoding.from_msh2(values[1])
        segments.append(Segment(name=name, values=values, raw=chunk))
    return HL7Message(segments=tuple(segments), encoding=encoding)


#: HL7 TS（时间戳）：``YYYY[MM[DD[HH[MM[SS[.S]]]]]][+/-ZZZZ]``。
_TS_RE = re.compile(
    r"^(?P<year>\d{4})(?P<month>\d{2})?(?P<day>\d{2})?"
    r"(?P<hour>\d{2})?(?P<minute>\d{2})?(?P<second>\d{2})?"
    r"(?:[.,]\d+)?(?P<offset>[+-]\d{4})?$"
)


def parse_timestamp(text: str) -> Optional[datetime]:
    """解析 HL7 时间戳；写法不认识或非法返回 ``None``（不猜）。"""
    value = (text or "").strip()
    if not value:
        return None
    match = _TS_RE.match(value)
    if not match:
        return None
    parts = match.groupdict()
    try:
        moment = datetime(
            int(parts["year"]),
            int(parts["month"] or 1),
            int(parts["day"] or 1),
            int(parts["hour"] or 0),
            int(parts["minute"] or 0),
            int(parts["second"] or 0),
        )
    except ValueError:
        return None
    offset = parts["offset"]
    if offset:
        sign = 1 if offset[0] == "+" else -1
        delta = timedelta(hours=int(offset[1:3]), minutes=int(offset[3:5])) * sign
        return moment.replace(tzinfo=timezone(delta))
    return moment


#: 纯数值（判定入参）。带比较符（``>250``）或文本（``阳性``）不算数值 ——
#: 拿不准时**不判定**，交由上层落 unparsed，绝不猜一个数。
_NUM_RE = re.compile(r"^[+-]?\d+(?:\.\d+)?$")


def parse_numeric(text: str) -> Optional[Decimal]:
    """解析数值型观察值；非纯数值返回 ``None``。"""
    value = (text or "").strip()
    if not value or not _NUM_RE.match(value):
        return None
    try:
        return Decimal(value)
    except InvalidOperation:  # pragma: no cover - 正则已挡住绝大多数
        return None
