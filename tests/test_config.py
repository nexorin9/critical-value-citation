"""配置面测试：键集与环境变量清单一致、热载入、凭据脱敏、代码内无硬编码密钥。

本轮（任务 id 20）补齐配置深度：

* 启动自检 —— 缺必填项 / 格式非法 / 入站通道组全空时启动即报错，
  且要说明「缺哪个环境变量、它服务于哪条通道、能否用默认值继续」；
* 配置快照 —— 每次热载入产生新版本 + 变更差异，并落盘到 reports/；
* 脱敏 —— 快照与日志里都不出现完整凭据；
* 可追溯 —— 判定 / 召回 / 推送 / 回写四个环节记录当时生效的配置版本。
"""

from __future__ import annotations

import io
import json
import logging
import re
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from critval.card.builder import build_card
from critval.config import (
    CONFIG_DEFAULTS,
    CONFIG_KEYS,
    SECRET_KEYS,
    Config,
    ConfigError,
    install_log_redaction,
    mask_secret,
)
from critval.judge.critical import judge_event
from critval.kb.loader import load_threshold_table
from critval.models import CriticalEvent, LedgerRecord
from critval.trace import (
    STAGE_JUDGE,
    STAGE_NOTIFY,
    STAGE_RETRIEVE,
    STAGE_WRITEBACK,
    EventTrace,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_EXAMPLE = PROJECT_ROOT / ".env.example"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "脱敏样例阈值表.xlsx"

#: 一套齐全且合法的院内配置（自检通过用）。
_FULL_ENV = {
    "CRITVAL_WECOM_WEBHOOK_URL": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abcdef",
    "CRITVAL_BASE_URL": "http://10.0.0.8:8080",
    "CRITVAL_TOKEN": "registry-token-0001",
    "NURSE_ROSTER_BASE_URL": "http://10.0.0.9:9000",
    "ROSTER_TOKEN": "roster-token-0001",
    "CRITVAL_LISTEN_BASE_URL": "http://0.0.0.0:8760",
    "LIS_CRITICAL_TOKEN": "lis-token-0001",
    "CONSOLE_TOKEN": "console-token-0001",
    "LIS_MLLP_BIND": "0.0.0.0:2575",
}


@pytest.fixture(autouse=True)
def _clean_config(monkeypatch):
    """每个用例从「环境变量全空、覆盖值全清」开始，避免用例间互相污染。"""
    Config._overrides.clear()
    for key in CONFIG_KEYS:
        monkeypatch.delenv(key, raising=False)
    yield
    Config._overrides.clear()


@pytest.fixture(scope="module")
def table():
    return load_threshold_table(FIXTURE)


def _event() -> CriticalEvent:
    return CriticalEvent(
        event_id="evt-0001",
        message_control_id="MSG0001",
        specimen_no="SPEC-0001",
        item_code="718-7",
        item_name="血红蛋白",
        value=Decimal("25"),
        unit="g/L",
        observed_at=datetime(2026, 1, 2, 8, 30),
        dept_code="ICU",
    )


def _env_example_keys() -> set[str]:
    keys: set[str] = set()
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        keys.add(line.split("=", 1)[0].strip())
    return keys


def test_键集与环境变量清单一致():
    assert _env_example_keys() == set(CONFIG_KEYS)


def test_凭据位没有默认值():
    for key in SECRET_KEYS:
        assert key not in CONFIG_DEFAULTS


def test_热载入读取环境变量并递增版本(monkeypatch):
    monkeypatch.setenv("CRITVAL_TOKEN", "token-from-env-1234")
    before = Config.config_version()
    snapshot = Config.reload()
    assert snapshot.version == before + 1
    assert Config.get_env("CRITVAL_TOKEN") == "token-from-env-1234"
    assert snapshot.sources["CRITVAL_TOKEN"] == "env"


def test_未设置的凭据位取不到值(monkeypatch):
    monkeypatch.delenv("ROSTER_TOKEN", raising=False)
    Config.reload()
    assert Config.get_env("ROSTER_TOKEN") is None
    assert Config.snapshot().sources["ROSTER_TOKEN"] == "unset"


def test_快照只暴露脱敏值(monkeypatch):
    monkeypatch.setenv("CRITVAL_TOKEN", "supersecrettoken")
    Config.reload()
    masked = Config.snapshot().masked_values()
    assert "supersecrettoken" not in masked["CRITVAL_TOKEN"]
    assert masked["CRITVAL_TOKEN"].startswith("supe")
    assert "oken" in masked["CRITVAL_TOKEN"]
    assert "16 chars" in masked["CRITVAL_TOKEN"]
    assert mask_secret("") == "(unset)"
    assert mask_secret(None) == "(unset)"


def test_代码内不出现硬编码密钥():
    pattern = re.compile(r"(CRITVAL_TOKEN|ROSTER_TOKEN|LIS_CRITICAL_TOKEN|CONSOLE_TOKEN)\s*=\s*[\"'][^\"']+[\"']")
    offenders: list[str] = []
    for path in (PROJECT_ROOT / "critval").rglob("*.py"):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if pattern.search(line):
                offenders.append(f"{path.relative_to(PROJECT_ROOT)}:{number}")
    assert offenders == []


# ---- 启动自检：缺必填项 ---------------------------------------------------


def test_缺必填项时自检失败并列出缺失清单():
    Config.reload()
    check = Config.self_check()
    assert check.blocking is True
    assert check.ok is False
    missing = {issue.key for issue in check.missing}
    assert {"CRITVAL_TOKEN", "CRITVAL_WECOM_WEBHOOK_URL", "ROSTER_TOKEN"} <= missing


def test_缺失项说清服务于哪条通道与能否继续():
    Config.reload()
    check = Config.self_check()
    issue = next(i for i in check.missing if i.key == "CRITVAL_TOKEN")
    assert "登记系统" in issue.channel  # 服务哪条出站通道
    assert issue.hint  # 可执行的补齐提示
    assert issue.default_ok is False  # 凭据位没有兜底，缺失即该通道不可用
    report = check.report_text()
    assert "CRITVAL_TOKEN" in report
    assert "服务通道" in report
    assert "是否可继续" in report


def test_缺必填项时启动即报错():
    Config.reload()
    with pytest.raises(ConfigError) as excinfo:
        Config.require_ready()
    message = str(excinfo.value)
    assert "启动自检未通过" in message
    assert "CRITVAL_TOKEN" in message


def test_格式非法给出具体原因(monkeypatch):
    monkeypatch.setenv("CRITVAL_TOKEN", "short")
    monkeypatch.setenv("LIS_MLLP_BIND", "no-port-here")
    Config.reload()
    check = Config.self_check()
    invalid = {issue.key: issue for issue in check.invalid}
    assert "CRITVAL_TOKEN" in invalid
    assert "12 位下限" in invalid["CRITVAL_TOKEN"].message
    assert "LIS_MLLP_BIND" in invalid
    assert "端口" in invalid["LIS_MLLP_BIND"].message
    assert check.blocking is True


def test_入站通道组全空时自检失败(monkeypatch):
    monkeypatch.setenv("LIS_BASE_URL", "")
    Config.reload()
    check = Config.self_check()
    group_issues = [issue for issue in check.issues if issue.kind == "group_empty"]
    assert len(group_issues) == 1
    assert group_issues[0].key == "lis_inbound"
    assert check.blocking is True


def test_入站通道组配一条即可继续(monkeypatch):
    monkeypatch.setenv("LIS_MLLP_BIND", "0.0.0.0:2575")
    Config.reload()
    check = Config.self_check()
    assert not [issue for issue in check.issues if issue.kind == "group_empty"]
    assert any("LIS_BASE_URL" in note for note in check.notes)


def test_必填齐备时自检通过(monkeypatch):
    for key, value in _FULL_ENV.items():
        monkeypatch.setenv(key, value)
    Config.reload()
    check = Config.self_check()
    assert check.blocking is False
    assert check.ok is True
    assert Config.require_ready().version == check.version


# ---- 热载入：版本递增 + 变更差异 ------------------------------------------


def test_热载入产生新版本并记录变更差异(monkeypatch):
    Config.reload()  # 基线：环境变量全空
    monkeypatch.setenv("CRITVAL_TOKEN", "token-value-000001")
    first = Config.reload()
    assert first.changes["CRITVAL_TOKEN"] == "added"
    monkeypatch.setenv("CRITVAL_TOKEN", "token-value-000002")
    second = Config.reload()
    assert second.version == first.version + 1
    assert second.changes["CRITVAL_TOKEN"] == "changed"


def test_值未变但来源变了也算差异(monkeypatch):
    Config.reload()  # 基线：环境变量全空
    monkeypatch.setenv("CRITVAL_TOKEN", "token-value-000001")
    Config.reload()
    Config.init_config(CRITVAL_TOKEN="token-value-000001")
    assert Config.snapshot().changes["CRITVAL_TOKEN"] == "source_changed"


def test_配置键被撤销记为移除(monkeypatch):
    monkeypatch.setenv("CRITVAL_TOKEN", "token-value-000001")
    Config.reload()
    monkeypatch.delenv("CRITVAL_TOKEN")
    second = Config.reload()
    assert second.changes["CRITVAL_TOKEN"] == "removed"


# ---- 快照落盘 + 脱敏 ------------------------------------------------------


def test_快照落盘只含脱敏值(tmp_path, monkeypatch):
    secret = "wecom-webhook-secret-value"
    monkeypatch.setenv("CRITVAL_WECOM_WEBHOOK_URL", secret)
    snapshot = Config.reload()
    path = Config.persist_snapshot(snapshot, tmp_path)
    text = path.read_text(encoding="utf-8")
    assert secret not in text
    payload = json.loads(text)
    assert payload["version"] == snapshot.version
    assert payload["changes"] == dict(snapshot.changes)
    assert payload["masked_values"]["CRITVAL_WECOM_WEBHOOK_URL"].startswith("weco")
    assert payload["sources"]["CRITVAL_WECOM_WEBHOOK_URL"] == "env"
    history = (tmp_path / "history.jsonl").read_text(encoding="utf-8")
    assert secret not in history


def test_日志里不出现完整凭据(monkeypatch):
    secret = "log-secret-token-abcdef"
    monkeypatch.setenv("CRITVAL_TOKEN", secret)
    Config.reload()
    stream = io.StringIO()
    logger = logging.getLogger("critval.test.redaction")
    logger.handlers.clear()
    logger.propagate = False
    logger.addHandler(logging.StreamHandler(stream))
    install_log_redaction(logger)
    logger.warning("回写失败，token=%s", secret)
    output = stream.getvalue()
    assert secret not in output
    assert "chars" in output


# ---- 配置版本可追溯 -------------------------------------------------------


def test_判定记录所用配置版本(table):
    Config.reload()
    version = Config.config_version()
    judgement = judge_event(_event(), table.rows)
    assert judgement.config_version == version


def test_热载入后旧结论仍指向旧配置版本(table):
    Config.reload()
    judgement = judge_event(_event(), table.rows)
    old_version = judgement.config_version
    Config.reload()
    assert Config.config_version() == old_version + 1
    assert judgement.config_version == old_version  # 旧结论不被改写


def test_处置卡带装配时的配置版本(table):
    Config.reload()
    version = Config.config_version()
    judgement = judge_event(_event(), table.rows)
    card = build_card(_event(), judgement)
    assert card.config_version == version


def test_四个环节各自记录当时配置版本():
    trace = EventTrace(event_id="evt-0001")
    trace.record(STAGE_JUDGE, ruleset_snapshot_id="rs-0001-abcdef")
    first_version = Config.config_version()
    Config.reload()
    trace.record(STAGE_RETRIEVE, ruleset_snapshot_id="rs-0001-abcdef")
    trace.record(STAGE_NOTIFY)
    trace.record(STAGE_WRITEBACK)
    versions = trace.versions()
    assert versions[STAGE_JUDGE] == first_version
    assert versions[STAGE_RETRIEVE] == first_version + 1
    assert versions[STAGE_NOTIFY] == first_version + 1
    assert versions[STAGE_WRITEBACK] == first_version + 1


def test_轨迹写进台账记录():
    trace = EventTrace(event_id="evt-0001")
    trace.record(STAGE_JUDGE, ruleset_snapshot_id="rs-0001-abcdef")
    record = LedgerRecord(event_id="evt-0001")
    trace.attach(record)
    assert record.config_version == Config.config_version()
    assert record.ruleset_snapshot_id == "rs-0001-abcdef"
