"""值班台基础组件（任务 id 48）。

**这一层提供什么**

一组**服务端渲染**的基础组件 —— 按钮、输入框、下拉、表格、状态徽标、
出处角标、提示条、空态、toast、统计条 —— 全部返回 HTML 片段，可直接拼进
``templates/base.html`` 的 ``{{content}}`` 位。

**三条纪律**

1. **不引前端构建链**。检验科值班机上装个 Python 就能跑，是本产品能被
   写进采购清单的前提之一 —— 所以组件是 Python 函数 + 原生 CSS，不是
   React 组件。
2. **不出现 emoji**。所有图标是**内联 SVG**（线性描边、``currentColor``），
   跟着语义色走；emoji 在值班大屏上分辨率不可控，还会跟文字基线打架。
3. **样式只走 token**。组件里不写死颜色 / 字号 / 间距，颜色一律由
   ``tokens.py`` 的语义角色决定（见 ``critval/console/tokens.py``）。

**为什么按钮要写全四态**：值班人点「确认处置」之后，按钮必须马上变成
loading 且**不可重复点** —— 否则就是同一张卡被确认两次（幂等键挡得住，
但值班人看见的是「我点了没反应」）。所以 ``disabled`` / ``loading`` 是
**功能状态**，不是装饰。
"""

from __future__ import annotations

import html
import re
from typing import Any, Iterable, Mapping, Optional, Sequence

from critval.console.tokens import badge_class
from critval.ledger.states import describe_status

__all__ = [
    "BUTTON_VARIANTS",
    "ICONS",
    "TOAST_KINDS",
    "button",
    "citation_chip",
    "empty_state",
    "icon",
    "input_field",
    "notice",
    "page_head",
    "render_template",
    "select_field",
    "statbar",
    "status_badge",
    "table",
    "toast",
]


def escape(value: Any) -> str:
    """HTML 转义 —— 组件里出现的任何外来文本都要过这一道。"""
    return html.escape("" if value is None else str(value), quote=True)


# ---------------------------------------------------------------------------
# 图标（内联 SVG，不用 emoji）
# ---------------------------------------------------------------------------

#: 内联 SVG 图标集。``viewBox`` 统一 24×24，描边色走 ``currentColor``。
ICONS: dict[str, str] = {
    "list": '<path d="M8 6h13M8 12h13M8 18h13"/><path d="M3.5 6h.01M3.5 12h.01M3.5 18h.01"/>',
    "activity": '<path d="M22 12h-4l-3 9L9 3l-3 9H2"/>',
    "check": '<path d="M20 6 9 17l-5-5"/>',
    "check-circle": '<circle cx="12" cy="12" r="9"/><path d="m8.5 12 2.5 2.5 4.5-5"/>',
    "alert": '<path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z"/><path d="M12 9v4M12 17h.01"/>',
    "info": '<circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 8h.01"/>',
    "clock": '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    "inbox": '<path d="M22 12h-6l-2 3h-4l-2-3H2"/><path d="M5.5 5h13l3.5 7v6a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2v-6Z"/>',
    "file": '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8Z"/><path d="M14 2v6h6"/>',
    "shield": '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10Z"/><path d="m9 12 2 2 4-4"/>',
    "user": '<circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/>',
    "refresh": '<path d="M21 12a9 9 0 1 1-2.6-6.4"/><path d="M21 3v6h-6"/>',
    "x": '<path d="M18 6 6 18M6 6l12 12"/>',
    "chevron-down": '<path d="m6 9 6 6 6-6"/>',
    "chevron-right": '<path d="m9 6 6 6-6 6"/>',
    "sort-asc": '<path d="M12 19V5M5 12l7-7 7 7"/>',
    "sort-desc": '<path d="M12 5v14M19 12l-7 7-7-7"/>',
    "external": '<path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><path d="M15 3h6v6M10 14 21 3"/>',
    "spinner": '<path d="M12 3a9 9 0 1 0 9 9" />',
    "hospital": '<path d="M3 21h18"/><path d="M5 21V7l7-4 7 4v14"/><path d="M12 9v6M9 12h6"/>',
}


