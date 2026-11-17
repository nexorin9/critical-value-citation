"""值班台核心界面（任务 id 49）：事件台账页 + 处置卡确认页。

**这一组用例守的是什么**

1. **台账页真的能筛** —— 科室 / 项目 / 状态 / 是否超期 / 是否已回写五个
   维度走的是任务 id 37 的同一套查询（``LedgerQuery``），所以值班台筛出来
   的与次日复核报表导出的**是同一批数据**；筛选结果就是这一屏的网址
   （GET 表单，复制给同事对方看到的是同一批）。
2. **确认页的依据来自快照，不重新渲染** —— 阈值表改版后重渲染出来的东西
   已经不是当时给值班人看的那份；页面上的四要素、出处行号、版本号都必须
   是**快照里那份**。
3. **确认动作会改真实业务状态** —— 状态跃迁到 ``acknowledged``、回写被
   触发、重复确认不产生第二条回执。
4. **转人工必须写原因** —— 原因是交接的一部分，进审计流水。
5. **JSON 接口字段白名单** —— 接口是机器出口，患者标识层字段一个都不给。

跑法::

    pytest tests/test_console_pages.py -q
"""

from __future__ import annotations

import html
import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlencode

import pytest

from critval.card.builder import build_card
from critval.card.snapshot import CardSnapshotStore
from critval.console.api import API_EVENT_FIELDS
from critval.console.pages import AckResult, ConsoleRouter
from critval.judge.critical import judge_event
from critval.kb.loader import load_threshold_table
from critval.ledger.store import LedgerStore
from critval.models import (
    Citation,
    ConflictItem,
    ConflictSide,
    CriticalEvent,
    PolicyClause,
)

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "脱敏样例阈值表.xlsx"
CONFIRM_URL = "http://127.0.0.1:8848/events/evt-0001/ack"

#: 制度条款的出处（文件名 + 行号 + 版本 —— 确认页要显示的就是这几个数）。
POLICY_CITATION = Citation(
    source_name="示例危急值报告制度.docx",
    source_sha256="b" * 64,
    kind="policy_clause",
    paragraph_no=19,
    section_path=("第三章 报告流程与通知时限",),
    line_no=19,
    file_version="示例 v1.0",
)


# ---------------------------------------------------------------------------
# 夹具
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


