"""登记系统替身（测试数据层）。

**它只是测试替身，不是最终对接方式。** 院内危急值登记系统的真实接口按
spec.md 的契约是：

* ``GET  {CRITVAL_BASE_URL}/api/v1/critical-events?message_control_id=…``
* ``GET  {CRITVAL_BASE_URL}/api/v1/critical-events``
* ``POST {CRITVAL_BASE_URL}/api/v1/critical-events``（幂等键 ``message_control_id``）

本替身用**真实 socket + 真实 HTTP**实现上面三条契约，因此产品侧的出站
客户端（``critval.writeback.client``）在本机跑的就是真实协议调用 ——
被替身替代的只有对端系统本身。

替身放在 ``tests/fixtures/`` 下（不在产品代码目录里），产品代码不 import
它 —— 产品代码里没有「默认指向本机替身」的路径。
"""

from __future__ import annotations

import hmac
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

__all__ = ["RegistryDouble", "RegistryDoubleError"]


class RegistryDoubleError(RuntimeError):
    """替身启动 / 停止失败。"""


class _Handler(BaseHTTPRequestHandler):
    """登记系统替身的请求处理。"""

    protocol_version = "HTTP/1.1"

    @property
    def double(self) -> "RegistryDouble":
        return self.server.double  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        """静音 —— 测试输出里不要有访问日志。"""

    # ---- 工具 -------------------------------------------------------------
    def _authorized(self) -> bool:
        header = self.headers.get("Authorization") or ""
        parts = header.split(None, 1)
        if len(parts) != 2 or parts[0].lower() != "bearer":
            return False
        return hmac.compare_digest(
            parts[1].strip().encode("utf-8"),
            self.double.token.encode("utf-8"),
        )

    def _send(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _guard(self) -> bool:
        if self._authorized():
            return True
        self._send(401, {"error": "unauthorized", "detail": "缺少或错误的 Bearer 凭据"})
        return False

    def _fault(self, method: str) -> bool:
        """模拟对端故障（``status_code`` 非 200 时一律回该状态码）。

        ``post_status_code`` 只作用于 POST —— 用来测「读得到、写不通」
        （对账阶段正常、补写阶段失败）。
        """
        status = int(self.double.status_code or 200)
        if method == "POST" and self.double.post_status_code:
            status = int(self.double.post_status_code)
        if status == 200:
            return False
        self._send(status, {"error": "upstream_error", "detail": f"对端返回 {status}"})
        return True

    # ---- 路由 -------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path != self.double.path:
            self._send(404, {"error": "not_found", "detail": parsed.path})
            return
        if not self._guard():
            return
        if self._fault("GET"):
            return
        query = parse_qs(parsed.query)
        key = (query.get("message_control_id") or [""])[0]
        self.double.calls.append(("GET", self.path, ""))
        if key:
            if key in self.double.fault_keys:
                self._send(500, {"error": "upstream_error", "detail": f"{key} 查询失败"})
                return
            receipt = self.double.receipts.get(key)
            if receipt is None:
                self._send(404, {"error": "not_found", "detail": f"没有 {key} 的回执"})
                return
            self._send(200, receipt)
            return
        if not self.double.supports_list:
            self._send(405, {"error": "method_not_allowed", "detail": "不支持列表查询"})
            return
        self._send(200, list(self.double.receipts.values()))

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path != self.double.path:
            self._send(404, {"error": "not_found", "detail": parsed.path})
            return
        if not self._guard():
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8") if length else ""
        # 请求先记账再判故障 —— **失败的那次也要留下痕迹**，
        # 「首发失败 + 补写成功」这类断言才数得清出站调用次数。
        self.double.calls.append(("POST", self.path, raw))
        if self._fault("POST"):
            return
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            self._send(400, {"error": "invalid_json", "detail": "请求体不是合法 JSON"})
            return
        if not isinstance(payload, dict):
            self._send(400, {"error": "invalid_payload", "detail": "请求体必须是 JSON 对象"})
            return
        key = str(payload.get("message_control_id") or "").strip()
        if not key:
            self._send(422, {"error": "missing_idempotency_key", "detail": "缺 message_control_id"})
            return
        self.double.receipts[key] = payload
        self.double.posted.append(dict(payload))
        self._send(200, {"status": "accepted", "message_control_id": key})


class RegistryDouble:
    """本机起一个登记系统替身（真 socket · 真 HTTP）。"""

    def __init__(
        self,
        token: str = "test-registry-token-0001",
        *,
        path: str = "/api/v1/critical-events",
        supports_list: bool = True,
        status_code: int = 200,
    ) -> None:
        self.token = token
        self.path = path
        self.supports_list = supports_list
        #: 非 200 时，替身一律回该状态码（测 4xx / 5xx 分级用）。
        self.status_code = status_code
        #: 只作用于 POST 的故障状态码（测「读得到、写不通」）。
        self.post_status_code = 0
        self.receipts: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, str, str]] = []
        self.posted: list[dict[str, Any]] = []
        #: 这些消息控制号查询时一律回 500（测「查不到 ≠ 没有」）。
        self.fault_keys: set[str] = set()
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    # ---- 生命周期 ---------------------------------------------------------
    def start(self) -> "RegistryDouble":
        server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        server.double = self  # type: ignore[attr-defined]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self._server = server
        self._thread = thread
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def __enter__(self) -> "RegistryDouble":
        return self.start()

    def __exit__(self, *exc_info: object) -> None:
        self.stop()

    @property
    def base_url(self) -> str:
        if self._server is None:
            raise RegistryDoubleError("替身还没启动")
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    # ---- 造数据 -----------------------------------------------------------
    def seed(self, receipt: dict[str, Any]) -> dict[str, Any]:
        """预置一条回执。"""
        key = str(receipt.get("message_control_id") or "").strip()
        if not key:
            raise RegistryDoubleError("预置回执必须带 message_control_id")
        self.receipts[key] = dict(receipt)
        return receipt

    def get(self, message_control_id: str) -> Optional[dict[str, Any]]:
        return self.receipts.get(message_control_id)
