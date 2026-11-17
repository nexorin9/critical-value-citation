"""值班台设计 token（任务 id 48）。

**这一层解决什么问题**

值班台是**夜班**用的：值班人盯着它盯一整夜，眼睛累、屏幕偏色、还要在
「已推送待回执」和「已回写」之间一秒分辨出差别。所以本模块只干一件事 ——
把「颜色 / 间距 / 字号 / 圆角 / 阴影」这些**本来会散落在各处硬编码**的
值，收敛成一份**单一真源**：

* 业务代码（``components.py`` / ``pages.py``）只引用 token 名，不写死
  ``#D93025`` 这样的字面量；
* ``templates/styles.css`` 的 ``:root`` 块是这些 token 的**样式侧落地**，
  由 ``tests/test_console_tokens.py`` 做**双向逐值比对** —— 两侧漂了就红。

**为什么语义色必须与状态机一一对应**

状态机（``critval/ledger/states.py``）的 13 个状态是值班台上一眼要看懂的
**事实分类**：谁在等我、谁已经完了、谁坏了。颜色如果只由「好看」决定，
就会出现「红的不一定是急的」这种事 —— 夜班场景下这是事故。所以这里把
**13 个状态 → 6 个语义角色**的映射写死成一张表（``STATUS_SEMANTIC``），
并由单测锁住：状态机新增一个状态而这里没跟上，测试直接失败。

**与源参考的关系**：源产品的值班台交互（``github_ref/RAGFlow/web/src/
components/api-service/embed-modal/index.tsx``）给出的是「处置卡可嵌入既有
页面、按钮带 loading 态、结果列表可选中计数」这套**交互骨架**。本产品是
**服务端模板 + 原生 CSS**（不引前端构建链 —— 检验科值班机上装个 Python 就
能跑），所以这里不复用它的组件代码，只沿用它的交互纪律：**写操作要有
loading 态、列表要有计数、失败要留可见痕迹**。

**零外部依赖**：本模块不 import 任何第三方库，也不读任何配置 —— 设计 token
是产品观感的一部分，不该随环境变量漂移。
"""

from __future__ import annotations

from pathlib import Path
from typing import Mapping

from critval.ledger.states import ALL_STATUSES

__all__ = [
    "BASE_COLORS",
    "BASE_TEMPLATE_FILENAME",
    "DENSITY",
    "FEEDBACK_SEMANTIC",
    "FONT_FAMILY",
    "FONT_SIZE",
    "RADIUS",
    "SEMANTIC",
    "SEMANTIC_LABELS",
    "SHADOW",
    "SPACING",
    "STATUS_SEMANTIC",
    "STYLESHEET_FILENAME",
    "TEMPLATES_DIR",
    "TOAST_KIND_BY_ROLE",
    "badge_class",
    "base_template_path",
    "css_root_block",
    "css_variables",
    "semantic_for_status",
    "stylesheet_path",
]


# ---------------------------------------------------------------------------
# 模板与静态资源位置
# ---------------------------------------------------------------------------

#: 值班台模板目录（服务端模板 + 静态样式都在这里）。
TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

#: 静态样式文件名（由 ``/static/styles.css`` 路由直接吐出去）。
STYLESHEET_FILENAME = "styles.css"

#: 页面骨架模板文件名。
BASE_TEMPLATE_FILENAME = "base.html"


def stylesheet_path() -> Path:
    """静态样式文件路径。"""
    return TEMPLATES_DIR / STYLESHEET_FILENAME


def base_template_path() -> Path:
    """页面骨架模板路径。"""
    return TEMPLATES_DIR / BASE_TEMPLATE_FILENAME


# ---------------------------------------------------------------------------
# 颜色
# ---------------------------------------------------------------------------

