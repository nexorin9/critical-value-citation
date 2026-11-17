"""值班台设计 token 与基础组件（任务 id 48）。

**这一组用例守的是什么**

1. **token 齐备** —— 色板 / 语义色 / 栅格 / 字号 / 圆角 / 阴影都有确定值，
   且颜色都是合法的 ``#RRGGBB``（不写 ``red`` / ``rgb()`` 这类难核对的写法）；
2. **语义色与状态机一一对应** —— 状态机（``critval.ledger.states``）的
   13 个状态**每个**都有语义色，且每个语义色**都被用到**（没有死色）。
   状态机以后加了新状态而这里没跟上，本组用例会红；
3. **CSS 变量齐备** —— ``tokens.css_variables()`` 与
   ``templates/styles.css`` 的 ``:root`` 块**双向逐名逐值一致**（不只看
   样式表有没有写，也要看样式表有没有多写）；
4. **组件可渲染** —— 按钮四态、输入框、表格三态、状态徽标、出处角标都能
   渲染出可用的 HTML，且外来文本被转义。
"""

from __future__ import annotations

import re

import pytest

from critval.console import components as comp
from critval.console import pages
from critval.console.tokens import (
    BASE_COLORS,
    DENSITY,
    FONT_FAMILY,
    FONT_SIZE,
    RADIUS,
    SEMANTIC,
    SEMANTIC_LABELS,
    SHADOW,
    SPACING,
    STATUS_SEMANTIC,
    base_template_path,
    css_root_block,
    css_variables,
    semantic_for_status,
    stylesheet_path,
)
from critval.ledger.states import ALL_STATUSES, BRANCH_STATUSES, MAIN_CHAIN

HEX = re.compile(r"^#[0-9A-Fa-f]{6}$")


# ---------------------------------------------------------------------------
# token 常量齐备
# ---------------------------------------------------------------------------


def test_色板常量齐备() -> None:
    """基础色 + 中性灰阶都有确定值，且都是合法六位十六进制。"""
    assert BASE_COLORS["brand"] == "#1F5EFF"
    for key in ("surface", "canvas", "border", "text", "text-muted", "focus"):
        assert key in BASE_COLORS, f"基础色缺少 {key}"
    for key, value in BASE_COLORS.items():
        assert HEX.match(value), f"基础色 {key} 不是合法六位十六进制：{value}"


def test_语义色板每个角色三件套且取自灰阶体系() -> None:
    """语义色 = 前景 / 底 / 描边 三件套，缺一件就用不了。"""
    for role in ("critical", "pending", "done", "info", "failed", "neutral"):
        assert role in SEMANTIC, f"语义色缺少 {role}"
        trio = SEMANTIC[role]
        assert set(trio) == {"fg", "bg", "border"}, f"{role} 的三件套不齐：{trio}"
        for slot, value in trio.items():
            assert HEX.match(value), f"{role}.{slot} 不是合法六位十六进制：{value}"


def test_语义角色都有中文名() -> None:
    """每个语义角色都有人话名（写文档与调试输出用）。"""
    assert set(SEMANTIC_LABELS) == set(SEMANTIC)
    assert SEMANTIC_LABELS["critical"] == "危急红"
    assert SEMANTIC_LABELS["pending"] == "待确认琥珀"
    assert SEMANTIC_LABELS["done"] == "已完成绿"


def test_间距是8px栅格() -> None:
    """间距取值必须是 4 的倍数且落在 4/8/12/16/24/32 这一档上。"""
    assert sorted(SPACING.values(), key=lambda v: int(v[:-2])) == [
        "4px",
        "8px",
        "12px",
        "16px",
        "24px",
        "32px",
    ]
    for value in SPACING.values():
        px = int(value[:-2])
        assert px % 4 == 0, f"间距 {value} 不在 4px 栅格上"


def test_字号层级四级且与视觉目标卡一致() -> None:
    """页头 20 / 区块 16 / 正文 14 / 辅助 12 —— 四级，缺一级就分不出层级。"""
    assert FONT_SIZE == {"page": "20px", "section": "16px", "body": "14px", "aux": "12px"}
    assert len(set(FONT_SIZE.values())) == 4


def test_字体族含中文系统栈与等宽栈() -> None:
    """中文系统字体栈 + 等宽字体栈（文件名 / 行号 / sha256 要等宽）。"""
    assert "PingFang SC" in FONT_FAMILY["sans"]
    assert "Microsoft YaHei" in FONT_FAMILY["sans"]
    assert "monospace" in FONT_FAMILY["mono"]


