"""结构化精确索引 + 版本索引。

## 为什么要结构化这一路

纯词法召回解决不了「精确命中」类问题：制度里写的是"血红蛋白（718-7）"，值班人的
查询里可能只有"血红蛋白"，也可能只有项目码；阈值表更是一个项目码一行。靠
BM25 去猜"哪一行是血红蛋白"既慢又不准 —— 判定依据必须是**查表命中**，不是
"排第一的那条大概对"。

结构化索引把每个条目拆成可精确匹配的键值对：

* ``item_code`` —— 检验项目码（阈值表主键，也是制度条款点名的主键）；
* ``direction`` —— 危急方向（``low`` / ``high`` / ``both`` / ``none``）；
* ``unit`` —— 单位；
* ``version`` —— 材料版本号；
* ``section_title`` / ``clause_no`` —— 制度条款的位置；
* ``item_codes`` —— 条款正文里点名的项目码（**一个条款可点名多个项目**，属多值键）。

多键组合查询是 **AND 语义**：给 ``item_code=718-7`` + ``version=示例 v1.0``，
只命中同时满足两个键的条目。命中了哪些键会一并返回 —— 重排层据此加权，
值班人也能看懂"这条为什么算精确命中"。

多值键（``item_codes``）按**每个值分别建索引**：一条点名的了三个项目的条款，
查任意一个项目码都要能命中它；查询时按单值匹配，不按"整串相等"匹配。

## 版本索引的口径：失效版本不参与召回

阈值表与制度都会改版，新旧两版常常**同时躺在共享目录里**。按观察时刻择一
（``kb/version.py:select_version``）是判定层的事；索引层要保证的是：**在给定观察
时刻下，失效版本与未生效版本根本不该出现在候选里**。

因此 ``visible_at(at)`` 是硬过滤：

* ``effective_from <= at <= effective_to`` → 可见；
* ``effective_to < at`` → **已失效**，不参与召回；
* ``at < effective_from`` → **尚未生效**，不参与召回；
* 没写生效期的条目按"无期限"处理，始终可见。

不给 ``at`` 时**不做**时间过滤（调用方明确表示"我不按时刻筛"）—— 这与
"失效版本不参与召回"不矛盾：没有参考时刻就没有"失效"可言。
"""

from __future__ import annotations

from collections.abc import Iterable as IterableABC
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Iterable, Mapping, Optional

from critval.models import Citation

__all__ = [
    "IndexDocument",
    "KeyMatch",
    "StructuredHit",
    "StructuredIndex",
    "documents_from_fields",
    "explain_hit",
    "merge_field_values",
    "normalize_filters",
]


