"""运行期配置面：环境变量 → 配置快照，支持热载入。

同构源参考 ``github_ref/RAGFlow/api/db/runtime_config.py`` 的
``init_config`` / ``init_env`` / ``get_env`` 语义：配置集中在单一对象上，
运行期可重新载入并整体切换，业务代码不直接读 ``os.environ``。

融合后的产品行为：

* 全部凭据位只从环境变量读，代码内不出现任何密钥默认值；
* ``reload()`` 重新采集环境变量并**原子切换**整份快照，判定 / 召回 / 推送
  三个环节在一次处理内持有同一份快照，避免半程切换；
* 快照对外只暴露脱敏视图（token 只给首 4 尾 4 与长度）。

配置深度（任务 id 20）：

* **必填项清单 + 启动自检**：每个配置项声明它服务于哪条通道、缺失时是否
  可用默认值继续；缺项或格式非法时启动即报错，并给出可执行的补齐提示；
* **配置快照落盘**：每次热载入产生一份快照（版本号 / 键集合 / 各键来源 /
  脱敏值 / 变更差异 / 生效时刻），写到 ``reports/config_snapshots/``；
* **环节可追溯**：判定 / 召回 / 推送 / 回写四个环节记录当时生效的配置版本号
  与规则快照 ID（见 ``critval/trace.py``）；
* **日志脱敏**：日志与快照里凭据只显示首 4 尾 4，完整密钥不落任何文本产物。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping, Optional
from urllib.parse import urlparse

__all__ = [
    "CONFIG_KEYS",
    "CONFIG_SNAPSHOT_DIR_KEY",
    "SECRET_KEYS",
    "Config",
    "ConfigCheck",
    "ConfigError",
    "ConfigIssue",
    "ConfigRequirement",
    "ConfigSnapshot",
    "REQUIREMENTS",
    "VALIDATORS",
    "install_log_redaction",
    "mask_secret",
    "redact_secrets",
]


class ConfigError(RuntimeError):
    """配置缺失或非法。"""


#: 配置键集。必须与仓库根 ``.env.example`` 逐条对应（tests/test_config.py 断言一致）。
CONFIG_KEYS: tuple[str, ...] = (
    "CRITVAL_WECOM_WEBHOOK_URL",
    "CRITVAL_BASE_URL",
    "CRITVAL_TOKEN",
    "NURSE_ROSTER_BASE_URL",
    "ROSTER_TOKEN",
    "CRITVAL_LISTEN_BASE_URL",
    "LIS_CRITICAL_TOKEN",
    "LIS_BASE_URL",
    "LIS_MLLP_BIND",
    "CONSOLE_TOKEN",
    "CRITVAL_DB_PATH",
    "CRITVAL_CONFIG_SNAPSHOT_DIR",
    "CRITVAL_THRESHOLD_PATH",
    "CRITVAL_INBOUND_MAX_BODY_BYTES",
    "CRITVAL_INBOUND_RATE_LIMIT_PER_MINUTE",
    "CRITVAL_MERGE_WINDOW_SECONDS",
    "CRITVAL_LOCAL_NOTIFICATION_PATH",
    "CRITVAL_NOTIFY_MAX_ATTEMPTS",
    "CRITVAL_NOTIFY_BACKOFF_BASE_SECONDS",
    "CRITVAL_NOTIFY_BACKOFF_FACTOR",
    "CRITVAL_NOTIFY_MAX_BACKOFF_SECONDS",
    "CRITVAL_NOTIFY_JITTER_RATIO",
    "CRITVAL_POLICY_PATH",
    "CRITVAL_FRAME_DIR",
    "CRITVAL_MATERIAL_DIR",
    "CRITVAL_SNAPSHOT_DIR",
    "CRITVAL_QUEUE_SIZE",
    "CRITVAL_WORKERS",
    "CRITVAL_HEARTBEAT_SECONDS",
    "CRITVAL_TIMER_INTERVAL_SECONDS",
    "CRITVAL_HEARTBEAT_PATH",
    "CRITVAL_LOCK_PATH",
    "CRITVAL_SHUTDOWN_GRACE_SECONDS",
    "CRITVAL_LOG_DIR",
    "CRITVAL_LOG_MAX_BYTES",
    "CRITVAL_LOG_BACKUP_COUNT",
)

#: 配置快照落盘目录的配置键（运行期本地路径，非凭据位）。
CONFIG_SNAPSHOT_DIR_KEY = "CRITVAL_CONFIG_SNAPSHOT_DIR"

#: 需要脱敏的凭据位（写日志 / 落快照时只显示首 4 尾 4）。
SECRET_KEYS: frozenset[str] = frozenset(
    {
        "CRITVAL_WECOM_WEBHOOK_URL",
        "CRITVAL_TOKEN",
        "ROSTER_TOKEN",
        "LIS_CRITICAL_TOKEN",
        "CONSOLE_TOKEN",
    }
)

#: 非凭据位的默认值。凭据位一律无默认值 —— 缺就是缺，不给兜底假值。
CONFIG_DEFAULTS: Mapping[str, str] = {
    "CRITVAL_DB_PATH": "./data/critval.db",
    CONFIG_SNAPSHOT_DIR_KEY: "./reports/config_snapshots",
    # 阈值表默认指向**示例材料**。院内上线必须在 .env 里改成本院危急值标准文件。
    "CRITVAL_THRESHOLD_PATH": "./samples/threshold/示例危急值阈值表.xlsx",
    "CRITVAL_INBOUND_MAX_BODY_BYTES": "65536",
    "CRITVAL_INBOUND_RATE_LIMIT_PER_MINUTE": "120",
    # 合并通知时间窗（秒）：同标本同科室的多个危急项目合成一张卡。
    "CRITVAL_MERGE_WINDOW_SECONDS": "300",
    # 本机通知队列文件（企微推不出去时的降级落点，运行期数据）。
    "CRITVAL_LOCAL_NOTIFICATION_PATH": "./data/local_notifications.jsonl",
    # 推送重试参数（任务 id 42）：指数退避 + 抖动。
    "CRITVAL_NOTIFY_MAX_ATTEMPTS": "3",
    "CRITVAL_NOTIFY_BACKOFF_BASE_SECONDS": "0.5",
    "CRITVAL_NOTIFY_BACKOFF_FACTOR": "2",
    "CRITVAL_NOTIFY_MAX_BACKOFF_SECONDS": "8",
    "CRITVAL_NOTIFY_JITTER_RATIO": "0.2",
    # 院内危急值报告制度（召回依据）。默认指向仓库内示例材料。
    "CRITVAL_POLICY_PATH": "./samples/policy/示例危急值报告制度.docx",
    # 守护进程的目录投递面：ER7 帧目录与材料目录（任务 id 13）。
    "CRITVAL_FRAME_DIR": "./data/inbound_frames",
    "CRITVAL_MATERIAL_DIR": "./data/materials",
    # 处置卡不可变快照目录（值班台复核读快照，不重新渲染）。
    "CRITVAL_SNAPSHOT_DIR": "./data/card_snapshots",
    # 有界队列容量与并发处理线程数（守护进程运行参数）。
    "CRITVAL_QUEUE_SIZE": "256",
    "CRITVAL_WORKERS": "2",
    # 心跳周期与定时任务周期（秒）。
    "CRITVAL_HEARTBEAT_SECONDS": "30",
    "CRITVAL_TIMER_INTERVAL_SECONDS": "60",
    # 运行保障（任务 id 46）：心跳文件、单实例锁、停机宽限期、日志轮转。
    "CRITVAL_HEARTBEAT_PATH": "./data/heartbeat.json",
    "CRITVAL_LOCK_PATH": "./data/critval.lock",
    "CRITVAL_SHUTDOWN_GRACE_SECONDS": "30",
    "CRITVAL_LOG_DIR": "./logs",
    "CRITVAL_LOG_MAX_BYTES": "5242880",
    "CRITVAL_LOG_BACKUP_COUNT": "5",
}

#: 已加载的完整凭据值（仅内存，用于日志脱敏替换）。
_SECRET_VALUES: dict[str, str] = {}


def mask_secret(value: str | None) -> str:
    """脱敏：只给首 4 尾 4 与长度；空值显示为 ``(unset)``。"""
    if not value:
        return "(unset)"
    if len(value) <= 8:
        return f"({len(value)} chars)"
    return f"{value[:4]}…{value[-4:]} ({len(value)} chars)"


def redact_secrets(text: str) -> str:
    """把文本中出现的**完整凭据**替换成脱敏形式。

    凭据只在内存里注册（``reload()`` 时登记），因此日志与快照文本产物里
    不会出现完整密钥。
    """
    if not text:
        return text
    out = text
    for value in _SECRET_VALUES.values():
        if value and len(value) >= 8:
            out = out.replace(value, mask_secret(value))
    return out


class SecretRedactingFilter(logging.Filter):
    """日志过滤器：把已登记的完整凭据替换成脱敏形式。

    **只脱敏字符串实参**：非字符串实参（行数、端口号等）保持原类型 ——
    一律转成字符串会让 ``%d`` / ``%f`` 这类格式在真正写日志时抛
    ``TypeError``，那一行日志就整条丢了（logging 只往 stderr 打一句
    「Logging error」，日志里反而看不到内容）。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact_secrets(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {
                    key: redact_secrets(value) if isinstance(value, str) else value
                    for key, value in record.args.items()
                }
            else:
                record.args = tuple(
                    redact_secrets(arg) if isinstance(arg, str) else arg
                    for arg in record.args
                )
        return True


def install_log_redaction(logger: Optional[logging.Logger] = None) -> logging.Logger:
    """给 logger（默认根 logger）挂上凭据脱敏过滤器。

    重复调用是幂等的 —— 同一个 logger 上只挂一个过滤器。
    """
    target = logger if logger is not None else logging.getLogger()
    for handler in list(target.handlers):
        if not any(isinstance(f, SecretRedactingFilter) for f in handler.filters):
            handler.addFilter(SecretRedactingFilter())
    if not any(isinstance(f, SecretRedactingFilter) for f in target.filters):
        target.addFilter(SecretRedactingFilter())
    return target


# ---------------------------------------------------------------------------
# 必填项清单与格式校验
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConfigRequirement:
    """一个配置项的「必填声明」。

    ``channel`` 写清它服务于哪条通道（出站 / 入站），自检报错时直接给
    运维看，不让对方猜这个环境变量是干什么的。

    ``group`` 把「同一件事的多个可选实现」归到一组（如同为 LIS 入站的
    HTTP 回调与 MLLP 监听）：组内至少配一个即可，全空才算启动失败。

    ``default_ok`` 表示缺失时是否**可以继续**（有兜底或由同组另一项承担）。
    """

    key: str
    channel: str
    kind: str = "required"  # required | conditional | optional
    blocking: bool = True
    default_ok: bool = False
    hint: str = ""
    group: str = ""


#: 必填项清单。顺序即自检报告里的展示顺序（先出站、后入站）。
REQUIREMENTS: tuple[ConfigRequirement, ...] = (
    ConfigRequirement(
        key="CRITVAL_WECOM_WEBHOOK_URL",
        channel="出站：企业微信群机器人推送处置卡",
        hint="群机器人 webhook 完整地址（形如 https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=…）",
    ),
    ConfigRequirement(
        key="CRITVAL_BASE_URL",
        channel="出站：危急值登记系统回写回执",
        hint="登记系统根地址（形如 http://10.0.0.8:8080）",
    ),
    ConfigRequirement(
        key="CRITVAL_TOKEN",
        channel="出站：危急值登记系统回写回执",
        hint="回写接口的 Bearer 凭据，至少 12 位",
    ),
    ConfigRequirement(
        key="NURSE_ROSTER_BASE_URL",
        channel="出站：排班系统读取当班人",
        hint="排班系统根地址（形如 http://10.0.0.9:9000）",
    ),
    ConfigRequirement(
        key="ROSTER_TOKEN",
        channel="出站：排班系统读取当班人",
        hint="排班系统接口的 Bearer 凭据，至少 12 位",
    ),
    ConfigRequirement(
        key="CRITVAL_LISTEN_BASE_URL",
        channel="入站：LIS 以 HTTP 推送危急值",
        hint="本守护进程对外监听地址（形如 http://0.0.0.0:8760）",
    ),
    ConfigRequirement(
        key="LIS_CRITICAL_TOKEN",
        channel="入站：LIS 推送危急值时校验 Bearer 凭据",
        hint="与 LIS 约定的共享 Bearer 凭据，至少 12 位",
    ),
    ConfigRequirement(
        key="CONSOLE_TOKEN",
        channel="入站：值班台访问鉴权",
        hint="值班台页面与确认接口的访问令牌，至少 12 位",
    ),
    ConfigRequirement(
        key="LIS_BASE_URL",
        channel="入站：LIS REST 回调",
        kind="conditional",
        blocking=False,
        default_ok=True,
        hint="院内 LIS 以 REST 回调本系统时填写（与 MLLP 至少配一条）",
        group="lis_inbound",
    ),
    ConfigRequirement(
        key="LIS_MLLP_BIND",
        channel="入站：HL7 v2 MLLP 监听",
        kind="conditional",
        blocking=False,
        default_ok=True,
        hint="MLLP 监听地址（形如 0.0.0.0:2575），与 REST 回调至少配一条",
        group="lis_inbound",
    ),
    ConfigRequirement(
        key="CRITVAL_DB_PATH",
        channel="本地：台账 / 审计 / 幂等键库文件",
        kind="optional",
        blocking=False,
        default_ok=True,
        hint="SQLite 库文件路径，留空用默认 ./data/critval.db",
    ),
)


def _validate_url(value: str) -> Optional[str]:
    if not value.startswith(("http://", "https://")):
        return "必须是 http:// 或 https:// 开头的完整地址"
    if not urlparse(value).netloc:
        return "地址里没有主机名"
    return None


def _validate_token(value: str) -> Optional[str]:
    if len(value) < 12:
        return f"长度只有 {len(value)} 位，低于 12 位下限（过短的多半是占位值）"
    if re.search(r"\s", value):
        return "不能包含空白字符"
    return None


def _validate_bind(value: str) -> Optional[str]:
    host, sep, port = value.rpartition(":")
    if not sep or not host:
        return "应为 主机:端口 形式（形如 0.0.0.0:2575）"
    if not port.isdigit():
        return f"端口 {port!r} 不是数字"
    if not 1 <= int(port) <= 65535:
        return f"端口 {port} 超出 1-65535"
    return None


#: 各配置项的格式校验器；返回 ``None`` 表示通过，否则返回错误原因。
VALIDATORS: Mapping[str, Callable[[str], Optional[str]]] = {
    "CRITVAL_WECOM_WEBHOOK_URL": _validate_url,
    "CRITVAL_BASE_URL": _validate_url,
    "CRITVAL_LISTEN_BASE_URL": _validate_url,
    "NURSE_ROSTER_BASE_URL": _validate_url,
    "LIS_BASE_URL": _validate_url,
    "CRITVAL_TOKEN": _validate_token,
    "ROSTER_TOKEN": _validate_token,
    "LIS_CRITICAL_TOKEN": _validate_token,
    "CONSOLE_TOKEN": _validate_token,
    "LIS_MLLP_BIND": _validate_bind,
}


@dataclass(frozen=True)
class ConfigIssue:
    """一条自检问题：缺项 / 格式非法 / 通道组全空。"""

    key: str
    kind: str  # missing | invalid_format | group_empty
    channel: str
    message: str
    hint: str = ""
    blocking: bool = True
    default_ok: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "key": self.key,
            "kind": self.kind,
            "channel": self.channel,
            "message": self.message,
            "hint": self.hint,
            "blocking": self.blocking,
            "default_ok": self.default_ok,
        }