def test_圆角与阴影三级() -> None:
    """卡片 / 徽标 / 输入框三级圆角；卡片 / 浮层 / 模态三级阴影。"""
    assert set(RADIUS) == {"card", "badge", "input"}
    assert set(SHADOW) == {"card", "float", "modal"}
    # 密度：表格行高 / 卡片内边距 / 区块间距 / 顶栏高度（toast 挂在顶栏之下）
    assert set(DENSITY) == {
        "row_height",
        "card_padding",
        "section_gap",
        "topbar_height",
    }


# ---------------------------------------------------------------------------
# 语义色 ↔ 状态机 一一对应
# ---------------------------------------------------------------------------


def test_状态机的每个状态都有语义色() -> None:
    """状态机新增状态而本表没跟上 → 这里失败。"""
    missing = sorted(ALL_STATUSES - set(STATUS_SEMANTIC))
    assert missing == [], f"这些状态还没有语义色：{missing}"


def test_语义色表没有多余状态() -> None:
    """本表里的状态都必须在状态机里（打错的状态名不能混进来）。"""
    extra = sorted(set(STATUS_SEMANTIC) - ALL_STATUSES)
    assert extra == [], f"这些状态不在状态机里：{extra}"


def test_每个语义角色都被至少一个状态使用() -> None:
    """没有死色 —— 定义了却没人用的语义色说明映射写歪了。"""
    used = set(STATUS_SEMANTIC.values())
    unused = sorted(set(SEMANTIC) - used)
    assert unused == [], f"这些语义色没有任何状态在用：{unused}"


def test_主链与分支态都覆盖到() -> None:
    """7 态主链 + 6 分支态，一个都不能漏。"""
    assert len(MAIN_CHAIN) == 7
    assert len(BRANCH_STATUSES) == 6
    assert len(ALL_STATUSES) == 13
    for status in set(MAIN_CHAIN) | BRANCH_STATUSES:
        assert status in STATUS_SEMANTIC


def test_待确认类状态用危急红() -> None:
    """已推送 / 已推送待回执 = 人在等我 → 危急红。"""
    assert STATUS_SEMANTIC["notified"] == "critical"
    assert STATUS_SEMANTIC["pending_ack"] == "critical"


def test_待回写与转人工用琥珀() -> None:
    """已确认（待回写）/ 转人工 = 人接了手但还没完 → 琥珀。"""
    assert STATUS_SEMANTIC["acknowledged"] == "pending"
    assert STATUS_SEMANTIC["transferred_to_human"] == "pending"


def test_已生效事实用绿() -> None:
    """已回写 / 已归档 = 事实定格 → 绿。"""
    assert STATUS_SEMANTIC["registered"] == "done"
    assert STATUS_SEMANTIC["archived"] == "done"


def test_失败用深灰不用红() -> None:
    """推送失败 / 回写失败 = 坏了但不是危急；红色要留给临床。"""
    assert STATUS_SEMANTIC["failed"] == "failed"
    assert STATUS_SEMANTIC["writeback_failed"] == "failed"


def test_未知状态落到中性灰而不是抛错() -> None:
    """页面不该因为一个没见过的状态字符串就打不开。"""
    assert semantic_for_status("not-a-status") == "neutral"
    assert semantic_for_status("") == "neutral"


# ---------------------------------------------------------------------------
# CSS 变量齐备（双向逐值比对）
# ---------------------------------------------------------------------------


def _root_declarations(css_text: str) -> dict[str, str]:
    """从样式表里抠出 ``:root`` 块的自定义属性。"""
    match = re.search(r":root\s*\{(.*?)\}", css_text, re.S)
    assert match, "样式表里找不到 :root 块"
    declarations: dict[str, str] = {}
    for line in match.group(1).splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        name, _, value = line.partition(":")
        declarations[name.strip()] = value.strip().rstrip(";").strip()
    return declarations


def test_css变量齐备且与样式表逐值一致() -> None:
    """token 与 ``styles.css`` 的 ``:root`` 块必须双向一致。"""
    assert stylesheet_path().exists(), f"样式表不存在：{stylesheet_path()}"
    declared = _root_declarations(stylesheet_path().read_text(encoding="utf-8"))
    expected = css_variables()

    missing = sorted(set(expected) - set(declared))
    assert missing == [], f"样式表 :root 缺少这些变量：{missing}"

    extra = sorted(set(declared) - set(expected))
    assert extra == [], f"样式表 :root 多出这些变量（token 里没有）：{extra}"

    drifted = {
        name: (expected[name], declared[name])
        for name in expected
        if expected[name] != declared[name]
    }
    assert drifted == {}, f"这些变量的值对不上：{drifted}"


def test_css_root_block与变量表一致() -> None:
    """``css_root_block()``（生成侧）与样式表里的 ``:root`` 同源。"""
    block = css_root_block()
    declared = _root_declarations(block)
    assert declared == css_variables()


