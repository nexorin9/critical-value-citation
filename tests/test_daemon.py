"""守护进程主循环与运维 CLI 骨架的验收（任务 id 13）。

验收的三件事（task.json 的验收条目）：

1. 守护进程常驻时投递同一帧两次 → **台账只出现一条事件**；
2. 重启守护进程后**按状态续跑，不重复推送**；
3. 值班台页面（台账列表 / 详情 / 确认页）能打开，确认动作走真实的
   状态跃迁与幂等回写。

外加几条守护进程自己的边界：有界队列满了要拒收留痕、心跳行要带关键
计数、材料投递指纹变了要重装、MLLP 真 socket 收帧要能处理。
"""

from __future__ import annotations

import socket
import sys
import time
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "tests"))

from critval.config import Config  # noqa: E402
from critval.console.pages import AckResult, ConsoleRouter  # noqa: E402
from critval.daemon import Daemon, DaemonOptions  # noqa: E402
from critval.ledger.store import LedgerStore  # noqa: E402

THRESHOLD = PROJECT_ROOT / "samples" / "threshold" / "示例危急值阈值表.xlsx"
POLICY = PROJECT_ROOT / "samples" / "policy" / "示例危急值报告制度.docx"
FRAME = PROJECT_ROOT / "samples" / "hl7" / "示例ORU_R01_多项目.hl7"
FRAME_NORMAL = PROJECT_ROOT / "samples" / "hl7" / "示例ORU_R01_正常.hl7"