#: 品牌与中性色（不承载业务语义，只承载界面骨架）。
#:
#: 取值与 ``docs/concepts/visual-goal.md`` 的视觉目标卡一致 —— 目标卡是
#: 「想做成什么样」，token 是「这次真的做成什么样」，两者不该各说一套。
BASE_COLORS: dict[str, str] = {
    # 主色（信息蓝）—— 主按钮、链接、聚焦环。
    "brand": "#1F5EFF",
    # 主色的交互态（hover / active）—— 按钮四态要用，不能在样式里现编。
    "brand-hover": "#1A4FE0",
    "brand-active": "#1745C4",
    # 中性灰阶
    "surface": "#FFFFFF",
    "canvas": "#F5F7FA",
    "subtle": "#EEF1F5",
    "border": "#E4E7EC",
    "border-strong": "#D5D8DC",
    "text": "#1F2329",
    "text-muted": "#5F6368",
    "text-faint": "#8A9099",
    # 焦点环（无障碍键盘焦点）
    "focus": "#C3D2FF",
}

#: 语义色板 —— 每个角色三件套：**前景色 / 底色 / 描边色**。
#:
#: 三件套而不是单一色，是为了在**低亮度大屏**与**色觉差异**下仍然可读：
#: 底色区分「哪一列」，前景色保证文字对比度，描边把徽标从卡片里拔出来。
SEMANTIC: dict[str, dict[str, str]] = {
    # 危急红 —— 待确认（要人马上动的事）。
    "critical": {"fg": "#D93025", "bg": "#FDEEED", "border": "#F5C6C3"},
    # 待确认琥珀 —— 待回写 / 转人工（等了但还没坏）。
    "pending": {"fg": "#E8890C", "bg": "#FEF6E9", "border": "#F7D9A8"},
    # 已完成绿 —— 已登记 / 已归档（事实已生效）。
    "done": {"fg": "#1E8E3E", "bg": "#EAF6ED", "border": "#B8DFC2"},
    # 信息蓝 —— 判定 / 出卡等处理中，以及口径分歧提示。
    "info": {"fg": "#1F5EFF", "bg": "#EDF2FF", "border": "#C3D2FF"},
    # 失败深灰 —— 推送失败 / 回写失败（坏了，但不是危急；危急留给临床）。
    "failed": {"fg": "#5F6368", "bg": "#F0F1F3", "border": "#D5D8DC"},
    # 中性灰 —— 不危急 / 已被取代（不打扰值班人的注意力）。
    "neutral": {"fg": "#5F6368", "bg": "#F5F7FA", "border": "#E4E7EC"},
}

#: 语义角色的中文名（写文档与调试输出用）。
SEMANTIC_LABELS: dict[str, str] = {
    "critical": "危急红",
    "pending": "待确认琥珀",
    "done": "已完成绿",
    "info": "信息蓝",
    "failed": "失败深灰",
    "neutral": "中性灰",
}


# ---------------------------------------------------------------------------
# 状态 → 语义色（与业务状态机一一对应）
# ---------------------------------------------------------------------------