@dataclass(frozen=True)
class IndexDocument:
    """进入索引的一条材料条目。

    ``doc_id`` 必须**稳定且内容寻址**：同一份材料的同一条目在重建后拿到同一个
    id（材料指纹 + 条目定位），这样"覆盖"与"新增"能分得清，撤销旧版本时也能
    精确撤掉对应那几条。
    """

    doc_id: str
    source_name: str
    source_sha256: str
    kind: str  # threshold_row | policy_clause | corpus_line
    text: str = ""
    #: 单值结构化键值对（键名 → 值；值为空的不进索引）。
    fields: Mapping[str, str] = field(default_factory=dict)
    #: 多值结构化键（键名 → 若干个值；每个值单独建索引）。
    multi_fields: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    effective_from: Optional[date] = None
    effective_to: Optional[date] = None
    version: str = ""
    #: 跨版本稳定的条目标识（制度条款的 stable_id / 阈值行的行号标识）。
    stable_id: str = ""
    citation: Optional[Citation] = None

    def field(self, key: str) -> str:
        """取一个键的展示值（多值键用逗号拼起来）。"""
        if key in self.fields:
            return self.fields[key]
        values = self.multi_fields.get(key)
        if values:
            return ",".join(values)
        return ""

    def values_of(self, key: str) -> tuple[str, ...]:
        """取一个键的全部值（单值键返回一个，多值键返回多个）。"""
        if key in self.fields:
            return (self.fields[key],)
        return tuple(self.multi_fields.get(key, ()))

    def indexable(self) -> dict[str, tuple[str, ...]]:
        """本次要进倒排表的键值对。"""
        out: dict[str, tuple[str, ...]] = {}
        for key, value in self.fields.items():
            if value:
                out[key] = (value,)
        for key, values in self.multi_fields.items():
            kept = tuple(value for value in values if value)
            if kept:
                out[key] = kept
        return out

    def covers(self, at: date) -> bool:
        """在时刻 ``at`` 是否生效。"""
        if self.effective_from is not None and at < self.effective_from:
            return False
        if self.effective_to is not None and at > self.effective_to:
            return False
        return True

    def state_at(self, at: date) -> str:
        """在时刻 ``at`` 的状态：``active`` / ``expired`` / ``pending``。"""
        if self.effective_from is not None and at < self.effective_from:
            return "pending"
        if self.effective_to is not None and at > self.effective_to:
            return "expired"
        return "active"

    def to_dict(self) -> dict[str, Any]:
        return {
            "doc_id": self.doc_id,
            "source_name": self.source_name,
            "source_sha256": self.source_sha256,
            "kind": self.kind,
            "text": self.text,
            "fields": dict(self.fields),
            "multi_fields": {key: list(values) for key, values in self.multi_fields.items()},
            "effective_from": self.effective_from.isoformat() if self.effective_from else None,
            "effective_to": self.effective_to.isoformat() if self.effective_to else None,
            "version": self.version,
            "stable_id": self.stable_id,
        }


@dataclass(frozen=True)
class KeyMatch:
    """一个键上的命中说明（键名 + 命中的具体值）。

    重排层据此加权（"精确命中了几个维度"），值班人也据此看懂"这条为什么算
    精确命中"—— 只给一个布尔命中，等于让人自己猜。
    """

    key: str
    values: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"key": self.key, "values": list(self.values)}


@dataclass(frozen=True)
class StructuredHit:
    """一次结构化命中的结果。"""

    doc_id: str
    matched_keys: tuple[str, ...] = ()
    document: Optional[IndexDocument] = None
    #: 逐键命中说明（键 + 命中的值），顺序与查询请求的键顺序一致。
    key_matches: tuple[KeyMatch, ...] = ()

    @property
    def matched_values(self) -> dict[str, tuple[str, ...]]:
        """键 → 命中的值（多值键可能有多个命中值）。"""
        return {match.key: match.values for match in self.key_matches}

    def to_dict(self) -> dict[str, Any]:
        return {
            "doc_id": self.doc_id,
            "matched_keys": list(self.matched_keys),
            "matched_values": {
                key: list(values) for key, values in self.matched_values.items()
            },
            "document": self.document.to_dict() if self.document else None,
        }


