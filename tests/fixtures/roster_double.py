"""排班系统的**协议替身**（测试数据层）。

**它只是测试替身，不是最终对接方式。** 院内排班 / 值班名单系统的真实
接口按 spec.md 的契约是：

* ``GET {NURSE_ROSTER_BASE_URL}/api/v1/duty-roster?dept_code=…``
* 头 ``Authorization: Bearer $ROSTER_TOKEN``

本替身用**真实 socket + 真实 HTTP**实现上面的契约，因此产品侧的出站
客户端（``critval.roster.client``）在本机跑的就是真实协议调用 ——
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

__all__ = ["RosterDouble", "RosterDoubleError"]


class RosterDoubleError(RuntimeError):
    """替身启动 / 停止失败。"""


class _Handler(BaseHTTPRequestHandler):
    """排班系统替身的请求处理。"""

    protocol_version = "HTTP/1.1"

    @property
    def double(self) -> "RosterDouble":
        return self.server.double  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        """静音 —— 测试输出里不要有访问日志。"""

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

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        self.double.calls.append(("GET", self.path, self.headers.get("Authorization") or ""))
        if parsed.path != self.double.path:
            self._send(404, {"error": "not_found", "detail": parsed.path})
            return
        if not self._authorized():
            self._send(401, {"error": "unauthorized", "detail": "缺少或错误的 Bearer 凭据"})
            return
        if self.double.status_code != 200:
            self._send(
                self.double.status_code,
                {"error": "upstream_error", "detail": f"对端返回 {self.double.status_code}"},
            )
            return
        if self.double.raw_body is not None:
            body = self.double.raw_body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        query = parse_qs(parsed.query)
        dept_code = (query.get("dept_code") or [""])[0]
        people = self.double.rosters.get(dept_code)
        if people is None:
            self._send(200, {"data": []})
            return
        self._send(200, {"data": list(people)})


class RosterDouble:
    """本机起一个排班系统替身（真 socket · 真 HTTP）。"""

    def __init__(
        self,
        token: str = "test-roster-token-0001",
        *,
        path: str = "/api/v1/duty-roster",
        status_code: int = 200,
    ) -> None:
        self.token = token
        self.path = path
        #: 非 200 时一律回该状态码（测 4xx / 5xx 降级用）。
        self.status_code = status_code
        #: 直接指定响应体（测「响应不是合法 JSON」「换了响应外壳」用）。
        self.raw_body: Optional[str] = None
        #: 科室 → 当班人列表。
        self.rosters: dict[str, list[dict[str, Any]]] = {}
        #: 收到的请求（方法 / 路径 / Authorization 头）。
        self.calls: list[tuple[str, str, str]] = []
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    # ---- 生命周期 ---------------------------------------------------------
    def start(self) -> "RosterDouble":
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

    def __enter__(self) -> "RosterDouble":
        return self.start()

    def __exit__(self, *exc_info: object) -> None:
        self.stop()

    @property
    def base_url(self) -> str:
        if self._server is None:
            raise RosterDoubleError("替身还没启动")
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    # ---- 造数据 -----------------------------------------------------------
    def seed(self, dept_code: str, people: list[dict[str, Any]]) -> None:
        """预置某个科室的当班人。"""
        self.rosters[str(dept_code)] = list(people)

    def call_count(self) -> int:
        """收到过多少次请求（降级路径要能断言「确实发出去了」）。"""
        return len(self.calls)