def icon(name: str, *, class_name: str = "cv-icon", title: str = "") -> str:
    """内联 SVG 图标。

    未知图标名回落到 ``info`` 图标而不是抛错 —— 图标拼错不该让值班台
    页面打不开。
    """
    body = ICONS.get(str(name or "").strip(), ICONS["info"])
    attrs = f'class="{escape(class_name)}" viewBox="0 0 24 24" aria-hidden="true"'
    if title:
        attrs = f'class="{escape(class_name)}" viewBox="0 0 24 24" role="img"'
        return f"<svg {attrs}><title>{escape(title)}</title>{body}</svg>"
    return f"<svg {attrs}>{body}</svg>"


def spinner(class_name: str = "cv-spinner") -> str:
    """loading 态的转圈图标。"""
    return (
        f'<svg class="{escape(class_name)}" viewBox="0 0 24 24" aria-hidden="true">'
        f"{ICONS['spinner']}</svg>"
    )


# ---------------------------------------------------------------------------
# 模板替换
# ---------------------------------------------------------------------------

_PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


def render_template(text: str, values: Mapping[str, Any]) -> str:
    """把 ``{{name}}`` 占位符替换成 ``values`` 里的值。

    **不做 HTML 转义** —— 塞进模板的每一段都已经是成品 HTML，转义要由
    各自的渲染函数负责（在这里再转一次会把标签也转掉）。
    未提供的占位符替换成空串（不留 ``{{...}}`` 残渣在页面上）。
    """

    def _replace(match: "re.Match[str]") -> str:
        key = match.group(1)
        if key not in values:
            return ""
        return "" if values[key] is None else str(values[key])

    return _PLACEHOLDER.sub(_replace, str(text or ""))


def load_base_template() -> str:
    """读页面骨架模板（``templates/base.html``）。"""
    from critval.console.tokens import base_template_path

    try:
        return base_template_path().read_text(encoding="utf-8")
    except OSError:
        return ""


# ---------------------------------------------------------------------------
# 按钮
# ---------------------------------------------------------------------------

#: 按钮变体（主 / 次 / 危险 / 幽灵）。
BUTTON_VARIANTS: tuple[str, ...] = ("primary", "secondary", "danger", "ghost")

#: toast 样式档（写操作反馈的语义色）。
#:
#: ``failed`` 与 ``err`` 分开：危急红留给临床上的危急值，通道坏了是
#: 「坏了」，不该用红色去打断夜班。
TOAST_KINDS: tuple[str, ...] = ("ok", "warn", "err", "info", "failed")


def button(
    label: str,
    *,
    variant: str = "primary",
    state: str = "default",
    icon_name: str = "",
    button_type: str = "button",
    name: str = "",
    value: str = "",
    href: str = "",
    title: str = "",
) -> str:
    """按钮 —— 变体（主 / 次 / 危险 / 幽灵）× 状态（default / hover / active /
    disabled / loading）。

    * ``state="disabled"`` → 渲染 ``disabled`` 属性，**真的点不动**；
    * ``state="loading"`` → 加 ``is-loading`` 类并插入转圈图标，配合
      ``disabled`` 让按钮不可重复点击。

    ``hover`` / ``active`` 由 CSS 伪类承担，不作为 HTML 属性输出 ——
    只有 ``disabled`` / ``loading`` 是服务端能确定的状态。
    """
    variant = variant if variant in BUTTON_VARIANTS else "primary"
    classes = ["cv-btn", f"cv-btn--{variant}"]
    attrs: list[str] = []

    if state == "loading":
        classes.append("is-loading")
        attrs.append("disabled")
    elif state == "disabled":
        classes.append("is-disabled")
        attrs.append("disabled")

    if name:
        attrs.append(f'name="{escape(name)}"')
    if value:
        attrs.append(f'value="{escape(value)}"')
    if title:
        attrs.append(f'title="{escape(title)}"')
    if href:
        attrs.append(f'href="{escape(href)}"')

    inner = ""
    if state == "loading":
        inner += spinner()
    elif icon_name:
        inner += icon(icon_name)
    inner += f'<span class="cv-btn__label">{escape(label)}</span>'

    class_attr = " ".join(classes)
    attr_text = (" " + " ".join(attrs)) if attrs else ""
    if href and state not in {"disabled", "loading"}:
        return f'<a class="{class_attr}"{attr_text}>{inner}</a>'
    type_attr = f'type="{escape(button_type)}"'
    return f'<button class="{class_attr}" {type_attr}{attr_text}>{inner}</button>'


# ---------------------------------------------------------------------------
# 表单
# ---------------------------------------------------------------------------


