#!/usr/bin/env python3
"""生成单测用的脱敏制度样例（docx + pdf）。

样例刻意覆盖制度解析要处理的每一类边界：

* 正常条款（三项事实齐全）；
* **只写了一半**的条款（只有上报时限）→ 字段缺失，进解析报告；
* **空正文**的条款（只有条号）→ 未识别行；
* **含要素却无条款号**的段落 → 未识别行（不能当条款引用，也不能丢）；
* 普通叙述段（无要素）→ 只是正文，不算未识别；
* 章节标题（Heading 1 / Heading 2）→ 章节路径；
* 条款正文里点名检验项目码 → 绑定表靠它挂项目；
* 同一条款号重号（按项目分条写）→ 稳定标识按 ``条款号#项目码`` 区分；
* **PDF 版本**（真实 pypdf 抽取，验证 pdf 通道不是摆设）。

    python3 tests/fixtures/build_policy_fixture.py
"""

from __future__ import annotations

from pathlib import Path

HERE = Path(__file__).resolve().parent

DOCX_PATH = HERE / "脱敏样例制度.docx"
DOCX_V2_PATH = HERE / "脱敏样例制度_v2.docx"
PDF_PATH = HERE / "脱敏样例制度.pdf"

META = "版本：测试 v1.0　生效日期：2026-02-01　失效日期：2026-02-25　编制部门：检验科／医务科"

#: (标题层级, 文本) —— 0 = 正文段。
DOCX_PARAGRAPHS: list[tuple[int, str]] = [
    (0, "脱敏样例危急值报告制度"),
    (0, META),
    (1, "第一章 总则"),
    (0, "第一条 为规范危急值报告与记录流程，制定本制度。"),
    (1, "第二章 报告流程与通知时限"),
    (0, "第二条 检验科发现危急值后应当班复核并发出报告；通知对象：检验科值班技师；上报时限：10 分钟内；记录去向：危急值登记系统。"),
    (0, "第三条 临床科室值班护士长应在 15 分钟内响应；上报时限：15 分钟内。"),
    (0, "第四条 血红蛋白（718-7）低于 30 或高于 200 判定为危急值。"),
    (0, "第五条 检验科发现危急值后应当在 20 分钟内发出报告；通知对象：检验科组长；上报时限：20 分钟内；记录去向：危急值登记系统。"),
    (0, "第六条"),
    (0, "通知对象：检验科值班技师；上报时限：30 分钟内；记录去向：危急值登记系统。"),
    (1, "第三章 记录与登记"),
    (0, "第七条 危急值处置记录统一进入危急值登记系统。"),
]

#: 改版样例：第七条时限改了、新增第八条、删掉第五条。
DOCX_V2_PARAGRAPHS: list[tuple[int, str]] = [
    (0, "脱敏样例危急值报告制度"),
    (0, "版本：测试 v2.0　生效日期：2026-03-01　编制部门：检验科／医务科"),
    (1, "第一章 总则"),
    (0, "第一条 为规范危急值报告与记录流程，制定本制度。"),
    (1, "第二章 报告流程与通知时限"),
    (0, "第二条 检验科发现危急值后应当班复核并发出报告；通知对象：检验科值班技师；上报时限：10 分钟内；记录去向：危急值登记系统。"),
    (0, "第三条 临床科室值班护士长应在 15 分钟内响应；上报时限：15 分钟内。"),
    (0, "第四条 血红蛋白（718-7）低于 30 或高于 200 判定为危急值。"),
    (1, "第三章 记录与登记"),
    (0, "第七条 危急值处置记录统一进入危急值登记系统；通知对象：检验科组长；上报时限：每日核对；记录去向：危急值登记系统。"),
    (0, "第八条 危急值台账每周由检验科组长复核。"),
]


def build_docx(path: Path, paragraphs: list[tuple[int, str]]) -> Path:
    from docx import Document

    document = Document()
    for level, text in paragraphs:
        if level == 1:
            document.add_heading(text, level=1)
        else:
            document.add_paragraph(text)
    path.parent.mkdir(parents=True, exist_ok=True)
    document.save(path)
    return path


def build_pdf(path: Path, lines: list[str]) -> Path:
    """手写一个最小可读的 PDF（文本用 UTF-16BE 十六进制串，pypdf 能抽出中文）。

    真实院内制度既有 docx 也有 pdf，这条通道必须真的跑通，不能只测 docx。
    """
    content_ops: list[str] = []
    y = 800
    for line in lines:
        hex_text = line.encode("utf-16-be").hex().upper()
        content_ops.append(f"BT /F1 11 Tf 40 {y} Td <{hex_text}> Tj ET")
        y -= 18
    content = "\n".join(content_ops).encode("latin-1")

    objects: list[bytes] = [
        b"<</Type/Catalog/Pages 2 0 R>>",
        b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
        b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 595 842]"
        b"/Resources<</Font<</F1 4 0 R>>>>/Contents 5 0 R>>",
        b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica/Encoding/Identity-H>>",
        b"<</Length " + str(len(content)).encode() + b">>stream\n" + content + b"\nendstream",
    ]

    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for index, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{index} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_pos = len(out)
    size = len(objects) + 1
    out += f"xref\n0 {size}\n".encode()
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += f"trailer\n<</Size {size}/Root 1 0 R>>\nstartxref\n{xref_pos}\n%%EOF\n".encode()

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(out))
    return path


PDF_LINES = [
    "脱敏样例危急值报告制度",
    "版本：测试 v1.0　生效日期：2026-02-01",
    "第一章 总则",
    "第一条 为规范危急值报告与记录流程，制定本制度。",
    "第二章 报告流程与通知时限",
    "第二条 检验科发现危急值后应当班复核并发出报告；通知对象：检验科值班技师；上报时限：10 分钟内；记录去向：危急值登记系统。",
    "第三条 血红蛋白（718-7）低于 30 或高于 200 判定为危急值。",
]


def main() -> None:
    build_docx(DOCX_PATH, DOCX_PARAGRAPHS)
    build_docx(DOCX_V2_PATH, DOCX_V2_PARAGRAPHS)
    build_pdf(PDF_PATH, PDF_LINES)
    print(f"已生成：{DOCX_PATH}")
    print(f"已生成：{DOCX_V2_PATH}")
    print(f"已生成：{PDF_PATH}")


if __name__ == "__main__":
    main()
