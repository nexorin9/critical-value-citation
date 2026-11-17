"""依据来源与降级标注（任务 id 21）。

处置卡的诚实性落在两件事上：

1. **缺项就说缺项** —— 四要素任一取不到依据，标 ``missing`` 并写明
   具体原因（未命中阈值表行 / 未召回制度条款 / 排班系统不可用），
   **不用占位文案冒充已解决**。
2. **降级要说降级** —— 依据有值但来源是降级来的（当班名单来自缓存、
   阈值行来自历史版本、制度召回走了降级层级），标 ``degraded``，
   并把来源、时间戳、层级号写到卡片上，让值班人知道这条依据弱在哪。

本模块只放**常量与构造助手**：来源枚举、原因码、以及把来源翻译成
一句人话（卡片头部标注）的函数。判定与装配在 ``builder.py``。
"""

from __future__ import annotations

from critval.models import Citation, Provenance
from critval.retrieve.levels import FALLBACK_LEVELS

__all__ = [
    "ORIGIN_ACTIVE",
    "ORIGIN_CACHE",
    "ORIGIN_FALLBACK",
    "ORIGIN_HISTORICAL",
    "ORIGIN_LIVE",
    "ORIGIN_NO_DUTY",
    "ORIGIN_UNAVAILABLE",
    "REASON_CONFLICT_PENDING",
    "REASON_POLICY_FIELD_EMPTY",
    "REASON_POLICY_NOT_RECALLED",
    "REASON_RESOLVED",
    "REASON_RETRIEVAL_FALLBACK",
    "REASON_ROSTER_FALLBACK",
    "REASON_ROSTER_FROM_CACHE",
    "REASON_ROSTER_NO_DUTY_PERSON",
    "REASON_ROSTER_UNAVAILABLE",
    "REASON_ITEM_CODE_FALLBACK",
    "REASON_THRESHOLD_HISTORICAL",
    "REASON_THRESHOLD_ROW_NOT_MATCHED",
    "RETRIEVAL_LEVELS",
    "SOURCE_DUTY_ROSTER",
    "SOURCE_POLICY_RETRIEVAL",
    "SOURCE_THRESHOLD_TABLE",
    "describe_provenance",
    "retrieval_provenance",
    "roster_provenance",
    "threshold_provenance",
]

# —— 来源环节 ——
SOURCE_DUTY_ROSTER = "duty_roster"
SOURCE_THRESHOLD_TABLE = "threshold_table"
SOURCE_POLICY_RETRIEVAL = "policy_retrieval"

# —— 来源性质 ——
ORIGIN_LIVE = "live"          # 实时取到
ORIGIN_ACTIVE = "active"      # 当期生效（阈值行按当前版本取到）
ORIGIN_CACHE = "cache"        # 带时间戳的缓存
ORIGIN_HISTORICAL = "historical"  # 历史版本
ORIGIN_FALLBACK = "fallback"  # 兜底（科室值班长顶班等）
ORIGIN_NO_DUTY = "no_duty"    # 名单可用，但本班次窗口内无人在班
ORIGIN_UNAVAILABLE = "unavailable"  # 上游不可用

#: 非降级来源：这些来源不需要在卡片上额外标注。
CLEAN_ORIGINS = frozenset({ORIGIN_LIVE, ORIGIN_ACTIVE})

# —— 要素原因码（可被机器解析）——
REASON_RESOLVED = "resolved"
REASON_THRESHOLD_ROW_NOT_MATCHED = "threshold_row_not_matched"
REASON_POLICY_NOT_RECALLED = "policy_clause_not_recalled"
REASON_POLICY_FIELD_EMPTY = "policy_field_empty"
REASON_ROSTER_UNAVAILABLE = "roster_unavailable"
REASON_ROSTER_FROM_CACHE = "roster_from_cache"
REASON_ROSTER_FALLBACK = "roster_fallback"
#: 名单读到了，但**本班次窗口内一个在班的人都没有**，且名单里也没有
#: 科室值班长可兜底（任务 id 41）。这与「排班系统不可用」是两回事 ——
#: 上游好好的，只是此刻没人当班。
REASON_ROSTER_NO_DUTY_PERSON = "roster_no_duty_person"
REASON_THRESHOLD_HISTORICAL = "threshold_row_historical"
REASON_RETRIEVAL_FALLBACK = "retrieval_fallback_level"
REASON_ITEM_CODE_FALLBACK = "item_code_fallback"
#: 两路召回口径冲突（任务 id 33）：依据有值，但两方说法不一致，
#: **未裁决前不算「已解决」**。
REASON_CONFLICT_PENDING = "conflict_pending_adjudication"

