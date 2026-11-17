"""守护进程运行保障的验收（任务 id 46）。

值班机上的进程要连跑一整周不出静默故障。这一任务验收五件事：

1. **心跳写进文件**（不只是打一行日志）：版本 / 队列深度 / 处理计数 /
   最近错误时刻，且**原子写** —— 读的人永远读到完整一份；
2. **``GET /healthz``** 返回状态 + 关键计数 + **依赖可达性**（排班系统 /
   登记系统 / 企微通道通不通）；
3. **优雅停机**：停机即拒收新事件，在途事件在宽限期内处理完，
   **已落库的状态不回退**；
4. **单实例锁**：同一个数据目录起第二个守护进程 → **明确报错退出**，
   不静默并存（两个实例会各推一遍处置卡）；
5. **日志轮转**：按大小滚动，旧日志留档，凭据照样脱敏。
"""

from __future__ import annotations

import json
import logging
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Optional

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "tests"))

from critval.config import Config  # noqa: E402
from critval.daemon import Daemon, DaemonOptions  # noqa: E402
from critval.ledger.store import LedgerStore  # noqa: E402
from critval.runtime.lock import SingleInstanceError, SingleInstanceLock  # noqa: E402
from critval.runtime.logging_setup import configure_runtime_logging  # noqa: E402

THRESHOLD = PROJECT_ROOT / "samples" / "threshold" / "示例危急值阈值表.xlsx"
POLICY = PROJECT_ROOT / "samples" / "policy" / "示例危急值报告制度.docx"
FRAME = PROJECT_ROOT / "samples" / "hl7" / "示例ORU_R01_多项目.hl7"

#: 样例多项目帧里有两条 OBX（钾 / 钠），所以一帧落**两条**事件。
EXPECTED_EVENTS = 2

#: 停机排空用例里两帧合计入队的事件数（多项目帧 2 条 + 正常帧 1 条）。
QUEUED_EVENTS = 3


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
        db_path=str(tmp_path / "data" / "critval.db"),
        threshold_path=str(THRESHOLD),
        policy_path=str(POLICY),
        frame_dir=str(tmp_path / "frames"),
        material_dir=str(tmp_path / "materials"),
        snapshot_dir=str(tmp_path / "snapshots"),
        heartbeat_path=str(tmp_path / "data" / "heartbeat.json"),
        lock_path=str(tmp_path / "data" / "critval.lock"),
        log_dir=str(tmp_path / "logs"),
        console_enabled=False,
        workers=1,
        heartbeat_seconds=0.2,
        timer_interval_seconds=0.2,
        poll_interval=0.05,
        listen_enabled=False,
        shutdown_grace_seconds=10.0,
    )
    for key, value in overrides.items():
        setattr(options, key, value)
    return options


