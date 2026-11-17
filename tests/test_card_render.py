"""处置卡多渠道渲染、不可变快照与离线纯文本版。

三个渠道讲的是同一件事：企微群里的 Markdown、值班台的 HTML、打印出来的
纯文本，必须包含**同一组四要素**、同一批出处行号。快照则把「当时推出去的
原文」原样存下来 —— 事后复核读快照，不重新渲染（阈值表改版后重渲染出来的
东西，已经不是当时给值班人看的那份了）。
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from critval.card.builder import build_card
from critval.card.provenance import ORIGIN_CACHE, roster_provenance
from critval.card.renderer import (
    CHANNELS,
    TRUNCATION_MARK,
    WECOM_FIELD_MAX_CHARS,
    WECOM_MARKDOWN_MAX_CHARS,
    build_card_view,
    render_card,
    render_html,
    render_markdown,
    render_text,
)
from critval.card.snapshot import CardSnapshotStore, SnapshotNotFound
from critval.judge.critical import judge_event
from critval.kb.loader import load_threshold_table
from critval.models import CardProvenance, Citation, CriticalEvent, PolicyClause

PROJECT_ROOT = Path(__file__).resolve().parent.parent
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


def make_event(**kwargs) -> CriticalEvent:
    payload = {
        "event_id": "evt-0001",
        "message_control_id": "MSG0001",
        "specimen_no": "SPEC-0001",
        "item_code": "718-7",
        "item_name": "血红蛋白",
        "value": Decimal("25"),
        "unit": "g/L",
        "observed_at": datetime(2026, 1, 2, 8, 30),
        "dept_code": "ICU",
        "patient_ref": "PATREF-9f2c",
    }
    payload.update(kwargs)
    return CriticalEvent(**payload)


def make_card(table, clause, **kwargs):
    event = make_event(**kwargs.pop("event", {}))
    judgement = judge_event(event, table.rows)
    return build_card(
        event,
        judgement,
        policy_clause=clause,
        duty_person=kwargs.pop("duty_person", "李技师"),
        **kwargs,
    )


# —— 一、三渠道要素集合一致 ——


def test_三渠道都包含同一组四要素(table, clause):
    card = make_card(table, clause)
    texts = {
        "markdown": render_markdown(card, CONFIRM_URL),
        "html": render_html(card, CONFIRM_URL),
        "text": render_text(card, CONFIRM_URL),
    }
    for channel, text in texts.items():
        for label in ("阈值依据行", "通知对象", "上报时限", "记录去向"):
            assert label in text, f"{channel} 渠道缺要素 {label}"


def test_三渠道都带出处行号与确认回执(table, clause):
    card = make_card(table, clause)
    for channel in CHANNELS:
        text = render_card(card, channel, CONFIRM_URL)
        assert "第 2 行" in text, f"{channel} 渠道缺阈值表行号"
        assert "第 19 行" in text, f"{channel} 渠道缺制度条款行号"
        assert CONFIRM_URL in text, f"{channel} 渠道缺确认回执链接"


def test_三渠道出自同一份视图(table, clause):
    """渲染只读 ``build_card_view`` 的数据 —— 渠道之间不各取一份。"""
    card = make_card(table, clause)
    view = build_card_view(card, CONFIRM_URL)
    assert [element.label for element in view.elements] == [
        "阈值依据行",
        "通知对象",
        "上报时限",
        "记录去向",
    ]
    markdown = render_markdown(card, CONFIRM_URL)
    for element in view.elements:
        assert element.value in markdown


def test_缺项与降级在三渠道里都看得出来(table, clause):
    card = build_card(
        make_event(),
        judge_event(make_event(), table.rows),
        policy_clause=None,
        duty_person="李技师",
        provenance=CardProvenance(
            duty_roster=roster_provenance(ORIGIN_CACHE, as_of="2026-01-02 08:30")
        ),
    )
    for channel in CHANNELS:
        text = render_card(card, channel, CONFIRM_URL)
        assert "依据缺失" in text, f"{channel} 渠道没标出缺项"
        assert "缓存" in text, f"{channel} 渠道没标出降级来源"


# —— 二、企微 Markdown 的截断策略 ——


def test_超长要素按要素截断并标注(table, clause):
    long_value = "检验科值班技师" * 60
    broken = PolicyClause(
        section_title=clause.section_title,
        body="",
        notify_target=long_value,
        deadline_text=clause.deadline_text,
        deadline_minutes=clause.deadline_minutes,
        record_destination=clause.record_destination,
        citation=POLICY_CITATION,
    )
    card = build_card(make_event(), judge_event(make_event(), table.rows), policy_clause=broken)
    markdown = render_markdown(card, CONFIRM_URL)

    assert TRUNCATION_MARK in markdown
    assert len(long_value) > WECOM_FIELD_MAX_CHARS
    # 完整内容不在企微正文里（值班台与纯文本版里仍在）
    assert long_value not in markdown


def test_截断不影响另外两个渠道的完整内容(table, clause):
    long_value = "检验科值班技师" * 60
    broken = PolicyClause(
        section_title=clause.section_title,
        body="",
        notify_target=long_value,
        deadline_text=clause.deadline_text,
        deadline_minutes=clause.deadline_minutes,
        record_destination=clause.record_destination,
        citation=POLICY_CITATION,
    )
    card = build_card(make_event(), judge_event(make_event(), table.rows), policy_clause=broken)
    assert long_value in render_text(card, CONFIRM_URL)
    assert long_value in render_html(card, CONFIRM_URL)


def test_整卡长度不超过企微正文上限(table, clause):
    long_value = "检验科值班技师与检验科组长" * 200
    broken = PolicyClause(
        section_title=clause.section_title,
        body="",
        notify_target=long_value,
        deadline_text=long_value,
        deadline_minutes=10,
        record_destination=long_value,
        citation=POLICY_CITATION,
    )
    card = build_card(make_event(), judge_event(make_event(), table.rows), policy_clause=broken)
    markdown = render_markdown(card, CONFIRM_URL)
    assert len(markdown) <= WECOM_MARKDOWN_MAX_CHARS
    # 截断也不能把确认回执丢掉 —— 值班人得点它
    assert CONFIRM_URL in markdown


# —— 三、值班台 HTML ——


def test_HTML出处可展开且版本与生效期可见(table, clause):
    card = make_card(table, clause)
    html = render_html(card, CONFIRM_URL)

    assert "<details" in html and "<summary>" in html
    assert "第 2 行" in html
    assert "示例 v1.0" in html
    assert "版本与生效期" in html
    assert "2026-01-01" in html  # 阈值表生效期


def test_HTML带四要素状态徽标(table, clause):
    card = make_card(table, clause)
    html = render_html(card, CONFIRM_URL)
    assert "critval-card__badge--resolved" in html
    assert "依据齐备" in html

    degraded = build_card(
        make_event(),
        judge_event(make_event(), table.rows),
        policy_clause=clause,
        provenance=CardProvenance(
            duty_roster=roster_provenance(ORIGIN_CACHE, as_of="2026-01-02 08:30")
        ),
    )
    assert "critval-card__badge--degraded" in render_html(degraded, CONFIRM_URL)


def test_HTML转义外部文本(table, clause):
    """要素值里的尖括号不能破坏页面结构。"""
    broken = PolicyClause(
        section_title=clause.section_title,
        body="",
        notify_target="<script>alert(1)</script>",
        deadline_text=clause.deadline_text,
        deadline_minutes=clause.deadline_minutes,
        record_destination=clause.record_destination,
        citation=POLICY_CITATION,
    )
    card = build_card(make_event(), judge_event(make_event(), table.rows), policy_clause=broken)
    html = render_html(card, CONFIRM_URL)
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


# —— 四、离线纯文本版 ——


def test_纯文本版不含Markdown与HTML标记(table, clause):
    card = make_card(table, clause)
    text = render_text(card, CONFIRM_URL)
    assert "**" not in text
    # 没有 HTML 标签（阈值行里的「< 30 或 > 200」是数值比较符，不是标记）
    assert re.search(r"</?[a-zA-Z][^>]*>", text) is None
    assert "#" not in text
    assert "|" not in text


def test_纯文本版含四要素与确认回执(table, clause):
    card = make_card(table, clause)
    text = render_text(card, CONFIRM_URL)
    assert "处置依据（四要素）" in text
    assert CONFIRM_URL in text
    assert "第 2 行" in text
    assert "危急值登记系统" in text


# —— 五、不可变快照 ——


def test_快照保存后与当前渲染逐字一致(table, clause, tmp_path):
    card = make_card(table, clause)
    store = CardSnapshotStore(tmp_path)
    snapshot = store.save(card, CONFIRM_URL)

    assert snapshot.rendering("markdown") == render_markdown(card, CONFIRM_URL)
    assert snapshot.rendering("html") == render_html(card, CONFIRM_URL)
    assert snapshot.rendering("text") == render_text(card, CONFIRM_URL)


def test_同内容重复保存得到同一快照且文件不被改写(table, clause, tmp_path):
    card = make_card(table, clause)
    store = CardSnapshotStore(tmp_path)
    first = store.save(card, CONFIRM_URL)
    path = tmp_path / f"{first.snapshot_id}.json"
    before = path.read_text(encoding="utf-8")

    second = store.save(card, CONFIRM_URL)
    assert second.snapshot_id == first.snapshot_id
    assert path.read_text(encoding="utf-8") == before  # 不可变：不重写


def test_快照内容变了就是新快照且旧快照仍在(table, clause, tmp_path):
    store = CardSnapshotStore(tmp_path)
    first = store.save(make_card(table, clause), CONFIRM_URL)
    second = store.save(make_card(table, clause, event={"event_id": "evt-0002"}), CONFIRM_URL)

    assert first.snapshot_id != second.snapshot_id
    assert (tmp_path / f"{first.snapshot_id}.json").exists()
    assert store.load(first.snapshot_id).card_id == first.card_id


def test_快照记录配置版本与阈值表制度版本(table, clause, tmp_path):
    card = make_card(table, clause)
    snapshot = CardSnapshotStore(tmp_path).save(card, CONFIRM_URL)
    assert snapshot.config_version == card.config_version
    assert snapshot.threshold_version == card.judgement.threshold_row.version
    assert snapshot.policy_version == "示例 v1.0"
    assert snapshot.event_id == card.event.event_id


def test_按事件取回最近一份快照(table, clause, tmp_path):
    store = CardSnapshotStore(tmp_path)
    store.save(make_card(table, clause), CONFIRM_URL)
    found = store.find_by_event("evt-0001")
    assert found is not None
    assert found.event_id == "evt-0001"
    assert store.find_by_event("evt-9999") is None


def test_快照被改动后校验能发现(table, clause, tmp_path):
    store = CardSnapshotStore(tmp_path)
    snapshot = store.save(make_card(table, clause), CONFIRM_URL)
    assert store.verify(snapshot.snapshot_id).ok

    path = tmp_path / f"{snapshot.snapshot_id}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["renderings"]["text"] = "被改过的正文"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    verification = store.verify(snapshot.snapshot_id)
    assert not verification.ok
    assert verification.problems


def test_快照不存在时报错而不是返回空(table, clause, tmp_path):
    store = CardSnapshotStore(tmp_path)
    with pytest.raises(SnapshotNotFound):
        store.load("snap-0000000000000000")


# —— 六、患者可识别明细三渠道都不出现 ——


def test_患者明细三渠道都不出现(table, clause):
    card = make_card(table, clause, event={"patient_ref": "PID-3|住院流水号 00012345"})
    for channel in CHANNELS:
        text = render_card(card, channel, CONFIRM_URL)
        assert "00012345" not in text
        assert "住院流水号" not in text


# —— 七、CLI 导出快照 ——


def test_CLI导出纯文本卡(table, clause, tmp_path):
    card = make_card(table, clause)
    CardSnapshotStore(tmp_path).save(card, CONFIRM_URL)

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "critval.cli",
            "export",
            "--event",
            "evt-0001",
            "--format",
            "text",
            "--snapshot-dir",
            str(tmp_path),
        ],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "危急值处置卡" in result.stdout
    assert "处置依据（四要素）" in result.stdout
    assert "第 2 行" in result.stdout
    assert CONFIRM_URL in result.stdout


def test_CLI导出可校验快照完整性(table, clause, tmp_path):
    card = make_card(table, clause)
    snapshot = CardSnapshotStore(tmp_path).save(card, CONFIRM_URL)

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "critval.cli",
            "export",
            "--event",
            "evt-0001",
            "--format",
            "markdown",
            "--snapshot-dir",
            str(tmp_path),
            "--verify",
            "--json",
        ],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["snapshot_id"] == snapshot.snapshot_id
    assert payload["threshold_version"]
    assert payload["policy_version"] == "示例 v1.0"


def test_CLI查不到快照时返回业务失败码(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "critval.cli",
            "export",
            "--event",
            "evt-9999",
            "--snapshot-dir",
            str(tmp_path),
        ],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "没有处置卡快照" in result.stderr