def test_样式表在root之外不再出现字面量颜色() -> None:
    """``:root`` 之外不许硬编码颜色 —— 否则 token 就不是唯一真源了。"""
    css_text = stylesheet_path().read_text(encoding="utf-8")
    root_match = re.search(r":root\s*\{(.*?)\}", css_text, re.S)
    assert root_match
    outside = css_text.replace(root_match.group(0), "")
    literals = re.findall(r"#[0-9A-Fa-f]{3,8}\b", outside)
    assert literals == [], f"样式表 :root 之外出现字面量颜色：{sorted(set(literals))}"


def test_样式表不出现emoji() -> None:
    """图标一律内联 SVG —— emoji 在值班大屏上分辨率不可控。"""
    css_text = stylesheet_path().read_text(encoding="utf-8")
    emoji = [ch for ch in css_text if ord(ch) > 0x1F000]
    assert emoji == [], f"样式表里出现 emoji：{emoji}"


# ---------------------------------------------------------------------------
# 组件可渲染
# ---------------------------------------------------------------------------


def test_按钮四态都能渲染() -> None:
    """主 / 次 / 危险三变体 × 默认 / 禁用 / loading 三状态。"""
    for variant in ("primary", "secondary", "danger"):
        default = comp.button("确认处置", variant=variant)
        assert f"cv-btn--{variant}" in default
        assert "确认处置" in default
        assert "disabled" not in default

        disabled = comp.button("确认处置", variant=variant, state="disabled")
        assert "disabled" in disabled
        assert "cv-btn__label" in disabled

        loading = comp.button("确认处置", variant=variant, state="loading")
        assert "is-loading" in loading
        assert "disabled" in loading, "loading 态必须同时禁用，否则能重复点"
        assert "cv-spinner" in loading


def test_按钮文案被转义() -> None:
    """外来文本进按钮不能破坏 HTML。"""
    html = comp.button('<script>alert("x")</script>')
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_按钮图标是内联svg不是emoji() -> None:
    """带图标按钮输出 ``<svg>``。"""
    html = comp.button("确认", icon_name="check")
    assert "<svg" in html
    assert "cv-icon" in html


def test_输入框有标签与错误态() -> None:
    """输入框 = 标签 + 控件 + 错误提示（或辅助说明）。"""
    ok = comp.input_field("handler", label="确认人", value="张三")
    assert 'name="handler"' in ok
    assert "确认人" in ok
    assert "cv-field__error" not in ok

    bad = comp.input_field("handler", label="确认人", error="必须写确认人")
    assert "has-error" in bad
    assert "cv-field__error" in bad
    assert "必须写确认人" in bad


def test_表格三态齐备() -> None:
    """有数据 / 空态 / 加载骨架，三态都要有。"""
    columns = [{"key": "event_id", "label": "事件号"}, {"key": "count", "label": "数量", "align": "right"}]

    filled = comp.table(columns, [{"event_id": "evt-1", "count": 3}])
    assert "evt-1" in filled
    assert "cv-table" in filled

    empty = comp.table(columns, [], empty_title="当前没有待处置的危急值事件", empty_desc="等待 LIS 推送")
    assert "当前没有待处置的危急值事件" in empty
    assert "等待 LIS 推送" in empty
    assert "cv-empty__icon" in empty, "空态要有图标，不能只是一行字"

    loading = comp.table(columns, [], loading=True, skeleton_rows=3)
    assert loading.count("cv-skeleton") >= 3
    assert "cv-empty__title" not in loading, "加载态不能同时渲染空态"


def test_表格排序表头可点() -> None:
    """可排序列的表头是链接，当前排序列带箭头。"""
    columns = [{"key": "observed_at", "label": "观察时刻", "sortable": True}]
    html = comp.table(columns, [{"observed_at": "2026-10-30 08:00"}], sort_key="observed_at", sort_desc=True)
    assert "is-sortable" in html
    assert "?sort=observed_at" in html
    assert "cv-sort" in html


def test_表格数字列右对齐且带data_label() -> None:
    """数字列右对齐 + 等宽；响应式转卡片时靠 ``data-label`` 显示列名。"""
    columns = [{"key": "count", "label": "数量", "align": "right"}]
    html = comp.table(columns, [{"count": 7}])
    assert 'class="num"' in html
    assert 'data-label="数量"' in html


def test_状态徽标带文字且语义色正确() -> None:
    """徽标不靠颜色单独区分 —— 必须带中文文字。"""
    html = comp.status_badge("pending_ack")
    assert "已推送待回执" in html
    assert "cv-badge--critical" in html

    done = comp.status_badge("registered")
    assert "已回写" in done
    assert "cv-badge--done" in done


def test_徽标文字取自状态机() -> None:
    """徽标文字必须与状态机自己的标签一致，不能各说一套。"""
    from critval.ledger.states import describe_status

    for status in sorted(ALL_STATUSES):
        html = comp.status_badge(status)
        assert describe_status(status) in html


