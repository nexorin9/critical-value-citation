"""心跳与健康摘要（守护进程运行保障，任务 id 46）。

**心跳回答的是一句话**：这台值班机上的进程还活着吗。

值班机大多没人盯着 —— 进程假死、端口还在通、但事件不处理了，这种**静默
故障**最难查。所以心跳不是「打一行日志」，而是**写一份可被外部读取的状态
文件**：版本、队列深度、处理计数、最近错误时刻。谁都能 ``cat`` 一下就知道
它是不是还在动。

**心跳原子写**：先写临时文件再 ``os.replace`` 改名。读心跳的人永远读到
完整的一份，不会读到写了一半的 JSON（值班机上的监控脚本可能每 5 秒读一次）。

**健康检查**（``GET /healthz``）比心跳多两件事：

* **依赖可达性** —— 排班系统 / 登记系统 / 企微通道这三个出站依赖**通不通**。
  通不通用**短超时 TCP 探活**判断（只验「网络能到」，不验业务），超时就标
  不可达而不是把健康检查自己卡住。
* **心跳新鲜度** —— 心跳文件多久没更新了。进程在但心跳停 = 内部线程死了，
  这比端口不通更隐蔽。

健康检查**只回答「流水线还在不在动」，不判断「业务对不对」** —— 判定对不对
是台账与值班台的事。
"""

from __future__ import annotations

import json
import os
import socket
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import urlsplit

__all__ = [
    "DEPENDENCY_REGISTRY",
    "DEPENDENCY_ROSTER",
    "DEPENDENCY_WECOM",
    "HEARTBEAT_VERSION",
    "DependencySpec",
    "DependencyStatus",
    "HeartbeatState",
    "HeartbeatWriter",
    "build_health_payload",
    "probe_dependencies",
]

#: 心跳格式版本。格式变了就改这里 —— 读心跳的脚本据此判断能不能解析。
HEARTBEAT_VERSION = "0.1"

#: 依赖名（固定取值，便于监控脚本按名字取）。
DEPENDENCY_ROSTER = "roster"
DEPENDENCY_REGISTRY = "registry"
DEPENDENCY_WECOM = "wecom"


# ---------------------------------------------------------------------------
# 心跳状态
# ---------------------------------------------------------------------------


@dataclass
class HeartbeatState:
    """一份心跳。"""

    version: str = HEARTBEAT_VERSION
    pid: int = 0
    started_at: str = ""
    updated_at: str = ""
    uptime_seconds: float = 0.0
    queue_depth: int = 0
    queue_capacity: int = 0
    counters: dict[str, int] = field(default_factory=dict)
    last_error_at: str = ""
    last_error: str = ""
    status: str = "ok"
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "pid": self.pid,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "uptime_seconds": round(float(self.uptime_seconds), 3),
            "queue_depth": int(self.queue_depth),
            "queue_capacity": int(self.queue_capacity),
            "counters": dict(self.counters),
            "last_error_at": self.last_error_at,
            "last_error": self.last_error,
            "status": self.status,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "HeartbeatState":
        counters = raw.get("counters")
        return cls(
            version=str(raw.get("version", "") or HEARTBEAT_VERSION),
            pid=int(raw.get("pid", 0) or 0),
            started_at=str(raw.get("started_at", "") or ""),
            updated_at=str(raw.get("updated_at", "") or ""),
            uptime_seconds=float(raw.get("uptime_seconds", 0) or 0),
            queue_depth=int(raw.get("queue_depth", 0) or 0),
            queue_capacity=int(raw.get("queue_capacity", 0) or 0),
            counters=dict(counters) if isinstance(counters, dict) else {},
            last_error_at=str(raw.get("last_error_at", "") or ""),
            last_error=str(raw.get("last_error", "") or ""),
            status=str(raw.get("status", "") or "ok"),
            note=str(raw.get("note", "") or ""),
        )


