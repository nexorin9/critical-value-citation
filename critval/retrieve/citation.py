"""出处随命中走 —— 引用收集、落库、空召回兜底与版本一致性断言。

## 这一层守的是「依据可核对」

值班人看到处置卡上写着「血红蛋白危急值，阈值 30 g/L，上报时限 10 分钟」，
要能当场问出三个问题：

1. **这条依据出在哪儿？** —— 文件名 + 第几行 + 哪一版。答不上来的依据
   不能上卡。
2. **召回不到依据时怎么办？** —— 说「没检索到」，转人工并留痕，
   **绝不编一条依据**（源产品 ``empty_response`` 槽位的同源语义）。
3. **同一张卡上的依据是不是同一套口径？** —— 阈值表 v1.0 配制度 v2.0 的卡
   自相矛盾（按旧阈值判的危急值，配的却是新版的时限），必须拒掉
   （同源产品 ``assert len(embd_nms) == 1, "Knowledge bases use different
   embedding models."`` 的语义，改写成「只允许用同一套材料版本」）。

## 出处五件套

``source_sha256``（材料指纹）+ ``paragraph_no``（段落序号）+ ``section_path``
（章节路径）+ ``line_no``（可读行号）+ ``file_version``（文件版本）。

前四项回答「在哪」，第五项锁定「哪一版」—— 缺了版本号，材料改版之后同一条
行号会指向完全不同的阈值，值班人按行号去翻表就会翻错。

## 为什么出处**随命中**落库，而不是出卡时才算

出卡（``critval.card``）只是**渲染**依据；依据本身来自召回。出处在召回
这一步就该定下来并落库：

* 召回阶段就能回答「这条依据是从哪一行来的」，不必等卡片渲染；
* 出卡被拒（版本混用）时，引用已经留在台账里，事后复核仍查得到「当时召回
  到了什么」；
* 空召回时**一条引用都不落** —— 台账里「有引用」就一定「真有依据」，
  不会出现「台账有引用但查无出处」的幽灵行。

## 审计留痕口径

空召回写一条 ``retrieve_empty_no_citation`` 审计：事件号、项目码、科室、
标本号、消息控制号、原因。**不落患者明细**（与台账其余审计同一纪律）。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Iterable, Optional, Sequence

from critval.kb.consistency import (
    VersionBinding,
    VersionConsistency,
    VersionMismatchError,
    check_version_consistency,
)
from critval.models import Citation

__all__ = [
    "AUDIT_EMPTY_RETRIEVAL",
    "CITATION_LOCATOR_FIELDS",
    "HUMAN_REVIEW_STATUS",
    "RetrievalOutcome",
    "apply_retrieval_outcome",
    "assert_versions_consistent",
    "citation_gaps",
    "citations_of_hits",
    "describe_citations",
    "persist_citations",
    "resolve_retrieval",
]

#: 空召回时事件转入的状态（与判定侧 ``judge.critical.HUMAN_REVIEW_STATUS``
#: 同一个值 —— 「转人工」在本产品里是一条状态，不是各模块各写一个）。
HUMAN_REVIEW_STATUS = "transferred_to_human"

#: 空召回的审计动作名。
AUDIT_EMPTY_RETRIEVAL = "retrieve_empty_no_citation"

#: 出处五件套（定位依据所需的全集）。
CITATION_LOCATOR_FIELDS = (
    "source_sha256",
    "paragraph_no",
    "section_path",
    "line_no",
    "file_version",
)


# ---------------------------------------------------------------------------
# 出处本身
# ---------------------------------------------------------------------------


def citation_gaps(citation: Optional[Citation], mode: str = "kind_aware") -> tuple[str, ...]:
    """这条出处**缺**哪些定位字段（空 tuple = 齐备）。

    * ``kind_aware``（默认）—— 制度条款要 sha256 + 段落序号 + 可读行号 +
      文件版本；阈值行没有「段落」这个概念，按 sha256 + 可读行号 + 文件版本
      判定（章节路径对行级条目没有意义，不计入缺项）。
    * ``strict`` —— 五件套缺一不可（章节路径也必须有）。

    没有出处（``citation is None``）一律算缺 —— 「没有出处」不是「出处齐备」。
    """
    if mode not in ("kind_aware", "strict"):
        raise ValueError(f"不认识的出处口径：{mode}")
    if citation is None:
        return ("citation",)
    missing: list[str] = []
    if not citation.source_sha256:
        missing.append("source_sha256")
    if citation.line_no is None:
        missing.append("line_no")
    if not citation.file_version:
        missing.append("file_version")
    if citation.kind == "policy_clause" and citation.paragraph_no is None:
        missing.append("paragraph_no")
    if mode == "strict":
        if citation.paragraph_no is None:
            missing.append("paragraph_no")
        if not citation.section_path:
            missing.append("section_path")
    return tuple(missing)


def citations_of_hits(hits: Iterable[Any]) -> tuple[Citation, ...]:
    """把一批召回命中里的出处收成引用序列（**去重、保序**）。

    去重按出处五件套：同一条依据被两路同时命中时只留一条（两路的分与命中
    说明留在命中对象上，出处本身不需要重复）。

    **降级层级随引用一起落库**（任务 id 32）：命中的 ``level`` 写进出处的
    ``level`` 字段 —— 台账里那条"降级捞到的依据"事后复核时能看出它是
    靠第几级降级拿到的，而不是直接命中的。
    """
    seen: set[tuple[Any, ...]] = set()
    citations: list[Citation] = []
    for hit in hits:
        citation = getattr(hit, "citation", None)
        if citation is None:
            continue
        level = int(getattr(hit, "level", 0) or 0)
        if level and citation.level != level:
            citation = replace(citation, level=level)
        key = (
            citation.source_sha256,
            citation.kind,
            citation.paragraph_no,
            citation.line_no,
            citation.file_version,
        )
        if key in seen:
            continue
        seen.add(key)
        citations.append(citation)
    return tuple(citations)


def describe_citations(citations: Sequence[Citation]) -> list[str]:
    """把引用渲染成一行行「文件 第 N 行 · 版本」（值班人看的样子）。"""
    lines: list[str] = []
    for citation in citations:
        gaps = citation_gaps(citation)
        locator = citation.locator
        if gaps:
            locator = f"{locator}（出处缺：{'、'.join(gaps)}）"
        lines.append(locator)
    return lines


def persist_citations(
    store: Any,
    event_id: str,
    card_id: str,
    citations: Sequence[Citation],
) -> int:
    """把引用逐条落库（``citations`` 表），返回落库条数。

    落库的是**出处本身**，不含任何患者明细；同一条依据重复落库会插两条 ——
    台账是流水，事后要看「召回阶段落过几次」时这是有意保留的。
    """
    count = 0
    for citation in citations:
        store.record_citation(event_id, card_id, citation)
        count += 1
    return count


# ---------------------------------------------------------------------------
# 版本一致性断言
# ---------------------------------------------------------------------------


def assert_versions_consistent(
    citations: Sequence[Citation] = (),
    *,
    binding: Optional[VersionBinding] = None,
    strict_pair: bool = False,
) -> VersionConsistency:
    """核对一次处置引用的材料版本是否自洽；不一致**抛错拒绝出卡**。

    抛的是 ``VersionMismatchError``（``code = "version_mismatch"``），值班台
    与台账按它分流。判定口径在 ``critval.kb.consistency``（任务 id 26 已落地），
    本函数是召回侧的**调用口** —— 召回出来的引用与出卡前生成的引用走同一道闸。
    """
    consistency = check_version_consistency(
        citations, binding=binding, strict_pair=strict_pair
    )
    if not consistency.ok:
        raise VersionMismatchError(consistency)
    return consistency


# ---------------------------------------------------------------------------
# 空召回兜底
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RetrievalOutcome:
    """一次召回的**处置结论**：引用 + 状态 + 审计动作。

    两条分支：

    * **有依据** —— ``citations`` 非空，状态不变（``status`` 为空串），
      审计动作也为空串（正常召回不必写异常审计）。
    * **没依据** —— ``citations`` **必须为空**，状态转
      ``transferred_to_human``，审计动作 ``retrieve_empty_no_citation``，
      ``empty_response`` 记下给值班人看的话术。
    """

    empty: bool
    status: str = ""
    citations: tuple[Citation, ...] = ()
    empty_response: str = ""
    audit_action: str = ""
    detail: str = ""
    #: 本次召回所处的**降级层级**（任务 id 32）：0 原样 / 1 关键词扩展 /
    #: 2 章节级兜底 / 3 无依据转人工。卡片按它标「依据来自第几级降级」。
    level: int = 0

    @property
    def transferred_to_human(self) -> bool:
        """是否转入人工处理。"""
        return self.empty

    def to_dict(self) -> dict[str, Any]:
        return {
            "empty": self.empty,
            "status": self.status,
            "transferred_to_human": self.transferred_to_human,
            "citation_count": len(self.citations),
            "empty_response": self.empty_response,
            "audit_action": self.audit_action,
            "detail": self.detail,
            "level": self.level,
            "citations": [citation.to_dict() for citation in self.citations],
        }


def resolve_retrieval(result: Any) -> RetrievalOutcome:
    """把一次召回结果解析成处置结论（引用 + 状态 + 审计动作）。

    **空召回时一条引用都不产出** —— 这是本产品最硬的一条线：没有依据就说
    没有依据，转人工并留痕，不编依据。
    """
    hits = tuple(getattr(result, "hits", ()) or ())
    empty_response = getattr(result, "empty_response", "") or ""
    level = int(getattr(result, "level", 0) or 0)
    if not hits:
        return RetrievalOutcome(
            empty=True,
            status=HUMAN_REVIEW_STATUS,
            citations=(),
            empty_response=empty_response,
            audit_action=AUDIT_EMPTY_RETRIEVAL,
            detail=(
                "召回 0 条依据：不产出任何引用，事件转人工处理并留痕"
                "（不编造依据）"
            ),
            level=level,
        )
    detail = "召回 {} 条依据，出处随命中落库".format(len(hits))
    if level > 0:
        detail = "{}（第 {} 级降级取得）".format(detail, level)
    return RetrievalOutcome(
        empty=False,
        status="",
        citations=citations_of_hits(hits),
        empty_response="",
        audit_action="",
        detail=detail,
        level=level,
    )


def apply_retrieval_outcome(
    store: Any,
    event_id: str,
    outcome: RetrievalOutcome,
    *,
    card_id: str = "",
    dept_code: str = "",
    specimen_no: str = "",
    item_code: str = "",
    message_control_id: str = "",
    actor: str = "",
) -> None:
    """把处置结论落到台账：引用落库 / 空召回转状态并写审计。

    * 有依据 → 引用逐条落 ``citations`` 表；
    * 没依据 → 事件状态转 ``transferred_to_human`` + 写一条
      ``retrieve_empty_no_citation`` 审计（**不落任何引用**）。

    审计只落标本号 / 项目码 / 科室 / 消息控制号 / 原因，不落患者明细。
    """
    if outcome.empty:
        store.set_status(event_id, outcome.status or HUMAN_REVIEW_STATUS)
        store.append_audit(
            outcome.audit_action or AUDIT_EMPTY_RETRIEVAL,
            event_id=event_id,
            actor=actor,
            dept_code=dept_code,
            specimen_no=specimen_no,
            item_code=item_code,
            message_control_id=message_control_id,
            detail=outcome.detail,
        )
        return
    persist_citations(store, event_id, card_id, outcome.citations)
