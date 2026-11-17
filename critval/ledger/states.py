"""业务状态机：7 态主链 + 6 分支态。

主链（一条危急值的正常生命周期）::

    received → judged → card_built → notified → acknowledged → registered → archived

分支态::

    not_critical          仅入台账，不通知（判定结论为不危急）
    pending_ack           已推送待回执，按 due_minutes 到期复查重推
    failed                推送重试耗尽 —— 不静默丢弃，留在台账里等人处理
    writeback_failed      回写登记系统失败（已确认回执**不丢**）
    superseded            被修正结果取代（终态）
    transferred_to_human  未命中依据 / 无法判定 → 转人工

**为什么状态机要单独立一张表**：本项目的产出分两段 —— 「建议」（判定 +
处置卡）与「已生效事实」（回写登记系统后的回执）。这两段之间隔着一次
**人工确认**。状态机就是守住这条线的守门人：状态从 ``notified`` 走到
``acknowledged`` 只有一个入口 —— **值班人点确认**；任何自动路径都跨不过
这条线。表一旦写成散落在各处的 ``if status == ...``，这条线就守不住了。

**两条硬规则**：

1. **非法跃迁被拒** —— 跳步（``judged`` 直接到 ``notified``）说明中间环节
   被绕过，必须报错而不是放行。报错即留痕，不静默。
2. **终态不可静默回退** —— ``archived`` / ``superseded`` 是事实定格的地方，
   普通路径不许把它们改回去；要改必须显式声明（人工裁决路径）。

**同状态重复设置是幂等空操作**：值班人点两次确认、不该报���；但它**不写
审计**（否则审计里全是噪音，真正的状态变化反而看不见）。
"""

from __future__ import annotations

__all__ = [
    "ALL_STATUSES",
    "BRANCH_STATUSES",
    "IllegalTransitionError",
    "LEGAL_TRANSITIONS",
    "MAIN_CHAIN",
    "STATUS_ACKNOWLEDGED",
    "STATUS_ARCHIVED",
    "STATUS_CARD_BUILT",
    "STATUS_FAILED",
    "STATUS_JUDGED",
    "STATUS_NOT_CRITICAL",
    "STATUS_NOTIFIED",
    "STATUS_PENDING_ACK",
    "STATUS_RECEIVED",
    "STATUS_REGISTERED",
    "STATUS_SUPERSEDED",
    "STATUS_TRANSFERRED_TO_HUMAN",
    "STATUS_WRITEBACK_FAILED",
    "TERMINAL_STATUSES",
    "describe_status",
    "is_known_status",
    "is_terminal",
    "validate_transition",
]


# ---------------------------------------------------------------------------
# 状态常量
# ---------------------------------------------------------------------------

STATUS_RECEIVED = "received"
STATUS_JUDGED = "judged"
STATUS_CARD_BUILT = "card_built"
STATUS_NOTIFIED = "notified"
STATUS_ACKNOWLEDGED = "acknowledged"
STATUS_REGISTERED = "registered"
STATUS_ARCHIVED = "archived"

STATUS_NOT_CRITICAL = "not_critical"
STATUS_PENDING_ACK = "pending_ack"
STATUS_FAILED = "failed"
STATUS_WRITEBACK_FAILED = "writeback_failed"
STATUS_SUPERSEDED = "superseded"
STATUS_TRANSFERRED_TO_HUMAN = "transferred_to_human"


#: 7 态主链（顺序即正常路径的推进顺序）。
MAIN_CHAIN: tuple[str, ...] = (
    STATUS_RECEIVED,
    STATUS_JUDGED,
    STATUS_CARD_BUILT,
    STATUS_NOTIFIED,
    STATUS_ACKNOWLEDGED,
    STATUS_REGISTERED,
    STATUS_ARCHIVED,
)

#: 6 个分支态。
BRANCH_STATUSES: frozenset[str] = frozenset(
    {
        STATUS_NOT_CRITICAL,
        STATUS_PENDING_ACK,
        STATUS_FAILED,
        STATUS_WRITEBACK_FAILED,
        STATUS_SUPERSEDED,
        STATUS_TRANSFERRED_TO_HUMAN,
    }
)

