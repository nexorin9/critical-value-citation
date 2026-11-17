#!/usr/bin/env python3
"""Synthea CSV → HL7 v2 ORU^R01 帧生成器。

主路径（唯一）：Synthea CSV 导出（patients / observations / encounters）→ ER7 ORU^R01 帧。
断网降级：上游 Synthea 发行包不可达时，先用配置化仿真器产出同 schema 的 CSV，
再走同一条转换路径 —— 转换代码不区分数据来源，产物在 manifest 中标注 source=synthetic。

输出：
  data/synth/oru/part-XXXX.hl7   ER7 帧文件（帧间以 \\x1c\\r 分隔，段间 \\r）
  data/synth/oru/manifest.json   帧数 / 患者数 / 项目分布 / 危急值候选数 / 修正帧数
  samples/hl7/*.hl7              随仓库提交的 4 份样例帧（正常 / 缺字段 / 修正结果 / 多项目）

用法：
  python3 scripts/gen_synth.py --simulate --patients 320 --out-dir data/synth/oru
  python3 scripts/gen_synth.py --synthea-dir data/raw/synthea_csv --out-dir data/synth/oru
"""

from __future__ import annotations

import argparse
import csv
import random
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _catalog import DEPARTMENTS, load_specs  # noqa: E402
from _datapipe import PROJECT_ROOT, sha256_file, write_json, write_text  # noqa: E402

SEGMENT_SEP = "\r"
FILE_SEP = "\x1c\r"
HL7_ESCAPE = "\\"
ENCODING_CHARS = "^~" + HL7_ESCAPE + "&"

SENDER_APP = "SYNTHETIC_LIS"
SENDER_FACILITY = "SYNTH_HOSP_A"
RECEIVING_APP = "CRITVAL"

# 项目抽样权重：血常规与生化高频项目出现更多，贴近真实门诊量
SAMPLE_WEIGHTS = {
    "6690-2": 4, "777-3": 4, "718-7": 4, "2823-3": 4, "2951-2": 3,
    "2345-7": 4, "2160-0": 4, "3094-0": 3, "26499-4": 3, "6768-6": 2,
    "17861-6": 2, "2075-0": 2, "1742-6": 2, "1920-8": 2, "1975-2": 2,
    "2157-6": 2, "10839-9": 2, "10841-5": 2, "5902-2": 3, "14982-3": 2,
    "3255-7": 2, "2601-3": 1, "789-8": 2, "8480-6": 3, "8867-4": 3,
    "2708-6": 3, "8331-1": 2,
}

BASE_DATE = (2026, 1, 1)


# ---------------------------------------------------------------------------
# ER7 段构造
# ---------------------------------------------------------------------------
def _pad(fields, size):
    fields = list(fields)
    if len(fields) < size:
        fields += [""] * (size - len(fields))
    return fields[:size]


def build_msh(timestamp: str, control_id: str) -> str:
    """MSH-1 为字段分隔符本身，MSH-10 为消息控制号（事件幂等键）。"""
    tail = [
        ENCODING_CHARS,          # MSH-2 编码字符
        SENDER_APP,              # MSH-3
        SENDER_FACILITY,         # MSH-4
        RECEIVING_APP,           # MSH-5
        SENDER_FACILITY,         # MSH-6
        timestamp,               # MSH-7
        "",                      # MSH-8
        "ORU^R01",               # MSH-9 消息类型
        control_id,              # MSH-10 消息控制号
        "P",                     # MSH-11
        "2.5.1",                 # MSH-12
        "",                      # MSH-13
        "",                      # MSH-14
        "AL",                    # MSH-15
        "NE",                    # MSH-16
        "CHN",                   # MSH-17
    ]
    return "MSH|" + "|".join(tail)


def build_pid(patient_id: str, birth_date: str, gender: str) -> str:
    """PID-3 只带院内标识，不含姓名 / 身份证 / 住院号。"""
    fields = ["1", patient_id, patient_id, "", "", "", birth_date, gender]
    return "PID|" + "|".join(_pad(fields, 30))


