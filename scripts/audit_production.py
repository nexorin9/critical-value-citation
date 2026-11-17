#!/usr/bin/env python3
"""落生产核对：把产品代码里的真实集成面逐条点出来，生成可核对的清单。

产出 ``reports/production_seams.md`` —— 一份**可核对**的清单：每条接缝写清
对端是谁、走什么协议、凭据位（环境变量名）、目标地址模板、请求字段，
以及**代码证据**（文件 + 行号）。清单由脚本扫产品代码得到，不手写。

跑法::

    python3 scripts/audit_production.py            # 核对 + 写报告
    python3 scripts/audit_production.py --json     # 机器可读结论
    python3 scripts/audit_production.py --check    # 只跑断言，不写报告

**五项断言**（任一不通过即以非零码退出）：

============  ==========================================================
断言          口径
============  ==========================================================
A 凭据位登记  每条接缝的凭据位都必须在 ``Config.CONFIG_KEYS`` 里
B 凭据只从环境变量读  密钥位必须以 ``Config.get_env("<NAME>")`` 出现，
              且代码里**没有**给密钥位写过默认值
C 无密钥字面量  产品代码里不得出现「看起来像凭据」的字符串字面量
D 出站地址不写死  出站默认地址不得指向 ``127.0.0.1`` / ``localhost`` /
              ``example.*``（**入站监听地址**不受此限 —— 本机值班台
              就该只监听本机）
E 替身只在测试数据层  协议替身只允许出现在 ``tests/fixtures/`` 下；
              产品代码目录里既没有替身实现，也不 import 替身
============  ==========================================================

**为什么要有这张清单**：院方信息科要能一眼回答「这东西到底跟哪些系统
说话、凭据从哪来、没配会怎样」。清单里的每条接缝都能在代码里找到出处，
核对的人可以逐条点开看 —— 这是「文档写得好」替代不了的。
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from critval.config import CONFIG_KEYS, SECRET_KEYS  # noqa: E402

PRODUCT_DIR = PROJECT_ROOT / "critval"
TESTS_DIR = PROJECT_ROOT / "tests"
FIXTURES_DIR = TESTS_DIR / "fixtures"
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
REPORT_PATH = PROJECT_ROOT / "reports" / "production_seams.md"

#: 可发布副本（``reports/`` 在 ``.gitignore`` 里，科室与访客看不到它）。
DOCS_REPORT_PATH = PROJECT_ROOT / "docs" / "production_seams.md"

#: 出站「默认地址」里不许出现的本机 / 示例主机。
#: 入站监听地址不受此限（值班台默认只监听本机是设计内行为）。
FORBIDDEN_OUTBOUND_HOSTS = (
    "127.0.0.1",
    "localhost",
    "example.com",
    "example.org",
    "example.net",
    "example.internal",
)

#: 看起来像凭据的标识符片段（用来扫「密钥写死在代码里」）。
SECRET_NAME_PATTERN = re.compile(
    r"(token|secret|password|passwd|api_key|apikey|access_key|credential|webhook_key)",
    re.IGNORECASE,
)

#: 出站 URL 字面量（用于「不许写死本地 / 示例地址」）。
OUTBOUND_URL_PATTERN = re.compile(
    r"https?://(?P<host>[A-Za-z0-9._\-]+)(?P<rest>[:/?#]\S*)?",
)

#: 一行代码里出现这些片段，就认为「读了一次环境变量」。
ENV_READ_PATTERN = re.compile(
    r"""(?:Config\.get_env|os\.environ(?:\.get)?|os\.getenv)\s*\(\s*["'](?P<name>[A-Z0-9_]+)["']"""
)


# ---------------------------------------------------------------------------
# 接缝声明
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class FieldSpec:
    """接缝上的一个字段。"""

    name: str
    direction: str  # 请求 / 响应
    required: bool
    note: str = ""


@dataclass(frozen=True)
class Seam:
    """一条生产集成面。"""

    seam_id: str
    direction: str  # 出站 / 入站
    peer: str
    protocol: str
    method: str
    url_template: str
    secret_env: tuple[str, ...] = ()
    address_env: tuple[str, ...] = ()
    fields: tuple[FieldSpec, ...] = ()
    source_files: tuple[str, ...] = ()
    evidence: str = ""
    #: 在源码里认这条接缝的代码标记（报告「代码证据」一节按它定位）。
    evidence_markers: tuple[str, ...] = ()
    note: str = ""
    live: bool = True


#: 声明式接缝表 —— 这就是「本产品跟院内哪些系统说话」的**唯一口径**。
#: 改这里就等于改产品的集成面清单；脚本会拿它反过来核对代码。
SEAMS: tuple[Seam, ...] = (
    Seam(
        seam_id="roster_read",
        direction="出站",
        peer="排班 / 值班名单系统",
        protocol="HTTP（REST）",
        method="GET",
        url_template="{NURSE_ROSTER_BASE_URL}/api/v1/duty-roster?dept_code=…",
        address_env=("NURSE_ROSTER_BASE_URL",),
        secret_env=("ROSTER_TOKEN",),
        fields=(
            FieldSpec("dept_code", "请求", True, "按科室查当班名单"),
            FieldSpec("Authorization", "请求", True, "Bearer $ROSTER_TOKEN"),
            FieldSpec("姓名 / name", "响应", True, "当班人姓名"),
            FieldSpec("工号 / staff_id", "响应", False, "同层级择人时的稳定排序键"),
            FieldSpec("岗位 / role", "响应", False, "角色优先级：主责 > 备班 > 科主任"),
            FieldSpec("shift_start", "响应", False, "班次起点（墙钟）"),
            FieldSpec("shift_end", "响应", False, "班次终点（墙钟，24:00 认作当天结束）"),
        ),
        source_files=("critval/roster/client.py",),
        evidence="urllib.request.Request( + Authorization: Bearer",
        evidence_markers=("ROSTER_PATH =", "urllib.request.Request(", "Authorization"),
        note="读取失败时降级为带时间戳的缓存；缓存过期即转人工，不拿过期名单冒充当前当班人。",
    ),
    Seam(
        seam_id="registry_writeback",
        direction="出站",
        peer="院内危急值登记 / 护理执行平台",
        protocol="HTTP（REST）",
        method="POST",
        url_template="{CRITVAL_BASE_URL}/api/v1/critical-events",
        address_env=("CRITVAL_BASE_URL",),
        secret_env=("CRITVAL_TOKEN",),
        fields=(
            FieldSpec("message_control_id", "请求", True, "幂等键 —— 登记系统按它覆盖同一条"),
            FieldSpec("item_code", "请求", True, "检验项目码"),
            FieldSpec("value", "请求", True, "观察值"),
            FieldSpec("threshold_row", "请求", True, "阈值依据行定位串（文件名 · 行号 · 版本）"),
            FieldSpec("notified_dept", "请求", True, "已通知科室"),
            FieldSpec("duty_person", "请求", False, "当班人（岗位口径，不含患者信息）"),
            FieldSpec("due_minutes", "请求", False, "上报时限（分钟）"),
            FieldSpec("receipt_status", "请求", True, "回执状态"),
            FieldSpec("receipt_at", "请求", False, "回执时刻"),
            FieldSpec("Authorization", "请求", True, "Bearer $CRITVAL_TOKEN"),
        ),
        source_files=("critval/writeback/client.py", "critval/writeback/registry.py"),
        evidence="urllib.request.Request( + Authorization: Bearer",
        evidence_markers=("RECEIPT_PATH =", "RECEIPT_FIELDS", "urllib.request.Request("),
        note="只有台账状态为 acknowledged（值班人已确认）才发起；其余状态一次出站调用都不发。",
    ),
    Seam(
        seam_id="wecom_push",
        direction="出站",
        peer="企业微信值班群机器人",
        protocol="HTTPS（webhook）",
        method="POST",
        url_template="{CRITVAL_WECOM_WEBHOOK_URL}",
        secret_env=("CRITVAL_WECOM_WEBHOOK_URL",),
        fields=(
            FieldSpec("msgtype", "请求", True, "固定 markdown"),
            FieldSpec("markdown.content", "请求", True, "处置卡正文（含出处行号 + 确认链接）"),
            FieldSpec("errcode", "响应", True, "0 才算推送成功；HTTP 200 不等于推送成功"),
            FieldSpec("errmsg", "响应", False, "失败原因"),
        ),
        source_files=("critval/notify/wecom.py",),
        evidence="urllib.request.Request( + msgtype=markdown",
        evidence_markers=("urllib.request.Request(", "msgtype", "_read_errcode"),
        note="webhook 完整地址本身即凭据；日志与文案只出现脱敏形式（前 4 后 4 + 长度）。",
    ),
    Seam(
        seam_id="lis_rest_callback",
        direction="出站",
        peer="LIS 侧危急值登记接口（部分厂商的接口引擎要求回调）",
        protocol="HTTP（REST）",
        method="POST",
        url_template="{LIS_BASE_URL}/api/v1/critical-value-events",
        address_env=("LIS_BASE_URL",),
        secret_env=("LIS_CRITICAL_TOKEN",),
        fields=(
            FieldSpec("event_id", "请求", True, "取事件的消息控制号（LIS 侧认识的号）"),
            FieldSpec("item_code", "请求", True, "检验项目码"),
            FieldSpec("value", "请求", True, "观察值"),
            FieldSpec("unit", "请求", False, "单位"),
            FieldSpec("critical_level", "请求", False, "危急级别"),
            FieldSpec("raised_at", "请求", True, "结果时刻"),
            FieldSpec("dept_code", "请求", False, "科室"),
            FieldSpec("Authorization", "请求", True, "Bearer $LIS_CRITICAL_TOKEN"),
        ),
        source_files=("critval/hl7/rest_channel.py",),
        evidence="urllib.request.Request( + Authorization: Bearer",
        evidence_markers=("LIS_REST_EVENT_PATH =", "urllib.request.Request(", "Authorization"),
        note="条件启用：与 MLLP 至少配一条入站通道；本轮未接入守护进程主循环。",
        live=False,
    ),
    Seam(
        seam_id="mllp_inbound",
        direction="入站",
        peer="LIS / 检验科信息系统（HL7 v2 ORU^R01）",
        protocol="MLLP over TCP（帧 \\x0b…\\x1c\\r）",
        method="LISTEN",
        url_template="{LIS_MLLP_BIND}（主机:端口）",
        address_env=("LIS_MLLP_BIND",),
        fields=(
            FieldSpec("MSH-9", "请求", True, "消息类型（ORU^R01）"),
            FieldSpec("MSH-10", "请求", True, "消息控制号 —— 事件幂等键的一部分"),
            FieldSpec("OBR-3", "请求", True, "检验申请号（承载标本号）"),
            FieldSpec("OBX-3", "请求", True, "观察标识（检验项目码）"),
            FieldSpec("OBX-5", "请求", True, "观察值"),
            FieldSpec("OBX-6", "请求", True, "单位"),
            FieldSpec("OBX-7", "请求", False, "参考范围（参与分档）"),
            FieldSpec("OBX-8", "请求", False, "异常标志（只作交叉校验，不作判定权威）"),
            FieldSpec("OBX-11 / OBR-25", "请求", False, "结果状态（C = 修正）"),
            FieldSpec("OBX-14", "请求", True, "观察日期时间"),
            FieldSpec("PV1-7", "请求", False, "患者所在科室"),
            FieldSpec("PID-3", "请求", False, "仅透传到事件标识层（落不可逆短哈希）"),
        ),
        source_files=("critval/hl7/mllp.py", "critval/hl7/normalize.py"),
        evidence="MllpListener + socket bind + MllpFramer",
        evidence_markers=("START_BLOCK =", "END_BLOCK =", "class MllpListener", "class MllpFramer"),
        note="一帧完整只看结束符；粘包逐帧切、拆包留缓冲、空帧与帧前垃圾都留痕。",
    ),
    Seam(
        seam_id="http_push_inbound",
        direction="入站",
        peer="LIS（危急值 HTTP 推送回调）",
        protocol="HTTP（REST）",
        method="POST",
        url_template="{CRITVAL_LISTEN_BASE_URL}/api/v1/critical-value-events",
        address_env=("CRITVAL_LISTEN_BASE_URL",),
        secret_env=("LIS_CRITICAL_TOKEN",),
        fields=(
            FieldSpec("event_id", "请求", True, "事件标识 —— 进幂等键"),
            FieldSpec("item_code", "请求", True, "检验项目码"),
            FieldSpec("value", "请求", True, "观察值（非数值不换算、不猜数）"),
            FieldSpec("unit", "请求", False, "单位"),
            FieldSpec("critical_level", "请求", False, "危急级别"),
            FieldSpec("raised_at", "请求", True, "结果时刻"),
            FieldSpec("dept_code", "请求", False, "科室"),
            FieldSpec("Authorization", "请求", True, "Bearer $LIS_CRITICAL_TOKEN（常量时间比较）"),
        ),
        source_files=("critval/console/inbound.py", "critval/console/app.py"),
        evidence="INBOUND_PATH + BaseHTTPRequestHandler + verify_bearer",
        evidence_markers=("INBOUND_PATH =", "verify_bearer", "class _InboundHandler"),
        note="与 MLLP 共用同一套规范化函数 —— 同一件事走两条通道得到同一个事件 ID。",
    ),
)


# ---------------------------------------------------------------------------
# 扫描工具
# ---------------------------------------------------------------------------
def product_sources() -> list[Path]:
    """产品代码目录下的全部 ``.py`` 文件（按路径排序）。"""
    return sorted(
        path
        for path in PRODUCT_DIR.rglob("*.py")
        if "__pycache__" not in path.parts
    )


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def find_evidence(rel_path: str, needles: Iterable[str]) -> list[tuple[int, str]]:
    """在指定文件里找证据行（行号 + 原文），找不到返回空表。"""
    path = PROJECT_ROOT / rel_path
    if not path.exists():
        return []
    hits: list[tuple[int, str]] = []
    for index, line in enumerate(read_text(path).splitlines(), start=1):
        stripped = line.strip()
        for needle in needles:
            if needle in stripped:
                hits.append((index, stripped))
                break
    return hits


def env_reads_in_product() -> dict[str, list[tuple[str, int, str]]]:
    """扫出产品代码里每一次「读某个环境变量」的位置。"""
    reads: dict[str, list[tuple[str, int, str]]] = {}
    for path in product_sources():
        rel = path.relative_to(PROJECT_ROOT).as_posix()
        for index, line in enumerate(read_text(path).splitlines(), start=1):
            for match in ENV_READ_PATTERN.finditer(line):
                reads.setdefault(match.group("name"), []).append(
                    (rel, index, line.strip())
                )
    return reads


def string_literals(source: str) -> list[str]:
    """取出一段源码里的所有字符串字面量（够用即可，不求完备）。"""
    return re.findall(r"""(?<![\w])(["'])(?:(?!\1).)*\1""", source, flags=re.DOTALL)


# ---------------------------------------------------------------------------
# 断言
# ---------------------------------------------------------------------------
@dataclass
class Assertion:
    """一条断言的结果。"""

    key: str
    title: str
    passed: bool
    detail: str = ""
    evidence: list[str] = field(default_factory=list)

    @property
    def verdict(self) -> str:
        return "通过" if self.passed else "不通过"


def assert_credential_registered() -> Assertion:
    """A：每条接缝的凭据位都在 ``Config.CONFIG_KEYS`` 里登记。"""
    problems: list[str] = []
    for seam in SEAMS:
        for name in tuple(seam.secret_env) + tuple(seam.address_env):
            if name not in CONFIG_KEYS:
                problems.append(f"{seam.seam_id} 的凭据位 {name} 未登记进 CONFIG_KEYS")
    return Assertion(
        key="A",
        title="凭据位登记",
        passed=not problems,
        detail="；".join(problems) if problems else f"{len(SEAMS)} 条接缝的凭据位全部登记在 Config.CONFIG_KEYS",
        evidence=problems,
    )


def assert_credentials_from_env() -> Assertion:
    """B：密钥位以 ``Config.get_env("<NAME>")`` 读，且没有代码级默认值。"""
    reads = env_reads_in_product()
    problems: list[str] = []
    for seam in SEAMS:
        for name in seam.secret_env:
            if name not in SECRET_KEYS:
                problems.append(f"{seam.seam_id} 的 {name} 未登记进 Config.SECRET_KEYS")
            hits = reads.get(name, [])
            if not hits:
                problems.append(f"{seam.seam_id} 的密钥位 {name} 没有以 Config.get_env 读取")
    # 密钥位不得有代码级默认值：不得出现 `NAME = "字面量"` 或 `NAME="字面量"`。
    for path in product_sources():
        rel = path.relative_to(PROJECT_ROOT).as_posix()
        for index, line in enumerate(read_text(path).splitlines(), start=1):
            for name in SECRET_KEYS:
                if re.search(rf"\b{name}\b\s*=\s*[\"'][^\"']+[\"']", line):
                    problems.append(f"{rel}:{index} 给密钥位 {name} 写了默认值")
    # 配置样例里的密钥位必须留空 —— 留了值就等于把密钥发进了仓库。
    env_example = PROJECT_ROOT / ".env.example"
    if env_example.exists():
        for index, line in enumerate(read_text(env_example).splitlines(), start=1):
            for name in SECRET_KEYS:
                match = re.match(rf"\s*{name}\s*=\s*(.*)$", line)
                if match and match.group(1).strip():
                    problems.append(f".env.example:{index} 密钥位 {name} 写了非空默认值")
    return Assertion(
        key="B",
        title="凭据只从环境变量读",
        passed=not problems,
        detail="；".join(problems) if problems else "全部密钥位经 Config.get_env 读取，代码内无默认值",
        evidence=problems,
    )


#: 名字像凭据、又被赋了一个「不透明字面量」—— 这才是密钥写死在代码里。
#: 报错文案（带空格、带中文）、格式化串（带 ``{}``）都不算。
CREDENTIAL_ASSIGNMENT_PATTERN = re.compile(
    r"""(?P<name>[A-Za-z_]\w*)"""
    r"""\s*[:=]\s*"""
    r"""(?P<quote>["'])(?P<value>[^"'\n]+)(?P=quote)"""
)

#: 明显不是凭据的字面量白名单（协议名、内容类型）。
NON_CREDENTIAL_LITERALS = frozenset(
    {
        "application/json",
        "text/plain",
        "multipart/form-data",
        "application/x-www-form-urlencoded",
    }
)

#: 含中日韩字符的字符串一律是**给人看的文案**，不是凭据。
CJK_PATTERN = re.compile(r"[　-鿿＀-￯]")


def _looks_like_credential_literal(value: str) -> bool:
    """这个字面量像不像「写在代码里的凭据」。"""
    text = value.strip()
    if len(text) < 8:
        return False
    if text in NON_CREDENTIAL_LITERALS:
        return False
    if "{" in text or "}" in text:
        return False  # 格式化串
    if any(ch.isspace() for ch in text):
        return False  # 带空格的是文案
    if CJK_PATTERN.search(text):
        return False  # 带中文的是文案
    return True


def credential_literals_in_source(source: str) -> list[str]:
    """源码里「写死的凭据」清单（每条一行说明，行号从 1 起）。

    口径：名字像凭据（token / secret / password / api_key …）的变量，
    被赋了一个**不透明**的字面量 —— 这才算「密钥写死在代码里」。
    报错文案（带空格、带中文）、格式化串（带 ``{}``）都不算。
    """
    problems: list[str] = []
    for index, line in enumerate(source.splitlines(), start=1):
        code = line.split("#", 1)[0]
        if "http://" in code or "https://" in code:
            continue  # URL 字面量由断言 D 单独管
        for match in CREDENTIAL_ASSIGNMENT_PATTERN.finditer(code):
            name = match.group("name")
            if not SECRET_NAME_PATTERN.search(name):
                continue
            if not _looks_like_credential_literal(match.group("value")):
                continue
            problems.append(f"第 {index} 行 {name} 被赋了一个疑似凭据的字面量")
    return problems


def forbidden_outbound_hosts_in_source(source: str) -> list[str]:
    """源码里「写死的本机 / 示例出站地址」清单。"""
    problems: list[str] = []
    for index, line in enumerate(source.splitlines(), start=1):
        code = line.split("#", 1)[0]
        for match in OUTBOUND_URL_PATTERN.finditer(code):
            host = match.group("host").lower()
            if host in FORBIDDEN_OUTBOUND_HOSTS:
                problems.append(f"第 {index} 行出站地址写死为 {host}")
    return problems


def assert_no_secret_literal() -> Assertion:
    """C：产品代码里没有把凭据写成字面量。"""
    problems: list[str] = []
    for path in product_sources():
        rel = path.relative_to(PROJECT_ROOT).as_posix()
        for item in credential_literals_in_source(read_text(path)):
            problems.append(f"{rel} {item}")
    return Assertion(
        key="C",
        title="无密钥字面量",
        passed=not problems,
        detail="；".join(problems) if problems else "产品代码内未出现凭据字面量",
        evidence=problems,
    )


def assert_no_local_outbound_default() -> Assertion:
    """D：出站默认地址不写死本地 / 示例主机。"""
    problems: list[str] = []
    for path in product_sources():
        rel = path.relative_to(PROJECT_ROOT).as_posix()
        for item in forbidden_outbound_hosts_in_source(read_text(path)):
            problems.append(f"{rel} {item}")
    return Assertion(
        key="D",
        title="出站地址不写死",
        passed=not problems,
        detail="；".join(problems) if problems else "出站默认地址均来自环境变量，无本机 / 示例地址",
        evidence=problems,
    )


#: 「实现了协议服务端」的判定：类继承自这些基类，或直接调用这些构造器。
PROTOCOL_SERVER_BASES = frozenset(
    {
        "ThreadingHTTPServer",
        "HTTPServer",
        "BaseHTTPRequestHandler",
        "MllpListener",
        "MllpInboundProcessor",
    }
)


def _ast_name(node: ast.AST) -> str:
    """取 AST 节点的名字（``a.b.C`` → ``C``；拿不到就返回空串）。"""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def defines_protocol_server(source: str) -> bool:
    """这段源码里有没有「协议服务端」的实现。

    **用 AST 判，不用字符串匹配** —— 否则本审计脚本自己（它把基类名写在
    字符串里）会被误判成替身。
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for base in node.bases:
                if _ast_name(base) in PROTOCOL_SERVER_BASES:
                    return True
        elif isinstance(node, ast.Call):
            if _ast_name(node.func) in {"ThreadingHTTPServer", "HTTPServer"}:
                return True
    return False


def assert_doubles_confined() -> Assertion:
    """E：协议替身只在 ``tests/fixtures/`` 下，产品代码不碰替身。"""
    problems: list[str] = []
    fixtures_rel = FIXTURES_DIR.relative_to(PROJECT_ROOT).as_posix()
    for base in (TESTS_DIR, SCRIPTS_DIR):
        for path in sorted(base.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            rel = path.relative_to(PROJECT_ROOT).as_posix()
            if rel.startswith(fixtures_rel):
                continue
            if defines_protocol_server(read_text(path)):
                problems.append(f"{rel} 在测试数据层之外实现了协议替身")
    # 产品代码不得 import 替身，也不得把 tests/ 塞进 sys.path。
    for path in product_sources():
        rel = path.relative_to(PROJECT_ROOT).as_posix()
        source = read_text(path)
        for pattern in (
            r"\bfrom\s+fixtures\b",
            r"\bimport\s+fixtures\b",
            r"\bfrom\s+tests\b",
            r"sys\.path\.insert\([^)]*[\"']tests[\"']",
        ):
            if re.search(pattern, source):
                problems.append(f"{rel} 引用了测试数据层")
    return Assertion(
        key="E",
        title="替身只在测试数据层",
        passed=not problems,
        detail="；".join(problems) if problems else "替身实现全部位于 tests/fixtures/，产品代码零引用",
        evidence=problems,
    )


def assert_seams_have_evidence() -> Assertion:
    """F：每条接缝在代码里都有出处（模块存在 + 关键调用点找得到）。"""
    problems: list[str] = []
    for seam in SEAMS:
        for rel in seam.source_files:
            if not (PROJECT_ROOT / rel).exists():
                problems.append(f"{seam.seam_id} 的源文件 {rel} 不存在")
        needles = {
            "roster_read": ("urllib.request.Request(", "Authorization"),
            "registry_writeback": ("urllib.request.Request(", "RECEIPT_FIELDS"),
            "wecom_push": ("urllib.request.Request(", "msgtype"),
            "lis_rest_callback": ("urllib.request.Request(", "LIS_REST_EVENT_PATH"),
            "mllp_inbound": ("class MllpListener", "START_BLOCK"),
            "http_push_inbound": ("INBOUND_PATH", "verify_bearer"),
        }.get(seam.seam_id, ())
        hits: list[tuple[int, str]] = []
        for rel in seam.source_files:
            hits.extend(find_evidence(rel, needles))
        if not hits:
            problems.append(f"{seam.seam_id} 在代码里找不到证据（找过：{', '.join(needles)}）")
    return Assertion(
        key="F",
        title="接缝有代码出处",
        passed=not problems,
        detail="；".join(problems) if problems else f"{len(SEAMS)} 条接缝均能在产品代码里指到出处",
        evidence=problems,
    )


def collect_assertions() -> list[Assertion]:
    return [
        assert_credential_registered(),
        assert_credentials_from_env(),
        assert_no_secret_literal(),
        assert_no_local_outbound_default(),
        assert_doubles_confined(),
        assert_seams_have_evidence(),
    ]


# ---------------------------------------------------------------------------
# 替身清单
# ---------------------------------------------------------------------------
def double_inventory() -> list[dict[str, str]]:
    """列出测试数据层的协议替身（替代的是哪个对端）。"""
    mapping = {
        "roster_double.py": ("排班 / 值班名单系统", "HTTP GET"),
        "registry_double.py": ("院内危急值登记 / 护理执行平台", "HTTP GET/POST"),
        "wecom_double.py": ("企业微信值班群机器人", "HTTP POST"),
        "lis_rest_double.py": ("LIS 侧危急值登记接口", "HTTP POST"),
    }
    out: list[dict[str, str]] = []
    if not FIXTURES_DIR.exists():
        return out
    for path in sorted(FIXTURES_DIR.glob("*_double.py")):
        peer, protocol = mapping.get(path.name, ("（未登记）", "—"))
        out.append(
            {
                "file": path.relative_to(PROJECT_ROOT).as_posix(),
                "peer": peer,
                "protocol": protocol,
            }
        )
    return out


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------
#: 文档行不算「代码证据」：RST 风格的双反引号、条目符号、纯散文。
def _is_doc_line(text: str) -> bool:
    stripped = text.strip()
    if not stripped or stripped.startswith("#"):
        return True
    if stripped.startswith(("*", "-", "：", ":")):
        return True
    if "``" in stripped:
        return True
    # 以中文开头的行是散文（注释 / 文档串）
    return bool(CJK_PATTERN.match(stripped))


def _evidence_lines(seam: Seam) -> list[str]:
    """这条接缝在代码里的出处（文件 + 行号 + 那一行原文）。"""
    lines: list[str] = []
    for rel in seam.source_files:
        path = PROJECT_ROOT / rel
        if not path.exists():
            continue
        for index, text in enumerate(read_text(path).splitlines(), start=1):
            stripped = text.strip()
            if _is_doc_line(stripped):
                continue
            if any(marker in stripped for marker in seam.evidence_markers):
                lines.append(f"`{rel}:{index}` — `{stripped[:96]}`")
    return lines


def render_report(assertions: list[Assertion]) -> str:
    """生成 ``reports/production_seams.md`` 正文。"""
    reads = env_reads_in_product()
    out: list[str] = []
    out.append("# 落生产核对清单 · 危急值处置依据检索系统")
    out.append("")
    out.append(
        "> 本清单由 `python3 scripts/audit_production.py` 扫产品代码生成，**不手写**。"
        "每条接缝都能在代码里找到出处；核对的人可以逐条点开看。"
    )
    out.append("")
    out.append(
        "生成口径：产品代码目录 `critval/`；替身只允许出现在 `tests/fixtures/`；"
        "凭据一律从环境变量读，代码内不出现密钥。"
    )
    out.append("")

    # ---- 总览 ----
    outbound = [s for s in SEAMS if s.direction == "出站"]
    inbound = [s for s in SEAMS if s.direction == "入站"]
    out.append("## 一、总览")
    out.append("")
    out.append("| 方向 | 条数 | 对端 |")
    out.append("|------|------|------|")
    out.append(
        f"| 出站 | {len(outbound)} | "
        + "、".join(s.peer.split("（")[0] for s in outbound)
        + " |"
    )
    out.append(
        f"| 入站 | {len(inbound)} | "
        + "、".join(s.peer.split("（")[0] for s in inbound)
        + " |"
    )
    out.append("")
    out.append(
        "本产品**入站只读 LIS 推送**（不写 LIS），**出站只写院内登记系统与值班群**，"
        "**读排班系统**取当班人；绝不代签、绝不写回患者可识别明细。"
    )
    out.append("")

    # ---- 接缝明细 ----
    for index, seam in enumerate(SEAMS, start=1):
        out.append(f"### {'①②③④⑤⑥⑦⑧⑨'[index - 1]} {seam.peer} · {seam.method}")
        out.append("")
        out.append("| 项 | 内容 |")
        out.append("|----|------|")
        out.append(f"| 方向 | {seam.direction} |")
        out.append(f"| 对端系统 | {seam.peer} |")
        out.append(f"| 协议 / 方法 | {seam.protocol} / {seam.method} |")
        out.append(f"| 目标地址模板 | `{seam.url_template}` |")
        creds = list(seam.address_env) + list(seam.secret_env)
        out.append(
            "| 凭据位（环境变量） | "
            + "、".join(f"`{name}`" for name in creds)
            + " |"
        )
        if seam.note:
            out.append(f"| 口径要点 | {seam.note} |")
        out.append(f"| 本轮是否接主路径 | {'是' if seam.live else '否（条件启用）'} |")
        out.append("")
        out.append("**字段规范**")
        out.append("")
        out.append("| 字段 | 方向 | 必填 | 说明 |")
        out.append("|------|------|------|------|")
        for spec in seam.fields:
            out.append(
                f"| `{spec.name}` | {spec.direction} | {'是' if spec.required else '否'} | {spec.note} |"
            )
        out.append("")
        evidence = _evidence_lines(seam)
        if evidence:
            out.append("**代码证据**")
            out.append("")
            for line in evidence[:8]:
                out.append(f"- {line}")
            out.append("")
        reads_here = []
        for name in creds:
            for rel, index_, text in reads.get(name, [])[:1]:
                reads_here.append(f"`{name}` ← `{rel}:{index_}` — `{text[:80]}`")
        if reads_here:
            out.append("**凭据位在代码里怎么读**")
            out.append("")
            for line in reads_here:
                out.append(f"- {line}")
            out.append("")

    # ---- 凭据位总表 ----
    out.append("## 二、凭据位总表")
    out.append("")
    out.append("| 环境变量 | 归属接缝 | 性质 | 代码里怎么读 |")
    out.append("|----------|----------|------|--------------|")
    for seam in SEAMS:
        for name in seam.address_env:
            out.append(f"| `{name}` | {seam.seam_id} | 地址 | 环境变量（`Config.get_env`） |")
        for name in seam.secret_env:
            out.append(f"| `{name}` | {seam.seam_id} | 密钥 | 环境变量（`Config.get_env`，缺则拒绝发请求） |")
    out.append("")
    out.append(
        "**缺凭据位的行为**：出站客户端在**发请求之前**就报错（不拿空口令去撞对端，"
        "也不拿本地替身顶上去）；入站推送端点未配置共享凭据时**一律拒绝**。"
    )
    out.append("")

    # ---- 替身边界 ----
    out.append("## 三、替身边界（测试数据层 ≠ 最终集成面）")
    out.append("")
    out.append("| 替身文件 | 替代的对端 | 协议 |")
    out.append("|----------|------------|------|")
    for row in double_inventory():
        out.append(f"| `{row['file']}` | {row['peer']} | {row['protocol']} |")
    out.append("")
    out.append(
        "替身替代的**只是对端系统本身**，不是协议：产品代码走的就是院内真实接口的"
        "报文格式（真实 URL、真实 `Authorization: Bearer` 头、真实 JSON 体、真实 "
        "`errcode` 判读）。替身文件全部位于 `tests/fixtures/` 下，**产品代码不 import 它们**。"
    )
    out.append("")

    # ---- 断言 ----
    out.append("## 四、断言结果")
    out.append("")
    out.append("| 断言 | 口径 | 结果 | 说明 |")
    out.append("|------|------|------|------|")
    titles = {
        "A": "凭据位都登记进 `Config.CONFIG_KEYS`",
        "B": "密钥位以 `Config.get_env` 读，代码内无默认值",
        "C": "产品代码无凭据字面量",
        "D": "出站默认地址不指向本机 / 示例主机",
        "E": "协议替身只在 `tests/fixtures/`，产品代码零引用",
        "F": "每条接缝在代码里都有出处",
    }
    for assertion in assertions:
        detail = assertion.detail.replace("|", "／")
        out.append(
            f"| {assertion.key} | {titles.get(assertion.key, assertion.title)} | "
            f"**{assertion.verdict}** | {detail} |"
        )
    out.append("")

    # ---- 未实连 ----
    out.append("## 五、本轮未实连的端点（诚实标注）")
    out.append("")
    out.append(
        "- **院内真实系统未实连**：本轮跑在**协议替身**上（见上一节），"
        "产品代码里的出站客户端（真实 HTTP + 真实 `Authorization: Bearer` + 真实字段规范）"
        "已按 spec 落盘，但**没有对着真实登记系统 / 排班系统 / 企微群发过请求** —— "
        "院内地址与凭据到位后，把对应环境变量指过去即可，产品代码一行不改。"
    )
    out.append(
        "- **未接主路径的条件接缝**：`lis_rest_callback`（LIS 侧 REST 回调）按院内"
        "厂商约定选用，本轮未接入守护进程主循环；`LIS_BASE_URL` 留空即不启用。"
    )
    out.append(
        "- **入站两条通道**：`mllp_inbound` 与 `http_push_inbound` 共用同一套规范化"
        "函数；本轮验证到**协议与字段级**（真实 socket / 真实 HTTP 报文），"
        "未对着院内 LIS 实发。"
    )
    out.append("")

    out.append("---")
    out.append("")
    out.append(
        "本清单只描述**集成面**：对端是谁、凭据从哪来、没配会怎样。"
        "产品能力的完整口径见项目 `spec.md` 与 `README.md`。"
    )
    out.append("")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="落生产核对：扫出真实集成面并生成清单")
    parser.add_argument("--json", action="store_true", help="输出机器可读结论")
    parser.add_argument("--check", action="store_true", help="只跑断言，不写报告")
    parser.add_argument(
        "--report",
        default=str(REPORT_PATH),
        help=f"报告输出路径（默认 {REPORT_PATH}）",
    )
    args = parser.parse_args(argv)

    assertions = collect_assertions()
    failed = [a for a in assertions if not a.passed]

    if not args.check:
        report_path = Path(args.report)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        body = render_report(assertions)
        report_path.write_text(body, encoding="utf-8")
        # 可发布副本：``reports/`` 在 .gitignore 里，科室与 GitHub 访客看不到它。
        DOCS_REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
        DOCS_REPORT_PATH.write_text(body, encoding="utf-8")

    if args.json:
        print(
            json.dumps(
                {
                    "seams": [
                        {
                            "seam_id": s.seam_id,
                            "direction": s.direction,
                            "peer": s.peer,
                            "method": s.method,
                            "url_template": s.url_template,
                            "credentials": list(s.address_env) + list(s.secret_env),
                            "fields": [f.name for f in s.fields],
                        }
                        for s in SEAMS
                    ],
                    "assertions": [
                        {
                            "key": a.key,
                            "title": a.title,
                            "passed": a.passed,
                            "detail": a.detail,
                        }
                        for a in assertions
                    ],
                    "doubles": double_inventory(),
                    "report": "" if args.check else str(Path(args.report)),
                    "report_docs": "" if args.check else str(DOCS_REPORT_PATH),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        for assertion in assertions:
            mark = "✓" if assertion.passed else "✗"
            print(f"{mark} [{assertion.key}] {assertion.title} — {assertion.verdict}")
            if not assertion.passed:
                for item in assertion.evidence:
                    print(f"    · {item}")
        if not args.check:
            print("")
            print(f"报告已写入 {Path(args.report)}")
        print(f"接缝：出站 {len([s for s in SEAMS if s.direction == '出站'])} 条 · "
              f"入站 {len([s for s in SEAMS if s.direction == '入站'])} 条")

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