class StructuredIndex:
    """多键组合精确索引 + 版本索引（生效期过滤）。"""

    def __init__(self) -> None:
        self._documents: dict[str, IndexDocument] = {}
        #: ``键 → 值 → doc_id 集合``
        self._key_index: dict[str, dict[str, set[str]]] = {}
        self._replacements = 0

    # —— 写入 ——

    def add(self, document: IndexDocument) -> bool:
        """加入（或覆盖）一条目。返回 ``True`` 表示这是一次覆盖。"""
        if not document.doc_id:
            raise ValueError("doc_id 不能为空")
        replaced = document.doc_id in self._documents
        if replaced:
            self.remove(document.doc_id)
        self._documents[document.doc_id] = document
        for key, values in document.indexable().items():
            bucket = self._key_index.setdefault(key, {})
            for value in values:
                bucket.setdefault(value, set()).add(document.doc_id)
        if replaced:
            self._replacements += 1
        return replaced

    def remove(self, doc_id: str) -> bool:
        """移除一条目。"""
        document = self._documents.pop(doc_id, None)
        if document is None:
            return False
        for key, values in document.indexable().items():
            bucket = self._key_index.get(key)
            if not bucket:
                continue
            for value in values:
                holders = bucket.get(value)
                if holders is None:
                    continue
                holders.discard(doc_id)
                if not holders:
                    bucket.pop(value, None)
            if not bucket:
                self._key_index.pop(key, None)
        return True

    def clear(self) -> None:
        self._documents.clear()
        self._key_index.clear()

    # —— 读取 ——

    def __len__(self) -> int:
        return len(self._documents)

    def __contains__(self, doc_id: object) -> bool:
        return doc_id in self._documents

    @property
    def doc_count(self) -> int:
        return len(self._documents)

    @property
    def replacements(self) -> int:
        return self._replacements

    def doc_ids(self) -> tuple[str, ...]:
        return tuple(self._documents.keys())

    def document(self, doc_id: str) -> Optional[IndexDocument]:
        return self._documents.get(doc_id)

    def values(self, key: str) -> tuple[str, ...]:
        """某个键上出现过哪些值。"""
        bucket = self._key_index.get(key)
        if not bucket:
            return ()
        return tuple(sorted(bucket))

    def keys(self) -> tuple[str, ...]:
        return tuple(sorted(self._key_index))

    def versions(self) -> dict[str, tuple[str, ...]]:
        """版本号 → 属于该版本的条目 id。"""
        out: dict[str, list[str]] = {}
        for doc_id, document in self._documents.items():
            if not document.version:
                continue
            out.setdefault(document.version, []).append(doc_id)
        return {version: tuple(sorted(ids)) for version, ids in sorted(out.items())}

    # —— 版本索引（生效期过滤）——

    def visible_at(self, at: date) -> tuple[str, ...]:
        """在时刻 ``at`` 生效的条目 id（失效与未生效版本都不在其中）。"""
        return tuple(
            sorted(
                doc_id
                for doc_id, document in self._documents.items()
                if document.covers(at)
            )
        )

    def expired_at(self, at: date) -> tuple[str, ...]:
        """在时刻 ``at`` 已失效的条目 id。"""
        return tuple(
            sorted(
                doc_id
                for doc_id, document in self._documents.items()
                if document.state_at(at) == "expired"
            )
        )

    def pending_at(self, at: date) -> tuple[str, ...]:
        """在时刻 ``at`` 尚未生效的条目 id。"""
        return tuple(
            sorted(
                doc_id
                for doc_id, document in self._documents.items()
                if document.state_at(at) == "pending"
            )
        )

    def visible_versions(self, at: date) -> dict[str, tuple[str, ...]]:
        """时刻 ``at`` 可见的版本号 → 条目 id。"""
        visible = set(self.visible_at(at))
        out: dict[str, list[str]] = {}
        for doc_id, document in self._documents.items():
            if doc_id not in visible or not document.version:
                continue
            out.setdefault(document.version, []).append(doc_id)
        return {version: tuple(sorted(ids)) for version, ids in sorted(out.items())}

    # —— 查询 ——

    def query(
        self,
        *,
        at: Optional[date] = None,
        **filters: Any,
    ) -> list[StructuredHit]:
        """多键组合查询（**键间 AND、键内 OR**）。

        ``at`` 给定时按生效期硬过滤（失效 / 未生效版本不参与召回）。

        ``filters`` 的值可以是单个字符串，也可以是字符串序列：

        * ``item_code="718-7"`` —— 该键必须命中这个值；
        * ``item_codes=("718-7", "2601-3")`` —— 该键命中**任意一个**即可
          （键内 OR），命中值会记进 ``key_matches``，融合层据此算覆盖率。

        键与键之间仍是 **AND**：查 ``item_code`` + ``unit`` 时，两键都要对上。

        返回的每条命中带 ``matched_keys``（命中的键，供重排层加权）与
        ``key_matches``（键 + 命中的具体值，供人工解释）。
        """
        wanted = normalize_filters(filters)
        if not wanted:
            candidates = set(self._documents)
        else:
            candidates: Optional[set[str]] = None
            for key, values in wanted.items():
                holders: set[str] = set()
                for value in values:
                    bucket = self._key_index.get(key)
                    if bucket:
                        holders |= set(bucket.get(value, ()))
                candidates = holders if candidates is None else (candidates & holders)
                if not candidates:
                    break
            candidates = candidates or set()

        hits: list[StructuredHit] = []
        for doc_id in sorted(candidates):
            document = self._documents.get(doc_id)
            if document is None:
                continue
            if at is not None and not document.covers(at):
                continue
            key_matches: list[KeyMatch] = []
            for key, accepted in wanted.items():
                hit_values = tuple(
                    value for value in document.values_of(key) if value in accepted
                )
                if hit_values:
                    key_matches.append(KeyMatch(key=key, values=hit_values))
            hits.append(
                StructuredHit(
                    doc_id=doc_id,
                    matched_keys=tuple(match.key for match in key_matches),
                    document=document,
                    key_matches=tuple(key_matches),
                )
            )
        return hits

    def query_keys(self, **filters: Any) -> tuple[str, ...]:
        """本次查询实际用到的键（请求顺序）—— 归一化覆盖率的分母。"""
        return tuple(normalize_filters(filters).keys())

    def stats(self) -> dict[str, Any]:
        return {
            "doc_count": self.doc_count,
            "keys": list(self.keys()),
            "replacements": self._replacements,
            "versions": {version: len(ids) for version, ids in self.versions().items()},
        }


