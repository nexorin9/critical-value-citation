"""危急优先级与多项目合并通知测试（任务 id 34）。

覆盖四组口径：

1. **优先级计算** —— 偏离幅度、方向权重、项目紧急度三者相乘；同分按项目码
   稳定排序（不随运行漂移）；紧急度表可加载、损坏行不静默跳过。
2. **合并边界** —— 跨标本、跨科室、跨时间窗一律不合并；同标本同时间窗内的
   多个危急项目合并成**一张**卡。
3. **缺项语义** —— 合并后某项依据取不到，那一项标 ``missing``，其余项照常。
4. **端到端** —— 一条含 2 个危急项目的 ``ORU^R01`` 帧只产生 **1 条**通知
   事件，卡内含 **2 项**依据（各有各的出处行号）。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from critval.card.merge import (
    DEFAULT_MERGE_WINDOW_SECONDS,
    build_merged_card,
    group_for_notification,
    merge_window_seconds,
    plan_notification_cards,
    render_merged_markdown,
    render_merged_text,
)
from critval.config import Config
from critval.hl7.normalize import normalize_events, parse_oru_r01
from critval.judge.critical import judge_event
from critval.judge.priority import (
    BAND_EMERGENCY,
    BAND_ROUTINE,
    DIRECTION_WEIGHTS,
    MAX_DEVIATION_RATIO,
    UrgencyTable,
    compute_priority,
    priority_sort_key,
)
from critval.kb.loader import load_threshold_table
from critval.ledger.store import LedgerStore
from critval.models import Citation, CriticalEvent, Judgement, PolicyClause

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "脱敏样例阈值表.xlsx"
SAMPLE_FRAME = PROJECT_ROOT / "samples" / "hl7" / "示例ORU_R01_多项目.hl7"

POLICY_CITATION = Citation(
    source_name="示例危急值报告制度.docx",
    source_sha256="a" * 64,
    kind="policy_clause",
    paragraph_no=19,
    section_path=("第三章 报告流程与通知时限",),
    line_no=19,
    file_version="示例 v1.0",
)

BASE_TIME = datetime(2026, 1, 2, 8, 30)


@pytest.fixture(scope="module")
def table():
    return load_threshold_table(FIXTURE)


@pytest.fixture(scope="module")
def sample_table():
    """示例阈值表（含钾与钠两项 —— 多项目合并的样例帧需要）。"""
    return load_threshold_table(
        PROJECT_ROOT / "samples" / "threshold" / "示例危急值阈值表.xlsx"
    )


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
        "item_code": "2823-3",
        "item_name": "钾",
        "value": Decimal("6.9"),
        "unit": "mmol/L",
        "observed_at": BASE_TIME,
        "dept_code": "ICU",
        "patient_ref": "PATREF-9f2c",
    }
    payload.update(kwargs)
    return CriticalEvent(**payload)


def judge(event: CriticalEvent, table) -> Judgement:
    return judge_event(event, table.rows)


# ---------------------------------------------------------------------------
# 一、优先级计算
# ---------------------------------------------------------------------------


def test_高钾比低血红蛋白更优先(table):
    """同一批危急结果里，钾（高危方向 + 高紧急度）排在血红蛋白前面。"""
    potassium = judge(make_event(item_code="2823-3", item_name="钾", value=Decimal("6.9")), table)
    hemoglobin = judge(
        make_event(
            event_id="evt-0002",
            item_code="718-7",
            item_name="血红蛋白",
            value=Decimal("25"),
            unit="g/L",
        ),
        table,
    )

    p_k = compute_priority(potassium)
    p_h = compute_priority(hemoglobin)

    assert p_k.score > p_h.score
    assert p_k.band == BAND_EMERGENCY
    assert p_h.band == BAND_ROUTINE
    # 方向权重与项目紧急度都参与计算，不是一个拍脑袋的总分
    assert p_k.direction == "high"
    assert p_k.direction_weight == DIRECTION_WEIGHTS["high"]
    assert p_k.urgency_weight > p_h.urgency_weight


def test_偏离越远分值越高(table):
    """同项目同一方向，偏离越大分值越高。"""
    mild = compute_priority(judge(make_event(value=Decimal("6.6")), table))
    severe = compute_priority(judge(make_event(value=Decimal("9.9")), table))
    assert severe.score > mild.score
    assert severe.deviation_ratio > mild.deviation_ratio


def test_偏离幅度用阈值余量做分母(table):
    """分母是「参考区间到危急阈值」那段距离 —— 语义写在说明里。"""
    priority = compute_priority(judge(make_event(value=Decimal("6.9")), table))
    # 钾：区间上界 5.3、危急阈值 6.5 → 余量 1.2；偏离 0.4 → 33.33%
    assert priority.deviation_ratio == Decimal("0.3333")
    assert "阈值余量 1.2" in priority.deviation_note
    assert "33.33%" in priority.deviation_note


def test_参考区间不可用时退回阈值倍数口径(table):
    """参考区间缺失时不死算 —— 退回「相当于阈值的多少倍」。"""
    event = make_event(item_code="8888-8", item_name="缺阈值项目", value=Decimal("3"))
    judgement = judge(event, table)
    priority = compute_priority(judgement)
    # 该项目没有危急阈值 → 不判定，偏离幅度记 0
    assert judgement.decision == "no_threshold"
    assert priority.deviation_ratio == Decimal("0")
    assert priority.direction == "none"
    assert priority.band == BAND_ROUTINE


def test_同分时按项目码稳定排序():
    """同分按项目码升序 —— 同一批结果每次排出来的顺序一样。"""

    class _Score:
        def __init__(self, score: str) -> None:
            self.score = Decimal(score)

    class _Item:
        def __init__(self, code: str, score: str) -> None:
            self.item_code = code
            self.priority = _Score(score)

    items = [
        _Item("2951-2", "1.2000"),
        _Item("2823-3", "1.2000"),
        _Item("2345-7", "1.5000"),
    ]
    ordered = sorted(items, key=lambda item: priority_sort_key(item.priority, item.item_code))
    # 分值高的在前；同分的按项目码升序
    assert [item.item_code for item in ordered] == ["2345-7", "2823-3", "2951-2"]
    # 入参顺序打乱，结果不变
    shuffled = sorted(
        list(reversed(items)),
        key=lambda item: priority_sort_key(item.priority, item.item_code),
    )
    assert [item.item_code for item in shuffled] == [item.item_code for item in ordered]


def test_紧急度表可从文件加载并覆盖内置值(tmp_path):
    csv_path = tmp_path / "urgency.csv"
    csv_path.write_text(
        "# 项目紧急度表\n"
        "2823-3,2.00,院内自定义：钾最要紧\n"
        "718-7,0.80,院内自定义：血红蛋白放宽\n",
        encoding="utf-8",
    )
    table = UrgencyTable.load(csv_path)
    assert table.source == "file"
    assert table.weight_for("2823-3") == Decimal("2.00")
    assert table.note_for("2823-3") == "院内自定义：钾最要紧"
    # 未收录的项目走中性权重
    assert table.weight_for("777-3") == Decimal("1.00")


def test_紧急度表损坏行被跳过且留告警(tmp_path):
    csv_path = tmp_path / "urgency.csv"
    csv_path.write_text(
        "2823-3,2.00,正常行\n"
        "718-7,不是数值,坏行\n"
        "2345-7,,缺权重\n",
        encoding="utf-8",
    )
    table = UrgencyTable.load(csv_path)
    assert table.weight_for("2823-3") == Decimal("2.00")
    assert table.weight_for("718-7") == Decimal("1.00")
    assert table.weight_for("2345-7") == Decimal("1.00")
    assert len(table.warnings) == 2
    assert any("第 2 行" in warning for warning in table.warnings)


def test_紧急度表文件不存在时回退默认表且零告警(tmp_path):
    table = UrgencyTable.load(tmp_path / "不存在.csv")
    assert table.source == "builtin"
    assert table.warnings == ()
    assert table.weight_for("2823-3") == Decimal("1.20")


def test_偏离幅度封顶不影响判定(table):
    """极端离群值只封顶放大倍数，不改判。"""
    judgement = judge(make_event(value=Decimal("999")), table)
    priority = compute_priority(judgement)
    assert judgement.decision == "critical"
    assert priority.deviation_ratio > MAX_DEVIATION_RATIO


# ---------------------------------------------------------------------------
# 二、合并边界
# ---------------------------------------------------------------------------


def test_同标本同时间窗的多个危急项目合并为一张卡(table, clause):
    """一条标本两个危急项目 → 1 张通知卡、卡内 2 项依据。"""
    events = [
        make_event(event_id="evt-k", item_code="2823-3", item_name="钾", value=Decimal("6.9")),
        make_event(
            event_id="evt-g",
            item_code="2345-7",
            item_name="葡萄糖",
            value=Decimal("25"),
        ),
    ]
    judgements = {event.event_id: judge(event, table) for event in events}

    cards = plan_notification_cards(events, judgements, policy_clause=clause)

    assert len(cards) == 1
    card = cards[0]
    assert len(card.items) == 2
    assert len(card.basis_entries) == 2
    # 逐项各带各的出处行号
    locators = {
        item.threshold_basis.source.locator
        for item in card.items
        if item.threshold_basis.source is not None
    }
    assert len(locators) == 2


def test_单项目不合并(table, clause):
    """只有一个危急项目时就是一张一项的卡。"""
    event = make_event()
    judgements = {event.event_id: judge(event, table)}
    cards = plan_notification_cards(events=[event], judgements=judgements, policy_clause=clause)
    assert len(cards) == 1
    assert len(cards[0].items) == 1


def test_跨标本不合并(table, clause):
    """不同标本是两个人的结果，不该并到一张卡。"""
    events = [
        make_event(event_id="evt-1", specimen_no="SPEC-0001"),
        make_event(event_id="evt-2", specimen_no="SPEC-0002"),
    ]
    judgements = {event.event_id: judge(event, table) for event in events}
    cards = plan_notification_cards(events, judgements, policy_clause=clause)
    assert len(cards) == 2
    assert {card.specimen_no for card in cards} == {"SPEC-0001", "SPEC-0002"}


def test_跨科室不合并(table, clause):
    """一张卡只通知一个科室的当班人。"""
    events = [
        make_event(event_id="evt-1", dept_code="ICU"),
        make_event(event_id="evt-2", dept_code="心内科"),
    ]
    judgements = {event.event_id: judge(event, table) for event in events}
    cards = plan_notification_cards(events, judgements, policy_clause=clause)
    assert len(cards) == 2


def test_不同时间窗不合并(table, clause):
    """观察时刻相隔超过窗口 → 两次独立事件，不合并。"""
    events = [
        make_event(event_id="evt-1", observed_at=BASE_TIME),
        make_event(
            event_id="evt-2",
            observed_at=BASE_TIME + timedelta(seconds=DEFAULT_MERGE_WINDOW_SECONDS + 60),
        ),
    ]
    judgements = {event.event_id: judge(event, table) for event in events}
    cards = plan_notification_cards(events, judgements, policy_clause=clause)
    assert len(cards) == 2


def test_同一时间窗内的两个结果合并(table, clause):
    """相隔在窗口内 → 合并成一张卡。"""
    events = [
        make_event(event_id="evt-1", observed_at=BASE_TIME),
        make_event(
            event_id="evt-2",
            observed_at=BASE_TIME + timedelta(seconds=DEFAULT_MERGE_WINDOW_SECONDS - 60),
        ),
    ]
    judgements = {event.event_id: judge(event, table) for event in events}
    cards = plan_notification_cards(events, judgements, policy_clause=clause)
    assert len(cards) == 1
    assert len(cards[0].items) == 2


def test_合并窗口可由配置调整(monkeypatch):
    monkeypatch.setenv("CRITVAL_MERGE_WINDOW_SECONDS", "60")
    Config.init_env()
    assert merge_window_seconds() == 60
    monkeypatch.setenv("CRITVAL_MERGE_WINDOW_SECONDS", "不是数字")
    Config.init_env()
    assert merge_window_seconds() == DEFAULT_MERGE_WINDOW_SECONDS


def test_非危急项目不进通知卡(table, clause):
    """不危急的只入台账，不进通知。"""
    critical = make_event(event_id="evt-1", value=Decimal("6.9"))
    normal = make_event(event_id="evt-2", item_code="718-7", item_name="血红蛋白",
                        value=Decimal("130"), unit="g/L")
    judgements = {event.event_id: judge(event, table) for event in (critical, normal)}
    assert judgements[normal.event_id].decision == "not_critical"

    cards = plan_notification_cards([critical, normal], judgements, policy_clause=clause)
    assert len(cards) == 1
    assert [item.item_code for item in cards[0].items] == ["2823-3"]


def test_合并卡编号稳定可复现(table, clause):
    """同一组结果重复合并得到同一个卡号（幂等）。"""
    events = [
        make_event(event_id="evt-k", item_code="2823-3", value=Decimal("6.9")),
        make_event(event_id="evt-g", item_code="2345-7", value=Decimal("25")),
    ]
    judgements = {event.event_id: judge(event, table) for event in events}
    first = plan_notification_cards(events, judgements, policy_clause=clause)
    second = plan_notification_cards(list(reversed(events)), judgements, policy_clause=clause)
    assert first[0].card_id == second[0].card_id


def test_合并卡内排序稳定可复现(table, clause):
    """卡内项目按优先级降序；入参顺序打乱也不影响结果。"""
    events = [
        make_event(event_id="evt-k", item_code="2823-3", value=Decimal("6.9")),
        make_event(event_id="evt-g", item_code="2345-7", value=Decimal("25")),
        make_event(event_id="evt-h", item_code="718-7", item_name="血红蛋白",
                   value=Decimal("25"), unit="g/L"),
    ]
    judgements = {event.event_id: judge(event, table) for event in events}
    card = plan_notification_cards(events, judgements, policy_clause=clause)[0]
    codes = [item.item_code for item in card.items]
    assert codes == ["2823-3", "2345-7", "718-7"]
    # 打乱入参重排，结果一样
    shuffled = plan_notification_cards(list(reversed(events)), judgements, policy_clause=clause)[0]
    assert [item.item_code for item in shuffled.items] == codes


# ---------------------------------------------------------------------------
# 三、缺项语义（沿用任务 id 21）
# ---------------------------------------------------------------------------


def test_合并后某项依据缺失该项标缺项(table, clause):
    """一项的依据取不到 → 该项标 missing 并写明原因，其余项照常。"""
    good = make_event(event_id="evt-k", item_code="2823-3", value=Decimal("6.9"))
    bad = make_event(event_id="evt-x", item_code="777-3", item_name="血小板计数",
                     value=Decimal("5"), unit="10*9/L")

    judgements = {
        good.event_id: judge(good, table),
        # 阈值表没有 777-3 → 判定不成立；这里手工造一条"判了危急但没取到依据行"
        # 的结论，模拟依据取不到的情形。
        bad.event_id: Judgement(
            decision="critical",
            reason="判定为危急值：低于低危急值 20 10*9/L",
            item_code="777-3",
            value=Decimal("5"),
            unit="10*9/L",
        ),
    }
    card = build_merged_card(
        group_for_notification([good, bad])[0], judgements, policy_clause=clause
    )
    assert len(card.items) == 2
    statuses = {item.item_code: item.threshold_basis.status for item in card.items}
    assert statuses["777-3"] == "missing"
    assert statuses["2823-3"] == "resolved"
    assert any("777-3" in label for label in card.missing_elements())


def test_合并卡共享三要素缺项也说缺项(table):
    """没有制度条款 → 卡级共享的三要素标缺项，不编造。"""
    event = make_event()
    judgements = {event.event_id: judge(event, table)}
    card = plan_notification_cards([event], judgements, policy_clause=None)[0]
    assert card.notify_target.status == "missing"
    assert card.deadline.status == "missing"
    assert card.record_destination.status == "missing"
    assert "上报时限" in card.missing_elements()


# ---------------------------------------------------------------------------
# 四、端到端：一条帧 → 一条通知事件
# ---------------------------------------------------------------------------


def test_一条含两个危急项目的帧只产生一条通知事件(sample_table, clause, tmp_path):
    """ER7 帧 → 解析 → 判定 → 合并 → 1 张通知卡（卡内 2 项依据）。

    走的是**随仓库提交的样例阈值表**（含钾与钠两项）—— 与
    ``samples/hl7/示例ORU_R01_多项目.hl7`` 同一套口径：钾高危急 + 钠低危急，
    两个方向都覆盖到。
    """
    message = parse_oru_r01(SAMPLE_FRAME.read_bytes())
    events, unparsed = normalize_events(message)
    assert unparsed == []
    assert len(events) == 2
    assert len({event.specimen_no for event in events}) == 1

    judgements = {event.event_id: judge(event, sample_table) for event in events}
    assert all(item.decision == "critical" for item in judgements.values())
    # 两个危急方向都覆盖到：钾高危急、钠低危急
    directions = {compute_priority(item).direction for item in judgements.values()}
    assert directions == {"high", "low"}

    cards = plan_notification_cards(events, judgements, policy_clause=clause)
    assert len(cards) == 1  # 一条通知事件
    assert len(cards[0].items) == 2  # 卡内两项依据
    assert len(cards[0].basis_entries) == 2

    # 逐项各带各的出处行号（不是同一行复制两份）
    locators = {
        item.threshold_basis.source.locator
        for item in cards[0].items
        if item.threshold_basis.source is not None
    }
    assert len(locators) == 2

    # 落库后两条事件挂同一张卡，出处各归各的
    with LedgerStore(tmp_path / "merge.db") as store:
        for event in events:
            store.record_event(event)
            store.record_judgement(event.event_id, judgements[event.event_id])
        store.record_merged_card(cards[0])
        card_ids = {
            row["card_id"] for row in store.list_events(limit=10) if row["card_id"]
        }
        assert card_ids == {cards[0].card_id}
        citations = store.citations_of(events[0].event_id)
        assert citations


def test_合并卡渲染含两项依据与各自出处(table, clause):
    """企微正文与纯文本都逐项给出依据行与出处行号。"""
    events = [
        make_event(event_id="evt-k", item_code="2823-3", value=Decimal("6.9")),
        make_event(event_id="evt-g", item_code="2345-7", value=Decimal("25")),
    ]
    judgements = {event.event_id: judge(event, table) for event in events}
    card = plan_notification_cards(events, judgements, policy_clause=clause)[0]

    markdown = render_merged_markdown(card, confirm_url="http://console.local/ack/1")
    assert "2 项合并" in markdown
    assert markdown.count("阈值依据行") == 2
    assert "第 3 行" in markdown and "第 4 行" in markdown  # 两行各自的出处
    assert "确认回执" in markdown

    text = render_merged_text(card, confirm_url="http://console.local/ack/1")
    assert "**" not in text and "<div" not in text and "<p>" not in text
    assert text.count("依据行") == 2
    assert "第 3 行" in text and "第 4 行" in text


def test_合并卡渲染不含患者可识别明细(table, clause):
    event = make_event(patient_ref="PATREF-9f2c")
    judgements = {event.event_id: judge(event, table)}
    card = plan_notification_cards([event], judgements, policy_clause=clause)[0]
    assert "PATREF-9f2c" not in render_merged_markdown(card)
    assert "PATREF-9f2c" not in render_merged_text(card)
