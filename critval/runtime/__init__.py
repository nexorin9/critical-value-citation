"""运行保障：心跳、健康检查、单实例锁、日志轮转（任务 id 46）。

值班机上的这个进程要**连续跑一整周**不出静默故障。静默故障是最难查的
一类故障：进程还在、端口还通，但已经不处理事件了 —— 值班群里没有卡片，
台账里没有记录，谁也不知道。

因此运行期必须有四件**能被人核对**的东西：

1. **心跳**（``heartbeat.py``）—— 每周期写一份心跳文件（版本 / 队列深度 /
   处理计数 / 最近错误时刻）。进程活着，心跳就在更新；心跳停了，就是出事了。
   心跳**原子写**：读的人永远读到完整一份，不会读到写了一半的 JSON。
2. **健康检查**（``heartbeat.py``）—— ``GET /healthz`` 返回状态 + 关键计数 +
   **依赖可达性**（排班系统 / 登记系统 / 企微通道通不通）。健康检查不判断
   「业务对不对」，只判断「这条流水线还在不在动」。
3. **单实例锁**（``lock.py``）—— 同一个数据目录只允许一个守护进程。两个实例
   同时推送 = 值班群收到两张一样的卡 = 值班人不知道该按哪一张 —— 比挂掉更糟。
   第二个实例**明确报错退出**，不静默并存。
4. **日志轮转**（``logging_setup.py``）—— 值班机不重启，日志会涨到把磁盘吃满。
   按大小滚动，旧日志留档不删（事后复核要查）。

这四件事都是**运行期保障**，不改变业务判定口径 —— 危急值怎么判、卡片怎么写，
仍由阈值表与院内制度说了算。

**配置面同构源参考** ``github_ref/RAGFlow/api/db/runtime_config.py`` 的
``init_config`` / ``init_env`` / ``get_env`` 语义：运行期参数（心跳路径、
锁路径、日志目录、停机宽限期）全部从环境变量读，不硬编码在代码里；配置在
运行期整体切换，业务代码不直接读进程状态。
"""

from __future__ import annotations

from critval.runtime.heartbeat import (
    DEPENDENCY_ROSTER,
    DEPENDENCY_REGISTRY,
    DEPENDENCY_WECOM,
    HEARTBEAT_VERSION,
    DependencySpec,
    DependencyStatus,
    HeartbeatState,
    HeartbeatWriter,
    build_health_payload,
    probe_dependencies,
)
from critval.runtime.lock import SingleInstanceError, SingleInstanceLock
from critval.runtime.logging_setup import (
    DEFAULT_BACKUP_COUNT,
    DEFAULT_LOG_FILENAME,
    DEFAULT_MAX_BYTES,
    configure_runtime_logging,
)

__all__ = [
    "DEFAULT_BACKUP_COUNT",
    "DEFAULT_LOG_FILENAME",
    "DEFAULT_MAX_BYTES",
    "DEPENDENCY_REGISTRY",
    "DEPENDENCY_ROSTER",
    "DEPENDENCY_WECOM",
    "HEARTBEAT_VERSION",
    "DependencySpec",
    "DependencyStatus",
    "HeartbeatState",
    "HeartbeatWriter",
    "SingleInstanceError",
    "SingleInstanceLock",
    "build_health_payload",
    "configure_runtime_logging",
    "probe_dependencies",
]