def build_pv1(dept: dict, visit_no: str) -> str:
    """PV1-3 记录科室位置；PV1-7 按院内口径记录患者所在科室。"""
    location = "{}^{}^{}".format(dept["name"], dept["code"], dept["name"])
    dept_field = "{}^{}".format(dept["name"], dept["code"])
    fields = ["1", "I", location, "E"]
    fields += [""] * 2              # PV1-5 / PV1-6
    fields.append(dept_field)       # PV1-7
    fields += [""] * 11             # PV1-8 .. PV1-18
    fields.append(visit_no)         # PV1-19
    return "PV1|" + "|".join(_pad(fields, 50))


def build_obr(spec: dict, obs_ts: str, filler_id: str, placer_id: str, result_status: str,
              omit_universal_id: bool = False) -> str:
    """OBR-3 检验申请号（= 标本号）；OBR-4 通用检验标识（兜底匹配）；OBR-25 结果状态。

    ``omit_universal_id=True`` 时 OBR-4 也留空 —— 用来造「既没有 OBX-3
    也没有 OBR-4」真正缺字段的样例帧（这类帧才会落 unparsed 转人工；
    只缺 OBX-3 但有 OBR-4 的帧会走兜底匹配，进台账并标低置信）。
    """
    universal = "" if omit_universal_id else "{}^{}^{}".format(
        spec["code"], spec["name_zh"], spec["code"]
    )
    fields = ["1", placer_id, filler_id, universal]
    fields += [""]                  # OBR-5 优先级
    fields.append(obs_ts)           # OBR-6 申请时间
    fields.append(obs_ts)           # OBR-7 观察时间
    fields += [""] * 6              # OBR-8 .. OBR-13
    fields.append(obs_ts)           # OBR-14 标本接收时间
    fields += [""] * 10             # OBR-15 .. OBR-24
    fields.append(result_status)    # OBR-25 结果状态（C = 修正）
    return "OBR|" + "|".join(_pad(fields, 25))


def build_obx(spec: dict, value: str, unit: str, ref_text: str, flag: str, obs_ts: str,
              omit_code: bool = False, set_id: str = "1") -> str:
    """OBX-3 项目码；omit_code=True 时留空，用于「缺项目码」的样例帧。

    ``set_id`` 是 OBX-1（结果序号）—— 一条报文挂多个 OBX 时依次递增。
    """
    identifier = "" if omit_code else "{}^{}^{}".format(spec["code"], spec["name_zh"], spec["code"])
    fields = [set_id, "NM", identifier, "1", value, unit, ref_text, flag, "", "", "F", "", "", obs_ts]
    return "OBX|" + "|".join(_pad(fields, 14))


def abnormal_flag(value: float, spec: dict) -> str:
    """OBX-8 异常标志：仅作筛选与交叉校验提示，不是判定权威。"""
    low = spec.get("ref_low")
    high = spec.get("ref_high")
    if low is not None and value < low:
        return "LL" if (spec.get("crit_low") is not None and value < spec["crit_low"]) else "L"
    if high is not None and value > high:
        return "HH" if (spec.get("crit_high") is not None and value > spec["crit_high"]) else "H"
    return "N"


#: 多项目样例帧用的一对危急项目（同一条标本挂两个 OBX）：
#: ``(项目码, 危急方向)`` —— 钾走高危急、钠走低危急，**两个方向都覆盖到**。
MULTI_ITEM_SPECS: tuple[tuple[str, str], ...] = (("2823-3", "high"), ("2951-2", "low"))