#: 全部合法状态。
ALL_STATUSES: frozenset[str] = frozenset(MAIN_CHAIN) | BRANCH_STATUSES

#: **终态**：事实已定格，普通路径不许再迁走。
TERMINAL_STATUSES: frozenset[str] = frozenset({STATUS_ARCHIVED, STATUS_SUPERSEDED})


#: 合法跃迁表：``当前状态 → 允许去的状态``。
#:
#: 表里**不写同状态** —— 同状态是幂等空操作，由
#: :func:`validate_transition` 单独处理（不算非法，也不写审计）。
LEGAL_TRANSITIONS: dict[str, frozenset[str]] = {
    # 刚收下报文，还没判定。
    STATUS_RECEIVED: frozenset(
        {
            STATUS_JUDGED,
            STATUS_NOT_CRITICAL,
            STATUS_TRANSFERRED_TO_HUMAN,
            STATUS_FAILED,
            STATUS_SUPERSEDED,
        }
    ),
    # 判定完成（危急 / 不危急 / 转人工都从这里分叉）。
    STATUS_JUDGED: frozenset(
        {
            STATUS_CARD_BUILT,
            STATUS_NOT_CRITICAL,
            STATUS_TRANSFERRED_TO_HUMAN,
            STATUS_FAILED,
            STATUS_SUPERSEDED,
        }
    ),
    # 处置卡已装配（四要素齐备 / 缺项 / 降级都算装好了）。
    STATUS_CARD_BUILT: frozenset(
        {
            STATUS_NOTIFIED,
            STATUS_PENDING_ACK,
            STATUS_TRANSFERRED_TO_HUMAN,
            STATUS_FAILED,
            STATUS_SUPERSEDED,
        }
    ),
    # 已推送（推送动作成功，尚未确认）。
    STATUS_NOTIFIED: frozenset(
        {
            STATUS_ACKNOWLEDGED,
            STATUS_PENDING_ACK,
            STATUS_TRANSFERRED_TO_HUMAN,
            STATUS_FAILED,
            STATUS_SUPERSEDED,
        }
    ),
    # 已推送待回执（到期要复查重推）。
    STATUS_PENDING_ACK: frozenset(
        {
            STATUS_ACKNOWLEDGED,
            STATUS_TRANSFERRED_TO_HUMAN,
            STATUS_FAILED,
            STATUS_SUPERSEDED,
        }
    ),
    # 值班人已确认 —— **人工确认点**。
    #
    # ``transferred_to_human`` 是任务 id 43 补的出边：**幂等冲突**要由人
    # 裁决（同一消息控制号下内容与既有事实不同，既有事实不许被自动覆盖），
    # 所以「已确认 → 转人工」这条必须存在 —— 否则冲突事件只能挂在
    # ``writeback_failed`` 里，人分不清它是「对端抽风」还是「内容打架」。
    STATUS_ACKNOWLEDGED: frozenset(
        {STATUS_REGISTERED, STATUS_WRITEBACK_FAILED, STATUS_TRANSFERRED_TO_HUMAN}
    ),
    # 回写登记系统失败：已确认的回执**不丢**，等补写。
    #
    # ``transferred_to_human`` 是任务 id 43 补的出边：**幂等冲突**必须由人
    # 裁决（既有事实不许被自动覆盖），所以冲突那条要能直接转人工，
    # 而不是挂在「回写失败」里等人猜是哪一类失败。
    STATUS_WRITEBACK_FAILED: frozenset(
        {STATUS_REGISTERED, STATUS_FAILED, STATUS_TRANSFERRED_TO_HUMAN}
    ),
    # 回写成功 —— 已生效事实。
    STATUS_REGISTERED: frozenset({STATUS_ARCHIVED}),
    # 终态。
    STATUS_ARCHIVED: frozenset(),
    STATUS_SUPERSEDED: frozenset(),
    # 不危急：只入台账，不通知；复核后归档。
    STATUS_NOT_CRITICAL: frozenset({STATUS_ARCHIVED}),
    # 转人工：人接了手 —— 可以补判定、补出卡、补确认，也可以直接归档。
    STATUS_TRANSFERRED_TO_HUMAN: frozenset(
        {
            STATUS_JUDGED,
            STATUS_CARD_BUILT,
            STATUS_NOTIFIED,
            STATUS_ACKNOWLEDGED,
            STATUS_ARCHIVED,
            STATUS_FAILED,
        }
    ),
    # 自动路径放弃（推送重试耗尽）：留在台账里等人处理。
    STATUS_FAILED: frozenset(
        {STATUS_TRANSFERRED_TO_HUMAN, STATUS_ARCHIVED}
    ),
}


