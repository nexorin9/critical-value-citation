"""入站 HTTP 推送端点的服务层（任务 id 24）。

院内 LIS 推危急值有两条路：一条是 HL7 v2 ``ORU^R01`` 走 MLLP（主通道），
一条是直接 HTTP 推送（辅通道）。本模块是辅通道的**服务层** —— 它不碰
socket，只处理「一次请求 → 一个结论」；socket 适配在 ``critval/console/app.py``。

一次请求的处理顺序（顺序本身就是边界）：

1. **路由** —— 只有 ``POST /api/v1/critical-value-events``；
2. **体积上限** —— 超限直接拒，不解析（挡住超大 body 打满内存）；
3. **Content-Type 校验** —— 只收 ``application/json``；
4. **鉴权** —— ``Authorization: Bearer $LIS_CRITICAL_TOKEN``，常量时间比较；
5. **限流** —— 按来源地址限流（放在鉴权之后：限的是「判定 + 出卡」这段
   昂贵处理，不该让一次未授权的洪水把院内 LIS 的配额吃掉）；
6. **JSON 解析 + 载荷体检** —— 患者标识字段当场拒绝；
7. **规范化** —— **与 MLLP 路径共用** ``critval.hl7.normalize`` 的同一套规范函数；
8. **落台账 → 判定 → 装配处置卡** —— 重复推送只跳过，不重复判定、不重复推送。

**凭据从哪来**：``LIS_CRITICAL_TOKEN``（环境变量）。服务端没配这个凭据位时
**一律拒绝**，不给默认口令 —— 缺就是缺，不给兜底假值。

**为什么未授权也要写审计**：值班台要能回答「今天有没有人乱推我们的接口」。
审计只记来源地址、路径与时刻，**不记 token 明文**。
"""

from __future__ import annotations

import hmac
import json
import threading
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Optional

from critval.hl7.normalize import normalize_http_payload
from critval.judge.ambiguity import check_ambiguity
from critval.judge.critical import (
    DECISION_AMBIGUOUS_OBSERVATION,
    DECISION_CRITICAL,
    DECISION_STATUS,
    judge_event,
)
from critval.judge.explain import build_judgement_record
from critval.judge.supersede import apply_supersede
from critval.models import CriticalEvent, PolicyClause

__all__ = [
    "DEFAULT_MAX_BODY_BYTES",
    "DEFAULT_RATE_LIMIT_PER_MINUTE",
    "HEALTHZ_PATH",
    "INBOUND_PATH",
    "InboundOutcome",
    "InboundRequest",
    "InboundResponse",
    "InboundService",
    "LedgerInboundProcessor",
    "LOCAL_NOTIFICATIONS_PATH",
    "RateLimiter",
    "verify_bearer",
]

#: 入站推送端点路径（与 spec.md「入站辅通道」一致）。
INBOUND_PATH = "/api/v1/critical-value-events"

#: 本机通知队列查询路径（任务 id 42）—— 值班台看「哪条推不出去、卡在这儿」。
LOCAL_NOTIFICATIONS_PATH = "/api/v1/local-notifications"

#: 健康检查路径（任务 id 46）—— 监控探针读它判断「进程还在不在动」。
HEALTHZ_PATH = "/healthz"

#: 请求体上限（字节）。危急值推送是一条结果，64 KiB 足够；超了就是异常流量。
DEFAULT_MAX_BODY_BYTES = 64 * 1024

#: 单来源地址每分钟允许的处理次数。
DEFAULT_RATE_LIMIT_PER_MINUTE = 120


# ---------------------------------------------------------------------------
# 请求 / 响应
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InboundRequest:
    """一次入站请求（与 socket 无关，便于直接单测）。"""

    method: str = "POST"
    path: str = INBOUND_PATH
    headers: Mapping[str, str] = field(default_factory=dict)
    body: bytes = b""
    client_addr: str = ""


@dataclass(frozen=True)
class InboundResponse:
    """一次入站请求的结论。"""

    status: int
    payload: dict[str, Any] = field(default_factory=dict)
    headers: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class InboundOutcome:
    """事件处理结论（服务层 → 处理器的返回）。"""

    event_id: str
    status: str = "accepted"  # accepted | duplicate | not_critical | transferred_to_human
    duplicate: bool = False
    decision: str = ""
    card_id: str = ""
    threshold_row: str = ""
    missing_elements: tuple[str, ...] = ()
    ruleset_snapshot_id: str = ""
    config_version: int = 0