def input_field(
    name: str,
    *,
    label: str = "",
    value: str = "",
    placeholder: str = "",
    input_type: str = "text",
    error: str = "",
    hint: str = "",
    required: bool = False,
    disabled: bool = False,
    attrs: str = "",
) -> str:
    """输入框 —— 标签、错误态提示与辅助说明齐全。"""
    classes = ["cv-input"]
    if error:
        classes.append("has-error")
    field_attrs = [f'type="{escape(input_type)}"', f'name="{escape(name)}"']
    if value:
        field_attrs.append(f'value="{escape(value)}"')
    if placeholder:
        field_attrs.append(f'placeholder="{escape(placeholder)}"')
    if required:
        field_attrs.append("required")
    if disabled:
        field_attrs.append("disabled")
    if attrs:
        field_attrs.append(attrs)

    parts = []
    if label:
        parts.append(f'<label class="cv-field__label" for="{escape(name)}">{escape(label)}</label>')
    parts.append(f'<input class="{" ".join(classes)}" {" ".join(field_attrs)} />')
    if error:
        parts.append(f'<div class="cv-field__error">{escape(error)}</div>')
    elif hint:
        parts.append(f'<div class="cv-field__hint">{escape(hint)}</div>')
    return f'<div class="cv-field">{"".join(parts)}</div>'


def select_field(
    name: str,
    options: Sequence[tuple[str, str]],
    *,
    label: str = "",
    value: str = "",
    error: str = "",
    disabled: bool = False,
) -> str:
    """下拉框 —— ``options`` 是 ``(值, 显示文字)`` 序列。"""
    classes = ["cv-select"]
    if error:
        classes.append("has-error")
    opts = []
    for opt_value, opt_label in options:
        selected = " selected" if str(opt_value) == str(value) else ""
        opts.append(
            f'<option value="{escape(opt_value)}"{selected}>{escape(opt_label)}</option>'
        )
    attrs = [f'name="{escape(name)}"']
    if disabled:
        attrs.append("disabled")
    parts = []
    if label:
        parts.append(f'<label class="cv-field__label" for="{escape(name)}">{escape(label)}</label>')
    parts.append(f'<select class="{" ".join(classes)}" {" ".join(attrs)}>{"".join(opts)}</select>')
    if error:
        parts.append(f'<div class="cv-field__error">{escape(error)}</div>')
    return f'<div class="cv-field">{"".join(parts)}</div>'


# ---------------------------------------------------------------------------
# 状态徽标 / 出处角标
# ---------------------------------------------------------------------------


def status_badge(status: str, *, label: str = "") -> str:
    """状态徽标 —— **带中文文字**，不靠颜色单独区分。

    颜色由状态机语义角色决定（``tokens.STATUS_SEMANTIC``），文字取状态机
    自己的标签（``states.describe_status``）—— 两边不会各说一套。
    """
    text = label or describe_status(status)
    return (
        f'<span class="cv-badge {badge_class(status)}">'
        f'<span class="cv-badge__dot"></span>{escape(text)}</span>'
    )


def citation_chip(
    file_name: str,
    line_no: Any = "",
    *,
    href: str = "",
    title: str = "",
    sha256: str = "",
) -> str:
    """出处角标 —— 文件名 + 行号（依据能点回原文位置）。

    行号是**可读行号**（人能在 Excel 里找到的那一行），不是数据库主键。
    """
    inner = f'<span class="cv-cite__file">{escape(file_name)}</span>'
    if line_no not in ("", None):
        inner += f'<span class="cv-cite__line">{escape(line_no)}</span>'
    tip = title
    if not tip and sha256:
        tip = f"材料指纹 {str(sha256)[:8]}"
    attrs = f' title="{escape(tip)}"' if tip else ""
    if href:
        return f'<a class="cv-cite" href="{escape(href)}"{attrs}>{inner}</a>'
    return f'<span class="cv-cite"{attrs}>{inner}</span>'


# ---------------------------------------------------------------------------
# 表格
# ---------------------------------------------------------------------------


