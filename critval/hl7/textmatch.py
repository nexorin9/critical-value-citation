"""文本 → 检验项目码的兜底匹配（任务 id 4）。

**为什么需要它**：有些 LIS 厂商的 ``OBX-3``（检验项目码）是空的，只在订单级
的 ``OBR-4``（通用检验标识）写了一个**中文项目名**（「血红蛋白」）。拿这个名字
当项目码去查阈值表必然查不到 —— 阈值表的主键是项目码（如 ``718-7``）。

于是有了这条兜底链：

    OBX-3 缺项目码 → 取 OBR-4 的文本 → 拿文本去**阈值表的项目名**里匹配
    → 匹配上：项目码落成表内的码，置信度 ``low``（值班人需人工核对）
    → 匹配不上：仍按「缺项目码」处理，落 ``unparsed`` 转人工

**不猜**：匹配有歧义（多个项目名都命中）时返回「匹配不上」，宁可转人工，
也不把危急值安到另一个项目的阈值上。

匹配表由调用方提供（``(项目码, 项目名)`` 序列）—— 本模块不认识阈值表，
只认识「一个码 + 一个名字」，因此可以被任何院内项目清单复用。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable, Optional, Sequence

__all__ = [
    "ItemTextEntry",
    "ItemTextMatch",
    "ItemTextMatcher",
    "normalize_item_text",
]


#: 比对前先剥掉的标点与空白（中英文标点都算）。
_PUNCT_RE = re.compile(r"[\s\-_/\\.,;:()\[\]{}<>《》【】（）「」『』·、,。:;!?！？\"'`~^*&|/\\]+")


def normalize_item_text(text: str) -> str:
    """把项目名 / 项目码归一到「可比较」的形式。

    大小写、全角半角、空白与标点都不参与比较 —— 院内同一个项目在 LIS 里
    可能写成「血红蛋白」「血 红 蛋 白」「Hemoglobin」，阈值表里写「血红蛋白」。
    """
    if not text:
        return ""
    value = unicodedata.normalize("NFKC", str(text)).strip().casefold()
    return _PUNCT_RE.sub("", value)


@dataclass(frozen=True)
class ItemTextEntry:
    """一条可比对的条目：项目码 + 项目名（+ 可选别名）。"""

    item_code: str
    item_name: str = ""
    aliases: tuple[str, ...] = ()

    @classmethod
    def from_row(cls, row: object) -> "ItemTextEntry":
        """从阈值表行（``ThresholdRow``）取项目码与项目名。"""
        return cls(
            item_code=str(getattr(row, "item_code", "") or "").strip(),
            item_name=str(getattr(row, "item_name", "") or "").strip(),
        )


@dataclass(frozen=True)
class ItemTextMatch:
    """一次文本匹配的结果。"""

    item_code: str
    item_name: str = ""
    #: 命中方式：``code``（文本本身就是项目码）/ ``name``（项目名）/ ``alias``（别名）。
    matched_on: str = "name"

    @property
    def confidence(self) -> str:
        """兜底链来的项目码一律 ``low`` —— 需人工核对。"""
        return "low"


class ItemTextMatcher:
    """按项目名 / 别名把一段文本匹配到阈值表里的项目码。

    匹配顺序：项目码 → 项目名 → 别名 → 包含关系。
    **多个条目同时命中时判定为歧义，不返回结果** —— 危急值安错项目的后果，
    比晚几分钟转人工严重得多。
    """

    def __init__(self, entries: Iterable[ItemTextEntry | Sequence[str] | object]) -> None:
        self._entries: list[ItemTextEntry] = []
        for entry in entries or ():
            self._entries.append(self._coerce(entry))
        self._by_code: dict[str, ItemTextEntry] = {}
        self._by_name: dict[str, ItemTextEntry] = {}
        self._by_alias: dict[str, ItemTextEntry] = {}
        for entry in self._entries:
            if not entry.item_code:
                continue
            self._by_code.setdefault(normalize_item_text(entry.item_code), entry)
            name_key = normalize_item_text(entry.item_name)
            if name_key:
                self._by_name.setdefault(name_key, entry)
            for alias in entry.aliases:
                alias_key = normalize_item_text(alias)
                if alias_key:
                    self._by_alias.setdefault(alias_key, entry)

    # ---- 构造 -----------------------------------------------------------
    @staticmethod
    def _coerce(entry: object) -> ItemTextEntry:
        if isinstance(entry, ItemTextEntry):
            return entry
        if isinstance(entry, (tuple, list)):
            code = str(entry[0]) if len(entry) > 0 else ""
            name = str(entry[1]) if len(entry) > 1 else ""
            aliases = tuple(str(a) for a in entry[2]) if len(entry) > 2 and entry[2] else ()
            return ItemTextEntry(code.strip(), name.strip(), aliases)
        return ItemTextEntry.from_row(entry)

    # ---- 匹配 -----------------------------------------------------------
    def match(self, text: str) -> Optional[ItemTextMatch]:
        """拿一段文本去匹配；匹配不上或命中歧义都返回 ``None``。"""
        key = normalize_item_text(text)
        if not key:
            return None

        hit = self._by_code.get(key)
        if hit is not None:
            return ItemTextMatch(hit.item_code, hit.item_name, "code")

        hit = self._by_name.get(key)
        if hit is not None:
            return ItemTextMatch(hit.item_code, hit.item_name, "name")

        hit = self._by_alias.get(key)
        if hit is not None:
            return ItemTextMatch(hit.item_code, hit.item_name, "alias")

        # 包含关系：文本是项目名的一部分（或反过来）。命中多条 → 歧义 → 不匹配。
        candidates: list[ItemTextEntry] = []
        for entry in self._entries:
            for candidate in (entry.item_name, *entry.aliases):
                other = normalize_item_text(candidate)
                if len(other) < 2 or len(key) < 2:
                    continue
                if other in key or key in other:
                    candidates.append(entry)
                    break
        unique = {entry.item_code for entry in candidates}
        if len(unique) == 1:
            entry = candidates[0]
            return ItemTextMatch(entry.item_code, entry.item_name, "name")
        return None

    def match_by_name(self, text: str) -> Optional[ItemTextMatch]:
        """只按**项目名 / 别名**匹配（不把「文本本身就是项目码」算进来）。

        兜底链用它：``OBR-4`` 给的文本若恰好等于某个项目码，那本来就是码，
        不该被标成「文本匹配」而丢掉原有的来源标注。
        """
        key = normalize_item_text(text)
        if not key:
            return None
        hit = self._by_name.get(key) or self._by_alias.get(key)
        if hit is not None:
            return ItemTextMatch(hit.item_code, hit.item_name, "name")
        candidates: list[ItemTextEntry] = []
        for entry in self._entries:
            for candidate in (entry.item_name, *entry.aliases):
                other = normalize_item_text(candidate)
                if len(other) < 2 or len(key) < 2:
                    continue
                if other in key or key in other:
                    candidates.append(entry)
                    break
        unique = {entry.item_code for entry in candidates}
        if len(unique) == 1:
            entry = candidates[0]
            return ItemTextMatch(entry.item_code, entry.item_name, "name")
        return None

    def __len__(self) -> int:
        return len(self._entries)
