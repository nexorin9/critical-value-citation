"""回写失败分类与重试策略（任务 id 43）。

**为什么要把失败分成「能重试」和「不能重试」**：回写失败有两种完全不同的
病因，处置方式也相反。

* **契约错**（4xx）—— 载荷不合登记系统的口径（字段超长、枚举值不认识、
  路径不对）。**重试一万次也是一样的错**，只会把对端刷满、把待写队列
  越堆越长。正确处置是**人工修数据**。
* **对端暂时不可用**（5xx / 超时 / 连不上）—— 载荷没问题，对端暂时抽风。
  这类**可以重试**，而且必须重试：危急值不等。

把两者混在一起（「都重试三次」）或者混在一起（「都不重试」）都不行 ——
前者让契约错的事件永远补不上去，后者让对端抽风五分钟的危急值回执直接
卡死。

**分类结果直接决定重试策略**（见 :func:`retry_decision`），不靠调用方
自己判断。分类口径：

=============================  ======  ==================================
分类                           可重试  处置
=============================  ======  ==================================
``validation`` 本地契约校验未过   否    人工修数据后显式补写
``contract``   对端 4xx          否    人工修数据（契约错，重试无意义）
``conflict``   幂等冲突           否    人工裁决（既有事实不许被覆盖）
``server``     对端 5xx          是    退避重试
``timeout``    调用超时           是    退避重试
``network``    网络不可达         是    退避重试
``unknown``    说不清             否    交人工（不猜）
=============================  ======  ==================================
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = [
    "DEFAULT_MAX_ATTEMPTS",
    "FAILURE_CONFLICT",
    "FAILURE_CONTRACT",
    "FAILURE_LABELS",
    "FAILURE_NETWORK",
    "FAILURE_SERVER",
    "FAILURE_TIMEOUT",
    "FAILURE_UNKNOWN",
    "FAILURE_VALIDATION",
    "RETRYABLE_FAILURES",
    "FailureDecision",
    "classify_failure",
    "is_retryable",
    "retry_decision",
]


FAILURE_VALIDATION = "validation"
FAILURE_CONTRACT = "contract"
FAILURE_CONFLICT = "conflict"
FAILURE_SERVER = "server"
FAILURE_TIMEOUT = "timeout"
FAILURE_NETWORK = "network"
FAILURE_UNKNOWN = "unknown"

#: **可以重试**的分类（对端暂时不可用）。
RETRYABLE_FAILURES: frozenset[str] = frozenset(
    {FAILURE_SERVER, FAILURE_TIMEOUT, FAILURE_NETWORK}
)

#: 分类的中文标签（审计与值班台看）。
FAILURE_LABELS: dict[str, str] = {
    FAILURE_VALIDATION: "本地契约校验未过",
    FAILURE_CONTRACT: "对端契约错（4xx）",
    FAILURE_CONFLICT: "幂等冲突",
    FAILURE_SERVER: "对端服务错误（5xx）",
    FAILURE_TIMEOUT: "调用超时",
    FAILURE_NETWORK: "网络不可达",
    FAILURE_UNKNOWN: "原因不明",
}

#: 默认最大自动重试次数（只对**可重试**分类有效）。
DEFAULT_MAX_ATTEMPTS = 3


def is_retryable(failure_class: str) -> bool:
    """这个分类能不能重试。"""
    return str(failure_class or "").strip() in RETRYABLE_FAILURES


def classify_failure(
    http_status: int,
    error: str = "",
    *,
    timeout: bool = False,
    network: bool = False,
) -> str:
    """把一次出站失败归到分类里。

    * ``timeout=True`` → 超时（可重试）；
    * ``network=True`` → 网络不可达（可重试）；
    * ``4xx`` → 契约错（不可重试，**除 408 / 429** —— 这两个是对端在
      说「现在不行，等会儿」，不是「你这个载荷不对」）；
    * ``5xx`` → 对端服务错误（可重试）；
    * 状态码为 0 且没有显式标记 → 原因不明（**不猜**，交人工）。
    """
    if timeout:
        return FAILURE_TIMEOUT
    if network:
        return FAILURE_NETWORK
    status = int(http_status or 0)
    if status in (408, 429):
        return FAILURE_SERVER
    if 400 <= status < 500:
        return FAILURE_CONTRACT
    if status >= 500:
        return FAILURE_SERVER
    if status:
        return FAILURE_UNKNOWN
    text = str(error or "")
    if "超时" in text:
        return FAILURE_TIMEOUT
    if "不可达" in text:
        return FAILURE_NETWORK
    return FAILURE_UNKNOWN


@dataclass(frozen=True)
class FailureDecision:
    """重试决策：该不该重试、重试几次、下一步谁来做。"""

    failure_class: str
    retryable: bool
    should_retry: bool
    attempts: int = 0
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    action: str = ""
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "failure_class": self.failure_class,
            "label": FAILURE_LABELS.get(self.failure_class, self.failure_class),
            "retryable": self.retryable,
            "should_retry": self.should_retry,
            "attempts": self.attempts,
            "max_attempts": self.max_attempts,
            "action": self.action,
            "reason": self.reason,
        }


def retry_decision(
    failure_class: str,
    *,
    attempts: int = 0,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    explicit: bool = False,
) -> FailureDecision:
    """给定失败分类，算出**该不该重试**与**下一步谁来做**。

    ``explicit=True`` 表示这是**人工显式**发起的补写（CLI
    ``writeback-retry``）—— 显式补写不受自动重试次数限制：人点了就是
    要再试一次；但**契约错与冲突依然不重试**（重试也解决不了）。
    """
    kind = str(failure_class or "").strip() or FAILURE_UNKNOWN
    retryable = is_retryable(kind)
    done = max(0, int(attempts))
    limit = max(0, int(max_attempts))

    if not retryable:
        if kind == FAILURE_CONFLICT:
            action = "标记人工处理"
            reason = "幂等冲突：同一消息控制号下的内容与既有事实不同，既有事实不许被覆盖"
        elif kind == FAILURE_VALIDATION:
            action = "人工修数据后补写"
            reason = "本地契约校验未过：载荷不合登记系统口径，先把数据改对再补写"
        elif kind == FAILURE_UNKNOWN:
            action = "交人工（原因不明）"
            reason = "说不出失败原因就不猜：先人工核对登记系统那条，再决定是否重写"
        else:
            action = "人工修数据后补写"
            reason = "契约错：重试只会把同一个错再发一次，先把数据或口径改对"
        return FailureDecision(
            failure_class=kind,
            retryable=False,
            should_retry=False,
            attempts=done,
            max_attempts=limit,
            action=action,
            reason=reason,
        )

    if explicit:
        return FailureDecision(
            failure_class=kind,
            retryable=True,
            should_retry=True,
            attempts=done,
            max_attempts=limit,
            action="人工显式补写",
            reason="人工显式发起的补写：不受自动重试次数限制",
        )

    if done >= limit:
        return FailureDecision(
            failure_class=kind,
            retryable=True,
            should_retry=False,
            attempts=done,
            max_attempts=limit,
            action="停止自动重试 · 转人工",
            reason=f"已重试 {done} 次仍未成功，超过上限 {limit} 次",
        )

    return FailureDecision(
        failure_class=kind,
        retryable=True,
        should_retry=True,
        attempts=done,
        max_attempts=limit,
        action="退避后重试",
        reason=f"对端暂时不可用（{FAILURE_LABELS.get(kind, kind)}），第 {done + 1} 次重试",
    )