class HeartbeatWriter:
    """心跳文件写入器（原子写 + 可回读）。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def write(self, state: HeartbeatState) -> Path:
        """写一份心跳（原子替换）。返回落盘路径。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(state.to_dict(), ensure_ascii=False, indent=2) + "\n"
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(payload, encoding="utf-8")
        os.replace(str(tmp), str(self.path))
        return self.path

    def read(self) -> Optional[HeartbeatState]:
        """回读最近一份心跳；读不到（没写过 / 坏了）返回 None。"""
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(raw, dict):
            return None
        return HeartbeatState.from_dict(raw)

    def age_seconds(self) -> Optional[float]:
        """心跳文件距上次更新过了多少秒；读不到返回 None。"""
        state = self.read()
        if state is None or not state.updated_at:
            return None
        try:
            moment = datetime.fromisoformat(state.updated_at)
        except ValueError:
            return None
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - moment).total_seconds())


# ---------------------------------------------------------------------------
# 依赖可达性
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DependencySpec:
    """一个出站依赖的探活目标。"""

    name: str
    base_url: str
    required: bool = False


@dataclass
class DependencyStatus:
    """一个出站依赖的可达性结论。"""

    name: str
    configured: bool
    reachable: bool
    endpoint: str = ""
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "configured": self.configured,
            "reachable": self.reachable,
            "endpoint": self.endpoint,
            "detail": self.detail,
        }


def _host_port(base_url: str) -> tuple[str, int]:
    """从基地址取主机与端口（默认端口按 scheme 猜）。"""
    raw = str(base_url or "").strip()
    if not raw:
        return "", 0
    split = urlsplit(raw if "://" in raw else f"http://{raw}")
    host = split.hostname or ""
    port = split.port or (443 if split.scheme == "https" else 80)
    return host, int(port)


def _tcp_probe(host: str, port: int, timeout: float) -> bool:
    """短超时 TCP 探活：只验「网络能到」，不验业务。"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def probe_dependencies(
    specs: Iterable[DependencySpec], *, timeout: float = 0.3
) -> list[DependencyStatus]:
    """探一批出站依赖的可达性。

    **没配就是没配**：未配置的依赖报 ``configured=False``，不算故障 ——
    没配企微 webhook 时处置卡落本机队列，这是**设计内的降级**，不是异常。
    """
    results: list[DependencyStatus] = []
    for spec in specs:
        configured = bool(str(spec.base_url or "").strip())
        if not configured:
            results.append(
                DependencyStatus(
                    name=spec.name,
                    configured=False,
                    reachable=False,
                    detail="未配置（按设计降级，不算故障）",
                )
            )
            continue
        host, port = _host_port(spec.base_url)
        if not host:
            results.append(
                DependencyStatus(
                    name=spec.name,
                    configured=True,
                    reachable=False,
                    endpoint=str(spec.base_url),
                    detail="地址里解析不出主机名",
                )
            )
            continue
        reachable = _tcp_probe(host, port, timeout)
        results.append(
            DependencyStatus(
                name=spec.name,
                configured=True,
                reachable=reachable,
                endpoint=f"{host}:{port}",
                detail="可达" if reachable else f"连不上（{host}:{port}）",
            )
        )
    return results


# ---------------------------------------------------------------------------
# 健康摘要
# ---------------------------------------------------------------------------


def build_health_payload(
    *,
    status: str = "ok",
    version: str = HEARTBEAT_VERSION,
    pid: int = 0,
    uptime_seconds: float = 0.0,
    queue_depth: int = 0,
    queue_capacity: int = 0,
    counters: Optional[dict[str, int]] = None,
    status_counts: Optional[dict[str, int]] = None,
    dependencies: Optional[Iterable[DependencyStatus]] = None,
    heartbeat: Optional[dict[str, Any]] = None,
    last_error_at: str = "",
    last_error: str = "",
    extra: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """装配健康摘要（``/healthz`` 的响应体）。"""
    payload: dict[str, Any] = {
        "status": status,
        "version": version,
        "pid": pid,
        "uptime_seconds": round(float(uptime_seconds), 3),
        # 队列深度同时给平铺与嵌套两种写法：平铺给脚本直接取，嵌套给页面读。
        "queue_depth": int(queue_depth),
        "queue_capacity": int(queue_capacity),
        "queue": {"depth": int(queue_depth), "capacity": int(queue_capacity)},
        "counters": dict(counters or {}),
        "status_counts": dict(status_counts or {}),
        "dependencies": {
            item.name: item.to_dict() for item in (dependencies or [])
        },
        "heartbeat": dict(heartbeat or {}),
        "last_error_at": last_error_at,
        "last_error": last_error,
    }
    if extra:
        payload.update(extra)
    return payload
