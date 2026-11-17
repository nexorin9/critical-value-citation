#!/usr/bin/env python3
"""离线安装自检：交付给检验科 / 信息科的一条命令。

    python3 scripts/check_install.py                # 人读清单
    python3 scripts/check_install.py --json         # 机器可读结论
    python3 scripts/check_install.py --strict       # 凭据没配齐也算不通过（上线闸门）

自检覆盖五件事，**全程离线**（不连任何院内系统、不发任何请求）：

1. **运行环境** —— Python 版本 + 三个解析依赖是否装得上、版本够不够；
2. **配置项** —— 按 ``.env.example`` 逐条查必填凭据位与格式（只报键名，
   **不打印任何凭据值**）；
3. **目录权限** —— 台账 / 日志 / 快照 / 材料 / 帧目录能不能建、能写；
4. **台账库可写** —— 在库文件所在目录开一个临时库写一写、读一读、删掉
   （不碰真库，也不留残留）；
5. **院内材料可解析** —— 阈值表 xlsx 与制度 docx / pdf 能不能读进来，
   有几行被认出来、哪几行没认出来（未识别行不静默跳过）。

退出码：``0`` 无阻断项 / ``1`` 有阻断项 / ``2`` 参数错。
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

LEVEL_OK = "ok"
LEVEL_WARN = "warn"
LEVEL_ERROR = "error"

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2

#: 运行环境最低要求（与 pyproject.toml 的 requires-python 一致）。
MIN_PYTHON = (3, 10)

#: 依赖清单：模块名 → (pip 包名, 是否必需)。
#: ``pypdf`` 缺失只影响 pdf 制度解析，docx 仍可用 → 提醒级而非阻断级。
DEPENDENCIES: tuple[tuple[str, str, bool], ...] = (
    ("openpyxl", "openpyxl", True),
    ("docx", "python-docx", True),
    ("pypdf", "pypdf", False),
)

#: 依赖包最低版本（主版本比对；版本过低直接报阻断）。
MIN_VERSIONS: dict[str, tuple[int, ...]] = {
    "openpyxl": (3, 1),
    "python-docx": (1, 1),
    "pypdf": (4, 0),
}


@dataclass
class Item:
    """一条自检结论。"""

    level: str
    name: str
    message: str
    hint: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"level": self.level, "name": self.name, "message": self.message, "hint": self.hint}


@dataclass
class Section:
    """一组自检结论。"""

    title: str
    items: list[Item] = field(default_factory=list)

    def add(self, level: str, name: str, message: str, hint: str = "") -> None:
        self.items.append(Item(level=level, name=name, message=message, hint=hint))

    @property
    def worst(self) -> str:
        if any(item.level == LEVEL_ERROR for item in self.items):
            return LEVEL_ERROR
        if any(item.level == LEVEL_WARN for item in self.items):
            return LEVEL_WARN
        return LEVEL_OK

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "level": self.worst,
            "items": [item.to_dict() for item in self.items],
        }


# ---------------------------------------------------------------------------
# .env 读取（只补没设过的键；绝不打印值）
# ---------------------------------------------------------------------------


def load_env_file(path: Path) -> tuple[dict[str, str], list[str]]:
    """读 ``.env``，返回 ``(键值对, 解析异常行)``。

    只在环境变量里**还没有**这个键时才写入 —— 真实环境变量优先于文件，
    免得离线自检把运维已经注入的配置盖掉。
    """
    values: dict[str, str] = {}
    problems: list[str] = []
    if not path.exists():
        return values, problems
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        problems.append(f".env 读不出来：{exc}")
        return values, problems
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            problems.append(f".env 第 {lineno} 行没有等号，已跳过")
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if not key:
            problems.append(f".env 第 {lineno} 行没有键名，已跳过")
            continue
        values[key] = value
    return values, problems


def apply_env_file(values: dict[str, str]) -> int:
    """把 ``.env`` 里的键补进环境变量（已存在的键不动）。返回补进去的条数。"""
    applied = 0
    for key, value in values.items():
        if os.environ.get(key):
            continue
        os.environ[key] = value
        applied += 1
    return applied


# ---------------------------------------------------------------------------
# ① 运行环境
# ---------------------------------------------------------------------------


def _version_tuple(raw: str) -> tuple[int, ...]:
    parts: list[int] = []
    for chunk in raw.split("."):
        digits = ""
        for char in chunk:
            if char.isdigit():
                digits += char
            else:
                break
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


def check_runtime() -> Section:
    section = Section("运行环境")
    if sys.version_info >= MIN_PYTHON:
        section.add(
            LEVEL_OK,
            "Python 版本",
            f"{sys.version.split()[0]}（要求 ≥ {'.'.join(map(str, MIN_PYTHON))}）",
        )
    else:
        section.add(
            LEVEL_ERROR,
            "Python 版本",
            f"{sys.version.split()[0]} 低于要求的 {'.'.join(map(str, MIN_PYTHON))}",
            "换用 Python 3.10 及以上版本后重新安装依赖",
        )

    for module_name, package_name, required in DEPENDENCIES:
        try:
            module = importlib.import_module(module_name)
        except ImportError as exc:
            section.add(
                LEVEL_ERROR if required else LEVEL_WARN,
                f"依赖 {package_name}",
                f"装不上（{exc.__class__.__name__}）",
                f"pip install '{package_name}'" + ("" if required else "（缺它只能读 docx 制度，读不了 pdf）"),
            )
            continue
        raw = str(getattr(module, "__version__", "") or "")
        minimum = MIN_VERSIONS.get(package_name)
        if raw and minimum:
            current = _version_tuple(raw)
            if current and current < minimum:
                section.add(
                    LEVEL_ERROR,
                    f"依赖 {package_name}",
                    f"版本 {raw} 低于要求的 {'.'.join(map(str, minimum))}",
                    f"pip install -U '{package_name}>={'.'.join(map(str, minimum))}'",
                )
                continue
        section.add(LEVEL_OK, f"依赖 {package_name}", f"已安装（版本 {raw or '未标版本'}）")

    try:
        import critval  # noqa: F401
        from critval import cli, daemon  # noqa: F401

        section.add(LEVEL_OK, "本项目模块", "critval / cli / daemon 均可导入")
    except Exception as exc:  # 装坏了要在自检里就说清楚，而不是等到起服务才炸
        section.add(
            LEVEL_ERROR,
            "本项目模块",
            f"导入失败：{exc}",
            "确认在项目根目录执行本脚本，且源码完整",
        )
    return section


# ---------------------------------------------------------------------------
# ② 配置项
# ---------------------------------------------------------------------------


def check_config(strict: bool) -> Section:
    from critval.config import CONFIG_KEYS, Config, REQUIREMENTS

    section = Section("配置项")
    env_file = PROJECT_ROOT / ".env"
    values, problems = load_env_file(env_file)
    applied = apply_env_file(values) if values else 0

    if env_file.exists():
        section.add(
            LEVEL_OK,
            ".env 文件",
            f"读到 {len(values)} 个键（补进环境变量 {applied} 个；已有环境变量优先）",
        )
    else:
        section.add(
            LEVEL_WARN,
            ".env 文件",
            "没有 .env（配置全靠系统环境变量注入）",
            "复制 .env.example 为 .env 后按院内实际填写",
        )
    for problem in problems:
        section.add(LEVEL_WARN, ".env 文件", problem)

    Config.init_env()
    check = Config.self_check()
    required_keys = {req.key for req in REQUIREMENTS if req.blocking and not req.default_ok}
    group_only = {req.key for req in REQUIREMENTS if req.group}

    missing_required = sorted(
        {issue.key for issue in check.missing} & required_keys
    )
    group_empty = sorted(
        issue.key for issue in check.issues if issue.kind == "group_empty"
    )
    invalid = sorted(issue.key for issue in check.invalid)

    for key in missing_required:
        section.add(
            LEVEL_ERROR if strict else LEVEL_WARN,
            f"必填 {key}",
            "没设置",
            "在 .env 或 systemd EnvironmentFile 里补上",
        )
    for key in group_empty:
        section.add(
            LEVEL_ERROR if strict else LEVEL_WARN,
            f"入站通道组 {key}",
            "组内所有通道都没配，LIS 推不出危急值",
            "至少配 MLLP 监听或 LIS REST 回调其中一条",
        )
    for issue in check.invalid:
        section.add(
            LEVEL_ERROR,
            f"格式 {issue.key}",
            issue.message,
            issue.hint,
        )

    # 组内只配了一条的：不算错，但要提醒对方知道现在走的是哪条通道
    for note in check.notes:
        section.add(LEVEL_WARN, "通道提示", note)

    unset_optional = [key for key in CONFIG_KEYS if key not in group_only and not Config.get_env(key)]
    if unset_optional:
        section.add(
            LEVEL_OK,
            "其余配置项",
            f"{len(CONFIG_KEYS) - len(unset_optional)} 项已生效（有默认值或已设置）",
        )
    if not missing_required and not group_empty and not invalid:
        section.add(LEVEL_OK, "配置自检", f"通过（配置版本 {check.version}）")
    return section


# ---------------------------------------------------------------------------
# ③ 目录权限
# ---------------------------------------------------------------------------


def _resolve(path_text: str) -> Path:
    path = Path(path_text).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path


def _probe_writable(target: Path, *, is_file: bool) -> tuple[bool, str]:
    """试着建目录 / 写临时文件，确认真的能写。"""
    try:
        if is_file:
            target.parent.mkdir(parents=True, exist_ok=True)
            probe = target.with_name(target.name + ".check_install_probe")
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            return True, f"目录可写：{target.parent}"
        target.mkdir(parents=True, exist_ok=True)
        probe = target / ".check_install_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True, f"目录可写：{target}"
    except OSError as exc:
        return False, f"{exc.__class__.__name__}：{exc}"


def check_paths() -> Section:
    from critval.config import Config

    section = Section("目录权限")
    targets: list[tuple[str, str, bool]] = [
        ("台账库", Config.get_env("CRITVAL_DB_PATH") or "./data/critval.db", True),
        ("心跳文件", Config.get_env("CRITVAL_HEARTBEAT_PATH") or "./data/heartbeat.json", True),
        ("单实例锁", Config.get_env("CRITVAL_LOCK_PATH") or "./data/critval.lock", True),
        ("本机通知队列", Config.get_env("CRITVAL_LOCAL_NOTIFICATION_PATH") or "./data/local_notifications.jsonl", True),
        ("处置卡快照", Config.get_env("CRITVAL_SNAPSHOT_DIR") or "./data/card_snapshots", False),
        ("材料投递目录", Config.get_env("CRITVAL_MATERIAL_DIR") or "./data/materials", False),
        ("帧投递目录", Config.get_env("CRITVAL_FRAME_DIR") or "./data/inbound_frames", False),
        ("配置快照目录", Config.get_env("CRITVAL_CONFIG_SNAPSHOT_DIR") or "./reports/config_snapshots", False),
        ("日志目录", Config.get_env("CRITVAL_LOG_DIR") or "./logs", False),
    ]
    for name, raw, is_file in targets:
        target = _resolve(raw)
        ok, message = _probe_writable(target, is_file=is_file)
        if ok:
            section.add(LEVEL_OK, name, message)
        else:
            section.add(
                LEVEL_ERROR,
                name,
                message,
                f"给运行账号写权限，或把路径改到可写目录：{target}",
            )
    return section


# ---------------------------------------------------------------------------
# ④ 台账库可写
# ---------------------------------------------------------------------------


def check_sqlite() -> Section:
    from critval.config import Config

    section = Section("台账库")
    db_raw = Config.get_env("CRITVAL_DB_PATH") or "./data/critval.db"
    db_path = _resolve(db_raw)
    probe = db_path.with_name(db_path.name + ".check_install_probe.db")
    try:
        probe.unlink()
    except OSError:
        pass
    try:
        conn = sqlite3.connect(str(probe), timeout=10.0)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("CREATE TABLE probe (id TEXT PRIMARY KEY, payload TEXT)")
            conn.execute("INSERT INTO probe VALUES (?, ?)", ("1", "ok"))
            row = conn.execute("SELECT payload FROM probe WHERE id = ?", ("1",)).fetchone()
            if not row or row[0] != "ok":
                section.add(LEVEL_ERROR, "写入回读", "写进去读不出来", "换一块磁盘或检查存储层")
            else:
                section.add(LEVEL_OK, "写入回读", "临时库写入并读回成功")
            mode = conn.execute("PRAGMA journal_mode").fetchone()
            mode_text = str(mode[0]) if mode else "未知"
            if mode_text.lower() in ("wal", "memory"):
                section.add(LEVEL_OK, "并发加固", f"journal_mode={mode_text}，busy_timeout=30000ms")
            else:
                section.add(
                    LEVEL_WARN,
                    "并发加固",
                    f"journal_mode={mode_text}（不是 WAL，读写会互相阻塞）",
                    "确认数据目录所在文件系统支持 WAL（本地盘即可，NFS 不支持）",
                )
        finally:
            conn.close()
    except sqlite3.Error as exc:
        section.add(
            LEVEL_ERROR,
            "台账库",
            f"开不了临时库：{exc}",
            f"检查目录权限与磁盘空间：{probe.parent}",
        )
    finally:
        for suffix in ("", "-wal", "-shm"):
            try:
                probe.with_name(probe.name + suffix).unlink()
            except OSError:
                pass
    section.add(LEVEL_OK, "台账路径", f"真库位置：{db_path}（自检不碰真库）")
    return section


# ---------------------------------------------------------------------------
# ⑤ 院内材料可解析
# ---------------------------------------------------------------------------


def check_materials() -> Section:
    from critval.config import Config

    section = Section("院内材料")
    threshold_path = _resolve(Config.get_env("CRITVAL_THRESHOLD_PATH") or "")
    policy_path = _resolve(Config.get_env("CRITVAL_POLICY_PATH") or "")

    # ---- 阈值表 ---------------------------------------------------------
    if not threshold_path.exists():
        section.add(
            LEVEL_ERROR,
            "阈值表",
            f"文件不存在：{threshold_path}",
            "在 .env 里把 CRITVAL_THRESHOLD_PATH 指到院内危急值标准文件",
        )
    else:
        try:
            from critval.kb.loader import load_threshold_table

            table = load_threshold_table(threshold_path)
            report = table.report
            total = report.total_rows
            recognized = report.recognized_rows
            unrecognized = len(report.unrecognized_rows)
            incomplete = len(report.incomplete_rows)
            columns = len(report.unrecognized_columns)
            if unrecognized:
                section.add(
                    LEVEL_ERROR,
                    "阈值表",
                    f"共 {total} 行，识别 {recognized} 行，{unrecognized} 行没认出来",
                    "看解析报告（kb-watch / parse-queue）逐行回填，未识别行不会进判定集",
                )
            else:
                section.add(
                    LEVEL_OK,
                    "阈值表",
                    f"{threshold_path.name}：共 {total} 行，识别 {recognized} 行"
                    + (f"，{incomplete} 行字段缺失" if incomplete else ""),
                )
            if columns:
                section.add(
                    LEVEL_WARN,
                    "阈值表列名",
                    f"有 {columns} 个列名没认出来：{'、'.join(report.unrecognized_columns[:6])}",
                    "没认出来的列会被忽略；确认里面没有你指望的字段",
                )
            if recognized == 0:
                section.add(
                    LEVEL_ERROR,
                    "阈值表",
                    "一行都没认出来（表头可能不在前 5 行）",
                    "把表头放到表格顶部，或按 docs/material_spec.md 改列名",
                )
        except Exception as exc:
            section.add(
                LEVEL_ERROR,
                "阈值表",
                f"解析失败：{exc.__class__.__name__}：{exc}",
                "确认文件是 .xlsx 且没加密损坏",
            )

    # ---- 制度 -----------------------------------------------------------
    if not policy_path.exists():
        section.add(
            LEVEL_ERROR,
            "报告制度",
            f"文件不存在：{policy_path}",
            "在 .env 里把 CRITVAL_POLICY_PATH 指到院内危急值报告制度（docx / pdf）",
        )
    else:
        try:
            from critval.kb.policy import load_policy_document

            document = load_policy_document(policy_path)
            if document.degraded:
                section.add(
                    LEVEL_ERROR,
                    "报告制度",
                    f"解析降级：{document.degrade_reason}",
                    "装齐依赖（python-docx / pypdf）后重跑自检",
                )
            else:
                report = document.report
                clauses = len(document.clauses)
                section.add(
                    LEVEL_OK,
                    "报告制度",
                    f"{policy_path.name}：{report.total_rows if report else 0} 段，"
                    f"识别 {clauses} 条条款"
                    + (f"，版本 {document.version}" if document.version else "")
                    + (
                        f"，生效 {document.effective_from.isoformat()}"
                        if document.effective_from
                        else ""
                    ),
                )
                if document.report and document.report.unrecognized_rows:
                    section.add(
                        LEVEL_WARN,
                        "报告制度",
                        f"有 {len(document.report.unrecognized_rows)} 段没认出来",
                        "这些段落不会进召回依据，看解析报告确认是否漏了关键条款",
                    )
        except Exception as exc:
            section.add(
                LEVEL_ERROR,
                "报告制度",
                f"解析失败：{exc.__class__.__name__}：{exc}",
                "确认文件是 .docx / .pdf 且没加密",
            )
    return section


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------

_MARKERS = {LEVEL_OK: "[通过]", LEVEL_WARN: "[提醒]", LEVEL_ERROR: "[阻断]"}


def render_text(sections: list[Section]) -> str:
    lines: list[str] = []
    lines.append("危急值处置依据检索系统 · 离线安装自检")
    lines.append("=" * 46)
    for section in sections:
        lines.append("")
        lines.append(f"■ {section.title}")
        if not section.items:
            lines.append("  （没有检查项）")
            continue
        for item in section.items:
            lines.append(f"  {_MARKERS[item.level]} {item.name}：{item.message}")
            if item.hint:
                lines.append(f"          → {item.hint}")
    errors = sum(
        1 for section in sections for item in section.items if item.level == LEVEL_ERROR
    )
    warns = sum(
        1 for section in sections for item in section.items if item.level == LEVEL_WARN
    )
    lines.append("")
    lines.append("=" * 46)
    lines.append(f"阻断项 {errors} 项 · 提醒项 {warns} 项")
    if errors:
        lines.append("结论：不通过 —— 上面标 [阻断] 的项处理完再上线")
    elif warns:
        lines.append("结论：可以安装，但上线前把 [提醒] 项确认一遍")
    else:
        lines.append("结论：通过 —— 装好了，可以按 docs/operations.md 起服务")
    return "\n".join(lines)


def build_report(sections: list[Section]) -> dict[str, Any]:
    errors = sum(
        1 for section in sections for item in section.items if item.level == LEVEL_ERROR
    )
    warns = sum(
        1 for section in sections for item in section.items if item.level == LEVEL_WARN
    )
    return {
        "command": "check-install",
        "ok": errors == 0,
        "exit_code": EXIT_OK if errors == 0 else EXIT_FAIL,
        "project_root": str(PROJECT_ROOT),
        "errors": errors,
        "warnings": warns,
        "sections": [section.to_dict() for section in sections],
    }


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python3 scripts/check_install.py",
        description="离线安装自检：依赖 / 配置 / 目录权限 / 台账库可写 / 院内材料可解析",
    )
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="凭据位没配齐也算不通过（上线前闸门用）",
    )
    parser.add_argument(
        "--skip-materials",
        action="store_true",
        help="跳过院内材料解析（材料还没到位时用）",
    )
    args = parser.parse_args(argv)

    sections = [check_runtime(), check_config(strict=args.strict), check_paths(), check_sqlite()]
    if not args.skip_materials:
        sections.append(check_materials())

    report = build_report(sections)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(render_text(sections))
    return int(report["exit_code"])


if __name__ == "__main__":
    sys.exit(main())
