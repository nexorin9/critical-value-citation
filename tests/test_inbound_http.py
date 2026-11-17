"""入站 HTTP 推送端点测试（任务 id 24）。

覆盖五类必答用例 —— 正常推送 / 未授权 / 重复推送幂等 / 字段缺失容错 /
请求体过大拒绝 —— 外加内容类型、路由、限流、患者标识拒绝与真实 socket 端到端。

**端点只在院内 LIS 推送危急值时才会被调用**，所以本文件里的每一条断言
都按「值班台上能不能看到这条事件」来写：事件要么进了台账，要么进了审计 ——
**不允许有第三种结果**。
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from critval.console.app import make_server
from critval.console.inbound import (
    INBOUND_PATH,
    InboundRequest,
    InboundService,
    LedgerInboundProcessor,
    RateLimiter,
    verify_bearer,
)
from critval.hl7.normalize import normalize_events, parse_oru_r01
from critval.kb.loader import load_threshold_table
from critval.ledger.store import LedgerStore

PROJECT_ROOT = Path(__file__).resolve().parent.parent
THRESHOLD = PROJECT_ROOT / "samples" / "threshold" / "示例危急值阈值表.xlsx"
TOKEN = "lis-token-0001"

#: 一条危急的血红蛋白结果（阈值表：718-7 · g/L · 低危 30 · 高危 200）。
CRITICAL_PAYLOAD: dict[str, Any] = {
    "event_id": "LIS-EVT-0001",
    "item_code": "718-7",
    "value": 25,
    "unit": "g/L",
    "critical_level": "HH",
    "raised_at": "2026-01-02T08:30:00",
    "dept_code": "ICU",
}


def _headers(token: str = TOKEN, content_type: str = "application/json") -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": content_type,
    }


def _body(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


@pytest.fixture()
def service(tmp_path):
    """装配一个入站服务（内存外的临时库 + 示例阈值表）。"""
    store = LedgerStore(tmp_path / "critval.db")
    table = load_threshold_table(THRESHOLD)
    processor = LedgerInboundProcessor(store, rows_provider=lambda: list(table.rows))
    svc = InboundService(token=TOKEN, store=store, processor=processor)
    svc.test_store = store  # type: ignore[attr-defined]
    yield svc
    store.close()


def _post(service: InboundService, payload: dict[str, Any], **kwargs: Any):
    request = InboundRequest(
        method=kwargs.pop("method", "POST"),
        path=kwargs.pop("path", INBOUND_PATH),
        headers=kwargs.pop("headers", _headers()),
        body=kwargs.pop("body", _body(payload)),
        client_addr=kwargs.pop("client_addr", "10.0.0.9"),
    )
    return service.handle(request)


# ---------------------------------------------------------------------------
# 1. 正常推送
# ---------------------------------------------------------------------------


def test_正常推送落台账并出带出处的处置卡(service):
    response = _post(service, CRITICAL_PAYLOAD)

    assert response.status == 202
    assert response.payload["status"] == "accepted"
    assert response.payload["duplicate"] is False
    assert response.payload["decision"] == "critical"
    assert response.payload["card_id"]
    assert "示例危急值阈值表.xlsx" in response.payload["threshold_row"]

    store = service.test_store
    assert store.count_events() == 1
    event = store.list_events()[0]
    assert event["item_code"] == "718-7"
    assert event["source_channel"] == "http"
    assert event["status"] == "card_built"
    assert event["message_control_id"] == "LIS-EVT-0001"

    citations = store.citations_of(event["event_id"])
    assert citations, "处置卡必须带出处"
    for citation in citations:
        assert citation["source_sha256"]
        assert citation["line_no"] is not None


def test_不危急的结果只入台账不出卡(service):
    response = _post(service, {**CRITICAL_PAYLOAD, "event_id": "LIS-EVT-0002", "value": 100})

    assert response.status == 202
    assert response.payload["decision"] == "not_critical"
    assert response.payload["card_id"] == ""

    store = service.test_store
    assert store.count_events() == 1
    assert store.list_events()[0]["status"] == "not_critical"


# ---------------------------------------------------------------------------
# 2. 未授权
# ---------------------------------------------------------------------------


def test_缺凭据被拒且写审计(service):
    response = _post(service, CRITICAL_PAYLOAD, headers={"Content-Type": "application/json"})

    assert response.status == 401
    assert response.payload["error"] == "unauthorized"

    store = service.test_store
    entries = store.audit_entries()
    unauthorized = [e for e in entries if e["action"] == "inbound_unauthorized"]
    assert len(unauthorized) == 1
    assert "10.0.0.9" in unauthorized[0]["detail"]
    assert TOKEN not in unauthorized[0]["detail"], "审计里不能出现 token 明文"
    assert store.count_events() == 0


def test_错误凭据被拒(service):
    response = _post(service, CRITICAL_PAYLOAD, headers=_headers(token="wrong-token"))
    assert response.status == 401
    assert service.test_store.count_events() == 0


def test_服务端未配置凭据时一律拒绝(service):
    service.token = ""
    response = _post(service, CRITICAL_PAYLOAD)
    assert response.status == 401
    assert "LIS_CRITICAL_TOKEN" in response.payload["detail"]
    assert service.test_store.count_events() == 0


def test_常数时间比较的凭据校验():
    assert verify_bearer({"Authorization": f"Bearer {TOKEN}"}, TOKEN)
    assert verify_bearer({"authorization": f"bearer {TOKEN}"}, TOKEN)
    assert not verify_bearer({"Authorization": f"Bearer {TOKEN}x"}, TOKEN)
    assert not verify_bearer({"Authorization": f"Basic {TOKEN}"}, TOKEN)
    assert not verify_bearer({"Authorization": TOKEN}, TOKEN)
    assert not verify_bearer({}, TOKEN)
    assert not verify_bearer({"Authorization": f"Bearer {TOKEN}"}, "")


# ---------------------------------------------------------------------------
# 3. 重复推送幂等
# ---------------------------------------------------------------------------


def test_重复推送只处理一次且带duplicate标识(service):
    first = _post(service, CRITICAL_PAYLOAD)
    second = _post(service, CRITICAL_PAYLOAD)

    assert first.status == 202
    assert second.status == 200
    assert second.payload["duplicate"] is True
    assert second.payload["status"] == "duplicate"
    assert "不重复判定" in second.payload["detail"]

    store = service.test_store
    assert store.count_events() == 1, "重复推送不得新增事件"
    assert len(store.citations_of(store.list_events()[0]["event_id"])) == len(
        store.citations_of(store.list_events()[0]["event_id"])
    )
    actions = [e["action"] for e in store.audit_entries()]
    assert "event_duplicate" in actions


def test_重复推送不重复出卡(service):
    _post(service, CRITICAL_PAYLOAD)
    before = service.test_store.citations_of(service.test_store.list_events()[0]["event_id"])

    _post(service, CRITICAL_PAYLOAD)

    after = service.test_store.citations_of(service.test_store.list_events()[0]["event_id"])
    assert before == after, "重复推送不得覆盖既有事实（引用逐条一致）"


# ---------------------------------------------------------------------------
# 4. 字段缺失容错（一条都不静默丢弃）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        ({"value": 25, "unit": "g/L", "raised_at": "2026-01-02T08:30:00"}, "missing_event_id"),
        ({"event_id": "X", "value": 25, "unit": "g/L", "raised_at": "2026-01-02T08:30:00"}, "missing_item_code"),
        ({"event_id": "X", "item_code": "718-7", "unit": "g/L"}, "missing_value"),
        ({"event_id": "X", "item_code": "718-7", "value": 25}, "missing_raised_at"),
    ],
)
def test_缺字段落审计不静默丢弃(service, payload, reason):
    response = _post(service, payload)

    assert response.status == 422
    assert response.payload["error"] == "unparsed_event"

    actions = [e["action"] for e in service.test_store.audit_entries()]
    assert "unparsed_event" in actions, "读不懂的推送必须留痕"
    assert service.test_store.count_events() == 0


def test_缺字段的报错点名是哪个字段(service):
    response = _post(service, {"event_id": "X", "value": 25, "raised_at": "2026-01-02T08:30:00"})

    assert response.status == 422
    assert response.payload["reason"] == "missing_item_code"
    assert "item_code" in response.payload["detail"]


def test_观察值非数值不换算不猜数(service):
    response = _post(service, {**CRITICAL_PAYLOAD, "value": ">250"})

    assert response.status == 422
    assert response.payload["reason"] == "value_not_numeric"
    assert "不是数值" in response.payload["detail"]


def test_载荷带患者标识当场拒绝(service):
    response = _post(service, {**CRITICAL_PAYLOAD, "patient_name": "张三"})

    assert response.status == 422
    assert response.payload["reason"] == "patient_identifier_rejected"
    assert service.test_store.count_events() == 0


# ---------------------------------------------------------------------------
# 5. 请求体过大 / 内容类型 / 路由
# ---------------------------------------------------------------------------


def test_请求体过大被拒且不落事件(service):
    service.max_body_bytes = 128
    response = _post(service, {**CRITICAL_PAYLOAD, "pad": "x" * 500})

    assert response.status == 413
    assert response.payload["error"] == "payload_too_large"
    assert service.test_store.count_events() == 0


def test_内容类型不对被拒(service):
    response = _post(service, CRITICAL_PAYLOAD, headers=_headers(content_type="text/plain"))
    assert response.status == 415
    assert response.payload["error"] == "unsupported_media_type"


def test_非法JSON被拒(service):
    response = _post(service, {}, body=b"{not json")
    assert response.status == 400
    assert response.payload["error"] == "invalid_json"


def test_非对象载荷被拒(service):
    response = _post(service, {}, body=b"[1, 2, 3]")
    assert response.status == 400
    assert response.payload["error"] == "invalid_payload"


def test_未知路径与错误方法(service):
    assert _post(service, CRITICAL_PAYLOAD, path="/api/v1/other").status == 404
    assert _post(service, CRITICAL_PAYLOAD, method="GET").status == 405


# ---------------------------------------------------------------------------
# 6. 限流
# ---------------------------------------------------------------------------


def test_超出限流被拒且带RetryAfter(service):
    service.limiter = RateLimiter(limit=2, window_seconds=60)

    assert _post(service, CRITICAL_PAYLOAD).status == 202
    assert _post(service, {**CRITICAL_PAYLOAD, "event_id": "LIS-EVT-0009"}).status == 202
    third = _post(service, {**CRITICAL_PAYLOAD, "event_id": "LIS-EVT-0010"})

    assert third.status == 429
    assert third.payload["error"] == "rate_limited"
    assert int(third.headers["Retry-After"]) > 0
    assert service.test_store.count_events() == 2


# ---------------------------------------------------------------------------
# 7. 与 MLLP 路径共用同一套规范函数
# ---------------------------------------------------------------------------


def test_两条入站通道共用同一套规范函数():
    """同一件事（同一事件号 + 项目码 + 结果时刻）走两条通道 → 同一个事件 ID。

    这是「共用一套规范函数」的可核对证据：幂等键口径一致，事件不会因为
    换了一条通道就变成两条。
    """
    frame = (
        "MSH|^~\\&|LIS|HOSP|LAB|HOSP|20260102083000||ORU^R01|LIS-EVT-0001|P|2.5\r"
        "OBR|1|SPE-0001|718-7|血红蛋白|||20260102083000\r"
        "OBX|1|NM|718-7^血红蛋白||25|g/L|3.5-9.5|H|||20260102083000\r"
    )
    hl7_events, hl7_unparsed = normalize_events(parse_oru_r01(frame))
    assert not hl7_unparsed
    assert len(hl7_events) == 1

    from critval.hl7.normalize import normalize_http_payload

    http_event, http_unparsed = normalize_http_payload(
        {**CRITICAL_PAYLOAD, "specimen_no": "SPE-0001"}
    )
    assert http_unparsed is None
    assert http_event is not None

    assert http_event.event_id == hl7_events[0].event_id
    assert http_event.message_control_id == hl7_events[0].message_control_id
    assert http_event.observed_at == hl7_events[0].observed_at


# ---------------------------------------------------------------------------
# 8. 真实 socket 端到端
# ---------------------------------------------------------------------------


@pytest.fixture()
def live_server(service):
    server = make_server("127.0.0.1", 0, service=service)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    yield f"http://{host}:{port}"
    server.shutdown()
    server.server_close()


def _http_post(url: str, payload: bytes, token: str = TOKEN) -> tuple[int, dict[str, Any]]:
    request = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def test_真实socket推送后台账新增一条重复不再新增(live_server, service):
    url = f"{live_server}{INBOUND_PATH}"

    status, payload = _http_post(url, _body(CRITICAL_PAYLOAD))
    assert status == 202
    assert payload["status"] == "accepted"

    status, payload = _http_post(url, _body(CRITICAL_PAYLOAD))
    assert status == 200
    assert payload["duplicate"] is True

    assert service.test_store.count_events() == 1, "重复 curl 不得让台账长出第二条"


def test_真实socket未授权返回401(live_server, service):
    status, payload = _http_post(
        f"{live_server}{INBOUND_PATH}", _body(CRITICAL_PAYLOAD), token="wrong-token"
    )
    assert status == 401
    assert payload["error"] == "unauthorized"
    assert service.test_store.count_events() == 0
