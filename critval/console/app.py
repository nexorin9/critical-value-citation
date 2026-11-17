"""值班台 HTTP 承载：入站推送端点 + 值班台页面路由（任务 id 24 / 49）。

同进程承担两个方向的 HTTP：

* **入站** —— 院内 LIS 把危急值结果推过来（``POST /api/v1/critical-value-events``），
  请求与响应规则见 ``critval/console/inbound.py``；
* **出站页面** —— 值班人在值班台看事件台账、确认处置卡。页面路由与模板由
  任务 id 13 / 48 / 49 / 50 填充。

另有一条**只读**的运维出口（任务 id 42）：
``GET /api/v1/local-notifications`` —— 本机通知队列（企微未配置或重试耗尽时
处置卡的落点）。它带处置卡正文，**只认值班台凭据**（``CONSOLE_TOKEN``），
没配凭据位就一律 401。

**值班台路由表（任务 id 49）**：

====================== ============================================ ==========
路由                    内容                                          出口
====================== ============================================ ==========
``GET /``               值班台概览：当前待办计数                       HTML
``GET /events``         事件台账页：筛选 + 明细 + 分页                 HTML
``GET /events``         （?fragment=region 台账片段 / ?bootstrap=1    HTML
                        首屏加载态骨架）—— 任务 id 50
``GET /events/{id}``    事件详情：事实 / 出处 / 推送 / 审计             HTML
``GET /events/{id}/ack`` 处置卡确认页：四要素 / 冲突 / 确认 / 转人工    HTML
``POST /events/{id}/ack`` 人工确认 → acknowledged → 幂等回写           HTML
``POST /events/{id}/transfer`` 转人工 → transferred_to_human           HTML
``POST /events/{id}/replay`` 补写回写（重放一次出站回写）—— id 50     HTML
``GET /api/events``     台账列表 JSON（**字段白名单**）               JSON
``GET /api/events/{id}`` 事件详情 JSON（**字段白名单**）              JSON
====================== ============================================ ==========

**JSON 出口与页面出口分开**：页面给值班人看，接口给院内系统看；两者读同一份
台账与同一份处置卡快照，但接口的字段是白名单 —— 患者标识层字段一个都不给
（见 ``critval/console/api.py``）。``/api/v1/*`` 仍归入站服务层，与值班台
路由不冲突。

**零外部中间件**：只用 Python 标准库的 ``http.server``。检验科值班机上装个
Python 就能跑，不要求院方先装网关或应用服务器 —— 这是本产品能被写进
采购清单的前提之一。

**监听地址与凭据全部从配置对象读**（同构源参考
``github_ref/RAGFlow/api/db/runtime_config.py`` 的 ``HTTP_PORT`` /
``init_config`` / ``init_env`` / ``get_env`` 语义：服务监听端口来自配置对象，
不硬编码在代码里；配置在运行期整体切换，业务代码不直接读进程状态）。
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional

from critval.config import Config, install_log_redaction
from critval.console.api import API_PATH, API_PATH_PREFIX
from critval.console.inbound import (
    DEFAULT_MAX_BODY_BYTES,
    DEFAULT_RATE_LIMIT_PER_MINUTE,
    HEALTHZ_PATH,
    INBOUND_PATH,
    LOCAL_NOTIFICATIONS_PATH,
    InboundRequest,
    InboundService,
    LedgerInboundProcessor,
)
from critval.judge.registry import RuleRegistry
from critval.kb.loader import load_threshold_table
from critval.ledger.store import LedgerStore
from critval.notify.local import LocalNotifier

__all__ = [
    "CONSOLE_ROUTES",
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "build_service",
    "is_console_api_path",
    "make_server",
    "serve",
]

logger = logging.getLogger("critval.console")

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8760

#: 值班台路由表（**给人看的那一份**；实现分散在 ``console/pages.py`` 的
#: ``ConsoleRouter`` 与本模块的承载层）。改路由时这张表要跟着改 ——
#: 它是「值班台到底有哪些入口」的唯一说明。
CONSOLE_ROUTES: tuple[tuple[str, str, str], ...] = (
    ("GET", "/", "值班台概览：当前待办计数", "HTML"),
    (
        "GET",
        "/events",
        "事件台账页：筛选（科室 / 项目 / 状态 / 超期 / 回写）+ 分页"
        "（?fragment=region 取台账片段，?bootstrap=1 首屏骨架）",
        "HTML",
    ),
    ("GET", "/events/{id}", "事件详情：事实 / 依据出处 / 推送记录 / 审计流水", "HTML"),
    ("GET", "/events/{id}/ack", "处置卡确认页：四要素 / 口径冲突 / 确认 / 转人工 / 补写", "HTML"),
    ("POST", "/events/{id}/ack", "人工确认：acknowledged + 幂等回写登记系统", "HTML"),
    ("POST", "/events/{id}/transfer", "转人工：transferred_to_human，原因进审计", "HTML"),
    ("POST", "/events/{id}/replay", "补写回写：重放一次出站回写，原因进审计", "HTML"),
    ("GET", API_PATH, "台账列表 JSON（字段白名单）", "JSON"),
    ("GET", f"{API_PATH_PREFIX}/{{id}}", "事件详情 JSON（字段白名单）", "JSON"),
)


def is_console_api_path(path: str) -> bool:
    """这个路径归值班台的 **JSON 接口** 吗？

    ``/api/v1/*``（入站推送与本机通知队列）**不算** —— 那两条归
    ``console/inbound.py`` 的服务层。
    """
    clean = str(path or "").split("?", 1)[0]
    return clean == API_PATH or clean.startswith(API_PATH_PREFIX + "/")


# ---------------------------------------------------------------------------
# 服务装配
# ---------------------------------------------------------------------------


def build_service(
    *,
    db_path: Optional[str] = None,
    threshold_path: Optional[str] = None,
    token: Optional[str] = None,
    max_body_bytes: Optional[int] = None,
    rate_limit_per_minute: Optional[int] = None,
) -> InboundService:
    """按配置装配入站服务。

    凭据从环境变量读（``LIS_CRITICAL_TOKEN``），**服务端没配就一律拒绝** ——
    不给默认口令。阈值表路径取 ``CRITVAL_THRESHOLD_PATH``；配了就编译成规则集，
    判定按规则集走；没配则判定结论为「查不到阈值 → 转人工」（不猜）。
    """
    Config.init_env()

    resolved_db = db_path or Config.get_env("CRITVAL_DB_PATH") or "./data/critval.db"
    resolved_token = token if token is not None else (Config.get_env("LIS_CRITICAL_TOKEN") or "")
    resolved_threshold = threshold_path or Config.get_env("CRITVAL_THRESHOLD_PATH") or ""
    resolved_max = (
        max_body_bytes
        if max_body_bytes is not None
        else int(Config.get_env("CRITVAL_INBOUND_MAX_BODY_BYTES") or DEFAULT_MAX_BODY_BYTES)
    )
    resolved_rate = (
        rate_limit_per_minute
        if rate_limit_per_minute is not None
        else int(
            Config.get_env("CRITVAL_INBOUND_RATE_LIMIT_PER_MINUTE")
            or DEFAULT_RATE_LIMIT_PER_MINUTE
        )
    )

    rows: list[Any] = []
    if resolved_threshold and Path(resolved_threshold).exists():
        table = load_threshold_table(resolved_threshold)
        rows = list(table.rows)
        RuleRegistry.init_config(resolved_threshold)
        logger.info(
            "阈值表已装载：%s（%d 行 · 规则集 %s）",
            table.source_name,
            len(rows),
            RuleRegistry.snapshot_id(),
        )
    elif resolved_threshold:
        logger.warning("阈值表不存在：%s（判定将转人工）", resolved_threshold)
    else:
        logger.warning("未配置 CRITVAL_THRESHOLD_PATH，判定将转人工")

    store = LedgerStore(resolved_db)
    processor = LedgerInboundProcessor(
        store,
        ruleset_provider=RuleRegistry.get_ruleset,
        rows_provider=lambda: rows,
    )
    service = InboundService(
        token=resolved_token,
        store=store,
        processor=processor,
        max_body_bytes=resolved_max,
        rate_limit_per_minute=resolved_rate,
        # 值班台凭据与本机通知队列（任务 id 42）：队列里有处置卡正文，
        # 只对值班台开放；没配 CONSOLE_TOKEN 就一律 401。
        console_token=Config.get_env("CONSOLE_TOKEN") or "",
        local_queue=LocalNotifier.from_config(),
    )
    if not resolved_token:
        logger.error("未配置 LIS_CRITICAL_TOKEN —— 入站推送一律拒绝（401）")

    # 值班台页面路由：**没挂就补一个默认的**。
    #
    # 守护进程会用自己的路由覆盖它（带确认回写实现）；独立跑
    # ``console-serve`` 时没有守护进程，页面仍然要能打开 —— 只是确认动作
    # 没有回写通道（页面会明说「确认通道未接入」，不假装成功）。
    if getattr(service, "console_router", None) is None:
        from critval.console.pages import ConsoleRouter

        service.console_router = ConsoleRouter(
            store,
            token=Config.get_env("CONSOLE_TOKEN") or "",
            snapshot_dir=Config.get_env("CRITVAL_SNAPSHOT_DIR") or "",
            base_url="",
            threshold_path=resolved_threshold,
        )
    return service


# ---------------------------------------------------------------------------
# HTTP 适配
# ---------------------------------------------------------------------------


class _InboundHandler(BaseHTTPRequestHandler):
    """把 socket 请求翻译成服务层的 ``InboundRequest``。"""

    server_version = "critval/0.1"
    protocol_version = "HTTP/1.1"

    # ---- 路由 -------------------------------------------------------------
    def do_POST(self) -> None:  # noqa: N802 - 标准库约定的命名
        self._dispatch("POST")

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_PUT(self) -> None:  # noqa: N802
        self._dispatch("PUT")

    def do_DELETE(self) -> None:  # noqa: N802
        self._dispatch("DELETE")

    # ---- 实现 -------------------------------------------------------------
    def _dispatch(self, method: str) -> None:
        service: InboundService = self.server.critval_service  # type: ignore[attr-defined]
        router = getattr(service, "console_router", None)
        headers = {str(key).lower(): str(value) for key, value in self.headers.items()}
        try:
            size = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            size = 0

        # 健康检查（任务 id 46）：只回计数与依赖可达性，不带处置卡正文，
        # 因此**不设值班台凭据**（值班机的监控探针拿不到 token）。
        clean_path = self.path.split("?", 1)[0]
        if clean_path == HEALTHZ_PATH:
            payload = service.health_payload()
            # degraded 仍算「进程还在动」→ 200（重启治不好依赖不通）；
            # 只有真正 down 才 503。
            status_code = 503 if payload.get("status") == "down" else 200
            self._respond(status_code, payload)
            return

        # 值班台路由（任务 id 13 / 49）先于入站端点分流：
        #
        #   * ``/api/events*``  → JSON 接口（**字段白名单**，见 console/api.py）
        #   * 其余值班台路径     → 服务端渲染 HTML
        #
        # ``/api/v1/*``（入站推送端点与本机通知队列）**不在**值班台路由里 ——
        # 那两条归 ``console/inbound.py`` 的服务层，与页面互不干扰。
        if router is not None and router.handles(self.path):
            body = self.rfile.read(size) if size > 0 else b""
            response = router.handle(method, self.path, headers=headers, body=body)
            logger.info(
                "值班台路由命中：%s %s（%s，出口 %s）",
                method,
                clean_path,
                response.status,
                "JSON" if is_console_api_path(clean_path) else "HTML",
            )
            self._respond_raw(
                response.status,
                response.body,
                response.headers,
                response.content_type,
            )
            return

        if size > service.max_body_bytes:
            # 体积超限：**不把 body 读进内存**，直接拒并断开连接。
            self.close_connection = True
            self._respond(
                413,
                {
                    "error": "payload_too_large",
                    "detail": (
                        f"请求体 {size} 字节超过上限 {service.max_body_bytes} 字节"
                    ),
                },
                close=True,
            )
            return

        body = self.rfile.read(size) if size > 0 else b""
        client = ""
        if self.client_address:
            client = str(self.client_address[0])

        request = InboundRequest(
            method=method,
            path=self.path,
            headers=headers,
            body=body,
            client_addr=client,
        )
        response = service.handle(request)
        self._respond(response.status, response.payload, response.headers)

    def _respond(
        self,
        status: int,
        payload: dict[str, Any],
        extra_headers: Optional[dict[str, str]] = None,
        close: bool = False,
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        if close:
            self.close_connection = True
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _respond_html(
        self,
        status: int,
        body: str,
        extra_headers: Optional[dict[str, str]] = None,
        content_type: str = "text/html; charset=utf-8",
    ) -> None:
        """值班台页面响应（HTML 而非 JSON）。

        ``content_type`` 由路由决定 —— 静态样式表走 ``text/css``，否则浏览器
        会把 CSS 当 HTML 解析，页面样式直接失效。
        """
        self._respond_raw(status, body, extra_headers, content_type)

    def _respond_raw(
        self,
        status: int,
        body: str,
        extra_headers: Optional[dict[str, str]] = None,
        content_type: str = "text/html; charset=utf-8",
    ) -> None:
        """按原始正文回响应（HTML 页面与 JSON 接口共用一条路）。

        正文已经是成品（HTML 片段或 JSON 文本），这里**不再序列化** ——
        JSON 接口的正文由 ``critval/console/api.py`` 序列化好（键序固定、
        中文不转义），这一层只负责写字节。
        """
        payload = str(body or "").encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(payload)

    # ---- 日志 -------------------------------------------------------------
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        logger.info("%s %s", self.address_string(), format % args)

    def log_error(self, format: str, *args: Any) -> None:  # noqa: A002
        logger.warning("%s %s", self.address_string(), format % args)


class ConsoleServer(ThreadingHTTPServer):
    """值班台服务承载（线程化：推送端点与页面互不阻塞）。"""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], service: InboundService) -> None:
        super().__init__(address, _InboundHandler)
        self.critval_service = service


def make_server(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    *,
    service: Optional[InboundService] = None,
    **service_kwargs: Any,
) -> ConsoleServer:
    """装配并绑定服务（不进入服务循环）。"""
    resolved = service or build_service(**service_kwargs)
    return ConsoleServer((host, int(port)), resolved)


def serve(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    *,
    service: Optional[InboundService] = None,
    **service_kwargs: Any,
) -> None:
    """启动服务并进入服务循环（常驻）。"""
    install_log_redaction()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    server = make_server(host, port, service=service, **service_kwargs)
    bound_host, bound_port = server.server_address[:2]
    # 确认链接要能从聊天工具里直接点开 —— 补上本机 base URL（守护进程那边
    # 自己带 base_url，这里只在它为空时补）。
    router = getattr(getattr(server, "critval_service", None), "console_router", None)
    if router is not None and not getattr(router, "base_url", ""):
        router.base_url = f"http://{bound_host}:{bound_port}"
    logger.info("值班台已启动：http://%s:%s", bound_host, bound_port)
    logger.info("入站推送端点：POST %s", INBOUND_PATH)
    logger.info("本机通知队列：GET %s（需值班台凭据）", LOCAL_NOTIFICATIONS_PATH)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("收到中断，准备退出")
    finally:
        server.server_close()
        store = getattr(server.critval_service, "store", None)
        if store is not None and hasattr(store, "close"):
            store.close()


def main(argv: Optional[list[str]] = None) -> int:
    """``python -m critval.console.app`` 入口。"""
    parser = argparse.ArgumentParser(
        prog="python -m critval.console.app",
        description="危急值处置依据检索系统 · 值班台与入站推送端点",
    )
    parser.add_argument("--host", default=DEFAULT_HOST, help="监听地址（默认 127.0.0.1）")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="监听端口（默认 8760）")
    parser.add_argument("--db", default=None, help="SQLite 台账路径（默认取 CRITVAL_DB_PATH）")
    parser.add_argument(
        "--threshold", default=None, help="院内阈值表 xlsx 路径（默认取 CRITVAL_THRESHOLD_PATH）"
    )
    args = parser.parse_args(argv)

    service = build_service(db_path=args.db, threshold_path=args.threshold)
    serve(args.host, args.port, service=service)
    return 0


if __name__ == "__main__":
    sys.exit(main())
