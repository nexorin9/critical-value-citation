#!/usr/bin/env python3
"""方言兼容矩阵对拍：同一批报文，用多套方言各解析一遍，逐字段比对。

**对拍有两条线**：

1. **协议对拍（期望基线）** —— 每条帧在 ``samples/dialects/expected.json``
   里有一份「参照方言 + 期望字段值」。参照方言的解析结果必须与期望**逐字段
   一致**，差异数必须是 0；官方样例报文（``samples/hl7/官方样例/``）的参照
   方言就是默认方言。
2. **方言互拍（参照方言 vs 其它方言）** —— 同一帧换方言解析，字段不同的地方
   逐条落进 ``reports/dialect_diff.csv``：哪一帧、哪个方言、哪个字段、参照值
   与实际值各是什么。这张表就是院内选方言、改配置的对照依据。

用法::

    python3 scripts/parse_matrix.py                 # 全量对拍
    python3 scripts/parse_matrix.py --json          # 机器可读输出
    python3 scripts/parse_matrix.py --frames DIR    # 只对拍某目录

退出码：0 = 参照方言逐字段差异为 0；1 = 有差异（对拍没过）。
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from critval.hl7.dialects import (  # noqa: E402
    BUILTIN_DIALECTS,
    Dialect,
    load_dialect,
    parse_oru_r01,
)
from critval.hl7.normalize import OruMessage  # noqa: E402

DEFAULT_FRAMES = [
    PROJECT_ROOT / "samples" / "hl7",
    PROJECT_ROOT / "samples" / "hl7" / "官方样例",
    PROJECT_ROOT / "samples" / "hl7" / "方言样例",
]
DEFAULT_DIALECTS_DIR = PROJECT_ROOT / "samples" / "dialects"
DEFAULT_EXPECTED = PROJECT_ROOT / "samples" / "dialects" / "expected.json"
DEFAULT_OUT = PROJECT_ROOT / "reports" / "dialect_diff.csv"

FRAME_SUFFIXES = (".hl7", ".txt")

#: 参与逐字段比对的字段（消息级 + 第一条结果级）。
COMPARED_FIELDS = (
    "message_control_id",
    "specimen_no",
    "specimen_no_source",
    "dept_code",
    "result_status",
    "observation_count",
    "item_codes",
    "first_item_code",
    "first_item_name",
    "first_value",
    "first_unit",
    "first_observed_at",
    "first_obx_flag",
    "first_result_status",
    "first_item_code_source",
    "first_item_code_confidence",
)

CSV_HEADER = ("帧", "参照方言", "对拍方言", "比对口径", "字段", "参照值", "实际值")


def collect_frames(directories: list[Path]) -> list[Path]:
    """列出待对拍的帧（同名文件以路径后缀区分）。"""
    frames: list[Path] = []
    seen: set[str] = set()
    for directory in directories:
        if not directory.exists():
            continue
        for path in sorted(directory.iterdir()):
            if not path.is_file() or path.suffix.lower() not in FRAME_SUFFIXES:
                continue
            key = path.name
            if key in seen:
                continue
            seen.add(key)
            frames.append(path)
    return frames


def load_dialects(directory: Path) -> dict[str, Dialect]:
    """加载方言：``samples/dialects/*.json`` 优先，其次内置方言。"""
    dialects: dict[str, Dialect] = {}
    if directory.exists():
        for path in sorted(directory.glob("*.json")):
            if path.name == "expected.json":
                continue
            dialect = load_dialect(path)
            dialects[dialect.name] = dialect
    for name, dialect in BUILTIN_DIALECTS.items():
        dialects.setdefault(name, dialect)
    return dialects


def summarize(message: OruMessage) -> dict[str, str]:
    """把一次解析压成可比对的字段字典。"""
    first = message.observations[0] if message.observations else None
    return {
        "message_control_id": message.message_control_id,
        "specimen_no": message.specimen_no,
        "specimen_no_source": message.specimen_no_source,
        "dept_code": message.dept_code,
        "result_status": message.result_status,
        "observation_count": str(len(message.observations)),
        "item_codes": "|".join(obs.item_code for obs in message.observations),
        "first_item_code": first.item_code if first else "",
        "first_item_name": first.item_name if first else "",
        "first_value": first.value_raw if first else "",
        "first_unit": first.unit if first else "",
        "first_observed_at": (
            first.observed_at.strftime("%Y-%m-%d %H:%M:%S") if first and first.observed_at else ""
        ),
        "first_obx_flag": first.abnormal_flag if first else "",
        "first_result_status": first.result_status if first else "",
        "first_item_code_source": first.item_code_source if first else "",
        "first_item_code_confidence": first.item_code_confidence if first else "",
    }


def diff_fields(expected: dict[str, str], actual: dict[str, str]) -> list[tuple[str, str, str]]:
    """逐字段比对，返回 ``(字段, 期望值, 实际值)`` 差异列表。"""
    diffs: list[tuple[str, str, str]] = []
    for field in COMPARED_FIELDS:
        want = str(expected.get(field, ""))
        got = str(actual.get(field, ""))
        if want != got:
            diffs.append((field, want, got))
    return diffs


def run(
    frame_dirs: list[Path],
    dialects_dir: Path,
    expected_path: Path,
    out_path: Path,
) -> dict[str, object]:
    """跑一遍对拍，返回可核对的摘要。"""
    expected_raw = json.loads(expected_path.read_text(encoding="utf-8"))
    expected = {k: v for k, v in expected_raw.items() if not k.startswith("_")}

    dialects = load_dialects(dialects_dir)
    frames = collect_frames(frame_dirs)

    rows: list[tuple[str, str, str, str, str, str, str]] = []
    reference_mismatches = 0
    reference_frames = 0
    cross_diffs = 0
    per_dialect: dict[str, int] = {name: 0 for name in dialects}
    details: list[dict[str, object]] = []

    for frame in frames:
        baseline = expected.get(frame.name)
        reference = str(baseline.get("reference_dialect", "default")) if baseline else "default"
        if reference not in dialects:
            raise SystemExit(f"参照方言 {reference} 不存在（帧 {frame.name}）")

        parsed: dict[str, dict[str, str]] = {}
        for name, dialect in dialects.items():
            message = parse_oru_r01(frame.read_text(encoding="utf-8"), dialect)
            parsed[name] = summarize(message)

        # ---- 线 1：参照方言 vs 期望基线（差异必须为 0）------------------
        if baseline:
            reference_frames += 1
            diffs = diff_fields(baseline, parsed[reference])
            reference_mismatches += len(diffs)
            for field, want, got in diffs:
                rows.append((frame.name, reference, reference, "期望基线", field, want, got))
            for name, values in parsed.items():
                if name == reference:
                    continue
                for field, want, got in diff_fields(baseline, values):
                    rows.append((frame.name, reference, name, "期望基线", field, want, got))

        # ---- 线 2：各方言 vs 参照方言 --------------------------------
        reference_values = parsed[reference]
        for name, values in parsed.items():
            if name == reference:
                continue
            for field, want, got in diff_fields(reference_values, values):
                rows.append((frame.name, reference, name, "方言互拍", field, want, got))
                cross_diffs += 1
                per_dialect[name] = per_dialect.get(name, 0) + 1

        details.append(
            {
                "frame": frame.name,
                "reference_dialect": reference,
                "has_baseline": bool(baseline),
                "observation_count": parsed[reference]["observation_count"],
                "differences": {
                    name: [
                        {"field": f, "reference": w, "actual": g}
                        for f, w, g in diff_fields(reference_values, values)
                    ]
                    for name, values in parsed.items()
                    if name != reference
                },
            }
        )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(CSV_HEADER)
        writer.writerows(rows)

    return {
        "frames": len(frames),
        "dialects": sorted(dialects),
        "reference_frames": reference_frames,
        "reference_mismatches": reference_mismatches,
        "cross_diffs": cross_diffs,
        "per_dialect_diffs": per_dialect,
        "csv": str(out_path),
        "csv_rows": len(rows),
        "details": details,
    }


def render(summary: dict[str, object]) -> str:
    """人读的输出。"""
    lines: list[str] = []
    lines.append(
        f"[对拍] 帧 {summary['frames']} 条 · 方言 {len(summary['dialects'])} 套 · "
        f"参照基线帧 {summary['reference_frames']} 条"
    )
    lines.append(
        f"[参照方言逐字段比对] 差异 {summary['reference_mismatches']} 处"
        f"（要求为 0）"
    )
    lines.append(f"[方言互拍] 差异 {summary['cross_diffs']} 处 → {summary['csv']}")
    per = summary["per_dialect_diffs"]  # type: ignore[index]
    for name in sorted(per):  # type: ignore[index]
        count = per[name]  # type: ignore[index]
        if count:
            lines.append(f"  · {name} 与参照方言有 {count} 处差异")
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="HL7 厂商方言兼容矩阵对拍")
    parser.add_argument(
        "--frames",
        action="append",
        default=None,
        help="对拍帧目录（可多次指定；默认样例帧 + 官方样例 + 方言样例）",
    )
    parser.add_argument("--dialects", default=str(DEFAULT_DIALECTS_DIR), help="方言配置目录")
    parser.add_argument("--expected", default=str(DEFAULT_EXPECTED), help="期望基线 JSON")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="差异表输出路径")
    parser.add_argument("--json", action="store_true", help="输出 JSON 摘要")
    args = parser.parse_args(argv)

    frame_dirs = [Path(p) for p in args.frames] if args.frames else DEFAULT_FRAMES
    summary = run(
        frame_dirs=frame_dirs,
        dialects_dir=Path(args.dialects),
        expected_path=Path(args.expected),
        out_path=Path(args.out),
    )

    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(render(summary))
    return 0 if int(summary["reference_mismatches"]) == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