def test_出处角标带文件名与行号() -> None:
    """出处角标 = 文件名 + 可读行号（人能回原文找到那一行）。"""
    html = comp.citation_chip("示例危急值阈值表.xlsx", 2)
    assert "示例危急值阈值表.xlsx" in html
    assert "cv-cite__line" in html
    assert ">2<" in html

    linked = comp.citation_chip("示例危急值阈值表.xlsx", 2, href="/events/evt-1")
    assert 'href="/events/evt-1"' in linked


def test_toast与提示条按语义色渲染() -> None:
    """写操作反馈走站内 toast，禁用原生 alert。"""
    ok = comp.toast("回执已回写登记系统", kind="ok")
    assert "cv-toast--ok" in ok
    assert "回执已回写登记系统" in ok

    err = comp.notice("回写失败，请人工补写", kind="err")
    assert "cv-notice--err" in err
    assert "回写失败" in err


def test_统计条按角色着色() -> None:
    """统计条数字用等宽字，角色决定颜色。"""
    html = comp.statbar([{"label": "待确认", "value": 3, "role": "critical"}])
    assert "cv-statbar__value" in html
    assert "cv-statbar__item--critical" in html
    assert "待确认" in html


def test_图标是内联svg且未知图标回落() -> None:
    """图标走 SVG；拼错的名字回落到 info，而不是抛错。"""
    svg = comp.icon("list")
    assert svg.startswith("<svg")
    assert "stroke" not in svg  # 描边色由 CSS 的 currentColor 承担

    unknown = comp.icon("不存在的图标")
    assert unknown.startswith("<svg")
    assert unknown != svg


def test_模板替换不残留占位符() -> None:
    """``{{name}}`` 替换干净；未提供的占位符变空串。"""
    out = comp.render_template("<p>{{a}}·{{b}}</p>", {"a": "A"})
    assert out == "<p>A·</p>"
    assert "{{" not in out


# ---------------------------------------------------------------------------
# 页面骨架
# ---------------------------------------------------------------------------


def test_页面骨架模板存在且含页头导航页脚与toast容器() -> None:
    """``base.html`` 四个骨架位齐全。"""
    path = base_template_path()
    assert path.exists(), f"页面骨架模板不存在：{path}"
    text = path.read_text(encoding="utf-8")
    assert "cv-topbar" in text
    assert "cv-sidenav" in text
    assert "cv-footer" in text
    assert "cv-toast-host" in text
    assert "{{content}}" in text
    assert "{{nav}}" in text


def test_渲染出的页面不再内联style且引用静态样式() -> None:
    """页面里没有 ``<style>`` 块 —— 样式只走 ``/static/styles.css``。"""
    html = pages.render_layout("事件台账", "<p>内容</p>")
    assert "<style" not in html
    assert 'href="/static/styles.css"' in html
    assert "{{" not in html, "占位符没替换干净"


def test_页面骨架含侧边导航与页脚() -> None:
    """导航项指向真实路由；页脚有复核用的信息位。"""
    html = pages.render_layout("事件台账", "<p>内容</p>", active="events")
    assert "cv-sidenav" in html
    assert 'href="/events"' in html
    assert "is-active" in html
    assert "cv-footer" in html


def test_首页用组件库渲染且状态徽标带文字() -> None:
    """首页是「组件库 + 语义 token」的产物，不是手拼 HTML。"""

    class _Store:
        def status_counts(self):
            return {"notified": 3, "pending_ack": 2, "failed": 1}

    html = pages.render_home(_Store())
    assert "cv-statbar" in html
    assert "cv-table" in html
    assert "cv-badge--critical" in html
    assert "已推送，待值班人确认" in html
    assert 'href="/static/styles.css"' in html


def test_静态资源路由只放行白名单() -> None:
    """静态资源路由不能变成任意文件读取口。"""
    router = pages.ConsoleRouter(store=None)
    assert router.handles("/static/styles.css")

    response = router.handle("GET", "/static/styles.css")
    assert response.status == 200
    assert response.content_type.startswith("text/css")
    assert ":root" in response.body

    # 越权路径：路由接了，但白名单挡住（名字不在白名单里 → 404）
    blocked = router.handle("GET", "/static/../../task.json")
    assert blocked.status == 404
    assert "task.json" not in blocked.body

    assert router.handle("GET", "/static/base.html").status == 404


def test_样式表在响应里带CSS变量() -> None:
    """样式表真的带上了 token 变量（不是空文件）。"""
    response = pages.ConsoleRouter(store=None).handle("GET", "/static/styles.css")
    assert "--cv-semantic-critical-fg" in response.body
    assert "--cv-space-4" in response.body


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
