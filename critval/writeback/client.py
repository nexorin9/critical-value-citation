"""登记系统出站客户端（任务 id 38 · 对账与补写）。

院内危急值登记 / 护理执行平台是**对端系统**。本模块只做一件事：按院内
约定的 REST 契约与它对话。

==================  ======================================================
出站调用            用途
==================  ======================================================
``GET  {CRITVAL_BASE_URL}/api/v1/critical-events?message_control_id=…``
                    按幂等键取**一条**回执（对账用）
``GET  {CRITVAL_BASE_URL}/api/v1/critical-events``
                    列全部回执（找「登记系统有、本地没有」的漏记）
``POST {CRITVAL_BASE_URL}/api/v1/critical-events``
                    补写 / 重放一条回执（幂等键 = ``message_control_id``）
==================  ======================================================

**凭据从环境变量读**：``CRITVAL_BASE_URL``（基地址）与 ``CRITVAL_TOKEN``
（``Authorization: Bearer``）。代码内不出现密钥；**缺凭据位时在发请求之前
就报错** —— 不能「先发出去再说」，也不能拿本地替身顶上去。

**幂等键是 ``message_control_id``**：登记系统按这个键覆盖同一条。重放一条
已经写过的回执不会在登记系统里生成第二条 —— 这是「重放」这个动作敢做的
前提。

**回执字段规范**（与 spec.md「出站 — 三条真实调用」一致）：

======================  ==================================================
字段                    含义
======================  ==================================================
``message_control_id``  幂等键（消息控制号）
``item_code``           检验项目码
``value``               观察值
``threshold_row``       阈值依据行定位串（文件名 · 行号 · 版本）
``notified_dept``       已通知科室
``duty_person``         当班人（岗位口径，不落姓名以外的患者信息）
``due_minutes``         上报时限（分钟）
``receipt_status``      回执状态（accepted / rejected / voided …）
``receipt_at``          回执时刻
======================  ==================================================

**错误分级**：``4xx`` 是**契约错**（路径不对、载荷不对），重试没有意义；
``5xx`` 与网络超时是**对端暂时不可用**，可以重试。两类都**不改本地状态**
—— 对账与补写都只在 ``POST`` 明确成功之后才动台账。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence

__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "RECEIPT_FIELDS",
    "RECEIPT_PATH",
    "ReceiptRecord",
    "RegistrationError",
    "RegistrationResult",
    "RegistrationSystemClient",
]


#: 登记系统回执接口路径（拼在 ``CRITVAL_BASE_URL`` 之后）。
RECEIPT_PATH = "/api/v1/critical-events"

#: 回执字段规范 —— 对账时逐字段比对的就是这九项。
RECEIPT_FIELDS: tuple[str, ...] = (
    "message_control_id",
    "item_code",
    "value",
    "threshold_row",
    "notified_dept",
    "duty_person",
    "due_minutes",
    "receipt_status",
    "receipt_at",
)

#: 默认超时（秒）。危急值等不起，对端慢也不能一直挂着。
DEFAULT_TIMEOUT_SECONDS = 5.0


class RegistrationError(RuntimeError):
    """登记系统出站调用失败。

    ``retryable`` 区分两类失败：``4xx`` 是契约错（重试无意义），
    ``5xx`` / 超时 / 网络不可达是对端暂时不可用（可以重试）。
    """

    def __init__(self, message: str, *, retryable: bool = False, status: int = 0) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.status = status


@dataclass(frozen=True)
class ReceiptRecord:
    """登记系统侧的一条回执。

    字段按 :data:`RECEIPT_FIELDS` 规范读；**多出来的字段原样留在
    ``extra`` 里** —— 对端将来加字段时，本地不必改代码就能看见「对端说了
    什么」，而不是静默丢掉。
    """

    message_control_id: str = ""
    item_code: str = ""
    value: str = ""
    threshold_row: str = ""
    notified_dept: str = ""
    duty_person: str = ""
    due_minutes: Optional[int] = None
    receipt_status: str = ""
    receipt_at: str = ""
    extra: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ReceiptRecord":
        """从一条 JSON 对象读出回执（未知字段进 ``extra``）。"""
        data = dict(payload or {})
        due = data.get("due_minutes")
        minutes: Optional[int] = None
        if due not in (None, ""):
            try:
                minutes = int(due)
            except (TypeError, ValueError):
                minutes = None
        extra = {k: v for k, v in data.items() if k not in RECEIPT_FIELDS}
        return cls(
            message_control_id=str(data.get("message_control_id") or "").strip(),
            item_code=str(data.get("item_code") or "").strip(),
            value="" if data.get("value") is None else str(data.get("value")).strip(),
            threshold_row=str(data.get("threshold_row") or "").strip(),
            notified_dept=str(data.get("notified_dept") or "").strip(),
            duty_person=str(data.get("duty_person") or "").strip(),
            due_minutes=minutes,
            receipt_status=str(data.get("receipt_status") or "").strip(),
            receipt_at=str(data.get("receipt_at") or "").strip(),
            extra=extra,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "message_control_id": self.message_control_id,
            "item_code": self.item_code,
            "value": self.value,
            "threshold_row": self.threshold_row,
            "notified_dept": self.notified_dept,
            "duty_person": self.duty_person,
            "due_minutes": self.due_minutes,
            "receipt_status": self.receipt_status,
            "receipt_at": self.receipt_at,
        }


@dataclass(frozen=True)
class RegistrationResult:
    """一次出站调用的结论。"""

    ok: bool
    status: int = 0
    receipts: tuple[ReceiptRecord, ...] = ()
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
            "receipts": [r.to_dict() for r in self.receipts],
        }


def _extract_receipts(payload: Any) -> tuple[ReceiptRecord, ...]:
    """把响应体里的回执挑出来。

    对端可能回**一个对象**（按 key 取）、**一个数组**（列全部），也可能
    包一层 ``{"data": [...]}`` / ``{"items": [...]}``。三种都认 —— 认不出来
    就当「没有回执」，由调用方按差异处理，而不是当成功。
    """
    if isinstance(payload, Mapping):
        for key in ("data", "items", "receipts", "records"):
            inner = payload.get(key)
            if isinstance(inner, list):
                payload = inner
                break
        else:
            payload = [payload]
    if not isinstance(payload, list):
        return ()
    return tuple(
        ReceiptRecord.from_payload(item)
        for item in payload
        if isinstance(item, Mapping)
    )


class RegistrationSystemClient:
    """登记系统 REST 客户端（读回执 / 写回执）。

    * 基地址：``CRITVAL_BASE_URL``（环境变量）；
    * 凭据：``CRITVAL_TOKEN``（环境变量），``Authorization: Bearer``；
    * 出错分级：``4xx`` 不可重试（契约错），``5xx`` / 超时可重试。
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        path: str = RECEIPT_PATH,
        opener: Optional[Any] = None,
    ) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.token = token or ""
        self.timeout = float(timeout)
        self.path = path if path.startswith("/") else f"/{path}"
        self._opener = opener or urllib.request.urlopen

    # ---- 构造 -------------------------------------------------------------
    @classmethod
    def from_config(cls, *, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> "RegistrationSystemClient":
        """按环境变量装配客户端；缺凭据位**立即报错**（不发起任何请求）。"""
        from critval.config import Config

        base_url = Config.get_env("CRITVAL_BASE_URL") or ""
        token = Config.get_env("CRITVAL_TOKEN") or ""
        if not base_url:
            raise RegistrationError(
                "缺少 CRITVAL_BASE_URL（登记系统根地址），无法发起出站调用"
            )
        if not token:
            raise RegistrationError(
                "缺少 CRITVAL_TOKEN（登记系统 Bearer 凭据），无法发起出站调用"
            )
        return cls(base_url, token, timeout=timeout)

    @property
    def endpoint(self) -> str:
        """完整目标地址。"""
        return f"{self.base_url}{self.path}"

    # ---- 出站 -------------------------------------------------------------
    def fetch_receipt(self, message_control_id: str) -> RegistrationResult:
        """按幂等键取一条回执（``GET …?message_control_id=…``）。"""
        query = urllib.parse.urlencode({"message_control_id": message_control_id})
        return self._request("GET", query=f"?{query}")

    def list_receipts(self) -> RegistrationResult:
        """列全部回执（``GET …``，不带 query）。

        对端不支持列表查询时返回 ``ok=False`` + ``status=404/405``；
        调用方据此**降级**（只做「按 key 逐条查」，放弃「远端有本地没有」
        这一类差异的检出），并在摘要里写明降级原因 —— 少查一类差异可以，
        假装查全了不行。
        """
        return self._request("GET")

    def post_receipt(self, payload: Mapping[str, Any]) -> RegistrationResult:
        """补写 / 重放一条回执（``POST …``）。

        载荷里必须带 ``message_control_id`` —— 它是幂等键，登记系统按它
        覆盖同一条，重放不会生成第二条。
        """
        body = dict(payload or {})
        if not str(body.get("message_control_id") or "").strip():
            raise RegistrationError("回写载荷缺 message_control_id（幂等键不能为空）")
        return self._request("POST", body=body)

    # ---- 内部 -------------------------------------------------------------
    def _request(
        self,
        method: str,
        *,
        query: str = "",
        body: Optional[Mapping[str, Any]] = None,
    ) -> RegistrationResult:
        if not self.base_url:
            raise RegistrationError("缺少 CRITVAL_BASE_URL，无法发起出站调用")
        if not self.token:
            raise RegistrationError("缺少 CRITVAL_TOKEN，无法发起出站调用")

        url = f"{self.endpoint}{query}"
        data: Optional[bytes] = None
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.token}",
        }
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"

        request = urllib.request.Request(url, data=data, method=method, headers=headers)
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
            return RegistrationResult(
                ok=False,
                status=exc.code,
                body=detail,
                error=f"登记系统返回 {exc.code}",
                retryable=exc.code >= 500 or exc.code in {408, 429},
                request_body=dict(body or {}),
            )
        except urllib.error.URLError as exc:
            return RegistrationResult(
                ok=False,
                status=0,
                error=f"登记系统不可达：{exc.reason}",
                retryable=True,
                request_body=dict(body or {}),
            )
        except TimeoutError as exc:
            return RegistrationResult(
                ok=False,
                status=0,
                error=f"登记系统调用超时（{self.timeout} 秒）：{exc}",
                retryable=True,
                request_body=dict(body or {}),
            )

        ok = 200 <= status < 300
        if not ok:
            return RegistrationResult(
                ok=False,
                status=status,
                body=text,
                error=f"登记系统返回 {status}",
                retryable=status >= 500 or status in {408, 429},
                request_body=dict(body or {}),
            )

        payload: Any = None
        if text.strip():
            try:
                payload = json.loads(text)
            except json.JSONDecodeError as exc:
                return RegistrationResult(
                    ok=False,
                    status=status,
                    body=text,
                    error=f"登记系统响应不是合法 JSON：{exc.msg}",
                    request_body=dict(body or {}),
                )
        return RegistrationResult(
            ok=True,
            status=status,
            receipts=_extract_receipts(payload),
            body=text,
            request_body=dict(body or {}),
        )


