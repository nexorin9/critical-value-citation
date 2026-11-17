"""LIS REST 回调的**协议替身**（测试数据层）。

用途：在测试里替掉院内 LIS 的 ``POST /api/v1/critical-value-events`` 回调端点
—— 记录它收到的请求（路径 / 方法 / 头 / 体），并按配置返回状态码。

**边界**：这是**测试替身**，不是最终集成面。产品代码里跑的是真实 HTTP 协议
（真实 URL、真实 ``Authorization: Bearer`` 头、真实 JSON 体）；替身只负责把
对端换成一台本机临时服务器，好让「请求确实发出去了、头确实带上了、体确实
是约定的字段」这件事可以被断言。

真实院内 LIS 端点不在本仓库内 —— 联调时把 ``LIS_BASE_URL`` 指到院内地址、
``LIS_CRITICAL_TOKEN`` 指到网管给的共享凭据即可。
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional

__all__ = ["LisRestDouble", "RecordedRequest"]


@dataclass
class RecordedRequest:
    """一次收到的请求。"""

    method: str
    path: str
    headers: dict[str, str]
    body: bytes

    @property
    def json(self) -> Any:
        """请求体（按 JSON 解析）。"""
        if not self.body:
            return None
        return json.loads(self.body.decode("utf-8"))

    @property
    def authorization(self) -> str:
        for key, value in self.headers.items():
            if key.lower() == "authorization":
                return value
        return ""


@dataclass
class LisRestDouble:
    """一台临时的 LIS 回调端点。"""

    status: int = 200
    response_body: str = '{"ok":true}'
    requests: list[RecordedRequest] = field(default_factory=list)
    _server: Optional[ThreadingHTTPServer] = None
    _thread: Optional[threading.Thread] = None

    # ---- 生命周期 --------------------------------------------------------
    def start(self) -> str:
        """起服务并返回基地址。"""
        double = self

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self) -> None:  # noqa: N802 —— BaseHTTPRequestHandler 的约定
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                double.requests.append(
                    RecordedRequest(
                        method="POST",
                        path=self.path,
                        headers={k: v for k, v in self.headers.items()},
                        body=body,
                    )
                )
                payload = double.response_body.encode("utf-8")
                self.send_response(double.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args: Any) -> None:  # 静音：别污染测试输出
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="lis-rest-double", daemon=True
        )
        self._thread.start()
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None

    def __enter__(self) -> "LisRestDouble":
        self.base_url = self.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.stop()
