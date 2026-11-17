"""单实例锁（守护进程运行保障，任务 id 46）。

**为什么要有**：值班机上如果不小心起了两个守护进程（比如 systemd 配了一条、
同事又手动跑了一条），两套进程会读同一个台账、各推各的 —— 值班群收到两张
一样的处置卡，值班人不知道该按哪一张；更糟的是两边都往登记系统回写回执。
**两个实例同时跑比一个实例挂掉更危险**，所以第二个实例必须**明确报错退出**，
而不是「谁抢到算谁的」。

**怎么实现的**：在数据目录里放一个锁文件，用 ``flock`` 排他锁（非阻塞）。
拿到锁的进程把「自己的 pid / 主机 / 启动时刻」写进锁文件 —— 后来者抢锁失败
时，把这段信息原样报给运维，一眼看出该去停哪个进程。

**锁的生命周期**跟着进程走：进程正常停机时释放锁（``stop()`` 里做），
进程被 ``kill -9`` 时由操作系统在文件描述符关闭时自动释放 —— 不会出现
「进程没了但锁还在，启不起来了」的死锁。
"""

from __future__ import annotations

import errno
import json
import os
import socket
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

try:  # pragma: no cover —— Windows 没有 fcntl，走不到这条分支
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

__all__ = [
    "SingleInstanceError",
    "SingleInstanceLock",
]


class SingleInstanceError(RuntimeError):
    """已有另一个守护进程占着这个数据目录。"""


class SingleInstanceLock:
    """文件锁：同一个数据目录只允许一个守护进程。

    用法::

        lock = SingleInstanceLock("./data/critval.lock")
        lock.acquire()      # 抢不到就抛 SingleInstanceError
        ...
        lock.release()
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._fd: Optional[int] = None

    # ---- 状态 -------------------------------------------------------------
    @property
    def held(self) -> bool:
        """本对象当前是否持有锁。"""
        return self._fd is not None

    def read_holder(self) -> dict[str, Any]:
        """读锁文件里的持有者信息（pid / 主机 / 启动时刻）。"""
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return raw if isinstance(raw, dict) else {}

    # ---- 加解锁 -----------------------------------------------------------
    def acquire(self) -> "SingleInstanceLock":
        """抢锁。抢不到就抛 :class:`SingleInstanceError`（消息里说清该停谁）。"""
        if self.held:
            return self
        if fcntl is None:  # pragma: no cover —— 非 POSIX 平台
            raise SingleInstanceError(
                "当前平台没有文件锁能力（fcntl 不可用），无法保证同一数据目录只有一个守护进程。"
            )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            holder = self.read_holder()
            os.close(fd)
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                raise SingleInstanceError(self._conflict_message(holder)) from exc
            raise
        self._fd = fd
        self._write_holder()
        return self

    def release(self) -> None:
        """放锁（幂等）。"""
        if self._fd is None:
            return
        try:
            if fcntl is not None:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(self._fd)
        except OSError:
            pass
        self._fd = None

    # ---- 内部 -------------------------------------------------------------
    def _write_holder(self) -> None:
        """把持有者信息写进锁文件（给后来者看）。"""
        payload = json.dumps(
            {
                "pid": os.getpid(),
                "host": socket.gethostname(),
                "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "lock_file": str(self.path),
            },
            ensure_ascii=False,
            indent=2,
        )
        try:
            os.lseek(self._fd, 0, os.SEEK_SET)
            os.ftruncate(self._fd, 0)
            os.write(self._fd, (payload + "\n").encode("utf-8"))
            os.fsync(self._fd)
        except OSError:
            pass

    def _conflict_message(self, holder: dict[str, Any]) -> str:
        """抢锁失败时给运维看的话 —— 说清「谁占着」与「怎么办」。"""
        pid = holder.get("pid", "未知")
        host = holder.get("host", "未知")
        started = holder.get("started_at", "未知")
        return (
            f"另一个守护进程已经占着这个数据目录（锁文件 {self.path}）："
            f"进程 {pid}（主机 {host}，启动于 {started}）。"
            f"两个实例同时跑会把同一张处置卡推两遍，值班人不知道该按哪一张 —— "
            f"请先停掉旧实例，或给新实例换一个数据目录。"
        )

    # ---- 上下文管理器 -----------------------------------------------------
    def __enter__(self) -> "SingleInstanceLock":
        return self.acquire()

    def __exit__(self, *exc_info: object) -> None:
        self.release()