#: **13 个业务状态 → 6 个语义角色**。
#:
#: 状态集合直接取自 ``critval.ledger.states.ALL_STATUSES``（状态机是唯一
#: 真源）—— 本表若漏一个状态，``tests/test_console_tokens.py`` 会失败。
#:
#: 分色理由（夜班一眼可辨）：
#:
#: * **红（critical）** —— 已推送、已推送待回执：**人在等我**，不点确认就要
#:   出事；
#: * **琥珀（pending）** —— 已确认（待回写）、转人工：**人已经接了手**，
#:   现在等系统或等下一位人；
#: * **绿（done）** —— 已回写、已归档：**事实已生效**，不用再盯；
#: * **蓝（info）** —— 已判定、已出卡：机器还在跑，不用人管；
#: * **深灰（failed）** —— 推送失败、回写失败：**坏了但不是危急**，进
#:   死信队列等人处理，不用打断夜班；
#: * **中性灰（neutral）** —— 已接收、不危急、已被取代：不值得占注意力。
STATUS_SEMANTIC: dict[str, str] = {
    # ---- 7 态主链 -----------------------------------------------------
    "received": "neutral",       # 刚收下，还没判定
    "judged": "info",            # 判定完成，待出卡
    "card_built": "info",        # 卡已装好，待推送
    "notified": "critical",      # 已推到值班人，待确认 —— 人在等我
    "acknowledged": "pending",   # 人已确认，待回写登记系统
    "registered": "done",        # 已回写 —— 已生效事实
    "archived": "done",          # 事实定格
    # ---- 6 个分支态 ---------------------------------------------------
    "not_critical": "neutral",   # 不危急 —— 不通知
    "pending_ack": "critical",   # 已推未确认，到期要复查重推
    "failed": "failed",          # 推送重试耗尽
    "writeback_failed": "failed",  # 回写失败（已确认的回执不丢）
    "superseded": "neutral",     # 被修正结果取代（终态）
    "transferred_to_human": "pending",  # 未命中依据 / 无法判定 —— 等人接
}


def semantic_for_status(status: str) -> str:
    """业务状态 → 语义角色名。

    未知状态落到 ``neutral``（**不抛错**）—— 值班台页面不该因为一个没见过的
    状态字符串就打不开；未知状态本身会由状态机在写入侧拦住。
    """
    return STATUS_SEMANTIC.get(str(status or "").strip(), "neutral")


def badge_class(status: str) -> str:
    """业务状态 → 徽标 class（``cv-badge--critical`` 之类）。"""
    return f"cv-badge--{semantic_for_status(status)}"


# ---------------------------------------------------------------------------
# 写操作反馈（任务 id 50）
# ---------------------------------------------------------------------------

#: **写操作反馈类型 → 语义角色**。
#:
#: 四类反馈是值班人做完写操作后**唯一**会看到的提示，各自有固定语义：
#:
#: * ``ack_ok`` —— 确认成功（绿：事实已生效）；
#: * ``notify_failed`` —— 推送失败（深灰：坏了但不是危急，进死信队列）；
#: * ``writeback_failed`` —— 回写失败（深灰：已确认的回执不丢，等补写）；
#: * ``transferred`` —— 转人工（琥珀：人已经接了手）。
#:
#: 复用任务 id 48 的六个语义角色，**不另开一套颜色** —— 值班人看颜色
#: 就能预判「这件事急不急」。
FEEDBACK_SEMANTIC: dict[str, str] = {
    "ack_ok": "done",
    "notify_failed": "failed",
    "writeback_failed": "failed",
    "transferred": "pending",
}

#: 语义角色 → toast 样式键。
#:
#: ``failed``（失败深灰）与 ``critical``（危急红）**刻意分开**：危急红留给
#: 临床上的危急值，通道坏了是「坏了」，不是「病人要死了」。
TOAST_KIND_BY_ROLE: dict[str, str] = {
    "done": "ok",
    "pending": "warn",
    "critical": "err",
    "failed": "failed",
    "info": "info",
    "neutral": "info",
}


# ---------------------------------------------------------------------------
# 间距 / 字号 / 字体 / 圆角 / 阴影
# ---------------------------------------------------------------------------

#: 8px 基准间距栅格（键名是「几格」）。
SPACING: dict[str, str] = {
    "1": "4px",
    "2": "8px",
    "3": "12px",
    "4": "16px",
    "6": "24px",
    "8": "32px",
}

#: 字号层级（4 级：页头 / 区块标题 / 正文 / 辅助文字）。
FONT_SIZE: dict[str, str] = {
    "page": "20px",
    "section": "16px",
    "body": "14px",
    "aux": "12px",
}

#: 字体族（中文系统字体栈 + 等宽字体栈）。
FONT_FAMILY: dict[str, str] = {
    "sans": '-apple-system, BlinkMacSystemFont, "PingFang SC", "Microsoft YaHei", '
    '"Segoe UI", "Helvetica Neue", Arial, sans-serif',
    "mono": 'ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace',
}

