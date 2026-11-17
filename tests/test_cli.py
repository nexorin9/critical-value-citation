"""运维 CLI 骨架的验收（任务 id 13）。

运维通道有六个子命令：**ingest / bind / probe / replay / export / bench**。
这一组命令**不承担主路径**（主路径是常驻守护进程），提供的是：

* 材料入库出**解析报告**（未识别的行不静默跳过，逐行列出来）；
* 试检索回放（参与召回的材料版本 + 候选、得分与出处）；
* 批量重放（断点续跑 + 幂等去重）；
* 处置卡快照导出与复核；
* 版本绑定表；
* 入站吞吐基准。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "tests"))

from critval.cli import EXIT_BUSINESS, EXIT_OK, EXIT_USAGE, main  # noqa: E402
from critval.ledger.store import LedgerStore  # noqa: E402

THRESHOLD = PROJECT_ROOT / "samples" / "threshold" / "示例危急值阈值表.xlsx"
POLICY = PROJECT_ROOT / "samples" / "policy" / "示例危急值报告制度.docx"
DIRTY_TABLE = PROJECT_ROOT / "tests" / "fixtures" / "脱敏样例阈值表.xlsx"
FRAME = PROJECT_ROOT / "samples" / "hl7" / "示例ORU_R01_多项目.hl7"


def _run(*argv: str) -> int:
    return main(list(argv))


# ---------------------------------------------------------------------------
# 子命令齐备
# ---------------------------------------------------------------------------


def test_六个运维子命令都在():
    from critval.cli import _COMMANDS

    for name in ("ingest", "bind", "probe", "replay", "export", "bench"):
        assert name in _COMMANDS, f"缺子命令 {name}"


def test_六个运维子命令的帮助都能打出来(capsys):
    for name in ("ingest", "bind", "probe", "replay", "export", "bench"):
        with pytest.raises(SystemExit) as excinfo:
            main([name, "--help"])
        assert excinfo.value.code == 0
        assert name in capsys.readouterr().out


# ---------------------------------------------------------------------------
# ingest：材料入库出解析报告
# ---------------------------------------------------------------------------


def test_ingest_阈值表出解析报告(capsys):
    code = _run("ingest", "--threshold", str(THRESHOLD))
    assert code == EXIT_OK
    out = capsys.readouterr().out
    assert "阈值表" in out
    assert "识别" in out


def test_ingest_未识别的行逐行列出来不静默跳过(capsys):
    code = _run("ingest", "--threshold", str(DIRTY_TABLE))
    assert code == EXIT_OK
    out = capsys.readouterr().out
    assert "未识别" in out
    # 脏表里有缺项目码的行与缺阈值的行 —— 两类都要点名到行号
    assert "未识别 第" in out
    assert "字段缺失 第" in out


def test_ingest_阈值表_JSON输出可解析(capsys):
    code = _run("ingest", "--threshold", str(THRESHOLD), "--json")
    assert code == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert "threshold" in payload
    assert payload["threshold"]["recognized_rows"] >= 1
    assert "unrecognized_rows" in payload["threshold"]


def test_ingest_制度出条款清单(capsys):
    code = _run("ingest", "--policy", str(POLICY))
    assert code == EXIT_OK
    out = capsys.readouterr().out
    assert "制度" in out
    assert "条款" in out


def test_ingest_不给来源时报用法错误(capsys):
    code = _run("ingest")
    assert code == EXIT_USAGE
    assert "至少给一个来源" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# probe：试检索回放
# ---------------------------------------------------------------------------


def test_probe_给出参与召回的材料与候选出处(capsys):
    code = _run("probe", "--item-code", "718-7", "--query", "血红蛋白危急值上报时限")
    assert code == EXIT_OK
    out = capsys.readouterr().out
    assert "试检索回放" in out
    assert "参与召回的材料版本" in out
    assert "示例危急值阈值表.xlsx" in out
    assert "第" in out  # 出处带可读行号


def test_probe_JSON输出可解析且带命中路(capsys):
    code = _run("probe", "--item-code", "718-7", "--query", "上报时限", "--json")
    assert code == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["item_code"] == "718-7"
    assert "hits" in payload
    assert "materials" in payload


def test_probe_召回不到依据时明说不编造(capsys):
    # 查询词与材料没有任何词面重合 → 融合分全被硬闸门挡下 → 一条依据都不产出
    code = _run("probe", "--item-code", "9999-9", "--query", "qqqqzzxx")
    assert code == EXIT_OK
    out = capsys.readouterr().out
    assert "未召回到依据" in out


# ---------------------------------------------------------------------------
# replay：批量重放（幂等 + 续跑）
# ---------------------------------------------------------------------------


def test_replay_批量灌进台账且重复跑幂等(capsys, tmp_path):
    frames_dir = tmp_path / "frames"
    frames_dir.mkdir()
    (frames_dir / "a.hl7").write_bytes(FRAME.read_bytes())
    db = str(tmp_path / "replay.db")

    code = _run(
        "replay",
        "--frames",
        str(frames_dir),
        "--threshold",
        str(THRESHOLD),
        "--db",
        db,
    )
    assert code == EXIT_OK
    out = capsys.readouterr().out
    assert "新增事件 2" in out

    # 第二遍：同一批帧 —— 幂等键挡下，台账不增加
    code = _run(
        "replay",
        "--frames",
        str(frames_dir),
        "--threshold",
        str(THRESHOLD),
        "--db",
        db,
    )
    assert code == EXIT_OK
    out = capsys.readouterr().out
    assert "新增事件 0" in out
    assert "幂等跳过 2" in out

    store = LedgerStore(db)
    try:
        assert store.count_events() == 2
    finally:
        store.close()


def test_replay_JSON输出可解析(capsys, tmp_path):
    frames_dir = tmp_path / "frames"
    frames_dir.mkdir()
    (frames_dir / "a.hl7").write_bytes(FRAME.read_bytes())
    code = _run(
        "replay",
        "--frames",
        str(frames_dir),
        "--threshold",
        str(THRESHOLD),
        "--db",
        str(tmp_path / "replay.db"),
        "--json",
    )
    assert code == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["frames_seen"] == 1
    assert payload["events_new"] == 2
    assert payload["throughput"] >= 0


def test_replay_目录里没有帧时报用法错误(capsys, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    code = _run("replay", "--frames", str(empty), "--db", str(tmp_path / "x.db"))
    assert code == EXIT_USAGE
    assert "没有可重放" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# bind / export：版本绑定与处置卡导出
# ---------------------------------------------------------------------------


def test_bind_能列出版本绑定表(capsys):
    code = _run("bind", "--list")
    assert code == EXIT_OK
    assert "绑定表" in capsys.readouterr().out


def test_export_查不到快照时返回业务失败码(capsys, tmp_path):
    code = _run(
        "export",
        "--event",
        "evt-not-exist",
        "--db",
        str(tmp_path / "empty.db"),
    )
    assert code == EXIT_BUSINESS
    assert "没有处置卡快照" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# bench：吞吐基准
# ---------------------------------------------------------------------------


def test_bench_跑出吞吐与峰值内存(capsys, tmp_path):
    frames_dir = tmp_path / "frames"
    frames_dir.mkdir()
    (frames_dir / "a.hl7").write_bytes(FRAME.read_bytes())
    code = _run(
        "bench",
        "--frames",
        str(frames_dir),
        "--db",
        str(tmp_path / "bench.db"),
        "--mode",
        "ingest",
    )
    assert code == EXIT_OK
    out = capsys.readouterr().out
    assert "吞吐" in out and "帧/s" in out