@dataclass(frozen=True)
class ConfigCheck:
    """一次启动自检的结论。"""

    ok: bool
    version: int
    fingerprint: str
    loaded_at: str
    issues: tuple[ConfigIssue, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def blocking(self) -> bool:
        """是否有阻断性问题（缺必填 / 格式非法 / 通道组全空）。"""
        return any(issue.blocking for issue in self.issues)

    @property
    def missing(self) -> tuple[ConfigIssue, ...]:
        return tuple(i for i in self.issues if i.kind == "missing")

    @property
    def invalid(self) -> tuple[ConfigIssue, ...]:
        return tuple(i for i in self.issues if i.kind == "invalid_format")

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "blocking": self.blocking,
            "config_version": self.version,
            "config_fingerprint": self.fingerprint,
            "loaded_at": self.loaded_at,
            "issues": [issue.to_dict() for issue in self.issues],
            "notes": list(self.notes),
        }

    def report_text(self) -> str:
        """人读的自检报告（CLI 与启动报错共用）。"""
        lines = [
            f"配置版本号  : {self.version}",
            f"配置指纹    : {self.fingerprint[:16]}…",
            f"生效时刻    : {self.loaded_at}",
            f"自检结论    : {'通过' if not self.blocking else '未通过（存在阻断项）'}",
        ]
        if self.issues:
            lines.append("──────────────────────────────")
            for issue in self.issues:
                lines.append(f"[{issue.kind}] {issue.key}")
                lines.append(f"  服务通道   : {issue.channel}")
                lines.append(f"  问题       : {issue.message}")
                if issue.hint:
                    lines.append(f"  补齐提示   : {issue.hint}")
                lines.append(
                    "  是否可继续 : " + ("是（可用默认值 / 同组另一通道）" if issue.default_ok else "否（缺失即该通道不可用）")
                )
        if self.notes:
            lines.append("──────────────────────────────")
            lines.extend(f"提示：{note}" for note in self.notes)
        return "\n".join(lines)