#: 圆角（卡片 / 徽标 / 输入框三级）。
RADIUS: dict[str, str] = {
    "card": "8px",
    "badge": "4px",
    "input": "6px",
}

#: 阴影（卡片 / 浮层 / 模态三级）。
SHADOW: dict[str, str] = {
    "card": "0 1px 2px rgba(31, 35, 41, 0.06)",
    "float": "0 4px 16px rgba(31, 35, 41, 0.12)",
    "modal": "0 8px 32px rgba(31, 35, 41, 0.18)",
}

#: 中后台密度（表格行高 / 卡片内边距 / 区块间距 / 顶栏高度）。
DENSITY: dict[str, str] = {
    "row_height": "40px",
    "card_padding": "16px",
    "section_gap": "24px",
    # 顶栏实测高度约 50px（品牌行高 + 上下内边距），取 52px 留一点余量 ——
    # toast 挂在这个高度之下，就不会压在顶栏上。
    "topbar_height": "52px",
}


# ---------------------------------------------------------------------------
# CSS 变量
# ---------------------------------------------------------------------------


def css_variables() -> dict[str, str]:
    """全部设计 token → CSS 自定义属性（``--cv-*``）。

    **本函数是「token → 样式」的唯一转换点**：``styles.css`` 的 ``:root``
    块必须与这里的返回值**逐名逐值一致**，由单测双向比对守住。
    """
    variables: dict[str, str] = {}
    for name, value in BASE_COLORS.items():
        variables[f"--cv-color-{name}"] = value
    for role, trio in SEMANTIC.items():
        for slot, value in trio.items():
            variables[f"--cv-semantic-{role}-{slot}"] = value
    for name, value in SPACING.items():
        variables[f"--cv-space-{name}"] = value
    for name, value in FONT_SIZE.items():
        variables[f"--cv-font-size-{name}"] = value
    for name, value in FONT_FAMILY.items():
        variables[f"--cv-font-family-{name}"] = value
    for name, value in RADIUS.items():
        variables[f"--cv-radius-{name}"] = value
    for name, value in SHADOW.items():
        variables[f"--cv-shadow-{name}"] = value
    for name, value in DENSITY.items():
        variables[f"--cv-density-{name}"] = value
    return variables


def css_root_block() -> str:
    """``:root`` 声明块（token 的样式侧文本形态）。"""
    lines = [":root {"]
    for name, value in css_variables().items():
        lines.append(f"  {name}: {value};")
    lines.append("}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 自检（供单测直接引用，避免测试里再抄一份常量）
# ---------------------------------------------------------------------------


def uncovered_statuses() -> list[str]:
    """状态机里有、但本模块**没配语义色**的状态（应为空）。"""
    return sorted(ALL_STATUSES - set(STATUS_SEMANTIC))


def unused_semantic_roles() -> list[str]:
    """定义了、但**没有任何状态用**的语义角色（应为空）。"""
    used = set(STATUS_SEMANTIC.values())
    return sorted(set(SEMANTIC) - used)


def token_snapshot() -> Mapping[str, object]:
    """token 全量快照（给 CLI / 调试输出用，也是单测的比对基准）。"""
    return {
        "base_colors": dict(BASE_COLORS),
        "semantic": {role: dict(trio) for role, trio in SEMANTIC.items()},
        "spacing": dict(SPACING),
        "font_size": dict(FONT_SIZE),
        "font_family": dict(FONT_FAMILY),
        "radius": dict(RADIUS),
        "shadow": dict(SHADOW),
        "density": dict(DENSITY),
        "status_semantic": dict(STATUS_SEMANTIC),
        "feedback_semantic": dict(FEEDBACK_SEMANTIC),
        "toast_kind_by_role": dict(TOAST_KIND_BY_ROLE),
    }