def build_multi_item_frame(specs: list, timestamp: str, control_id: str,
                           specimen_id: str, order_id: str) -> str:
    """一条标本、两个危急项目的样例帧（任务 id 34 的合并通知场景）。

    同一份标本（**一条 OBR**）挂两个 OBX：钾高危急 + 钠低危急 —— 这正是
    「一条标本一张卡」要解决的现场：同一份血样两个项目同时越限，值班群
    不该收到两条通知，但两项依据都得看得见。

    取值由**阈值口径**推出来（高于高危急值 / 低于低危急值），阈值改了这条
    样例帧跟着变 —— 不会出现"样例不再危急"的死样例。
    """
    by_code = {spec["code"]: spec for spec in specs}
    picks = []
    for code, direction in MULTI_ITEM_SPECS:
        spec = by_code.get(code)
        if spec is None:
            continue
        if direction == "high" and spec.get("crit_high") is not None:
            picks.append((spec, round(spec["crit_high"] * 1.06, 1)))
        elif direction == "low" and spec.get("crit_low") is not None:
            picks.append((spec, round(spec["crit_low"] * 0.98, 1)))
    if len(picks) < 2:
        return ""

    dept = {"name": "心脏内科", "code": "CARD"}
    segments = [
        build_msh(timestamp, control_id),
        build_pid("SYN-PAT-000001", "19470110", "F"),
        build_pv1(dept, "V0000005"),
        build_obr(picks[0][0], timestamp, specimen_id, order_id, "F"),
    ]
    for index, (spec, value) in enumerate(picks, start=1):
        segments.append(
            build_obx(
                spec,
                "{:g}".format(value),
                spec["unit"],
                spec.get("ref_text", ""),
                abnormal_flag(value, spec),
                timestamp,
                set_id=str(index),
            )
        )
    return SEGMENT_SEP.join(segments) + SEGMENT_SEP


# ---------------------------------------------------------------------------
# 仿真：产出 Synthea CSV schema 子集
# ---------------------------------------------------------------------------
def _hl7_timestamp(year: int, month: int, day: int, hour: int, minute: int, second: int) -> str:
    return "{:04d}{:02d}{:02d}{:02d}{:02d}{:02d}".format(year, month, day, hour, minute, second)


def _sample_value(rng: random.Random, spec: dict, critical_ratio: float) -> float:
    low = spec.get("ref_low")
    high = spec.get("ref_high")
    crit_low = spec.get("crit_low")
    crit_high = spec.get("crit_high")
    roll = rng.random()

    if roll < critical_ratio and (crit_low is not None or crit_high is not None):
        sides = []
        if crit_low is not None:
            sides.append("low")
        if crit_high is not None:
            sides.append("high")
        side = rng.choice(sides)
        if side == "low":
            span = abs(crit_low) if crit_low else 1.0
            value = crit_low - span * rng.uniform(0.05, 0.6) - 0.01
        else:
            span = abs(crit_high) if crit_high else 1.0
            value = crit_high + span * rng.uniform(0.05, 0.6) + 0.01
        return round(max(value, 0.0), 2)

    if roll < critical_ratio + 0.22:
        # 异常但未达危急值：考验"按阈值表判定"与"仅看 OBX-8"的分歧
        options = []
        if low is not None:
            options.append("low")
        if high is not None:
            options.append("high")
        if not options:
            return round(rng.uniform(0.0, 10.0), 2)
        side = rng.choice(options)
        if side == "low":
            span = abs(low) if low else 1.0
            value = low - span * rng.uniform(0.03, 0.25) - 0.01
        else:
            span = abs(high) if high else 1.0
            value = high + span * rng.uniform(0.03, 0.25) + 0.01
        return round(max(value, 0.0), 2)

    if low is None and high is None:
        return round(rng.uniform(0.0, 10.0), 2)
    if low is None:
        return round(max(high - abs(high) * rng.uniform(0.0, 0.3), 0.0), 2)
    if high is None:
        return round(max(low + abs(low) * rng.uniform(0.0, 0.3), 0.0), 2)
    return round(rng.uniform(low, high), 2)


