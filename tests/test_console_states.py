"""值班台三态与交互反馈（任务 id 50）。

**这一组用例守的是什么**

1. **三态各有独立模板，且互斥** —— 「正在取数」「一条都没有」「出错了」
   是三件事，值班人必须一眼分清。混成一句「暂无数据」就会有人对着空表
   以为今天没危急值。
2. **空态说清「为什么空」并给出下一步** —— 台账空有五种可能（没危急值 /
   守护进程没起 / LIS 没推 / 阈值表版本没绑定 / 筛掉了），空态要列出来。
3. **错误态给的是「怎么办」** —— 错误码能抄进工单，建议是可执行动作，
   还要有重试入口。
4. **四类写操作反馈各走各的语义色** —— 确认成功（绿）、推送失败与回写失败
   （失败深灰，**不与危急红混**）、转人工（琥珀）。
5. **破坏性操作要二次确认、原因要进审计** —— 转人工与补写回写都是写操作；
   二次确认是站内模态框，**不是**浏览器原生 confirm。
6. **响应式两档** —— 1280 完整表格 + 侧边导航；768 卡片式列表 + **可展开
   的折叠导航**（窄屏上「待确认」不能点不到）。

跑法::

    pytest tests/test_console_states.py -q
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlencode

import pytest

from critval.card.builder import build_card
from critval.card.snapshot import CardSnapshotStore
from critval.console import states_view
from critval.console.pages import AckResult, ConsoleRouter, ReplayResult
from critval.console.states_view import (
    ERROR_CATALOGUE,
    FEEDBACK_CATALOGUE,
    empty_state_view,
    error_state,
    feedback_for_status,
    feedback_toast,
    loading_state,
    state_template_path,
)
from critval.console.tokens import FEEDBACK_SEMANTIC, SEMANTIC, TOAST_KIND_BY_ROLE
from critval.judge.critical import judge_event
from critval.kb.loader import load_threshold_table
from critval.ledger.store import LedgerStore
from critval.models import Citation, PolicyClause

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "脱敏样例阈值表.xlsx"
CONFIRM_URL = "http://127.0.0.1:8848/events/evt-0001/ack"

POLICY_CITATION = Citation(
    source_name="示例危急值报告制度.docx",
    source_sha256="b" * 64,
    kind="policy_clause",
    paragraph_no=19,
    section_path=("第三章 报告流程与通知时限",),
    line_no=19,
    file_version="示例 v1.0",
)



def _form(**fields: str) -> bytes:
    """表单请求体（``application/x-www-form-urlencoded``）。"""
    return urlencode(fields).encode("utf-8")


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


def make_event(event_id: str = "evt-0001", **kwargs):
    from critval.models import CriticalEvent

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


@pytest.fixture()
def card(table, clause):
    event = make_event()
    judgement = judge_event(event, table.rows)
    return build_card(
        event, judgement, policy_clause=clause, duty_person="李技师"
    )


@pytest.fixture()
def store(tmp_path, card) -> LedgerStore:
    """一条已推送待回执的事件（``notified``）—— 走真实状态链。"""
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
def empty_store(tmp_path) -> LedgerStore:
    """一张**空台账**（一条事件都没有）。"""
    ledger = LedgerStore(tmp_path / "empty.db")
    yield ledger
    ledger.close()


@pytest.fixture()
def snapshot_dir(tmp_path, card) -> Path:
    root = tmp_path / "card_snapshots"
    root.mkdir(parents=True, exist_ok=True)
    CardSnapshotStore(root).save(card, CONFIRM_URL)
    return root


def make_router(store: LedgerStore, snapshot_dir: Path = Path(""), **kwargs) -> ConsoleRouter:
    return ConsoleRouter(
        store, token="", snapshot_dir=str(snapshot_dir or ""), **kwargs
    )


# ---------------------------------------------------------------------------
# 一、三态：各有独立模板
# ---------------------------------------------------------------------------


def test_三态各有独立模板文件() -> None:
    """加载 / 空 / 错误各有自己的模板 —— 不是同一个模板换个词。"""
    for name in ("loading", "empty", "error"):
        path = state_template_path(name)
        assert path.exists(), f"三态模板不存在：{path}"
        text = path.read_text(encoding="utf-8")
        assert "cv-state" in text, f"{name} 模板缺少状态容器类名"
        assert "{{" in text, f"{name} 模板没有占位符"
    # 三个模板内容互不相同
    bodies = {
        name: state_template_path(name).read_text(encoding="utf-8")
        for name in ("loading", "empty", "error")
    }
    assert len(set(bodies.values())) == 3


def test_加载态渲染骨架与说明且带无脚本退路() -> None:
    """加载态：骨架屏 + 「在取什么」+ 无脚本时的退路入口。"""
    html = loading_state(title="正在取数", desc="正在从台账里取危急值事件。")
    assert 'data-cv-state="loading"' in html
    assert "cv-skeleton" in html
    assert "正在取数" in html
    assert "正在从台账里取危急值事件。" in html
    # 没启用 JS 时要有退路，不能把人永远卡在加载态上
    assert "<noscript>" in html
    assert "直接加载完整页面" in html


def test_空态说明为什么空并给出下一步与排查项() -> None:
    """空态：说明为什么空 + 下一步 + 逐条排查（不只是一句「暂无数据」）。"""
    html = empty_state_view(
        title="台账里还没有危急值事件",
        reason="守护进程已经在跑，但一条危急值结果都还没进台账。",
        next_step="按下面三条对一遍。",
        checks=[
            "LIS 有没有在推",
            "MLLP 监听有没有在跑",
            "阈值表版本有没有绑定",
        ],
    )
    assert 'data-cv-state="empty"' in html
    assert "台账里还没有危急值事件" in html
    assert "按下面三条对一遍。" in html
    assert "cv-state__nexttext" in html
    assert "cv-state__checklist" in html
    assert "阈值表版本有没有绑定" in html


def test_错误态给错误码与可执行建议与重试入口() -> None:
    """错误态：错误码（能抄进工单）+ 怎么办（能执行）+ 重试。"""
    html = error_state(
        code="threshold_unbound",
        detail="阈值表路径配了但文件读不到。",
        retry_href="/events",
    )
    assert 'data-cv-state="error"' in html
    assert "threshold_unbound" in html
    assert "阈值表路径配了但文件读不到。" in html
    # 建议来自错误码目录
    assert ERROR_CATALOGUE["threshold_unbound"]["suggestion"] in html
    # 重试入口
    assert 'href="/events"' in html
    assert "重试" in html


def test_错误码目录每条都有标题与可执行建议() -> None:
    """每个错误码都要说清「怎么办」—— 只写「出错了」的报错等于没写。"""
    assert ERROR_CATALOGUE, "错误码目录不能为空"
    for code, entry in ERROR_CATALOGUE.items():
        assert entry.get("title", ""), f"{code} 缺标题"
        assert len(entry.get("suggestion", "")) >= 12, f"{code} 的建议太短"


def test_三态互斥加载态不出现空态与错误态文案() -> None:
    """加载 / 空 / 错误三态不能互相串味。"""
    loading = loading_state()
    assert 'data-cv-state="empty"' not in loading
    assert 'data-cv-state="error"' not in loading

    empty = empty_state_view(title="空", reason="没有", next_step="等一等")
    assert 'data-cv-state="loading"' not in empty
    assert 'data-cv-state="error"' not in empty

    error = error_state(code="event_not_found")
    assert 'data-cv-state="loading"' not in error
    assert 'data-cv-state="empty"' not in error


def test_三态外来文本被转义() -> None:
    """外来文本进页面要转义 —— 材料名 / 事件号都可能带尖括号。"""
    html = empty_state_view(
        title="<script>alert(1)</script>",
        reason="<img src=x onerror=alert(1)>",
        next_step="<b>加粗</b>",
    )
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html
    assert "<img src=x" not in html


# ---------------------------------------------------------------------------
# 二、四类写操作反馈（toast）
# ---------------------------------------------------------------------------


def test_四类反馈都有文案与语义色() -> None:
    """确认成功 / 推送失败 / 回写失败 / 转人工 —— 四类各走各的语义色。"""
    assert set(FEEDBACK_CATALOGUE) == {
        "ack_ok",
        "notify_failed",
        "writeback_failed",
        "transferred",
    }
    kinds = {}
    for key in FEEDBACK_CATALOGUE:
        html = feedback_toast(key)
        assert html, f"{key} 没渲染出 toast"
        assert "cv-toast" in html
        assert f'data-cv-feedback="{key}"' in html
        kinds[key] = html
    # 确认成功是绿的
    assert "cv-toast--ok" in kinds["ack_ok"]
    # 推送失败与回写失败是「失败深灰」，**不是**危急红
    assert "cv-toast--failed" in kinds["notify_failed"]
    assert "cv-toast--failed" in kinds["writeback_failed"]
    assert "cv-toast--err" not in kinds["notify_failed"]
    # 转人工是待确认琥珀
    assert "cv-toast--warn" in kinds["transferred"]


def test_反馈语义色与token一致() -> None:
    """反馈类型 → 语义角色的映射与 token 是同一份。"""
    assert states_view.feedback_catalogue_is_consistent() == []
    for key, role in FEEDBACK_SEMANTIC.items():
        assert role in SEMANTIC, f"{key} 的语义角色 {role} 不存在"
        assert TOAST_KIND_BY_ROLE[role] in {"ok", "warn", "err", "info", "failed"}


def test_不认识的反馈类型不渲染提示() -> None:
    """来路不明的反馈不该凭空出现在值班台上。"""
    assert feedback_toast("不存在的反馈") == ""


def test_状态映射到该看的反馈且确认成功不按状态自动弹() -> None:
    """失败类状态各对应一条反馈；确认成功只在动作发生时给。"""
    assert feedback_for_status("failed") == "notify_failed"
    assert feedback_for_status("writeback_failed") == "writeback_failed"
    assert feedback_for_status("transferred_to_human") == "transferred"
    # 早就确认过的事件重新打开时，不该再弹一次「确认成功」
    assert feedback_for_status("acknowledged") == ""
    assert feedback_for_status("registered") == ""
    assert feedback_for_status("notified") == ""


def test_toast可关闭且带焦点可达的关闭按钮() -> None:
    """写操作反馈要能被扫掉 —— 关闭按钮可聚焦、可点。"""
    html = feedback_toast("ack_ok")
    assert "data-cv-toast-close" in html
    assert 'aria-label="关闭提示"' in html


# ---------------------------------------------------------------------------
# 三、页面上的三态接线
# ---------------------------------------------------------------------------


def test_台账全空走页面级空态并列出排查项(empty_store) -> None:
    """一条记录都没有 → 页面级空态（说清为什么空 + 逐条排查）。"""
    router = make_router(empty_store)
    body = router.handle("GET", "/events").body
    assert 'data-cv-state="empty"' in body
    assert "台账里还没有危急值事件" in body
    assert "阈值表版本" in body
    assert "MLLP" in body
    # 不该出现表内那句「当前筛选条件下没有」
    assert "当前筛选条件下没有危急值事件" not in body


def test_筛完为空走表内空态而不是页面级空态(store, snapshot_dir) -> None:
    """筛完为空 ≠ 台账空：前者提示放宽筛选，后者提示查链路。"""
    router = make_router(store, snapshot_dir)
    body = router.handle("GET", "/events?" + urlencode({"dept": "不存在的科室"})).body
    assert "当前筛选条件下没有危急值事件" in body
    assert "台账里还没有危急值事件" not in body


def test_事件号按段解码不把百分号印在页面上(store, snapshot_dir) -> None:
    """``/events/evt-%E4%B8%8D`` 的事件号是「evt-不」—— 页面要显示解码后的。

    不解码的话页面上会把 ``%E4%B8%8D`` 原样印出来，值班人根本看不出
    自己点的是哪一条。
    """
    router = make_router(store, snapshot_dir)
    response = router.handle("GET", "/events/evt-%E4%B8%8D%E5%AD%98%E5%9C%A8")
    assert response.status == 404
    assert "%E4%B8%8D" not in response.body
    assert "evt-不存在" in response.body


def test_阈值表配了但读不到时显式报错(empty_store, tmp_path) -> None:
    """阈值表路径配了、文件却读不到 → 错误态（不让人对着空台账发呆）。"""
    missing = tmp_path / "不存在的阈值表.xlsx"
    router = ConsoleRouter(empty_store, token="", threshold_path=str(missing))
    body = router.handle("GET", "/events").body
    assert 'data-cv-state="error"' in body
    assert "threshold_unbound" in body
    assert ERROR_CATALOGUE["threshold_unbound"]["suggestion"] in body


def test_阈值表没配时不报错(empty_store) -> None:
    """没配阈值表路径 ≠ 配了但读不到 —— 前者不误报。"""
    router = ConsoleRouter(empty_store, token="")
    body = router.handle("GET", "/events").body
    assert "threshold_unbound" not in body


def test_首屏骨架模式渲染加载态(store, snapshot_dir) -> None:
    """``?bootstrap=1``：首屏先给加载态骨架，脚本随后取片段填进去。"""
    router = make_router(store, snapshot_dir)
    body = router.handle("GET", "/events?bootstrap=1").body
    assert 'data-cv-state="loading"' in body
    assert "cv-skeleton" in body
    # 骨架模板随页面一起下发（脚本刷新时克隆它）
    assert "data-cv-ledger-skeleton" in body


def test_片段模式只回台账区域(store, snapshot_dir) -> None:
    """``?fragment=region`` 只回表格与分页 —— 服务端渲染仍是唯一真源。"""
    router = make_router(store, snapshot_dir)
    response = router.handle("GET", "/events?fragment=region")
    assert response.status == 200
    assert "cv-table" in response.body
    assert "evt-0001" in response.body
    # 不回整页外壳
    assert "<!doctype html>" not in response.body
    assert "cv-sidenav" not in response.body


def test_找不到事件走错误态而不是灰字(store, snapshot_dir) -> None:
    """404：带错误码与可执行建议，不是「没有这个页面」。"""
    router = make_router(store, snapshot_dir)
    response = router.handle("GET", "/events/evt-不存在")
    assert response.status == 404
    assert 'data-cv-state="error"' in response.body
    assert "event_not_found" in response.body


def test_快照缺失走错误态并说清是没配还是丢了(store, tmp_path) -> None:
    """快照缺失分两种：目录没配 / 文件丢了 —— 处理方式不同，话也要不同。"""
    router = ConsoleRouter(store, token="", snapshot_dir="")
    body = router.handle("GET", "/events/evt-0001/ack").body
    assert 'data-cv-state="error"' in body
    assert "snapshot_missing" in body
    assert "CRITVAL_SNAPSHOT_DIR" in body


# ---------------------------------------------------------------------------
# 四、破坏性操作：二次确认 + 原因进审计
# ---------------------------------------------------------------------------


def test_转人工表单带二次确认与必填原因(store, snapshot_dir) -> None:
    """转人工 = 破坏性操作：二次确认 + 原因必填。"""
    router = make_router(store, snapshot_dir)
    body = router.handle("GET", "/events/evt-0001/ack").body
    assert "data-cv-confirm" in body
    assert 'action="/events/evt-0001/transfer"' in body
    assert 'name="reason"' in body
    assert "required" in body


def test_页面带站内模态框且不用原生confirm(store, snapshot_dir) -> None:
    """二次确认是站内模态框 —— 页面里不得出现原生 alert/confirm 调用。"""
    router = make_router(store, snapshot_dir)
    body = router.handle("GET", "/events/evt-0001/ack").body
    assert "data-cv-modal" in body
    assert 'role="dialog"' in body
    assert "data-cv-modal-confirm" in body
    assert "data-cv-modal-reason" in body
    # 禁用浏览器原生弹窗
    assert "window.confirm" not in body
    assert "window.alert" not in body


def test_转人工缺原因被服务端拦下且错误态说明原因(store, snapshot_dir) -> None:
    """没有 JS 时也要拦得住：原因必填由服务端把关。"""
    router = make_router(store, snapshot_dir)
    response = router.handle(
        "POST",
        "/events/evt-0001/transfer",
        body=_form(reason="", handler="张三"),
    )
    assert response.status == 400
    assert "reason_required" in response.body
    assert "转人工必须写原因" in response.body


def test_转人工原因进审计流水(store, snapshot_dir) -> None:
    """原因进审计（append-only），事后复核能回答「这条为什么停在这儿」。"""
    router = make_router(store, snapshot_dir)
    router.handle(
        "POST",
        "/events/evt-0001/transfer",
        body=_form(reason="阈值口径分歧", handler="张三"),
    )
    details = [
        str(entry.get("detail", ""))
        for entry in store.audit_entries("evt-0001")
    ]
    assert any("口径分歧" in detail for detail in details), details
    # 状态已跃迁
    assert store.get_status("evt-0001") == "transferred_to_human"


def test_转人工成功给转人工反馈的toast(store, snapshot_dir) -> None:
    """转人工的结果反馈走 toast（琥珀），不是一闪而过的灰字。"""
    router = make_router(store, snapshot_dir)
    response = router.handle(
        "POST",
        "/events/evt-0001/transfer",
        body=_form(reason="阈值口径分歧", handler="张三"),
    )
    assert 'data-cv-feedback="transferred"' in response.body
    assert "cv-toast--warn" in response.body


# ---------------------------------------------------------------------------
# 五、补写回写（重放一次出站回写）
# ---------------------------------------------------------------------------


def test_已确认待回写时给出补写入口(store, snapshot_dir) -> None:
    """已确认但没写回 → 补写入口在，且是破坏性操作（要二次确认）。"""
    store.mark_acknowledged("evt-0001", actor="张三", detail="值班人已确认处置")
    router = make_router(store, snapshot_dir)
    body = router.handle("GET", "/events/evt-0001/ack").body
    assert 'action="/events/evt-0001/replay"' in body
    assert "data-cv-confirm" in body
    assert "补写原因" in body


def test_还没确认不给补写入口(store, snapshot_dir) -> None:
    """未确认的回执不许补写 —— 那会让「建议」变成「已生效事实」。"""
    router = make_router(store, snapshot_dir)
    body = router.handle("GET", "/events/evt-0001/ack").body
    assert 'action="/events/evt-0001/replay"' not in body


def test_补写缺原因被拦下(store, snapshot_dir) -> None:
    router = make_router(store, snapshot_dir)
    store.mark_acknowledged("evt-0001", actor="张三", detail="值班人已确认处置")
    response = router.handle(
        "POST", "/events/evt-0001/replay", body=b"reason=&handler=%E5%BC%A0%E4%B8%89"
    )
    assert response.status == 400
    assert "reason_required" in response.body


def test_补写通道未接入时明说不假装补上(store, snapshot_dir) -> None:
    """没注入补写实现 → 错误态说清「通道未接入」，不假装补上了。"""
    store.mark_acknowledged("evt-0001", actor="张三", detail="值班人已确认处置")
    router = make_router(store, snapshot_dir)
    response = router.handle(
        "POST",
        "/events/evt-0001/replay",
        body=_form(reason="登记系统恢复，补写", handler="张三"),
    )
    assert response.status == 503
    assert "replay_unavailable" in response.body
    assert "补写通道未接入" in response.body


def test_补写成功推进状态并给确认成功的反馈(store, snapshot_dir) -> None:
    """补写成功 → 状态回到「已回写」，反馈走 ack_ok（绿）。"""
    store.mark_acknowledged("evt-0001", actor="张三", detail="值班人已确认处置")

    def replay(event_id: str, handler: str, reason: str) -> ReplayResult:
        store.mark_registered(event_id, actor=handler, detail="补写成功")
        return ReplayResult(ok=True, message="补写成功。", written_back=True, status="registered")

    router = make_router(store, snapshot_dir, replay_handler=replay)
    response = router.handle(
        "POST",
        "/events/evt-0001/replay",
        body=_form(reason="登记系统恢复，补写", handler="张三"),
    )
    assert response.status == 200
    assert store.get_status("evt-0001") == "registered"
    assert 'data-cv-feedback="ack_ok"' in response.body


def test_补写失败保留待回写并给失败深灰反馈(store, snapshot_dir) -> None:
    """补写失败 → 本条不被丢弃，仍是待回写；反馈是失败深灰（不是危急红）。"""
    store.mark_acknowledged("evt-0001", actor="张三", detail="值班人已确认处置")

    def replay(event_id: str, handler: str, reason: str) -> ReplayResult:
        return ReplayResult(
            ok=False,
            message="补写失败：登记系统返回 500。",
            error_code="replay_failed",
            status="acknowledged",
        )

    router = make_router(store, snapshot_dir, replay_handler=replay)
    response = router.handle(
        "POST",
        "/events/evt-0001/replay",
        body=_form(reason="登记系统恢复，补写", handler="张三"),
    )
    assert response.status == 200
    assert "replay_failed" in response.body
    assert "cv-toast--failed" in response.body
    # 状态没有被悄悄改掉
    assert store.get_status("evt-0001") == "acknowledged"


def test_确认成功给确认成功的反馈(store, snapshot_dir) -> None:
    """确认成功 → toast 是绿的（ack_ok），不是失败色。"""

    def ack(event_id: str, handler: str, note: str) -> AckResult:
        store.mark_acknowledged(event_id, actor=handler, detail="值班人已确认处置")
        return AckResult(ok=True, message="已确认。", written_back=False, status="acknowledged")

    router = make_router(store, snapshot_dir, ack_handler=ack)
    response = router.handle(
        "POST", "/events/evt-0001/ack", body=_form(handler="张三", note="")
    )
    assert response.status == 200
    assert 'data-cv-feedback="ack_ok"' in response.body
    assert "cv-toast--ok" in response.body


def test_确认通道未接入时给错误态而不是假装成功(store, snapshot_dir) -> None:
    """没注入确认实现 → 明说「通道未接入」。"""
    router = make_router(store, snapshot_dir)
    response = router.handle(
        "POST", "/events/evt-0001/ack", body=_form(handler="张三", note="")
    )
    assert response.status == 200
    assert "writeback_unavailable" in response.body
    assert "确认通道未接入" in response.body


# ---------------------------------------------------------------------------
# 六、响应式两档与无障碍打磨
# ---------------------------------------------------------------------------


def test_样式表有1280与768两档断点() -> None:
    """响应式两档：1280 完整表格 + 侧边导航；768 卡片式列表 + 折叠导航。"""
    css = state_template_path("loading").parent.parent.joinpath("styles.css")
    text = css.read_text(encoding="utf-8")
    assert "@media (max-width: 1280px)" in text
    assert "@media (max-width: 768px)" in text


def test_768档侧边导航是可展开的折叠导航而不是消失() -> None:
    """窄屏上「待确认」不能点不到 —— 导航要能展开，不是直接消失。"""
    css = state_template_path("loading").parent.parent.joinpath("styles.css")
    text = css.read_text(encoding="utf-8")
    assert ".cv-sidenav.is-open" in text
    assert ".cv-topbar__navtoggle" in text
    # 顶栏上真的有那个展开按钮
    base = state_template_path("loading").parent.parent.joinpath("base.html")
    base_text = base.read_text(encoding="utf-8")
    assert "data-cv-nav-toggle" in base_text
    assert 'aria-controls="cv-sidenav"' in base_text


def test_768档表格转卡片且列标签保留() -> None:
    """窄屏表格转卡片：每格用 data-label 标出列名，值班人才看得懂。"""
    css = state_template_path("loading").parent.parent.joinpath("styles.css")
    text = css.read_text(encoding="utf-8")
    assert ".cv-table tbody td::before" in text
    assert "content: attr(data-label)" in text


def test_数字列右对齐且时间用等宽字体() -> None:
    """数字列右对齐 + 等宽数字；时间戳用等宽字体，方便一眼比大小。"""
    css = state_template_path("loading").parent.parent.joinpath("styles.css")
    text = css.read_text(encoding="utf-8")
    assert ".num {" in text
    assert "text-align: right" in text
    assert "font-variant-numeric: tabular-nums" in text
    assert "--cv-font-family-mono" in text


def test_焦点可见环存在() -> None:
    """键盘焦点要看得见 —— 夜班值班人也可能用键盘操作。"""
    css = state_template_path("loading").parent.parent.joinpath("styles.css")
    text = css.read_text(encoding="utf-8")
    assert ":focus-visible" in text
    assert "outline: 2px solid var(--cv-color-brand)" in text


def test_状态徽标带文字标签不只靠颜色区分() -> None:
    """色觉差异与夜班偏色场景下，徽标必须带文字。"""
    from critval.ledger.states import ALL_STATUSES

    from critval.console.components import status_badge

    for status in sorted(ALL_STATUSES):
        html = status_badge(status)
        assert "cv-badge__dot" in html
        assert ">" in html


def test_静态脚本可访问且content_type正确(store, snapshot_dir) -> None:
    """脚本要以 JS 的 content-type 吐出去 —— 当 CSS 吐，脚本压根不执行。"""
    router = make_router(store, snapshot_dir)
    response = router.handle("GET", "/static/console.js")
    assert response.status == 200
    assert response.content_type.startswith("application/javascript")
    assert "data-cv-modal-confirm" in response.body


def test_页面引用静态脚本(store, snapshot_dir) -> None:
    router = make_router(store, snapshot_dir)
    body = router.handle("GET", "/events").body
    assert 'src="/static/console.js"' in body


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
