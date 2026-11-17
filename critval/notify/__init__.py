"""通知层：值班群推送与本机降级出口。

* ``critval.notify.wecom`` —— 企业微信群机器人出站通道（真实 webhook
  POST + 指数退避重试）；
* ``critval.notify.local`` —— 本机通知记录（企微不可用时事件不丢）。
"""

from critval.notify.local import (
    CHANNEL_LOCAL,
    DEFAULT_LOCAL_NOTIFICATION_PATH,
    FallbackNotifier,
    LocalNotification,
    LocalNotifier,
    NotifyOutcome,
)
from critval.notify.wecom import (
    DEFAULT_BACKOFF_BASE_SECONDS,
    DEFAULT_MAX_ATTEMPTS,
    WECOM_ERRCODES,
    WeComNotifier,
    WeComResult,
)

__all__ = [
    "CHANNEL_LOCAL",
    "DEFAULT_BACKOFF_BASE_SECONDS",
    "DEFAULT_LOCAL_NOTIFICATION_PATH",
    "DEFAULT_MAX_ATTEMPTS",
    "FallbackNotifier",
    "LocalNotification",
    "LocalNotifier",
    "NotifyOutcome",
    "WECOM_ERRCODES",
    "WeComNotifier",
    "WeComResult",
]
