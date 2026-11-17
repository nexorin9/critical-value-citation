#!/usr/bin/env python3
"""生成制度改版样例与版本绑定表样例。

产物：

* ``samples/policy/示例危急值报告制度_v2.docx`` —— 制度**改版**样例
  （版本 示例 v2.0，生效 2026-06-01）。相对 v1.0 有三处变化：
  第七条的上报时限与通知对象改了、第十五条新增、第十四条删除 ——
  用来演示「制度改版影响面」清单。
* ``samples/bindings/版本绑定表.json`` —— 版本绑定表样例
  （项目码 → 阈值表版本 + 制度版本）。

    python3 scripts/build_policy_samples.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from critval.kb.binding import BindingTable, save_binding_table  # noqa: E402
from critval.kb.consistency import VersionBinding  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parent.parent
POLICY_V2_PATH = PROJECT_ROOT / "samples" / "policy" / "示例危急值报告制度_v2.docx"
BINDINGS_PATH = PROJECT_ROOT / "samples" / "bindings" / "版本绑定表.json"

META = (
    "版本：示例 v2.0　生效日期：2026-06-01　编制部门：检验科／医务科　"
    "说明：本文件为演示用改版样例，数值与时限取自公开文献整理，院内上线以本院标准为准。"
)

#: (标题层级, 文本) —— 0 = 正文段，1 = 章标题。
PARAGRAPHS: list[tuple[int, str]] = [
    (0, "示例危急值报告制度"),
    (0, META),
    (1, "第一章 总则"),
    (0, "第一条 为规范本院危急值的判定、报告、通知、记录与统计流程，保障患者安全，制定本制度。"),
    (0, "第二条 本制度适用于全院各临床科室、医技科室，以及门诊、住院、急诊的危急值处置。"),
    (0, "第三条 危急值判定以院内《危急值阈值表》为准。阈值表未列出的检验项目，"
        "由检验科提出并经医务科确认后纳入下一个生效版本。"),
    (1, "第二章 危急值项目与阈值"),
    (0, "第四条 危急值阈值表由检验科维护，文件标注版本号与生效日期；改版须提前一个工作日通知全院，"
        "改版生效后按新阈值判定。"),
    (0, "第五条 检验科应保持参考区间与危急值阈值口径一致；参考区间调整时，危急值阈值同步复核。"),
    (0, "第六条 血红蛋白（718-7）的参考区间为 115-150，单位 g/L；低于 30 或高于 200 判定为危急值。"),
    (0, "第六条 红细胞压积（789-8）的参考区间为 40-50，单位 %；低于 20 或高于 60 判定为危急值。"),
    (0, "第六条 白细胞计数（6690-2）的参考区间为 3.5-9.5，单位 10*9/L；低于 1.5 或高于 30 判定为危急值。"),
    (0, "第六条 血小板计数（777-3）的参考区间为 125-350，单位 10*9/L；低于 20 或高于 1000 判定为危急值。"),
    (0, "第六条 钾（2823-3）的参考区间为 3.5-5.3，单位 mmol/L；低于 2.8 或高于 6.5 判定为危急值。"),
    (1, "第三章 报告流程与通知时限"),
    # 改版点 1：时限 10 分钟 → 15 分钟，通知对象增加检验科组长。
    (0, "第七条 检验科发现危急值后应当班复核，并在 15 分钟内发出危急值报告；"
        "通知对象：检验科值班技师、检验科组长；上报时限：15 分钟内；"
        "记录去向：危急值登记系统，并同步记入检验科危急值台账。"),
    (0, "第八条 危急值报告发出后，临床科室值班护士长应在 15 分钟内响应并到岗处置；"
        "通知对象：临床科室值班护士长；上报时限：15 分钟内；"
        "记录去向：危急值登记系统，并同步记入护理交接班记录。"),
    (0, "第九条 护理单元处置完成后应回执确认，检验科值班技师核对回执状态；"
        "通知对象：检验科值班技师、临床科室值班护士长；上报时限：30 分钟内；"
        "记录去向：危急值登记系统，并同步记入患者病程记录。"),
    # 改版点 2：只写了时限，没写通知对象与记录去向 —— 解析时标「字段缺失」。
    (0, "第十条 未在时限内确认的危急值，由检验科组长到期复查并重推；上报时限：次日 08:00 前。"),
    (1, "第四章 记录与登记"),
    (0, "第十一条 危急值处置记录统一进入危急值登记系统，逐条登记项目、依据、通知对象、时限与确认人；"
        "通知对象：检验科组长、医务科质控专员；上报时限：每日核对；"
        "记录去向：危急值登记系统，并同步记入科室交班本。"),
    (0, "第十二条 危急值台账每日由检验科组长复核前一日记录，对超时未确认与回写失败的条目单独标注，"
        "作为次日复核的第一批条目。"),
    (1, "第五章 责任与考核"),
    (0, "第十三条 危急值报告与处置情况纳入科室质量考核，统计口径以危急值登记系统记录为准；"
        "通知对象：医务科、质控办；上报时限：每月汇总；"
        "记录去向：危急值登记系统，并同步记入科室交班本。"),
    # 改版点 3：新增条款（v1.0 没有第十五条）。
    (0, "第十五条 危急值通知与回执留痕纳入月度质控抽查，抽查结果向医务科月度例会通报。"),
]

BINDINGS = (
    VersionBinding(
        item_code="718-7",
        threshold_version="示例 v1.0",
        policy_version="示例 v1.0",
        note="检验科 2026-01 确认",
    ),
    VersionBinding(
        item_code="6690-2",
        threshold_version="示例 v1.0",
        policy_version="示例 v1.0",
        note="检验科 2026-01 确认",
    ),
    VersionBinding(
        item_code="777-3",
        threshold_version="示例 v1.0",
        policy_version="示例 v1.0",
        note="检验科 2026-01 确认",
    ),
)


def build_policy_docx(dest: Path) -> Path:
    from docx import Document
    from docx.shared import Pt

    document = Document()
    style = document.styles["Normal"]
    style.font.name = "宋体"
    style.font.size = Pt(10.5)

    for level, text in PARAGRAPHS:
        if level == 1:
            document.add_heading(text, level=1)
        else:
            document.add_paragraph(text)

    dest.parent.mkdir(parents=True, exist_ok=True)
    document.save(dest)
    return dest


def main() -> None:
    path = build_policy_docx(POLICY_V2_PATH)
    print(f"已生成制度改版样例：{path}")
    bindings_path = save_binding_table(BindingTable(bindings=BINDINGS), BINDINGS_PATH)
    print(f"已生成版本绑定表样例：{bindings_path}")
    print(json.dumps(BindingTable(bindings=BINDINGS).to_dict(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