def table(
    columns: Sequence[Mapping[str, Any]],
    rows: Iterable[Mapping[str, Any]],
    *,
    loading: bool = False,
    skeleton_rows: int = 5,
    empty_title: str = "暂无数据",
    empty_desc: str = "",
    empty_action: str = "",
    sort_key: str = "",
    sort_desc: bool = False,
    row_class: Optional[Any] = None,
    caption: str = "",
) -> str:
    """数据表格 —— 排序表头 / 空态 / 加载骨架三态齐备。

    * ``columns`` 每项是 ``{"key", "label", "sortable", "align", "cell"}``；
      ``cell`` 可给一个渲染函数（``cell(row) -> str``），不给就取
      ``row[key]`` 的文本。
    * ``loading=True`` → 渲染骨架行（灰底占位条，非转圈动画库）。
    * ``rows`` 为空且不在加载 → 渲染空态（图标 + 说明 + 行动入口）。
    """
    column_list = list(columns or [])
    row_list = list(rows or [])

    head_cells = []
    for column in column_list:
        label = str(column.get("label", ""))
        key = str(column.get("key", ""))
        align = str(column.get("align", ""))
        sortable = bool(column.get("sortable"))
        classes = []
        if align == "right":
            classes.append("num")
        if sortable:
            classes.append("is-sortable")
        class_attr = f' class="{" ".join(classes)}"' if classes else ""
        if sortable:
            active = sort_key == key
            arrow = "sort-desc" if (active and sort_desc) else "sort-asc"
            glyph = icon(arrow, class_name="cv-sort")
            mark = " ↓" if (active and sort_desc) else (" ↑" if active else "")
            direction = "&amp;dir=desc" if (active and not sort_desc) else ""
            href = f"?sort={escape(key)}{direction}"
            head_cells.append(
                f'<th{class_attr}><a href="{href}">'
                f"{escape(label)}{mark}{glyph}</a></th>"
            )
        else:
            head_cells.append(f"<th{class_attr}>{escape(label)}</th>")

    if loading:
        skeleton_cells = []
        for column in column_list:
            num_class = ' class="num"' if str(column.get("align", "")) == "right" else ""
            skeleton_cells.append(
                f"<td{num_class}><span class=\"cv-skeleton cv-skeleton--w60\"></span></td>"
            )
        one_row = "".join(skeleton_cells)
        body = "".join(
            f"<tr>{one_row}</tr>" for _ in range(max(1, int(skeleton_rows)))
        )
    elif not row_list:
        action_html = f'<div class="cv-empty__action">{empty_action}</div>' if empty_action else ""
        body = (
            "<tbody><tr><td colspan=\"%d\">"
            '<div class="cv-empty">%s<div class="cv-empty__title">%s</div>'
            '<div class="cv-empty__desc">%s</div>%s</div>'
            "</td></tr></tbody>"
            % (
                max(1, len(column_list)),
                icon("inbox", class_name="cv-empty__icon"),
                escape(empty_title),
                escape(empty_desc),
                action_html,
            )
        )
        return (
            f'<div class="cv-table-wrap"><table class="cv-table">'
            f'<thead><tr>{"".join(head_cells)}</tr></thead>{body}</table></div>'
        )
    else:
        body_rows = []
        for row in row_list:
            cells = []
            for column in column_list:
                key = str(column.get("key", ""))
                align = str(column.get("align", ""))
                cell_fn = column.get("cell")
                if callable(cell_fn):
                    content = str(cell_fn(row) or "")
                else:
                    content = escape(row.get(key, ""))
                classes = []
                if align == "right":
                    classes.append("num")
                class_attr = f' class="{" ".join(classes)}"' if classes else ""
                label_attr = f' data-label="{escape(column.get("label", ""))}"'
                cells.append(f"<td{class_attr}{label_attr}>{content}</td>")
            extra_class = ""
            if callable(row_class):
                extra_class = str(row_class(row) or "")
            body_rows.append(f'<tr class="{escape(extra_class)}">{"".join(cells)}</tr>')
        body = f'<tbody>{"".join(body_rows)}</tbody>'

    caption_html = f'<caption class="visually-hidden">{escape(caption)}</caption>' if caption else ""
    return (
        f'<div class="cv-table-wrap"><table class="cv-table">{caption_html}'
        f'<thead><tr>{"".join(head_cells)}</tr></thead>{body}</table></div>'
    )


# ---------------------------------------------------------------------------
# 提示条 / 空态 / toast / 统计条 / 页头
# ---------------------------------------------------------------------------


