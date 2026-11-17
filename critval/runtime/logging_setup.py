"""日志轮转（守护进程运行保障，任务 id 46）。

**为什么按大小滚动**：值班机上的守护进程可能连跑几周不重启。日志只追加不
滚动，早晚把磁盘吃满 —— 磁盘满了，台账写不进去、快照存不下，危急值直接
进不来。所以日志必须**按大小滚动**。

**旧日志留档不删**：滚出来的 ``critval-daemon.log.1`` 之类**不自动清理** ——
事后复核「那条卡当时推了什么」要翻日志。备份份数由
``CRITVAL_LOG_BACKUP_COUNT`` 控制（默认留 5 份），到量后最旧的被覆盖。

**凭据照样脱敏**：轮转不改变脱敏口径 —— 完整密钥依然只显示首 4 尾 4
（由 :class:`critval.config.SecretRedactingFilter` 就地替换），写进轮转文件
的每一份日志都带这个过滤器。
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

from critval.config import install_log_redaction

__all__ = [
    "DEFAULT_BACKUP_COUNT",
    "DEFAULT_LOG_FILENAME",
    "DEFAULT_MAX_BYTES",
    "configure_runtime_logging",
]

#: 默认日志文件名（落在日志目录下）。
DEFAULT_LOG_FILENAME = "critval-daemon.log"

#: 单个日志文件上限：5 MiB。
DEFAULT_MAX_BYTES = 5 * 1024 * 1024

#: 保留的历史份数。
DEFAULT_BACKUP_COUNT = 5

#: 日志行格式。
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


def resolve_log_path(log_path: str = "", log_dir: str = "") -> Optional[Path]:
    """算出日志落盘路径。

    优先级：显式 ``log_path`` > ``log_dir`` + 默认文件名 > 不落盘（只进 stderr）。
    """
    explicit = str(log_path or "").strip()
    if explicit:
        return Path(explicit)
    directory = str(log_dir or "").strip()
    if directory:
        return Path(directory) / DEFAULT_LOG_FILENAME
    return None


def configure_runtime_logging(
    log_path: str = "",
    *,
    log_dir: str = "",
    max_bytes: int = DEFAULT_MAX_BYTES,
    backup_count: int = DEFAULT_BACKUP_COUNT,
    console: bool = True,
    level: int = logging.INFO,
) -> list[logging.Handler]:
    """装配日志（stderr + 按大小滚动的文件）。

    返回装配好的 handler 列表（便于测试断言轮转参数与脱敏过滤器）。
    """
    handlers: list[logging.Handler] = []
    if console:
        handlers.append(logging.StreamHandler(sys.stderr))
    target = resolve_log_path(log_path, log_dir)
    if target is not None:
        target.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(
            RotatingFileHandler(
                str(target),
                maxBytes=max(1024, int(max_bytes)),
                backupCount=max(0, int(backup_count)),
                encoding="utf-8",
            )
        )
    logging.basicConfig(level=level, format=LOG_FORMAT, handlers=handlers, force=True)
    # 脱敏过滤器必须**在 basicConfig 之后**挂 —— 否则挂到的是被 force 换掉的旧 handler。
    install_log_redaction()
    return handlers
