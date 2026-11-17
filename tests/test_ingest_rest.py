"""LIS REST 回调入站测试（任务 id 4 · 入站辅通道）。

院内 LIS 除了走 MLLP 推 ``ORU^R01``，还会按 REST 契约直接推一条危急值。
本文件验证两件事：

1. **载荷字段映射** —— REST 字段 → 事件字段，一一对得上；缺字段落未识别
   记录，**一条都不静默丢弃**；观察值非数值**不换算、不猜数**；载荷里带
   患者标识字段**当场拒绝**；
2. **回调是真实出站调用** —— 真起一台本机 HTTP 端点（协议替身），断言
   请求确实发到了约定路径、确实带上了 ``Authorization: Bearer`` 头、体里
   确实是约定的字段；4xx 不可重试、5xx 与不可达可重试。

**与 MLLP 通道同构**：同一条危急值走 REST 与走 MLLP，必须得到**同一个
事件 ID** —— 这是「两条入站通道共用同一套规范函数」的验收口径。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from critval.hl7.normalize import normalize_events, parse_oru_r01
from critval.hl7.rest_channel import (
    LIS_REST_EVENT_PATH,
    LisRestCallbackClient,
    LisRestError,
    build_event_body,
    normalize_rest_body,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SAMPLES = PROJECT_ROOT / "samples" / "hl7"
TOKEN = "lis-shared-token-0001"

#: 一条危急的血红蛋白结果（阈值表：718-7 · g/L · 低危 30 · 高危 200）。
CRITICAL_BODY: dict[str, Any] = {
    "event_id": "LIS-EVT-0001",
    "item_code": "718-7",
    "value": 25,
    "unit": "g/L",
    "critical_level": "HH",
    "raised_at": "2026-01-02T08:30:00",
    "dept_code": "ICU",
}


# ---------------------------------------------------------------------------
# 载荷字段映射
# ---------------------------------------------------------------------------


def test_REST载荷字段映射到事件字段():
    """REST 字段 → 事件字段，一一对得上。"""
    event, unparsed = normalize_rest_body(CRITICAL_BODY)
    assert unparsed is None
    assert event is not None
    assert event.message_control_id == "LIS-EVT-0001"
    assert event.item_code == "718-7"
    assert float(event.value) == 25.0
    assert event.unit == "g/L"
    assert event.obx_flag == "HH"
    assert event.dept_code == "ICU"
    assert event.observed_at is not None
    assert event.observed_at.strftime("%Y-%m-%d %H:%M:%S") == "2026-01-02 08:30:00"
    assert event.source_channel == "http"


def test_标本号在本通道非必填():
    """本通道只传结果不传标本 —— 不因为缺标本号就丢这条危急值。"""
    event, unparsed = normalize_rest_body(CRITICAL_BODY)
    assert unparsed is None
    assert event is not None
    assert event.specimen_no == ""


@pytest.mark.parametrize(
    ("field", "reason"),
    [
        ("event_id", "missing_message_control_id"),
        ("item_code", "missing_item_code"),
        ("value", "missing_value"),
        ("raised_at", "missing_observed_at"),
    ],
)
def test_REST缺必需字段逐项落未识别记录(field: str, reason: str):
    """缺任一必需字段 → 落未识别记录 —— **一条都不静默丢弃**。"""
    body = dict(CRITICAL_BODY)
    body.pop(field)
    event, unparsed = normalize_rest_body(body)
    assert event is None
    assert unparsed is not None
    assert unparsed.reason == reason


def test_观察值非数值不换算不猜数():
    """``>250`` 这种值不换算、不猜数 —— 落未识别记录转人工。"""
    body = dict(CRITICAL_BODY)
    body["value"] = ">250"
    event, unparsed = normalize_rest_body(body)
    assert event is None
    assert unparsed is not None
    assert unparsed.reason == "value_not_numeric"


def test_患者标识字段当场拒绝():
    """本通道只传结果不传人 —— 带患者标识字段的载荷当场拒。"""
    body = dict(CRITICAL_BODY)
    body["patient_name"] = "张三"
    event, unparsed = normalize_rest_body(body)
    assert event is None
    assert unparsed is not None
    assert unparsed.reason == "patient_identifier_rejected"
    assert "张三" not in unparsed.detail


def test_数值与字符串两种写法都能解析():
    """JSON 里的数字与字符串都认 —— 但都按原样解析，不做单位猜测。"""
    for raw in (25, 25.0, "25"):
        body = dict(CRITICAL_BODY)
        body["value"] = raw
        event, unparsed = normalize_rest_body(body)
        assert unparsed is None
        assert event is not None
        assert float(event.value) == 25.0


# ---------------------------------------------------------------------------
# 与 MLLP 通道同构
# ---------------------------------------------------------------------------


def test_REST与MLLP产出同一个事件ID():
    """同一条危急值走两条通道 —— 事件 ID 与关键字段完全一致。"""
    raw = (SAMPLES / "示例ORU_R01_正常.hl7").read_text(encoding="utf-8").strip()
    mllp_events, _ = normalize_events(parse_oru_r01(raw))
    assert len(mllp_events) == 1
    mllp_event = mllp_events[0]

    rest_event, unparsed = normalize_rest_body(
        {
            "event_id": mllp_event.message_control_id,
            "item_code": mllp_event.item_code,
            "value": float(mllp_event.value),
            "unit": mllp_event.unit,
            "critical_level": mllp_event.obx_flag,
            "raised_at": mllp_event.observed_at.isoformat(sep=" ", timespec="seconds"),
            "dept_code": mllp_event.dept_code,
        }
    )
    assert unparsed is None
    assert rest_event is not None
    assert rest_event.event_id == mllp_event.event_id
    assert rest_event.observed_at == mllp_event.observed_at


# ---------------------------------------------------------------------------
# 回调出站：真实协议 + 凭据位
# ---------------------------------------------------------------------------


def test_回调请求打到约定路径且带Bearer凭据():
    """真起一台本机端点 —— 断言请求确实发出去了、头带上了。"""
    from tests.fixtures.lis_rest_double import LisRestDouble

    with LisRestDouble() as double:
        client = LisRestCallbackClient(double.base_url, TOKEN)
        result = client.post_body(CRITICAL_BODY)

    assert result.ok
    assert result.status == 200
    assert len(double.requests) == 1
    request = double.requests[0]
    assert request.method == "POST"
    assert request.path == LIS_REST_EVENT_PATH
    assert request.authorization == f"Bearer {TOKEN}"
    assert request.headers.get("Content-Type") == "application/json"


def test_回调请求体字段与契约一致():
    """体里就是约定的七个字段 —— 不多不少。"""
    from tests.fixtures.lis_rest_double import LisRestDouble

    with LisRestDouble() as double:
        client = LisRestCallbackClient(double.base_url, TOKEN)
        client.post_body(CRITICAL_BODY)

    payload = double.requests[0].json
    assert payload == CRITICAL_BODY
    assert set(payload) == {
        "event_id",
        "item_code",
        "value",
        "unit",
        "critical_level",
        "raised_at",
        "dept_code",
    }


def test_回调体用事件的消息控制号做事件号():
    """LIS 侧认识的号是消息控制号 —— 用它做幂等键才不会生成两条记录。"""
    from tests.fixtures.lis_rest_double import LisRestDouble

    raw = (SAMPLES / "示例ORU_R01_正常.hl7").read_text(encoding="utf-8").strip()
    events, _ = normalize_events(parse_oru_r01(raw))
    event = events[0]

    body = build_event_body(event)
    assert body["event_id"] == event.message_control_id
    assert body["item_code"] == event.item_code
    assert body["unit"] == event.unit
    assert body["raised_at"]

    with LisRestDouble() as double:
        client = LisRestCallbackClient(double.base_url, TOKEN)
        result = client.post_event(event)
    assert result.ok
    assert double.requests[0].json["event_id"] == event.message_control_id


def test_4xx不可重试_5xx可重试():
    """契约错（4xx）重试没有意义；对端暂时不可用（5xx）可以重试。"""
    from tests.fixtures.lis_rest_double import LisRestDouble

    with LisRestDouble(status=400, response_body='{"error":"bad_payload"}') as double:
        client = LisRestCallbackClient(double.base_url, TOKEN)
        result = client.post_body(CRITICAL_BODY)
    assert not result.ok
    assert result.status == 400
    assert not result.retryable

    with LisRestDouble(status=503, response_body='{"error":"unavailable"}') as double:
        client = LisRestCallbackClient(double.base_url, TOKEN)
        result = client.post_body(CRITICAL_BODY)
    assert not result.ok
    assert result.status == 503
    assert result.retryable


def test_对端不可达标记为可重试且不抛栈():
    """LIS 侧暂时不可达 —— 回调失败要**可观测**，不是崩掉整个进程。"""
    client = LisRestCallbackClient("http://127.0.0.1:9", TOKEN, timeout=1.0)
    result = client.post_body(CRITICAL_BODY)
    assert not result.ok
    assert result.retryable
    assert "不可达" in result.error


def test_缺凭据位直接报错不发起请求():
    """没配凭据位就**不发** —— 不拿空口令去撞对端。"""
    with pytest.raises(LisRestError):
        LisRestCallbackClient("", "").post_body(CRITICAL_BODY)
    with pytest.raises(LisRestError):
        LisRestCallbackClient("http://127.0.0.1:9", "").post_body(CRITICAL_BODY)


def test_客户端不把凭据写进请求体():
    """凭据只在请求头里 —— 体里不能出现 token。"""
    from tests.fixtures.lis_rest_double import LisRestDouble

    with LisRestDouble() as double:
        client = LisRestCallbackClient(double.base_url, TOKEN)
        client.post_body(CRITICAL_BODY)

    body_text = double.requests[0].body.decode("utf-8")
    assert TOKEN not in body_text