def build_receipt_payload(
    row: Mapping[str, Any],
    *,
    receipt_status: str = "acknowledged",
    receipt_at: str = "",
) -> dict[str, Any]:
    """台账事件行 → 回写载荷（字段按 :data:`RECEIPT_FIELDS`）。

    **只取白名单字段** —— 台账行里的患者标识不进载荷。``duty_person`` 取
    台账的 ``notify_target``（处置卡上的通知对象），``receipt_at`` 取值班人
    的确认时刻；两者都缺时留空，不编。
    """
    due = row.get("due_minutes")
    payload: dict[str, Any] = {
        "message_control_id": str(row.get("message_control_id") or "").strip(),
        "item_code": str(row.get("item_code") or "").strip(),
        "value": str(row.get("value") or "").strip(),
        "threshold_row": str(row.get("threshold_row") or "").strip(),
        "notified_dept": str(row.get("dept_code") or "").strip(),
        "duty_person": str(row.get("notify_target") or "").strip(),
        "due_minutes": int(due) if due not in (None, "") else None,
        "receipt_status": receipt_status,
        "receipt_at": receipt_at or str(row.get("acknowledged_at") or "").strip(),
    }
    return payload


def merge_receipts(records: Sequence[ReceiptRecord]) -> dict[str, ReceiptRecord]:
    """回执列表 → ``message_control_id`` 索引（后到的覆盖先到的）。

    同一个键出现多条时保留**最后一条** —— 登记系统按幂等键覆盖，最后一条
    才是当前事实。
    """
    index: dict[str, ReceiptRecord] = {}
    for record in records:
        key = record.message_control_id
        if not key:
            continue
        index[key] = record
    return index
