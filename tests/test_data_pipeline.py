"""数据获取层测试：manifest 完整性、sha256 校验、断网降级、规模目标。

这些用例不依赖外网：一律以 ``--offline`` 跑管道，验证"抓不到也能产出可跑语料"。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FETCH_SCRIPT = PROJECT_ROOT / "scripts" / "fetch_data.py"


def _run_pipeline(workspace: Path, extra_args=None):
    """在独立工作目录里离线跑一次数据管道，返回 (manifest, stdout)。"""
    args = [
        sys.executable, str(FETCH_SCRIPT),
        "--offline",
        "--data-dir", str(workspace / "data"),
        "--out-dir", str(workspace / "data" / "synth" / "oru"),
        "--reference-csv", str(workspace / "data" / "raw" / "reference_intervals.csv"),
        "--manifest", str(workspace / "data" / "manifest.json"),
        "--threshold-out", str(workspace / "samples" / "threshold" / "示例危急值阈值表.xlsx"),
        "--policy-out", str(workspace / "samples" / "policy" / "示例危急值报告制度.docx"),
        "--corpus-out", str(workspace / "data" / "synth" / "policy_corpus.txt"),
        "--samples-hl7", str(workspace / "samples" / "hl7"),
    ]
    if extra_args:
        args.extend(extra_args)
    completed = subprocess.run(
        args, cwd=str(PROJECT_ROOT), capture_output=True, text=True, timeout=900)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    manifest_path = workspace / "data" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    return manifest, completed.stdout


@pytest.fixture(scope="module")
def offline_pipeline(tmp_path_factory):
    workspace = tmp_path_factory.mktemp("datapipe")
    manifest, stdout = _run_pipeline(workspace)
    return {"workspace": workspace, "manifest": manifest, "stdout": stdout}


# ---------------------------------------------------------------------------
# manifest 完整性
# ---------------------------------------------------------------------------
def test_manifest_has_all_sections(offline_pipeline):
    manifest = offline_pipeline["manifest"]
    for key in ("generated_at", "pipeline", "offline_forced", "sources", "artifacts",
                "scale", "degradation"):
        assert key in manifest, "manifest 缺少字段 {}".format(key)


def test_every_source_records_url_license_and_reason(offline_pipeline):
    sources = offline_pipeline["manifest"]["sources"]
    assert len(sources) >= 5
    for record in sources:
        assert record["key"]
        assert record["url"].startswith("https://")
        assert record["license"]
        assert record["license_url"].startswith("https://")
        assert record["reachable"] in (True, False)
        assert record["source"] in ("downloaded", "unreachable")
        if not record["reachable"]:
            assert record["degradation_reason"], "不可达的来源必须写降级原因"


def test_manifest_scale_block_is_complete(offline_pipeline):
    scale = offline_pipeline["manifest"]["scale"]
    for key in ("oru_frames", "patients", "critical_candidates", "policy_corpus_lines",
                "threshold_rows", "items"):
        assert key in scale
        assert isinstance(scale[key], int)
        assert scale[key] > 0


# ---------------------------------------------------------------------------
# sha256 校验
# ---------------------------------------------------------------------------
def test_artifacts_record_matching_sha256(offline_pipeline):
    manifest = offline_pipeline["manifest"]
    checked = 0
    for artifact in manifest["artifacts"]:
        if "sha256" not in artifact:
            continue
        path = Path(artifact["path"])
        assert path.exists(), "清单登记的产物不存在：{}".format(path)
        import hashlib
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        assert digest == artifact["sha256"], "sha256 与清单不一致：{}".format(path)
        checked += 1
    assert checked >= 3, "至少应登记 3 个带 sha256 的产物"


def test_sha256_detects_corrupted_file(tmp_path):
    sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
    from _datapipe import sha256_file, verify_sha256

    target = tmp_path / "payload.bin"
    target.write_bytes(b"critical-value-demo")
    expected = sha256_file(target)
    assert verify_sha256(target, expected) is True
    target.write_bytes(b"critical-value-demo-corrupted")
    assert verify_sha256(target, expected) is False
    assert verify_sha256(tmp_path / "missing.bin", expected) is False


# ---------------------------------------------------------------------------
# 断网降级
# ---------------------------------------------------------------------------
def test_offline_run_marks_sources_unreachable_with_reason(offline_pipeline):
    manifest = offline_pipeline["manifest"]
    assert manifest["offline_forced"] is True
    unreachable = [record for record in manifest["sources"] if not record["reachable"]]
    assert unreachable, "离线运行时全部来源都应标记为不可达"
    for record in unreachable:
        assert "offline" in record["degradation_reason"] or record["degradation_reason"]
    assert manifest["degradation"], "离线运行必须留下降级说明"


def test_offline_run_still_produces_usable_corpus(offline_pipeline):
    workspace = offline_pipeline["workspace"]
    manifest = offline_pipeline["manifest"]
    assert manifest["synthea"]["source"] == "synthetic-simulator"
    assert manifest["scale"]["oru_frames"] > 0
    assert manifest["scale"]["policy_corpus_lines"] > 0

    frames = sorted((workspace / "data" / "synth" / "oru").glob("part-*.hl7"))
    assert frames, "离线运行也必须产出 ORU 帧文件"
    payload = frames[0].read_text(encoding="utf-8")
    assert "MSH|" in payload and "ORU^R01" in payload
    assert "OBR|" in payload and "OBX|" in payload

    corpus = (workspace / "data" / "synth" / "policy_corpus.txt").read_text(encoding="utf-8")
    assert corpus.count("\n") > 0
    assert "通知对象" in corpus and "上报时限" in corpus


def test_offline_env_var_forces_offline_without_cli_flag(tmp_path, monkeypatch):
    monkeypatch.setenv("CRITVAL_OFFLINE", "1")
    workspace = tmp_path / "env-offline"
    manifest, _ = _run_pipeline(workspace, extra_args=["--patients", "6", "--min-obs", "2",
                                                       "--max-obs", "3", "--policy-lines", "80"])
    assert manifest["offline_forced"] is True
    assert all(record["reachable"] is False for record in manifest["sources"])
    assert manifest["scale"]["oru_frames"] > 0


# ---------------------------------------------------------------------------
# 规模目标（真实规模：≥4000 帧 / ≥300 患者 / ≥200 危急值候选 / ≥3000 行语料）
# ---------------------------------------------------------------------------
def test_scale_targets_met(offline_pipeline):
    scale = offline_pipeline["manifest"]["scale"]
    assert scale["oru_frames"] >= 4000, "ORU 帧数不足：{}".format(scale["oru_frames"])
    assert scale["patients"] >= 300, "患者数不足：{}".format(scale["patients"])
    assert scale["critical_candidates"] >= 200, \
        "危急值候选数不足：{}".format(scale["critical_candidates"])
    assert scale["policy_corpus_lines"] >= 3000, \
        "制度与阈值语料行数不足：{}".format(scale["policy_corpus_lines"])


# ---------------------------------------------------------------------------
# 随仓库提交的小样例
# ---------------------------------------------------------------------------
def test_committed_samples_exist_and_parse():
    threshold = PROJECT_ROOT / "samples" / "threshold" / "示例危急值阈值表.xlsx"
    policy = PROJECT_ROOT / "samples" / "policy" / "示例危急值报告制度.docx"
    frames = sorted((PROJECT_ROOT / "samples" / "hl7").glob("*.hl7"))
    assert threshold.exists() and policy.exists()
    assert len(frames) == 4, "应提交 4 条 ER7 样例帧"
    assert (PROJECT_ROOT / "samples" / "hl7" / "示例ORU_R01_多项目.hl7").exists()

    from openpyxl import load_workbook
    workbook = load_workbook(threshold)
    sheet = workbook.active
    header = [cell.value for cell in sheet[1]]
    for column in ("项目码", "项目名", "单位", "低危急值", "高危急值", "参考区间",
                   "生效日期", "版本号"):
        assert column in header, "阈值表缺少列：{}".format(column)
    assert sheet.max_row >= 20

    from docx import Document
    document = Document(policy)
    text = "\n".join(paragraph.text for paragraph in document.paragraphs)
    assert "危急值" in text
    assert "通知对象" in text and "上报时限" in text and "记录去向" in text


def test_gitignore_excludes_regenerated_corpus():
    ignore_text = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "data/" in ignore_text
    assert "reports/" in ignore_text
    assert "qrcode.jpg" not in ignore_text.replace("# 注意：qrcode.jpg 是仓库资源，不在忽略范围内。", "")
