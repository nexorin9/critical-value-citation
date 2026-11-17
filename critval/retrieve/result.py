"""召回结果条目与召回器协议 —— 评测器与召回实现之间的唯一接口。

## 为什么它住在召回层，而不是评测器里

评测器（``critval.evaluate``）只认一个协议：``retrieve(query, depth)`` 返回
**按名次排好**的结果条目。换召回做法（融合引擎 / 纯 BM25 基线）不该改评测
口径，所以「结果条目」与「召回器协议」这两个类型必须住在**召回层**里 —— 评测器
与召回实现都从这儿取，依赖方向是单向的（召回层 ← 评测层）。

早先把它们放在评测器里会绕成环：评测器 → 召回层（包初始化）→ 基线实现 →
评测器（此时评测器只初始化了一半，取不到 ``RetrievedItem``）。挪到召回层
之后环就断了，基线实现与评测器可以互相引用而不打架。

## 条目只带四样评测要用的东西

**键 / 名次 / 分 / 出处**。其余（稳定主键 / doc_id / 命中路径 / 项目码）
留着供排查用，不参与指标计算 —— 评测口径要窄，窄才不会各算各的。

## 协议里的 ``query`` 是什么

一条**标注查询**（``critval.evaluate.EvalQuery``：查询文本 + 结构化过滤条件 +
期望命中的条目键）。这里刻意**不 import** 评测器的类型（协议用结构化子类型
标注即可），否则又绕回环里；实现方按鸭子类型取 ``query.text`` 与
``query.filters`` 就行。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Protocol, Sequence

from critval.index.structured import IndexDocument
from critval.models import Citation

__all__ = [
    "RetrievedItem",
    "Retriever",
    "document_key",
]


@dataclass(frozen=True)
class RetrievedItem:
    """一条检索结果（评测器只认这四样：键 / 名次 / 分 / 出处）。"""

    key: str
    rank: int = 0
    score: float = 0.0
    citation: Optional[Citation] = None
    stable_key: str = ""
    doc_id: str = ""
    paths: tuple[str, ...] = ()
    item_code: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "rank": self.rank,
            "score": round(self.score, 6),
            "citation": self.citation.to_dict() if self.citation else None,
            "stable_key": self.stable_key,
            "doc_id": self.doc_id,
            "paths": list(self.paths),
            "item_code": self.item_code,
        }


class Retriever(Protocol):
    """召回器协议 —— 评测器只认这个，换召回做法不影响评测口径。

    ``query`` 是一条标注查询（带 ``text`` 与 ``filters`` 两条属性）。
    """

    def retrieve(self, query: Any, depth: int) -> Sequence[RetrievedItem]:
        """返回**按名次排好**的结果（最多 ``depth`` 条）。"""


def document_key(document: IndexDocument) -> str:
    """一个条目的**评测键** —— 评测集的真值与检索结果必须用同一个函数算。

    * 阈值表行 → ``threshold_row:{项目码}#{行号}``；
    * 制度条款 → ``policy_clause:{条款稳定标识}``；
    * 语料行 → ``corpus_line:{条款号}``。

    键里**不含**材料 sha256：语料与制度会改版，键跟着变就等于评测集每次都要
    重做；改版是否影响召回由版本索引那一层管，不混进评测指标。
    """
    kind = document.kind or "unknown"
    stable = document.stable_id or document.doc_id
    return f"{kind}:{stable}"
