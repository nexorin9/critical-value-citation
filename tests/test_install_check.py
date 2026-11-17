"""交付物自检：离线安装自检脚本 + 四份科室交付文档。

这组测试守的是一件事：**交付文档与代码不能脱节**。科室拿着文档上手，
文档里写错一个配置项名、一个列名别名、一条跑不通的命令，上线当天就会出事。

覆盖：

* ``scripts/check_install.py`` 在干净环境上给出可执行结论与正确退出码；
* ``docs/operations.md`` 的配置项速查表 = ``.env.example`` = ``critval.config.CONFIG_KEYS``；
* ``docs/material_spec.md`` 的列名别名 = ``critval.kb.schema.THRESHOLD_FIELDS`` /
  ``POLICY_LABELS``（材料规范是代码里那份 schema 的投影，不能各写各的）；
* 四份文档里出现的 ``python -m critval.cli <子命令>`` 都真实存在；
* 回归：CLI 不得调用不存在的 ``Config.get``（那个名字不存在，会让 8 个子命令崩）。
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = PROJECT_ROOT / "scripts" / "check_install.py"
DOCS = PROJECT_ROOT / "docs"
ENV_EXAMPLE = PROJECT_ROOT / ".env.example"

DELIVERY_DOCS = {
    "operations": DOCS / "operations.md",
    "handover_checklist": DOCS / "handover_checklist.md",
    "material_spec": DOCS / "material_spec.md",
    "troubleshooting": DOCS / "troubleshooting.md",
}


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def _load_script_module():
    """把 ``scripts/check_install.py`` 当模块加载（scripts 不是包）。

    加载后必须塞进 ``sys.modules`` —— 脚本里用了 ``dataclass``，
    dataclasses 解析注解时会回头找 ``sys.modules[cls.__module__]``，
    没登记就拿不到模块命名空间，直接抛 AttributeError。
    """
    spec = importlib.util.spec_from_file_location("critval_check_install", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _clean_env() -> dict[str, str]:
    """一份**不带任何本项目环境变量**的环境（模拟干净机器）。"""
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/tmp"),
        "LANG": os.environ.get("LANG", "zh_CN.UTF-8"),
        "PYTHONIOENCODING": "utf-8",
    }
    for key in list(os.environ):
        if key.startswith(("CRITVAL_", "LIS_", "ROSTER_", "NURSE_")) or key == "CONSOLE_TOKEN":
            continue
    return env


def _run_isolated(tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """把脚本复制到临时目录里跑，让它的 PROJECT_ROOT 落在隔离区。

    为什么要复制：脚本按「自己所在目录的上一级」当项目根，复制一份就能把
    开发者机器上的 ``.env``、``data/`` 隔离开，测出来的结论才稳定。
    ``PYTHONPATH`` 指回真项目，``import critval`` 照常可用。
    """
    target = tmp_path / "scripts" / "check_install.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(SCRIPT, target)
    env = _clean_env()
    env["PYTHONPATH"] = str(PROJECT_ROOT)
    return subprocess.run(
        [sys.executable, str(target), *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(tmp_path),
        timeout=180,
    )


# ---------------------------------------------------------------------------
# 离线安装自检脚本
# ---------------------------------------------------------------------------


def test_check_install_script_exists() -> None:
    assert SCRIPT.exists(), "离线安装自检脚本缺失"
    assert SCRIPT.stat().st_size > 2000, "自检脚本内容过短，可能是空壳"


def test_check_install_runs_in_place_and_reports_sections() -> None:
    """在真项目里跑一遍：结论可读、五项检查都在、样例材料能解析。"""
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--json"],
        capture_output=True,
        text=True,
        cwd=str(PROJECT_ROOT),
        timeout=180,
    )
    assert result.returncode in (0, 1), f"自检脚本异常退出：{result.stderr[-800:]}"
    payload = json.loads(result.stdout)
    assert payload["command"] == "check-install"
    assert "exit_code" in payload and "ok" in payload
    titles = [section["title"] for section in payload["sections"]]
    for expected in ("运行环境", "配置项", "目录权限", "台账库", "院内材料"):
        assert expected in titles, f"自检少了「{expected}」这一项"
    # 样例材料必须解析得出来 —— 解析不出来说明材料面坏了
    material = next(s for s in payload["sections"] if s["title"] == "院内材料")
    messages = " ".join(item["message"] for item in material["items"])
    assert "阈值表" in messages, "院内材料检查里没有阈值表结论"


def test_check_install_clean_env_passes_without_credentials(tmp_path: Path) -> None:
    """干净环境：凭据没配齐只是提醒，不该把安装判成失败。"""
    result = _run_isolated(tmp_path, "--json", "--skip-materials")
    assert result.returncode == 0, f"干净环境下不该判失败：{result.stdout[-800:]}"
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["errors"] == 0


def test_check_install_strict_blocks_on_missing_credentials(tmp_path: Path) -> None:
    """上线闸门：``--strict`` 下凭据没配齐必须拦下来（退出码 1）。"""
    result = _run_isolated(tmp_path, "--json", "--strict", "--skip-materials")
    assert result.returncode == 1, f"--strict 应当拦下未配凭据的环境：{result.stdout[-800:]}"
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["errors"] >= 1
    names = " ".join(
        item["name"]
        for section in payload["sections"]
        for item in section["items"]
    )
    assert "CONSOLE_TOKEN" in names, "缺失凭据清单里应当点名 CONSOLE_TOKEN"


def test_check_install_env_file_parsing(tmp_path: Path) -> None:
    """``.env`` 解析：注释、空行、引号、缺等号的行都要处理对。"""
    module = _load_script_module()
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "# 注释行",
                "",
                "PLAIN=value",
                'QUOTED="quoted value"',
                "SINGLE='single'",
                "NO_EQUALS",
                "=novalue",
            ]
        ),
        encoding="utf-8",
    )
    values, problems = module.load_env_file(env_file)
    assert values["PLAIN"] == "value"
    assert values["QUOTED"] == "quoted value"
    assert values["SINGLE"] == "single"
    assert "NO_EQUALS" not in values
    assert len(problems) == 2, f"两行异常应被记下：{problems}"


def test_check_install_env_file_does_not_override_existing(monkeypatch) -> None:
    """真实环境变量优先于 ``.env``：别把运维注入的配置盖掉。"""
    module = _load_script_module()
    monkeypatch.setenv("CRITVAL_ALREADY_SET", "from-env")
    monkeypatch.delenv("CRITVAL_ONLY_FILE", raising=False)
    applied = module.apply_env_file(
        {"CRITVAL_ALREADY_SET": "from-file", "CRITVAL_ONLY_FILE": "from-file"}
    )
    assert os.environ["CRITVAL_ALREADY_SET"] == "from-env"
    assert os.environ["CRITVAL_ONLY_FILE"] == "from-file"
    assert applied == 1


# ---------------------------------------------------------------------------
# 四份交付文档
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("key", sorted(DELIVERY_DOCS))
def test_delivery_doc_exists_with_substantial_content(key: str) -> None:
    path = DELIVERY_DOCS[key]
    assert path.exists(), f"缺交付文档：{path.name}"
    text = path.read_text(encoding="utf-8")
    assert len(text) > 1500, f"{path.name} 内容过短"
    assert text.lstrip().startswith("#"), f"{path.name} 应当以标题开头"
    assert "|" in text, f"{path.name} 应当有表格"


def test_operations_doc_covers_start_stop_health_log_and_config() -> None:
    text = DELIVERY_DOCS["operations"].read_text(encoding="utf-8")
    for needle in (
        "部署形态",
        "启动",
        "停止",
        "健康检查",
        "日志",
        "配置项速查",
        "scripts/check_install.py",
        "systemd",
    ):
        assert needle in text, f"operations.md 缺「{needle}」"


def test_handover_checklist_covers_five_checks() -> None:
    text = DELIVERY_DOCS["handover_checklist"].read_text(encoding="utf-8")
    for needle in (
        "守护进程",
        "材料版本",
        "当班名单",
        "待补写",
        "死信",
        "交接单",
    ):
        assert needle in text, f"handover_checklist.md 缺「{needle}」"


def test_troubleshooting_has_symptom_cause_action_escalation() -> None:
    text = DELIVERY_DOCS["troubleshooting"].read_text(encoding="utf-8")
    for needle in ("症状", "可能原因", "处理动作", "何时转人工"):
        assert needle in text, f"troubleshooting.md 缺「{needle}」"
    for topic in ("入站", "推送", "回写", "名单", "阈值"):
        assert topic in text, f"troubleshooting.md 缺「{topic}」类故障"


# ---------------------------------------------------------------------------
# 文档 ↔ 代码一致性（防脱节）
# ---------------------------------------------------------------------------


def test_operations_config_table_matches_env_example_and_code() -> None:
    """配置项速查表 = ``.env.example`` = ``CONFIG_KEYS``，一条都不能少。"""
    from critval.config import CONFIG_KEYS

    env_keys = {
        line.split("=", 1)[0].strip()
        for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines()
        if "=" in line and not line.strip().startswith("#")
    }
    text = DELIVERY_DOCS["operations"].read_text(encoding="utf-8")

    assert env_keys == set(CONFIG_KEYS), ".env.example 与 CONFIG_KEYS 不一致"
    missing = sorted(key for key in CONFIG_KEYS if key not in text)
    assert not missing, f"operations.md 配置速查表漏了：{missing}"


def test_material_spec_aliases_match_schema() -> None:
    """材料规范里的列名别名必须与 ``critval.kb.schema`` 一致。"""
    from critval.kb.schema import THRESHOLD_FIELDS

    text = DELIVERY_DOCS["material_spec"].read_text(encoding="utf-8")
    for spec in THRESHOLD_FIELDS:
        assert spec.canonical in text, f"material_spec.md 缺规范字段 {spec.canonical}"
        for alias in spec.aliases:
            assert alias in text, f"material_spec.md 缺别名 {alias}（字段 {spec.canonical}）"


def test_material_spec_policy_labels_match_schema() -> None:
    """制度三项事实字段的别名必须与代码一致。"""
    from critval.kb.schema import POLICY_LABELS

    text = DELIVERY_DOCS["material_spec"].read_text(encoding="utf-8")
    for canonical, aliases in POLICY_LABELS.items():
        assert canonical in text, f"material_spec.md 缺事实字段 {canonical}"
        for alias in aliases:
            assert alias in text, f"material_spec.md 缺事实字段别名 {alias}"


def test_docs_only_reference_existing_cli_commands() -> None:
    """文档里写的每条 ``python -m critval.cli <子命令>`` 都得真存在。"""
    from critval.cli import _REGISTRY

    known = set(_REGISTRY.names())
    pattern = re.compile(r"critval\.cli\s+([a-z][a-z0-9\-]*)")
    referenced: set[str] = set()
    for path in DELIVERY_DOCS.values():
        text = path.read_text(encoding="utf-8")
        referenced.update(pattern.findall(text))
    unknown = sorted(referenced - known)
    assert not unknown, f"文档里引用了不存在的子命令：{unknown}"
    assert len(referenced) >= 8, "文档引用的子命令太少，说明交接清单没写实"


def test_cli_does_not_call_nonexistent_config_get() -> None:
    """回归：``Config`` 只有 ``get_env``；写 ``Config.get`` 会让子命令崩。"""
    from critval.config import Config

    assert hasattr(Config, "get_env")
    assert not hasattr(Config, "get"), "Config.get 不该存在（历史上写错过，会崩）"
    offenders = []
    for path in (PROJECT_ROOT / "critval").rglob("*.py"):
        if "Config.get(" in path.read_text(encoding="utf-8"):
            offenders.append(str(path.relative_to(PROJECT_ROOT)))
    assert not offenders, f"这些文件调用了不存在的 Config.get：{offenders}"