def simulate_synthea_csv(out_dir: Path, specs: list, patients: int, min_obs: int, max_obs: int,
                         critical_ratio: float, correction_ratio: float, seed: int) -> dict:
    """生成 Synthea CSV 导出 schema 子集（patients / encounters / observations）。

    仿真器只负责"造 Synthea 格式的输入"，转换路径与真实 Synthea CSV 完全一致。
    """
    rng = random.Random(seed)
    out_dir.mkdir(parents=True, exist_ok=True)

    codes = [spec["code"] for spec in specs]
    weights = [SAMPLE_WEIGHTS.get(code, 1) for code in codes]

    patient_rows = []
    encounter_rows = []
    observation_rows = []

    for p_index in range(1, patients + 1):
        patient_id = "SYN-PAT-{:06d}".format(p_index)
        gender = "F" if p_index % 2 == 0 else "M"
        birth_year = rng.randint(1945, 2005)
        birth_date = "{:04d}{:02d}{:02d}".format(birth_year, rng.randint(1, 12), rng.randint(1, 28))
        patient_rows.append({
            "Id": patient_id,
            "FIRST_NAME": "SYNTHETIC",
            "LAST_NAME": "PAT{:06d}".format(p_index),
            "GENDER": gender,
            "BIRTHDATE": birth_date,
            "RACE": "synthetic",
            "ETHNICITY": "synthetic",
        })

        dept = DEPARTMENTS[(p_index - 1) % len(DEPARTMENTS)]
        encounter_id = "SYN-ENC-{:06d}".format(p_index)
        encounter_rows.append({
            "Id": encounter_id,
            "DATE": "{}-01-01 08:00:00".format(BASE_DATE[0]),
            "PATIENT": patient_id,
            "ENCOUNTER_CLASS": "inpatient",
            "DEPARTMENT": dept["name"],
            "DEPT_CODE": dept["code"],
        })

        obs_count = rng.randint(min_obs, max_obs)
        previous = []
        for o_index in range(1, obs_count + 1):
            spec = rng.choices(specs, weights=weights, k=1)[0]
            value = _sample_value(rng, spec, critical_ratio)
            day_offset = rng.randint(0, 364)
            hour = rng.randint(0, 23)
            minute = rng.randint(0, 59)
            second = rng.randint(0, 59)
            date_text = "{}-{:02d}-{:02d} {:02d}:{:02d}:{:02d}".format(
                BASE_DATE[0], 1 + day_offset // 31, 1 + day_offset % 28, hour, minute, second)
            correction_of = ""
            if previous and rng.random() < correction_ratio:
                correction_of = previous[rng.randrange(len(previous))]
            observation_rows.append({
                "Id": "SYN-OBS-{:08d}".format(len(observation_rows) + 1),
                "DATE": date_text,
                "PATIENT": patient_id,
                "CODE": spec["code"],
                "DESCRIPTION": spec["name_zh"],
                "VALUE": "{:g}".format(value),
                "UNITS": spec["unit"],
                "TYPE": "laboratory",
                "ENCOUNTER": encounter_id,
                "CORRECTION_OF": correction_of,
            })
            previous.append("SYN-OBS-{:08d}".format(len(observation_rows)))

    _write_csv(out_dir / "patients.csv", patient_rows)
    _write_csv(out_dir / "encounters.csv", encounter_rows)
    _write_csv(out_dir / "observations.csv", observation_rows)

    # 来源标记：下游据此区分"抓到的真实 Synthea CSV"与"仿真生成的同 schema CSV"
    write_json(out_dir / "SOURCE.json", {
        "source": "synthetic-simulator",
        "generator": "scripts/gen_synth.py",
        "seed": seed,
        "patients": len(patient_rows),
        "encounters": len(encounter_rows),
        "observations": len(observation_rows),
        "note": "配置化仿真产出的 Synthea CSV schema 子集，非真实患者数据",
    })

    return {
        "source": "synthetic-simulator",
        "synthea_dir": str(out_dir),
        "patients": len(patient_rows),
        "encounters": len(encounter_rows),
        "observations": len(observation_rows),
        "seed": seed,
    }


def _write_csv(path: Path, rows: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def _read_csv(path: Path) -> list:
    if not path.exists():
        return []
    with open(path, encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


# ---------------------------------------------------------------------------
# 主路径：Synthea CSV → ORU^R01 ER7 帧
# ---------------------------------------------------------------------------
def _to_hl7_ts(date_text: str) -> str:
    text = (date_text or "").strip().replace("T", " ")
    digits = "".join(ch for ch in text if ch.isdigit())
    if len(digits) >= 14:
        return digits[:14]
    if len(digits) >= 8:
        return digits[:8] + "000000"
    return "20260101000000"


def csv_to_oru(synthea_dir: Path, out_dir: Path, specs: list, frames_per_file: int = 200,
               samples_dir: Path = None) -> dict:
    """把 Synthea CSV 导出转成 ER7 ORU^R01 帧，写入分片文件并产出 manifest。"""
    spec_by_code = {spec["code"]: spec for spec in specs}
    patients = {row.get("Id", ""): row for row in _read_csv(synthea_dir / "patients.csv")}
    encounters = {row.get("Id", ""): row for row in _read_csv(synthea_dir / "encounters.csv")}
    observations = _read_csv(synthea_dir / "observations.csv")

    out_dir.mkdir(parents=True, exist_ok=True)
    for stale in sorted(out_dir.glob("part-*.hl7")):
        stale.unlink()

    item_counter = Counter()
    dept_counter = Counter()
    unmapped = Counter()
    critical_candidates = 0
    corrections = 0
    frames_written = 0
    patient_ids = set()
    file_records = []
    sample_frames = {}

    buffer = []
    part_index = 1
    seq = 0

    def flush(part_no, chunk):
        if not chunk:
            return
        path = out_dir / "part-{:04d}.hl7".format(part_no)
        path.write_text("".join(chunk), encoding="utf-8")
        file_records.append({
            "path": str(path),
            "frames": len(chunk),
            "sha256": sha256_file(path),
        })

    for observation in observations:
        code = (observation.get("CODE") or "").strip()
        spec = spec_by_code.get(code)
        if spec is None:
            unmapped[code] += 1
            continue

        seq += 1
        patient_id = (observation.get("PATIENT") or "").strip()
        patient_ids.add(patient_id)
        patient = patients.get(patient_id, {})
        encounter = encounters.get((observation.get("ENCOUNTER") or "").strip(), {})
        dept_code = (encounter.get("DEPT_CODE") or "").strip()
        dept = next((d for d in DEPARTMENTS if d["code"] == dept_code), DEPARTMENTS[0])

        obs_ts = _to_hl7_ts(observation.get("DATE", ""))
        control_id = "SYNMSG{:08d}".format(seq)
        specimen_id = "SYN-ORD-{:08d}".format(seq)
        order_id = "SYN-REQ-{:08d}".format(seq)
        value_text = (observation.get("VALUE") or "").strip()
        unit = (observation.get("UNITS") or spec["unit"]).strip() or spec["unit"]
        ref_text = spec.get("ref_text") or ""
        try:
            value_num = float(value_text)
        except ValueError:
            value_num = None
        flag = abnormal_flag(value_num, spec) if value_num is not None else "N"

        is_critical = False
        if value_num is not None:
            if spec.get("crit_low") is not None and value_num < spec["crit_low"]:
                is_critical = True
            if spec.get("crit_high") is not None and value_num > spec["crit_high"]:
                is_critical = True
        if is_critical:
            critical_candidates += 1

        correction_of = (observation.get("CORRECTION_OF") or "").strip()
        result_status = "C" if correction_of else "F"
        if correction_of:
            corrections += 1

        segments = [
            build_msh(obs_ts, control_id),
            build_pid(patient_id, patient.get("BIRTHDATE", "19000101"), patient.get("GENDER", "U")),
            build_pv1(dept, "V{:07d}".format(seq)),
            build_obr(spec, obs_ts, specimen_id, order_id, result_status),
            build_obx(spec, value_text, unit, ref_text, flag, obs_ts),
        ]
        frame = SEGMENT_SEP.join(segments) + SEGMENT_SEP

        # 三份样例帧：正常 / 缺字段（OBX-3 缺失）/ 修正结果（OBR-25=C）
        if len(sample_frames) < 3:
            if "missing_field" not in sample_frames and seq > 1:
                missing_segments = list(segments)
                missing_segments[3] = build_obr(
                    spec, obs_ts, specimen_id, order_id, result_status, omit_universal_id=True
                )
                missing_segments[4] = build_obx(spec, value_text, unit, ref_text, flag, "", omit_code=True)
                sample_frames["missing_field"] = (SEGMENT_SEP.join(missing_segments) + SEGMENT_SEP)
            elif "normal" not in sample_frames and is_critical:
                sample_frames["normal"] = frame
            elif "correction" not in sample_frames and result_status == "C":
                sample_frames["correction"] = frame

        buffer.append(frame + FILE_SEP)
        frames_written += 1
        item_counter[code] += 1
        dept_counter[dept["code"]] += 1

        if len(buffer) >= frames_per_file:
            flush(part_index, buffer)
            buffer = []
            part_index += 1

    flush(part_index, buffer)

    if samples_dir is not None:
        samples_dir.mkdir(parents=True, exist_ok=True)
        fallback = next(iter(buffer)) if buffer else ""
        mapping = [
            ("示例ORU_R01_正常.hl7", sample_frames.get("normal")),
            ("示例ORU_R01_缺字段.hl7", sample_frames.get("missing_field")),
            ("示例ORU_R01_修正结果.hl7", sample_frames.get("correction")),
        ]
        for filename, content in mapping:
            if not content:
                content = fallback
            if content:
                write_text(samples_dir / filename, content)

        # 多项目样例帧：一条标本挂两个危急项目（合并通知的现场样例）。
        # 不依赖 Synthea 语料 —— 阈值口径齐就能生成，断网也能重建。
        multi_item = build_multi_item_frame(
            specs,
            "20261228234910",
            "SYNMSG00000042",
            "SYN-ORD-00000042",
            "SYN-REQ-00000042",
        )
        if multi_item:
            write_text(samples_dir / "示例ORU_R01_多项目.hl7", multi_item)

    manifest = {
        "generated_by": "scripts/gen_synth.py",
        "synthea_dir": str(synthea_dir),
        "frames": frames_written,
        "patients": len(patient_ids),
        "observations": len(observations),
        "corrections": corrections,
        "critical_candidates": critical_candidates,
        "item_distribution": dict(sorted(item_counter.items())),
        "dept_distribution": dict(sorted(dept_counter.items())),
        "unmapped_codes": dict(unmapped),
        "files": file_records,
    }
    write_json(out_dir / "manifest.json", manifest)
    return manifest


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Synthea CSV → HL7 ORU^R01 帧生成器")
    parser.add_argument("--synthea-dir", default=None, help="Synthea CSV 导出目录（patients/observations/encounters）")
    parser.add_argument("--simulate", action="store_true", help="用配置化仿真器产出同 schema 的 Synthea CSV")
    parser.add_argument("--patients", type=int, default=320)
    parser.add_argument("--min-obs", type=int, default=12)
    parser.add_argument("--max-obs", type=int, default=18)
    parser.add_argument("--critical-ratio", type=float, default=0.07)
    parser.add_argument("--correction-ratio", type=float, default=0.03)
    parser.add_argument("--seed", type=int, default=20260101)
    parser.add_argument("--frames-per-file", type=int, default=200)
    parser.add_argument("--out-dir", default="data/synth/oru")
    parser.add_argument("--samples-dir", default="samples/hl7")
    parser.add_argument("--reference-csv", default="data/raw/reference_intervals.csv")
    args = parser.parse_args(argv)

    project_root = PROJECT_ROOT
    specs = load_specs(project_root / args.reference_csv)
    synthea_dir = Path(args.synthea_dir) if args.synthea_dir else project_root / "data/synth/synthea_csv"
    out_dir = project_root / args.out_dir
    samples_dir = project_root / args.samples_dir if args.samples_dir else None

    source_info = {}
    if args.simulate or not (synthea_dir / "observations.csv").exists():
        source_info = simulate_synthea_csv(
            synthea_dir, specs, args.patients, args.min_obs, args.max_obs,
            args.critical_ratio, args.correction_ratio, args.seed)
    else:
        source_info = {"source": "synthea-csv", "synthea_dir": str(synthea_dir)}

    manifest = csv_to_oru(synthea_dir, out_dir, specs, args.frames_per_file, samples_dir)
    manifest.update(source_info)
    write_json(out_dir / "manifest.json", manifest)

    print("[gen_synth] 帧数={} 患者={} 危急值候选={} 修正帧={}".format(
        manifest["frames"], manifest["patients"], manifest["critical_candidates"], manifest["corrections"]))
    print("[gen_synth] 输出目录：{}".format(out_dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
