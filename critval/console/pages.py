"""值班台页面：事件台账页 + 处置卡确认页（任务 id 13 / 48 / 49）。

**这一层现在提供什么**

三块页面的服务端渲染 —— 值班台概览、事件台账页、处置卡确认页 —— 加上
「值班人点确认」与「转人工」两个动作的处理，以及给院内系统用的 JSON
接口（``/api/events``）。设计 token 与基础组件来自任务 id 48，
加载 / 空 / 错误三态与交互反馈在任务 id 50 补齐。

**但数据来源与业务动作就是真的**

* 台账页读台账（``events`` 表），**筛选与分页走任务 id 37 的同一套查询**
  （``critval.ledger.query.LedgerQuery``）—— 值班台上筛出来的与次日复核
  报表导出的，口径是同一个口径；
* 确认页读**落库的处置卡快照**（``CardSnapshotStore``）—— **不重新渲染**：
  阈值表改版了、制度改版了，重渲染出来的就不是当时推给值班人的那份了；
  值班人复核时看到的就是快照里的原文；
* 确认走真实的业务状态跃迁（``notified → acknowledged``）与**幂等回写**
  登记系统 —— 这是整条链上唯一一处能把「建议」变成「已生效事实」的闸门；
* 转人工走真实状态跃迁（``→ transferred_to_human``），**原因必须填**，
  原因进审计（append-only）。

**边界**：页面里不出现患者姓名 / 身份证 / 住院号 —— 处置卡正文本身已被
白名单闸门过滤过（``critval/card/renderer.py``），本模块只做展示，
不再引入新的患者字段。JSON 接口另有一层字段白名单
（``critval/console/api.py``）。

**访问控制**：配了 ``CONSOLE_TOKEN`` 就必须带凭据（``Authorization:
Bearer`` 头或 ``?token=`` 查询参数 —— 确认链接要能从聊天工具里直接点开，
所以两种都收）；没配则本机可直接访问，与入站端点的口径一致。
"""

from __future__ import annotations

import html
import hmac
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence
from urllib.parse import parse_qs, unquote, urlencode, urlsplit

from critval.card.renderer import CARD_ELEMENT_KEYS, STATUS_LABELS
from critval.console.api import (
    API_PATH,
    API_PATH_PREFIX,
    build_event_detail,
    build_event_list,
    json_response,
)
from critval.console.components import (
    button,
    citation_chip,
    empty_state,
    icon,
    input_field,
    load_base_template,
    notice,
    render_template,
    select_field,
    statbar,
    status_badge,
    table,
)
from critval.console.states_view import (
    empty_state_view,
    error_state,
    feedback_for_status,
    feedback_toast,
    loading_state,
)
from critval.ledger.query import (
    LedgerQuery,
    SORT_COLUMNS,
    count_events,
    distinct_values,
    query_events,
)
from critval.ledger.states import ALL_STATUSES, IllegalTransitionError, describe_status

__all__ = [
    "AckResult",
    "ConsoleRouter",
    "PAGE_SIZE",
    "PageResponse",
    "ReplayResult",
    "escape",
    "render_ack_page",
    "render_event_detail",
    "render_event_list",
    "render_home",
    "render_layout",
]

logger = logging.getLogger("critval.console.pages")

#: 台账列表页路径。
EVENTS_PATH = "/events"

#: 首页路径（值班台概览）。
HOME_PATH = "/"

#: 台账页每页条数（分页大小固定 —— 值班大屏一屏放得下，太多反而看不清）。
PAGE_SIZE = 20

#: 处置卡四要素的**语义角色**（resolved / degraded / missing → 语义色角色）。
#:
#: 「依据齐备」走绿（事实已定）、「降级依据」走琥珀（等了但要盯着）、
#: 「依据缺失」走红（人得马上动）—— 与状态机的用色纪律一致。
_ELEMENT_SEMANTIC: dict[str, str] = {
    "resolved": "done",
    "degraded": "pending",
    "missing": "critical",
}


def escape(value: Any) -> str:
    """HTML 转义 —— 页面里出现的任何外来文本都要过这一道。"""
    return html.escape("" if value is None else str(value), quote=True)


def _fmt_moment(text: Any) -> str:
    """把 ISO 时刻格式化成 ``YYYY-MM-DD HH:mm``（页面上统一一个格式）。"""
    raw = str(text or "").strip()
    if not raw:
        return "—"
    try:
        moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return raw
    return moment.strftime("%Y-%m-%d %H:%M")


def _status_label(status: Any) -> str:
    """状态的中文说法（页面上给人看的，不是状态机枚举）。

    **直接取状态机自己的标签表**（``critval.ledger.states``）—— 值班台上
    写「已接收」、审计里写「已接收」、机器状态写 ``received``，三处必须是
    同一件事，不能各说一套。
    """
    key = str(status or "").strip()
    if not key:
        return "—"
    return describe_status(key)


# ---------------------------------------------------------------------------
# 页面骨架
# ---------------------------------------------------------------------------

#: 静态样式的访问路径（由值班台路由直接吐出去）。
STYLESHEET_URL = "/static/styles.css"

#: 静态脚本的访问路径（任务 id 50：toast 自动消失 / 二次确认模态 / 折叠导航）。
SCRIPT_URL = "/static/console.js"

#: 静态资源前缀。
STATIC_PREFIX = "/static/"

#: 静态资源白名单（只放行这些文件名，其余一律 404）。
_STATIC_FILES: frozenset[str] = frozenset({"styles.css", "console.js"})

#: 静态资源的 Content-Type（**按扩展名给** —— CSS 当 HTML 吐会让样式失效，
#: JS 当 CSS 吐会让脚本压根不执行）。
_STATIC_CONTENT_TYPES: dict[str, str] = {
    "styles.css": "text/css; charset=utf-8",
    "console.js": "application/javascript; charset=utf-8",
}

#: 侧边导航项 —— 只放**真实存在**的路由，不摆点不动的空入口。
_NAV_ITEMS: tuple[tuple[str, str, str, str], ...] = (
    # (key, 标签, 路径, 图标)
    ("home", "值班台概览", HOME_PATH, "activity"),
    ("events", "事件台账", EVENTS_PATH, "list"),
    ("pending", "待确认", f"{EVENTS_PATH}?status=pending_ack", "clock"),
)


def render_nav(active: str = "events", counts: Optional[Mapping[str, Any]] = None) -> str:
    """侧边导航 —— 活动项高亮，可带待办计数。"""
    stats = dict(counts or {})
    items = []
    for key, label, path, icon_name in _NAV_ITEMS:
        active_class = " is-active" if key == active else ""
        count = stats.get(key)
        count_html = (
            f'<span class="cv-sidenav__count">{escape(count)}</span>' if count else ""
        )
        items.append(
            f'<a class="cv-sidenav__item{active_class}" href="{escape(path)}">'
            f"{icon(icon_name)}{escape(label)}{count_html}</a>"
        )
    return "".join(items)


def render_layout(
    title: str,
    body: str,
    *,
    active: str = "events",
    heading: str = "",
    subtitle: str = "",
    actions: str = "",
    footer: str = "",
    nav_counts: Optional[Mapping[str, Any]] = None,
    topbar_meta: str = "",
    toast_html: str = "",
) -> str:
    """值班台页面骨架 —— 顶栏 + 侧边导航 + 主内容区 + 页脚 + toast 容器。

    外壳走 ``templates/base.html``，样式走 ``templates/styles.css``
    （``/static/styles.css``）—— **页面里不再内联 ``<style>``**，设计 token
    只有一处真源（``critval/console/tokens.py``）。

    ``body`` 里若已自带页头（``<header class="page">``），调用方把
    ``heading`` / ``subtitle`` 留空即可 —— 两处页头不会重复出现。

    ``toast_html`` 是写操作反馈（任务 id 50）：它落在 toast 容器里，
    **不是**页面顶部的提示条 —— 提示条是给人停下来读的，toast 是「刚才
    那一下」的回执，看完就该走。
    """
    template = load_base_template()
    if not template:
        # 模板文件缺失时也要能出页面（不能因为一个静态文件就让值班台打不开）。
        template = (
            "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
            "<title>{{title}}</title>"
            f'<link rel="stylesheet" href="{STYLESHEET_URL}"></head><body>'
            "<div class=\"cv-app\"><main class=\"cv-main\">{{content}}</main></div>"
            "</body></html>"
        )
    footer_html = footer or _default_footer()
    return render_template(
        template,
        {
            "title": escape(f"{title} · 危急值值班台"),
            "stylesheet": STYLESHEET_URL,
            "script": SCRIPT_URL,
            "brand_icon": icon("hospital", class_name="cv-icon"),
            "topbar_meta": topbar_meta,
            "nav": render_nav(active, nav_counts),
            "heading": escape(heading),
            "subtitle": escape(subtitle),
            "actions": actions,
            "content": body,
            "footer": footer_html,
            "toast_host": toast_html,
        },
    )


def _default_footer() -> str:
    """页脚 —— 规则快照 ID 与配置版本号（复核时要看的那两个号）。"""
    return (
        "<span>危急值处置依据检索系统</span>"
        "<span>值班台只读回执入口：确认前均为建议</span>"
    )


#: 首页待办清单：**状态 → 这件事现在该谁做什么**。
#:
#: 值班人看首页不是看状态机枚举，是看「我现在该干什么」—— 所以每行都要
#: 有一句人话。
_HOME_TODOS: tuple[tuple[str, str], ...] = (
    ("pending_ack", "已推送，待值班人确认"),
    ("notified", "已推送，待值班人确认"),
    ("acknowledged", "已确认，待回写登记系统"),
    ("writeback_failed", "回写失败，待人工补写"),
    ("transferred_to_human", "依据未命中，待人工接管"),
    ("failed", "推送失败，待人工处理"),
    ("received", "刚收下，待判定"),
    ("judged", "已判定，待出卡"),
    ("card_built", "已出卡，待推送"),
)


