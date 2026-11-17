"""落生产核对 · 凭据卫生单测（任务 id 51）。

盯住三件事：

* **代码内不出现密钥** —— 凭据一律从环境变量读（``Config.get_env``），
  产品代码里不得出现「看起来像凭据」的字符串字面量；
* **出站地址不写死** —— 默认出站地址不得指向 ``127.0.0.1`` /
  ``localhost`` / ``example.*``（入站监听地址不受此限）；
* **替身只在测试数据层** —— 协议替身只允许出现在 ``tests/fixtures/`` 下，
  产品代码既不实现替身，也不 import 替身。

**这些断言不是空跑**：本文件里用「故意违规的样例源码」验证检测器本身
抓得住 —— 检测器抓不住违规，就等于没有检测器。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import audit_production as audit  # noqa: E402

from critval.config import CONFIG_KEYS, SECRET_KEYS  # noqa: E402


# ---------------------------------------------------------------------------
# 检测器本身抓得住违规（不是空跑）
# ---------------------------------------------------------------------------
def test_检测器抓得住写死的密钥字面量():
    违规源码 = (
        "CRITVAL_TOKEN = 'aBcDeF0123456789XyZ'\n"
        "def f():\n"
        "    api_key = 'sk-live-9f8e7d6c5b4a3210'\n"
        "    return api_key\n"
    )
    hits = audit.credential_literals_in_source(违规源码)
    assert len(hits) == 2, hits
    assert any("CRITVAL_TOKEN" in hit for hit in hits)
    assert any("api_key" in hit for hit in hits)


def test_检测器不误伤报错文案与格式化串():
    合规源码 = (
        "token = Config.get_env('CRITVAL_TOKEN') or ''\n"
        "raise RegistrationError(\n"
        "    '缺少 CRITVAL_TOKEN（登记系统 Bearer 凭据），无法发起出站调用'\n"
        ")\n"
        "headers = {'Authorization': f'Bearer {self.token}'}\n"
        "message = '登记系统回写通道未接入（本轮没有 CRITVAL_BASE_URL）'\n"
    )
    assert audit.credential_literals_in_source(合规源码) == []


def test_检测器抓得住写死的本机出站地址():
    违规源码 = (
        "BASE = 'http://127.0.0.1:8080/api/v1/critical-events'\n"
        "OTHER = 'https://example.com/api/v1/roster'\n"
    )
    hits = audit.forbidden_outbound_hosts_in_source(违规源码)
    assert len(hits) == 2, hits
    assert any("127.0.0.1" in hit for hit in hits)
    assert any("example.com" in hit for hit in hits)


def test_检测器不误伤入站监听地址与真实内网地址():
    合规源码 = (
        "DEFAULT_HOST = '127.0.0.1'          # 入站监听地址（本机值班台）\n"
        "HINT = '登记系统根地址（形如 http://10.0.0.8:8080）'\n"
        "SPLIT = urlsplit(raw if '://' in raw else f'http://{raw}')\n"
    )
    assert audit.forbidden_outbound_hosts_in_source(合规源码) == []


def test_检测器认得出协议替身():
    替身源码 = (
        "from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer\n"
        "class _Handler(BaseHTTPRequestHandler):\n"
        "    pass\n"
        "class Double(ThreadingHTTPServer):\n"
        "    pass\n"
    )
    assert audit.defines_protocol_server(替身源码) is True
    assert audit.defines_protocol_server("class Plain:\n    pass\n") is False


# ---------------------------------------------------------------------------
# 产品代码本身干净
# ---------------------------------------------------------------------------
def test_产品代码里没有写死的密钥():
    problems: list[str] = []
    for path in audit.product_sources():
        rel = path.relative_to(PROJECT_ROOT).as_posix()
        for item in audit.credential_literals_in_source(audit.read_text(path)):
            problems.append(f"{rel} {item}")
    assert not problems, "产品代码里出现了写死的凭据：\n" + "\n".join(problems)


def test_产品代码里没有写死的本机出站地址():
    problems: list[str] = []
    for path in audit.product_sources():
        rel = path.relative_to(PROJECT_ROOT).as_posix()
        for item in audit.forbidden_outbound_hosts_in_source(audit.read_text(path)):
            problems.append(f"{rel} {item}")
    assert not problems, "产品代码里出现了写死的本机 / 示例出站地址：\n" + "\n".join(problems)


def test_产品代码不引用测试数据层():
    import re

    problems: list[str] = []
    for path in audit.product_sources():
        rel = path.relative_to(PROJECT_ROOT).as_posix()
        source = audit.read_text(path)
        for pattern in (
            r"\bfrom\s+fixtures\b",
            r"\bimport\s+fixtures\b",
            r"\bfrom\s+tests\b",
            r"sys\.path\.insert\([^)]*[\"']tests[\"']",
        ):
            if re.search(pattern, source):
                problems.append(f"{rel} 引用了测试数据层")
    assert not problems, "产品代码引用了测试替身：\n" + "\n".join(problems)


def test_替身实现只出现在测试数据层():
    断言 = audit.assert_doubles_confined()
    assert 断言.passed, "；".join(断言.evidence)


# ---------------------------------------------------------------------------
# 凭据位口径
# ---------------------------------------------------------------------------
def test_每个密钥位都以环境变量读取且没有代码默认值():
    断言 = audit.assert_credentials_from_env()
    assert 断言.passed, "；".join(断言.evidence)


def test_配置样例里的密钥位留空():
    env_example = PROJECT_ROOT / ".env.example"
    assert env_example.exists(), ".env.example 缺失"
    import re

    problems: list[str] = []
    for index, line in enumerate(env_example.read_text(encoding="utf-8").splitlines(), start=1):
        for name in SECRET_KEYS:
            match = re.match(rf"\s*{name}\s*=\s*(.*)$", line)
            if match and match.group(1).strip():
                problems.append(f".env.example:{index} {name}")
    assert not problems, "配置样例里写了密钥值：" + "、".join(problems)


def test_配置样例与配置键集一一对应():
    env_example = PROJECT_ROOT / ".env.example"
    declared = set()
    for line in env_example.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        declared.add(stripped.split("=", 1)[0].strip())
    assert declared == set(CONFIG_KEYS)


# ---------------------------------------------------------------------------
# 端到端跑一次审计脚本
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module", autouse=True)
def _生成落生产核对报告():
    """本模块的用例跑之前，先把「落生产核对」报告跑出来。

    报告正文落在 ``reports/production_seams.md``（运行期目录，已 gitignore），
    可发布副本同时写到 ``docs/production_seams.md``。

    **为什么测试要自己生成它**：报告原本靠「上一轮跑过 audit_production.py」
    这类带外产物撑着 —— 干净 clone 上来一条 ``pytest -q`` 就会因为报告不在
    而报三条失败。测试自己跑一遍生成器，套件才是**自足且可重复**的：
    清空 ``reports/`` 再跑，结果一样。
    """
    result = subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "audit_production.py"),
            "--json",
        ],
        capture_output=True,
        text=True,
        cwd=str(PROJECT_ROOT),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert (PROJECT_ROOT / "reports" / "production_seams.md").exists(), "核对报告没生成"
    return result


def test_审计脚本本身跑得通且全部断言通过():
    result = subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "audit_production.py"),
            "--check",
            "--json",
        ],
        capture_output=True,
        text=True,
        cwd=str(PROJECT_ROOT),
    )
    assert result.returncode == 0, result.stdout + result.stderr

    import json

    payload = json.loads(result.stdout)
    failed = [a for a in payload["assertions"] if not a["passed"]]
    assert not failed, failed

    # 接缝清单要覆盖「三条出站 + 两条入站」
    outbound = [s for s in payload["seams"] if s["direction"] == "出站"]
    inbound = [s for s in payload["seams"] if s["direction"] == "入站"]
    assert len(outbound) >= 3, outbound
    assert len(inbound) >= 2, inbound

    # 每条接缝都要有凭据位与字段规范
    for seam in payload["seams"]:
        assert seam["credentials"], seam
        assert seam["fields"], seam
        assert seam["url_template"], seam


def test_审计报告已落盘且含六项断言():
    report = PROJECT_ROOT / "reports" / "production_seams.md"
    assert report.exists(), "reports/production_seams.md 未生成"
    text = report.read_text(encoding="utf-8")
    for key in ("A", "B", "C", "D", "E", "F"):
        assert f"| {key} |" in text, f"报告里没有断言 {key}"
    # 落生产核对表里不许出现「不通过」
    assert "**不通过**" not in text


def test_报告列出三条真实出站与两条入站():
    """任务 id 51 的验收口径：企微写 / 登记系统写 / 排班系统读 + MLLP / HTTP 推送。"""
    report = PROJECT_ROOT / "reports" / "production_seams.md"
    assert report.exists(), "reports/production_seams.md 未生成"
    text = report.read_text(encoding="utf-8")

    # 三条出站：写、写、读
    assert "企业微信值班群机器人" in text
    assert "院内危急值登记 / 护理执行平台" in text
    assert "排班 / 值班名单系统" in text
    # 两条入站
    assert "MLLP over TCP" in text
    assert "{CRITVAL_LISTEN_BASE_URL}/api/v1/critical-value-events" in text
    # 凭据位与字段规范都要点出来
    assert "CRITVAL_WECOM_WEBHOOK_URL" in text
    assert "CRITVAL_BASE_URL" in text
    assert "CRITVAL_TOKEN" in text
    assert "NURSE_ROSTER_BASE_URL" in text
    assert "ROSTER_TOKEN" in text
    assert "LIS_CRITICAL_TOKEN" in text
    assert "message_control_id" in text
    assert "threshold_row" in text


def test_可发布副本与报告一致():
    """``reports/`` 在 .gitignore 里，docs/ 下要有同一份副本。"""
    report = PROJECT_ROOT / "reports" / "production_seams.md"
    docs = PROJECT_ROOT / "docs" / "production_seams.md"
    assert report.exists(), "reports/production_seams.md 未生成"
    assert docs.exists(), "docs/production_seams.md 未生成"
    assert report.read_text(encoding="utf-8") == docs.read_text(encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