@dataclass(frozen=True)
class ConfigSnapshot:
    """一份不可变配置快照。

    ``version`` 随每次热载入递增；``fingerprint`` 是键值对的 sha256，
    供事件落库时记录「这条判定用的是哪份配置」。

    ``changes`` 记录相对上一份快照的差异（``added`` / ``removed`` /
    ``changed`` / ``source_changed``），让运维一眼看出这次热载入改了什么。
    """

    version: int
    values: Mapping[str, str]
    sources: Mapping[str, str]
    fingerprint: str
    loaded_at: str
    changes: Mapping[str, str] = field(default_factory=dict)

    def get(self, key: str) -> str | None:
        return self.values.get(key)

    def masked_values(self) -> dict[str, str]:
        """脱敏视图：凭据位只给首 4 尾 4 与长度；未设置的键显示 ``(unset)``。"""
        out: dict[str, str] = {}
        for key in CONFIG_KEYS:
            value = self.values.get(key)
            if value is None:
                out[key] = "(unset)"
            elif key in SECRET_KEYS:
                out[key] = mask_secret(value)
            else:
                out[key] = value
        return out

    @property
    def source_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for source in self.sources.values():
            counts[source] = counts.get(source, 0) + 1
        return counts

    def to_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "fingerprint": self.fingerprint,
            "loaded_at": self.loaded_at,
            "sources": dict(self.sources),
            "source_counts": self.source_counts,
            "changes": dict(self.changes),
            "masked_values": self.masked_values(),
        }