def render_home(store: Any, *, counts: Optional[Mapping[str, int]] = None) -> str:
    """首页：值班台概览（待办计数 + 入口）。

    页面用组件库（``critval/console/components.py``）拼装：统计条 + 待办表
    （状态徽标带文字，颜色由语义 token 决定）+ 下一步入口。
    """
    stats = dict(counts or {})
    if not stats:
        try:
            stats = dict(store.status_counts())
        except Exception:  # noqa: BLE001 —— 首页不该因为体检失败而打不开
            stats = {}

    rows = []
    for key, action in _HOME_TODOS:
        count = int(stats.get(key, 0) or 0)
        if count:
            rows.append({"status": key, "action": action, "count": count})

    waiting = sum(int(stats.get(key, 0) or 0) for key in ("notified", "pending_ack"))
    broken = sum(int(stats.get(key, 0) or 0) for key in ("failed", "writeback_failed"))
    human = int(stats.get("transferred_to_human", 0) or 0)

    columns = [
        {"key": "action", "label": "事项"},
        {
            "key": "status",
            "label": "状态",
            "cell": lambda row: status_badge(row["status"]),
        },
        {"key": "count", "label": "数量", "align": "right"},
    ]

    body = [
        statbar(
            [
                {"label": "待确认", "value": waiting, "role": "critical"},
                {"label": "转人工", "value": human, "role": "pending"},
                {"label": "失败待处理", "value": broken, "role": "failed"},
                {"label": "待办合计", "value": sum(int(r["count"]) for r in rows)},
            ]
        ),
        '<div class="cv-card"><h2 class="cv-card__title">当前待办</h2>',
        table(
            columns,
            rows,
            empty_title="当前没有待处置的危急值事件",
            empty_desc="等待 LIS 推送或 MLLP 监听收单",
            empty_action=button("查看事件台账", variant="secondary", href="/events"),
        ),
        "</div>",
        '<div class="cv-card"><h2 class="cv-card__title">下一步</h2>',
        '<p class="cv-muted">打开 <a href="/events">事件台账</a> 看待处置的危急值，'
        "点进详情看依据出处与上报时限。</p>",
        "</div>",
    ]

    return render_layout(
        "值班台概览",
        "".join(body),
        active="home",
        heading="危急值值班台",
        subtitle="入站推送 → 判定 → 带出处的处置卡 → 值班人确认 → 回写登记系统",
        nav_counts={"pending": waiting},
    )


def _query_string(filters: Mapping[str, Any], **overrides: Any) -> str:
    """把筛选条件拼成查询串（空值不带上去，页面上的链接就不会挂一串空参数）。

    **布尔值走 ``1`` / ``0``**：``overdue=False``（只看未超期）如果直接交给
    ``urlencode`` 会变成 ``overdue=False``，而筛选解析只认 ``1`` / ``0`` ——
    翻页链接就会把「未超期」这个条件悄悄丢掉，值班人以为还在筛，其实已经
    不是那一屏了。所以布尔在这里统一落成 ``1`` / ``0``。
    """
    merged = {key: value for key, value in dict(filters or {}).items()}
    # ``statuses`` 是 ``status`` 的内部形态（多值元组），不进 URL ——
    # URL 里只有一个 ``status`` 参数，值是逗号分隔的状态名。
    merged.pop("statuses", None)
    for key, value in overrides.items():
        if value in (None, ""):
            merged.pop(key, None)
        else:
            merged[key] = value
    clean: dict[str, Any] = {}
    for key, value in merged.items():
        if value is None or value == "":
            continue
        if isinstance(value, bool):
            clean[key] = "1" if value else "0"
            continue
        clean[key] = value
    return ("?" + urlencode(clean)) if clean else ""


def _tri_state_options(current: Optional[bool]) -> list[tuple[str, str]]:
    """三态下拉（不限 / 是 / 否）—— 超期与回写两个筛选都用它。"""
    options = [("", "不限")]
    options.append(("1", "是"))
    options.append(("0", "否"))
    return options


# ---------------------------------------------------------------------------
# 台账页概览条（五个「这事现在归谁管」的桶）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Bucket:
    """概览条上的一格。

    一格 = 一个**待办类别**：现在有几件要人动手、几件已经坏了、几件等人接。
    状态集合是「这一格管哪几个状态」，``overdue`` 是这一格额外覆盖的口径。
    """

    key: str
    label: str
    role: str
    hint: str
    statuses: tuple[str, ...] = ()
    overdue: Optional[bool] = None


#: 概览条的五格。
#:
#: **五格共用同一个底口径**：当前这一屏的筛选里去掉「状态」这一维，其余
#: （科室 / 项目 / 是否超期 / 是否已回写）沿用。这样值班人看到的五个数是
#: **同一批数据切出来的五个类别**，而不是五个各查各的数 —— 五个数加起来
#: 大于总数是正常的（一条事件可能同时「已超期」又「待回写」），但每个数
#: 都来自同一批。
_LEDGER_BUCKETS: tuple[_Bucket, ...] = (
    _Bucket(
        "pending_confirm",
        "待确认",
        "critical",
        "已推送待确认 + 到期仍未确认",
        statuses=("notified", "pending_ack"),
    ),
    _Bucket(
        "overdue",
        "已超期",
        "critical",
        "上报时限已过（不论是否已确认）",
        overdue=True,
    ),
    _Bucket(
        "pending_writeback",
        "待回写",
        "pending",
        "已确认，待回写登记系统",
        statuses=("acknowledged",),
    ),
    _Bucket(
        "failed",
        "失败待处理",
        "failed",
        "推送失败或回写失败",
        statuses=("failed", "writeback_failed"),
    ),
    _Bucket(
        "transferred",
        "转人工",
        "pending",
        "依据未命中或口径分歧，等人工接管",
        statuses=("transferred_to_human",),
    ),
)

#: 状态多值选项（概览条钻取用）—— 状态筛选下拉里也放这两项，
#: 这样从概览条点进去之后，下拉里能看到当前选的是哪一档。
_BUCKET_STATUS_CHOICES: tuple[tuple[str, str], ...] = (
    ("notified,pending_ack", "待确认（待我处理）"),
    ("failed,writeback_failed", "失败待处理（推送或回写失败）"),
)


def _status_filter_label(raw: str) -> str:
    """状态筛选条件的中文说法（多值用「、」连起来）。"""
    parts = [describe_status(part) for part in str(raw or "").split(",") if part]
    return "、".join(parts) if parts else ""


def _bucket_link(filters: Mapping[str, Any], bucket: _Bucket) -> str:
    """概览条上某一格的钻取链接。

    **链接替换掉「状态」这一维**（桶自己就是按状态分的类），其余筛选条件
    原样带着走 —— 点进去看到的仍是值班人当前筛的那批科室 / 项目。
    """
    merged = {key: value for key, value in dict(filters or {}).items()}
    merged.pop("status", None)
    merged.pop("page", None)
    if bucket.statuses:
        merged["status"] = ",".join(bucket.statuses)
    if bucket.overdue is not None:
        merged["overdue"] = "1" if bucket.overdue else "0"
    return "/events" + _query_string(merged)


def _ledger_buckets(store: Any, filters: Mapping[str, Any]) -> list[dict[str, Any]]:
    """概览条的五格计数（**与任务 id 37 的查询同一套口径**）。"""
    base = {
        "dept": str(filters.get("dept", "") or ""),
        "item": str(filters.get("item", "") or ""),
        "overdue": filters.get("overdue", None),
        "written_back": filters.get("written_back", None),
    }
    buckets: list[dict[str, Any]] = []
    for bucket in _LEDGER_BUCKETS:
        scope = dict(base)
        if bucket.overdue is not None:
            scope["overdue"] = bucket.overdue
        query = LedgerQuery(
            dept_code=scope["dept"],
            item_code=scope["item"],
            statuses=tuple(bucket.statuses),
            overdue=scope["overdue"],
            written_back=scope["written_back"],
        )
        buckets.append(
            {
                "key": bucket.key,
                "label": bucket.label,
                "role": bucket.role,
                "hint": bucket.hint,
                "value": count_events(store, query),
                "href": _bucket_link(filters, bucket),
            }
        )
    return buckets


def _filter_form(
    filters: Mapping[str, Any],
    *,
    dept_options: Sequence[str] = (),
    item_options: Sequence[str] = (),
) -> str:
    """筛选表单（GET）—— 科室 / 项目 / 状态 / 是否超期 / 是否已回写。

    表单走 ``GET`` 而不是 ``POST``：值班人筛选出来的这一屏，**网址本身就是
    这次筛选**—— 复制给同事、截图给科主任，对方看到的是同一批数据。
    """
    status_options: list[tuple[str, str]] = [("", "全部状态")]
    # 概览条上的两类多值桶也放进下拉 —— 从概览条钻取进来后，下拉里能看出
    # 当前选的是哪一档，而不是回到「全部状态」。
    status_options.extend(_BUCKET_STATUS_CHOICES)
    for key in sorted(ALL_STATUSES):
        status_options.append((key, describe_status(key)))

    dept_select = select_field(
        "dept",
        [("", "全部科室")] + [(str(value), str(value)) for value in dept_options],
        label="科室",
        value=str(filters.get("dept", "") or ""),
    )
    item_select = select_field(
        "item",
        [("", "全部项目")] + [(str(value), str(value)) for value in item_options],
        label="检验项目码",
        value=str(filters.get("item", "") or ""),
    )
    status_select = select_field(
        "status",
        status_options,
        label="状态",
        value=str(filters.get("status", "") or ""),
    )
    overdue_raw = filters.get("overdue", None)
    overdue_select = select_field(
        "overdue",
        _tri_state_options(overdue_raw),
        label="是否超期",
        value=("" if overdue_raw is None else ("1" if overdue_raw else "0")),
    )
    written_raw = filters.get("written_back", None)
    written_select = select_field(
        "written_back",
        _tri_state_options(written_raw),
        label="是否已回写",
        value=("" if written_raw is None else ("1" if written_raw else "0")),
    )

    return (
        '<form class="cv-filterbar" method="get" action="/events">'
        f"{dept_select}{item_select}{status_select}{overdue_select}{written_select}"
        '<div class="cv-filterbar__actions">'
        + button("筛选", variant="primary", button_type="submit", icon_name="refresh")
        + button("重置", variant="ghost", href="/events")
        + "</div></form>"
    )