#: 原因码 → 人话（渲染与日志共用一份，措辞不两处各写）。
REASON_TEXT: dict[str, str] = {
    REASON_RESOLVED: "依据齐备",
    REASON_THRESHOLD_ROW_NOT_MATCHED: "阈值表未命中该项目的危急值行",
    REASON_POLICY_NOT_RECALLED: "未召回制度条款",
    REASON_POLICY_FIELD_EMPTY: "制度条款中未写明该要素",
    REASON_ROSTER_UNAVAILABLE: "排班系统不可用，未取到当班人",
    REASON_ROSTER_FROM_CACHE: "当班名单来自缓存",
    REASON_ROSTER_FALLBACK: "当班名单由兜底人选提供",
    REASON_ROSTER_NO_DUTY_PERSON: "本班次窗口内无人在班，且名单中无科室值班长可兜底",
    REASON_THRESHOLD_HISTORICAL: "阈值行来自历史版本",
    REASON_ITEM_CODE_FALLBACK: "项目码来自兜底匹配，需人工核对",
    REASON_RETRIEVAL_FALLBACK: "制度召回走了降级层级",
    REASON_CONFLICT_PENDING: "两路召回口径不一致，待人工裁决",
}

#: 召回降级层级（**真源在召回层** ``critval.retrieve.levels``，此处只做转出，
#: 避免两处各写一套层级号 —— 卡片写"第 2 级"、召回写"level=2"必须对得上）。
RETRIEVAL_LEVELS: dict[int, str] = dict(FALLBACK_LEVELS)


def roster_provenance(
    origin: str = ORIGIN_LIVE,
    detail: str = "",
    as_of: str = "",
) -> Provenance:
    """当班名单的来源标注。

    ``origin=cache`` 时 ``as_of`` 必须带缓存时间戳 —— 卡片上要写明
    「当班名单来源：缓存（时间戳）」，值班人才知道这份名单有多旧。
    """
    return Provenance(
        source=SOURCE_DUTY_ROSTER,
        origin=origin,
        detail=detail,
        as_of=as_of,
    )


def threshold_provenance(
    origin: str = ORIGIN_ACTIVE,
    detail: str = "",
    as_of: str = "",
    citation: Citation | None = None,
) -> Provenance:
    """阈值行的来源标注（历史版本判定时 origin=historical）。"""
    return Provenance(
        source=SOURCE_THRESHOLD_TABLE,
        origin=origin,
        detail=detail,
        as_of=as_of,
        citation=citation,
    )


def retrieval_provenance(level: int = 0, detail: str = "") -> Provenance:
    """制度召回的层级标注（level>0 即为降级召回）。"""
    return Provenance(
        source=SOURCE_POLICY_RETRIEVAL,
        origin=ORIGIN_LIVE if level <= 0 else ORIGIN_FALLBACK,
        detail=detail or RETRIEVAL_LEVELS.get(level, f"第 {level} 级降级"),
        as_of=f"L{level}",
        level=level,
    )


def describe_provenance(provenance: Provenance) -> str:
    """把一条来源标注翻译成一句人话（卡片头部用）。"""
    if provenance.source == SOURCE_DUTY_ROSTER:
        if provenance.origin == ORIGIN_CACHE:
            stamp = f"（{provenance.as_of}）" if provenance.as_of else ""
            detail = f" · {provenance.detail}" if provenance.detail else ""
            return f"当班名单来源：缓存{detail}{stamp}"
        if provenance.origin == ORIGIN_FALLBACK:
            return f"当班名单来源：兜底人选（{provenance.detail or '科室值班长'}）"
        if provenance.origin == ORIGIN_NO_DUTY:
            return f"当班名单来源：排班系统实时（{provenance.detail or '本班次窗口内无人在班'}）"
        if provenance.origin == ORIGIN_UNAVAILABLE:
            return "当班名单来源：排班系统不可用"
        return f"当班名单来源：排班系统实时（{provenance.detail or '当班人'}）"
    if provenance.source == SOURCE_THRESHOLD_TABLE:
        if provenance.origin == ORIGIN_HISTORICAL:
            return f"阈值行来源：历史版本（{provenance.detail or provenance.as_of}）"
        return f"阈值行来源：当期生效版本（{provenance.detail or provenance.as_of}）"
    if provenance.source == SOURCE_POLICY_RETRIEVAL:
        level = provenance.level
        if level <= 0:
            return f"制度召回：原样查询（{provenance.detail or '精确匹配'}）"
        name = RETRIEVAL_LEVELS.get(level, f"第 {level} 级降级")
        return f"制度召回：第 {level} 级降级（{name}）"
    return f"{provenance.source}：{provenance.origin}"
