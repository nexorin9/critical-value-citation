#!/usr/bin/env python3
"""公开数据获取层：抓取 → 合并 → 仿真降级 → 生成语料 → 落 manifest。

一条命令跑完整条数据管道：

    python3 scripts/fetch_data.py                 # 正常跑（联网可抓则抓，抓不到自动降级）
    python3 scripts/fetch_data.py --offline       # 强制离线（等价于院内无外网环境）
    python3 scripts/fetch_data.py --quick         # 小规模体检（测试用）

产物分两层：
  samples/  随仓库提交的小样例（阈值表 xlsx、制度 docx、3 条 ER7 帧）
  data/     可再生的大语料（已 gitignore，随时用本脚本重建）

断网降级：任何来源抓取失败都不抛栈退出，改走合成器，并在 manifest 中标注
source=synthetic 与降级原因。测试替身只出现在 tests/fixtures/，不是最终集成面。
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import build_materials  # noqa: E402
import gen_synth  # noqa: E402
from _catalog import ANALYTES, direction, reference_range_text  # noqa: E402
from _datapipe import (  # noqa: E402
    OFFLINE_ENV,
    PROJECT_ROOT,
    Source,
    download,
    sha256_file,
    write_json,
)

HL7_LICENSE_URL = "https://hl7.org/fhir/license.html"
SYNTHEA_LICENSE_URL = "https://github.com/synthetichealth/synthea"

# 公开数据源登记表：URL / 许可证 / 许可证链接三件套齐全，落 manifest 可复核
SOURCES = [
    Source(
        key="hl7_fhir_observation_example_json",
        url="https://hl7.org/fhir/R4/observation-example.json",
        filename="observation-example.json",
        license="HL7® FHIR® R4 官方发布示例（许可见 https://hl7.org/fhir/license.html）",
        license_url=HL7_LICENSE_URL,
        kind="json",
        note="官方 Observation 示例，含真实 referenceRange 区间",
    ),
    Source(
        key="hl7_fhir_observation_example_xml",
        url="https://hl7.org/fhir/R4/observation-example.xml",
        filename="observation-example.xml",
        license="HL7® FHIR® R4 官方发布示例（许可见 https://hl7.org/fhir/license.html）",
        license_url=HL7_LICENSE_URL,
        kind="xml",
        note="同上，XML 形态",
    ),
    Source(
        key="hl7_fhir_bundle_example_json",
        url="https://hl7.org/fhir/R4/bundle-example.json",
        filename="bundle-example.json",
        license="HL7® FHIR® R4 官方发布示例（许可见 https://hl7.org/fhir/license.html）",
        license_url=HL7_LICENSE_URL,
        kind="json",
        note="官方示例集合包",
    ),
    Source(
        key="hl7_v2_sample_messages",
        url=(
            "https://raw.githubusercontent.com/guijealth/hl7v2/master/"
            "test/hl7v2/data/oru-r01.hl7"
        ),
        filename="hl7-v2-sample.hl7",
        license=(
            "HL7 v2.5.1 样例报文（经 hl7v2 开源解析库的测试数据公开流传，"
            "该库以 MIT 许可发布；报文本身取自 HL7 v2.5.1 规范示例）"
        ),
        license_url="https://www.hl7.org/copyright/",
        kind="hl7",
        note=(
            "官方 ER7 样例报文（ORU^R01）：协议对拍基线 —— "
            "去标识副本随仓库提交于 samples/hl7/官方样例/"
        ),
    ),
    Source(
        key="synthea_release_csv",
        url="https://github.com/synthetichealth/synthea/releases",
        filename="synthea-release-index.html",
        license="Synthea 公共发行包（代码 MIT，合成数据 CC0）",
        license_url=SYNTHEA_LICENSE_URL,
        kind="release-index",
        note="Synthea CSV 导出的发行入口；资产不可达时改用配置化仿真器",
    ),
]

REFERENCE_CSV_COLUMNS = [
    "项目码", "项目名", "英文名", "单位", "参考区间下界", "参考区间上界", "参考区间",
    "低危急值", "高危急值", "危急方向", "来源", "来源备注",
]


# ---------------------------------------------------------------------------
# 参考区间样例集：公开文献整理种子 + 抓取到的公开样例合并
# ---------------------------------------------------------------------------
def write_public_reference_csv(dest: Path) -> dict:
    """公开文献整理的参考区间与危急值阈值种子表（随管道重新生成，不入库）。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    for item in ANALYTES:
        rows.append({
            "项目码": item["code"],
            "项目名": item["name_zh"],
            "英文名": item["name_en"],
            "单位": item["unit"],
            "参考区间下界": "" if item["ref_low"] is None else "{:g}".format(item["ref_low"]),
            "参考区间上界": "" if item["ref_high"] is None else "{:g}".format(item["ref_high"]),
            "参考区间": reference_range_text(item),
            "低危急值": "" if item["crit_low"] is None else "{:g}".format(item["crit_low"]),
            "高危急值": "" if item["crit_high"] is None else "{:g}".format(item["crit_high"]),
            "危急方向": direction(item),
            "来源": item["source"],
            "来源备注": item["source_note"],
        })
    with open(dest, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=REFERENCE_CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    return {"path": str(dest), "rows": len(rows), "sha256": sha256_file(dest)}


def _walk_reference_ranges(node, found):
    """在任意 JSON 结构里找 referenceRange（FHIR 示例结构不固定）。"""
    if isinstance(node, dict):
        if "referenceRange" in node and isinstance(node["referenceRange"], list):
            code = ""
            coding = (node.get("code") or {}).get("coding") or []
            if coding and isinstance(coding[0], dict):
                code = coding[0].get("code") or ""
            for entry in node["referenceRange"]:
                low = (entry.get("low") or {}).get("value")
                high = (entry.get("high") or {}).get("value")
                unit = ((entry.get("low") or entry.get("high") or {}).get("unit")) or ""
                if low is not None or high is not None:
                    found.append({"code": code, "low": low, "high": high, "unit": unit})
        for value in node.values():
            _walk_reference_ranges(value, found)
    elif isinstance(node, list):
        for item in node:
            _walk_reference_ranges(item, found)


def harvest_reference_intervals(raw_dir: Path, dest: Path) -> dict:
    """把抓到的 HL7 官方示例里的 referenceRange 并入参考区间样例集。"""
    harvested = []
    unmapped = []
    example = raw_dir / "hl7_fhir_observation_example_json" / "observation-example.json"
    if example.exists():
        try:
            payload = json.loads(example.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = None
        if payload is not None:
            found = []
            _walk_reference_ranges(payload, found)
            known = {item["code"]: item for item in ANALYTES}
            for entry in found:
                spec = known.get(entry["code"])
                if spec is None:
                    unmapped.append(entry)
                    continue
                harvested.append({
                    "项目码": spec["code"],
                    "项目名": spec["name_zh"],
                    "英文名": spec["name_en"],
                    "单位": spec["unit"],
                    "参考区间下界": "" if entry["low"] is None else "{:g}".format(entry["low"]),
                    "参考区间上界": "" if entry["high"] is None else "{:g}".format(entry["high"]),
                    "参考区间": reference_range_text({
                        "ref_low": entry["low"], "ref_high": entry["high"]}),
                    "低危急值": "" if spec["crit_low"] is None else "{:g}".format(spec["crit_low"]),
                    "高危急值": "" if spec["crit_high"] is None else "{:g}".format(spec["crit_high"]),
                    "危急方向": direction(spec),
                    "来源": "hl7-fhir-example",
                    "来源备注": "HL7 官方 Observation 示例给出的 referenceRange",
                })

    # 合并：种子表为主，抓到的官方区间若同项目码且更窄则补记到来源备注
    seed_path = dest.parent / "reference_intervals_public.csv"
    merged = []
    with open(seed_path, encoding="utf-8", newline="") as handle:
        merged = list(csv.DictReader(handle))
    by_code = {row["项目码"]: row for row in merged}
    for row in harvested:
        current = by_code.get(row["项目码"])
        if current is None:
            merged.append(row)
            by_code[row["项目码"]] = row
        else:
            current["来源备注"] = "{}；{}".format(
                current.get("来源备注", ""), row["来源备注"]).strip("；")

    with open(dest, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=REFERENCE_CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(merged)

    return {
        "path": str(dest),
        "rows": len(merged),
        "harvested_from_public_example": len(harvested),
        "unmapped_example_codes": [entry["code"] for entry in unmapped],
        "sha256": sha256_file(dest),
    }


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def _abs(root: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else root / path


def run(args) -> dict:
    root = PROJECT_ROOT
    data_dir = _abs(root, args.data_dir)
    raw_dir = data_dir / "raw"
    out_dir = _abs(root, args.out_dir)
    reference_csv = _abs(root, args.reference_csv)
    manifest_path = _abs(root, args.manifest)
    threshold_out = _abs(root, args.threshold_out)
    policy_out = _abs(root, args.policy_out)
    corpus_out = _abs(root, args.corpus_out)
    samples_hl7 = _abs(root, args.samples_hl7)

    if args.offline:
        os.environ[OFFLINE_ENV] = "1"

    started = time.time()
    manifest = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "pipeline": "scripts/fetch_data.py",
        "offline_forced": bool(args.offline) or os.environ.get(OFFLINE_ENV, "") == "1",
        "sources": [],
        "artifacts": [],
        "scale": {},
        "degradation": [],
    }

    # ---- 1. 抓取公开数据 ----
    for source in SOURCES:
        dest = raw_dir / source.key / source.filename
        cached = dest.exists() and dest.stat().st_size > 0
        if cached and not args.refresh:
            ok, reason = True, "cache_hit"
        else:
            ok, reason = download(source, dest)
        record = source.to_record(dest, ok, reason)
        record["usage"] = _usage_note(source, ok)
        record["cache_hit"] = bool(cached and ok and not args.refresh)
        if record["cache_hit"]:
            record["degradation_reason"] = "cache_hit:复用 data/raw 本地缓存（--refresh 可强制重抓）"
        if not ok:
            record["source"] = "unreachable"
            manifest["degradation"].append({
                "key": source.key,
                "url": source.url,
                "reason": reason,
                "fallback": _fallback_for(source.key),
            })
        manifest["sources"].append(record)
        print("[fetch_data] {:<36} {}".format(
            source.key,
            "已抓取" if ok and not record["cache_hit"]
            else ("缓存命中" if record["cache_hit"] else "不可达（{}）".format(reason))))

    # ---- 2. 参考区间样例集 ----
    seed_info = write_public_reference_csv(raw_dir / "reference_intervals_public.csv")
    harvest_info = harvest_reference_intervals(raw_dir, reference_csv)
    manifest["artifacts"].append(seed_info)
    manifest["artifacts"].append(harvest_info)

    # ---- 3. Synthea CSV：抓到的原始 CSV 放 raw/，仿真产出的放 synth/，两者不混 ----
    downloaded_synthea = raw_dir / "synthea_csv"
    synth_synthea = data_dir / "synth" / "synthea_csv"
    specs = build_materials.load_specs(reference_csv)
    if (downloaded_synthea / "observations.csv").exists():
        synthea_dir = downloaded_synthea
        source_info = {"source": "synthea-csv", "synthea_dir": str(synthea_dir)}
    else:
        synthea_dir = synth_synthea
        source_info = gen_synth.simulate_synthea_csv(
            synthea_dir, specs, args.patients, args.min_obs, args.max_obs,
            args.critical_ratio, args.correction_ratio, args.seed)
        manifest["degradation"].append({
            "key": "synthea_release_csv",
            "url": SOURCES[4].url,
            "reason": "发行包 CSV 资产不可达",
            "fallback": "配置化仿真器产出同 schema 的 Synthea CSV（source=synthetic-simulator）",
        })
    manifest["synthea"] = source_info

    # ---- 4. ORU^R01 帧 ----
    oru_manifest = gen_synth.csv_to_oru(synthea_dir, out_dir, specs, args.frames_per_file, samples_hl7)
    oru_manifest.update(source_info)
    write_json(out_dir / "manifest.json", oru_manifest)
    manifest["oru"] = {key: value for key, value in oru_manifest.items() if key != "files"}
    manifest["artifacts"].append({"path": str(out_dir / "manifest.json"),
                                  "sha256": sha256_file(out_dir / "manifest.json")})

    # ---- 5. 材料：阈值表 xlsx + 制度 docx + 大语料 ----
    threshold_info = build_materials.build_threshold_xlsx(
        specs, threshold_out, args.effective_date, args.version)
    policy_info = build_materials.build_policy_docx(
        specs, policy_out, args.effective_date, args.version)
    corpus_info = build_materials.build_policy_corpus(specs, corpus_out, args.policy_lines)
    manifest["artifacts"].extend([threshold_info, policy_info, corpus_info])

    # ---- 6. ER7 样例帧（随仓库提交的小样例） ----
    sample_frames = []
    if samples_hl7.exists():
        for path in sorted(samples_hl7.glob("*.hl7")):
            try:
                sample_frames.append(str(path.relative_to(root)))
            except ValueError:
                sample_frames.append(str(path))
    manifest["sample_frames"] = sample_frames

    # ---- 7. 规模与清单 ----
    manifest["scale"] = {
        "oru_frames": oru_manifest["frames"],
        "patients": oru_manifest["patients"],
        "observations": oru_manifest["observations"],
        "critical_candidates": oru_manifest["critical_candidates"],
        "corrections": oru_manifest["corrections"],
        "policy_corpus_lines": corpus_info["lines"],
        "threshold_rows": threshold_info["rows"],
        "policy_chapters": policy_info["chapters"],
        "items": len(specs),
        "departments": len(gen_synth.DEPARTMENTS),
    }
    manifest["elapsed_seconds"] = round(time.time() - started, 2)
    manifest["regenerate"] = "python3 scripts/fetch_data.py"

    write_json(manifest_path, manifest)

    scale = manifest["scale"]
    print("[fetch_data] 帧={} 患者={} 危急值候选={} 语料行={} → {}".format(
        scale["oru_frames"], scale["patients"], scale["critical_candidates"],
        scale["policy_corpus_lines"], manifest_path))
    return manifest


def _fallback_for(key: str) -> str:
    fallbacks = {
        "hl7_v2_sample_messages": (
            "随仓库提交的去标识官方样例报文（samples/hl7/官方样例/）"
            "与 4 条样例帧（samples/hl7/）"
        ),
        "synthea_release_csv": "配置化仿真器产出同 schema 的 Synthea CSV",
        "hl7_fhir_observation_example_json": "公开文献整理的参考区间种子表",
        "hl7_fhir_observation_example_xml": "公开文献整理的参考区间种子表",
        "hl7_fhir_bundle_example_json": "公开文献整理的参考区间种子表",
    }
    return fallbacks.get(key, "合成降级路径")


def _usage_note(source: Source, ok: bool) -> str:
    """说明这一份抓取物在本管道里实际怎么用，避免"抓到入口页 = 拿到数据集"的误读。"""
    if not ok:
        return "未取得，已走降级路径：{}".format(_fallback_for(source.key))
    if source.key == "synthea_release_csv":
        return "仅取得发行索引页；CSV 导出资产需访问发行 CDN，本环境不可达 → 走仿真器"
    if source.key.startswith("hl7_fhir"):
        return "已并入参考区间样例集（data/raw/reference_intervals.csv）"
    return source.note


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="公开数据获取层（可重跑 · 断网降级）")
    parser.add_argument("--offline", action="store_true", help="强制离线（等价 CRITVAL_OFFLINE=1）")
    parser.add_argument("--refresh", action="store_true", help="忽略 data/raw 本地缓存，强制重新抓取")
    parser.add_argument("--quick", action="store_true", help="小规模体检，用于测试")
    parser.add_argument("--patients", type=int, default=320)
    parser.add_argument("--min-obs", type=int, default=12)
    parser.add_argument("--max-obs", type=int, default=18)
    parser.add_argument("--critical-ratio", type=float, default=0.07)
    parser.add_argument("--correction-ratio", type=float, default=0.03)
    parser.add_argument("--policy-lines", type=int, default=3600)
    parser.add_argument("--frames-per-file", type=int, default=200)
    parser.add_argument("--seed", type=int, default=20260101)
    parser.add_argument("--effective-date", default=build_materials.DEFAULT_EFFECTIVE_DATE)
    parser.add_argument("--version", default=build_materials.DEFAULT_VERSION)
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--out-dir", default="data/synth/oru")
    parser.add_argument("--reference-csv", default="data/raw/reference_intervals.csv")
    parser.add_argument("--threshold-out", default="samples/threshold/示例危急值阈值表.xlsx")
    parser.add_argument("--policy-out", default="samples/policy/示例危急值报告制度.docx")
    parser.add_argument("--corpus-out", default="data/synth/policy_corpus.txt")
    parser.add_argument("--samples-hl7", default="samples/hl7")
    parser.add_argument("--manifest", default="data/manifest.json")
    args = parser.parse_args(argv)

    if args.quick:
        args.patients = 8
        args.min_obs = 2
        args.max_obs = 3
        args.policy_lines = 120
        args.frames_per_file = 20
        args.offline = True

    run(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