def _fingerprint(values: Mapping[str, str]) -> str:
    digest = hashlib.sha256()
    for key in sorted(values):
        digest.update(key.encode("utf-8"))
        digest.update(b"\x00")
        digest.update((values.get(key) or "").encode("utf-8"))
        digest.update(b"\x01")
    return digest.hexdigest()


def _diff(
    previous: "ConfigSnapshot", values: Mapping[str, str], sources: Mapping[str, str]
) -> dict[str, str]:
    """相对上一份快照的变更差异。"""
    changes: dict[str, str] = {}
    for key in CONFIG_KEYS:
        old = previous.values.get(key)
        new = values.get(key)
        old_source = previous.sources.get(key, "unset")
        new_source = sources.get(key, "unset")
        if old is None and new is not None:
            changes[key] = "added"
        elif old is not None and new is None:
            changes[key] = "removed"
        elif old != new:
            changes[key] = "changed"
        elif old_source != new_source:
            changes[key] = "source_changed"
    return changes


class Config:
    """全局配置对象（类方法风格，同构源 ``RuntimeConfig``）。"""

    _lock = threading.RLock()
    _overrides: dict[str, str] = {}
    #: 热载入后是否自动把快照落盘（守护进程常驻时开启）。
    persist_on_reload: bool = False
    _snapshot: ConfigSnapshot = ConfigSnapshot(
        version=0,
        values={},
        sources={},
        fingerprint=_fingerprint({}),
        loaded_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )

    # ---- 源 RuntimeConfig 同构入口 -------------------------------------
    @classmethod
    def init_config(cls, **kwargs: str) -> None:
        """注入显式覆盖值（运维命令行 / 测试用），随后重载快照。"""
        with cls._lock:
            cls._overrides.update({k: v for k, v in kwargs.items() if v is not None})
        cls.reload()

    @classmethod
    def init_env(cls) -> ConfigSnapshot:
        """从环境变量采集一份快照并切换（源 ``init_env`` 语义）。"""
        return cls.reload()

    @classmethod
    def get_env(cls, key: str) -> str | None:
        """读一个配置项；未设置返回 ``None``（源 ``get_env`` 语义）。"""
        return cls._snapshot.values.get(key)

    @classmethod
    def get_all_env(cls) -> dict[str, str]:
        """读全部配置项（源 ``get_all_env`` 语义）。"""
        return dict(cls._snapshot.values)

    # ---- 本产品新增：热载入与快照 ---------------------------------------
    @classmethod
    def reload(cls) -> ConfigSnapshot:
        """重新采集环境变量 + 覆盖值，**原子切换**到新快照。

        切换在锁内完成，读取方要么看到旧快照、要么看到新快照，不会看到半份。
        """
        with cls._lock:
            values: dict[str, str] = {}
            sources: dict[str, str] = {}
            for key in CONFIG_KEYS:
                override = cls._overrides.get(key)
                if override is not None:
                    values[key] = override
                    sources[key] = "override"
                    continue
                env_value = os.environ.get(key)
                if env_value is not None and env_value != "":
                    values[key] = env_value
                    sources[key] = "env"
                    continue
                default = CONFIG_DEFAULTS.get(key)
                if default is not None:
                    values[key] = default
                    sources[key] = "default"
                    continue
                # 未设置的键**不进 values**：缺就是缺，不给兜底假值
                sources[key] = "unset"
            previous = cls._snapshot
            snapshot = ConfigSnapshot(
                version=previous.version + 1,
                values=values,
                sources=sources,
                fingerprint=_fingerprint(values),
                loaded_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                changes=_diff(previous, values, sources),
            )
            cls._snapshot = snapshot
            cls._register_secrets(values)
            if cls.persist_on_reload:
                cls.persist_snapshot(snapshot)
            return snapshot

    @classmethod
    def _register_secrets(cls, values: Mapping[str, str]) -> None:
        """登记当前生效的完整凭据，供日志脱敏替换。"""
        with cls._lock:
            for key in SECRET_KEYS:
                value = values.get(key)
                if value:
                    _SECRET_VALUES[key] = value
                else:
                    _SECRET_VALUES.pop(key, None)

    @classmethod
    def snapshot(cls) -> ConfigSnapshot:
        """当前生效快照。"""
        return cls._snapshot

    @classmethod
    def config_version(cls) -> int:
        """当前配置版本号（事件落库时记录用）。"""
        return cls._snapshot.version

    @classmethod
    def require(cls, key: str) -> str:
        """取必填配置；缺失即报错并说明服务哪条通道。"""
        value = cls.get_env(key)
        if not value:
            raise ConfigError(f"缺少必填配置 {key}（该配置服务于对应出站/入站通道）")
        return value

    @classmethod
    def is_configured(cls, key: str) -> bool:
        return bool(cls.get_env(key))

    # ---- 启动自检 -------------------------------------------------------
    @classmethod
    def self_check(cls) -> ConfigCheck:
        """检查必填项与格式，返回结构化结论。

        逐条给出：缺哪个环境变量、它服务于哪条通道、是否能用默认值继续。
        """
        snap = cls._snapshot
        issues: list[ConfigIssue] = []
        notes: list[str] = []

        groups: dict[str, list[ConfigRequirement]] = {}
        for req in REQUIREMENTS:
            if req.group:
                groups.setdefault(req.group, []).append(req)

        for req in REQUIREMENTS:
            if req.group:
                continue  # 组内项由组检查统一处理
            value = snap.values.get(req.key)
            if value is None:
                issues.append(
                    ConfigIssue(
                        key=req.key,
                        kind="missing",
                        channel=req.channel,
                        message=f"缺少必填配置 {req.key}",
                        hint=req.hint,
                        blocking=req.blocking,
                        default_ok=req.default_ok,
                    )
                )

        for group, members in groups.items():
            configured = [m for m in members if snap.values.get(m.key)]
            if not configured:
                issues.append(
                    ConfigIssue(
                        key=group,
                        kind="group_empty",
                        channel=members[0].channel,
                        message=(
                            f"入站通道组「{group}」全空："
                            + "、".join(m.key for m in members)
                            + " 至少要配一条，否则 LIS 推不出危急值"
                        ),
                        hint="；".join(m.hint for m in members),
                        blocking=True,
                        default_ok=False,
                    )
                )
            else:
                for member in members:
                    if not snap.values.get(member.key):
                        notes.append(
                            f"{member.key} 未设置（本组入站通道由 "
                            + "、".join(m.key for m in configured)
                            + " 承担，可继续）"
                        )

        for req in REQUIREMENTS:
            value = snap.values.get(req.key)
            if not value:
                continue
            validator = VALIDATORS.get(req.key)
            if validator is None:
                continue
            problem = validator(value)
            if problem:
                issues.append(
                    ConfigIssue(
                        key=req.key,
                        kind="invalid_format",
                        channel=req.channel,
                        message=f"{req.key} 格式非法：{problem}",
                        hint=req.hint,
                        blocking=True,
                        default_ok=False,
                    )
                )

        return ConfigCheck(
            ok=not any(i.blocking for i in issues),
            version=snap.version,
            fingerprint=snap.fingerprint,
            loaded_at=snap.loaded_at,
            issues=tuple(issues),
            notes=tuple(notes),
        )

    @classmethod
    def require_ready(cls) -> ConfigCheck:
        """启动自检：有阻断项即抛错（缺失必填 / 格式非法 / 通道组全空）。"""
        check = cls.self_check()
        if check.blocking:
            raise ConfigError("启动自检未通过：\n" + check.report_text())
        return check

    # ---- 快照落盘 -------------------------------------------------------
    @classmethod
    def snapshot_dir(cls, directory: Optional[Path | str] = None) -> Path:
        """快照落盘目录（默认取 ``CRITVAL_CONFIG_SNAPSHOT_DIR``）。"""
        if directory is not None:
            return Path(directory)
        return Path(cls.get_env(CONFIG_SNAPSHOT_DIR_KEY) or CONFIG_DEFAULTS[CONFIG_SNAPSHOT_DIR_KEY])

    @classmethod
    def persist_snapshot(
        cls, snapshot: Optional[ConfigSnapshot] = None, directory: Optional[Path | str] = None
    ) -> Path:
        """把快照写到 ``reports/config_snapshots/``（只写脱敏视图）。

        同时追加一行到 ``history.jsonl``，便于按时序回看配置变更。
        """
        snap = snapshot or cls._snapshot
        base = cls.snapshot_dir(directory)
        base.mkdir(parents=True, exist_ok=True)
        # 文件名带上指纹：同版本同内容 → 同一文件（幂等），不同内容 → 不互相覆盖。
        path = base / f"config_snapshot_v{snap.version}-{snap.fingerprint[:8]}.json"
        payload = snap.to_dict()
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
        )
        history = base / "history.jsonl"
        with history.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
        return path
