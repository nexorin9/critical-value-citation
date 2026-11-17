"""当班人解析、班次窗口判定与通知对象唯一化（任务 id 41）。

盯住五件事：

* **跨零点夜班算得对** —— ``20:00 → 08:00`` 的班，``23:40`` 与 ``03:10``
  都在班，``12:00`` 不在班；
* **通知对象唯一** —— 同科室多人在班时按角色优先级择一，择谁、为什么
  择他都能追问；
* **无人当班有兜底** —— 本班次窗口内没人在班时，科室值班长顶上，来源
  显式标为「兜底」；
* **缓存过期就转人工** —— 绝不拿过期名单冒充当前当班人；
* **卡片上的通知对象唯一且带来源标注** —— 四种情形逐个验。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from critval.card.builder import build_card
from critval.card.provenance import (
    ORIGIN_CACHE,
    ORIGIN_FALLBACK,
    ORIGIN_LIVE,
    ORIGIN_NO_DUTY,
    ORIGIN_UNAVAILABLE,
    REASON_RESOLVED,
    REASON_ROSTER_FALLBACK,
    REASON_ROSTER_FROM_CACHE,
    REASON_ROSTER_NO_DUTY_PERSON,
    REASON_ROSTER_UNAVAILABLE,
)
from critval.card.renderer import build_render_payload, render_markdown
from critval.judge.critical import judge_event
from critval.kb.loader import load_threshold_table
from critval.models import CardProvenance, Citation, CriticalEvent, PolicyClause
from critval.roster.cache import CachedRoster, RosterCache
from critval.roster.resolve import resolve_notify_target, target_provenance
from critval.roster.roles import (
    ROLE_TIER_BACKUP,
    ROLE_TIER_DIRECTOR,
    ROLE_TIER_DUTY_LEADER,
    ROLE_TIER_GENERIC,
    ROLE_TIER_PRIMARY,
    role_tier,
)
from critval.roster.shifts import parse_shift_time, shift_window_of
from critval.roster.types import (
    SELECTION_DUTY_LEADER_FALLBACK,
    SELECTION_NONE,
    SELECTION_ON_DUTY,
    DutyPerson,
)

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "脱敏样例阈值表.xlsx"

AT_NIGHT = datetime(2026, 10, 29, 23, 40)
AT_EARLY = datetime(2026, 10, 30, 3, 10)
AT_NOON = datetime(2026, 10, 29, 12, 0)

POLICY_CITATION = Citation(
    source_name="示例危急值报告制度.docx",
    source_sha256="a" * 64,
    kind="policy_clause",
    paragraph_no=19,
    section_path=("第三章 报告流程与通知时限",),
    line_no=19,
    file_version="示例 v1.0",
)


def person(name: str, role: str = "", start: str = "", end: str = "", staff: str = "") -> DutyPerson:
    """一条当班记录（测试用，字段与排班系统返回一致）。"""
    return DutyPerson(
        name=name,
        staff_id=staff,
        role=role,
        shift_start=start,
        shift_end=end,
        dept_code="ICU",
    )


# ---------------------------------------------------------------------------
# 一、班次窗口（含跨零点）
# ---------------------------------------------------------------------------


def test_跨零点夜班在凌晨仍算在班():
    night = person("李静", "检验科值班技师", "20:00", "08:00")
    window = shift_window_of(night)

    assert window.crosses_midnight is True
    assert window.covers(AT_NIGHT) is True
    assert window.covers(AT_EARLY) is True
    assert window.covers(AT_NOON) is False


def test_白班窗口边界取开闭():
    day = person("王敏", "值班护士", "08:00", "20:00")
    window = shift_window_of(day)

    assert window.crosses_midnight is False
    assert window.covers(datetime(2026, 10, 29, 8, 0)) is True
    assert window.covers(datetime(2026, 10, 29, 19, 59)) is True
    assert window.covers(datetime(2026, 10, 29, 20, 0)) is False


def test_班次到当天结束写法按24点处理():
    late = person("赵磊", "值班技师", "08:00", "24:00")
    window = shift_window_of(late)

    assert window.end_day_end is True
    assert window.covers(datetime(2026, 10, 29, 23, 59)) is True
    assert window.covers(datetime(2026, 10, 29, 7, 59)) is False


def test_只写一端时刻时判不出不在班按在班处理():
    half = person("孙倩", "值班护士", "08:00", "")
    window = shift_window_of(half)

    assert window.incomplete is True
    assert window.covers(datetime(2026, 10, 29, 3, 0)) is True
    assert "不完整" in window.describe()


def test_班次时间完全没写时按在班处理并说明():
    blank = person("周涛", "值班护士")
    window = shift_window_of(blank)

    assert window.unknown is True
    assert window.covers(AT_NIGHT) is True
    assert "未写明" in window.describe()


def test_班次时刻解析不出来时不猜():
    assert parse_shift_time("") is None
    assert parse_shift_time("待定") is None
    assert parse_shift_time("2026-10-29") is None
    assert parse_shift_time("08:00").hour == 8
    assert parse_shift_time("2026-10-29 08:00").hour == 8
    assert parse_shift_time("202610290800").hour == 8


# ---------------------------------------------------------------------------
# 二、角色优先级与唯一化
# ---------------------------------------------------------------------------


def test_角色层级识别():
    assert role_tier("检验科主责") == ROLE_TIER_PRIMARY
    assert role_tier("责任班") == ROLE_TIER_PRIMARY
    assert role_tier("备班") == ROLE_TIER_BACKUP
    assert role_tier("科室主任") == ROLE_TIER_DIRECTOR
    assert role_tier("科主任") == ROLE_TIER_DIRECTOR
    assert role_tier("科室值班长") == ROLE_TIER_DUTY_LEADER
    assert role_tier("值班护士") == ROLE_TIER_GENERIC
    assert role_tier("") == ROLE_TIER_GENERIC


def test_同科室多人在班时取主责且通知对象唯一():
    roster = [
        person("王敏", "值班护士", "08:00", "20:00", staff="A01"),
        person("李静", "检验科主责", "08:00", "20:00", staff="B02"),
        person("陈锋", "科主任", "08:00", "20:00", staff="C03"),
    ]
    target = resolve_notify_target(roster, dept_code="ICU", at=AT_NOON)

    assert target.person is not None
    assert target.person.name == "李静"
    assert target.selection == SELECTION_ON_DUTY
    assert target.reason == REASON_RESOLVED
    assert target.origin == ORIGIN_LIVE
    # 唯一：一个名字。
    assert "李静" in target.label()
    assert "王敏" not in target.label()
    assert "陈锋" not in target.label()
    # 被比较过的人留着，事后能回答「为什么不选他」。
    assert [p.name for p in target.candidates] == ["李静", "陈锋", "王敏"]


def test_主责缺位时依次退到备班再退到科主任():
    with_backup = [
        person("王敏", "值班护士", "08:00", "20:00", staff="A01"),
        person("陈锋", "备班", "08:00", "20:00", staff="C03"),
    ]
    target = resolve_notify_target(with_backup, dept_code="ICU", at=AT_NOON)
    assert target.person.name == "陈锋"

    only_director = [
        person("王敏", "值班护士", "08:00", "20:00", staff="A01"),
        person("陈锋", "科主任", "08:00", "20:00", staff="C03"),
    ]
    target = resolve_notify_target(only_director, dept_code="ICU", at=AT_NOON)
    assert target.person.name == "陈锋"


def test_名单里只有一般在班人时就是他():
    roster = [person("王敏", "值班护士", "08:00", "20:00", staff="A01")]
    target = resolve_notify_target(roster, dept_code="ICU", at=AT_NOON)

    assert target.person.name == "王敏"
    assert target.reason == REASON_RESOLVED


def test_值班长在班但另有在班人时先找在班的人():
    roster = [
        person("孙倩", "值班护士", "08:00", "20:00", staff="A01"),
        person("周涛", "科室值班长", "08:00", "20:00", staff="D04"),
    ]
    target = resolve_notify_target(roster, dept_code="ICU", at=AT_NOON)

    assert target.person.name == "孙倩"
    assert target.selection == SELECTION_ON_DUTY


def test_同层级多人时排序稳定且与入参顺序无关():
    roster = [
        person("张三", "值班护士", "08:00", "20:00", staff="A02"),
        person("李四", "值班护士", "08:00", "20:00", staff="A01"),
    ]
    first = resolve_notify_target(roster, dept_code="ICU", at=AT_NOON)
    second = resolve_notify_target(list(reversed(roster)), dept_code="ICU", at=AT_NOON)

    assert first.person.staff_id == "A01"
    assert second.person.staff_id == "A01"
    assert first.label() == second.label()


def test_观察时刻落在夜班窗口外则该人不算在班():
    roster = [person("李静", "检验科值班技师", "20:00", "08:00", staff="B02")]
    target = resolve_notify_target(roster, dept_code="ICU", at=AT_NOON)

    # 无人在班且名单里没有值班长 → 没有通知对象。
    assert target.person is None
    assert target.selection == SELECTION_NONE
    assert target.requires_human is True


# ---------------------------------------------------------------------------
# 三、无人当班 → 科室值班长兜底
# ---------------------------------------------------------------------------


def test_本班次窗口内无人时由科室值班长兜底并标注来源():
    roster = [
        person("李静", "检验科值班技师", "08:00", "20:00", staff="B02"),
        person("孙倩", "科室值班长", "08:00", "20:00", staff="D04"),
    ]
    target = resolve_notify_target(roster, dept_code="ICU", at=AT_NIGHT)

    assert target.person is not None
    assert target.person.name == "孙倩"
    assert target.selection == SELECTION_DUTY_LEADER_FALLBACK
    assert target.origin == ORIGIN_FALLBACK
    assert target.reason == REASON_ROSTER_FALLBACK
    assert "科室值班长" in target.note
    # 来源标注要写明是兜底。
    provenance = target_provenance(target)
    assert provenance.origin == ORIGIN_FALLBACK
    assert "科室值班长" in provenance.detail


def test_无人在班且名单里也没有值班长时没有通知对象():
    roster = [person("李静", "检验科值班技师", "08:00", "20:00", staff="B02")]
    target = resolve_notify_target(roster, dept_code="ICU", at=AT_NIGHT)

    assert target.person is None
    assert target.label() == ""
    assert target.origin == ORIGIN_NO_DUTY
    assert target.reason == REASON_ROSTER_NO_DUTY_PERSON
    assert target.requires_human is True
    # 缺项时也要说清「在班窗口外」的是谁、班次怎么写的。
    assert "在班窗口外" in target.note


# ---------------------------------------------------------------------------
# 四、缓存：命中可用、过期转人工
# ---------------------------------------------------------------------------


def test_缓存命中时通知对象来自缓存且带取数时刻():
    roster = [person("李静", "检验科值班技师", "08:00", "20:00", staff="B02")]
    fetched_at = datetime.now(timezone.utc) - timedelta(seconds=60)
    entry = CachedRoster(dept_code="ICU", people=tuple(roster), fetched_at=fetched_at)

    target = resolve_notify_target(
        roster,
        dept_code="ICU",
        at=AT_NOON,
        origin=ORIGIN_CACHE,
        as_of=entry.as_of_text(),
        cache_entry=entry,
        cache_ttl_seconds=900,
    )

    assert target.person.name == "李静"
    assert target.origin == ORIGIN_CACHE
    assert target.reason == REASON_ROSTER_FROM_CACHE
    assert target.as_of == entry.as_of_text()
    # 来源标注带时间戳 —— 值班人知道这份名单有多旧。
    provenance = target_provenance(target)
    assert provenance.origin == ORIGIN_CACHE
    assert provenance.as_of == entry.as_of_text()


def test_缓存过期时转人工而不是拿旧名单冒充():
    roster = [person("李静", "检验科值班技师", "08:00", "20:00", staff="B02")]
    fetched_at = datetime.now(timezone.utc) - timedelta(hours=1)
    entry = CachedRoster(dept_code="ICU", people=tuple(roster), fetched_at=fetched_at)

    target = resolve_notify_target(
        roster,
        dept_code="ICU",
        at=AT_NOON,
        origin=ORIGIN_CACHE,
        as_of=entry.as_of_text(),
        cache_entry=entry,
        cache_ttl_seconds=900,
    )

    assert target.person is None
    assert target.origin == ORIGIN_UNAVAILABLE
    assert target.reason == REASON_ROSTER_UNAVAILABLE
    assert target.requires_human is True
    assert "过期" in target.note


def test_缓存来源但缺取数时刻时无法复核有效期按转人工处理():
    roster = [person("李静", "检验科值班技师", "08:00", "20:00", staff="B02")]
    target = resolve_notify_target(
        roster,
        dept_code="ICU",
        at=AT_NOON,
        origin=ORIGIN_CACHE,
        as_of="",
    )

    assert target.person is None
    assert target.requires_human is True


def test_缓存取数时刻按本地墙钟理解不会算成未来取数(tmp_path: Path):
    """回归：东八区里缓存曾被当成「未来取的」，于是永远不过期。"""
    cache = RosterCache(tmp_path / "roster_cache.json", ttl_seconds=900)
    now = datetime.now()
    cache.put("ICU", [person("李静")], at=now)

    entry = cache.get("ICU")
    assert entry is not None
    assert entry.age_seconds() < 60
    assert entry.expired(900) is False


# ---------------------------------------------------------------------------
# 五、处置卡上的通知对象：四种情形都唯一且带来源标注
# ---------------------------------------------------------------------------


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
def event() -> CriticalEvent:
    return CriticalEvent(
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


def _card(event, table, clause, target):
    judgement = judge_event(event, table.rows)
    return build_card(
        event,
        judgement,
        policy_clause=clause,
        duty_person=target.label() or None,
        provenance=CardProvenance(duty_roster=target_provenance(target)),
    )


def _notify_element(card):
    payload = build_render_payload(card)
    return next(e for e in payload["elements"] if e["key"] == "notify_target")


def test_卡片上正常在班时通知对象唯一且状态已解决(event, table, clause):
    roster = [
        person("王敏", "值班护士", "08:00", "20:00", staff="A01"),
        person("李静", "检验科主责", "08:00", "20:00", staff="B02"),
    ]
    target = resolve_notify_target(roster, dept_code="ICU", at=AT_NOON)
    card = _card(event, table, clause, target)

    element = _notify_element(card)
    assert element["status"] == "resolved"
    assert element["reason"] == REASON_RESOLVED
    assert "李静" in element["value"]
    assert "王敏" not in element["value"]
    # 全部实时来源时卡片不挂降级标注块（没有弱来源要说明）。
    text = render_markdown(card)
    assert "【降级依据】" not in text
    assert "【依据缺失】" not in text
    assert "当班名单来源" not in text


def test_卡片上缓存来源时通知对象带时间戳且标降级(event, table, clause):
    roster = [person("李静", "检验科值班技师", "08:00", "20:00", staff="B02")]
    fetched_at = datetime.now(timezone.utc) - timedelta(seconds=60)
    entry = CachedRoster(dept_code="ICU", people=tuple(roster), fetched_at=fetched_at)
    target = resolve_notify_target(
        roster,
        dept_code="ICU",
        at=AT_NOON,
        origin=ORIGIN_CACHE,
        as_of=entry.as_of_text(),
        cache_entry=entry,
        cache_ttl_seconds=900,
    )
    card = _card(event, table, clause, target)

    element = _notify_element(card)
    assert element["status"] == "degraded"
    assert element["reason"] == REASON_ROSTER_FROM_CACHE
    assert "李静" in element["value"]
    text = render_markdown(card)
    assert "当班名单来源：缓存" in text
    assert entry.as_of_text() in text


def test_卡片上值班长兜底时通知对象唯一且标降级(event, table, clause):
    roster = [
        person("李静", "检验科值班技师", "08:00", "20:00", staff="B02"),
        person("孙倩", "科室值班长", "08:00", "20:00", staff="D04"),
    ]
    target = resolve_notify_target(roster, dept_code="ICU", at=AT_NIGHT)
    card = _card(event, table, clause, target)

    element = _notify_element(card)
    assert element["status"] == "degraded"
    assert element["reason"] == REASON_ROSTER_FALLBACK
    assert "孙倩" in element["value"]
    assert "李静" not in element["value"]
    text = render_markdown(card)
    assert "兜底人选" in text


def test_卡片上缓存过期时通知对象标缺项并转人工(event, table, clause):
    roster = [person("李静", "检验科值班技师", "08:00", "20:00", staff="B02")]
    fetched_at = datetime.now(timezone.utc) - timedelta(hours=1)
    entry = CachedRoster(dept_code="ICU", people=tuple(roster), fetched_at=fetched_at)
    target = resolve_notify_target(
        roster,
        dept_code="ICU",
        at=AT_NOON,
        origin=ORIGIN_CACHE,
        as_of=entry.as_of_text(),
        cache_entry=entry,
        cache_ttl_seconds=900,
    )
    card = _card(event, table, clause, target)

    element = _notify_element(card)
    assert element["status"] == "missing"
    assert element["reason"] == REASON_ROSTER_UNAVAILABLE
    assert element["value"] == ""
    assert card.missing_elements() == ("通知对象",)


def test_卡片上无人在班且无值班长时通知对象标缺项(event, table, clause):
    roster = [person("李静", "检验科值班技师", "08:00", "20:00", staff="B02")]
    target = resolve_notify_target(roster, dept_code="ICU", at=AT_NIGHT)
    card = _card(event, table, clause, target)

    element = _notify_element(card)
    assert element["status"] == "missing"
    assert element["reason"] == REASON_ROSTER_NO_DUTY_PERSON
    assert card.missing_elements() == ("通知对象",)


def test_通知对象始终唯一不会拼出多个人():
    roster = [
        person("王敏", "值班护士", "08:00", "20:00", staff="A01"),
        person("李静", "检验科主责", "08:00", "20:00", staff="B02"),
        person("陈锋", "备班", "08:00", "20:00", staff="C03"),
    ]
    target = resolve_notify_target(roster, dept_code="ICU", at=AT_NOON)

    names = [p.name for p in roster]
    hit = [n for n in names if n in target.label()]
    assert len(hit) == 1
    assert target.unique is True


# ---------------------------------------------------------------------------
# 六、运维 CLI：roster-resolve
# ---------------------------------------------------------------------------


SAMPLE_ROSTER = (
    Path(__file__).resolve().parent.parent
    / "samples"
    / "roster"
    / "示例排班名单_ICU.json"
)


def test_CLI解析出唯一通知对象并给出机器原因码(capsys):
    from critval.cli import main

    code = main(
        [
            "roster-resolve",
            "--roster",
            str(SAMPLE_ROSTER),
            "--dept",
            "ICU",
            "--at",
            "2026-10-29 12:00",
        ]
    )
    out = capsys.readouterr().out

    assert code == 0
    assert "李静" in out
    assert "resolved" in out
    assert "当班名单来源：排班系统实时" in out


def test_CLI按JSON输出可被机器解析(capsys):
    import json as _json

    from critval.cli import main

    main(
        [
            "roster-resolve",
            "--roster",
            str(SAMPLE_ROSTER),
            "--dept",
            "ICU",
            "--at",
            "2026-10-29 23:40",
            "--json",
        ]
    )
    payload = _json.loads(capsys.readouterr().out)

    assert payload["reason"] == REASON_RESOLVED
    assert payload["person"]["name"] == "陈锋"
    assert payload["unique"] is True
    assert payload["mode"] == "roster-resolve"


def test_CLI缓存过期时转人工并返回业务失败码(capsys):
    from critval.cli import main

    code = main(
        [
            "roster-resolve",
            "--roster",
            str(SAMPLE_ROSTER),
            "--dept",
            "ICU",
            "--at",
            "2026-10-29 12:00",
            "--origin",
            "cache",
            "--as-of",
            "2026-10-29 10:00",
        ]
    )
    out = capsys.readouterr().out

    assert code == 1
    assert "转人工" in out
    assert REASON_ROSTER_UNAVAILABLE in out


def test_CLI名单文件不存在时返回用法错误码(capsys):
    from critval.cli import main

    code = main(["roster-resolve", "--roster", "不存在/名单.json"])
    assert code == 2
    assert "不存在" in capsys.readouterr().err
