"""企微推送通道单测（任务 id 11）。

盯住五件事：

* **HTTP 200 不等于推送成功** —— 企微用 ``errcode`` 表达成败，errcode≠0
  必须判失败（否则「推了但没推到」的危急值在群里静默消失）；
* **退避重试** —— 限流 / 网络抖动按指数退避重试，契约错不重试；
* **载荷合规** —— ``msgtype=markdown``，正文含处置卡与确认链接；
* **凭据不外泄** —— webhook 地址只以脱敏形式出现在文案里；
* **降级不丢事件** —— 企微未配置或推不出去时落本机通知记录并留痕。
"""

from __future__ import annotations

import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from critval.card.builder import build_card
from critval.judge.critical import judge_event
from critval.kb.loader import load_threshold_table
from critval.models import Citation, CriticalEvent, PolicyClause
from critval.notify.local import CHANNEL_LOCAL, FallbackNotifier, LocalNotifier
from critval.notify.wecom import WeComNotifier

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fixtures.wecom_double import WeComDouble  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "脱敏样例阈值表.xlsx"
CONFIRM_URL = "http://127.0.0.1:8848/events/evt-0001/ack"

POLICY_CITATION = Citation(
    source_name="示例危急值报告制度.docx",
    source_sha256="a" * 64,
    kind="policy_clause",
    paragraph_no=19,
    section_path=("第三章 报告流程与通知时限",),
    line_no=19,
    file_version="示例 v1.0",
)


@pytest.fixture(scope="module")
def table():
    return load_threshold_table(FIXTURE)


@pytest.fixture()
def clause() -> PolicyClause:
    return PolicyClause(
        section_title="第三章 报告流程与通知时限",
        body="第七条 检验科发现危急值后应当班复核……",
        notify_target="检验科值班技师与检验科组长",
        deadline_text="10 分钟内",
        deadline_minutes=10,
        record_destination="危急值登记系统，并同步记入检验科危急值台账",
        citation=POLICY_CITATION,
        clause_no="第七条",
    )


@pytest.fixture()
def card(table, clause):
    event = CriticalEvent(
        event_id="evt-0001",
        message_control_id="MSG0001",
        specimen_no="SPEC-0001",
        item_code="718-7",
        item_name="血红蛋白",
        value=Decimal("25"),
        unit="g/L",
        observed_at=datetime(2026, 1, 2, 8, 30),
        dept_code="ICU",
        patient_ref="PATREF-9f2c",
    )
    judgement = judge_event(event, table.rows)
    return build_card(
        event,
        judgement,
        policy_clause=clause,
        duty_person="李技师",
    )


@pytest.fixture()
def double():
    instance = WeComDouble()
    instance.start()
    try:
        yield instance
    finally:
        instance.stop()


def _notifier(double: WeComDouble, **kwargs) -> WeComNotifier:
    return WeComNotifier(double.webhook_url, sleep=lambda _seconds: None, **kwargs)


# ---------------------------------------------------------------------------
# 正常推送
# ---------------------------------------------------------------------------


def test_推送成功且载荷是markdown(double: WeComDouble, card):
    result = _notifier(double).send_card(card, confirm_url=CONFIRM_URL)

    assert result.ok is True
    assert result.errcode == 0
    assert result.attempts == 1
    assert double.call_count == 1
    request = double.requests[0]
    assert request.msgtype == "markdown"
    assert request.path.startswith("/cgi-bin/webhook/send")
    # 正文含处置卡四要素与确认链接。
    assert "阈值依据行" in request.content
    assert "上报时限" in request.content
    assert CONFIRM_URL in request.content


def test_正文里的出处行号也推出去(double: WeComDouble, card):
    _notifier(double).send_card(card, confirm_url=CONFIRM_URL)
    assert "第 2 行" in double.last_content


def test_推送正文不含患者标识(double: WeComDouble, card):
    _notifier(double).send_card(card, confirm_url=CONFIRM_URL)
    assert "PATREF-9f2c" not in double.last_content


def test_确认链接不重复追加(double: WeComDouble):
    _notifier(double).send_markdown("处置卡正文", confirm_url=CONFIRM_URL)
    assert double.last_content.count(CONFIRM_URL) == 1


# ---------------------------------------------------------------------------
# 失败判定与退避重试
# ---------------------------------------------------------------------------


def test_errcode非零时判失败不当作成功(double: WeComDouble):
    double.errcode = 40001
    double.errmsg = "invalid webhook key"
    result = _notifier(double).send_markdown("正文")

    assert result.ok is False
    assert result.errcode == 40001
    assert "40001" in result.errmsg
    assert result.error