#: 状态的中文标签（审计与值班台上给人看）。
STATUS_LABELS: dict[str, str] = {
    STATUS_RECEIVED: "已接收",
    STATUS_JUDGED: "已判定",
    STATUS_CARD_BUILT: "已出卡",
    STATUS_NOTIFIED: "已推送",
    STATUS_ACKNOWLEDGED: "已确认",
    STATUS_REGISTERED: "已回写",
    STATUS_ARCHIVED: "已归档",
    STATUS_NOT_CRITICAL: "不危急",
    STATUS_PENDING_ACK: "已推送待回执",
    STATUS_FAILED: "处理失败",
    STATUS_WRITEBACK_FAILED: "回写失败",
    STATUS_SUPERSEDED: "已被取代",
    STATUS_TRANSFERRED_TO_HUMAN: "转人工",
}


class IllegalTransitionError(RuntimeError):
    """非法状态跃迁被拒。

    带三个字段，方便调用方直接写进审计 / 报错文案：

    * ``current`` —— 跃迁前状态；
    * ``target`` —— 想去的状态；
    * ``reason`` —— 为什么不让走（跳步 / 从终态回退 / 未知状态）。
    """

    def __init__(self, current: str, target: str, reason: str = "") -> None:
        self.current = current
        self.target = target
        self.reason = reason or "该跃迁不在状态机允许的路径上"
        super().__init__(
            f"非法状态跃迁 {current} → {target}：{self.reason}"
        )


def is_known_status(status: str) -> bool:
    """这个状态是不是本状态机认识的状态。"""
    return str(status or "").strip() in ALL_STATUSES


def is_terminal(status: str) -> bool:
    """这个状态是不是终态（事实定格，普通路径不许迁走）。"""
    return str(status or "").strip() in TERMINAL_STATUSES


def describe_status(status: str) -> str:
    """状态 → 人话（``已推送待回执``）。未知状态原样返回。"""
    key = str(status or "").strip()
    return STATUS_LABELS.get(key, key or "未知状态")


def validate_transition(current: str, target: str) -> None:
    """校验一次状态跃迁；不合法就抛 :class:`IllegalTransitionError`。

    * **同状态** → 幂等空操作，直接放行（不抛错）；
    * **未知状态** → 抛错（拼错的状态名不能悄悄进库）；
    * **终态回退** → 抛错（终态不可静默回退）；
    * **跳步** → 抛错（中间环节被绕过）。
    """
    source = str(current or "").strip()
    dest = str(target or "").strip()

    if not dest:
        raise IllegalTransitionError(source, dest, "目标状态为空")
    if not is_known_status(dest):
        raise IllegalTransitionError(source, dest, f"未知状态 {dest!r}")
    if source == dest:
        return
    if source and not is_known_status(source):
        raise IllegalTransitionError(source, dest, f"当前状态 {source!r} 不是本状态机的状态")
    if is_terminal(source):
        raise IllegalTransitionError(
            source,
            dest,
            f"{source} 是终态，事实已定格，不许静默回退",
        )
    allowed = LEGAL_TRANSITIONS.get(source, frozenset())
    if dest not in allowed:
        raise IllegalTransitionError(
            source,
            dest,
            f"合法去向只有 {sorted(allowed) or '（无）'}",
        )