def documents_from_fields(
    doc_id: str,
    source_name: str,
    source_sha256: str,
    kind: str,
    text: str,
    fields: Mapping[str, str],
    multi_fields: Optional[Mapping[str, Iterable[str]]] = None,
    **kwargs: Any,
) -> IndexDocument:
    """便捷构造：把结构化字段收成一条 ``IndexDocument``。"""
    return IndexDocument(
        doc_id=doc_id,
        source_name=source_name,
        source_sha256=source_sha256,
        kind=kind,
        text=text,
        fields={key: value for key, value in fields.items() if value},
        multi_fields={
            key: tuple(value for value in values if value)
            for key, values in (multi_fields or {}).items()
        },
        **kwargs,
    )


def merge_field_values(values: Iterable[str]) -> str:
    """把多个值拼成一个可读的复合值（逗号分隔）。

    仅用于展示；匹配走的是多值键的逐值索引（见 ``multi_fields``）。
    """
    seen: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.append(value)
    return ",".join(seen)


def normalize_filters(filters: Mapping[str, Any]) -> dict[str, tuple[str, ...]]:
    """把查询条件归一成 ``键 → 可接受值集合``。

    * 空值（``None`` / ``""``）直接丢掉 —— 相当于没提这个键；
    * 字符串 → 单值；序列 → 多值（键内 OR）。
    """
    wanted: dict[str, tuple[str, ...]] = {}
    for key, value in filters.items():
        if value is None:
            continue
        if isinstance(value, str):
            values = (value,) if value else ()
        elif isinstance(value, IterableABC):
            values = tuple(str(item) for item in value if item)
        else:
            values = (str(value),)
        if values:
            wanted[key] = values
    return wanted


def explain_hit(hit: StructuredHit) -> str:
    """一句话说明"这条为什么算精确命中"。"""
    if not hit.key_matches:
        return f"{hit.doc_id}（无条件命中）"
    parts = [
        f"{match.key}={'/'.join(match.values)}" for match in hit.key_matches
    ]
    return f"{hit.doc_id}（命中键：{'、'.join(parts)}）"