def notice(message: str, *, kind: str = "info", action: str = "") -> str:
    """页面级提示条（成功 / 警告 / 错误 / 提示）。"""
    kind = kind if kind in {"ok", "warn", "err", "info"} else "info"
    icon_name = {"ok": "check-circle", "warn": "alert", "err": "alert", "info": "info"}[kind]
    action_html = f'<div style="margin-left:auto">{action}</div>' if action else ""
    return (
        f'<div class="cv-notice cv-notice--{kind}">'
        f'{icon(icon_name, class_name="cv-notice__icon")}'
        f'<div>{escape(message)}</div>{action_html}</div>'
    )


def empty_state(title: str, *, desc: str = "", action: str = "", icon_name: str = "inbox") -> str:
    """空态 —— 图标 + 说明 + 行动入口。"""
    action_html = f'<div class="cv-empty__action">{action}</div>' if action else ""
    return (
        f'<div class="cv-empty">{icon(icon_name, class_name="cv-empty__icon")}'
        f'<div class="cv-empty__title">{escape(title)}</div>'
        f'<div class="cv-empty__desc">{escape(desc)}</div>{action_html}</div>'
    )


def toast(
    message: str,
    *,
    kind: str = "info",
    dismissible: bool = False,
    data_kind: str = "",
) -> str:
    """toast 片段（写操作反馈）。

    **只渲染片段，不自带脚本** —— 值守台页面是服务端渲染的，反馈由
    浏览器端极小的脚本（``templates/console.js``）接管；本函数保证
    「有 toast 样式与语义色」。

    ``kind`` 五档：``ok`` / ``warn`` / ``err`` / ``info`` / ``failed``。
    ``failed``（失败深灰）与 ``err``（危急红）**刻意分开** —— 通道坏了
    是「坏了」，不是「病人要死了」，红色要留给临床上的危急值。

    ``dismissible=True`` 时带一个关闭按钮（写操作反馈要能被扫掉）；
    ``data_kind`` 把反馈类型标在 DOM 上（脚本按它决定自动消失的时长，
    单测也按它断言「四类反馈各有各的语义色」）。
    """
    kind = kind if kind in TOAST_KINDS else "info"
    icon_name = {
        "ok": "check-circle",
        "warn": "alert",
        "err": "alert",
        "info": "info",
        "failed": "alert",
    }[kind]
    close_html = ""
    if dismissible:
        close_html = (
            '<button class="cv-toast__close" type="button" data-cv-toast-close '
            'aria-label="关闭提示">'
            f'{icon("x", class_name="cv-toast__close-icon")}</button>'
        )
    data_attr = f' data-cv-feedback="{escape(data_kind)}"' if data_kind else ""
    return (
        f'<div class="cv-toast cv-toast--{kind}"{data_attr} role="status">'
        f'{icon(icon_name, class_name="cv-toast__icon")}'
        f'<div class="cv-toast__body">{escape(message)}</div>{close_html}</div>'
    )


def statbar(items: Sequence[Mapping[str, Any]]) -> str:
    """统计条 —— 今日 / 待确认 / 超时 / 回写失败 这类一眼看的计数。

    每项可带 ``href`` —— 带了就渲染成**可点的**统计格（点下去跳到对应的
    筛选视图），不带就是纯计数。值班人看一眼概览就能直接钻取，不必先在
    筛选条里翻找。
    """
    parts = []
    for item in items or []:
        value = str(item.get("value", ""))
        label = str(item.get("label", ""))
        role = str(item.get("role", ""))
        href = str(item.get("href", "") or "")
        title = str(item.get("title", "") or "")
        cls = f'cv-statbar__item cv-statbar__item--{role}' if role else "cv-statbar__item"
        inner = (
            f'{escape(label)}<span class="cv-statbar__value">{escape(value)}</span>'
        )
        attrs = f' title="{escape(title)}"' if title else ""
        if href:
            parts.append(f'<a class="{cls} is-link" href="{escape(href)}"{attrs}>{inner}</a>')
        else:
            parts.append(f'<span class="{cls}"{attrs}>{inner}</span>')
    if not parts:
        return ""
    return f'<div class="cv-statbar">{"".join(parts)}</div>'


def page_head(
    heading: str,
    *,
    subtitle: str = "",
    actions: str = "",
) -> str:
    """页头（标题 + 一句说明 + 主操作区）。"""
    action_html = f'<div class="cv-page-head__actions">{actions}</div>' if actions else ""
    return (
        '<div class="cv-page-head"><div class="cv-page-head__text">'
        f"<h1>{escape(heading)}</h1>"
        + (f"<p>{escape(subtitle)}</p>" if subtitle else "")
        + f"</div>{action_html}</div>"
    )