def _pager(
    *,
    total: int,
    page: int,
    per_page: int,
    filters: Mapping[str, Any],
) -> str:
    """分页条 —— 页码、总页数、上一页 / 下一页。

    **总条数是筛选后的条数**，不是全库条数 —— 值班人看到的「共 12 条」就是
    这一屏筛选结果真的有多少条。
    """
    pages = max(1, (int(total) + int(per_page) - 1) // int(per_page))
    page = max(1, min(int(page), pages))
    prev_link = f"/events{_query_string(filters, page=page - 1)}" if page > 1 else ""
    next_link = (
        f"/events{_query_string(filters, page=page + 1)}" if page < pages else ""
    )
    prev_html = (
        f'<a class="cv-pager__link" href="{escape(prev_link)}">上一页</a>'
        if prev_link
        else '<span class="cv-pager__link is-disabled">上一页</span>'
    )
    next_html = (
        f'<a class="cv-pager__link" href="{escape(next_link)}">下一页</a>'
        if next_link
        else '<span class="cv-pager__link is-disabled">下一页</span>'
    )
    return (
        '<nav class="cv-pager" aria-label="台账分页">'
        f'<span class="cv-pager__info">共 {int(total)} 条 · 第 {page} / {pages} 页</span>'
        f"{prev_html}{next_html}</nav>"
    )


def _is_overdue(row: Mapping[str, Any], now_text: str) -> bool:
    """到期时刻已过即算超期（**不论是否已确认**）—— 与复核报表同口径。"""
    due_at = str(row.get("due_at", "") or "")
    return bool(due_at) and due_at < now_text


def _ledger_columns() -> list[dict[str, Any]]:
    """台账页的列定义（**与任务 id 37 的复核报表明细列同口径**）。"""

    def event_cell(row: Mapping[str, Any]) -> str:
        event_id = str(row.get("event_id", "") or "")
        return f'<a href="/events/{escape(event_id)}">{escape(event_id)}</a>'

    def item_cell(row: Mapping[str, Any]) -> str:
        code = str(row.get("item_code", "") or "")
        name = str(row.get("item_name", "") or "")
        return escape(f"{code} {name}".strip())

    def value_cell(row: Mapping[str, Any]) -> str:
        value = str(row.get("value", "") or "")
        unit = str(row.get("unit", "") or "")
        return escape(f"{value} {unit}".strip())

    def observed_cell(row: Mapping[str, Any]) -> str:
        return escape(_fmt_moment(row.get("observed_at")))

    def due_cell(row: Mapping[str, Any]) -> str:
        due_at = str(row.get("due_at", "") or "")
        if not due_at:
            return '<span class="cv-muted">—</span>'
        parts = [f'<span class="mono">{escape(_fmt_moment(due_at))}</span>']
        if _is_overdue(row, _now()):
            parts.append(
                '<span class="cv-badge cv-badge--critical">'
                '<span class="cv-badge__dot"></span>已超期</span>'
            )
        return " ".join(parts)

    def written_cell(row: Mapping[str, Any]) -> str:
        registered_at = str(row.get("registered_at", "") or "")
        if registered_at:
            return (
                '<span class="cv-badge cv-badge--done">'
                '<span class="cv-badge__dot"></span>已回写</span>'
                f'<span class="mono cv-muted">{escape(_fmt_moment(registered_at))}</span>'
            )
        return (
            '<span class="cv-badge cv-badge--pending">'
            '<span class="cv-badge__dot"></span>未回写</span>'
        )

    def status_cell(row: Mapping[str, Any]) -> str:
        return status_badge(str(row.get("status", "") or ""))

    return [
        {"key": "event_id", "label": "事件号", "cell": event_cell},
        {"key": "item_code", "label": "检验项目", "cell": item_cell},
        {"key": "value", "label": "结果", "align": "right", "cell": value_cell},
        {"key": "dept_code", "label": "科室"},
        {"key": "observed_at", "label": "观察时刻", "cell": observed_cell},
        {"key": "due_at", "label": "上报时限", "cell": due_cell},
        {"key": "registered_at", "label": "回写", "cell": written_cell},
        {"key": "status", "label": "状态", "cell": status_cell},
    ]


def render_event_list(
    rows: list[Mapping[str, Any]],
    *,
    filters: Optional[Mapping[str, Any]] = None,
    total: int = 0,
    page: int = 1,
    per_page: int = PAGE_SIZE,
    dept_options: Sequence[str] = (),
    item_options: Sequence[str] = (),
    buckets: Optional[Sequence[Mapping[str, Any]]] = None,
    loading: bool = False,
    threshold_missing: bool = False,
) -> str:
    """事件台账页 —— 概览条 + 筛选表单 + 明细表 + 分页。

    **列与筛选口径与任务 id 37 的复核报表一致**：同一个 ``LedgerQuery``
    出数（科室 / 项目 / 状态 / 是否超期 / 是否已回写），值班台筛出来的
    与次日导出的报表是同一批数据。

    **概览条（``buckets``）是这一屏的第一眼**：五个待办类别的计数，每一格
    都能点进去钻取 —— 值班人先看「有几件要我现在动手」，再决定看哪一批。

    **三态（任务 id 50）**：这一屏可能是「正在取数」（``loading=True``）、
    「一条都没有」（台账全空）或「筛选后为空」（表内空态）—— 三者互斥，
    值班人一眼能分清「还没取到」「真没有」「筛掉了」。
    """
    active = dict(filters or {})
    filter_form = _filter_form(
        active, dept_options=dept_options, item_options=item_options
    )

    applied = []
    if active.get("dept"):
        applied.append(f"科室 {active['dept']}")
    if active.get("item"):
        applied.append(f"项目 {active['item']}")
    if active.get("status"):
        applied.append(f"状态 {_status_filter_label(str(active['status']))}")
    if active.get("overdue") is True:
        applied.append("已超期")
    elif active.get("overdue") is False:
        applied.append("未超期")
    if active.get("written_back") is True:
        applied.append("已回写")
    elif active.get("written_back") is False:
        applied.append("未回写")
    filter_text = f"（筛选：{' · '.join(applied)}）" if applied else ""

    # ---- 台账区域（刷新时整块替换，刷新按钮与脚本只认这一个容器） --------
    region_html = _ledger_region(
        rows,
        active=active,
        total=total,
        page=page,
        per_page=per_page,
        loading=loading,
    )
    region = (
        '<div class="cv-ledger-region" data-cv-ledger-region>'
        f"{region_html}</div>"
    )

    # ---- 阈值表未绑定：错误态（配置错了不该只留一条灰字） ----------------
    threshold_card = ""
    if threshold_missing:
        threshold_card = (
            '<div class="cv-card cv-card--alert">'
            + error_state(
                code="threshold_unbound",
                detail=(
                    "阈值表路径配了但文件读不到 —— 规则集没有绑定版本，"
                    "判定会直接转人工，不会产出危急值事件。"
                ),
                retry_href="/events",
            )
            + "</div>"
        )

    # ---- 加载态骨架（脚本刷新时先摆上，再拿真数据替换） ------------------
    skeleton_html = loading_state(
        title="正在取数",
        desc="正在从台账里取危急值事件，稍候。",
        rows=6,
        fallback_href="/events",
    )
    skeleton_template = (
        f'<template data-cv-ledger-skeleton>{skeleton_html}</template>'
    )

    body = (
        statbar(
            [
                {
                    "label": str(item.get("label", "")),
                    "value": item.get("value", 0),
                    "role": str(item.get("role", "")),
                    "href": str(item.get("href", "") or ""),
                    "title": str(item.get("hint", "") or ""),
                }
                for item in (buckets or [])
            ]
        )
        + filter_form
        + threshold_card
        + f'<div class="cv-card"><h2 class="cv-card__title">处置记录</h2>{region}</div>'
        + skeleton_template
    )
    refresh = (
        '<button class="cv-btn cv-btn--secondary" type="button" data-cv-refresh '
        'data-cv-refresh-title="正在取数" data-cv-refresh-desc="正在从台账里取危急值事件，稍候。">'
        f"{icon('refresh')}<span class=\"cv-btn__label\">刷新台账</span></button>"
    )
    return render_layout(
        "事件台账",
        body,
        heading="事件台账",
        subtitle=(
            f"共 {int(total)} 条{filter_text}"
            "（处置卡与依据出处点进详情看；确认动作在详情页的确认区）"
        ),
        actions=refresh,
        active="events",
    )


def _ledger_region(
    rows: list[Mapping[str, Any]],
    *,
    active: Mapping[str, Any],
    total: int,
    page: int,
    per_page: int,
    loading: bool = False,
) -> str:
    """台账区域内容 —— 明细表（或三态）+ 分页条。

    **空态分两种**：台账全空（一条记录都没有）走**页面级空态**，说清
    「为什么空」与「下一步」；筛完之后为空走表内空态，提示放宽筛选。
    两者混成一句「暂无数据」，值班人就不知道该等还是该查。
    """
    if loading:
        return loading_state(
            title="正在取数",
            desc="正在从台账里取危急值事件，稍候。",
            rows=6,
            fallback_href="/events",
        )

    has_filters = any(
        active.get(key) not in (None, "", ())
        for key in ("dept", "item", "status", "statuses", "overdue", "written_back")
    )
    if int(total) == 0 and not has_filters:
        return (
            '<div class="cv-card cv-card--flat">'
            + empty_state_view(
                title="台账里还没有危急值事件",
                reason=(
                    "守护进程已经在跑，但一条危急值结果都还没进台账 —— "
                    "空的是整张台账，不是筛出来的。"
                ),
                next_step=(
                    "按下面三条对一遍：对上了说明是真的没有危急值；"
                    "对不上就是链路没通，按对应那条去查。"
                ),
                checks=[
                    "LIS 有没有在推：确认 LIS 侧危急值推送已启用"
                    "（入站端点 POST /api/v1/critical-value-events）。",
                    "MLLP 监听有没有在跑：确认守护进程已启动，"
                    "并已配置监听地址与端口（CRITVAL_MLLP_BIND）。",
                    "阈值表版本有没有绑定：未绑定时判定直接转人工，"
                    "不会产出危急值事件（CRITVAL_THRESHOLD_PATH）。",
                ],
                action=button(
                    "打开值班台概览", variant="secondary", href="/"
                ),
            )
            + "</div>"
        )

    listing = table(
        _ledger_columns(),
        rows,
        empty_title="当前筛选条件下没有危急值事件",
        empty_desc="放宽筛选条件，或等待 LIS 推送与 MLLP 监听收单",
        empty_action=button("清空筛选", variant="secondary", href="/events"),
        caption="危急值事件台账",
    )
    return listing + _pager(
        total=total, page=page, per_page=per_page, filters=active
    )


def _citation_rows(citations: list[Mapping[str, Any]]) -> str:
    """引用出处表 —— 每条依据都能点回原文位置。

    走组件库的表格（sticky 表头 / 空态 / 窄屏卡片化），出处列用
    ``citation_chip``：文件名 + 行号，点不出处也认得出是哪一份材料。
    """
    columns = [
        {"key": "kind", "label": "类型"},
        {
            "key": "source_name",
            "label": "出处（文件名 · 行号）",
            "cell": lambda row: citation_chip(
                str(row.get("source_name", "") or row.get("locator", "") or "—"),
                row.get("line_no", ""),
                sha256=str(row.get("source_sha256", "") or ""),
                title=str(row.get("locator", "") or ""),
            ),
        },
        {
            "key": "source_sha256",
            "label": "材料指纹",
            "align": "right",
            "cell": lambda row: (
                f'<span class="mono">{escape((str(row.get("source_sha256", "")) or "")[:12])}</span>'
            ),
        },
        {"key": "section_path", "label": "章节"},
    ]
    return table(
        columns,
        list(citations or []),
        empty_title="本条事件没有落库的引用",
        empty_desc="依据缺失时不编造 —— 没有出处就是没有出处",
        caption="依据出处",
    )


def render_event_detail(
    row: Mapping[str, Any],
    *,
    citations: Optional[list[Mapping[str, Any]]] = None,
    notifications: Optional[list[Mapping[str, Any]]] = None,
    audits: Optional[list[Mapping[str, Any]]] = None,
    card_text: str = "",
    confirm_url: str = "",
) -> str:
    """事件详情页：事实 + 依据出处 + 推送记录 + 审计流水。

    与台账页 / 确认页共用同一套组件（卡片、键值表、表格、状态徽标）——
    **页面上不再有第二套版式**。
    """
    event_id = str(row.get("event_id", ""))
    status = str(row.get("status", ""))
    facts = [
        ("检验项目", f'{row.get("item_code", "")} {row.get("item_name", "")}'.strip()),
        ("观察值", f'{row.get("value", "")} {row.get("unit", "")}'.strip()),
        ("科室", str(row.get("dept_code", "") or "—")),
        ("标本号", str(row.get("specimen_no", "") or "—")),
        ("消息控制号", str(row.get("message_control_id", "") or "—")),
        ("观察时刻", _fmt_moment(row.get("observed_at"))),
        ("入站时刻", _fmt_moment(row.get("received_at"))),
        ("状态", _status_label(status)),
    ]
    kv = "".join(f"<dt>{escape(k)}</dt><dd>{escape(v)}</dd>" for k, v in facts)

    notify_columns = [
        {"key": "kind", "label": "类型"},
        {"key": "channel", "label": "通道"},
        {"key": "attempt", "label": "尝试", "align": "right"},
        {
            "key": "notified_at",
            "label": "时刻",
            "cell": lambda item: f'<span class="mono">{_fmt_moment(item.get("notified_at"))}</span>',
        },
        {"key": "detail", "label": "说明"},
    ]
    notify_table = table(
        notify_columns,
        list(notifications or []),
        empty_title="还没有推送记录",
        empty_desc="本条事件还没有推送给任何值班人",
        caption="推送记录",
    )

    audit_columns = [
        {"key": "audit_no", "label": "#", "align": "right"},
        {
            "key": "at",
            "label": "时刻",
            "cell": lambda item: f'<span class="mono">{escape(item.get("at", ""))}</span>',
        },
        {"key": "action", "label": "动作"},
        {"key": "actor", "label": "操作者"},
        {"key": "detail", "label": "说明"},
    ]
    audit_table = table(
        audit_columns,
        list(audits or []),
        empty_title="暂无审计流水",
        empty_desc="状态跃迁、确认与回写的每一步都在这里",
        caption="审计流水",
    )

    card_block = (
        f'<pre class="cv-cardtext">{escape(card_text)}</pre>'
        if card_text
        else '<p class="cv-muted">本条事件还没有处置卡快照'
        "（不危急或转人工的事件不会出卡）。</p>"
    )

    ack_link = (
        '<p class="cv-card__foot">'
        + button(
            "打开确认页",
            variant="primary",
            href=f"/events/{event_id}/ack",
            icon_name="check",
        )
        + "</p>"
        if event_id
        else ""
    )

    body = (
        '<div class="cv-card"><h2 class="cv-card__title">事实</h2>'
        f'<dl class="cv-kv">{kv}</dl>{ack_link}</div>'
        '<div class="cv-card"><h2 class="cv-card__title">依据出处</h2>'
        f"{_citation_rows(list(citations or []))}</div>"
        '<div class="cv-card"><h2 class="cv-card__title">处置卡（落库快照）</h2>'
        f"{card_block}</div>"
        '<div class="cv-card"><h2 class="cv-card__title">推送记录</h2>'
        f"{notify_table}</div>"
        '<div class="cv-card"><h2 class="cv-card__title">审计流水</h2>'
        f"{audit_table}</div>"
    )
    return render_layout(
        f"事件 {event_id}",
        body,
        heading=f"事件 {event_id}",
        subtitle="危急值处置记录 · 依据出处、推送与审计流水",
        actions=status_badge(status),
    )


def _citation_details(source: Mapping[str, Any], locator: str = "") -> str:
    """出处明细（可展开）—— 文件名 + 行号 + 版本 + 材料指纹前 8 位。

    展开看到的是**人能回原文核对的那几个数**：文件名、Excel 里的行号、
    材料版本、sha256 前 8 位（够认出是哪一份材料）。sha256 只给前 8 位是
    刻意取舍 —— 复核时人要确认的是「是不是同一份材料」，不是校验整个摘要。
    """
    file_name = str(source.get("source_name", "") or "")
    line_no = source.get("line_no", "")
    version = str(source.get("file_version", "") or "")
    sha = str(source.get("source_sha256", "") or "")
    section = source.get("section_path", "")
    if isinstance(section, (list, tuple)):
        section = " / ".join(str(part) for part in section)
    section = str(section or "")

    if not file_name and line_no in ("", None) and not version and not sha:
        return ""
    if not locator:
        locator = str(source.get("locator", "") or "")

    summary_parts = []
    if file_name:
        summary_parts.append(file_name)
    if line_no not in ("", None):
        summary_parts.append(f"第 {line_no} 行")
    if version:
        summary_parts.append(version)
    summary = " · ".join(summary_parts) or locator or "出处"

    rows: list[tuple[str, str]] = []
    if file_name:
        rows.append(("文件", file_name))
    if line_no not in ("", None):
        rows.append(("行号", str(line_no)))
    if version:
        rows.append(("版本", version))
    if sha:
        rows.append(("材料指纹", sha[:8]))
    if section:
        rows.append(("章节", section))
    if locator:
        rows.append(("定位串", locator))
    kv = "".join(
        f"<dt>{escape(key)}</dt><dd class=\"mono\">{escape(value)}</dd>"
        for key, value in rows
    )
    return (
        f'<details class="cv-element__cite"><summary>出处：{escape(summary)}</summary>'
        f'<dl class="cv-kv">{kv}</dl></details>'
    )


def _element_block(
    key: str,
    label: str,
    value: str,
    status: str,
    reason: str,
    note: str,
    source: Mapping[str, Any],
    locator: str = "",
) -> str:
    """四要素里的一条（值 + 依据状态 + 出处可展开）。"""
    role = _ELEMENT_SEMANTIC.get(status, "neutral")
    status_label = STATUS_LABELS.get(status, status or "依据状态未记录")
    if status == "missing":
        value_html = (
            '<p class="cv-element__value is-missing">【依据缺失】'
            f"{escape(note or reason or '原因未记录')}</p>"
        )
    else:
        degraded = ""
        if status == "degraded" and note:
            degraded = f'<span class="cv-element__degraded">【降级依据】{escape(note)}</span>'
        value_html = f'<p class="cv-element__value">{escape(value)}{degraded}</p>'
    cite = _citation_details(source, locator)
    return (
        f'<section class="cv-element cv-element--{escape(status or "neutral")}" '
        f'data-key="{escape(key)}">'
        '<div class="cv-element__head">'
        f'<span class="cv-badge cv-badge--{escape(role)}">'
        f'<span class="cv-badge__dot"></span>{escape(status_label)}</span>'
        f'<span class="cv-element__label">{escape(label or key)}</span>'
        "</div>"
        f"{value_html}{cite}"
        "</section>"
    )


def _element_sections(snapshot: Any) -> str:
    """四要素分区（**取自快照载荷，不重新渲染**）。

    快照里存的是「当时推给值班人的那份卡」的结构化载荷；阈值表改版、
    制度改版都不会改它 —— 复核时看到的依据与当时一致。
    """
    if snapshot is None:
        return empty_state(
            "本条事件没有处置卡快照",
            desc="不危急或已转人工的事件不会出卡；依据缺失时不编造。",
            icon_name="file",
        )
    payload = dict(getattr(snapshot, "payload", {}) or {})
    by_key = {
        str(item.get("key", "") or ""): item for item in (payload.get("elements") or [])
    }
    parts = []
    for key in CARD_ELEMENT_KEYS:
        element = by_key.get(key)
        if element is None:
            parts.append(
                _element_block(
                    key,
                    key,
                    "",
                    "missing",
                    "",
                    "快照里没有这一条要素",
                    {},
                    "",
                )
            )
            continue
        parts.append(
            _element_block(
                key,
                str(element.get("label", "") or key),
                str(element.get("value", "") or ""),
                str(element.get("status", "") or ""),
                str(element.get("reason", "") or ""),
                str(element.get("note", "") or ""),
                dict(element.get("source") or {}),
                str(element.get("locator", "") or ""),
            )
        )
    return "".join(parts)


def _conflict_block(snapshot: Any) -> str:
    """口径冲突区块（任务 id 33 的产物）—— 显式呈现，不替人裁决。

    两路召回（结构化精确路 / 词法 BM25 路）对同一个项目的说法可能对不上。
    系统**不替值班人选一个**：挑错一次就是按错时限处置一条危急值。
    """
    payload = dict(getattr(snapshot, "payload", {}) or {}) if snapshot is not None else {}
    conflicts = list(payload.get("conflicts") or [])
    if not conflicts:
        return (
            '<div class="cv-card"><h2 class="cv-card__title">口径冲突</h2>'
            '<p class="cv-muted">两路召回对本条项目的说法一致，'
            "没有需要人工裁决的口径分歧。</p></div>"
        )
    items = []
    for conflict in conflicts:
        sides = []
        for side in conflict.get("sides") or []:
            side_html = (
                f'<span class="cv-conflict__path">{escape(str(side.get("path_label", "") or ""))}</span>'
                f'<span class="cv-conflict__value">{escape(str(side.get("value", "") or ""))}</span>'
            )
            locator = str(side.get("locator", "") or "")
            if locator:
                side_html += (
                    f'<span class="cv-conflict__locator mono">{escape(locator)}</span>'
                )
            version = str(side.get("version", "") or "")
            if version:
                side_html += f'<span class="cv-conflict__version">{escape(version)}</span>'
            sides.append(f"<li>{side_html}</li>")
        items.append(
            f'<article class="cv-conflict" data-conflict-id="{escape(str(conflict.get("conflict_id", "") or ""))}">'
            '<div class="cv-conflict__head">'
            '<span class="cv-badge cv-badge--pending"><span class="cv-badge__dot"></span>'
            f'{escape(str(conflict.get("kind_label", "") or "口径冲突"))}</span>'
            f'<span class="cv-conflict__field">{escape(str(conflict.get("field_label", "") or ""))}</span>'
            "</div>"
            f'<p class="cv-conflict__desc">{escape(str(conflict.get("description", "") or ""))}</p>'
            f'<ul class="cv-conflict__sides">{"".join(sides)}</ul>'
            "</article>"
        )
    return (
        '<div class="cv-card"><h2 class="cv-card__title">口径冲突 · 待人工裁决</h2>'
        f'<p class="cv-muted">系统不替人选口径 —— 请按出处核对后再确认处置。</p>'
        f'<div class="cv-conflict-list">{"".join(items)}</div></div>'
    )


def _snapshot_meta(snapshot: Any) -> str:
    """快照版本信息（复核时要看的那几个号）。"""
    if snapshot is None:
        return ""
    rows: list[tuple[str, str]] = []
    if getattr(snapshot, "snapshot_id", ""):
        rows.append(("处置卡快照", str(snapshot.snapshot_id)))
    if getattr(snapshot, "threshold_version", ""):
        rows.append(("阈值表版本", str(snapshot.threshold_version)))
    if getattr(snapshot, "policy_version", ""):
        rows.append(("制度版本", str(snapshot.policy_version)))
    rows.append(("配置版本", str(getattr(snapshot, "config_version", 0) or 0)))
    if getattr(snapshot, "ruleset_snapshot_id", ""):
        rows.append(("规则集快照", str(snapshot.ruleset_snapshot_id)))
    if getattr(snapshot, "created_at", ""):
        rows.append(("快照落盘时刻", _fmt_moment(snapshot.created_at)))
    kv = "".join(
        f"<dt>{escape(key)}</dt><dd class=\"mono\">{escape(value)}</dd>"
        for key, value in rows
    )
    return f'<dl class="cv-kv">{kv}</dl>'


def _receipt_block(row: Mapping[str, Any]) -> str:
    """回执状态（确认后写回登记系统的那一段）。"""
    acknowledged_by = str(row.get("acknowledged_by", "") or "")
    acknowledged_at = str(row.get("acknowledged_at", "") or "")
    registered_at = str(row.get("registered_at", "") or "")
    status = str(row.get("status", "") or "")
    written = (
        '<span class="cv-badge cv-badge--done"><span class="cv-badge__dot"></span>'
        "已回写登记系统</span>"
        if registered_at
        else '<span class="cv-badge cv-badge--pending"><span class="cv-badge__dot"></span>'
        "未回写</span>"
    )
    rows: list[tuple[str, str]] = [
        ("确认人", acknowledged_by or "—"),
        ("确认时刻", _fmt_moment(acknowledged_at) if acknowledged_at else "—"),
        ("回写状态", written),
        ("当前状态", status_badge(status)),
    ]
    kv = "".join(f"<dt>{escape(k)}</dt><dd>{v}</dd>" for k, v in rows)
    return f'<dl class="cv-kv">{kv}</dl>'


def render_ack_page(
    row: Mapping[str, Any],
    *,
    snapshot: Any = None,
    card_text: str = "",
    confirm_url: str = "",
    notice_text: str = "",
    notice_kind: str = "",
    acknowledged: bool = False,
    feedback_kind: str = "",
    error_code: str = "",
    error_detail: str = "",
    replay_enabled: bool = False,
) -> str:
    """处置卡确认页 —— 值班人点确认的唯一入口。

    **页面上的依据全部取自落库快照**（四要素、出处行号、口径冲突、版本），
    不重新渲染 —— 阈值表改版后重渲染出来的东西，已经不是当时给值班人看的
    那份了。

    **版式：两栏**（≥1280 宽屏）—— 左栏是**依据**（四要素 / 口径冲突 /
    版本 / 卡原文），右栏是**动作**（事件事实 / 回执状态 / 确认 / 转人工）。
    值班人不用滚过一屏依据才够得着「确认处置」按钮，但依据始终在动作左边 ——
    先看依据再点确认的顺序不能反。

    **三态与反馈（任务 id 50）**：快照缺失走**错误态**（说清缺的是什么、
    该怎么办）；写操作结果走**toast**（确认成功 / 推送失败 / 回写失败 /
    转人工四类，语义色各不相同）；转人工与补写回写是**破坏性操作**，都要
    二次确认并写原因。
    """
    event_id = str(row.get("event_id", ""))
    status = str(row.get("status", ""))
    facts = [
        ("检验项目", f'{row.get("item_code", "")} {row.get("item_name", "")}'.strip()),
        ("观察值", f'{row.get("value", "")} {row.get("unit", "")}'.strip()),
        ("科室", str(row.get("dept_code", "") or "—")),
        ("消息控制号", str(row.get("message_control_id", "") or "—")),
        ("观察时刻", _fmt_moment(row.get("observed_at"))),
        ("当前状态", _status_label(status)),
    ]
    kv = "".join(f"<dt>{escape(k)}</dt><dd>{escape(v)}</dd>" for k, v in facts)

    notice_block = ""
    if notice_text:
        kind = notice_kind if notice_kind in {"ok", "warn", "err", "info"} else "warn"
        notice_block = notice(notice_text, kind=kind)

    # 写操作反馈（toast）：四类语义各有各的色，失败不与危急混色。
    kind = feedback_kind or feedback_for_status(status)
    toast_html = feedback_toast(kind) if kind else ""

    # 快照缺失 → 错误态（不是一句灰字）：说清缺的是什么、该干什么。
    snapshot_card = ""
    if error_code:
        snapshot_card = (
            '<div class="cv-card cv-card--alert">'
            + error_state(code=error_code, detail=error_detail)
            + "</div>"
        )
    elif snapshot is None:
        snapshot_card = (
            '<div class="cv-card cv-card--alert">'
            + error_state(
                code="snapshot_missing",
                detail=(
                    "本条没有落库的处置卡快照，页面上没有可复核的依据。"
                ),
            )
            + "</div>"
        )

    if acknowledged:
        action_block = (
            '<p class="cv-muted">本条处置已确认，回执按 message_control_id 幂等回写'
            "登记系统；重复点击不会产生第二条回执。</p>"
        )
    else:
        action_block = (
            '<form class="cv-form" method="post" '
            f'action="/events/{escape(event_id)}/ack">'
            + input_field(
                "handler",
                label="确认人（值班人姓名或工号）",
                placeholder="例：检验科值班技师 张三",
                required=True,
            )
            + input_field(
                "note",
                label="处置说明（可选）",
                placeholder="例：已电话通知 ICU 护士长",
            )
            + '<div class="cv-form__actions">'
            + button(
                "确认处置并回写登记系统",
                variant="primary",
                button_type="submit",
                icon_name="check",
            )
            + "</div></form>"
        )

    # 转人工收在可折叠区里：它是**交接**不是**放弃**，不该和「确认」抢同一个
    # 视觉层级；但展开后原因必填、原因进审计。
    transfer_block = (
        '<details class="cv-transfer">'
        '<summary class="cv-transfer__summary">'
        + icon("alert", class_name="cv-icon")
        + "转人工（需填写原因，进审计流水）</summary>"
        '<form class="cv-form cv-form--danger" method="post" novalidate '
        f'action="/events/{escape(event_id)}/transfer" '
        'data-cv-confirm="转人工后本条不再由值班台推进，确定转人工？" '
        'data-cv-confirm-title="确认转人工" '
        'data-cv-confirm-label="确认转人工" '
        'data-cv-confirm-variant="danger">'
        '<div class="cv-field">'
        '<label class="cv-field__label" for="reason">转人工原因（必填，进审计流水）</label>'
        '<textarea class="cv-textarea" id="reason" name="reason" required rows="2" '
        'placeholder="例：阈值表与制度对上报时限口径不一致，需护士长裁决"></textarea>'
        "</div>"
        + input_field(
            "handler",
            label="交接人（值班人姓名或工号）",
            placeholder="例：检验科值班技师 张三",
        )
        + '<div class="cv-form__actions">'
        + button(
            "转人工处理",
            variant="danger",
            button_type="submit",
            icon_name="alert",
        )
        + "</div></form>"
        "</details>"
    )

    # 补写回写：已确认但没写回登记系统的，本条保留在待写队列里等人补写。
    # 这也是**破坏性操作** —— 一次出站调用，幂等键不变但确实会发到登记系统。
    replay_block = ""
    if replay_enabled:
        replay_block = (
            '<div class="cv-card"><h2 class="cv-card__title">补写回写</h2>'
            '<p class="cv-muted">本条已确认但回执还没写回登记系统，'
            "保留在待写队列里等你补写（幂等键不变，不会产生第二条回执）。</p>"
            '<form class="cv-form cv-form--danger" method="post" novalidate '
            f'action="/events/{escape(event_id)}/replay" '
            'data-cv-confirm="补写会把这条回执发到登记系统（幂等键不变，不会产生第二条），确定补写？" '
            'data-cv-confirm-title="确认补写回写" '
            'data-cv-confirm-label="确认补写" '
            'data-cv-confirm-variant="danger">'
            '<div class="cv-field">'
            '<label class="cv-field__label" for="replay-reason">'
            "补写原因（必填，进审计流水）</label>"
            '<textarea class="cv-textarea" id="replay-reason" name="reason" required '
            'rows="2" placeholder="例：登记系统昨日维护后已恢复，本条回执补写"></textarea>'
            "</div>"
            + input_field(
                "handler",
                label="操作人（值班人姓名或工号）",
                placeholder="例：检验科值班技师 张三",
            )
            + '<div class="cv-form__actions">'
            + button(
                "补写回写登记系统",
                variant="danger",
                button_type="submit",
                icon_name="refresh",
            )
            + "</div></form></div>"
        )

    card_block = (
        f'<pre class="cv-cardtext">{escape(card_text)}</pre>'
        if card_text
        else '<p class="cv-muted">处置卡正文缺失，请人工核对阈值表与制度后处置。</p>'
    )

    main_column = (
        snapshot_card
        + '<div class="cv-card"><h2 class="cv-card__title">处置依据（四要素）</h2>'
        f'<div class="cv-element-list">{_element_sections(snapshot)}</div></div>'
        f"{_conflict_block(snapshot)}"
        '<div class="cv-card"><h2 class="cv-card__title">版本与生效期</h2>'
        f"{_snapshot_meta(snapshot)}</div>"
        '<div class="cv-card"><h2 class="cv-card__title">处置卡原文（落库快照）</h2>'
        f"{card_block}</div>"
    )
    side_column = (
        '<div class="cv-card"><h2 class="cv-card__title">事件事实</h2>'
        f'<dl class="cv-kv">{kv}</dl></div>'
        '<div class="cv-card"><h2 class="cv-card__title">回执状态</h2>'
        f"{_receipt_block(row)}</div>"
        '<div class="cv-card"><h2 class="cv-card__title">确认处置</h2>'
        f"{action_block}</div>"
        + replay_block
        + f'<div class="cv-card"><h2 class="cv-card__title">转人工</h2>{transfer_block}</div>'
    )

    body = (
        f"{notice_block}"
        '<div class="cv-ack-layout">'
        f'<div class="cv-ack-main">{main_column}</div>'
        f'<div class="cv-ack-side">{side_column}</div>'
        "</div>"
        f'<p class="cv-backlink"><a href="/events/{escape(event_id)}">← 回到事件详情</a></p>'
    )
    return render_layout(
        f"确认处置 {event_id}",
        body,
        heading=f"确认处置 · {event_id}",
        subtitle="点确认后回执才会回写登记系统；未确认前本条只是建议",
        actions=status_badge(status),
        toast_html=toast_html,
    )


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------


@dataclass
class PageResponse:
    """一次页面请求的结论。"""

    status: int = 200
    body: str = ""
    content_type: str = "text/html; charset=utf-8"
    headers: Mapping[str, str] = field(default_factory=dict)


@dataclass
class AckResult:
    """确认动作的结果。"""

    ok: bool = False
    message: str = ""
    detail: str = ""
    written_back: bool = False
    status: str = ""


@dataclass
class ReplayResult:
    """补写回写（重放一次出站回写）的结果。

    与 :class:`AckResult` 分开：补写是**破坏性操作**（一次真实出站调用），
    结果语义也不同 —— 补写失败**不丢**已确认的回执，只是本条仍留在待写
    队列里。
    """

    ok: bool = False
    message: str = ""
    error_code: str = ""
    written_back: bool = False
    status: str = ""


def _event_id(raw: str) -> str:
    """事件号（路径段）→ 解码后的事件号。

    **按段解码**：``/events/evt-%E4%B8%8D`` 的事件号是「evt-不」。
    不解码的话页面上会把 ``%E4%B8%8D`` 原样印出来，值班人根本看不出
    自己点的是哪一条。

    只解**这一段** —— 整条路径 unquote 可能把 ``%2F`` 变成真斜杠，
    段内解出来的斜杠仍留在段里，不会把路由拆成别的路径。
    """
    return unquote(str(raw or ""))


def _ack_feedback(result: "AckResult") -> tuple[str, str]:
    """确认动作的结果 → **(toast 反馈类型, 错误码)**。

    * 成功 → ``("ack_ok", "")``：绿，事实已生效；
    * 确认成功但回写失败 → ``("writeback_failed", "writeback_failed")`` ——
      已确认的回执不丢，本条留在待写队列里等人补写；
    * 通道没接入 → ``("", "writeback_unavailable")`` —— 这种要停下来处理，
      页面给**错误卡**而不是一闪而过的 toast。
    """
    if result.ok:
        return "ack_ok", ""
    message = str(result.message or "")
    if "未接入" in message:
        return "", "writeback_unavailable"
    if "回写" in message:
        return "writeback_failed", "writeback_failed"
    return "", ""


class ConsoleRouter:
    """值班台路由（页面 + 确认动作）。

    ``ack_handler`` 是确认动作的**唯一**实现入口：默认实现只做状态跃迁，
    不回写；守护进程会注入带幂等回写的实现（``RegistryWriteback``）。

    ``replay_handler`` 是补写回写的实现入口（任务 id 50）：默认实现明说
    「补写通道未接入」，不假装补上了。

    ``threshold_path`` 让页面能自己判断「阈值表版本绑没绑」—— 配了路径但
    文件读不到时，值班台要**显式报错**，而不是让值班人对着空台账以为今天
    没有危急值。
    """

    def __init__(
        self,
        store: Any,
        *,
        token: str = "",
        ack_handler: Optional[Callable[[str, str, str], AckResult]] = None,
        replay_handler: Optional[Callable[[str, str, str], ReplayResult]] = None,
        snapshot_dir: str = "",
        base_url: str = "",
        threshold_path: str = "",
    ) -> None:
        self.store = store
        self.token = token or ""
        self.ack_handler = ack_handler
        self.replay_handler = replay_handler
        self.snapshot_dir = snapshot_dir or ""
        self.base_url = base_url or ""
        self.threshold_path = threshold_path or ""

    # ---- 路由匹配 --------------------------------------------------------
    def handles(self, path: str) -> bool:
        """这个路径归值班台管吗？

        **入站推送端点不归这里管** —— ``/api/v1/critical-value-events`` 与
        ``/api/v1/local-notifications`` 走 ``console/inbound.py`` 的服务层，
        值班台路由只认 ``/api/events`` 前缀（两者不会撞）。
        """
        clean = str(path or "").split("?", 1)[0]
        if clean in {HOME_PATH, EVENTS_PATH}:
            return True
        if clean.startswith("/events/"):
            return True
        if clean == API_PATH or clean.startswith(API_PATH_PREFIX + "/"):
            return True
        if clean.startswith(STATIC_PREFIX):
            return True
        return False

    # ---- 鉴权 -------------------------------------------------------------
    def authorized(self, headers: Mapping[str, str], query: Mapping[str, list[str]]) -> bool:
        """没配凭据位就本机直开；配了就必须带对。"""
        if not self.token:
            return True
        presented = ""
        for key, value in (headers or {}).items():
            if str(key).strip().lower() == "authorization":
                parts = str(value or "").split(None, 1)
                if len(parts) == 2 and parts[0].lower() == "bearer":
                    presented = parts[1].strip()
                break
        if not presented:
            values = query.get("token") or []
            presented = str(values[0]) if values else ""
        if not presented:
            return False
        return hmac.compare_digest(
            presented.encode("utf-8"), self.token.encode("utf-8")
        )

    # ---- 主入口 -----------------------------------------------------------
    def handle(
        self,
        method: str,
        path: str,
        *,
        headers: Optional[Mapping[str, str]] = None,
        body: bytes = b"",
    ) -> PageResponse:
        """处理一次页面请求。"""
        split = urlsplit(path or "/")
        clean = split.path or "/"
        query = parse_qs(split.query or "")

        # 静态资源（样式表）**不设值班台凭据** —— 它不含任何业务数据，
        # 而且浏览器请求 ``<link>`` 时不会带上 ``?token=``。
        if clean.startswith(STATIC_PREFIX):
            return self._static(clean)

        if not self.authorized(headers or {}, query):
            return PageResponse(
                status=401,
                body=render_layout(
                    "需要凭据",
                    notice(
                        "值班台凭据缺失或不匹配。请在链接后带 ?token=… "
                        "或带 Authorization: Bearer 头访问。",
                        kind="err",
                    ),
                ),
            )

        if clean == HOME_PATH:
            return PageResponse(body=render_home(self.store))
        if clean == EVENTS_PATH:
            return self._list(query)
        if clean == API_PATH or clean.startswith(API_PATH_PREFIX + "/"):
            return self._api(clean, query)
        if clean.startswith("/events/"):
            rest = clean[len("/events/"):]
            if rest.endswith("/ack"):
                event_id = _event_id(rest[: -len("/ack")])
                if method.upper() == "POST":
                    return self._ack(event_id, body)
                return self._ack_page(event_id)
            if rest.endswith("/transfer"):
                event_id = _event_id(rest[: -len("/transfer")])
                if method.upper() == "POST":
                    return self._transfer(event_id, body)
                return PageResponse(
                    status=405,
                    body=render_layout(
                        "方法不允许",
                        notice("转人工是写操作，请用 POST 提交。", kind="warn"),
                    ),
                )
            if rest.endswith("/replay"):
                event_id = _event_id(rest[: -len("/replay")])
                if method.upper() == "POST":
                    return self._replay(event_id, body)
                return PageResponse(
                    status=405,
                    body=render_layout(
                        "方法不允许",
                        notice("补写回写是写操作，请用 POST 提交。", kind="warn"),
                    ),
                )
            return self._detail(_event_id(rest))
        return PageResponse(
            status=404,
            body=render_layout(
                "找不到页面",
                error_state(
                    code="event_not_found",
                    detail="这个地址在值班台上不存在。",
                    suggestion="回值班台概览或事件台账重新选一条。",
                    action=button(
                        "回到事件台账", variant="primary", href="/events"
                    ),
                ),
            ),
        )

    # ---- 筛选条件 ---------------------------------------------------------
    @staticmethod
    def _first(query: Mapping[str, list[str]], name: str) -> str:
        """取第一个查询参数值（空串表示「不限」）。"""
        values = query.get(name) or []
        return str(values[0]).strip() if values else ""

    @staticmethod
    def _tri_state(query: Mapping[str, list[str]], name: str) -> Optional[bool]:
        """三态参数（``1`` / ``0`` / 不带）。

        ``None`` = 不限；``True`` = 只要「是」；``False`` = 只要「否」。
        读不出来的一律按「不限」处理 —— 宁可多列几条，也不要因为一个参数
        拼错就让人以为台账里没有这条。
        """
        raw = ConsoleRouter._first(query, name)
        if raw in {"1", "true", "yes", "是", "y"}:
            return True
        if raw in {"0", "false", "no", "否", "n"}:
            return False
        return None

    def _parse_filters(self, query: Mapping[str, list[str]]) -> dict[str, Any]:
        """把查询串翻译成筛选条件（**状态不认识就丢掉这个条件**，不报错）。

        状态值来自状态机（13 个）；外来一个不认识的状态时，页面照常打开，
        只是不按状态筛 —— 值班人看到的是「没筛」，而不是一个 500。

        **状态可以是多个**（逗号分隔）：概览条上的「待确认」一次覆盖
        ``notified`` 与 ``pending_ack``，所以筛选条件里带得出多值。
        """
        raw = self._first(query, "status")
        wanted = [part.strip() for part in raw.split(",") if part.strip()]
        known = [part for part in wanted if part in ALL_STATUSES]
        status = ",".join(known)
        sort = self._first(query, "sort")
        if sort and sort not in SORT_COLUMNS:
            sort = ""
        return {
            "dept": self._first(query, "dept"),
            "item": self._first(query, "item"),
            "status": status,
            "statuses": tuple(known),
            "overdue": self._tri_state(query, "overdue"),
            "written_back": self._tri_state(query, "written_back"),
            "sort": sort,
        }

    def _ledger_query(
        self, filters: Mapping[str, Any], *, page: int, per_page: int = PAGE_SIZE
    ) -> LedgerQuery:
        """筛选条件 → **任务 id 37 的同一套查询**（口径不分叉）。"""
        return LedgerQuery(
            dept_code=str(filters.get("dept", "") or ""),
            item_code=str(filters.get("item", "") or ""),
            statuses=tuple(filters.get("statuses", ()) or ()),
            overdue=filters.get("overdue", None),
            written_back=filters.get("written_back", None),
            sort=str(filters.get("sort", "") or "observed_at"),
            limit=int(per_page),
            offset=max(0, (int(page) - 1) * int(per_page)),
        )

    def _distinct(self, column: str) -> list[str]:
        """取某列的去重值（给筛选下拉用）；台账读不出来时给空表。"""
        try:
            return list(distinct_values(self.store, column))
        except Exception:  # noqa: BLE001 —— 下拉取不到值不该让页面打不开
            logger.exception("取去重值失败：%s", column)
            return []

    # ---- 各页面 -----------------------------------------------------------
    def _static(self, clean_path: str) -> PageResponse:
        """吐静态资源（样式表与值班台脚本）。

        **只放行白名单里的文件名** —— 静态资源路由不能变成任意文件读取口
        （``/static/../../etc/passwd`` 这类路径必须在这里被挡住）。

        **Content-Type 按扩展名给**：CSS 当 HTML 吐，浏览器会把样式当标签
        解析，页面直接没样式；JS 当 CSS 吐，脚本压根不执行。
        """
        name = clean_path[len(STATIC_PREFIX):]
        if name not in _STATIC_FILES:
            return PageResponse(status=404, body="not found", content_type="text/plain; charset=utf-8")
        from critval.console.tokens import TEMPLATES_DIR

        target = TEMPLATES_DIR / name
        try:
            body = target.read_text(encoding="utf-8")
        except OSError:
            logger.exception("读取静态资源失败：%s", name)
            return PageResponse(status=500, body="read failed", content_type="text/plain; charset=utf-8")
        return PageResponse(
            body=body,
            content_type=_STATIC_CONTENT_TYPES.get(name, "text/plain; charset=utf-8"),
            headers={"Cache-Control": "no-cache"},
        )

    def _list(self, query: Mapping[str, list[str]]) -> PageResponse:
        """事件台账页。

        **两种片段模式**（任务 id 50）：

        * ``?fragment=region`` —— 只回台账区域（明细表 + 分页）的 HTML 片段，
          供页面上的「刷新台账」按钮整块替换。**服务端渲染仍是唯一真源**，
          浏览器端不拼 HTML —— 刷新出来的表格与首屏那张是同一套渲染函数。
        * ``?bootstrap=1`` —— 首屏先给**加载态**骨架，脚本随后取片段填进去；
          脚本没跑起来时，页面上有「直接加载完整页面」的退路（``<noscript>``）。
        """
        filters = self._parse_filters(query)
        page = max(1, self._page_number(query))
        per_page = PAGE_SIZE

        total = count_events(self.store, self._ledger_query(filters, page=1, per_page=0))
        pages = max(1, (total + per_page - 1) // per_page)
        page = min(page, pages)
        rows = query_events(self.store, self._ledger_query(filters, page=page, per_page=per_page))

        if self._first(query, "fragment") == "region":
            region = _ledger_region(
                rows,
                active=filters,
                total=total,
                page=page,
                per_page=per_page,
            )
            return PageResponse(body=region, content_type="text/html; charset=utf-8")

        bootstrap = self._first(query, "bootstrap") in {"1", "true", "yes"}
        return PageResponse(
            body=render_event_list(
                rows,
                filters=filters,
                total=total,
                page=page,
                per_page=per_page,
                dept_options=self._distinct("dept_code"),
                item_options=self._distinct("item_code"),
                buckets=_ledger_buckets(self.store, filters),
                loading=bootstrap,
                threshold_missing=self._threshold_missing(),
            )
        )

    def _threshold_missing(self) -> bool:
        """阈值表配了路径但文件读不到 → **显式报错**。

        配了路径读不到文件，说明规则集没绑定版本 —— 判定会直接转人工，
        不会产出危急值事件。值班台这时要是只留一句灰字，值班人会以为
        「今天真没有危急值」，实际是链路没通。
        """
        if not self.threshold_path:
            return False
        return not Path(self.threshold_path).exists()

    def _page_number(self, query: Mapping[str, list[str]]) -> int:
        """页码（非法值按第 1 页算，不报错）。"""
        raw = self._first(query, "page")
        try:
            return max(1, int(raw))
        except (TypeError, ValueError):
            return 1

    def _api(self, clean_path: str, query: Mapping[str, list[str]]) -> PageResponse:
        """JSON 接口：台账列表与事件详情（**字段白名单**）。

        出口与页面分开：页面给值班人看，接口给院内系统看；两者读的是同一份
        台账与同一份处置卡快照，但接口的字段是白名单 —— 患者标识层字段
        一个都不给（见 ``critval/console/api.py``）。
        """
        if clean_path == API_PATH:
            filters = self._parse_filters(query)
            page = max(1, self._page_number(query))
            per_page = PAGE_SIZE
            total = count_events(
                self.store, self._ledger_query(filters, page=1, per_page=0)
            )
            rows = query_events(
                self.store, self._ledger_query(filters, page=page, per_page=per_page)
            )
            payload = build_event_list(
                rows,
                total=total,
                page=page,
                per_page=per_page,
                filters={
                    key: value
                    for key, value in filters.items()
                    # ``statuses`` 是 ``status`` 的内部形态，不进对外契约
                    if value not in ("", None) and key != "statuses"
                },
            )
            return PageResponse(
                body=json_response(payload),
                content_type="application/json; charset=utf-8",
            )

        event_id = _event_id(clean_path[len(API_PATH_PREFIX) + 1:])
        row = self.store.get_event(event_id)
        if not row:
            return PageResponse(
                status=404,
                body=json_response(
                    {"error": "not_found", "detail": f"台账里没有事件 {event_id}。"}
                ),
                content_type="application/json; charset=utf-8",
            )
        payload = build_event_detail(
            row,
            citations=self.store.citations_of(event_id),
            notifications=self.store.notifications_of(event_id),
            audits=self.store.audit_entries(event_id),
            snapshot=self._snapshot(event_id),
        )
        return PageResponse(
            body=json_response(payload),
            content_type="application/json; charset=utf-8",
        )

    def _detail(self, event_id: str) -> PageResponse:
        row = self.store.get_event(event_id)
        if not row:
            return PageResponse(
                status=404,
                body=render_layout(
                    "找不到事件",
                    error_state(
                        code="event_not_found",
                        detail=f"台账里没有事件 {event_id}。",
                        action=button(
                            "回到事件台账", variant="primary", href="/events"
                        ),
                    ),
                ),
            )
        card_text = self._card_text(event_id)
        confirm_url = self.confirm_url(event_id)
        return PageResponse(
            body=render_event_detail(
                row,
                citations=self.store.citations_of(event_id),
                notifications=self.store.notifications_of(event_id),
                audits=self.store.audit_entries(event_id),
                card_text=card_text,
                confirm_url=confirm_url,
            )
        )

    def _ack_page(self, event_id: str) -> PageResponse:
        row = self.store.get_event(event_id)
        if not row:
            return PageResponse(
                status=404,
                body=render_layout(
                    "找不到事件",
                    error_state(
                        code="event_not_found",
                        detail=f"台账里没有事件 {event_id}。",
                        action=button(
                            "回到事件台账", variant="primary", href="/events"
                        ),
                    ),
                ),
            )
        status = str(row.get("status", ""))
        acknowledged = status in {"acknowledged", "registered", "archived"}
        snapshot = self._snapshot(event_id)
        return PageResponse(
            body=render_ack_page(
                row,
                snapshot=snapshot,
                card_text=self._card_text_from(snapshot),
                confirm_url=self.confirm_url(event_id),
                acknowledged=acknowledged,
                error_code="snapshot_missing" if snapshot is None else "",
                error_detail=self._snapshot_missing_detail(),
                replay_enabled=self._replay_enabled(status),
            )
        )

    def _snapshot_missing_detail(self) -> str:
        """快照缺失时要说清「是没配还是丢了」—— 两种情况的处理完全不同。"""
        if not self.snapshot_dir:
            return (
                "处置卡快照目录没配（CRITVAL_SNAPSHOT_DIR），页面上没有可复核的依据。"
            )
        return "本条没有落库的处置卡快照，页面上没有可复核的依据。"

    @staticmethod
    def _replay_enabled(status: str) -> bool:
        """这条能不能补写回写。

        只有**已确认但没写回**的状态才给补写入口 —— 已回写的不用补，
        还没确认的不许补（未确认的回执不许变成登记系统里的已生效事实）。
        """
        return str(status or "") in {"acknowledged", "writeback_failed"}

    def _ack(self, event_id: str, body: bytes) -> PageResponse:
        """确认动作 —— 值班人点确认的唯一入口。

        **结果反馈分两路**：表单级错误（没填确认人）走**提示条**（要停下来
        改）；写操作结果走 **toast**（确认成功 / 回写失败）。两者不重复。
        """
        row = self.store.get_event(event_id)
        if not row:
            return PageResponse(
                status=404,
                body=render_layout(
                    "找不到事件",
                    error_state(
                        code="event_not_found",
                        detail=f"台账里没有事件 {event_id}。",
                    ),
                ),
            )
        form = self._parse_form(body)
        handler = form.get("handler", "").strip()
        note = form.get("note", "").strip()
        snapshot = self._snapshot(event_id)
        if not handler:
            return PageResponse(
                status=400,
                body=render_ack_page(
                    row,
                    snapshot=snapshot,
                    card_text=self._card_text_from(snapshot),
                    notice_text="必须写确认人 —— 没人签的字不进门。",
                    notice_kind="err",
                    error_code="confirm_required",
                    error_detail="这一栏是必填的：没人签的字不进门。",
                    replay_enabled=self._replay_enabled(str(row.get("status", ""))),
                ),
            )
        if self.ack_handler is None:
            result = AckResult(ok=False, message="确认通道未接入，无法处理确认动作。")
        else:
            result = self.ack_handler(event_id, handler, note)
        kind = "ok" if result.ok else ("err" if "未接入" in result.message else "warn")
        # 确认后重新读事件行 —— 回执状态（确认人 / 确认时刻 / 回写时刻）要
        # 显示**落库后的真实值**，不是提交前的旧行。
        refreshed = self.store.get_event(event_id) or row
        feedback_kind, error_code = _ack_feedback(result)
        return PageResponse(
            body=render_ack_page(
                refreshed,
                snapshot=snapshot,
                card_text=self._card_text_from(snapshot),
                notice_text=result.message if not result.ok else "",
                notice_kind=kind,
                acknowledged=result.ok,
                feedback_kind=feedback_kind,
                error_code=error_code,
                error_detail=result.message if not result.ok else "",
                replay_enabled=self._replay_enabled(str(refreshed.get("status", ""))),
            )
        )

    def _transfer(self, event_id: str, body: bytes) -> PageResponse:
        """转人工：状态跃迁到 ``transferred_to_human``，**原因必须填**。

        为什么原因必填：转人工是**交接**，不是「放弃」。没有原因的交接，
        事后复核时看到的是「这条卡为什么停在这儿」—— 答不上来。原因进
        审计流水（append-only），与状态跃迁一起留痕。
        """
        row = self.store.get_event(event_id)
        if not row:
            return PageResponse(
                status=404,
                body=render_layout(
                    "找不到事件",
                    error_state(
                        code="event_not_found",
                        detail=f"台账里没有事件 {event_id}。",
                    ),
                ),
            )
        form = self._parse_form(body)
        reason = form.get("reason", "").strip()
        handler = form.get("handler", "").strip() or "值班台"
        snapshot = self._snapshot(event_id)

        if not reason:
            return PageResponse(
                status=400,
                body=render_ack_page(
                    row,
                    snapshot=snapshot,
                    card_text=self._card_text_from(snapshot),
                    notice_text="转人工必须写原因 —— 没人签的原因不算交接。",
                    notice_kind="err",
                    error_code="reason_required",
                    error_detail="转人工是交接，原因要写清为什么这条不由值班台继续推进。",
                    replay_enabled=self._replay_enabled(str(row.get("status", ""))),
                ),
            )
        try:
            changed = self.store.set_status(
                event_id,
                "transferred_to_human",
                actor=handler,
                detail=f"值班人转人工：{reason}",
            )
        except IllegalTransitionError as exc:
            return PageResponse(
                status=400,
                body=render_ack_page(
                    row,
                    snapshot=snapshot,
                    card_text=self._card_text_from(snapshot),
                    notice_text=f"转人工失败：{exc}",
                    notice_kind="err",
                    error_code="illegal_transition",
                    error_detail=str(exc),
                    replay_enabled=self._replay_enabled(str(row.get("status", ""))),
                ),
            )
        # 原因单独记一行审计（动作名可检索），状态跃迁审计里也带着原因。
        self.store.append_audit(
            "transfer_to_human",
            event_id=event_id,
            actor=handler,
            dept_code=str(row.get("dept_code", "") or ""),
            specimen_no=str(row.get("specimen_no", "") or ""),
            item_code=str(row.get("item_code", "") or ""),
            message_control_id=str(row.get("message_control_id", "") or ""),
            detail=reason,
        )
        refreshed = self.store.get_event(event_id) or row
        message = (
            "已转人工，原因已进审计流水；本条不再由值班台自动推进。"
            if changed
            else "本条已经是「转人工」状态，原因已补记进审计流水。"
        )
        return PageResponse(
            body=render_ack_page(
                refreshed,
                snapshot=snapshot,
                card_text=self._card_text_from(snapshot),
                notice_text=message,
                notice_kind="ok",
                acknowledged=True,
                feedback_kind="transferred",
                replay_enabled=self._replay_enabled(str(refreshed.get("status", ""))),
            )
        )

    def _replay(self, event_id: str, body: bytes) -> PageResponse:
        """补写回写：把已确认但没写回登记系统的回执补一次。

        **这是破坏性操作** —— 一次真实出站调用。幂等键与首次一致，所以
        不会在登记系统里产生第二条；**内容变了会被幂等冲突闸门拦下**
        （转人工，不覆盖既有事实）。

        **原因必须填**：补写为什么拖到现在，是复核时要回答的问题。
        """
        row = self.store.get_event(event_id)
        if not row:
            return PageResponse(
                status=404,
                body=render_layout(
                    "找不到事件",
                    error_state(
                        code="event_not_found",
                        detail=f"台账里没有事件 {event_id}。",
                    ),
                ),
            )
        form = self._parse_form(body)
        reason = form.get("reason", "").strip()
        handler = form.get("handler", "").strip() or "值班台"
        snapshot = self._snapshot(event_id)
        status = str(row.get("status", ""))

        if not reason:
            return PageResponse(
                status=400,
                body=render_ack_page(
                    row,
                    snapshot=snapshot,
                    card_text=self._card_text_from(snapshot),
                    notice_text="补写必须写原因 —— 没人签的原因不算补写。",
                    notice_kind="err",
                    error_code="reason_required",
                    error_detail="补写是一次真实出站调用，原因要写清为什么拖到现在。",
                    replay_enabled=self._replay_enabled(status),
                ),
            )
        if self.replay_handler is None:
            return PageResponse(
                status=503,
                body=render_ack_page(
                    row,
                    snapshot=snapshot,
                    card_text=self._card_text_from(snapshot),
                    notice_text="补写通道未接入，无法补写。",
                    notice_kind="err",
                    error_code="replay_unavailable",
                    error_detail=(
                        "补写要走登记系统的出站通道，凭据位没配时补不了。"
                    ),
                    replay_enabled=self._replay_enabled(status),
                ),
            )
        result = self.replay_handler(event_id, handler, reason)
        refreshed = self.store.get_event(event_id) or row
        feedback = "ack_ok" if result.ok else "writeback_failed"
        return PageResponse(
            body=render_ack_page(
                refreshed,
                snapshot=snapshot,
                card_text=self._card_text_from(snapshot),
                notice_text="" if result.ok else result.message,
                notice_kind="ok" if result.ok else "err",
                acknowledged=True,
                feedback_kind=feedback,
                error_code="" if result.ok else (result.error_code or "replay_failed"),
                error_detail="" if result.ok else result.message,
                replay_enabled=self._replay_enabled(
                    str(refreshed.get("status", ""))
                ),
            )
        )

    # ---- 辅助 -------------------------------------------------------------
    @staticmethod
    def _parse_form(body: bytes) -> dict[str, str]:
        """解析 ``application/x-www-form-urlencoded`` 表单。"""
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError:
            return {}
        return {k: v[0] for k, v in parse_qs(text, keep_blank_values=True).items()}

    def confirm_url(self, event_id: str) -> str:
        """确认链接 —— 推送卡片里的那个链接就是它。"""
        base = self.base_url or ""
        return f"{base}/events/{event_id}/ack"

    def _snapshot(self, event_id: str) -> Any:
        """取处置卡快照对象（**不重新渲染**）。

        快照读不到（目录没配 / 文件丢了）返回 ``None`` —— 页面据此显示
        「没有处置卡快照」，而不是重新渲染一张新的糊弄过去。
        """
        if not self.snapshot_dir:
            return None
        try:
            from critval.card.snapshot import CardSnapshotStore

            store = CardSnapshotStore(self.snapshot_dir)
            return store.find_by_event(event_id)
        except Exception:  # noqa: BLE001 —— 快照读不到不该让页面打不开
            logger.exception("读取处置卡快照失败：event_id=%s", event_id)
            return None

    @staticmethod
    def _card_text_from(snapshot: Any) -> str:
        """快照 → 处置卡正文（**当时推送出去的那一份原文**）。"""
        if snapshot is None:
            return ""
        try:
            return snapshot.rendering("text") or snapshot.rendering("markdown")
        except KeyError:
            return ""

    def _card_text(self, event_id: str) -> str:
        """取处置卡快照正文（**不重新渲染**）。"""
        return self._card_text_from(self._snapshot(event_id))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