def _wait_for(predicate, timeout: float = 10.0, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _drop_frame(frame_dir: str, source: Path = FRAME, name: str = "frame.hl7") -> Path:
    directory = Path(frame_dir)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / name
    target.write_bytes(source.read_bytes())
    return target


def _read_heartbeat(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _daemon_env(extra: Optional[dict[str, str]] = None) -> dict[str, str]:
    """子进程环境：干净 + 项目根在 PYTHONPATH 上。"""
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/tmp"),
        "PYTHONPATH": str(PROJECT_ROOT),
        "PYTHONIOENCODING": "utf-8",
    }
    for key, value in (extra or {}).items():
        env[key] = value
    return env


# ---------------------------------------------------------------------------
# ① 心跳
# ---------------------------------------------------------------------------


def test_心跳写进文件且带版本队列深度与处理计数(tmp_path):
    options = _options(tmp_path)
    daemon = Daemon(options)
    daemon.setup()
    daemon.start()
    try:
        _drop_frame(options.frame_dir)
        assert _wait_for(lambda: daemon.stats.events_processed >= EXPECTED_EVENTS)

        heartbeat = _read_heartbeat(options.heartbeat_path)
        assert heartbeat["version"], "心跳里没有版本号"
        assert heartbeat["queue_capacity"] == options.queue_size
        assert heartbeat["pid"] == os.getpid()
        assert heartbeat["updated_at"]
        assert heartbeat["status"] == "ok"
        # 周期心跳把处理计数带上（文件里是最近一期快照，稍等一拍）
        assert _wait_for(
            lambda: _read_heartbeat(options.heartbeat_path)["counters"][
                "events_processed"
            ]
            >= EXPECTED_EVENTS
        ), "心跳里的处理计数没跟上"
    finally:
        daemon.stop()


def test_心跳周期到了会自动更新文件(tmp_path):
    options = _options(tmp_path, heartbeat_seconds=0.2)
    daemon = Daemon(options)
    daemon.setup()
    daemon.start()
    try:
        first = _read_heartbeat(options.heartbeat_path)["updated_at"]
        assert _wait_for(
            lambda: _read_heartbeat(options.heartbeat_path)["updated_at"] != first
        ), "心跳文件没有按周期更新"
    finally:
        daemon.stop()


def test_心跳文件原子写不留半截(tmp_path):
    """读的人永远读到完整一份 JSON —— 不读到写了一半的内容。"""
    options = _options(tmp_path, heartbeat_seconds=0.05)
    daemon = Daemon(options)
    daemon.setup()
    daemon.start()
    try:
        broken = 0
        for _ in range(60):
            try:
                json.loads(Path(options.heartbeat_path).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                broken += 1
            time.sleep(0.01)
        assert broken == 0, "读心跳读到了写了一半的内容"
        # 临时文件不留残骸
        leftovers = [
            item.name
            for item in Path(options.heartbeat_path).parent.iterdir()
            if item.name.endswith(".tmp")
        ]
        assert not leftovers, f"心跳临时文件没清掉：{leftovers}"
    finally:
        daemon.stop()


def test_最近错误时刻写进心跳(tmp_path):
    options = _options(tmp_path)
    daemon = Daemon(options)
    daemon.setup()
    daemon.start()
    try:
        daemon.stats.note_error(RuntimeError("排班系统读不到当班人"))
        daemon.heartbeat()
        heartbeat = _read_heartbeat(options.heartbeat_path)
        assert heartbeat["last_error_at"], "心跳里没有最近错误时刻"
        assert "排班系统读不到当班人" in heartbeat["last_error"]
    finally:
        daemon.stop()


# ---------------------------------------------------------------------------
# ② 健康检查端点
# ---------------------------------------------------------------------------


def _start_console_daemon(tmp_path: Path, port: int) -> Daemon:
    options = _options(
        tmp_path, console_enabled=True, console_host="127.0.0.1", console_port=port
    )
    daemon = Daemon(options)
    daemon.setup()
    daemon.start()
    return daemon


def test_健康端点返回状态与关键计数(tmp_path):
    port = _free_port()
    daemon = _start_console_daemon(tmp_path, port)
    try:
        _drop_frame(daemon.options.frame_dir)
        assert _wait_for(lambda: daemon.stats.events_processed >= EXPECTED_EVENTS)

        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=5) as resp:
            assert resp.status == 200
            payload = json.loads(resp.read().decode("utf-8"))

        assert payload["status"] in {"ok", "degraded", "stopping"}
        assert payload["queue"]["capacity"] == daemon.options.queue_size
        assert payload["counters"]["events_processed"] >= EXPECTED_EVENTS
        assert payload["status_counts"], "健康摘要里没有台账状态分布"
        assert "dependencies" in payload
        assert payload["heartbeat"]["path"] == daemon.options.heartbeat_path
    finally:
        daemon.stop()


def test_健康端点报出依赖可达性(tmp_path, monkeypatch):
    """配了但连不上 → 明确报不可达；没配 → 报没配（不算故障）。"""
    port = _free_port()
    monkeypatch.setenv("NURSE_ROSTER_BASE_URL", f"http://127.0.0.1:{_free_port()}")
    monkeypatch.setenv("CRITVAL_BASE_URL", f"http://127.0.0.1:{_free_port()}")
    Config.reload()
    daemon = _start_console_daemon(tmp_path, port)
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=5) as resp:
            payload = json.loads(resp.read().decode("utf-8"))

        roster = payload["dependencies"]["roster"]
        assert roster["configured"] is True
        assert roster["reachable"] is False, "连不上的依赖报成可达了"
        assert "连不上" in roster["detail"]
        assert payload["dependencies"]["registry"]["configured"] is True
        assert payload["dependencies"]["wecom"]["configured"] is False
        assert payload["status"] == "degraded", "依赖不通却报健康"
    finally:
        daemon.stop()