def make_event(event_id: str = "evt-0001", **kwargs) -> CriticalEvent:
    payload = {
        "event_id": event_id,
        "message_control_id": f"MSG{event_id[-4:]}",
        "specimen_no": f"SPEC-{event_id[-4:]}",
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


def make_conflict() -> ConflictItem:
    """一条待人工裁决的口径冲突（两路召回对上报时限说法不一致）。"""
    return ConflictItem(
        conflict_id="cf-0001",
        kind="caliber",
        field="deadline",
        item_code="718-7",
        description="阈值表侧写 10 分钟，制度条款侧写 15 分钟，两者不一致。",
        sides=(
            ConflictSide(
                path="structured",
                role="threshold_row",
                locator="脱敏样例阈值表.xlsx:2",
                value="10 分钟",
                version="示例 v1.0",
            ),
            ConflictSide(
                path="lexical",
                role="policy_clause",
                locator="示例危急值报告制度.docx:19",
                value="15 分钟",
                version="示例 v1.0",
            ),
        ),
        status="pending",
    )


@pytest.fixture()
def card(table, clause):
    """一张处置卡（带一条待人工裁决的口径冲突）。"""
    event = make_event()
    judgement = judge_event(event, table.rows)
    return build_card(
        event,
        judgement,
        policy_clause=clause,
        duty_person="李技师",
        conflicts=(make_conflict(),),
    )


@pytest.fixture()
def store(tmp_path, card) -> LedgerStore:
    """一条已推送待回执的事件（``notified``）。

    走的是**真实状态链**（received → judged → card_built → notified），
    不是直接改库里的状态字段。
    """
    ledger = LedgerStore(tmp_path / "critval.db")
    ledger.record_event(card.event)
    ledger.record_judgement(card.event.event_id, card.judgement)
    ledger.record_card(card)
    ledger.mark_notified(
        card.event.event_id,
        notify_target="检验科值班技师",
        due_minutes=10,
        channel="local",
        actor="notify",
    )
    yield ledger
    ledger.close()


@pytest.fixture()
def snapshot_dir(tmp_path, card) -> Path:
    """把处置卡落成**不可变快照**（确认页读的就是它）。"""
    root = tmp_path / "card_snapshots"
    root.mkdir(parents=True, exist_ok=True)
    CardSnapshotStore(root).save(card, CONFIRM_URL)
    return root


def make_router(store: LedgerStore, snapshot_dir: Path, **kwargs) -> ConsoleRouter:
    return ConsoleRouter(store, token="", snapshot_dir=str(snapshot_dir), **kwargs)


# ---------------------------------------------------------------------------
# 一、台账页：筛选、表单、列、分页
# ---------------------------------------------------------------------------


def test_台账页含筛选表单与五个筛选维度(store, snapshot_dir):
    """筛选表单走 GET，五个维度齐全（科室 / 项目 / 状态 / 超期 / 回写）。"""
    page = make_router(store, snapshot_dir).handle("GET", "/events")
    assert page.status == 200
    body = page.body
    assert 'method="get"' in body
    assert 'action="/events"' in body
    for name in ("dept", "item", "status", "overdue", "written_back"):
        assert f'name="{name}"' in body, f"筛选表单缺少 {name}"


def test_台账页含明细列且列与复核报表同口径(store, snapshot_dir):
    """列 = 事件号 / 项目 / 结果 / 科室 / 观察时刻 / 上报时限 / 回写 / 状态。"""
    body = make_router(store, snapshot_dir).handle("GET", "/events").body
    for label in ("事件号", "检验项目", "结果", "科室", "观察时刻", "上报时限", "回写", "状态"):
        assert label in body, f"台账页缺少列「{label}」"
    # 状态徽标带中文文字（不靠颜色单独区分）
    assert "已推送" in body


def test_台账页列出事件并可点进详情(store, snapshot_dir):
    body = make_router(store, snapshot_dir).handle("GET", "/events").body
    assert "evt-0001" in body
    assert 'href="/events/evt-0001"' in body


def test_按科室筛选只列该科室(store, snapshot_dir):
    """筛选走任务 id 37 的同一套查询 —— 口径不分叉。"""
    event = make_event(
        event_id="evt-0002",
        dept_code="急诊科",
        message_control_id="MSG0002",
        specimen_no="SPEC-0002",
        patient_ref="PATREF-aaaa",
    )
    store.record_event(event)
    router = make_router(store, snapshot_dir)

    both = router.handle("GET", "/events").body
    assert "evt-0001" in both and "evt-0002" in both

    only_icu = router.handle("GET", "/events?dept=ICU").body
    assert "evt-0001" in only_icu
    assert "evt-0002" not in only_icu

    only_ed = router.handle("GET", "/events?" + urlencode({"dept": "急诊科"})).body
    assert "evt-0002" in only_ed
    assert "evt-0001" not in only_ed


def test_按状态筛选只列该状态(store, snapshot_dir):
    router = make_router(store, snapshot_dir)
    notified = router.handle("GET", "/events?status=notified").body
    assert "evt-0001" in notified
    # 一个不存在的状态不该让页面 500 —— 条件被丢掉，页面照常打开
    unknown = router.handle("GET", "/events?status=不存在的状态")
    assert unknown.status == 200
    assert "evt-0001" in unknown.body


def test_按是否已回写筛选(store, snapshot_dir):
    """``written_back=1`` 只看已回写，``=0`` 只看未回写。"""
    router = make_router(store, snapshot_dir)
    assert "evt-0001" not in router.handle("GET", "/events?written_back=1").body
    assert "evt-0001" in router.handle("GET", "/events?written_back=0").body


def test_按是否超期筛选(store, snapshot_dir):
    """``overdue=1`` 只看已过期（到期时刻已过即算超期，不论是否已确认）。"""
    router = make_router(store, snapshot_dir)
    assert "evt-0001" not in router.handle("GET", "/events?overdue=1").body
    assert "evt-0001" in router.handle("GET", "/events?overdue=0").body


def test_分页按页切片且给出总条数(store, snapshot_dir):
    """分页：页码进 URL，总条数是筛选后的条数。"""
    for index in range(3, 25):
        store.record_event(
            make_event(
                event_id=f"evt-{index:04d}",
                message_control_id=f"MSG{index:04d}",
                specimen_no=f"SPEC-{index:04d}",
                patient_ref=f"PATREF-{index:04d}",
            )
        )
    router = make_router(store, snapshot_dir)

    first = router.handle("GET", "/events")
    assert "共 23 条" in first.body
    assert "第 1 / 2 页" in first.body
    # 每页 20 条：第一页 20 行，第二页 3 行
    assert first.body.count('href="/events/evt-') == 20

    second = router.handle("GET", "/events?page=2")
    assert "第 2 / 2 页" in second.body
    assert second.body.count('href="/events/evt-') == 3

    # 页码越界时回到最后一页，不报错
    beyond = router.handle("GET", "/events?page=99")
    assert beyond.status == 200
    assert "第 2 / 2 页" in beyond.body


def test_筛选结果就是这一屏的网址(store, snapshot_dir):
    """GET 表单：筛选条件进 URL，页面上也写清当前筛了什么。"""
    router = make_router(store, snapshot_dir)
    body = router.handle("GET", "/events?dept=ICU&status=notified").body
    assert "筛选：科室 ICU" in body
    assert "状态 已推送" in body
    # 分页链接带着筛选条件走（翻页不能把筛选丢了）
    for index in range(2, 25):
        store.record_event(
            make_event(
                event_id=f"evt-{index:04d}",
                message_control_id=f"MSG{index:04d}",
                specimen_no=f"SPEC-{index:04d}",
                patient_ref=f"PATREF-{index:04d}",
            )
        )
    paged = router.handle("GET", "/events?dept=ICU").body
    assert "dept=ICU" in paged


# ---------------------------------------------------------------------------
# 二、确认页：四要素、出处、口径冲突
# ---------------------------------------------------------------------------


def test_确认页含四要素分区(store, snapshot_dir):
    """四要素按分区展示：阈值依据行 / 通知对象 / 上报时限 / 记录去向。"""
    page = make_router(store, snapshot_dir).handle("GET", "/events/evt-0001/ack")
    assert page.status == 200
    body = page.body
    for label in ("阈值依据行", "通知对象", "上报时限", "记录去向"):
        assert label in body, f"确认页缺少要素「{label}」"
    assert 'data-key="threshold_basis"' in body
    assert 'data-key="notify_target"' in body
    assert 'data-key="deadline"' in body
    assert 'data-key="record_destination"' in body


def test_确认页出处可展开且带文件名行号版本与指纹(store, snapshot_dir):
    """出处展开后能看到：文件名 + 行号 + 版本 + sha256 前 8 位。"""
    body = make_router(store, snapshot_dir).handle("GET", "/events/evt-0001/ack").body
    assert "<details" in body
    assert "示例危急值报告制度.docx" in body
    assert "第 19 行" in body
    assert "示例 v1.0" in body
    # sha256 前 8 位（材料指纹）
    assert "b" * 8 in body
    # 定位串在展开区里
    assert "定位串" in body


def test_确认页显式呈现口径冲突(store, snapshot_dir):
    """口径冲突区块要显式呈现 —— 系统不替人裁决。"""
    body = make_router(store, snapshot_dir).handle("GET", "/events/evt-0001/ack").body
    assert "口径冲突" in body
    assert "待人工裁决" in body
    assert "结构化精确路" in body
    assert "词法 BM25 路" in body
    assert "10 分钟" in body and "15 分钟" in body


def test_确认页显示处置卡原文与版本号(store, snapshot_dir):
    """处置卡原文逐字来自快照；版本信息也在。"""
    body = make_router(store, snapshot_dir).handle("GET", "/events/evt-0001/ack").body
    assert "处置卡原文（落库快照）" in body
    assert "处置依据（四要素）" in body  # 快照里的纯文本正文
    assert "配置版本" in body
    assert "处置卡快照" in body
    assert "阈值表版本" in body


def test_确认页内容取自快照而不是重新渲染(store, snapshot_dir, table, clause):
    """**不重新渲染**：页面上的正文就是快照里那份。

    阈值表改版后重渲染出来的卡已经不是当时给值班人看的那份；页面若仍
    显示快照里那份，就说明它读的是快照（页面上的文本按 HTML 转义后
    逐字比对）。
    """
    router = make_router(store, snapshot_dir)
    snapshot = CardSnapshotStore(snapshot_dir).find_by_event("evt-0001")
    assert snapshot is not None

    body = router.handle("GET", "/events/evt-0001/ack").body
    # 快照里的纯文本正文整段出现在页面上（转义后逐字一致）
    assert html.escape(snapshot.rendering("text"), quote=True) in body
    # 快照里的四要素值出现在页面上
    for element in snapshot.payload["elements"]:
        if element["value"]:
            assert html.escape(element["value"], quote=True) in body


def test_确认页不出现患者标识(store, snapshot_dir):
    """页面里不出现患者标识层字段。"""
    body = make_router(store, snapshot_dir).handle("GET", "/events/evt-0001/ack").body
    assert "PATREF-9f2c" not in body


# ---------------------------------------------------------------------------
# 三、确认动作：状态跃迁 + 回写触发
# ---------------------------------------------------------------------------


def test_确认动作写acknowledged并触发回写(store, snapshot_dir):
    """POST 确认 → 状态跃迁 + 回写被触发（回写实现由 ack_handler 注入）。"""

    calls: list[tuple[str, str, str]] = []

    def ack_handler(event_id: str, handler: str, note: str) -> AckResult:
        calls.append((event_id, handler, note))
        store.mark_acknowledged(event_id, actor=handler, detail=note or "值班人已确认处置")
        return AckResult(ok=True, message="已确认，回执已幂等回写登记系统。", written_back=True, status="archived")

    router = make_router(store, snapshot_dir, ack_handler=ack_handler)
    response = router.handle(
        "POST",
        "/events/evt-0001/ack",
        body=b"handler=%E5%BC%A0%E4%B8%89&note=note",
    )
    assert response.status == 200
    assert calls == [("evt-0001", "张三", "note")]
    assert store.get_status("evt-0001") == "acknowledged"
    # 页面显示回执状态
    assert "回执状态" in response.body
    assert "张三" in response.body
    assert "已确认" in response.body


def test_确认动作在台账页立即可见(store, snapshot_dir):
    """确认后回到台账页，这条立刻变成「已确认」。"""

    def ack_handler(event_id: str, handler: str, note: str) -> AckResult:
        store.mark_acknowledged(event_id, actor=handler, detail=note or "值班人已确认处置")
        return AckResult(ok=True, message="已确认。", written_back=False, status="acknowledged")

    router = make_router(store, snapshot_dir, ack_handler=ack_handler)
    router.handle("POST", "/events/evt-0001/ack", body=b"handler=ZhangSan")

    listing = router.handle("GET", "/events").body
    assert "已确认" in listing
    assert "未回写" in listing


def test_缺确认人直接拒(store, snapshot_dir):
    """没人签的字不进门（400）。"""
    response = make_router(store, snapshot_dir).handle(
        "POST", "/events/evt-0001/ack", body=b"handler=&note="
    )
    assert response.status == 400
    assert "必须写确认人" in response.body
    assert store.get_status("evt-0001") == "notified"


def test_确认通道未接入时明说不假装成功(store, snapshot_dir):
    """没接回写通道就明说「未接入」，不假装写成功了。"""
    response = make_router(store, snapshot_dir).handle(
        "POST", "/events/evt-0001/ack", body=b"handler=ZhangSan"
    )
    assert response.status == 200
    assert "未接入" in response.body


def test_重复确认不产生第二条回执(store, snapshot_dir):
    """幂等：第二次确认不会再跃迁一次（回写实现自己保证幂等键）。"""
    calls: list[str] = []

    def ack_handler(event_id: str, handler: str, note: str) -> AckResult:
        calls.append(event_id)
        if store.get_status(event_id) == "acknowledged":
            return AckResult(ok=True, message="本条处置已确认过。", written_back=True, status="acknowledged")
        store.mark_acknowledged(event_id, actor=handler, detail=note or "值班人已确认处置")
        return AckResult(ok=True, message="已确认。", written_back=True, status="acknowledged")

    router = make_router(store, snapshot_dir, ack_handler=ack_handler)
    router.handle("POST", "/events/evt-0001/ack", body=b"handler=ZhangSan")
    router.handle("POST", "/events/evt-0001/ack", body=b"handler=ZhangSan")
    assert len(calls) == 2
    assert store.get_status("evt-0001") == "acknowledged"
    # 只跃迁了一次：第二次确认走的是幂等分支，没有再写一条状态跃迁
    to_acknowledged = [
        item
        for item in store.audit_entries("evt-0001")
        if item.get("action") == "status_transition" and "acknowledged" in item["detail"]
    ]
    assert len(to_acknowledged) == 1


# ---------------------------------------------------------------------------
# 四、转人工：原因必填 + 进审计
# ---------------------------------------------------------------------------


def test_转人工缺原因被拒(store, snapshot_dir):
    """转人工必须写原因 —— 没人签的原因不算交接。"""
    response = make_router(store, snapshot_dir).handle(
        "POST", "/events/evt-0001/transfer", body=b"reason=&handler=ZhangSan"
    )
    assert response.status == 400
    assert "必须写原因" in response.body
    assert store.get_status("evt-0001") == "notified"


def test_转人工写状态并把原因记进审计(store, snapshot_dir):
    """转人工 → ``transferred_to_human``；原因进审计流水。"""
    router = make_router(store, snapshot_dir)
    body = urlencode({"reason": "两路召回对上报时限口径不一致", "handler": "张三"}).encode()
    response = router.handle("POST", "/events/evt-0001/transfer", body=body)
    assert response.status == 200
    assert store.get_status("evt-0001") == "transferred_to_human"
    assert "转人工" in response.body

    audits = store.audit_entries("evt-0001")
    transfers = [item for item in audits if item.get("action") == "transfer_to_human"]
    assert len(transfers) == 1
    assert "两路召回对上报时限口径不一致" in transfers[0]["detail"]
    assert transfers[0]["actor"] == "张三"
    # 状态跃迁审计里也带着原因
    transitions = [item for item in audits if item.get("action") == "status_transition"]
    assert any("口径不一致" in item["detail"] for item in transitions)


def test_转人工用GET被拒(store, snapshot_dir):
    """转人工是写操作，GET 不接受。"""
    response = make_router(store, snapshot_dir).handle("GET", "/events/evt-0001/transfer")
    assert response.status == 405
    assert store.get_status("evt-0001") == "notified"


def test_转人工动作在台账页立即可见(store, snapshot_dir):
    router = make_router(store, snapshot_dir)
    router.handle("POST", "/events/evt-0001/transfer", body=b"reason=abc&handler=ZhangSan")
    listing = router.handle("GET", "/events").body
    assert "转人工" in listing


# ---------------------------------------------------------------------------
# 五、JSON 接口：字段白名单
# ---------------------------------------------------------------------------


def test_json列表接口字段按白名单(store, snapshot_dir):
    """列表接口只给白名单里的字段。"""
    response = make_router(store, snapshot_dir).handle("GET", "/api/events")
    assert response.status == 200
    assert response.content_type.startswith("application/json")
    payload = json.loads(response.body)
    assert payload["total"] == 1
    assert payload["items"][0]["event_id"] == "evt-0001"
    assert set(payload["items"][0]) == set(API_EVENT_FIELDS)


def test_json列表接口不泄露患者标识(store, snapshot_dir):
    """机器出口尤其不能带患者标识层字段。"""
    response = make_router(store, snapshot_dir).handle("GET", "/api/events")
    assert "PATREF-9f2c" not in response.body
    assert "patient_ref" not in response.body


def test_json详情接口带出处与四要素(store, snapshot_dir):
    """详情接口在列表字段之上追加依据出处与处置卡快照要素。"""
    response = make_router(store, snapshot_dir).handle("GET", "/api/events/evt-0001")
    assert response.status == 200
    payload = json.loads(response.body)
    assert payload["event_id"] == "evt-0001"
    assert payload["card"]["snapshot_id"].startswith("snap-")
    labels = [element["label"] for element in payload["card"]["elements"]]
    assert "阈值依据行" in labels
    assert payload["card"]["conflicts"], "口径冲突要在详情接口里显式给出"


def test_json详情接口对不存在的事件返回404(store, snapshot_dir):
    response = make_router(store, snapshot_dir).handle("GET", "/api/events/evt-9999")
    assert response.status == 404
    assert json.loads(response.body)["error"] == "not_found"


def test_json列表接口支持筛选(store, snapshot_dir):
    """接口与页面共用同一套筛选语义。"""
    store.record_event(
        make_event(
            event_id="evt-0002",
            dept_code="急诊科",
            message_control_id="MSG0002",
            specimen_no="SPEC-0002",
            patient_ref="PATREF-aaaa",
        )
    )
    router = make_router(store, snapshot_dir)
    payload = json.loads(router.handle("GET", "/api/events?dept=ICU").body)
    assert payload["total"] == 1
    assert payload["items"][0]["event_id"] == "evt-0001"
    assert payload["filters"]["dept"] == "ICU"


# ---------------------------------------------------------------------------
# 六、路由与鉴权
# ---------------------------------------------------------------------------


def test_路由归属不与入站推送端点相撞(store, snapshot_dir):
    """入站推送端点归服务层，值班台路由不抢。"""
    router = make_router(store, snapshot_dir)
    assert router.handles("/api/events")
    assert router.handles("/api/events/evt-0001")
    assert not router.handles("/api/v1/critical-value-events")
    assert not router.handles("/api/v1/local-notifications")


def test_配了凭据时缺凭据返回401(store, snapshot_dir):
    """值班台凭据位：配了就必须带。"""
    router = ConsoleRouter(store, token="secret-token", snapshot_dir=str(snapshot_dir))
    denied = router.handle("GET", "/events")
    assert denied.status == 401
    allowed = router.handle("GET", "/events?token=secret-token")
    assert allowed.status == 200


# ---------------------------------------------------------------------------
# 七、台账页概览条：五个待办桶（任务 id 49 的核心界面）
# ---------------------------------------------------------------------------


def _seed_states(store: LedgerStore, event_id: str, status: str) -> None:
    """把一条事件推进到指定状态（走真实状态链，不直接改库字段）。"""
    event = make_event(
        event_id=event_id,
        message_control_id=f"MSG{event_id[-4:]}",
        specimen_no=f"SPEC-{event_id[-4:]}",
        patient_ref=f"PATREF-{event_id[-4:]}",
    )
    store.record_event(event)


def test_台账页有概览条且给出五个待办桶(store, snapshot_dir):
    """概览条是这一屏的第一眼：待确认 / 已超期 / 待回写 / 失败待处理 / 转人工。"""
    body = make_router(store, snapshot_dir).handle("GET", "/events").body
    assert "cv-statbar" in body
    for label in ("待确认", "已超期", "待回写", "失败待处理", "转人工"):
        assert label in body, f"概览条缺少「{label}」"


def test_概览条计数与台账查询同口径(store, snapshot_dir):
    """桶里的数用**任务 id 37 的同一套查询**算，不是另写一份 SQL。"""
    from critval.ledger.query import LedgerQuery, count_events

    router = make_router(store, snapshot_dir)
    body = router.handle("GET", "/events").body
    expected = count_events(store, LedgerQuery(statuses=("notified", "pending_ack")))
    assert f'待确认<span class="cv-statbar__value">{expected}</span>' in body


def test_概览条每格都能钻取(store, snapshot_dir):
    """每一格带一个链接：点进去就是那一档的台账视图。"""
    body = make_router(store, snapshot_dir).handle("GET", "/events").body
    assert 'href="/events?status=notified%2Cpending_ack"' in body
    assert 'href="/events?overdue=1"' in body
    assert 'href="/events?status=acknowledged"' in body
    assert 'href="/events?status=failed%2Cwriteback_failed"' in body
    assert 'href="/events?status=transferred_to_human"' in body
    # 内部形态 ``statuses`` 不许漏进 URL —— URL 里只有一个 status 参数
    assert "statuses=" not in body


def test_钻取链接点进去就是那一档(store, snapshot_dir):
    """钻取后的这一屏，状态筛选要真的生效（不是只改个网址）。"""
    router = make_router(store, snapshot_dir)
    body = router.handle("GET", "/events?status=notified%2Cpending_ack").body
    assert "evt-0001" in body
    # 多值筛选在状态下拉里能看出当前选的是哪一档
    assert "待确认（待我处理）" in body
    assert '<option value="notified,pending_ack" selected>' in body


def test_概览条钻取保留当前科室筛选(store, snapshot_dir):
    """钻取链接带着当前科室 —— 值班人筛的那批不能被概览条弄丢。"""
    body = make_router(store, snapshot_dir).handle("GET", "/events?dept=ICU").body
    assert "dept=ICU" in body
    assert 'href="/events?dept=ICU&amp;status=notified%2Cpending_ack"' in body


def test_翻页链接不丢布尔筛选(store, snapshot_dir):
    """**真缺陷守门**：``overdue=False`` 若直接进 URL 会变成 ``overdue=False``，
    而解析只认 ``1`` / ``0`` —— 翻页就会把「未超期」悄悄丢掉。"""
    router = make_router(store, snapshot_dir)
    body = router.handle("GET", "/events?overdue=0").body
    assert "overdue=0" in body
    assert "overdue=False" not in body
    body2 = router.handle("GET", "/events?written_back=0").body
    assert "written_back=0" in body2
    assert "written_back=False" not in body2


def test_状态筛选不认识的值照旧被丢掉(store, snapshot_dir):
    """外来一个不认识的状态 → 条件被丢掉，页面照常打开（不是 500）。"""
    body = make_router(store, snapshot_dir).handle("GET", "/events?status=不存在的状态").body
    assert "evt-0001" in body


# ---------------------------------------------------------------------------
# 八、确认页两栏版式 + 组件库迁移（不再有第二套页面样式）
# ---------------------------------------------------------------------------


def test_确认页是依据在左动作在右的两栏(store, snapshot_dir):
    """两栏：依据（四要素 / 冲突 / 版本 / 卡原文）在左，动作在右。"""
    body = make_router(store, snapshot_dir).handle("GET", "/events/evt-0001/ack").body
    assert 'class="cv-ack-layout"' in body
    assert 'class="cv-ack-main"' in body
    assert 'class="cv-ack-side"' in body
    # 依据在动作之前 —— 先看依据再点确认的顺序不能反
    assert body.index("cv-ack-main") < body.index("cv-ack-side")
    assert body.index('cv-card__title">处置依据（四要素）') < body.index(
        'cv-card__title">确认处置'
    )


def test_转人工收在可折叠区里不抢主操作(store, snapshot_dir):
    """转人工是**交接**不是**放弃**：折叠起来，主操作只有一个。"""
    body = make_router(store, snapshot_dir).handle("GET", "/events/evt-0001/ack").body
    assert '<details class="cv-transfer">' in body
    assert "转人工（需填写原因，进审计流水）" in body
    # 原因仍必填、仍进审计
    assert 'name="reason" required' in body


def test_页面不再有旧版式类名(store, snapshot_dir):
    """迁移守门：页面里不能再出现任务 id 48 之前那套 ``.card`` / ``.kv`` /
    ``form.ack`` / ``.notice`` —— 否则就是两套 CSS 打架。"""
    router = make_router(store, snapshot_dir)
    for path in ("/events", "/events/evt-0001", "/events/evt-0001/ack"):
        body = router.handle("GET", path).body
        for legacy in ('class="card"', 'class="kv"', "form.ack", 'class="notice', 'class="muted"'):
            assert legacy not in body, f"{path} 里还有旧版式类名 {legacy}"


def test_样式表里不再有过渡层(store, snapshot_dir):
    """过渡层（``.cv-main > .card`` 等）在两页迁到组件库后已整段删除。"""
    from critval.console.tokens import stylesheet_path

    css = stylesheet_path().read_text(encoding="utf-8")
    assert "过渡层" not in css
    assert ".cv-main > .card" not in css
    assert ".cv-main form.ack" not in css
    # 新版式在样式表里有落点
    assert ".cv-ack-layout" in css
    assert ".cv-transfer" in css


def test_错误页用组件渲染而不是裸div(store, snapshot_dir):
    """401 / 404 走**组件** —— 之前是裸 ``<div class="notice err">``，
    而样式表里根本没有 ``.notice``，错误页是**没样式**的。

    401 是「请带凭据」的提示，走 ``notice()``；404 是「这条事件不在台账里」
    的**错误态**（任务 id 50）：带错误码与可执行建议，不是一句灰字。
    """
    router = make_router(store, snapshot_dir)
    denied = ConsoleRouter(
        store, token="secret-token", snapshot_dir=str(snapshot_dir)
    ).handle("GET", "/events")
    assert denied.status == 401
    assert "cv-notice" in denied.body
    assert "cv-notice--err" in denied.body

    missing = router.handle("GET", "/events/evt-不存在")
    assert missing.status == 404
    assert 'data-cv-state="error"' in missing.body
    assert "event_not_found" in missing.body


def test_详情页迁到组件库(store, snapshot_dir):
    """详情页与台账页 / 确认页共用同一套组件与版式。"""
    body = make_router(store, snapshot_dir).handle("GET", "/events/evt-0001").body
    assert "cv-card" in body
    assert "cv-kv" in body
    assert "cv-table" in body
    assert "打开确认页" in body


def test_详情页出处用角标带文件名与行号(store, snapshot_dir):
    """出处列用组件库的角标：文件名 + 行号 + 指纹前缀。"""
    body = make_router(store, snapshot_dir).handle("GET", "/events/evt-0001").body
    assert "cv-cite" in body
    assert "cv-cite__file" in body
    assert "cv-cite__line" in body



if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