# ---------------------------------------------------------------------------
# 鉴权与限流
# ---------------------------------------------------------------------------


def verify_bearer(headers: Mapping[str, str], expected_token: str) -> bool:
    """校验 ``Authorization: Bearer <token>``。

    用 ``hmac.compare_digest`` 做**常量时间**比较 —— 逐字符比较的写法会
    让比对耗时随前缀匹配长度变化，攻击者可以据此一位一位试出凭据。
    """
    if not expected_token:
        return False
    raw = ""
    for key, value in headers.items():
        if str(key).strip().lower() == "authorization":
            raw = value or ""
            break
    parts = raw.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return False
    presented = parts[1].strip()
    if not presented:
        return False
    return hmac.compare_digest(
        presented.encode("utf-8"), expected_token.encode("utf-8")
    )


class RateLimiter:
    """按来源地址的固定窗口限流。

    固定窗口而不是滑动窗口：实现简单、内存可控，院内 LIS 的推送量是
    「几十条 / 分钟」这个量级，窗口边界的抖动不影响业务。
    """

    def __init__(
        self,
        limit: int = DEFAULT_RATE_LIMIT_PER_MINUTE,
        window_seconds: int = 60,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.limit = max(0, int(limit))
        self.window_seconds = max(1, int(window_seconds))
        self._clock = clock
        self._hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> tuple[bool, int]:
        """返回 ``(是否放行, 建议重试秒数)``。"""
        if self.limit <= 0:
            return True, 0
        now = self._clock()
        with self._lock:
            hits = self._hits.setdefault(key, [])
            cutoff = now - self.window_seconds
            while hits and hits[0] <= cutoff:
                hits.pop(0)
            if len(hits) >= self.limit:
                retry_after = max(1, int(self.window_seconds - (now - hits[0])) + 1)
                return False, retry_after
            hits.append(now)
            if len(self._hits) > 1024:
                self._prune(cutoff)
            return True, 0

    def _prune(self, cutoff: float) -> None:
        """清掉窗口内没有命中的来源地址，避免字典无限增长。"""
        for key in [k for k, hits in self._hits.items() if not hits or hits[-1] <= cutoff]:
            self._hits.pop(key, None)


# ---------------------------------------------------------------------------
# 处理器
# ---------------------------------------------------------------------------


class LedgerInboundProcessor:
    """默认处理器：落台账 → 判定 → 装配处置卡 → 落卡与引用。

    **重复推送在这里被挡住**：``record_event`` 返回 ``False`` 说明幂等键
    已存在 —— 此时**不判定、不出卡、不推送**，既有事实不被覆盖。
    """

    def __init__(
        self,
        store: Any,
        *,
        ruleset_provider: Optional[Callable[[], Any]] = None,
        rows_provider: Optional[Callable[[], Any]] = None,
        clause_provider: Optional[Callable[[], Optional[PolicyClause]]] = None,
    ) -> None:
        self.store = store
        self.ruleset_provider = ruleset_provider
        self.rows_provider = rows_provider
        self.clause_provider = clause_provider

    def _persist_judgement(self, event: CriticalEvent, judgement: Any) -> None:
        """落判定结论 + **判定链**（任务 id 36）。

        判定记录是「为什么这么判」的可复核凭据 —— 结论写在事件行上，
        过程写在 ``judgement_records`` 表里，事后按事件 ID 就能逐步复算。
        """
        self.store.record_judgement(event.event_id, judgement)
        record = build_judgement_record(event, judgement)
        self.store.record_judgement_record(record)

    def __call__(self, event: CriticalEvent) -> InboundOutcome:
        if not self.store.record_event(event):
            # 同一帧并发到达时只记一条审计（判重在写事务里做）——
            # 重复投递的**次数**由幂等键命中计数承载，不灌大审计流水。
            self.store.append_audit_once(
                "event_duplicate",
                event_id=event.event_id,
                dept_code=event.dept_code,
                specimen_no=event.specimen_no,
                item_code=event.item_code,
                message_control_id=event.message_control_id,
                detail="幂等键已存在，本次跳过（既有事实不被覆盖，不重复判定、不重复推送）",
            )
            return InboundOutcome(
                event_id=event.event_id,
                status="duplicate",
                duplicate=True,
            )
        ruleset = self.ruleset_provider() if self.ruleset_provider else None
        rows = list(self.rows_provider()) if self.rows_provider else []

        # 修正结果（OBR-25=C）：先接上取代链（任务 id 9）—— 旧事件置
        # ``superseded``（终态），再对修正后的值重新判定。
        supersede = apply_supersede(self.store, event)
        if supersede.applied:
            event = replace(event, supersedes=supersede.old_event_id)

        judgement = judge_event(event, rows, ruleset=ruleset)

        # 互斥值检测（任务 id 9）：同标本同项目同一时刻出现两个互斥结果 →
        # 不自动判定、不出卡、转人工裁决。
        ambiguity = check_ambiguity(self.store, event, rows)
        if ambiguity is not None:
            judgement = replace(
                judgement,
                decision=DECISION_AMBIGUOUS_OBSERVATION,
                reason=f"{ambiguity.reason}；{judgement.reason}",
            )
            self._persist_judgement(event, judgement)
            self.store.set_status(
                event.event_id, DECISION_STATUS[DECISION_AMBIGUOUS_OBSERVATION]
            )
            self.store.append_audit(
                "ambiguous_observation",
                event_id=event.event_id,
                actor="judge",
                dept_code=event.dept_code,
                specimen_no=event.specimen_no,
                item_code=event.item_code,
                message_control_id=event.message_control_id,
                detail=ambiguity.reason,
            )
            return InboundOutcome(
                event_id=event.event_id,
                status=DECISION_STATUS[DECISION_AMBIGUOUS_OBSERVATION],
                decision=DECISION_AMBIGUOUS_OBSERVATION,
                ruleset_snapshot_id=judgement.ruleset_snapshot_id,
                config_version=judgement.config_version,
            )

        if supersede.conflict:
            # 迟到的修正结果撞上已被人工处置的旧事件：不改写既成事实，
            # 但这条冲突要跟着判定结论一起留在台账里。
            judgement = replace(
                judgement,
                reason=f"{judgement.reason}；{supersede.reason}",
            )

        self._persist_judgement(event, judgement)
        self.store.set_status(
            event.event_id, DECISION_STATUS.get(judgement.decision, "judged")
        )

        if judgement.decision != DECISION_CRITICAL:
            # 不危急 / 转人工：**只入台账**，不出卡、不推送。
            return InboundOutcome(
                event_id=event.event_id,
                status=DECISION_STATUS.get(judgement.decision, "judged"),
                decision=judgement.decision,
                ruleset_snapshot_id=judgement.ruleset_snapshot_id,
                config_version=judgement.config_version,
            )

        if not event.item_name and judgement.threshold_row is not None:
            event = replace(event, item_name=judgement.threshold_row.item_name)

        clause = self.clause_provider() if self.clause_provider else None
        from critval.card.builder import build_card

        card = build_card(event, judgement, policy_clause=clause)
        self.store.record_card(card)
        return InboundOutcome(
            event_id=event.event_id,
            status="accepted",
            decision=judgement.decision,
            card_id=card.card_id,
            threshold_row=(
                judgement.threshold_row.citation.locator
                if judgement.threshold_row is not None
                else ""
            ),
            missing_elements=card.missing_elements(),
            ruleset_snapshot_id=judgement.ruleset_snapshot_id,
            config_version=judgement.config_version,
        )


# ---------------------------------------------------------------------------
# 服务层
# ---------------------------------------------------------------------------


class InboundService:
    """入站 HTTP 推送端点的服务层。"""

    def __init__(
        self,
        *,
        token: str,
        store: Any,
        processor: Optional[Callable[[CriticalEvent], InboundOutcome]] = None,
        max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
        rate_limit_per_minute: int = DEFAULT_RATE_LIMIT_PER_MINUTE,
        clock: Callable[[], float] = time.monotonic,
        console_token: str = "",
        local_queue: Optional[Any] = None,
        console_router: Optional[Any] = None,
    ) -> None:
        self.token = token or ""
        self.store = store
        self.processor = processor
        self.max_body_bytes = max(1, int(max_body_bytes))
        self.limiter = RateLimiter(rate_limit_per_minute, clock=clock)
        #: 值班台访问凭据（``CONSOLE_TOKEN``）—— 本机通知队列只对它开放。
        self.console_token = console_token or ""
        #: 本机通知队列（任务 id 42）：企微推不出去时处置卡的落点。
        self.local_queue = local_queue
        #: 值班台页面路由（任务 id 13）。给 None 时本服务只管入站推送端点，
        #: 页面路由由 HTTP 适配层另行分流 —— 两条路互不阻塞。
        self.console_router = console_router
        #: 健康摘要提供方（任务 id 46）。守护进程在位时给出完整摘要
        #: （状态 + 关键计数 + 依赖可达性）；独立跑值班台服务时用最小摘要。
        self.health_provider: Optional[Callable[[], dict[str, Any]]] = None

    # ---- 健康检查 ---------------------------------------------------------
    def health_payload(self) -> dict[str, Any]:
        """健康摘要（``/healthz`` 的响应体）。

        **只回答「流水线还在不在动」**，不判断业务对不对 —— 判定与处置对错
        是台账与值班台的事。取摘要失败时**如实降级**为 ``degraded``，
        不假装健康。
        """
        if self.health_provider is not None:
            try:
                payload = dict(self.health_provider() or {})
            except Exception as exc:  # noqa: BLE001 —— 健康检查自己不该把服务带倒
                return {
                    "status": "degraded",
                    "version": "",
                    "detail": f"健康摘要取不到：{type(exc).__name__}: {exc}",
                }
            payload.setdefault("status", "ok")
            return payload
        return {
            "status": "ok",
            "version": "0.1",
            "mode": "console-only",
            "detail": "值班台独立运行（未装配守护进程）：只做页面与入站，没有心跳与队列",
            "queue": {"depth": 0, "capacity": 0},
            "counters": {},
            "status_counts": {},
            "dependencies": {},
            "heartbeat": {"written": False},
        }

    # ---- 主入口 -----------------------------------------------------------
    def handle(self, request: InboundRequest) -> InboundResponse:
        """处理一次入站请求，返回结论。"""
        path = request.path.split("?", 1)[0]
        if path == LOCAL_NOTIFICATIONS_PATH:
            return self._handle_local_notifications(request)
        if path != INBOUND_PATH:
            return InboundResponse(
                404,
                {
                    "error": "not_found",
                    "detail": f"未知路径 {path}；本端点只接受 {INBOUND_PATH}",
                },
            )
        if request.method.upper() != "POST":
            return InboundResponse(
                405,
                {
                    "error": "method_not_allowed",
                    "detail": f"本端点只接受 POST，收到 {request.method}",
                },
                {"Allow": "POST"},
            )

        if len(request.body) > self.max_body_bytes:
            return InboundResponse(
                413,
                {
                    "error": "payload_too_large",
                    "detail": (
                        f"请求体 {len(request.body)} 字节超过上限 "
                        f"{self.max_body_bytes} 字节"
                    ),
                },
            )

        content_type = ""
        for key, value in request.headers.items():
            if str(key).strip().lower() == "content-type":
                content_type = (value or "").split(";", 1)[0].strip().lower()
                break
        if content_type != "application/json":
            return InboundResponse(
                415,
                {
                    "error": "unsupported_media_type",
                    "detail": f"本端点只接受 application/json，收到 {content_type or '空'}",
                },
            )

        if not self.token:
            self._audit_unauthorized(request, "服务端未配置 LIS_CRITICAL_TOKEN")
            return InboundResponse(
                401,
                {
                    "error": "unauthorized",
                    "detail": "服务端未配置 LIS_CRITICAL_TOKEN，请先补齐该凭据位",
                },
            )
        if not verify_bearer(request.headers, self.token):
            self._audit_unauthorized(request, "Bearer 凭据缺失或不匹配")
            return InboundResponse(
                401,
                {
                    "error": "unauthorized",
                    "detail": "缺少或错误的 Bearer 凭据",
                },
            )

        allowed, retry_after = self.limiter.allow(request.client_addr or "-")
        if not allowed:
            return InboundResponse(
                429,
                {
                    "error": "rate_limited",
                    "detail": (
                        f"来源 {request.client_addr or '-'} 的推送频率超过 "
                        f"{self.limiter.limit} 次/分钟，请稍后重试"
                    ),
                },
                {"Retry-After": str(retry_after)},
            )

        try:
            payload = json.loads(request.body.decode("utf-8"))
        except UnicodeDecodeError:
            return InboundResponse(
                400, {"error": "invalid_encoding", "detail": "请求体不是 UTF-8 编码"}
            )
        except json.JSONDecodeError as exc:
            return InboundResponse(
                400, {"error": "invalid_json", "detail": f"请求体不是合法 JSON：{exc.msg}"}
            )
        if not isinstance(payload, dict):
            return InboundResponse(
                400,
                {"error": "invalid_payload", "detail": "请求体必须是 JSON 对象"},
            )

        event, unparsed = normalize_http_payload(payload)
        if event is None:
            reason = unparsed.reason if unparsed else "unparsed"
            detail = unparsed.detail if unparsed else "载荷无法规范化"
            self.store.append_audit(
                "unparsed_event",
                actor="lis",
                message_control_id=str(payload.get("event_id") or "").strip(),
                item_code=str(payload.get("item_code") or "").strip(),
                detail=f"{reason}：{detail}",
            )
            return InboundResponse(
                422,
                {
                    "error": "unparsed_event",
                    "reason": reason,
                    "detail": detail,
                },
            )

        outcome = self._process(event)
        return self._render(outcome)

    # ---- 内部 -------------------------------------------------------------
    def _handle_local_notifications(self, request: InboundRequest) -> InboundResponse:
        """本机通知队列查询（任务 id 42）—— 值班台看「哪条推不出去」。

        **只认值班台凭据**（``CONSOLE_TOKEN``）：队列里有处置卡正文，
        不是公开信息。没配凭据位就一律 401 —— 不给默认口令。
        """
        if request.method.upper() != "GET":
            return InboundResponse(
                405,
                {"error": "method_not_allowed", "detail": "本端点只接受 GET"},
                {"Allow": "GET"},
            )
        if not self.console_token:
            return InboundResponse(
                401,
                {
                    "error": "unauthorized",
                    "detail": "服务端未配置 CONSOLE_TOKEN，本机通知队列不对外开放",
                },
            )
        if not verify_bearer(request.headers, self.console_token):
            return InboundResponse(
                401,
                {"error": "unauthorized", "detail": "缺少或错误的 Bearer 凭据"},
            )
        if self.local_queue is None:
            return InboundResponse(
                200,
                {
                    "count": 0,
                    "pending_count": 0,
                    "items": [],
                    "detail": "本机通知队列未启用",
                },
            )
        records = self.local_queue.records()
        pending = self.local_queue.pending()
        return InboundResponse(
            200,
            {
                "count": len(records),
                "pending_count": len(pending),
                "items": [record.to_dict() for record in records],
            },
        )

    def _process(self, event: CriticalEvent) -> InboundOutcome:
        if self.processor is None:
            return InboundOutcome(event_id=event.event_id, status="accepted")
        return self.processor(event)

    def _render(self, outcome: InboundOutcome) -> InboundResponse:
        payload: dict[str, Any] = {
            "status": outcome.status,
            "event_id": outcome.event_id,
            "duplicate": outcome.duplicate,
        }
        if outcome.duplicate:
            payload["detail"] = "幂等键已存在，本次不重复判定、不重复推送"
            return InboundResponse(200, payload)
        payload["decision"] = outcome.decision
        payload["card_id"] = outcome.card_id
        payload["threshold_row"] = outcome.threshold_row
        payload["missing_elements"] = list(outcome.missing_elements)
        payload["ruleset_snapshot_id"] = outcome.ruleset_snapshot_id
        payload["config_version"] = outcome.config_version
        payload["received_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return InboundResponse(202, payload)

    def _audit_unauthorized(self, request: InboundRequest, reason: str) -> None:
        """未授权请求写审计：**只记来源地址与路径，不记 token 明文**。"""
        self.store.append_audit(
            "inbound_unauthorized",
            actor="lis",
            detail=(
                f"入站推送未授权：{reason}；来源 {request.client_addr or '-'}；"
                f"路径 {request.path}"
            ),
        )