def test_健康摘要不带处置卡正文(tmp_path):
    """健康检查只给计数与状态，不泄漏处置卡内容与患者标识。"""
    port = _free_port()
    daemon = _start_console_daemon(tmp_path, port)
    try:
        _drop_frame(daemon.options.frame_dir)
        assert _wait_for(lambda: daemon.stats.events_processed >= EXPECTED_EVENTS)
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=5) as resp:
            body = resp.read().decode("utf-8")
        assert "处置依据" not in body
        assert "SYN-PAT-000001" not in body
    finally:
        daemon.stop()


def test_健康端点在值班台独立运行时也给摘要(tmp_path):
    """没装配守护进程时（独立跑值班台服务），健康端点仍返回摘要而不是 404。"""
    from critval.console.app import build_service, make_server

    service = build_service(db_path=str(tmp_path / "console.db"), threshold_path=str(THRESHOLD))
    server = make_server("127.0.0.1", _free_port(), service=service)
    port = server.server_address[1]
    thread = __import__("threading").Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=5) as resp:
            assert resp.status == 200
            payload = json.loads(resp.read().decode("utf-8"))
        assert payload["mode"] == "console-only"
    finally:
        server.shutdown()
        server.server_close()


# ---------------------------------------------------------------------------
# ③ 优雅停机
# ---------------------------------------------------------------------------


def test_停机后不再接收新事件(tmp_path):
    options = _options(tmp_path)
    daemon = Daemon(options)
    daemon.setup()
    daemon.start()
    try:
        daemon.request_stop("测试触发停机")
        accepted = daemon.submit(object(), origin="test")
        assert accepted is False, "停机后还在收新事件"
        actions = [row["action"] for row in daemon.store.audit_entries()]
        assert "daemon_stopping" in actions, "拒收没有留痕"
    finally:
        daemon.stop()


def test_停机把队列里剩下的事件处理完(tmp_path):
    """危急值已经在队列里了，丢掉就等于漏处置 —— 宽限期内必须处理完。

    用**慢处理**把事件压在队列里（工作线程忙不过来），再触发停机 ——
    这样「停机时把队列排空」这条路径才真的被走到。
    """
    options = _options(tmp_path)
    daemon = Daemon(options)
    daemon.setup()
    original = daemon.process_event

    def slow_process(event: Any) -> dict[str, Any]:
        time.sleep(0.3)
        return original(event)

    daemon.process_event = slow_process  # type: ignore[method-assign]
    daemon.start()
    try:
        # 两帧内容不同（幂等键不同），各落一条事件
        _drop_frame(options.frame_dir, source=FRAME, name="first.hl7")
        _drop_frame(
            options.frame_dir,
            source=PROJECT_ROOT / "samples" / "hl7" / "示例ORU_R01_正常.hl7",
            name="second.hl7",
        )
        # 两帧都进了队列（多项目帧 2 条 + 正常帧 1 条），但工作线程还在慢慢处理
        assert _wait_for(
            lambda: daemon.stats.events_submitted >= QUEUED_EVENTS
        ), "两帧没都进队列"
        daemon.stop()
        assert daemon.stats.events_processed >= QUEUED_EVENTS, (
            f"停机时把队列里的事件丢了：处理了 {daemon.stats.events_processed} 条"
        )
        assert daemon.stats.abandoned == 0, "宽限期内不该有被放弃的事件"
    finally:
        daemon.stop()


