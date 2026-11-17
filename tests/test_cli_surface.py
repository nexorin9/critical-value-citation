"""运维 CLI 面的验收（任务 id 47）。

这一组用例守的是**运维通道的契约**，不是某个业务功能：

* **子命令注册表统一管理** —— 每个子命令的 ``--help`` 都能打出来，
  参数不再散落在解析器的一长串 ``if name == ...`` 分支里；
* **统一 ``--json`` 输出约定** —— stdout 上只有一个 JSON 文档，可被
  ``json.loads`` 直接解析，且顶层固定带 ``command`` / ``ok`` / ``exit_code``；
* **统一退出码约定** —— ``0`` 成功 / ``1`` 业务异常 / ``2`` 参数错；
* **批处理续跑** —— ``ingest --dir`` 记游标，中断后重跑从断点继续；
* **试检索回放** —— ``probe --item <code> --value <v>`` 给出候选、得分、
  出处，并按同一份阈值表试判这条观察值。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from critval.cli import EXIT_BUSINESS, EXIT_OK, EXIT_USAGE, main  # noqa: E402
from critval.cli_registry import RESERVED_JSON_KEYS  # noqa: E402
from critval.cli_specs import COMMAND_ARG_SPECS  # noqa: E402
from critval.ledger.store import LedgerStore  # noqa: E402

THRESHOLD = PROJECT_ROOT / "samples" / "threshold" / "示例危急值阈值表.xlsx"
POLICY = PROJECT_ROOT / "samples" / "policy" / "示例危急值报告制度.docx"
FRAME = PROJECT_ROOT / "samples" / "hl7" / "示例ORU_R01_多项目.hl7"

#: 任务 id 47 点名的运维子命令（对应 id 36 / 45 / 20 / 27 / 38 / 43 / 39 / 40 的产物）。
NAMED_COMMANDS = (
    "judge-replay",
    "trace",
    "config-check",
    "parse-queue",
    "reconcile",
    "writeback-retry",
    "doctor",
    "audit-verify",
)


def _run(*argv: str) -> int:
    return main(list(argv))


def _all_command_names() -> list[str]:
    from critval.cli import _COMMANDS

    return list(_COMMANDS)


# ---------------------------------------------------------------------------
# 子命令注册表
# ---------------------------------------------------------------------------


def test_点名的运维子命令都在注册表里():
    from critval.cli import _COMMANDS

    for name in NAMED_COMMANDS:
        assert name in _COMMANDS, f"缺子命令 {name}"


def test_注册表与参数表一一对应():
    """每个登记的子命令都得有参数表条目（框架据此生成解析器）。"""
    from critval.cli import _COMMANDS

    for name in _COMMANDS:
        assert name in COMMAND_ARG_SPECS, f"{name} 没有登记参数"


def test_每个子命令的_help都能打出来(capsys):
    """全部子命令逐个验一遍 —— 少一个 help 都算不合格。"""
    for name in _all_command_names():
        with pytest.raises(SystemExit) as excinfo:
            main([name, "--help"])
        assert excinfo.value.code == 0, f"{name} --help 退出码不是 0"
        out = capsys.readouterr().out
        assert name in out, f"{name} --help 输出里没有自己的名字"


def test_总帮助列出全部子命令(capsys):
    with pytest.raises(SystemExit):
        main(["--help"])
    out = capsys.readouterr().out
    for name in _all_command_names():
        assert name in out, f"总帮助里少了 {name}"


def test_不给子命令时按参数错退出(capsys):
    assert _run() == EXIT_USAGE
    assert "子命令" in capsys.readouterr().out


def test_未知子命令按参数错退出():
    with pytest.raises(SystemExit) as excinfo:
        main(["no-such-command"])
    assert excinfo.value.code == EXIT_USAGE


def test_缺必填参数按参数错退出():
    """``--threshold`` 是必填的 —— 缺了就该报参数错（argparse 自己报 2）。"""
    with pytest.raises(SystemExit) as excinfo:
        main(["rules-summary"])
    assert excinfo.value.code == EXIT_USAGE


# ---------------------------------------------------------------------------
# 统一 --json 输出约定
# ---------------------------------------------------------------------------


def _json_payload(capsys) -> dict:
    out = capsys.readouterr().out
    return json.loads(out)


def test_json文档带三个保留键(capsys):
    code = _run("rules-summary", "--threshold", str(THRESHOLD), "--json")
    assert code == EXIT_OK
    payload = _json_payload(capsys)
    for key in RESERVED_JSON_KEYS:
        assert key in payload, f"--json 输出缺保留键 {key}"
    assert payload["command"] == "rules-summary"
    assert payload["ok"] is True
    assert payload["exit_code"] == EXIT_OK


def test_json输出里exit_code与实际退出码一致(capsys):
    """退出码与文档里的 ``exit_code`` 必须是同一个数 —— 脚本才敢只看其中一个。"""
    code = _run("config-check", "--json")
    payload = _json_payload(capsys)
    assert payload["exit_code"] == code
    assert payload["ok"] == (code == EXIT_OK)


def test_业务异常时json标ok为假(capsys):
    """配置不齐是业务异常：退出码 1，文档里 ``ok`` 也要是 False。"""
    code = _run("config-check", "--json")
    payload = _json_payload(capsys)
    assert code == EXIT_BUSINESS
    assert payload["ok"] is False
    assert payload["exit_code"] == EXIT_BUSINESS


def test_json输出只有一个文档不含额外文字(capsys):
    """stdout 上混了别的字，``json.loads`` 就会炸 —— 这里守住「只有一个文档」。"""
    _run("probe", "--item", "718-7", "--query", "上报时限", "--json")
    out = capsys.readouterr().out
    json.loads(out)  # 能整体解析即达标
    assert out.strip().startswith("{")
    assert out.strip().endswith("}")


@pytest.mark.parametrize(
    "argv",
    [
        ("rules-summary", "--threshold", str(THRESHOLD)),
        ("version-check", "--threshold", str(THRESHOLD)),
        ("policy-summary", "--policy", str(POLICY)),
        ("ingest", "--threshold", str(THRESHOLD)),
        ("probe", "--item", "718-7"),
        ("bind", "--list"),
        ("recall", "--threshold", str(THRESHOLD), "--policy", str(POLICY)),
        ("recall-merge", "--threshold", str(THRESHOLD), "--policy", str(POLICY)),
        ("recall-conflict", "--threshold", str(THRESHOLD), "--policy", str(POLICY)),
        ("recall-fallback", "--threshold", str(THRESHOLD), "--policy", str(POLICY)),
        ("policy-diff", "--old", str(POLICY), "--new", str(POLICY)),
    ],
)
def test_各子命令的_json输出都能被解析(argv, capsys):
    code = _run(*argv, "--json")
    payload = _json_payload(capsys)
    assert isinstance(payload, dict)
    assert payload.get("command") == argv[0]
    assert payload["exit_code"] == code


def test_parse_queue的_json两种写法等价(capsys):
    """``parse-queue --json list`` 与 ``parse-queue list --json`` 等价。"""
    code = _run("parse-queue", "--json", "list")
    first = _json_payload(capsys)
    assert code == EXIT_OK
    assert first["command"] == "parse-queue"

    code = _run("parse-queue", "list", "--json")
    second = _json_payload(capsys)
    assert code == EXIT_OK
    assert second["command"] == "parse-queue"
    assert "items" in second


# ---------------------------------------------------------------------------
# 批处理模式：ingest --dir + 断点续跑
# ---------------------------------------------------------------------------


def test_ingest_目录批处理写入游标并可续跑(capsys, tmp_path):
    """批处理模式：第一次落游标，第二次从游标继续（不重复灌）。"""
    frames_dir = tmp_path / "frames"
    frames_dir.mkdir()
    (frames_dir / "a.hl7").write_bytes(FRAME.read_bytes())
    db = str(tmp_path / "batch.db")
    cursor = str(tmp_path / "cursor.json")

    code = _run(
        "ingest",
        "--dir",
        str(frames_dir),
        "--threshold",
        str(THRESHOLD),
        "--db",
        db,
        "--cursor",
        cursor,
    )
    assert code == EXIT_OK
    out = capsys.readouterr().out
    assert "断点续跑游标" in out
    assert Path(cursor).exists(), "游标文件没落盘"

    store = LedgerStore(db)
    try:
        first_count = store.count_events()
    finally:
        store.close()
    assert first_count >= 1

    # 第二遍：游标生效 → 续跑跳过全部帧，台账不增加
    code = _run(
        "ingest",
        "--dir",
        str(frames_dir),
        "--threshold",
        str(THRESHOLD),
        "--db",
        db,
        "--cursor",
        cursor,
    )
    assert code == EXIT_OK
    out = capsys.readouterr().out
    assert "续跑跳过" in out

    store = LedgerStore(db)
    try:
        assert store.count_events() == first_count, "续跑不该重复落事件"
    finally:
        store.close()


def test_ingest_批处理默认启用游标(capsys, tmp_path):
    """``--dir`` 不给 ``--cursor`` 时默认用 data/ingest_cursor.json。"""
    frames_dir = tmp_path / "frames"
    frames_dir.mkdir()
    (frames_dir / "a.hl7").write_bytes(FRAME.read_bytes())
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkey = tmp_path  # noqa: F841 —— 只是占位，实际 chdir 在下面用 monkeypatch
    import os

    old = os.getcwd()
    os.chdir(cwd)
    try:
        code = _run(
            "ingest",
            "--dir",
            str(frames_dir),
            "--threshold",
            str(THRESHOLD),
            "--db",
            str(tmp_path / "batch2.db"),
            "--json",
        )
    finally:
        os.chdir(old)
    assert code == EXIT_OK
    payload = _json_payload(capsys)
    cursor_info = payload["frames"]["cursor"]
    assert cursor_info["enabled"] is True
    assert cursor_info["path"].endswith("ingest_cursor.json")
    assert (cwd / "data" / "ingest_cursor.json").exists()


def test_ingest_不给来源时按参数错退出(capsys):
    assert _run("ingest") == EXIT_USAGE
    assert "至少给一个来源" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# 试检索回放：probe --item / --value
# ---------------------------------------------------------------------------


def test_probe_给出候选得分与出处(capsys):
    code = _run("probe", "--item", "718-7", "--query", "血红蛋白危急值上报时限")
    assert code == EXIT_OK
    out = capsys.readouterr().out
    assert "试检索回放" in out
    assert "参与召回的材料版本" in out
    assert "示例危急值阈值表.xlsx" in out
    assert "最终分" in out


def test_probe_带观察值时给出试判结论与阈值依据(capsys):
    """给了观察值就按同一份阈值表试判，并给出命中的阈值行出处。"""
    code = _run("probe", "--item", "718-7", "--value", "25 g/dL", "--json")
    assert code == EXIT_OK
    payload = _json_payload(capsys)
    judgement = payload["judgement"]
    assert judgement["critical"] is True
    assert judgement["decision"] == "critical"
    assert judgement["threshold_row"], "试判必须给出阈值依据行"
    assert "示例危急值阈值表.xlsx" in judgement["threshold_row"]
    assert "第" in judgement["threshold_row"]


def test_probe_观察值带单位和不带单位都能解析(capsys):
    code = _run("probe", "--item", "718-7", "--value", "25000 mg/dL", "--json")
    assert code == EXIT_OK
    payload = _json_payload(capsys)
    assert payload["judgement"]["critical"] is True
    assert payload["judgement"]["unit"] == "mg/dL"


def test_probe_单位换不了时不判定并转人工(capsys):
    """缺换算即不判定 —— 绝不猜一个系数。"""
    code = _run("probe", "--item", "718-7", "--value", "25 mmol/L", "--json")
    payload = _json_payload(capsys)
    assert code == EXIT_OK
    judgement = payload["judgement"]
    assert judgement["critical"] is False
    assert judgement["judged"] is False
    assert judgement["requires_human_review"] is True
    assert judgement["decision"] == "unit_unknown"
    assert code == EXIT_OK


def test_probe_阈值表没收录该项目码时不判定(capsys):
    code = _run("probe", "--item", "9999-9", "--value", "5", "--json")
    payload = _json_payload(capsys)
    assert code == EXIT_OK
    judgement = payload["judgement"]
    assert judgement["decision"] == "no_threshold"
    assert judgement["critical"] is False
    assert judgement["requires_human_review"] is True


def test_probe_观察值写不成数字时不判定(capsys):
    code = _run("probe", "--item", "718-7", "--value", "abc", "--json")
    payload = _json_payload(capsys)
    assert code == EXIT_OK
    judgement = payload["judgement"]
    assert judgement["decision"] == "not_a_number"
    assert judgement["requires_human_review"] is True


def test_probe_不给观察值就不试判(capsys):
    code = _run("probe", "--item", "718-7", "--json")
    assert code == EXIT_OK
    payload = _json_payload(capsys)
    assert "judgement" not in payload


def test_probe_判为危急却没召回到制度条款时报业务异常(capsys):
    """危险状态：判了危急却一条制度条款都没召回 —— 处置卡会缺依据。

    检索式与制度毫无词面重合时，词法路召回不到任何条款；结构化路只命中
    阈值行（那是「判危急」的依据，不是「怎么处置」的依据）。
    """
    code = _run(
        "probe",
        "--item",
        "718-7",
        "--value",
        "25 g/dL",
        "--query",
        "qqqqzzxx",
        "--json",
    )
    payload = _json_payload(capsys)
    assert payload["judgement"]["critical"] is True
    kinds = {hit["citation"]["kind"] for hit in payload["hits"]}
    assert "policy_clause" not in kinds, "本用例前提：没召回到任何制度条款"
    assert code == EXIT_BUSINESS
    assert payload["ok"] is False
    assert payload["exit_code"] == EXIT_BUSINESS


def test_probe_召回得到制度条款时退出码为成功(capsys):
    """默认检索式能召回制度条款 —— 此时不该报业务异常。"""
    code = _run("probe", "--item", "718-7", "--value", "25 g/dL", "--json")
    payload = _json_payload(capsys)
    kinds = {hit["citation"]["kind"] for hit in payload["hits"]}
    assert "policy_clause" in kinds
    assert code == EXIT_OK
    assert payload["ok"] is True