#: 样例多项目帧里有两条 OBX（钾 / 钠），所以一帧落**两条**事件。
EXPECTED_EVENTS = 2


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch, tmp_path):
    """每个用例从「环境变量干净」开始 —— 不许误连真院内系统。"""
    for key in (
        "CRITVAL_WECOM_WEBHOOK_URL",
        "CRITVAL_BASE_URL",
        "CRITVAL_TOKEN",
        "NURSE_ROSTER_BASE_URL",
        "ROSTER_TOKEN",
        "LIS_CRITICAL_TOKEN",
        "CONSOLE_TOKEN",
        "LIS_MLLP_BIND",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("CRITVAL_LOCAL_NOTIFICATION_PATH", str(tmp_path / "local.jsonl"))
    Config.reload()
    yield
    Config.reload()


def _options(tmp_path: Path, **overrides) -> DaemonOptions:
    options = DaemonOptions(
        db_path=str(tmp_path / "critval.db"),
        threshold_path=str(THRESHOLD),
        policy_path=str(POLICY),
        frame_dir=str(tmp_path / "frames"),
        material_dir=str(tmp_path / "materials"),
        snapshot_dir=str(tmp_path / "snapshots"),
        console_enabled=False,
        workers=1,
        heartbeat_seconds=0.2,
        timer_interval_seconds=0.2,
        poll_interval=0.05,
        listen_enabled=False,
    )
    for key, value in overrides.items():
        setattr(options, key, value)
    return options


def _wait_for(predicate, timeout: float = 8.0, interval: float = 0.05) -> bool:
    """等一个条件成立（守护进程是异步的，测试要等它跑完）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def _drop_frame(frame_dir: Path, source: Path = FRAME, name: str = "frame.hl7") -> Path:
    """把一帧投到目录投递口。"""
    directory = Path(frame_dir)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / name
    target.write_bytes(source.read_bytes())
    return target


def _ledger(db_path: str) -> LedgerStore:
    return LedgerStore(db_path)


def _free_port() -> int:
    """取一个当前空闲的端口（MLLP 监听要绑真实端口，不能用 0）。"""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _seed_roster(monkeypatch, dept_code: str = "心脏内科") -> Any:
    """把排班系统替身接上 —— 通知对象（当班人）才有值。"""
    from fixtures.roster_double import RosterDouble

    double = RosterDouble(token="roster-token-0001")
    double.start()
    double.seed(
        dept_code,
        [
            {
                "姓名": "李静",
                "工号": "10231",
                "岗位": "检验科值班技师",
                # 样例帧的观察时刻落在夜里（23:49），所以这里排的是覆盖整天的夜班
                "shift_start": "00:00",
                "shift_end": "23:59",
            }
        ],
    )
    monkeypatch.setenv("NURSE_ROSTER_BASE_URL", double.base_url)
    monkeypatch.setenv("ROSTER_TOKEN", "roster-token-0001")
    Config.reload()
    return double


# ---------------------------------------------------------------------------
# 主路径：投递 → 判定 → 出卡 → 推送
# ---------------------------------------------------------------------------


def test_投递一帧后落台账并出带出处的处置卡(tmp_path):
    options = _options(tmp_path)
    daemon = Daemon(options)
    daemon.setup()
    daemon.start()
    try:
        _drop_frame(options.frame_dir)
        assert _wait_for(lambda: daemon.stats.events_processed >= EXPECTED_EVENTS), "守护进程没处理这一帧"

        store = _ledger(options.db_path)
        try:
            rows = store.list_events(limit=0)
            assert len(rows) == EXPECTED_EVENTS
            row = next(item for item in rows if item["item_code"] == "2823-3")
            assert row["status"] in {"notified", "pending_ack"}

            # 依据出处：阈值行与制度条款都要带文件名 + 行号
            citations = store.citations_of(row["event_id"])
            assert citations, "出卡了却没有落库的引用"
            for citation in citations:
                assert citation["source_sha256"]
                assert citation["line_no"]

            # 推送记录在案（渠道记清楚推到了哪里）
            notifications = store.notifications_of(row["event_id"])
            assert notifications
            assert notifications[0]["channel"] in {"wecom", "local"}
        finally:
            store.close()
    finally:
        daemon.stop()


def test_处置卡快照落盘且值班台复核读到的是同一份(tmp_path):
    options = _options(tmp_path)
    daemon = Daemon(options)
    daemon.setup()
    daemon.start()
    try:
        _drop_frame(options.frame_dir)
        assert _wait_for(lambda: daemon.stats.events_processed >= EXPECTED_EVENTS)
        store = _ledger(options.db_path)
        try:
            event_id = store.list_events(limit=0)[0]["event_id"]
        finally:
            store.close()

        from critval.card.snapshot import CardSnapshotStore

        snapshot = CardSnapshotStore(options.snapshot_dir).find_by_event(event_id)
        assert snapshot is not None, "处置卡快照没落盘"
        assert "处置依据" in snapshot.rendering("text")
        assert "第 " in snapshot.rendering("text")  # 出处带可读行号
    finally:
        daemon.stop()


# ---------------------------------------------------------------------------
# 幂等：同一帧投两次只落一条
# ---------------------------------------------------------------------------


def test_同一帧投递两次台账只出现一条事件(tmp_path):
    options = _options(tmp_path)
    daemon = Daemon(options)
    daemon.setup()
    daemon.start()
    try:
        _drop_frame(options.frame_dir, name="first.hl7")
        assert _wait_for(lambda: daemon.stats.events_processed >= EXPECTED_EVENTS)

        # 同一帧再投一次（换个文件名，内容一样）
        _drop_frame(options.frame_dir, name="second.hl7")
        assert _wait_for(lambda: daemon.stats.duplicates >= 1)

        store = _ledger(options.db_path)
        try:
            assert store.count_events() == EXPECTED_EVENTS
            for row in store.list_events(limit=0):
                # 幂等挡住的是**重复推送**：通知记录仍然只有一条
                assert len(store.notifications_of(row["event_id"])) == 1
        finally:
            store.close()
    finally:
        daemon.stop()


# ---------------------------------------------------------------------------
# 重启续跑：按台账状态接着走，不重复推送
# ---------------------------------------------------------------------------


def test_重启守护进程后按状态续跑不重复推送(tmp_path):
    options = _options(tmp_path)
    daemon = Daemon(options)
    daemon.setup()
    daemon.start()
    try:
        _drop_frame(options.frame_dir)
        assert _wait_for(lambda: daemon.stats.events_processed >= EXPECTED_EVENTS)
        event_id = daemon.store.list_events(limit=0)[0]["event_id"]
    finally:
        daemon.stop()

    # 重启：同一个库、同一份材料 —— 续跑按**已落库的事实**判断该做什么
    restarted = Daemon(options)
    restarted.setup()
    restarted.start()
    try:
        report = restarted.tick()
        assert report["resumed"] == 0, "已推送待回执的事件不该被续跑重推"
        store = _ledger(options.db_path)
        try:
            assert store.count_events() == EXPECTED_EVENTS
            assert len(store.notifications_of(event_id)) == 1, "续跑把处置卡又推了一遍"
        finally:
            store.close()
    finally:
        restarted.stop()


def test_续跑只补状态不覆盖既有事实(tmp_path):
    """崩溃落在「推送已发出、状态未更新」时 —— 续跑只补状态，不再推。"""
    options = _options(tmp_path)
    daemon = Daemon(options)
    daemon.setup()
    daemon.start()
    try:
        _drop_frame(options.frame_dir)
        assert _wait_for(lambda: daemon.stats.events_processed >= EXPECTED_EVENTS)
        event_id = daemon.store.list_events(limit=0)[0]["event_id"]
        # 模拟崩溃：推送已发出、状态还没更新 —— 状态机不允许回退，
        # 所以直接改库（真实崩溃就是这个状态：状态没来得及更新）。
        daemon.store._conn.execute(
            "UPDATE events SET status = 'card_built' WHERE event_id = ?", (event_id,)
        )
        daemon.store._conn.commit()
    finally:
        daemon.stop()

    restarted = Daemon(options)
    restarted.setup()
    restarted.start()
    try:
        restarted.tick()
        store = _ledger(options.db_path)
        try:
            assert store.get_status(event_id) == "notified"
            assert len(store.notifications_of(event_id)) == 1, "补状态不该再推一遍"
        finally:
            store.close()
    finally:
        restarted.stop()


# ---------------------------------------------------------------------------
# 有界队列
# ---------------------------------------------------------------------------


def test_队列满了拒收并留痕而不是无限堆积(tmp_path):
    options = _options(tmp_path, queue_size=1)
    daemon = Daemon(options)
    daemon.setup()
    daemon.start()
    try:
        # 手工塞满队列
        daemon.queue.put_nowait(object())
        accepted = daemon.submit(object(), origin="test")
        assert accepted is False
        assert daemon.stats.dropped == 1
        audits = daemon.store.audit_entries()
        actions = [row["action"] for row in audits]
        assert "daemon_queue_full" in actions
    finally:
        daemon.stop()


# ---------------------------------------------------------------------------
# 心跳
# ---------------------------------------------------------------------------


def test_心跳行带队列深度与处理计数(tmp_path):
    options = _options(tmp_path)
    daemon = Daemon(options)
    daemon.setup()
    daemon.start()
    try:
        line = daemon.heartbeat()
        assert "队列深度" in line
        assert "已处理" in line
        assert "最近错误" in line
    finally:
        daemon.stop()


def test_健康摘要给出队列与状态分布(tmp_path):
    options = _options(tmp_path)
    daemon = Daemon(options)
    daemon.setup()
    daemon.start()
    try:
        _drop_frame(options.frame_dir)
        assert _wait_for(lambda: daemon.stats.events_processed >= EXPECTED_EVENTS)
        health = daemon.health()
        assert health["status"] == "ok"
        assert health["queue_capacity"] == options.queue_size
        assert health["status_counts"]
    finally:
        daemon.stop()


# ---------------------------------------------------------------------------
# 值班台页面
# ---------------------------------------------------------------------------


def test_值班台台账页列出事件且详情带出处(tmp_path):
    options = _options(tmp_path)
    daemon = Daemon(options)
    daemon.setup()
    daemon.start()
    try:
        _drop_frame(options.frame_dir)
        assert _wait_for(lambda: daemon.stats.events_processed >= EXPECTED_EVENTS)
        event_id = daemon.store.list_events(limit=0)[0]["event_id"]

        router = ConsoleRouter(
            daemon.store,
            token="",
            ack_handler=daemon.ack_event,
            snapshot_dir=options.snapshot_dir,
            base_url="http://127.0.0.1:8760",
        )
        listing = router.handle("GET", "/events")
        assert listing.status == 200
        assert event_id in listing.body
        assert "事件台账" in listing.body

        detail = router.handle("GET", f"/events/{event_id}")
        assert detail.status == 200
        assert "依据出处" in detail.body
        assert "处置卡" in detail.body
        # 页面里不出现患者标识
        assert "SYN-PAT-000001" not in detail.body
    finally:
        daemon.stop()


def test_确认页缺确认人时拒绝并说明(tmp_path):
    options = _options(tmp_path)
    daemon = Daemon(options)
    daemon.setup()
    daemon.start()
    try:
        _drop_frame(options.frame_dir)
        assert _wait_for(lambda: daemon.stats.events_processed >= EXPECTED_EVENTS)
        event_id = daemon.store.list_events(limit=0)[0]["event_id"]

        router = ConsoleRouter(
            daemon.store,
            token="",
            ack_handler=daemon.ack_event,
            snapshot_dir=options.snapshot_dir,
        )
        page = router.handle("GET", f"/events/{event_id}/ack")
        assert page.status == 200
        assert "确认处置并回写登记系统" in page.body

        response = router.handle(
            "POST",
            f"/events/{event_id}/ack",
            body=b"handler=&note=",
        )
        assert response.status == 400
        assert "必须写确认人" in response.body
    finally:
        daemon.stop()


def test_确认动作走真实状态跃迁并幂等回写登记系统(tmp_path, monkeypatch):
    """确认 → 回写登记系统（真 HTTP 出站打到本机协议替身）→ 归档。"""
    from fixtures.registry_double import RegistryDouble

    roster_double = _seed_roster(monkeypatch)
    double = RegistryDouble(token="registry-token-0001")
    double.start()
    monkeypatch.setenv("CRITVAL_BASE_URL", double.base_url)
    monkeypatch.setenv("CRITVAL_TOKEN", "registry-token-0001")
    Config.reload()

    options = _options(tmp_path)
    daemon = Daemon(options)
    daemon.setup()
    daemon.start()
    try:
        _drop_frame(options.frame_dir)
        assert _wait_for(lambda: daemon.stats.events_processed >= EXPECTED_EVENTS)
        event_id = daemon.store.list_events(limit=0)[0]["event_id"]

        result = daemon.ack_event(event_id, "检验科值班技师 张三", "已电话通知护士长")
        assert isinstance(result, AckResult)
        assert result.ok is True, result.message
        assert result.written_back is True
        assert daemon.store.get_status(event_id) == "archived"

        posts = [call for call in double.calls if call[0] == "POST"]
        assert posts, "确认后没有回写登记系统"

        # 幂等：再点一次确认，不会产生第二条回执
        again = daemon.ack_event(event_id, "检验科值班技师 张三", "")
        assert again.ok is True
        assert again.written_back is True
        posts_again = [call for call in double.calls if call[0] == "POST"]
        assert len(posts_again) == len(posts), "重复确认又发了一次回写"
    finally:
        daemon.stop()
        double.stop()
        roster_double.stop()


def test_配了值班台凭据后没带凭据打不开页面(tmp_path):
    options = _options(tmp_path)
    daemon = Daemon(options)
    daemon.setup()
    daemon.start()
    try:
        router = ConsoleRouter(daemon.store, token="console-token-0001")
        denied = router.handle("GET", "/events")
        assert denied.status == 401

        allowed = router.handle(
            "GET", "/events?token=console-token-0001", headers={}
        )
        assert allowed.status == 200
    finally:
        daemon.stop()


# ---------------------------------------------------------------------------
# MLLP 真 socket
# ---------------------------------------------------------------------------


def test_MLLP监听收到一帧后能处理(tmp_path):
    options = _options(
        tmp_path, listen_enabled=True, mllp_bind=f"127.0.0.1:{_free_port()}"
    )
    daemon = Daemon(options)
    daemon.setup()
    daemon.start()
    try:
        assert daemon._listener is not None
        host, port = daemon._listener.host, daemon._listener.port
        payload = b"\x0b" + FRAME.read_bytes().strip() + b"\x1c\r"
        with socket.create_connection((host, port), timeout=5) as client:
            client.sendall(payload)
        assert _wait_for(lambda: daemon.stats.events_processed >= EXPECTED_EVENTS)
        assert daemon.store.count_events() == EXPECTED_EVENTS
    finally:
        daemon.stop()


# ---------------------------------------------------------------------------
# 材料投递
# ---------------------------------------------------------------------------


def test_材料投递目录指纹变了会重装(tmp_path):
    options = _options(tmp_path)
    daemon = Daemon(options)
    daemon.setup()
    daemon.start()
    try:
        material_dir = Path(options.material_dir)
        material_dir.mkdir(parents=True, exist_ok=True)
        target = material_dir / "院内危急值阈值表.xlsx"
        target.write_bytes(THRESHOLD.read_bytes())
        assert daemon.scan_materials() == 1
        assert daemon.stats.materials_reloaded == 1
        # 同一份材料再扫一次不重装
        assert daemon.scan_materials() == 0
        assert daemon.stats.materials_reloaded == 1
    finally:
        daemon.stop()


# ---------------------------------------------------------------------------
# 停机
# ---------------------------------------------------------------------------


def test_停机后不留半写状态(tmp_path):
    options = _options(tmp_path)
    daemon = Daemon(options)
    daemon.setup()
    daemon.start()
    daemon.start()
    try:
        _drop_frame(options.frame_dir)
        assert _wait_for(lambda: daemon.stats.events_processed >= EXPECTED_EVENTS)
    finally:
        daemon.stop()
    # 停机后再读台账：数据完整、事件还在
    store = _ledger(options.db_path)
    try:
        assert store.count_events() == EXPECTED_EVENTS
        assert store.integrity_check()[0] is True
    finally:
        store.close()