def test_停机后不留半写状态且状态不回退(tmp_path):
    options = _options(tmp_path)
    daemon = Daemon(options)
    daemon.setup()
    daemon.start()
    try:
        _drop_frame(options.frame_dir)
        assert _wait_for(lambda: daemon.stats.events_processed >= EXPECTED_EVENTS)
    finally:
        daemon.stop()

    store = LedgerStore(options.db_path)
    try:
        assert store.integrity_check()[0] is True, "停机后台账写坏了"
        rows = store.list_events(limit=0)
        assert len(rows) == EXPECTED_EVENTS
        for row in rows:
            # 已落库的状态不回退：不能停在「刚收到」这种中间态
            assert row["status"] not in {"received", "judged", "card_built"}, row["status"]
            assert row["status"] in {
                "notified",
                "pending_ack",
                "not_critical",
                "failed",
                "transferred_to_human",
                "acknowledged",
                "registered",
                "archived",
            }, row["status"]
    finally:
        store.close()


def test_停机时写最后一份心跳标为stopped(tmp_path):
    options = _options(tmp_path)
    daemon = Daemon(options)
    daemon.setup()
    daemon.start()
    try:
        daemon.request_stop("运维通知要停机")
    finally:
        daemon.stop()
    heartbeat = _read_heartbeat(options.heartbeat_path)
    assert heartbeat["status"] == "stopped"
    assert "运维通知要停机" in heartbeat["note"]


def test_收到SIGTERM走优雅停机并正常退出(tmp_path):
    """真起一个进程发 SIGTERM：退出码 0，心跳标 stopped，库里没有半写状态。"""
    options = _options(tmp_path)
    cmd = [
        sys.executable,
        "-m",
        "critval.daemon",
        "--db",
        options.db_path,
        "--threshold",
        options.threshold_path,
        "--policy",
        options.policy_path,
        "--frame-dir",
        options.frame_dir,
        "--material-dir",
        options.material_dir,
        "--snapshot-dir",
        options.snapshot_dir,
        "--heartbeat-path",
        options.heartbeat_path,
        "--lock-path",
        options.lock_path,
        "--log-dir",
        options.log_dir,
        "--no-console",
        "--run-seconds",
        "30",
    ]
    proc = subprocess.Popen(
        cmd, cwd=str(PROJECT_ROOT), env=_daemon_env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    try:
        assert _wait_for(
            lambda: Path(options.heartbeat_path).exists(), timeout=15
        ), "守护进程没起来（心跳文件没出现）"
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=20)
        assert proc.returncode == 0, f"SIGTERM 后退出码不是 0：{proc.returncode}"
    finally:
        if proc.poll() is None:  # pragma: no cover —— 兜底强杀
            proc.kill()
            proc.wait(timeout=10)

    heartbeat = _read_heartbeat(options.heartbeat_path)
    assert heartbeat["status"] == "stopped"


# ---------------------------------------------------------------------------
# ④ 单实例锁
# ---------------------------------------------------------------------------


def test_同目录起第二个守护进程被明确拒绝(tmp_path):
    options = _options(tmp_path)
    first = Daemon(options)
    first.setup()
    try:
        second = Daemon(options)
        with pytest.raises(SingleInstanceError) as excinfo:
            second.setup()
        message = str(excinfo.value)
        assert "另一个守护进程已经占着这个数据目录" in message
        assert str(os.getpid()) in message, "报错里没说是哪个进程占着"
    finally:
        first.stop()


