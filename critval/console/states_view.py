"""值班台三态与写操作反馈（任务 id 50）。

**这一层解决什么问题**

值班台是**夜班**用的。夜里三点，值班人要么在等一条危急值，要么在查一条
没回执的。屏幕上必须能一眼分清三件事：

* **正在加载**（骨架屏）—— 数据还在取，别以为是没有数据；
* **空**（说明为什么空 + 下一步）—— 空不是「没事」，可能是阈值表版本还没
  绑定、守护进程还没收到 LIS 推送；空态必须说清是哪一种；
* **错误**（错误码 + 可执行建议 + 重试）—— 出错了要能自己处理，而不是
  截图发微信群问「这是啥」。

外加两件事：**写操作反馈**（toast，四类）与**破坏性操作的二次确认**
（转人工 / 补写回写）。

**三条纪律**

1. **不引入状态机之外的状态**。三态是**界面态**，不是业务态 —— 业务态仍然
   只有状态机那 13 个（``critval.ledger.states``）；界面态不落库、不进审计、
   不参与任何判定。
2. **语义色只走 token**。四类反馈的颜色由 ``tokens.FEEDBACK_SEMANTIC`` 决定，
   这里不写死色值；``failed``（失败深灰）与 ``critical``（危急红）刻意分开 ——
   通道坏了不该用红色去打断夜班。
3. **不用浏览器原生 alert/confirm**。二次确认是站内模态框（模板
   ``templates/base.html`` 里的 ``cv-modal``），脚本在 ``templates/console.js``。

**零外部依赖**：本模块不 import 第三方库，也不读配置 —— 界面态不该随环境
变量漂移。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Mapping, Sequence

from critval.console.components import button, escape, icon, render_template, toast
from critval.console.tokens import (
    FEEDBACK_SEMANTIC,
    SEMANTIC,
    TEMPLATES_DIR,
    TOAST_KIND_BY_ROLE,
)

__all__ = [
    "ERROR_CATALOGUE",
    "FEEDBACK_CATALOGUE",
    "STATE_TEMPLATES_DIR",
    "empty_state_view",
    "error_state",
    "feedback_for_status",
    "feedback_toast",
    "loading_state",
    "render_state",
    "state_template_path",
]

logger = logging.getLogger("critval.console.states")

#: 三态模板目录（``templates/states/``）。
STATE_TEMPLATES_DIR = TEMPLATES_DIR / "states"


# ---------------------------------------------------------------------------
# 错误码目录
# ---------------------------------------------------------------------------

#: **错误码 → 标题 + 可执行建议**。
#:
#: 错误码是给人**抄进工单**的：它要能拿去搜日志、打电话问信息科。所以每个
#: 错误码都配一句「怎么办」—— 只写「出错了」的报错等于没写。
ERROR_CATALOGUE: dict[str, dict[str, str]] = {
    "event_not_found": {
        "title": "台账里没有这条事件",
        "suggestion": (
            "事件号可能抄错了，或这条事件还没进台账。回事件台账重新选一条，"
            "不要凭这条错误码判断处置结果。"
        ),
    },
    "threshold_unbound": {
        "title": "阈值表版本尚未绑定",
        "suggestion": (
            "判定要有依据才敢判：先在配置里指定院内阈值表（CRITVAL_THRESHOLD_PATH）"
            "并重启守护进程，让规则集绑定一个版本。未绑定时判定会直接转人工。"
        ),
    },
    "snapshot_missing": {
        "title": "处置卡快照缺失",
        "suggestion": (
            "本条没有落库的处置卡快照，无法按当时推送出去的那份依据复核。"
            "请人工核对阈值表与院内制度后再处置，并把这条例外记进复核台账。"
        ),
    },
    "writeback_unavailable": {
        "title": "回写通道未接入",
        "suggestion": (
            "登记系统的回写凭据位没配（CRITVAL_BASE_URL / CRITVAL_TOKEN）。"
            "回执不会凭空变成已生效事实 —— 先补配置，再点补写。"
        ),
    },
    "writeback_failed": {
        "title": "回写登记系统失败",
        "suggestion": (
            "处置已确认，但回执没写回登记系统 —— 本条保留在待写队列里，不会丢。"
            "查登记系统的接口契约与凭据后，用「补写回写」再试一次。"
        ),
    },
    "replay_unavailable": {
        "title": "补写通道未接入",
        "suggestion": (
            "补写回写要走登记系统的出站通道，凭据位没配时补不了。"
            "先补配置再重试，本条不会因此被丢弃。"
        ),
    },
    "replay_failed": {
        "title": "补写回写失败",
        "suggestion": (
            "本条仍留在待写队列里，没有被丢弃、也没有被假装成功。"
            "查登记系统的接口契约与凭据后再点一次补写。"
        ),
    },
    "ledger_unavailable": {
        "title": "台账读不出来",
        "suggestion": (
            "台账库打不开（文件锁、权限或磁盘异常）。先看守护进程日志里的 "
            "SQLite 报错，处理完再重试。"
        ),
    },
    "illegal_transition": {
        "title": "当前状态不允许这个动作",
        "suggestion": (
            "这条事件已经不在可操作的状态上（比如已归档或已转人工）。"
            "回台账看当前状态再决定，不要强推。"
        ),
    },
    "confirm_required": {
        "title": "确认人不能为空",
        "suggestion": "没人签的字不进门。填上确认人（姓名或工号）再提交。",
    },
    "reason_required": {
        "title": "原因不能为空",
        "suggestion": (
            "转人工与补写都是交接动作，原因是交接的一部分 —— 事后复核要能回答"
            "「这条为什么停在这儿」。请写明原因。"
        ),
    },
}


# ---------------------------------------------------------------------------
# 写操作反馈（四类）
# ---------------------------------------------------------------------------

#: **反馈类型 → 标题 / 文案 / 语义角色**。
#:
#: 四类覆盖值班人做完写操作后**唯一**会看到的提示：确认成功、推送失败、
#: 回写失败、转人工。文案是写给夜班值班人看的 —— 说清「发生了什么」和
#: 「现在归谁」，不说「操作已完成」这种没有信息量的话。
FEEDBACK_CATALOGUE: dict[str, dict[str, str]] = {
    "ack_ok": {
        "title": "确认成功",
        "text": "处置已确认，回执按 message_control_id 幂等回写登记系统。",
        "role": "done",
    },
    "notify_failed": {
        "title": "推送失败",
        "text": "处置卡没推到值班群，已进死信队列；本条留在台账里等人处理。",
        "role": "failed",
    },
    "writeback_failed": {
        "title": "回写失败",
        "text": "回执没写回登记系统，本条保留为待回写，已确认的回执不丢。",
        "role": "failed",
    },
    "transferred": {
        "title": "已转人工",
        "text": "已转人工，原因已进审计流水；本条不再由值班台自动推进。",
        "role": "pending",
    },
}

#: 事件状态 → 该状态下最该给值班人看的那条反馈。
#:
#: 值班人打开一条失败事件时，最先看到的应该是「它坏在哪」而不是一串状态
#: 枚举 —— 状态枚举在台账列表里已经有了。
#:
#: **``ack_ok`` 不在这里**：确认成功只在「刚刚点了确认」那一刻给提示；
#: 一条早就确认过的事件，重新打开时不该再弹一次「确认成功」。
_STATUS_FEEDBACK: dict[str, str] = {
    "failed": "notify_failed",
    "writeback_failed": "writeback_failed",
    "transferred_to_human": "transferred",
}


# ---------------------------------------------------------------------------
# 模板渲染
# ---------------------------------------------------------------------------


def state_template_path(name: str) -> Path:
    """三态模板文件路径（``templates/states/<name>.html``）。"""
    return STATE_TEMPLATES_DIR / f"{str(name or '').strip()}.html"


def render_state(name: str, values: Mapping[str, Any]) -> str:
    """渲染一个三态模板。

    **模板缺失时也要能出内容** —— 一个静态文件不该让值班台整页打不开；
    缺模板时退化成一行带 ``data-cv-state`` 的简短提示（状态语义不丢）。
    """
    path = state_template_path(name)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        logger.exception("读取三态模板失败：%s", path)
        text = ""
    if not text:
        title = escape(values.get("title", "") or "页面内容暂时不可用")
        return f'<div class="cv-state" data-cv-state="{escape(name)}">{title}</div>'
    return render_template(text, values)


def _skeleton_rows(rows: int = 6) -> str:
    """加载态的骨架行（灰底占位条 —— 不是转圈动画库）。"""
    cells = (
        '<span class="cv-skeleton cv-skeleton--w40"></span>'
        '<span class="cv-skeleton cv-skeleton--w60"></span>'
        '<span class="cv-skeleton cv-skeleton--w60"></span>'
        '<span class="cv-skeleton cv-skeleton--w40"></span>'
    )
    body = "".join(
        f'<div class="cv-skelrow">{cells}</div>' for _ in range(max(1, int(rows)))
    )
    return f'<div class="cv-skelgrid">{body}</div>'


# ---------------------------------------------------------------------------
# 三态
# ---------------------------------------------------------------------------


def loading_state(
    *,
    title: str = "正在取数",
    desc: str = "正在从台账里取危急值事件，稍候。",
    rows: int = 6,
    fallback_href: str = "/events",
) -> str:
    """加载态 —— 骨架屏 + 一句「在取什么」。

    **加载态与空态互斥**：有骨架就说明数据在路上，没数据要等骨架消失之后
    才谈得上。混在一起会让值班人把「还没取到」当成「没有危急值」。
    """
    return render_state(
        "loading",
        {
            "icon": icon("refresh", class_name="cv-state__icon cv-state__icon--spin"),
            "title": escape(title),
            "desc": escape(desc),
            "skeleton": _skeleton_rows(rows),
            "fallback_href": escape(fallback_href),
        },
    )


def empty_state_view(
    *,
    title: str,
    reason: str = "",
    next_step: str = "",
    checks: Sequence[str] = (),
    action: str = "",
    icon_name: str = "inbox",
) -> str:
    """空态 —— **说明为什么空 + 下一步**。

    ``checks`` 是「逐条排查项」：把「到底是哪一种空」列出来，值班人照着看
    一遍就知道该找谁。空态只写「暂无数据」是不够的 —— 那等于让值班人自己去
    猜系统是不是坏了。
    """
    items = [str(item).strip() for item in (checks or []) if str(item).strip()]
    checks_html = ""
    if items:
        lis = "".join(f"<li>{escape(item)}</li>" for item in items)
        checks_html = (
            '<div class="cv-state__checks">'
            '<span class="cv-state__checkslabel">逐条排查</span>'
            f'<ul class="cv-state__checklist">{lis}</ul>'
            "</div>"
        )
    return render_state(
        "empty",
        {
            "icon": icon(icon_name, class_name="cv-state__icon"),
            "title": escape(title),
            "reason": escape(reason),
            "next_step": escape(next_step),
            "checks": checks_html,
            "action": action,
        },
    )


def error_state(
    *,
    code: str,
    detail: str = "",
    title: str = "",
    suggestion: str = "",
    retry_href: str = "",
    retry_label: str = "重试",
    action: str = "",
    icon_name: str = "alert",
) -> str:
    """错误态 —— 错误码 + 可执行建议 + 重试入口。

    **错误码从 ``ERROR_CATALOGUE`` 取**：同一个错码在页面、告警、工单里
    说的是同一件事，值班人不用学两套说法。
    """
    key = str(code or "").strip()
    catalogue = ERROR_CATALOGUE.get(key, {})
    resolved_title = title or catalogue.get("title", "出了点问题")
    resolved_suggestion = suggestion or catalogue.get(
        "suggestion", "把错误码抄给信息科，从守护进程日志里定位这一条。"
    )
    retry_html = (
        button(retry_label, variant="primary", href=retry_href, icon_name="refresh")
        if retry_href
        else ""
    )
    action_html = f'<div class="cv-state__action">{action}</div>' if action else ""
    return render_state(
        "error",
        {
            "icon": icon(icon_name, class_name="cv-state__icon"),
            "title": escape(resolved_title),
            "detail": escape(detail),
            "code": escape(key or "unknown_error"),
            "suggestion": escape(resolved_suggestion),
            "retry": retry_html,
            "action": action_html,
        },
    )


# ---------------------------------------------------------------------------
# 写操作反馈（toast）
# ---------------------------------------------------------------------------


def feedback_toast(kind: str, *, detail: str = "") -> str:
    """四类写操作反馈的 toast 片段。

    ``kind`` 取 ``FEEDBACK_CATALOGUE`` 的键；不认识的键返回空串（**不**渲染
    一个来路不明的提示 —— 值班台不该凭空多出提示）。
    """
    entry = FEEDBACK_CATALOGUE.get(str(kind or "").strip())
    if entry is None:
        return ""
    role = str(entry.get("role", "info"))
    message = str(entry.get("text", ""))
    if detail:
        message = f"{message} {detail}"
    return toast(
        message,
        kind=TOAST_KIND_BY_ROLE.get(role, "info"),
        dismissible=True,
        data_kind=str(kind).strip(),
    )


def feedback_for_status(status: str) -> str:
    """事件状态 → 最该给值班人看的那条反馈类型（空串 = 没有要提示的）。"""
    return _STATUS_FEEDBACK.get(str(status or "").strip(), "")


def feedback_catalogue_is_consistent() -> list[str]:
    """反馈目录里**没有**对应语义角色 / 语义色不存在的条目（应为空）。"""
    problems: list[str] = []
    for key, entry in FEEDBACK_CATALOGUE.items():
        role = str(entry.get("role", ""))
        if role not in SEMANTIC:
            problems.append(f"{key}: 未知语义角色 {role}")
        if FEEDBACK_SEMANTIC.get(key) != role:
            problems.append(f"{key}: 与 tokens.FEEDBACK_SEMANTIC 不一致")
        if TOAST_KIND_BY_ROLE.get(role) is None:
            problems.append(f"{key}: 语义角色 {role} 没有对应的 toast 样式")
    return problems