def test_限流错误码按指数退避重试(double: WeComDouble):
    double.errcode = 45009
    waits: list[float] = []
    notifier = WeComNotifier(
        double.webhook_url,
        max_attempts=3,
        backoff_base=0.5,
        backoff_factor=2.0,
        sleep=waits.append,
    )
    result = notifier.send_markdown("正文")

    assert result.ok is False
    assert result.attempts == 3
    assert double.call_count == 3
    # 指数退避：0.5 → 1.0
    assert waits == [0.5, 1.0]


def test_契约错不重试(double: WeComDouble):
    double.errcode = 93000
    double.errmsg = "invalid webhook url"
    waits: list[float] = []
    notifier = WeComNotifier(double.webhook_url, max_attempts=3, sleep=waits.append)
    result = notifier.send_markdown("正文")

    assert result.ok is False
    assert result.attempts == 1
    assert double.call_count == 1
    assert waits == []


def test_HTTP5xx按退避重试(double: WeComDouble):
    double.status = 502
    waits: list[float] = []
    notifier = WeComNotifier(double.webhook_url, max_attempts=2, sleep=waits.append)
    result = notifier.send_markdown("正文")

    assert result.ok is False
    assert result.retryable is True
    assert double.call_count == 2
    assert len(waits) == 1


def test_退避时长封顶(double: WeComDouble):
    notifier = WeComNotifier(
        double.webhook_url, backoff_base=1.0, backoff_factor=10.0, max_backoff=8.0
    )
    assert notifier.backoff_delay(1) == 1.0
    assert notifier.backoff_delay(2) == 8.0
    assert notifier.backoff_delay(5) == 8.0


def test_响应不是合法JSON时判失败(double: WeComDouble):
    double.raw_body = "<html>502</html>"
    result = _notifier(double).send_markdown("正文")
    assert result.ok is False
    assert "JSON" in result.errmsg


def test_响应体为空时判失败(double: WeComDouble):
    double.raw_body = ""
    result = _notifier(double).send_markdown("正文")
    assert result.ok is False


# ---------------------------------------------------------------------------
# 凭据与降级
# ---------------------------------------------------------------------------


def test_webhook地址只以脱敏形式出现(double: WeComDouble):
    notifier = _notifier(double)
    masked = notifier.masked_endpoint
    assert "test-double-key" not in masked
    assert "***" in masked
    assert str(len(double.webhook_url)) in masked


def test_未配置webhook时构造直接报错(monkeypatch, double: WeComDouble):
    from critval.config import Config

    monkeypatch.delenv("CRITVAL_WECOM_WEBHOOK_URL", raising=False)
    with pytest.raises(RuntimeError) as excinfo:
        WeComNotifier.from_config()
    assert "CRITVAL_WECOM_WEBHOOK_URL" in str(excinfo.value)
    assert Config is not None


def test_企微未配置时事件不丢落本机记录(tmp_path: Path, card):
    local = LocalNotifier(tmp_path / "local_notifications.jsonl")
    notifier = FallbackNotifier(primary=None, fallback=local)
    outcome = notifier.send_card(
        card,
        confirm_url=CONFIRM_URL,
        event_id="evt-0001",
        notify_target="李技师",
        due_minutes=10,
    )

    assert outcome.ok is False
    assert outcome.fallback_used is True
    assert outcome.channel == CHANNEL_LOCAL
    records = local.records()
    assert len(records) == 1
    assert records[0].event_id == "evt-0001"
    assert records[0].due_minutes == 10
    assert "阈值依据行" in records[0].content
    assert records[0].reason


def test_企微推送失败时落本机记录并留痕(tmp_path: Path, card, double: WeComDouble):
    double.errcode = 40001
    local = LocalNotifier(tmp_path / "local_notifications.jsonl")
    notifier = FallbackNotifier(primary=_notifier(double), fallback=local)
    outcome = notifier.send_card(card, confirm_url=CONFIRM_URL, event_id="evt-0001")

    assert outcome.ok is False
    assert outcome.fallback_used is True
    assert "40001" in (outcome.local_record.reason if outcome.local_record else "")
    assert len(local.records_of("evt-0001")) == 1


def test_本机记录追加写不改旧行(tmp_path: Path):
    local = LocalNotifier(tmp_path / "local_notifications.jsonl")
    local.send("第一条", event_id="evt-1")
    local.send("第二条", event_id="evt-1")
    records = local.records_of("evt-1")
    assert [r.content for r in records] == ["第一条", "第二条"]
