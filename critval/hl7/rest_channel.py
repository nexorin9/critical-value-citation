"""LIS REST 回调通道（任务 id 4 · 入站辅通道）。

院内 LIS 推危急值有两条路：HL7 v2 ``ORU^R01`` 走 MLLP（主通道），以及直接
按 REST 契约推一条危急值（辅通道）。本模块是辅通道的**两侧**：

* **入站适配**（``normalize_rest_body``）：把 LIS 推来的 REST 载荷规范成
  事件模型。**复用** ``critval.hl7.normalize.normalize_http_payload`` ——
  与 MLLP 路径同一份规范函数，因此同一条危急值走两条通道得到**同一个
  ``event_id``**。
* **回调客户端**（``LisRestCallbackClient``）：按院内约定的 REST 契约，把
  一条危急值事件 POST 回 LIS 侧的接口（``{LIS_BASE_URL}/api/v1/
  critical-value-events``），头 ``Authorization: Bearer $LIS_CRITICAL_TOKEN``。
  这是**真实出站调用**：基地址与凭据都从环境变量读，代码内不出现密钥。

**载荷字段口径**（与 spec.md「入站辅通道」一致）：

==================  ==========================================
载荷字段            含义
==================  ==========================================
``event_id``        LIS 侧的事件号 —— 进入事件幂等键
``item_code``       检验项目码（阈值表主键）
``value``           观察值（判定入参）
``unit``            单位（换算入参）
``critical_level``  异常标志 —— **仅作交叉校验，不作判定权威**
``raised_at``       结果时刻（幂等键与生效期比较入参）
``dept_code``       科室
==================  ==========================================
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Optional

from critval.hl7.normalize import normalize_http_payload
from critval.models import CriticalEvent

__all__ = [
    "LIS_REST_EVENT_PATH",
    "LisRestCallbackClient",
    "LisRestError",
    "LisRestResult",
    "build_event_body",
    "normalize_rest_body",
]

#: LIS 侧危急值回调路径（拼在 ``LIS_BASE_URL`` 之后）。
LIS_REST_EVENT_PATH = "/api/v1/critical-value-events"

#: 默认超时（秒）。院内接口引擎偶发慢，超时不能太长 —— 危急值等不起。
DEFAULT_TIMEOUT_SECONDS = 5.0


class LisRestError(RuntimeError):
    """LIS REST 回调失败。

    ``retryable`` 区分两类失败：``4xx`` 是**契约错**（载荷不对、路径不对），
    重试没有意义；``5xx`` 与网络超时是**对端暂时不可用**，可以重试。
    """

    def __init__(self, message: str, *, retryable: bool = False, status: int = 0) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.status = status


@dataclass(frozen=True)
class LisRestResult:
    """一次回调的结论。"""

    ok: bool
    status: int = 0
    body: str = ""
    error: str = ""
    retryable: bool = False
    request_body: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "status": self.status,
            "error": self.error,
            "retryable": self.retryable,
            "request_body": dict(self.request_body),
        }


def build_event_body(
    event: CriticalEvent,
    *,
    critical_level: str = "",
) -> dict[str, Any]:
    """事件 → LIS REST 回调载荷。

    ``event_id`` 用**事件的消息控制号**（LIS 侧认识的号），不是本系统算出来的
    事件 ID —— LIS 用它做幂等键，我们用它对回去才不会生成两条记录。
    """
    observed_at = getattr(event, "observed_at", None)
    raised_at = (
        observed_at.isoformat(sep=" ", timespec="seconds")
        if isinstance(observed_at, datetime)
        else ""
    )
    value = getattr(event, "value", None)
    return {
        "event_id": event.message_control_id or event.event_id,
        "item_code": event.item_code,
        "value": "" if value is None else str(value),
        "unit": event.unit,
        "critical_level": critical_level or event.obx_flag,
        "raised_at": raised_at,
        "dept_code": event.dept_code,
    }


def normalize_rest_body(
    body: Mapping[str, Any]
) -> tuple[Optional[CriticalEvent], Optional[Any]]:
    """LIS REST 载荷 → （事件, 未识别记录）。

    **与 MLLP 路径共用同一套规范函数** —— 幂等键拼法、观察值解析、患者标识
    处理、事件构造只有一份实现。
    """
    return normalize_http_payload(body)


class LisRestCallbackClient:
    """按院内 REST 契约把危急值事件回调到 LIS 侧。

    * 基地址：``LIS_BASE_URL``（环境变量）；
    * 凭据：``LIS_CRITICAL_TOKEN``（环境变量），``Authorization: Bearer``；
    * 出错分级：``4xx`` 不可重试（契约错），``5xx`` / 超时可重试。
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        path: str = LIS_REST_EVENT_PATH,
        opener: Optional[Any] = None,
    ) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.token = token or ""
        self.timeout = float(timeout)
        self.path = path if path.startswith("/") else f"/{path}"
        self._opener = opener or urllib.request.urlopen

    @property
    def endpoint(self) -> str:
        """完整目标地址。"""
        return f"{self.base_url}{self.path}"

    # ---- 出站 ------------------------------------------------------------
    def post_event(
        self,
        event: CriticalEvent,
        *,
        critical_level: str = "",
    ) -> LisRestResult:
        """把一条事件 POST 回 LIS 侧。"""
        body = build_event_body(event, critical_level=critical_level)
        return self.post_body(body)

    def post_body(self, body: Mapping[str, Any]) -> LisRestResult:
        """按给定载荷 POST（测试与补推入口共用）。"""
        if not self.base_url:
            raise LisRestError("缺少 LIS_BASE_URL，无法发起 LIS REST 回调")
        if not self.token:
            raise LisRestError("缺少 LIS_CRITICAL_TOKEN，无法发起 LIS REST 回调")

        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.endpoint,
            data=payload,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.token}",
            },
        )
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
            retryable = exc.code >= 500 or exc.code in {408, 429}
            return LisRestResult(
                ok=False,
                status=exc.code,
                body=detail,
                error=f"LIS REST 回调返回 {exc.code}",
                retryable=retryable,
                request_body=body,
            )
        except urllib.error.URLError as exc:
            return LisRestResult(
                ok=False,
                status=0,
                error=f"LIS REST 回调不可达：{exc.reason}",
                retryable=True,
                request_body=body,
            )
        except TimeoutError as exc:
            return LisRestResult(
                ok=False,
                status=0,
                error=f"LIS REST 回调超时（{self.timeout} 秒）：{exc}",
                retryable=True,
                request_body=body,
            )

        ok = 200 <= status < 300
        return LisRestResult(
            ok=ok,
            status=status,
            body=text,
            error="" if ok else f"LIS REST 回调返回 {status}",
            retryable=not ok and (status >= 500 or status in {408, 429}),
            request_body=body,
        )
