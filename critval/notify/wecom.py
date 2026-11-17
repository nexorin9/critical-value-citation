"""企业微信群机器人推送通道（任务 id 11 · 三条真实出站通道之二）。

**对端**：值班群的企微群机器人 webhook。

=====================  =====================================================
出站调用               用途
=====================  =====================================================
``POST {CRITVAL_WECOM_WEBHOOK_URL}``
                     推送处置卡 Markdown（含出处行号 + 确认链接）
=====================  =====================================================

**凭据从环境变量读**：``CRITVAL_WECOM_WEBHOOK_URL`` —— 群机器人 webhook
本身就是一个带 key 的完整 URL，**它就是凭据**。代码内不出现真实地址；
日志与错误文案里只出现脱敏形式（前 4 后 4）。

**请求体**（企微群机器人约定）::

    {"msgtype": "markdown", "markdown": {"content": "<处置卡正文>"}}

**响应体**（企微群机器人约定）::

    {"errcode": 0, "errmsg": "ok"}

``errcode != 0`` 即失败 —— **HTTP 200 不等于推送成功**。这一点必须判，
否则「推了但没推到」的危急值会在值班群里静默消失。

**退避重试**：对端限流（``45009``）与网络抖动按**指数退避**重试，
默认最多 3 次、基数 0.5 秒、倍数 2、单次等待上限 8 秒。
契约错（``40001`` 凭据不对、``93000`` webhook 地址非法）**不重试** ——
重试一万次也是一样的错。

**与可靠性层的分工**（任务 id 42）：本模块的 ``send_payload`` 自带一档
退避重试，供「一次调用里推完」的简单场景使用；**逐次留痕 + 抖动 + 死信 +
重复投递抑制**由 ``critval.notify.reliability`` 负责，它调的是本模块的
``send_once``（**只发一次**，退避节奏由可靠性层统一管），这样每一次出站
尝试都能单独留痕（状态码 / 耗时 / 重试序号 / 错误摘要）。
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional

__all__ = [
    "DEFAULT_BACKOFF_BASE_SECONDS",
    "DEFAULT_BACKOFF_FACTOR",
    "DEFAULT_MAX_ATTEMPTS",
    "DEFAULT_MAX_BACKOFF_SECONDS",
    "DEFAULT_TIMEOUT_SECONDS",
    "WECOM_ERRCODES",
    "WeComNotifier",
    "WeComResult",
]

#: 默认超时（秒）。
DEFAULT_TIMEOUT_SECONDS = 5.0

#: 默认最多尝试几次（含第一次）。
DEFAULT_MAX_ATTEMPTS = 3

#: 退避基数（秒）：第一次重试等这么久。
DEFAULT_BACKOFF_BASE_SECONDS = 0.5

#: 退避倍数：每重试一次，等待时间乘这个数。
DEFAULT_BACKOFF_FACTOR = 2.0

#: 单次等待上限（秒）—— 再长也不该让危急值干等。
DEFAULT_MAX_BACKOFF_SECONDS = 8.0

#: 企微群机器人错误码 → 人话 + 是否值得重试。
#:
#: 只收**在值班群场景真的会遇到**的几个码；其余未知码一律按
#: 「不重试」处理 —— 对一个不认识的错误码反复重试，只会挡住后面的事件。
WECOM_ERRCODES: Mapping[int, tuple[str, bool]] = {
    0: ("ok", False),
    40001: ("access_token 或 webhook key 不正确", False),
    40014: ("access_token 无效", False),
    41001: ("缺少 access_token", False),
    42007: ("access_token 已过期", False),
    43009: ("群机器人已被移出群聊", False),
    45009: ("接口调用超过频率限制", True),
    93000: ("webhook 地址不合法", False),
}


def _mask_webhook(url: str) -> str:
    """webhook 地址脱敏（前 4 后 4 + 长度）—— 它就是凭据。"""
    text = str(url or "")
    if not text:
        return "(unset)"
    if len(text) <= 12:
        return f"***{len(text)} chars"
    return f"{text[:4]}***{text[-4:]} ({len(text)} chars)"


@dataclass(frozen=True)
class WeComResult:
    """一次企微推送的结论。"""

    ok: bool
    status: int = 0
    errcode: int = 0
    errmsg: str = ""
    error: str = ""
    retryable: bool = False
    #: 真实发出去几次（含失败的那些次）。
    attempts: int = 0
    #: 每次尝试之间的等待秒数（复现与审计用）。
    waits: tuple[float, ...] = ()
    body: str = ""
    request_body: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "status": self.status,
            "errcode": self.errcode,
            "errmsg": self.errmsg,
            "error": self.error,
            "retryable": self.retryable,
            "attempts": self.attempts,
            "waits": list(self.waits),
        }


class WeComNotifier:
    """企微群机器人推送器。

    * 地址：``CRITVAL_WECOM_WEBHOOK_URL``（环境变量，本身即凭据）；
    * 正文：企微 markdown；处置卡正文由 ``critval.card.renderer`` 渲染，
      这里只管**发出去**；
    * 重试：对端限流与网络抖动按指数退避重试，契约错不重试。
    """

    def __init__(
        self,
        webhook_url: str,
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        backoff_base: float = DEFAULT_BACKOFF_BASE_SECONDS,
        backoff_factor: float = DEFAULT_BACKOFF_FACTOR,
        max_backoff: float = DEFAULT_MAX_BACKOFF_SECONDS,
        sleep: Optional[Callable[[float], None]] = None,
        opener: Optional[Any] = None,
    ) -> None:
        self.webhook_url = str(webhook_url or "").strip()
        self.timeout = float(timeout)
        self.max_attempts = max(1, int(max_attempts))
        self.backoff_base = float(backoff_base)
        self.backoff_factor = float(backoff_factor)
        self.max_backoff = float(max_backoff)
        self._sleep = sleep if sleep is not None else time.sleep
        self._opener = opener or urllib.request.urlopen

    # ---- 构造 -------------------------------------------------------------
    @classmethod
    def from_config(
        cls,
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        sleep: Optional[Callable[[float], None]] = None,
    ) -> "WeComNotifier":
        """按环境变量装配推送器；缺 webhook **立即报错**（不发起任何请求）。"""
        from critval.config import Config

        url = Config.get_env("CRITVAL_WECOM_WEBHOOK_URL") or ""
        if not url:
            raise RuntimeError(
                "缺少 CRITVAL_WECOM_WEBHOOK_URL（企微群机器人 webhook），无法推送"
            )
        return cls(url, timeout=timeout, max_attempts=max_attempts, sleep=sleep)

    @property
    def endpoint(self) -> str:
        return self.webhook_url

    @property
    def masked_endpoint(self) -> str:
        """脱敏后的地址（写日志用）。"""
        return _mask_webhook(self.webhook_url)

    def backoff_delay(self, attempt: int) -> float:
        """第 ``attempt`` 次失败后该等多久（指数退避，封顶）。"""
        delay = self.backoff_base * (self.backoff_factor ** max(0, attempt - 1))
        return min(delay, self.max_backoff)

    # ---- 出站 -------------------------------------------------------------
    def send_markdown(
        self,
        content: str,
        *,
        confirm_url: str = "",
    ) -> WeComResult:
        """推送一段 markdown 正文。

        ``confirm_url`` 非空时**追加**到正文尾部 —— 值班人要点它确认。
        正文已经自带确认链接时不重复追加。
        """
        if not self.webhook_url:
            raise RuntimeError("缺少 CRITVAL_WECOM_WEBHOOK_URL，无法推送处置卡")
        text = str(content or "")
        if confirm_url and confirm_url not in text:
            text = f"{text}\n\n[确认回执]({confirm_url})"
        payload = {"msgtype": "markdown", "markdown": {"content": text}}
        return self.send_payload(payload)

    def send_card(self, card: Any, *, confirm_url: str = "") -> WeComResult:
        """推送一张处置卡（正文由渲染层出，这里只负责发）。"""
        from critval.card.renderer import build_push_payload

        payload = build_push_payload(card, confirm_url)
        return self.send_payload(payload)

    def send_payload(self, payload: Mapping[str, Any]) -> WeComResult:
        """按给定载荷 POST（**带指数退避重试**）。"""
        body = dict(payload or {})
        waits: list[float] = []
        last: WeComResult = WeComResult(
            ok=False, error="未发起请求", request_body=body
        )
        for attempt in range(1, self.max_attempts + 1):
            outcome = self.send_once(payload)
            last = WeComResult(
                ok=outcome.ok,
                status=outcome.status,
                errcode=outcome.errcode,
                errmsg=outcome.errmsg,
                error=outcome.error,
                retryable=outcome.retryable,
                attempts=attempt,
                waits=tuple(waits),
                body=outcome.body,
                request_body=body,
            )
            if last.ok:
                return last
            if not last.retryable or attempt >= self.max_attempts:
                return last
            delay = self.backoff_delay(attempt)
            waits.append(delay)
            self._sleep(delay)
        return last

    def send_once(self, payload: Mapping[str, Any]) -> WeComResult:
        """只发**一次**，不自带重试 —— 给可靠性层用。

        退避节奏由 ``critval.notify.reliability`` 统一管（它要**逐次留痕**：
        状态码 / 耗时 / 重试序号 / 错误摘要），所以这里只负责「发一次」。

        缺 webhook 时**不发起请求**，直接返回失败结论 —— 不静默吞掉。
        """
        body = dict(payload or {})
        if not self.webhook_url:
            return WeComResult(
                ok=False,
                error="缺少 CRITVAL_WECOM_WEBHOOK_URL，无法推送处置卡",
                request_body=body,
            )
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.endpoint,
            data=data,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        outcome = self._attempt(request, body)
        return WeComResult(
            ok=outcome.ok,
            status=outcome.status,
            errcode=outcome.errcode,
            errmsg=outcome.errmsg,
            error=outcome.error,
            retryable=outcome.retryable,
            attempts=1,
            waits=(),
            body=outcome.body,
            request_body=body,
        )

    def push_callable(
        self, content: str, *, confirm_url: str = ""
    ) -> Callable[[Mapping[str, Any]], WeComResult]:
        """给可靠性层用的一次性推送出口。

        每次被调用**只发一次**（不自带重试）；返回的对象带 ``ok`` /
        ``status`` / ``error`` / ``retryable``，可靠性层据此决定退避重试还是
        直接落死信。
        """
        text = str(content or "")
        if confirm_url and confirm_url not in text:
            text = f"{text}\n\n[确认回执]({confirm_url})"
        payload = {"msgtype": "markdown", "markdown": {"content": text}}

        def push(_row: Mapping[str, Any]) -> WeComResult:
            return self.send_once(payload)

        return push

    # ---- 内部 -------------------------------------------------------------
    def _attempt(
        self, request: urllib.request.Request, body: Mapping[str, Any]
    ) -> WeComResult:
        try:
            with self._opener(request, timeout=self.timeout) as response:
                text = response.read().decode("utf-8", errors="replace")
                status = int(getattr(response, "status", 200) or 200)
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001 —— 读不到响应体也要给出状态码
                detail = ""
            return WeComResult(
                ok=False,
                status=exc.code,
                error=f"企微 webhook 返回 {exc.code}",
                retryable=exc.code >= 500 or exc.code in {408, 429},
                body=detail,
                request_body=body,
            )
        except urllib.error.URLError as exc:
            return WeComResult(
                ok=False,
                status=0,
                error=f"企微 webhook 不可达：{exc.reason}",
                retryable=True,
                request_body=body,
            )
        except TimeoutError as exc:
            return WeComResult(
                ok=False,
                status=0,
                error=f"企微 webhook 调用超时（{self.timeout} 秒）：{exc}",
                retryable=True,
                request_body=body,
            )

        if not (200 <= status < 300):
            return WeComResult(
                ok=False,
                status=status,
                error=f"企微 webhook 返回 {status}",
                retryable=status >= 500 or status in {408, 429},
                body=text,
                request_body=body,
            )

        errcode, errmsg, retryable = self._read_errcode(text)
        return WeComResult(
            ok=errcode == 0,
            status=status,
            errcode=errcode,
            errmsg=errmsg,
            error="" if errcode == 0 else f"企微推送失败：{errmsg}",
            retryable=retryable,
            body=text,
            request_body=body,
        )

    @staticmethod
    def _read_errcode(text: str) -> tuple[int, str, bool]:
        """读企微响应里的 ``errcode`` / ``errmsg``。

        **响应不是 JSON 时按失败处理**（``errcode`` 取一个不存在的码）——
        宁可报失败让人来看，也不把「读不懂的响应」当成推送成功。
        """
        if not str(text or "").strip():
            return -1, "企微返回空响应体", False
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return -1, "企微响应不是合法 JSON", False
        if not isinstance(payload, Mapping):
            return -1, "企微响应不是 JSON 对象", False
        raw = payload.get("errcode", -1)
        try:
            errcode = int(raw)
        except (TypeError, ValueError):
            return -1, f"企微 errcode 不是数字：{raw!r}", False
        errmsg = str(payload.get("errmsg", "") or "")
        known = WECOM_ERRCODES.get(errcode)
        if known is not None:
            label, retryable = known
            if errcode == 0:
                return 0, errmsg or label, False
            return errcode, f"{errcode} {label}" + (f"（{errmsg}）" if errmsg else ""), retryable
        # 未知错误码：不重试（重试解决不了未知的错），但把码原样报出来。
        return errcode, f"{errcode} {errmsg}".strip() or f"企微返回错误码 {errcode}", False
