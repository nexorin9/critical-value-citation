"""规则集注册表：材料指纹变更 → 重新编译 → 原子切换 → 快照 ID 递增。

同构源参考 ``github_ref/RAGFlow/api/db/runtime_config.py`` 的
``init_config`` / ``init_env`` / ``get_env`` 语义（配置集中在单一对象上、
运行期可重载并整体切换、业务代码不直接读进程状态），以及
``github_ref/RAGFlow/agent/component/__init__.py`` 的
``component_class(name)`` 组件化装载（按名字取实现）。

融合后的产品行为：

* 规则集**只有一份**，挂在注册表上；判定方拿到的是**快照**而不是活对象，
  一次处理内不会看到半程切换；
* 材料文件指纹（sha256）没变就不重编译、不换快照 ID —— 避免同一份材料
  被反复重载冲淡版本号；
* 换了材料就换快照 ID，事件落库记录该 ID，事后复核可定位到当时的规则版本。
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Optional

from critval.judge.ruleset import RuleSet
from critval.kb.loader import load_threshold_table
from critval.kb.version import sha256_file

__all__ = ["RuleRegistry"]


class RuleRegistry:
    """规则集注册表（类级单例，类方法风格，同构 ``RuntimeConfig``）。"""

    _lock = threading.RLock()
    _ruleset: Optional[RuleSet] = None
    _path: Optional[str] = None
    _version: int = 0

    # ---- 源 RuntimeConfig 同构入口 -------------------------------------
    @classmethod
    def init_config(cls, path: str | Path) -> RuleSet:
        """装载阈值表并编译成规则集（首次装载 / 显式重载）。

        同构源 ``init_config``：**显式注入**材料路径后整体重建。
        """
        return cls._load(Path(path), force=True)

    @classmethod
    def get_ruleset(cls) -> Optional[RuleSet]:
        """取当前生效的规则集快照（源 ``get_env`` 语义）。"""
        with cls._lock:
            return cls._ruleset

    # ---- 本产品新增：热载入与快照 ---------------------------------------
    @classmethod
    def reload(cls, path: str | Path | None = None, force: bool = False) -> Optional[RuleSet]:
        """热载入：材料指纹变了才重编译并**原子切换**到新快照。

        指纹没变时返回当前快照且**不**递增版本 —— 反复重载同一份材料
        不应让快照 ID 一路上涨，否则事后复核会被噪音淹没。
        """
        with cls._lock:
            target = Path(path) if path is not None else (Path(cls._path) if cls._path else None)
            if target is None:
                raise RuntimeError("规则集尚未装载：请先调用 RuleRegistry.init_config(path)")
            return cls._load(target, force=force)

    @classmethod
    def snapshot_id(cls) -> str:
        """当前规则集快照 ID（未装载时为空串）。"""
        with cls._lock:
            return cls._ruleset.snapshot_id if cls._ruleset else ""

    @classmethod
    def summary(cls) -> dict[str, Any]:
        """当前规则集摘要；未装载时给出明确的空态而不是报错。"""
        with cls._lock:
            if cls._ruleset is None:
                return {
                    "snapshot_id": "",
                    "source_name": "",
                    "source_sha256": "",
                    "versions": [],
                    "active_versions": [],
                    "rule_count": 0,
                    "gap_count": 0,
                    "item_count": 0,
                    "compiled_at": "",
                    "gaps": [],
                }
            return cls._ruleset.summary()

    @classmethod
    def reset(cls) -> None:
        """清空注册表（测试与重装用）。"""
        with cls._lock:
            cls._ruleset = None
            cls._path = None
            cls._version = 0

    # ---- 内部 -----------------------------------------------------------
    @classmethod
    def _load(cls, path: Path, force: bool) -> RuleSet:
        """在锁内完成「编译 → 换快照」，读取方要么看到旧的、要么看到新的。"""
        with cls._lock:
            digest = sha256_file(path)
            current = cls._ruleset
            if (
                not force
                and current is not None
                and current.source_sha256 == digest
                and cls._path is not None
                and Path(cls._path) == path
            ):
                return current

            table = load_threshold_table(path)
            version = cls._version + 1
            snapshot_id = f"rs-{version:04d}-{digest[:12]}"
            ruleset = RuleSet.compile(
                table.rows,
                snapshot_id=snapshot_id,
                source_name=table.source_name,
                source_sha256=table.source_sha256,
            )
            cls._version = version
            cls._path = str(path)
            cls._ruleset = ruleset
            return ruleset
