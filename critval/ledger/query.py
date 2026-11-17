"""台账多维查询（次日复核的真实需求 · 任务 id 37）。

复核不是「把台账全导出来自己筛」。检验科组长要的是三样东西：

1. **按维度切开的数** —— 哪个科室、哪个项目、哪条依据行，各有多少条；
2. **异常计数** —— 超时未确认 / 待回写 / 失败；
3. **能带走的表** —— 明细与汇总，Excel 直开（见 ``critval/ledger/report.py``）。

本模块只管第 1 件事：把「筛选条件」翻译成一条**参数化**的 SELECT。

**为什么全参数化**：院内环境里，拼接出来的 SQL 既是注入面，也是「口径
悄悄变了」的来源 —— 有人把科室码写成 ``LAB' OR '1'='1``，报表里就凭空
多出别的科室记录，而这一行代码看上去跟别的查询没区别。这里所有条件值
都进参数位，条件值里的引号与分号**只是值**，不是语法；SQL 模板固定，
能拼的只有固定的几句。

**两个时间口径**（容易混，分开说）：

* ``day_from`` / ``day_to`` —— 按**观察日期**（``YYYY-MM-DD``）切，
  比的是日期前缀，跨时区写法也不受影响；日报周报用它。
* ``since`` / ``until`` —— 按**精确时刻**（ISO 文本）切，含两端；
  要卡到「某天 08:00 之后」用它。

**超期的口径**：``due_at`` 非空且已过期即算超期（**不论是否已确认**）；
「超期且仍未确认」是报表里的异常计数（``report.py``）叠加的另一个口径，
两处别混。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from critval.judge.critical import DECISION_CRITICAL
from critval.ledger.states import ALL_STATUSES

__all__ = [
    "DISTINCT_COLUMNS",
    "LedgerQuery",
    "count_events",
    "distinct_values",
    "query_events",
]


#: 允许取去重值的列（**白名单** —— 列名不能来自调用方的自由文本）。
DISTINCT_COLUMNS: frozenset[str] = frozenset({"dept_code", "item_code", "status"})


@dataclass(frozen=True)
class LedgerQuery:
    """多维查询条件。

    每个字段空 = 该维度**不限**。三个布尔字段用 ``None`` 区分「不限」与
    「明确要 False」：``overdue=None`` 不筛，``overdue=False`` 只看没超期的。
    """

    dept_code: str = ""
    item_code: str = ""
    status: str = ""
    #: 多状态筛选（``statuses``）—— 与 ``status`` 二选一，``statuses`` 优先。
    #:
    #: 为什么需要它：值班台概览条上的「待确认」一次要覆盖两个状态
    #: （``notified`` 已推送待确认 / ``pending_ack`` 到期未确认），
    #: 「失败待处理」一次要覆盖两个（``failed`` 推送失败 /
    #: ``writeback_failed`` 回写失败）。用单值 ``status`` 表达不了，
    #: 而拆成两次查询再相加，会让「筛选条上的状态」与「概览条上的数」
    #: 变成两套口径 —— 所以这里给查询层补一个多值维度，而不是在页面上凑数。
    statuses: tuple[str, ...] = ()
    #: 依据条款（阈值依据行 / 制度条款定位串），**子串匹配**。
    basis: str = ""
    day_from: str = ""
    day_to: str = ""
    since: str = ""
    until: str = ""
    overdue: Optional[bool] = None
    written_back: Optional[bool] = None
    critical_only: bool = False
    limit: int = 0
    #: 偏移量（分页用，任务 id 49 的值班台台账页）。
    #:
    #: ``limit=0`` 表示不限条数 —— 此时 ``offset`` 忽略（没有 LIMIT 就没有
    #: OFFSET 的意义）。分页的**总条数**走 :func:`count_events`（同一套条件，
    #: 不受 limit 影响），页码与总页数由调用方算 —— 查询层只负责「这一段」。
    offset: int = 0
    sort: str = "observed_at"

    def validated(self) -> "LedgerQuery":
        """校验条件；状态名不认识就直接报错（不静默返回空集）。"""
        if self.status and self.status not in ALL_STATUSES:
            raise ValueError(f"未知状态 {self.status!r} —— 状态取值见 ledger/states.py")
        for one in self.statuses:
            if one not in ALL_STATUSES:
                raise ValueError(f"未知状态 {one!r} —— 状态取值见 ledger/states.py")
        if self.sort not in SORT_COLUMNS:
            raise ValueError(f"未知排序字段 {self.sort!r}")
        return self


#: 排序字段白名单（列名走映射，不直接进 SQL）。
SORT_COLUMNS: dict[str, str] = {
    "observed_at": "e.observed_at",
    "received_at": "e.received_at",
    "status": "e.status",
    "dept_code": "e.dept_code",
    "item_code": "e.item_code",
}


def _like_pattern(text: str) -> str:
    """把用户输入变成 LIKE 的**值**：转义 ``%`` / ``_`` / ``\\``。

    不转义的话，用户输入 ``%`` 就等于「匹配全部」—— 口径悄悄变宽，
    报表里多出来的行没人知道从哪来。
    """
    escaped = (
        str(text or "")
        .replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )
    return f"%{escaped}%"


def _now_iso(now: Optional[datetime]) -> str:
    moment = now or datetime.now(timezone.utc)
    return moment.isoformat()


def _build_where(
    query: LedgerQuery, now: Optional[datetime]
) -> tuple[str, list[Any]]:
    """拼 WHERE 子句与参数表。

    **这里拼的是模板，不是值** —— 条件值一律 ``params.append`` 进去。
    """
    clauses: list[str] = ["1 = 1"]
    params: list[Any] = []

    if query.dept_code:
        clauses.append("e.dept_code = ?")
        params.append(query.dept_code)
    if query.item_code:
        clauses.append("e.item_code = ?")
        params.append(query.item_code)
    if query.statuses:
        # 多状态：一次 IN 查完，不拆成两次查询再相加（口径只有一处）。
        placeholders = ", ".join("?" for _ in query.statuses)
        clauses.append(f"e.status IN ({placeholders})")
        params.extend(str(one) for one in query.statuses)
    elif query.status:
        clauses.append("e.status = ?")
        params.append(query.status)
    if query.basis:
        pattern = _like_pattern(query.basis)
        clauses.append(
            "(e.threshold_row LIKE ? ESCAPE '\\'"
            " OR EXISTS (SELECT 1 FROM citations c"
            "            WHERE c.event_id = e.event_id"
            "              AND c.locator LIKE ? ESCAPE '\\'))"
        )
        params.append(pattern)
        params.append(pattern)
    if query.day_from:
        clauses.append("substr(e.observed_at, 1, 10) >= ?")
        params.append(query.day_from)
    if query.day_to:
        clauses.append("substr(e.observed_at, 1, 10) <= ?")
        params.append(query.day_to)
    if query.since:
        clauses.append("e.observed_at >= ?")
        params.append(query.since)
    if query.until:
        clauses.append("e.observed_at <= ?")
        params.append(query.until)
    if query.overdue is True:
        clauses.append("e.due_at != '' AND e.due_at < ?")
        params.append(_now_iso(now))
    elif query.overdue is False:
        clauses.append("(e.due_at = '' OR e.due_at >= ?)")
        params.append(_now_iso(now))
    if query.written_back is True:
        clauses.append("e.registered_at != ''")
    elif query.written_back is False:
        clauses.append("e.registered_at = ''")
    if query.critical_only:
        clauses.append("e.judgement_decision = ?")
        params.append(DECISION_CRITICAL)

    return " AND ".join(clauses), params


def query_events(
    store: Any,
    query: Optional[LedgerQuery] = None,
    *,
    now: Optional[datetime] = None,
) -> list[dict[str, Any]]:
    """按多维条件查台账事件（默认按观察时刻倒序）。

    返回的是**事件行本身**（``SELECT e.*``），不做二次加工 ——
    复核要看到的就是台账里那一行，不加工才不会「导出来的和库里不一样」。
    """
    cond = (query or LedgerQuery()).validated()
    where, params = _build_where(cond, now)
    sql = (
        "SELECT e.* FROM events e WHERE "
        + where
        + f" ORDER BY {SORT_COLUMNS[cond.sort]} DESC, e.event_id"
    )
    if cond.limit and cond.limit > 0:
        sql += " LIMIT ?"
        params.append(int(cond.limit))
        if cond.offset and cond.offset > 0:
            sql += " OFFSET ?"
            params.append(int(cond.offset))
    return store.query_rows(sql, params)


def count_events(
    store: Any,
    query: Optional[LedgerQuery] = None,
    *,
    now: Optional[datetime] = None,
) -> int:
    """同一套条件下的总条数（不受 ``limit`` 影响）。"""
    cond = (query or LedgerQuery()).validated()
    where, params = _build_where(cond, now)
    sql = "SELECT COUNT(*) AS n FROM events e WHERE " + where
    rows = store.query_rows(sql, params)
    return int(rows[0]["n"]) if rows else 0


def distinct_values(store: Any, column: str) -> list[str]:
    """取某列的去重值（给筛选下拉用）。

    **列名走白名单** —— 不接受调用方自由文本拼进 SQL。
    """
    if column not in DISTINCT_COLUMNS:
        raise ValueError(f"不允许按 {column!r} 取去重值（白名单：{sorted(DISTINCT_COLUMNS)}）")
    sql = (
        f"SELECT DISTINCT {column} AS value FROM events "
        f"WHERE {column} != '' ORDER BY {column}"
    )
    return [str(row.get("value", "")) for row in store.query_rows(sql, [])]
