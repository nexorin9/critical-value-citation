"""企业微信群机器人的**协议替身**（测试数据层）。

**它只是测试替身，不是最终对接方式。** 真实值班群的群机器人 webhook 是
``https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=…``，本替身在本机
起一台同协议的服务器，把**对端**换掉 —— 产品侧跑的是真实 HTTP POST
（真实 JSON 体、真实 ``msgtype=markdown`` 约定、真实 ``errcode`` 判读）。

替身放在 ``tests/fixtures/`` 下（不在产品代码目录里），产品代码不 import
它 —— 产品代码里没有「默认指向本机替身」的路径。
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional

__all__ = ["WeComDouble", "RecordedWebhookRequest"]


@dataclass
class RecordedWebhookRequest:
    """一次收到的 webhook 请求。"""

    path: str
    body: bytes

    @property
    def json(self) -> Any:
        if not self.body:
            return None
        return json.loads(self.body.decode("utf-8"))

    @property
    def content(self) -> str:
        """企微 markdown 正文。"""
        payload = self.json
        if not isinstance(payload, dict):
            return ""
        markdown = payload.get("markdown")
        if not isinstance(markdown, dict):
            return ""
        return str(markdown.get("content", "") or "")

    @property
    def msgtype(self) -> str:
        payload = self.json
        if not isinstance(payload, dict):
            return ""
        return str(payload.get("msgtype", "") or "")


class WeComDouble:
    """一台临时的企微群机器人 webhook 端点。"""

    def __init__(self) -> None:
        #: HTTP 状态码（默认 200）。
        self.status: int = 200
        #: 企微业务错误码（默认 0 = ok）。**errcode != 0 即失败**。
        self.errcode: int = 0
        self.errmsg: str = "ok"
        #: 直接指定响应体（测「响应不是合法 JSON」用）。
        self.raw_body: Optional[str] = None
        #: 收到的请求。
        self.requests: list[RecordedWebhookRequest] = []
        #: 本机 webhook 地址（``start()`` 之后可用）。
        self.webhook_url: str = ""
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    # ---- 生命周期 --------------------------------------------------------
    def start(self) -> str:
        double = self

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self) -> None:  # noqa: N802 —— BaseHTTPRequestHandler 的约定
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                double.requests.append(
                    RecordedWebhookRequest(path=self.path, body=body)
                )
                if double.raw_body is not None:
                    payload = double.raw_body.encode("utf-8")
                else:
                    payload = json.dumps(
                        {"errcode": double.errcode, "errmsg": double.errmsg},
                        ensure_ascii=False,
                    ).encode("utf-8")
                self.send_response(double.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args: Any) -> None:  # 静音：别污染测试输出
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="wecom-double", daemon=True
        )
        self._thread.start()
        host, port = self._server.server_address[:2]
        self.webhook_url = f"http://{host}:{port}/cgi-bin/webhook/send?key=test-double-key"
        return self.webhook_url

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None

    def __enter__(self) -> "WeComDouble":
        self.webhook_url = self.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.stop()

    # ---- 断言助手 ---------------------------------------------------------
    @property
    def call_count(self) -> int:
        return len(self.requests)

    @property
    def last_content(self) -> str:
        return self.requests[-1].content if self.requests else ""