def test_第二个实例命令行启动明确报错退出(tmp_path):
    """真起第二个进程：退出码非零 + 报错说清该停谁。"""
    options = _options(tmp_path)
    first = Daemon(options)
    first.setup()
    try:
        cmd = [
            sys.executable,
            "-m",
            "critval.daemon",
            "--db",
            options.db_path,
            "--threshold",
            options.threshold_path,
            "--no-console",
            "--heartbeat-path",
            options.heartbeat_path,
            "--lock-path",
            options.lock_path,
            "--log-dir",
            options.log_dir,
            "--run-seconds",
            "5",
        ]
        result = subprocess.run(
            cmd,
            cwd=str(PROJECT_ROOT),
            env=_daemon_env(),
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 3, (
            f"第二个实例没有明确报错退出（退出码 {result.returncode}）：{result.stderr[-500:]}"
        )
        assert "另一个守护进程已经占着这个数据目录" in result.stderr
        assert str(os.getpid()) in result.stderr, "报错里没说是哪个进程占着"
    finally:
        first.stop()


def test_停机后锁释放可以再起一个(tmp_path):
    options = _options(tmp_path)
    first = Daemon(options)
    first.setup()
    first.stop()

    second = Daemon(options)
    second.setup()
    try:
        assert second._lock is not None and second._lock.held
    finally:
        second.stop()


def test_锁文件里留着持有者信息(tmp_path):
    lock_path = tmp_path / "critval.lock"
    lock = SingleInstanceLock(lock_path)
    lock.acquire()
    try:
        holder = json.loads(lock_path.read_text(encoding="utf-8"))
        assert holder["pid"] == os.getpid()
        assert holder["host"]
        assert holder["started_at"]
    finally:
        lock.release()


# ---------------------------------------------------------------------------
# ⑤ 日志轮转
# ---------------------------------------------------------------------------


def test_日志按大小滚动并保留历史份数(tmp_path):
    log_dir = tmp_path / "logs"
    handlers = configure_runtime_logging(
        "", log_dir=str(log_dir), max_bytes=2048, backup_count=2, console=False
    )
    try:
        rotating = [h for h in handlers if hasattr(h, "doRollover")]
        assert rotating, "日志没有用滚动处理器"
        assert rotating[0].maxBytes == 2048
        assert rotating[0].backupCount == 2

        logger = logging.getLogger("critval.test.rolling")
        for index in range(200):
            logger.info("第 %d 行：%s", index, "危急值处置依据检索系统" * 4)
        for handler in handlers:
            handler.flush()

        files = sorted(item.name for item in log_dir.iterdir())
        assert any(name.endswith(".1") for name in files), f"日志没滚动：{files}"
        assert len([name for name in files if name.startswith("critval-daemon.log")]) <= 3
    finally:
        for handler in handlers:
            handler.close()
        logging.basicConfig(force=True)


def test_滚动日志里凭据照样脱敏(tmp_path, monkeypatch):
    monkeypatch.setenv("CRITVAL_TOKEN", "super-secret-token-value-1234")
    Config.reload()
    log_dir = tmp_path / "logs"
    handlers = configure_runtime_logging(
        "", log_dir=str(log_dir), max_bytes=65536, backup_count=1, console=False
    )
    try:
        logging.getLogger("critval.test.secret").error(
            "回写用凭据 %s 失败了", "super-secret-token-value-1234"
        )
        for handler in handlers:
            handler.flush()
        content = (log_dir / "critval-daemon.log").read_text(encoding="utf-8")
        assert "super-secret-token-value-1234" not in content, "完整凭据进了日志文件"
        assert "supe…1234" in content
    finally:
        for handler in handlers:
            handler.close()
        logging.basicConfig(force=True)


def test_守护进程日志默认落在日志目录并按大小滚动(tmp_path):
    options = _options(tmp_path, log_max_bytes=2048, log_backup_count=1)
    from critval.daemon import _configure_logging

    _configure_logging(options)
    try:
        logger = logging.getLogger("critval.daemon")
        for index in range(200):
            logger.info("第 %d 行：守护进程运行日志", index)
        for handler in logging.getLogger().handlers:
            handler.flush()
        files = sorted(item.name for item in Path(options.log_dir).iterdir())
        assert any(name.endswith(".1") for name in files), f"守护进程日志没滚动：{files}"
    finally:
        logging.basicConfig(force=True)
