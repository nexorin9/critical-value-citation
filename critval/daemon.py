"""常驻守护进程（任务 id 13）：三路事件源 → 有界队列 → 并发处理 → 心跳。

**这是产品的运行主路径**，不是「跑一次出一次表」的脚本。进程起来以后
一直活着，直到收到停机信号。

::

    LIS 推送 ──▶ MLLP 监听线程 ─┐
    院内共享目录 ─▶ 目录投递线程 ─┼─▶ 有界队列 ─▶ 工作线程 ─▶ 台账 / 处置卡 / 推送
    定时器（到期复查 + 崩溃续跑）─┘                       │
                                                       ▼
                                          同进程值班台（台账页 / 确认页）

**三路事件源**

1. **MLLP 监听**（``LIS_MLLP_BIND``）—— 院内 LIS 的主通道；
2. **目录投递**（帧目录 + 材料目录）—— 共享目录里的 ER7 帧与阈值表 /
   制度文件；材料变了就重建规则集（换快照原子切换）；
3. **定时器** —— 每隔一段时间跑两件事：**到期复查重推**（按制度时限
   盯回执）与**崩溃续跑**（按台账状态接着走，不重复推送、不重复回写）。

**有界队列**是关键的一条：队列满了就**拒收并留痕**，绝不无限堆积内存。
危急值不是可以慢慢处理的数据 —— 但也不是可以无限堆积的数据。

**重启续跑**：判断「该做什么」的依据**只有已落库的事实**（事件行状态 +
通知记录 + 审计流水），不靠进程内存 —— 内存随崩溃一起没了。续跑口径见
``critval/ledger/recovery.py``，本模块只负责把注入的出口接上。

**人工确认点只有一个**：值班人在值班台点确认，事件才从「建议」变成
「已生效事实」。守护进程的任何自动路径都跨不过这条线。

**凭据全部从环境变量读**（``Config.get_env``），代码内不出现密钥；
测试替身只在 ``tests/fixtures/`` 下，不是最终集成面。
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import queue
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from critval.config import Config
from critval.models import CriticalEvent
from critval.runtime.heartbeat import (
    DEPENDENCY_REGISTRY,
    DEPENDENCY_ROSTER,
    DEPENDENCY_WECOM,
    DependencySpec,
    HeartbeatState,
    HeartbeatWriter,
    build_health_payload,
    probe_dependencies,
)
from critval.runtime.lock import SingleInstanceError, SingleInstanceLock
from critval.runtime.logging_setup import (
    DEFAULT_BACKUP_COUNT,
    DEFAULT_MAX_BYTES,
    configure_runtime_logging,
)

__all__ = [
    "Daemon",
    "DaemonOptions",
    "DaemonStats",
    "main",
]

logger = logging.getLogger("critval.daemon")

#: 帧投递目录里认的扩展名。
FRAME_SUFFIXES = (".hl7", ".er7", ".txt")

#: 材料投递目录里认的扩展名。
MATERIAL_SUFFIXES = (".xlsx", ".docx", ".pdf")

#: 默认目录。
DEFAULT_FRAME_DIR = "./data/inbound_frames"
DEFAULT_MATERIAL_DIR = "./data/materials"
DEFAULT_SNAPSHOT_DIR = "./data/card_snapshots"

#: 运行保障默认落点（任务 id 46）。
DEFAULT_HEARTBEAT_PATH = "./data/heartbeat.json"
DEFAULT_LOG_DIR = "./logs"
DEFAULT_LOG_MAX_BYTES = DEFAULT_MAX_BYTES
DEFAULT_LOG_BACKUP_COUNT = DEFAULT_BACKUP_COUNT


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------


@dataclass
class DaemonOptions:
    """守护进程运行参数（命令行优先，其次环境变量，最后默认值）。"""

    db_path: str = "./data/critval.db"
    threshold_path: str = ""
    policy_path: str = ""
    frame_dir: str = DEFAULT_FRAME_DIR
    material_dir: str = DEFAULT_MATERIAL_DIR
    snapshot_dir: str = DEFAULT_SNAPSHOT_DIR
    mllp_bind: str = ""
    queue_size: int = 256
    workers: int = 2
    heartbeat_seconds: float = 30.0
    timer_interval_seconds: float = 60.0
    poll_interval: float = 1.0
    console_host: str = "127.0.0.1"
    console_port: int = 8760
    console_enabled: bool = True
    listen_enabled: bool = True
    log_path: str = ""
    log_dir: str = DEFAULT_LOG_DIR
    log_max_bytes: int = DEFAULT_LOG_MAX_BYTES
    log_backup_count: int = DEFAULT_LOG_BACKUP_COUNT
    run_seconds: float = 0.0
    # ---- 运行保障（任务 id 46）----
    #: 心跳文件路径：外部脚本读它判断「进程还在动吗」。
    heartbeat_path: str = DEFAULT_HEARTBEAT_PATH
    #: 单实例锁文件路径：留空则取台账库同级目录下的 ``critval.lock``。
    lock_path: str = ""
    #: 是否启用单实例锁（测试与临时试跑可关掉）。
    lock_enabled: bool = True
    #: 优雅停机宽限期（秒）：在途事件处理不完就强制结束。
    shutdown_grace_seconds: float = 30.0

    @classmethod
    def from_env(cls) -> "DaemonOptions":
        """按环境变量装配（凭据位与路径都不写在代码里）。"""
        Config.init_env()
        listen_base = Config.get_env("CRITVAL_LISTEN_BASE_URL") or ""
        host, port = cls._host_port(listen_base)
        return cls(
            db_path=Config.get_env("CRITVAL_DB_PATH") or "./data/critval.db",
            threshold_path=Config.get_env("CRITVAL_THRESHOLD_PATH") or "",
            policy_path=Config.get_env("CRITVAL_POLICY_PATH") or "",
            frame_dir=Config.get_env("CRITVAL_FRAME_DIR") or DEFAULT_FRAME_DIR,
            material_dir=Config.get_env("CRITVAL_MATERIAL_DIR") or DEFAULT_MATERIAL_DIR,
            snapshot_dir=Config.get_env("CRITVAL_SNAPSHOT_DIR") or DEFAULT_SNAPSHOT_DIR,
            mllp_bind=Config.get_env("LIS_MLLP_BIND") or "",
            queue_size=int(Config.get_env("CRITVAL_QUEUE_SIZE") or 256),
            workers=int(Config.get_env("CRITVAL_WORKERS") or 2),
            heartbeat_seconds=float(
                Config.get_env("CRITVAL_HEARTBEAT_SECONDS") or 30.0
            ),
            timer_interval_seconds=float(
                Config.get_env("CRITVAL_TIMER_INTERVAL_SECONDS") or 60.0
            ),
            console_host=host,
            console_port=port,
            log_dir=Config.get_env("CRITVAL_LOG_DIR") or DEFAULT_LOG_DIR,
            log_max_bytes=int(
                Config.get_env("CRITVAL_LOG_MAX_BYTES") or DEFAULT_LOG_MAX_BYTES
            ),
            log_backup_count=int(
                Config.get_env("CRITVAL_LOG_BACKUP_COUNT") or DEFAULT_LOG_BACKUP_COUNT
            ),
            heartbeat_path=Config.get_env("CRITVAL_HEARTBEAT_PATH")
            or DEFAULT_HEARTBEAT_PATH,
            lock_path=Config.get_env("CRITVAL_LOCK_PATH") or "",
            shutdown_grace_seconds=float(
                Config.get_env("CRITVAL_SHUTDOWN_GRACE_SECONDS") or 30.0
            ),
        )

    def resolved_lock_path(self) -> str:
        """锁文件落点：显式配置优先，否则与台账库同目录。"""
        explicit = str(self.lock_path or "").strip()
        if explicit:
            return explicit
        return str(Path(self.db_path).parent / "critval.lock")

    @staticmethod
    def _host_port(base_url: str) -> tuple[str, "int"]:
        """从 ``CRITVAL_LISTEN_BASE_URL`` 里取监听地址与端口。"""
        raw = str(base_url or "").strip()
        if not raw:
            return "127.0.0.1", 8760
        from urllib.parse import urlsplit

        split = urlsplit(raw if "://" in raw else f"http://{raw}")
        host = split.hostname or "127.0.0.1"
        port = split.port or (443 if split.scheme == "https" else 80)
        return host, int(port)


@dataclass
class DaemonStats:
    """运行计数（心跳行与体检都用它）。"""

    frames_received: int = 0
    events_submitted: int = 0
    events_processed: int = 0
    duplicates: int = 0
    failed: int = 0
    dropped: int = 0
    repushed: int = 0
    resumed: int = 0
    materials_reloaded: int = 0
    #: 停机宽限期内没处理完、被明确放弃的事件数（不静默消失）。
    abandoned: int = 0
    #: 正在处理中的事件数（在途）。
    inflight: int = 0
    started_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
    )
    last_error_at: str = ""
    last_error: str = ""

    def note_error(self, exc: BaseException) -> None:
        self.last_error_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.last_error = f"{type(exc).__name__}: {exc}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at,
            "frames_received": self.frames_received,
            "events_submitted": self.events_submitted,
            "events_processed": self.events_processed,
            "duplicates": self.duplicates,
            "failed": self.failed,
            "dropped": self.dropped,
            "repushed": self.repushed,
            "resumed": self.resumed,
            "materials_reloaded": self.materials_reloaded,
            "abandoned": self.abandoned,
            "inflight": self.inflight,
            "last_error_at": self.last_error_at,
            "last_error": self.last_error,
        }


# ---------------------------------------------------------------------------
# 推送出口
# ---------------------------------------------------------------------------


class DaemonNotifier:
    """推送出口：企微优先；企微没配或推不出去 → 落本机通知队列。

    **落本机队列也算送达** —— 本机队列就是值班台（值班人打开值班台就能
    看到这张处置卡）。但**渠道会如实记**：``channel=local`` +
    ``fallback_used=True`` + 原因写在通知记录与审计里，不假装推到了群里。

    这是「本产品不能假装推出去了」与「事件不能丢」之间的取舍点：
    两者都成立时，卡片落到值班台，状态记 ``notified``，渠道记 ``local``。
    """

    def __init__(self, *, primary: Any = None, local: Any = None) -> None:
        self.primary = primary
        self.local = local

    @classmethod
    def from_config(cls) -> "DaemonNotifier":
        """按环境变量装配（没配企微 webhook 就只用本机队列）。"""
        from critval.notify.local import LocalNotifier
        from critval.notify.wecom import WeComNotifier

        primary = None
        url = Config.get_env("CRITVAL_WECOM_WEBHOOK_URL") or ""
        if url:
            primary = WeComNotifier(url)
        return cls(primary=primary, local=LocalNotifier.from_config())

    def channel(self) -> str:
        return "wecom" if self.primary is not None else "local"

    def send_card(
        self,
        card: Any,
        *,
        confirm_url: str = "",
        event_id: str = "",
        notify_target: str = "",
        due_minutes: Optional[int] = None,
    ) -> Any:
        """推一张处置卡。"""
        from critval.card.renderer import render_markdown
        from critval.notify.local import NotifyOutcome

        content = render_markdown(card, confirm_url)
        if self.primary is not None:
            result = self.primary.send_card(card, confirm_url=confirm_url)
            if getattr(result, "ok", False):
                return NotifyOutcome(
                    ok=True,
                    channel="wecom",
                    attempts=getattr(result, "attempts", 0),
                    errcode=getattr(result, "errcode", 0),
                )
            reason = (
                f"企微推送失败：{getattr(result, 'error', '') or getattr(result, 'errmsg', '')}"
            )
            attempts = getattr(result, "attempts", 0)
        else:
            reason = "未配置企微群机器人 webhook（CRITVAL_WECOM_WEBHOOK_URL）"
            attempts = 0

        record = self.local.send(
            content,
            event_id=event_id or getattr(card.event, "event_id", ""),
            card_id=getattr(card, "card_id", ""),
            notify_target=notify_target,
            due_minutes=due_minutes,
            reason=reason,
        )
        return NotifyOutcome(
            ok=True,
            channel="local",
            fallback_used=True,
            error=reason,
            attempts=attempts,
            local_record=record,
        )

    def send_text(
        self,
        content: str,
        *,
        confirm_url: str = "",
        event_id: str = "",
        card_id: str = "",
        notify_target: str = "",
        due_minutes: Optional[int] = None,
        reason: str = "",
    ) -> Any:
        """推一段正文（重推 / 续跑走这条路，用的是落库的快照正文）。"""
        from critval.notify.local import NotifyOutcome

        if self.primary is not None:
            result = self.primary.send_markdown(content, confirm_url=confirm_url)
            if getattr(result, "ok", False):
                return NotifyOutcome(
                    ok=True,
                    channel="wecom",
                    attempts=getattr(result, "attempts", 0),
                    errcode=getattr(result, "errcode", 0),
                )
            detail = (
                f"企微推送失败：{getattr(result, 'error', '') or getattr(result, 'errmsg', '')}"
            )
        else:
            detail = reason or "未配置企微群机器人 webhook（CRITVAL_WECOM_WEBHOOK_URL）"

        record = self.local.send(
            content,
            event_id=event_id,
            card_id=card_id,
            notify_target=notify_target,
            due_minutes=due_minutes,
            reason=detail,
            kind="retry",
        )
        return NotifyOutcome(
            ok=True,
            channel="local",
            fallback_used=True,
            error=detail,
            local_record=record,
        )


# ---------------------------------------------------------------------------
# 出卡节点（附带落不可变快照）
# ---------------------------------------------------------------------------


def _build_snapshot_card_node() -> Any:
    """出卡节点 + 落快照（值班台复核读快照，不重新渲染）。"""
    from critval.flow.nodes import CardNode

    class SnapshotCardNode(CardNode):
        """出卡后立刻落一份不可变快照。"""

        def run(self, context: Any) -> Any:
            output = super().run(context)
            card = context.get("card")
            snapshot_store = context.get("snapshot_store")
            if card is not None and snapshot_store is not None:
                snapshot_store.save(
                    card,
                    str(
                        self.param.get("confirm_url")
                        or context.get("confirm_url")
                        or ""
                    ),
                )
            return output

    return SnapshotCardNode


def _build_node_registry() -> Any:
    """处置链节点注册表 —— 与内置注册表一致，只是「出卡」这环多了落快照。

    内置注册表（``build_default_registry``）不允许重复注册同名组件，
    所以这里**另建一份**并把 ``card`` 换成带快照的版本，其余节点原样复用
    （判定 / 召回 / 推送 / 确认 / 回写 / 转人工 一行都不重写）。
    """
    from critval.flow.dsl import DEFAULT_FALLBACK_COMPONENT, NodeRegistry
    from critval.flow.nodes import (
        AckNode,
        JudgeNode,
        NotifyNode,
        RetrieveNode,
        StageNode,
        TransferToHumanNode,
        WritebackNode,
    )

    registry = NodeRegistry()
    registry.register("received", lambda name, param: StageNode(name, param, "received"))
    registry.register("judge", JudgeNode)
    registry.register("retrieve", RetrieveNode)
    registry.register("card", _build_snapshot_card_node())
    registry.register("notify", NotifyNode)
    registry.register("ack", AckNode)
    registry.register("writeback", WritebackNode)
    registry.register("archived", lambda name, param: StageNode(name, param, "archived"))
    registry.register(DEFAULT_FALLBACK_COMPONENT, TransferToHumanNode)
    return registry


# ---------------------------------------------------------------------------
# 守护进程
# ---------------------------------------------------------------------------


class Daemon:
    """常驻守护进程。"""

    def __init__(self, options: Optional[DaemonOptions] = None) -> None:
        self.options = options or DaemonOptions()
        self.stats = DaemonStats()
        self.queue: "queue.Queue[CriticalEvent]" = queue.Queue(
            maxsize=max(1, int(self.options.queue_size))
        )
        self.store = None
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._material_lock = threading.Lock()
        self._material_fingerprints: dict[str, str] = {}
        self._rows: list[Any] = []
        self._ruleset: Any = None
        self._policy: Any = None
        self._threshold_path = self.options.threshold_path
        self._policy_path = self.options.policy_path
        self._notifier: Optional[DaemonNotifier] = None
        self._roster: Any = None
        self._registry: Any = None
        self._snapshot_store: Any = None
        self._pipeline: Any = None
        self._server: Any = None
        self._router: Any = None
        self._listener: Any = None
        self._processor: Any = None
        self._console_base_url = ""
        # ---- 运行保障（任务 id 46）----
        #: 单实例锁：同一个数据目录只允许一个守护进程。
        self._lock: Optional[SingleInstanceLock] = None
        #: 心跳文件写入器。
        self._heartbeat = HeartbeatWriter(self.options.heartbeat_path)
        #: 是否还接收新事件（停机开始后置 False —— 停机即拒收）。
        self._accepting = True
        #: 停机宽限期的截止时刻（``time.monotonic`` 口径）。
        self._drain_deadline = 0.0
        #: 进程启动时刻（算运行时长用）。
        self._started_monotonic = time.monotonic()
        #: 停机原因（写进心跳与审计）。
        self._stop_reason = ""

    # ---- 装配 -------------------------------------------------------------
    def setup(self) -> None:
        """把台账、材料、推送出口、处置链、值班台都装配起来。

        **先抢单实例锁**：抢不到就直接报错退出 —— 两个实例同时推同一张处置卡，
        比一个实例挂掉更麻烦。
        """
        from critval.card.snapshot import CardSnapshotStore
        from critval.console.app import build_service
        from critval.console.pages import ConsoleRouter
        from critval.flow.dsl import DEFAULT_FALLBACK_COMPONENT, FlowDSL
        from critval.flow.pipeline import Pipeline
        from critval.ledger.store import LedgerStore

        options = self.options
        self.acquire_lock()
        self.store = LedgerStore(options.db_path)
        self._snapshot_store = CardSnapshotStore(options.snapshot_dir)
        self._notifier = DaemonNotifier.from_config()
        self._roster = self._build_roster()
        self._registry = self._build_registry()
        self.load_materials()

        # 处置链：收到 → 判定 → 召回 → 出卡 → 推送（**到此为止**）。
        # 确认与回写不在自动路径上 —— 那是值班人点的。
        dsl = FlowDSL()
        dsl.add("received", "received", {}, downstream=["judged"])
        dsl.add("judged", "judge", {}, downstream=["retrieved"])
        dsl.add("retrieved", "retrieve", {}, downstream=["card_built"])
        dsl.add("card_built", "card", {}, downstream=["notified"])
        dsl.add("notified", "notify", {}, downstream=[])
        dsl.add(DEFAULT_FALLBACK_COMPONENT, DEFAULT_FALLBACK_COMPONENT, {}, downstream=[])
        registry = _build_node_registry()
        self._pipeline = Pipeline(dsl, registry, store=self.store)

        # 值班台（同进程）：入站推送端点 + 页面 + 确认动作。
        self._console_base_url = f"http://{options.console_host}:{options.console_port}"
        if options.console_enabled:
            service = build_service(
                db_path=options.db_path,
                threshold_path=options.threshold_path,
            )
            self._router = ConsoleRouter(
                self.store,
                token=Config.get_env("CONSOLE_TOKEN") or "",
                ack_handler=self.ack_event,
                replay_handler=self.replay_writeback,
                snapshot_dir=options.snapshot_dir,
                base_url=self._console_base_url,
                threshold_path=options.threshold_path,
            )
            service.console_router = self._router
            # 健康检查端点（/healthz）取守护进程的健康摘要 —— 独立跑值班台
            # 服务时没有守护进程，此时走服务层的最小摘要。
            service.health_provider = self.health
            from critval.console.app import make_server

            self._server = make_server(
                options.console_host, options.console_port, service=service
            )

    # ---- 单实例锁 ---------------------------------------------------------
    def acquire_lock(self) -> None:
        """抢单实例锁。抢不到就抛 :class:`SingleInstanceError`。"""
        if not self.options.lock_enabled:
            return
        if self._lock is None:
            self._lock = SingleInstanceLock(self.options.resolved_lock_path())
        if self._lock.held:
            return
        self._lock.acquire()
        logger.info("单实例锁已持有：%s", self._lock.path)

    def release_lock(self) -> None:
        """放锁（停机时做）。"""
        if self._lock is not None:
            self._lock.release()

    def _build_roster(self) -> Any:
        """排班系统客户端（没配凭据位就明说没有，不兜底假值）。"""
        try:
            from critval.roster.cache import RosterCache
            from critval.roster.client import RosterClient

            cache_path = Path(self.options.db_path).parent / "roster_cache.json"
            return RosterClient.from_config(cache=RosterCache(cache_path))
        except Exception as exc:  # noqa: BLE001 —— 缺凭据位不该让守护进程起不来
            logger.warning("排班系统未接入（%s）：处置卡的「通知对象」将标为依据缺失", exc)
            return None

    def _build_registry(self) -> Any:
        """登记系统回写器（没配凭据位就明说没有）。"""
        try:
            from critval.writeback.registry import RegistryWriteback

            return RegistryWriteback.from_config(self.store)
        except Exception as exc:  # noqa: BLE001 —— 缺凭据位不该让守护进程起不来
            logger.warning("登记系统回写未接入（%s）：确认后无法回写，将明说", exc)
            return None

    # ---- 材料 -------------------------------------------------------------
    def load_materials(self) -> dict[str, Any]:
        """装载阈值表与院内制度（材料变了就重装，规则集换快照原子切换）。"""
        from critval.judge.registry import RuleRegistry
        from critval.kb.loader import load_threshold_table
        from critval.kb.policy import load_policy_document

        summary: dict[str, Any] = {"threshold": "", "policy": "", "rows": 0}
        with self._material_lock:
            if self._threshold_path:
                try:
                    table = load_threshold_table(self._threshold_path)
                    self._rows = list(table.rows)
                    RuleRegistry.init_config(self._threshold_path)
                    self._ruleset = RuleRegistry.get_ruleset()
                    summary["threshold"] = table.source_name
                    summary["rows"] = len(self._rows)
                    # 已装载的材料登记指纹 —— 目录扫描不会把同一份材料当「刚变了」
                    self._remember_fingerprint(self._threshold_path)
                    logger.info(
                        "阈值表已装载：%s（%d 行 · 规则集 %s）",
                        table.source_name,
                        len(self._rows),
                        RuleRegistry.snapshot_id(),
                    )
                except Exception as exc:  # noqa: BLE001
                    self.stats.note_error(exc)
                    logger.warning("阈值表装载失败：%s", exc)
            if self._policy_path:
                try:
                    document = load_policy_document(self._policy_path)
                    self._policy = document
                    summary["policy"] = str(self._policy_path)
                    self._remember_fingerprint(self._policy_path)
                    logger.info(
                        "院内制度已装载：%d 条条款（版本 %s）",
                        len(document.clauses),
                        "、".join(document.versions()) or "未标注",
                    )
                except Exception as exc:  # noqa: BLE001
                    self.stats.note_error(exc)
                    logger.warning("院内制度装载失败：%s", exc)
        return summary

    def _remember_fingerprint(self, path: str) -> None:
        """登记已装载材料的指纹（避免目录扫描把同一份材料当「刚变了」）。"""
        try:
            digest = hashlib.sha256(Path(path).read_bytes()).hexdigest()
        except OSError:
            return
        self._material_fingerprints[str(Path(path).resolve())] = digest
        self._material_fingerprints[str(path)] = digest

    # ---- 生命周期 ---------------------------------------------------------
    def start(self) -> None:
        """起服务：值班台 + MLLP 监听 + 目录投递 + 定时器 + 工作线程。"""
        if self.store is None:
            self.setup()
        self._accepting = True
        self._started_monotonic = time.monotonic()
        self.heartbeat()

        if self._server is not None:
            thread = threading.Thread(
                target=self._server.serve_forever, name="critval-console", daemon=True
            )
            thread.start()
            self._threads.append(thread)
            logger.info(
                "值班台已启动：http://%s:%s/events", self.options.console_host, self.options.console_port
            )

        if self.options.listen_enabled and self.options.mllp_bind:
            self._start_mllp()

        for index in range(max(1, int(self.options.workers))):
            thread = threading.Thread(
                target=self._worker_loop, name=f"critval-worker-{index}", daemon=True
            )
            thread.start()
            self._threads.append(thread)

        thread = threading.Thread(target=self._directory_loop, name="critval-dir", daemon=True)
        thread.start()
        self._threads.append(thread)

        thread = threading.Thread(target=self._timer_loop, name="critval-timer", daemon=True)
        thread.start()
        self._threads.append(thread)

        thread = threading.Thread(target=self._heartbeat_loop, name="critval-heartbeat", daemon=True)
        thread.start()
        self._threads.append(thread)

        logger.info(
            "守护进程已就绪：工作线程 %d · 队列容量 %d · MLLP %s",
            self.options.workers,
            self.options.queue_size,
            self.options.mllp_bind or "（未配置）",
        )

    def _start_mllp(self) -> None:
        from critval.hl7.mllp import MllpInboundProcessor, MllpListener

        self._processor = MllpInboundProcessor(store=self.store)

        def _handle(frame: bytes, peer: str) -> None:
            self.stats.frames_received += 1
            try:
                events, unparsed, _encoding = self._processor.parse(frame)
            except Exception as exc:  # noqa: BLE001 —— 一帧读不懂不影响后面的帧
                self.stats.note_error(exc)
                logger.warning("MLLP 帧解析失败（来自 %s）：%s", peer or "未知", exc)
                return
            for record in unparsed:
                logger.warning("MLLP 帧里有未识别记录：%s", getattr(record, "reason", ""))
            for event in events:
                self.submit(event, origin="mllp")

        try:
            self._listener = MllpListener(
                self.options.mllp_bind, _handle, store=self.store
            )
            self._listener.start()
            logger.info(
                "MLLP 监听已启动：%s:%s", self._listener.host, self._listener.port
            )
        except Exception as exc:  # noqa: BLE001 —— 端口占用不该让整个守护进程死掉
            self.stats.note_error(exc)
            logger.error("MLLP 监听启动失败（%s）：%s", self.options.mllp_bind, exc)

    # ---- 停机（优雅）------------------------------------------------------
    def request_stop(self, reason: str = "") -> None:
        """请求停机：**立即停止接收新事件**，在途事件按宽限期处理完。

        这是 SIGTERM 的落点 —— 信号处理器里只做「置标志」，重活交给
        :meth:`stop`（信号处理器里做 IO 是不安全的）。
        """
        first = not self._stop_reason
        if first:
            self._stop_reason = reason or "收到停机请求"
        self._accepting = False
        if not self._drain_deadline:
            grace = max(0.0, float(self.options.shutdown_grace_seconds))
            self._drain_deadline = time.monotonic() + grace
        self._stop.set()
        if first:
            logger.info("收到停机请求：%s", self._stop_reason)

    def stop(self) -> None:
        """停机：拒收新事件 → 在途事件处理完（或超时强制结束）→ 落最后一份心跳。

        **已落库的状态不回退**：停机只影响「还没处理的事件」，已经写进台账的
        状态原样留在那里（状态机本身也不允许回退）。
        """
        if self._stop.is_set() and not self._threads:
            # 已经停过一次：只补一次收尾（放锁 + 关库）。
            self._finalize_stop()
            return
        self.request_stop(self._stop_reason or "调用 stop()")
        logger.info(
            "停止接收新事件，等待在途事件处理完（宽限期 %.1f 秒）",
            float(self.options.shutdown_grace_seconds),
        )

        # ① 先停入站（不再收新帧），再等在途。
        if self._listener is not None:
            try:
                self._listener.stop()
            except Exception:  # noqa: BLE001
                pass

        # ② 等工作线程把在途与队列里的事件处理完（宽限期到点就强制结束）。
        workers = [
            thread for thread in self._threads if thread.name.startswith("critval-worker")
        ]
        deadline = self._drain_deadline or (
            time.monotonic() + max(0.0, float(self.options.shutdown_grace_seconds))
        )
        for thread in workers:
            remaining = max(0.0, deadline - time.monotonic())
            thread.join(timeout=remaining)

        # ③ 值班台收尾（页面不再对外服务）。
        if self._server is not None:
            try:
                self._server.shutdown()
            except Exception:  # noqa: BLE001
                pass
            try:
                self._server.server_close()
            except Exception:  # noqa: BLE001
                pass

        # ④ 其余线程（目录 / 定时器 / 心跳）收尾。
        for thread in self._threads:
            if thread in workers:
                continue
            thread.join(timeout=5.0)
        self._threads.clear()

        self._finalize_stop()

    def _finalize_stop(self) -> None:
        """停机收尾：写最后一份心跳 → 关库 → 放锁。"""
        try:
            self.heartbeat(status="stopped", note=self._stop_reason or "已停机")
        except Exception:  # noqa: BLE001 —— 收尾不该再抛
            pass
        if self.store is not None:
            try:
                self.store.close()
            except Exception:  # noqa: BLE001
                pass
        self.release_lock()
        logger.info("守护进程已停止")

    def run_forever(self) -> int:
        """起服务并常驻（收到 SIGTERM / SIGINT 优雅停机）。"""
        self.start()

        def _handler(signum: int, _frame: Any) -> None:
            logger.info("收到信号 %s，准备停机", signum)
            self.request_stop(f"收到信号 {signum}")

        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, _handler)
            except (ValueError, OSError):  # pragma: no cover —— 非主线程时忽略
                pass

        deadline = time.monotonic() + self.options.run_seconds if self.options.run_seconds else 0.0
        try:
            while not self._stop.is_set():
                if deadline and time.monotonic() >= deadline:
                    logger.info("到达设定运行时长，准备停机")
                    break
                self._stop.wait(0.5)
        finally:
            self.stop()
        return 0

    # ---- 事件入队 ---------------------------------------------------------
    def submit(self, event: Any, *, origin: str = "") -> bool:
        """把事件放进有界队列。**队列满就拒收并留痕**，不无限堆积。

        **停机开始后不再收新事件** —— 拒收同样留痕，事件不静默消失
        （重启续跑会按台账状态补齐；这一条还没落库，由上游通道重投）。
        """
        if not self._accepting:
            self.stats.dropped += 1
            logger.warning(
                "守护进程正在停机，新事件拒收（不静默丢弃）：%s（来源 %s）",
                getattr(event, "event_id", "?"),
                origin or "未知",
            )
            if self.store is not None:
                self.store.append_audit(
                    "daemon_stopping",
                    actor="daemon",
                    event_id=getattr(event, "event_id", ""),
                    dept_code=getattr(event, "dept_code", ""),
                    specimen_no=getattr(event, "specimen_no", ""),
                    item_code=getattr(event, "item_code", ""),
                    message_control_id=getattr(event, "message_control_id", ""),
                    detail="守护进程正在停机，按宽限期处理完在途事件后退出；本条拒收待上游重投",
                )
            return False
        try:
            self.queue.put_nowait(event)
        except queue.Full:
            self.stats.dropped += 1
            logger.error(
                "队列已满，事件被拒收（不静默丢弃）：%s（来源 %s）",
                getattr(event, "event_id", "?"),
                origin or "未知",
            )
            if self.store is not None:
                self.store.append_audit(
                    "daemon_queue_full",
                    actor="daemon",
                    event_id=getattr(event, "event_id", ""),
                    dept_code=getattr(event, "dept_code", ""),
                    specimen_no=getattr(event, "specimen_no", ""),
                    item_code=getattr(event, "item_code", ""),
                    message_control_id=getattr(event, "message_control_id", ""),
                    detail=f"有界队列已满（容量 {self.options.queue_size}），事件拒收待人工处理",
                )
            return False
        self.stats.events_submitted += 1
        return True

    # ---- 工作线程 ---------------------------------------------------------
    def _worker_loop(self) -> None:
        """工作线程：正常时持续取事件；停机时**把队列里剩下的处理完**再退。

        停机宽限期内继续处理 —— 危急值已经在队列里了，丢掉它就等于漏处置。
        超过宽限期还没处理完的**明确留痕**（不算失败，也不静默消失）。
        """
        while True:
            try:
                event = self.queue.get(timeout=0.2)
            except queue.Empty:
                if self._stop.is_set():
                    break
                continue
            if self._stop.is_set() and self._drain_expired():
                self._abandon(event)
                self.queue.task_done()
                break
            self.stats.inflight += 1
            try:
                self.process_event(event)
            except Exception as exc:  # noqa: BLE001 —— 一条事件炸了不该带倒线程
                self.stats.failed += 1
                self.stats.note_error(exc)
                logger.exception("事件处理失败：%s", getattr(event, "event_id", "?"))
            finally:
                self.stats.inflight -= 1
                self.queue.task_done()

    def _drain_expired(self) -> bool:
        """停机宽限期是否已到。"""
        if not self._drain_deadline:
            return False
        return time.monotonic() >= self._drain_deadline

    def _abandon(self, event: Any) -> None:
        """宽限期到了还没处理完的事件：**明确留痕**，不当成处理成功。"""
        self.stats.abandoned += 1
        logger.error(
            "停机宽限期已到，这条事件未处理完（已留痕，重启后续跑补齐）：%s",
            getattr(event, "event_id", "?"),
        )
        if self.store is not None:
            self.store.append_audit(
                "daemon_shutdown_abandoned",
                actor="daemon",
                event_id=getattr(event, "event_id", ""),
                dept_code=getattr(event, "dept_code", ""),
                specimen_no=getattr(event, "specimen_no", ""),
                item_code=getattr(event, "item_code", ""),
                message_control_id=getattr(event, "message_control_id", ""),
                detail="停机宽限期已到，这条事件未处理完；重启后按台账状态续跑补齐",
            )

    def process_event(self, event: Any) -> dict[str, Any]:
        """处理一条事件：幂等去重 → 判定 → 召回 → 出卡 → 推送。"""
        if self.store is None:
            self.setup()
        event_id = getattr(event, "event_id", "")
        if not self.store.record_event(event):
            # 幂等键已存在 —— 既有事实不被覆盖，不重复判定、不重复推送。
            self.stats.duplicates += 1
            logger.info("重复到达，跳过（既有事实不覆盖）：%s", event_id)
            return {"event_id": event_id, "status": "duplicate"}

        clause = self._clause_for(event)
        duty_person, notify_target = self._resolve_duty(event)
        due_minutes = getattr(clause, "deadline_minutes", None) if clause else None
        confirm_url = self.confirm_url(event_id)

        context = {
            "event": event,
            "store": self.store,
            "rows": list(self._rows),
            "ruleset": self._ruleset,
            "clause_provider": clause,
            "notifier": self._notifier,
            "confirm_url": confirm_url,
            "duty_person": duty_person,
            "notify_target": notify_target,
            "due_minutes": due_minutes,
            "snapshot_store": self._snapshot_store,
        }
        result = self._pipeline.run(context)
        self.stats.events_processed += 1
        if result.status == "transferred_to_human":
            self.stats.note_error(RuntimeError(result.error or "转人工"))
        logger.info(
            "事件已处理：%s（%s）",
            event_id,
            " → ".join(result.executed) if result.executed else result.status,
        )
        return {
            "event_id": event_id,
            "status": result.status,
            "executed": list(result.executed),
            "error": result.error,
        }

    def _clause_for(self, event: Any) -> Any:
        """按项目码取院内制度条款（取不到就返回 None —— 卡片会标依据缺失）。

        **先要「有事实」的条款**：制度里点名某个项目的条款往往只写「本项目
        按阈值表执行」，通知对象 / 上报时限 / 记录去向在**一般条款**里。
        取依据的顺序是「点名本项目且事实齐全 → 事实齐全的一般条款 → 点名
        本项目的那条 → 第一条」—— 取不到就返回 None，**不拿别的条款顶替**。
        """
        document = self._policy
        if document is None:
            return None
        item_code = getattr(event, "item_code", "") or ""
        try:
            item_clauses = list(document.clauses_for_item(item_code)) if item_code else []
        except Exception:  # noqa: BLE001
            item_clauses = []

        def _has_facts(clause: Any) -> bool:
            return bool(getattr(clause, "notify_target", "")) and (
                getattr(clause, "deadline_minutes", None) is not None
            )

        usable_items = [clause for clause in item_clauses if _has_facts(clause)]
        if usable_items:
            return usable_items[0]
        general = [clause for clause in document.clauses if _has_facts(clause)]
        if general:
            return general[0]
        if item_clauses:
            return item_clauses[0]
        clauses = list(document.clauses)
        return clauses[0] if clauses else None

    def _resolve_duty(self, event: Any) -> tuple[str, str]:
        """取当班人（排班系统不可用就返回空 —— 卡片会标依据缺失）。"""
        if self._roster is None:
            return "", ""
        dept = getattr(event, "dept_code", "") or ""
        if not dept:
            return "", ""
        try:
            target = self._roster.resolve(
                dept, at=getattr(event, "observed_at", None)
            )
        except Exception as exc:  # noqa: BLE001 —— 排班挂了不该带倒整条链
            self.stats.note_error(exc)
            logger.warning("排班系统读取失败（科室 %s）：%s", dept, exc)
            return "", ""
        return (target.label() or ""), (target.label() or "")

    def confirm_url(self, event_id: str) -> str:
        """确认链接 —— 推送卡片里的那个链接就是它。"""
        return f"{self._console_base_url}/events/{event_id}/ack"

    # ---- 确认动作 ---------------------------------------------------------
    def ack_event(self, event_id: str, handler: str, note: str = "") -> Any:
        """值班人点确认：状态跃迁 + 幂等回写登记系统。"""
        from critval.console.pages import AckResult

        row = self.store.get_event(event_id)
        if not row:
            return AckResult(ok=False, message=f"台账里没有事件 {event_id}。")
        status = str(row.get("status", ""))
        if status in {"acknowledged", "registered", "archived"}:
            return AckResult(
                ok=True,
                message="本条处置已确认过，重复确认不会产生第二条回执。",
                written_back=status in {"registered", "archived"},
                status=status,
            )
        if status not in {"notified", "pending_ack"}:
            return AckResult(
                ok=False,
                message=f"当前状态是「{status}」，还不能确认（只有已推送待回执的事件可确认）。",
                status=status,
            )
        detail = note or "值班人已确认处置"
        self.store.mark_acknowledged(event_id, actor=handler, detail=detail)
        if self._registry is None:
            self.store.append_audit(
                "writeback_deferred",
                actor="writeback",
                event_id=event_id,
                detail="未接入登记系统回写通道（本轮没有 CRITVAL_BASE_URL / CRITVAL_TOKEN），回执未回写",
            )
            return AckResult(
                ok=True,
                message="已确认。登记系统回写通道未接入，回执暂未回写（已留痕）。",
                written_back=False,
                status="acknowledged",
            )
        outcome = self._registry.write_back(
            event_id,
            receipt_status="acknowledged",
            receipt_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        if not getattr(outcome, "ok", False):
            reason = getattr(outcome, "error", "") or getattr(outcome, "reason", "")
            return AckResult(
                ok=False,
                message=f"已确认，但回写登记系统失败：{reason}（事件留在台账里，可重试回写）",
                written_back=False,
                status="acknowledged",
            )
        self.store.mark_registered(
            event_id, actor=handler, detail="回执已按 message_control_id 幂等回写登记系统"
        )
        self.store.mark_archived(event_id, actor=handler, detail="处置链走完，事实定格归档")
        return AckResult(
            ok=True,
            message="已确认，回执已幂等回写登记系统。",
            written_back=True,
            status="archived",
        )

    def replay_writeback(self, event_id: str, handler: str, reason: str) -> Any:
        """值班人点「补写回写」：把已确认但没写回的回执补一次。

        **为什么是人点的**：自动重试会把「登记系统对端抽风」和「内容与
        既有事实打架」混成同一件事；幂等冲突必须由人裁决（转人工，不覆盖
        既有事实）。所以这条路径只在值班台上手动触发，且**原因必填** ——
        补写为什么拖到现在，是复核时要回答的问题。

        幂等键与首次回写一致（``message_control_id``），所以补写不会在
        登记系统里生成第二条回执；**内容变了会被幂等冲突闸门拦下**。
        """
        from critval.console.pages import ReplayResult

        row = self.store.get_event(event_id)
        if not row:
            return ReplayResult(
                ok=False,
                message=f"台账里没有事件 {event_id}。",
                error_code="event_not_found",
            )
        status = str(row.get("status", "") or "")
        if status not in {"acknowledged", "writeback_failed"}:
            return ReplayResult(
                ok=False,
                message=(
                    f"当前状态是「{status}」，不能补写"
                    "（只有已确认、尚未写回登记系统的回执才需要补写）。"
                ),
                error_code="illegal_transition",
                status=status,
            )
        if self._registry is None:
            return ReplayResult(
                ok=False,
                message=(
                    "登记系统回写通道未接入（本轮没有 CRITVAL_BASE_URL / "
                    "CRITVAL_TOKEN），补不了。"
                ),
                error_code="replay_unavailable",
                status=status,
            )
        outcome = self._registry.replay(event_id)
        if not getattr(outcome, "ok", False):
            reason_text = getattr(outcome, "error", "") or getattr(outcome, "reason", "")
            # 补写失败：**本条不丢**，留在待写队列里等人再试。
            self.store.append_audit(
                "writeback_replay_failed",
                actor=handler,
                event_id=event_id,
                detail=f"人工补写失败：{reason_text}（原因：{reason}）",
            )
            return ReplayResult(
                ok=False,
                message=f"补写失败：{reason_text}（本条仍在待写队列里，没有被丢弃）",
                error_code="replay_failed",
                written_back=False,
                status=str(getattr(outcome, "status", "") or status),
            )
        self.store.mark_registered(
            event_id, actor=handler, detail="人工补写回执已回写登记系统"
        )
        self.store.mark_archived(event_id, actor=handler, detail="处置链走完，事实定格归档")
        self.store.append_audit(
            "writeback_replayed",
            actor=handler,
            event_id=event_id,
            detail=f"人工补写回写登记系统（原因：{reason}）",
        )
        return ReplayResult(
            ok=True,
            message="补写成功，回执已按 message_control_id 幂等回写登记系统。",
            written_back=True,
            status="archived",
        )

    # ---- 目录投递 ---------------------------------------------------------
    def _directory_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.scan_frames()
                self.scan_materials()
            except Exception as exc:  # noqa: BLE001
                self.stats.note_error(exc)
                logger.exception("目录投递扫描失败")
            self._stop.wait(max(0.05, float(self.options.poll_interval)))

    def scan_frames(self) -> int:
        """扫帧投递目录：取新帧 → 解析 → 入队 → 归档已处理文件。"""
        directory = Path(self.options.frame_dir)
        if not directory.exists():
            return 0
        processed = directory / "processed"
        count = 0
        for path in sorted(directory.iterdir()):
            if not path.is_file() or path.suffix.lower() not in FRAME_SUFFIXES:
                continue
            try:
                payload = path.read_bytes()
            except OSError as exc:
                self.stats.note_error(exc)
                continue
            self.stats.frames_received += 1
            try:
                events, unparsed = self._parse_frame(payload)
            except Exception as exc:  # noqa: BLE001
                self.stats.note_error(exc)
                logger.warning("投递帧解析失败：%s（%s）", path.name, exc)
                self._archive_frame(path, processed, failed=True)
                continue
            for record in unparsed:
                logger.warning("投递帧里有未识别记录：%s", getattr(record, "reason", ""))
            for event in events:
                self.submit(event, origin=f"dir:{path.name}")
                count += 1
            self._archive_frame(path, processed, failed=False)
        return count

    def _parse_frame(self, payload: bytes) -> tuple[list[Any], list[Any]]:
        """帧字节 → （事件, 未识别记录）。复用 MLLP 的解析实现。

        一个文件里可能有多帧（MLLP 结束符分隔）—— 一帧一事件地拆开，
        不把多帧当成一帧读。
        """
        from critval.hl7.mllp import MllpInboundProcessor
        from critval.hl7.replay import split_frames

        if self._processor is None:
            self._processor = MllpInboundProcessor(store=self.store)
        events: list[Any] = []
        unparsed: list[Any] = []
        frames = split_frames(payload)
        if not frames:
            return events, unparsed
        for raw in frames:
            frame_events, frame_unparsed, _encoding = self._processor.parse(raw)
            events.extend(frame_events)
            unparsed.extend(frame_unparsed)
        return events, unparsed

    @staticmethod
    def _archive_frame(path: Path, processed: Path, *, failed: bool) -> None:
        """把处理过的帧挪走（成功进 processed，出错的进 failed —— 不静默丢）。"""
        try:
            target_dir = processed.parent / "failed" if failed else processed
            target_dir.mkdir(parents=True, exist_ok=True)
            path.replace(target_dir / path.name)
        except OSError:
            pass

    def scan_materials(self) -> int:
        """扫材料投递目录：材料指纹变了就重装（阈值表 / 制度）。"""
        directory = Path(self.options.material_dir)
        if not directory.exists():
            return 0
        changed = 0
        for path in sorted(directory.iterdir()):
            if not path.is_file() or path.suffix.lower() not in MATERIAL_SUFFIXES:
                continue
            try:
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
            except OSError:
                continue
            known = self._material_fingerprints.get(str(path)) or self._material_fingerprints.get(
                str(path.resolve())
            )
            if known == digest:
                continue
            self._material_fingerprints[str(path)] = digest
            if path.suffix.lower() == ".xlsx":
                self._threshold_path = str(path)
            elif path.suffix.lower() in {".docx", ".pdf"}:
                self._policy_path = str(path)
            self.load_materials()
            self.stats.materials_reloaded += 1
            changed += 1
            logger.info("材料已更新并重装：%s", path.name)
            if self.store is not None:
                self.store.append_audit(
                    "material_reloaded",
                    actor="daemon",
                    detail=f"材料 {path.name} 指纹变更，已重装并切换规则集快照",
                )
        return changed

    # ---- 定时器 -----------------------------------------------------------
    def _timer_loop(self) -> None:
        while not self._stop.is_set():
            self._stop.wait(max(0.1, float(self.options.timer_interval_seconds)))
            if self._stop.is_set():
                break
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001
                self.stats.note_error(exc)
                logger.exception("定时任务失败")

    def tick(self) -> dict[str, Any]:
        """一轮定时任务：到期复查重推 + 崩溃续跑。"""
        report = {"repushed": 0, "resumed": 0}

        retry = self._build_retry_runner()
        if retry is not None:
            result = retry.run()
            report["repushed"] = len(result.repushed)
            self.stats.repushed += report["repushed"]
            if result.outcomes:
                logger.info("到期复查：重推 %d 条", report["repushed"])

        recovery = self._build_recovery_runner()
        if recovery is not None:
            result = recovery.resume()
            acted = [
                outcome
                for outcome in result.outcomes
                if outcome.action
                in {
                    "pushed",
                    "push_reconciled",
                    "written_back",
                    "writeback_reconciled",
                    "rejudged",
                }
            ]
            report["resumed"] = len(acted)
            self.stats.resumed += report["resumed"]
            if acted:
                logger.info("崩溃续跑：处理 %d 条", report["resumed"])
        return report

    def _build_retry_runner(self) -> Any:
        from critval.ledger.retry import RetryRunner

        return RetryRunner(self.store, self._push_row, channel=self._notifier.channel())

    def _build_recovery_runner(self) -> Any:
        from critval.ledger.recovery import RecoveryRunner

        return RecoveryRunner(
            self.store,
            notifier=self._push_row,
            writeback=self._writeback_row,
            rejudge=self._rejudge_row,
            channel=self._notifier.channel(),
        )

    def _push_row(self, row: Any) -> bool:
        """推送出口（重推 / 续跑共用）：用**落库的快照正文**推，不重新渲染。"""
        event_id = str((row or {}).get("event_id", "") or "")
        if not event_id:
            return False
        try:
            snapshot = self._snapshot_store.find_by_event(event_id)
        except Exception:  # noqa: BLE001
            snapshot = None
        if snapshot is None:
            logger.warning("没有处置卡快照，推不了：%s", event_id)
            return False
        content = snapshot.rendering("markdown")
        confirm_url = self.confirm_url(event_id)
        outcome = self._notifier.send_text(
            content,
            confirm_url=confirm_url,
            event_id=event_id,
            card_id=snapshot.card_id,
            notify_target=str((row or {}).get("notify_target", "") or ""),
            due_minutes=(
                int(row["due_minutes"])
                if (row or {}).get("due_minutes") not in (None, "")
                else None
            ),
            reason="到期复查重推 / 崩溃续跑",
        )
        return bool(getattr(outcome, "ok", False))

    def _writeback_row(self, row: Any) -> bool:
        """回写出口（续跑用）：已确认的回执幂等回写登记系统。"""
        if self._registry is None:
            return False
        event_id = str((row or {}).get("event_id", "") or "")
        try:
            outcome = self._registry.write_back(
                event_id,
                receipt_status="acknowledged",
                receipt_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            )
        except Exception:  # noqa: BLE001
            logger.exception("续跑回写失败：%s", event_id)
            return False
        return bool(getattr(outcome, "ok", False))

    def _rejudge_row(self, row: Any) -> bool:
        """重新判定并出卡（续跑用；没有入口时明说推不动，不假装推进）。"""
        event_id = str((row or {}).get("event_id", "") or "")
        if not event_id:
            return False
        logger.warning("续跑需要重新判定，但本轮没有判定入口：%s", event_id)
        return False

    # ---- 心跳 -------------------------------------------------------------
    def _heartbeat_loop(self) -> None:
        while not self._stop.is_set():
            self._stop.wait(max(0.5, float(self.options.heartbeat_seconds)))
            if self._stop.is_set():
                break
            self.heartbeat()

    def heartbeat(self, status: str = "ok", note: str = "") -> str:
        """写心跳：**一行日志 + 一份心跳文件**（原子写）。

        心跳文件是给外部看的 —— 值班机上的监控脚本读它判断进程是不是还在动。
        """
        stats = self.stats
        line = (
            f"心跳 · 版本 0.1 · 队列深度 {self.queue.qsize()}/{self.options.queue_size} · "
            f"已收帧 {stats.frames_received} · 已处理 {stats.events_processed} · "
            f"重复 {stats.duplicates} · 失败 {stats.failed} · 拒收 {stats.dropped} · "
            f"重推 {stats.repushed} · 续跑 {stats.resumed} · "
            f"最近错误 {stats.last_error_at or '（无）'}"
        )
        logger.info(line)
        self.write_heartbeat_file(status=status, note=note)
        return line

    def write_heartbeat_file(self, *, status: str = "ok", note: str = "") -> None:
        """把当前状态写成心跳文件（写失败不打断主流程，只留日志）。"""
        try:
            state = HeartbeatState(
                pid=os.getpid(),
                started_at=self.stats.started_at,
                updated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                uptime_seconds=max(0.0, time.monotonic() - self._started_monotonic),
                queue_depth=self.queue.qsize(),
                queue_capacity=int(self.options.queue_size),
                counters={
                    "frames_received": self.stats.frames_received,
                    "events_submitted": self.stats.events_submitted,
                    "events_processed": self.stats.events_processed,
                    "duplicates": self.stats.duplicates,
                    "failed": self.stats.failed,
                    "dropped": self.stats.dropped,
                    "repushed": self.stats.repushed,
                    "resumed": self.stats.resumed,
                    "materials_reloaded": self.stats.materials_reloaded,
                    "abandoned": self.stats.abandoned,
                    "inflight": self.stats.inflight,
                },
                last_error_at=self.stats.last_error_at,
                last_error=self.stats.last_error,
                status=status,
                note=note or self._stop_reason,
            )
            self._heartbeat.write(state)
        except OSError as exc:
            logger.warning("心跳文件写不进去：%s", exc)

    # ---- 体检 -------------------------------------------------------------
    def dependency_specs(self) -> list[DependencySpec]:
        """要探活的出站依赖（排班系统 / 登记系统 / 企微通道）。"""
        return [
            DependencySpec(
                name=DEPENDENCY_ROSTER,
                base_url=Config.get_env("NURSE_ROSTER_BASE_URL") or "",
            ),
            DependencySpec(
                name=DEPENDENCY_REGISTRY,
                base_url=Config.get_env("CRITVAL_BASE_URL") or "",
            ),
            DependencySpec(
                name=DEPENDENCY_WECOM,
                base_url=Config.get_env("CRITVAL_WECOM_WEBHOOK_URL") or "",
            ),
        ]

    def health(self) -> dict[str, Any]:
        """健康摘要（``/healthz`` 的响应体）：状态 + 关键计数 + 依赖可达性。"""
        counts: dict[str, int] = {}
        if self.store is not None:
            try:
                counts = dict(self.store.status_counts())
            except Exception:  # noqa: BLE001
                counts = {}
        dependencies = probe_dependencies(self.dependency_specs())

        status = "ok"
        if self._stop.is_set():
            status = "stopping"
        elif any(item.configured and not item.reachable for item in dependencies):
            status = "degraded"

        heartbeat_info: dict[str, Any] = {
            "path": str(self._heartbeat.path),
            "written": self._heartbeat.path.exists(),
        }
        age = self._heartbeat.age_seconds()
        if age is not None:
            heartbeat_info["age_seconds"] = round(age, 3)
            heartbeat_info["stale"] = age > max(
                30.0, float(self.options.heartbeat_seconds) * 3
            )
        else:
            heartbeat_info["stale"] = True

        return build_health_payload(
            status=status,
            pid=os.getpid(),
            uptime_seconds=max(0.0, time.monotonic() - self._started_monotonic),
            queue_depth=self.queue.qsize(),
            queue_capacity=int(self.options.queue_size),
            counters=self.stats.to_dict(),
            status_counts=counts,
            dependencies=dependencies,
            heartbeat=heartbeat_info,
            last_error_at=self.stats.last_error_at,
            last_error=self.stats.last_error,
            extra={
                "console": self._console_base_url if self.options.console_enabled else "",
                "mllp": (
                    f"{self._listener.host}:{self._listener.port}"
                    if self._listener is not None
                    else ""
                ),
                "stop_reason": self._stop_reason,
                "accepting": self._accepting,
            },
        )


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m critval.daemon",
        description="危急值处置依据检索系统 · 常驻守护进程",
    )
    parser.add_argument("--db", default=None, help="台账库路径（默认取 CRITVAL_DB_PATH）")
    parser.add_argument("--threshold", default=None, help="阈值表 xlsx 路径")
    parser.add_argument("--policy", default=None, help="院内危急值报告制度 docx 路径")
    parser.add_argument("--frame-dir", default=None, help="帧投递目录")
    parser.add_argument("--material-dir", default=None, help="材料投递目录")
    parser.add_argument("--snapshot-dir", default=None, help="处置卡快照目录")
    parser.add_argument("--mllp-bind", default=None, help="MLLP 监听地址（如 127.0.0.1:2575）")
    parser.add_argument("--console-host", default=None, help="值班台监听地址")
    parser.add_argument("--console-port", type=int, default=None, help="值班台监听端口")
    parser.add_argument("--no-console", action="store_true", help="不起值班台（只跑入站与处理）")
    parser.add_argument("--workers", type=int, default=None, help="并发处理线程数")
    parser.add_argument("--queue-size", type=int, default=None, help="有界队列容量")
    parser.add_argument(
        "--heartbeat-seconds", type=float, default=None, help="心跳周期（秒）"
    )
    parser.add_argument(
        "--timer-interval-seconds", type=float, default=None, help="定时任务周期（秒）"
    )
    parser.add_argument("--log-file", default="", help="心跳与运行日志落盘路径")
    parser.add_argument(
        "--log-dir", default=None, help="日志目录（按大小滚动，默认取 CRITVAL_LOG_DIR）"
    )
    parser.add_argument(
        "--log-max-bytes", type=int, default=None, help="单个日志文件上限（字节）"
    )
    parser.add_argument(
        "--log-backup-count", type=int, default=None, help="保留的历史日志份数"
    )
    parser.add_argument(
        "--heartbeat-path", default=None, help="心跳文件路径（默认取 CRITVAL_HEARTBEAT_PATH）"
    )
    parser.add_argument(
        "--lock-path", default=None, help="单实例锁文件路径（默认与台账库同目录）"
    )
    parser.add_argument(
        "--no-lock", action="store_true", help="不启用单实例锁（仅用于临时试跑）"
    )
    parser.add_argument(
        "--shutdown-grace-seconds",
        type=float,
        default=None,
        help="停机宽限期（秒）：在途事件处理不完就强制结束",
    )
    parser.add_argument(
        "--run-seconds", type=float, default=0.0, help="跑够多少秒后自动退出（0 = 常驻）"
    )
    return parser


def _configure_logging(options: DaemonOptions) -> None:
    """装配日志：stderr + **按大小滚动**的文件（日志落 logs/，已在 .gitignore 中）。"""
    configure_runtime_logging(
        options.log_path,
        log_dir=options.log_dir,
        max_bytes=options.log_max_bytes,
        backup_count=options.log_backup_count,
    )


def main(argv: Optional[list[str]] = None) -> int:
    """``python -m critval.daemon`` 入口。"""
    args = _build_arg_parser().parse_args(argv)
    options = DaemonOptions.from_env()
    if args.db:
        options.db_path = args.db
    if args.threshold:
        options.threshold_path = args.threshold
    if args.policy:
        options.policy_path = args.policy
    if args.frame_dir:
        options.frame_dir = args.frame_dir
    if args.material_dir:
        options.material_dir = args.material_dir
    if args.snapshot_dir:
        options.snapshot_dir = args.snapshot_dir
    if args.mllp_bind:
        options.mllp_bind = args.mllp_bind
    if args.console_host:
        options.console_host = args.console_host
    if args.console_port:
        options.console_port = args.console_port
    if args.no_console:
        options.console_enabled = False
    if args.workers:
        options.workers = args.workers
    if args.queue_size:
        options.queue_size = args.queue_size
    if args.heartbeat_seconds is not None:
        options.heartbeat_seconds = args.heartbeat_seconds
    if args.timer_interval_seconds is not None:
        options.timer_interval_seconds = args.timer_interval_seconds
    if args.log_dir is not None:
        options.log_dir = args.log_dir
    if args.log_max_bytes is not None:
        options.log_max_bytes = args.log_max_bytes
    if args.log_backup_count is not None:
        options.log_backup_count = args.log_backup_count
    if args.heartbeat_path is not None:
        options.heartbeat_path = args.heartbeat_path
    if args.lock_path is not None:
        options.lock_path = args.lock_path
    if args.no_lock:
        options.lock_enabled = False
    if args.shutdown_grace_seconds is not None:
        options.shutdown_grace_seconds = args.shutdown_grace_seconds
    options.log_path = args.log_file
    options.run_seconds = args.run_seconds

    _configure_logging(options)
    daemon = Daemon(options)
    try:
        daemon.setup()
    except SingleInstanceError as exc:
        # 第二个实例：**明确报错退出**，不静默并存（两个实例会各推一遍卡片）。
        logger.error("无法启动：%s", exc)
        print(f"无法启动守护进程：{exc}", file=sys.stderr)
        return 3
    return daemon.run_forever()


if __name__ == "__main__":
    sys.exit(main())
