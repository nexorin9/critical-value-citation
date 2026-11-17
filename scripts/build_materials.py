#!/usr/bin/env python3
"""材料生成：阈值表 xlsx + 制度 docx + 大语料 policy_corpus.txt。

输入：data/raw/reference_intervals.csv（公开参考区间样例集，由 fetch_data.py 合并产出）
输出：
  samples/threshold/示例危急值阈值表.xlsx   项目码/项目名/单位/低危急值/高危急值/参考区间/生效日期/版本号
  samples/policy/示例危急值报告制度.docx    章节标题 + 条款正文 + 通知对象/上报时限/记录去向
  data/synth/policy_corpus.txt              ≥3000 行条款语料（召回评测与吞吐基准用）
  data/synth/policy_corpus.truth.jsonl      每行条款的**生成器真值**（项目码/科室/时限/通知对象…）

说明：样例数值取自公开文献整理的常见参考区间与危急值阈值，仅用于演示解析与判定；
院内上线必须以本院危急值标准文件为准。

**为什么多写一份真值侧车**：召回评测集（scripts/build_eval_set.py）的"期望命中条目"
必须是**生成时就知道的真值**，而不是事后从正文里反解出来的 —— 反解会把"解析器认错了
项目码"变成"评测标准也认错了"，错误互相抵消，指标就永远好看。侧车文件与语料同一次
生成循环产出，两者**逐行对得上**，重跑生成脚本得到同一份真值。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _catalog import (  # noqa: E402
    CLAUSE_TEMPLATES,
    DEPARTMENTS,
    NOTICE_TARGETS,
    POLICY_CHAPTERS,
    RECORD_DESTINATIONS,
    REPORT_DEADLINES,
    load_specs,
)
from _datapipe import PROJECT_ROOT, sha256_file, write_text  # noqa: E402

#: 「10 分钟内」→ 10（真值侧车里存分钟数，方便按时限分组）。
_MINUTES_RE = re.compile(r"(\d+)")

THRESHOLD_COLUMNS = [
    "项目码", "项目名", "单位", "低危急值", "高危急值", "参考区间", "生效日期", "版本号",
]
DEFAULT_EFFECTIVE_DATE = "2026-01-01"
DEFAULT_VERSION = "示例 v1.0"


def build_threshold_xlsx(specs: list, dest: Path, effective_date: str, version: str) -> dict:
    """写阈值表 xlsx。列名与后续解析层的别名映射保持一致。"""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "危急值阈值表"

    header_fill = PatternFill("solid", fgColor="E8EEF7")
    sheet.append(THRESHOLD_COLUMNS)
    for cell in sheet[1]:
        cell.font = Font(bold=True)
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center", vertical="center")

    written = 0
    for spec in specs:
        sheet.append([
            spec["code"],
            spec["name_zh"],
            spec["unit"],
            "" if spec.get("crit_low") is None else "{:g}".format(spec["crit_low"]),
            "" if spec.get("crit_high") is None else "{:g}".format(spec["crit_high"]),
            spec.get("ref_text") or "",
            effective_date,
            version,
        ])
        written += 1

    widths = [16, 22, 12, 12, 12, 16, 14, 14]
    for index, width in enumerate(widths, start=1):
        sheet.column_dimensions[chr(64 + index)].width = width
    sheet.freeze_panes = "A2"

    dest.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(dest)
    return {"rows": written, "path": str(dest), "sha256": sha256_file(dest)}


def _clause_text(name: str, code: str, unit: str, deadline: str, target: str,
                 destination: str, dept_name: str) -> str:
    return (
        "通知对象：{target}；上报时限：{deadline}；记录去向：{destination}。"
        "涉及项目：{name}（{code}），单位 {unit}。"
    ).format(target=target, deadline=deadline, destination=destination,
             name=name, code=code, unit=unit)


def build_policy_docx(specs: list, dest: Path, effective_date: str, version: str) -> dict:
    """写样例制度 docx：章节标题 + 条款正文（含通知对象 / 上报时限 / 记录去向）。"""
    from docx import Document
    from docx.shared import Pt

    document = Document()
    style = document.styles["Normal"]
    style.font.name = "宋体"
    style.font.size = Pt(10.5)

    document.add_heading("示例危急值报告制度", level=0)
    document.add_paragraph(
        "版本：{}　生效日期：{}　编制部门：检验科／医务科　"
        "说明：本文件为演示用样例，数值与时限取自公开文献整理，院内上线以本院标准为准。".format(
            version, effective_date)
    )

    document.add_heading(POLICY_CHAPTERS[0], level=1)
    document.add_paragraph(
        "第一条 为规范本院危急值的判定、报告、通知、记录与统计流程，保障患者安全，制定本制度。")
    document.add_paragraph(
        "第二条 本制度适用于全院各临床科室、医技科室，以及门诊、住院、急诊的危急值处置。")
    document.add_paragraph(
        "第三条 危急值判定以院内《危急值阈值表》为准。阈值表未列出的检验项目，"
        "由检验科提出并经医务科确认后纳入下一个生效版本。")

    document.add_heading(POLICY_CHAPTERS[1], level=1)
    document.add_paragraph(
        "第四条 危急值阈值表由检验科维护，文件标注版本号与生效日期；改版须提前一个工作日通知全院，"
        "改版生效后按新阈值判定。")
    document.add_paragraph(
        "第五条 检验科应保持参考区间与危急值阈值口径一致；参考区间调整时，危急值阈值同步复核。")
    for spec in specs[:8]:
        document.add_paragraph(
            "第六条 {name}（{code}）的参考区间为 {ref}，单位 {unit}；"
            "低于 {cl} 或高于 {ch} 判定为危急值。".format(
                name=spec["name_zh"], code=spec["code"], ref=spec.get("ref_text") or "—",
                unit=spec["unit"],
                cl="{:g}".format(spec["crit_low"]) if spec.get("crit_low") is not None else "—",
                ch="{:g}".format(spec["crit_high"]) if spec.get("crit_high") is not None else "—",
            ))

    document.add_heading(POLICY_CHAPTERS[2], level=1)
    document.add_paragraph(
        "第七条 检验科发现危急值后应当班复核，并在 10 分钟内发出危急值报告；"
        "通知对象：检验科值班技师与检验科组长；上报时限：10 分钟内；"
        "记录去向：危急值登记系统，并同步记入检验科危急值台账。")
    document.add_paragraph(
        "第八条 危急值报告发出后，临床科室值班护士长应在 15 分钟内响应并到岗处置；"
        "通知对象：临床科室值班护士长；上报时限：15 分钟内；"
        "记录去向：危急值登记系统，并同步记入护理交接班记录。")
    document.add_paragraph(
        "第九条 护理单元处置完成后应回执确认，检验科值班技师核对回执状态；"
        "通知对象：检验科值班技师、临床科室值班护士长；上报时限：30 分钟内；"
        "记录去向：危急值登记系统，并同步记入患者病程记录。")
    document.add_paragraph(
        "第十条 未在时限内确认的危急值，由检验科组长到期复查并重推；重推不重复计数，"
        "仍无回执的转交医务科值班人员处理。")

    document.add_heading(POLICY_CHAPTERS[3], level=1)
    document.add_paragraph(
        "第十一条 危急值处置记录统一进入危急值登记系统，逐条登记项目、依据、通知对象、时限与确认人；"
        "通知对象：检验科组长、医务科质控专员；上报时限：每日核对；"
        "记录去向：危急值登记系统，并同步记入科室交班本。")
    document.add_paragraph(
        "第十二条 危急值台账每日由检验科组长复核前一日记录，对超时未确认与回写失败的条目单独标注，"
        "作为次日复核的第一批条目。")

    document.add_heading(POLICY_CHAPTERS[4], level=1)
    document.add_paragraph(
        "第十三条 危急值报告与处置情况纳入科室质量考核，统计口径以危急值登记系统记录为准；"
        "通知对象：医务科、质控办；上报时限：每月汇总；"
        "记录去向：危急值登记系统，并同步记入科室交班本。")
    document.add_paragraph(
        "第十四条 因漏报、迟报导致的不良事件，按医院不良事件上报流程处理并纳入年度考核。")

    dest.parent.mkdir(parents=True, exist_ok=True)
    document.save(dest)
    return {"path": str(dest), "sha256": sha256_file(dest),
            "chapters": len(POLICY_CHAPTERS), "clauses": len(document.paragraphs)}


def build_policy_corpus(specs: list, dest: Path, min_lines: int = 3600,
                        truth_path: Path | None = None) -> dict:
    """生成大语料：每行一条带稳定编号与章节路径的条款文本，供召回评测与吞吐基准使用。

    ``truth_path`` 给定时，同一次生成循环把**每行的真值**（项目码 / 科室 / 时限 /
    通知对象 / 记录去向 / 章节 / 句式序号）写成 JSONL 侧车 —— 评测集的"期望命中条目"
    直接取自这份真值，不从正文反解。
    """
    lines = []
    truth_rows = []
    index = 0
    round_no = 0
    while len(lines) < min_lines:
        round_no += 1
        for spec in specs:
            for dept in DEPARTMENTS:
                for template_index, template in enumerate(CLAUSE_TEMPLATES):
                    deadline = REPORT_DEADLINES[(index + round_no) % len(REPORT_DEADLINES)]
                    target = NOTICE_TARGETS[(index + template_index) % len(NOTICE_TARGETS)]
                    destination = RECORD_DESTINATIONS[(index + template_index + round_no) % len(RECORD_DESTINATIONS)]
                    index += 1
                    crit_low = "{:g}".format(spec["crit_low"]) if spec.get("crit_low") is not None else "—"
                    crit_high = "{:g}".format(spec["crit_high"]) if spec.get("crit_high") is not None else "—"
                    body = template.format(
                        name=spec["name_zh"], code=spec["code"], unit=spec["unit"],
                        crit_low=crit_low, crit_high=crit_high,
                        deadline=deadline, dept=dept["name"], target=target,
                    )
                    chapter = POLICY_CHAPTERS[(index + template_index) % len(POLICY_CHAPTERS)]
                    tail = _clause_text(spec["name_zh"], spec["code"], spec["unit"],
                                        deadline, target, destination, dept["name"])
                    clause_id = "CLAUSE-{:06d}".format(index)
                    lines.append("{}\t{}\t{} {}".format(clause_id, chapter, body, tail))
                    truth_rows.append({
                        "clause_id": clause_id,
                        "line_no": index + 1,
                        "item_code": spec["code"],
                        "item_name": spec["name_zh"],
                        "unit": spec["unit"],
                        "dept_code": dept["code"],
                        "dept_name": dept["name"],
                        "deadline_text": deadline,
                        "deadline_minutes": _deadline_minutes(deadline),
                        "notify_target": target,
                        "record_destination": destination,
                        "section": chapter,
                        "template_index": template_index,
                    })
                    if len(lines) >= min_lines:
                        break
                if len(lines) >= min_lines:
                    break
            if len(lines) >= min_lines:
                break

    write_text(dest, "\n".join(lines) + "\n")
    result = {"path": str(dest), "lines": len(lines), "sha256": sha256_file(dest)}
    if truth_path is not None:
        truth_path.parent.mkdir(parents=True, exist_ok=True)
        with truth_path.open("w", encoding="utf-8") as handle:
            for row in truth_rows:
                handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True))
                handle.write("\n")
        result["truth_path"] = str(truth_path)
        result["truth_rows"] = len(truth_rows)
        result["truth_sha256"] = sha256_file(truth_path)
    return result


def _deadline_minutes(deadline_text: str) -> int:
    """「10 分钟内」→ 10；解析不出分钟数时记 0（评测时按"无时限"分组）。"""
    match = _MINUTES_RE.search(deadline_text or "")
    return int(match.group(1)) if match else 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="生成阈值表 / 制度 / 语料")
    parser.add_argument("--reference-csv", default="data/raw/reference_intervals.csv")
    parser.add_argument("--threshold-out", default="samples/threshold/示例危急值阈值表.xlsx")
    parser.add_argument("--policy-out", default="samples/policy/示例危急值报告制度.docx")
    parser.add_argument("--corpus-out", default="data/synth/policy_corpus.txt")
    parser.add_argument("--truth-out", default="",
                        help="语料真值侧车路径（默认与 --corpus-out 同目录的 .truth.jsonl）")
    parser.add_argument("--corpus-only", action="store_true",
                        help="只重生成语料与真值侧车（不碰阈值表 xlsx / 制度 docx —— "
                             "这两个是 zip 包，重生成会让文件指纹变化）")
    parser.add_argument("--effective-date", default=DEFAULT_EFFECTIVE_DATE)
    parser.add_argument("--version", default=DEFAULT_VERSION)
    parser.add_argument("--min-lines", type=int, default=3600)
    args = parser.parse_args(argv)

    root = PROJECT_ROOT
    specs = load_specs(root / args.reference_csv)
    if not specs:
        print("[build_materials] 参考区间样例集为空，无法生成材料", file=sys.stderr)
        return 1

    corpus_out = root / args.corpus_out
    truth_out = root / args.truth_out if args.truth_out else corpus_out.with_suffix(".truth.jsonl")

    if not args.corpus_only:
        threshold = build_threshold_xlsx(specs, root / args.threshold_out, args.effective_date, args.version)
        policy = build_policy_docx(specs, root / args.policy_out, args.effective_date, args.version)
        print("[build_materials] 阈值表 {} 行 → {}".format(threshold["rows"], threshold["path"]))
        print("[build_materials] 制度 {} 章 → {}".format(policy["chapters"], policy["path"]))

    corpus = build_policy_corpus(specs, corpus_out, args.min_lines, truth_path=truth_out)
    print("[build_materials] 语料 {} 行 → {}".format(corpus["lines"], corpus["path"]))
    print("[build_materials] 真值侧车 {} 行 → {}".format(corpus.get("truth_rows", 0), corpus.get("truth_path", "")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
