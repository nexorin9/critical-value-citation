#!/usr/bin/env python3
"""构造召回评测集（标注查询集）。

**期望命中条目从哪来**：不是人工手挑的，而是**生成时的真值**——

* 阈值表行、制度条款：材料本身就是事实来源，期望条目由解析结果（项目码 / 条款号）
  推出；
* 条款语料：语料生成器同一次循环写出了真值侧车 ``policy_corpus.truth.jsonl``
  （每条是哪个项目、哪个科室、哪个时限、哪条句式生成的），评测集直接取这份真值。

重跑本脚本得到**逐字节相同**的评测集文件（不写时间戳），因此任何时候跑出的
指标都可复算、可与历史对比。

用法::

    python3 scripts/build_eval_set.py                       # 生成 data/eval/queries.jsonl
    python3 scripts/build_eval_set.py --corpus-queries 72    # 语料类查询条数
    python3 scripts/build_eval_set.py --print-sample         # 打印前几条标注
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from critval.evaluate import EvalQuery, EvalSet, canonical_key, write_eval_set  # noqa: E402
from critval.index.builder import MaterialIndex, MaterialSource  # noqa: E402

DEFAULT_EVAL_SET = PROJECT_ROOT / "data" / "eval" / "queries.jsonl"
DEFAULT_SAMPLE = PROJECT_ROOT / "docs" / "examples" / "eval_set_sample.jsonl"
DEFAULT_THRESHOLD = PROJECT_ROOT / "samples" / "threshold" / "示例危急值阈值表.xlsx"
DEFAULT_POLICY = PROJECT_ROOT / "samples" / "policy" / "示例危急值报告制度.docx"
DEFAULT_CORPUS = PROJECT_ROOT / "data" / "synth" / "policy_corpus.txt"
DEFAULT_TRUTH = PROJECT_ROOT / "data" / "synth" / "policy_corpus.truth.jsonl"

#: 语料里**正文没有科室名**的句式序号（模板 3）——这类条款的科室无从查询，
#: 拿它构造"科室 + 时限"的查询会变成不公平的题，评测集里跳过。
TEMPLATES_WITHOUT_DEPT = (3,)

#: 负例：材料里压根没有这些检验项目（期望条目为空）。
NEGATIVE_ITEMS = [
    ("肌钙蛋白T", "TNI-T"),
    ("D-二聚体", "D-DIMER"),
    ("血气分析", "BG-ANALYSIS"),
    ("二氧化碳结合力", "CO2CP"),
    ("免疫球蛋白E", "IGE"),
    ("抗核抗体谱", "ANA-PANEL"),
    ("幽门螺旋杆菌抗体", "HP-AB"),
    ("尿液隐血", "UR-BLD"),
    ("粪便常规", "STOOL-ROUTINE"),
    ("血细胞沉降率", "ESR"),
    ("糖化血红蛋白", "HBA1C"),
    ("降钙素原", "PCT"),
    ("乙型肝炎表面抗原", "HBSAG"),
    ("甲状腺游离T4", "FT4"),
    ("交叉配血", "X-MATCH"),
    ("血型鉴定", "ABO-TYPE"),
    ("抗凝血酶III活性", "AT-III"),
    ("网织红细胞", "RETIC"),
    ("尿比重", "U-SG"),
    ("脑脊液常规", "CSF-ROUTINE"),
]

#: 语料类查询的话术（按句式序号选一种；同一句式内按序号轮换）。
CORPUS_QUERY_TEMPLATES = [
    "{dept}送检的 {name}（{code}）标本，危急值上下限判定成立后，检验科要在 "
    "{minutes} 分钟内发出危急值报告并通知临床科室 —— 制度里这一条怎么写的？",
    "{dept}患者的 {name}（{code}）危急值，检验科当班复核要在 {minutes} 分钟内完成"
    "报告确认，随后通知对应岗位 —— 这一条依据在哪？",
    "对 {dept}送检标本中 {name}（{code}）的危急值，检验科应在 {minutes} 分钟内通知"
    "到人并同步登记 —— 制度原文怎么写的？",
    "{dept}对 {name}（{code}）的危急值处置未在 {minutes} 分钟内响应，检验科组长有权"
    "电话追催并记入交班记录 —— 哪一条制度这么写的？",
]

#: 制度 docx 里与项目无关的流程条款（第三章 / 第四章）。
FLOW_CLAUSES = [
    ("检验科发现危急值后应当班复核，并在多久内发出危急值报告？通知谁？", "第七条"),
    ("危急值报告发出后，临床科室值班护士长应在多久内响应并到岗处置？", "第八条"),
    ("护理单元处置完成后要回执确认，回执状态由谁核对？", "第九条"),
    ("未在时限内确认的危急值，由谁到期复查并重推？仍无回执怎么处理？", "第十条"),
]


def ensure_corpus(corpus_path: Path, truth_path: Path) -> dict[str, Any]:
    """确保语料与真值侧车都在（缺了就用确定性生成器补上，不联网）。

    语料是纯文本，同一份输入生成的内容**逐字节相同**，所以这里补生成不会
    改动已有语料；真值侧车与语料同一次循环产出，两者天然对得上。
    """
    if corpus_path.exists() and truth_path.exists():
        return {"status": "reused", "lines": sum(1 for _ in corpus_path.open(encoding="utf-8"))}

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _catalog import load_specs
    from build_materials import build_policy_corpus

    specs = load_specs(PROJECT_ROOT / "data" / "raw" / "reference_intervals.csv")
    result = build_policy_corpus(specs, corpus_path, 3600, truth_path=truth_path)
    return {"status": "regenerated", "lines": result["lines"]}


def load_truth(truth_path: Path) -> list[dict[str, Any]]:
    """读语料真值侧车。"""
    rows: list[dict[str, Any]] = []
    with truth_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def build_eval_set(
    threshold_path: Path = DEFAULT_THRESHOLD,
    policy_path: Path = DEFAULT_POLICY,
    corpus_path: Path = DEFAULT_CORPUS,
    truth_path: Path = DEFAULT_TRUTH,
    corpus_queries: int = 72,
) -> EvalSet:
    """构造标注查询集。"""
    index = MaterialIndex()
    sources = [
        MaterialSource.from_path(threshold_path, kind="threshold_table", key="threshold"),
        MaterialSource.from_path(policy_path, kind="policy_document", key="policy"),
        MaterialSource.from_path(corpus_path, kind="corpus", key="corpus"),
    ]
    index.rebuild(sources)
    documents = index.documents_by_id()

    threshold_docs = [doc for doc in documents.values() if doc.kind == "threshold_row"]
    policy_docs = [doc for doc in documents.values() if doc.kind == "policy_clause"]
    corpus_docs = {doc.stable_id: doc for doc in documents.values() if doc.kind == "corpus_line"}
    #: 项目码 → 项目名（阈值表与制度写的是同一批项目，制度条款里只点名项目码）
    name_by_code = {
        doc.field("item_code"): doc.field("item_name")
        for doc in threshold_docs
        if doc.field("item_code")
    }

    queries: list[EvalQuery] = []

    # —— F1 阈值查表（结构化路：给项目码，期望命中该项目的阈值行）——
    for doc in sorted(threshold_docs, key=lambda d: d.stable_id):
        code = doc.field("item_code")
        name = doc.field("item_name")
        unit = doc.field("unit")
        queries.append(
            EvalQuery(
                query_id="thr-{}".format(code),
                kind="threshold_lookup",
                text="{name}（{code}）危急值的上下限阈值是多少，单位 {unit}？".format(
                    name=name, code=code, unit=unit
                ),
                filters={"item_code": code},
                expected_keys=(canonical_key(doc),),
                note="按项目码精确查阈值表",
                meta={"item_code": code},
            )
        )

    # —— F2 制度里的项目阈值条款（第六条按项目拆条）——
    item_clauses = [doc for doc in policy_docs if doc.stable_id.startswith("第六条#")]
    for doc in sorted(item_clauses, key=lambda d: d.stable_id):
        code = doc.stable_id.split("#", 1)[1]
        name = name_by_code.get(code, code)
        queries.append(
            EvalQuery(
                query_id="pol-item-{}".format(code),
                kind="policy_item_lookup",
                text="{name}（{code}）的参考区间与危急值阈值，制度里是怎么规定的？".format(
                    name=name, code=code
                ),
                expected_keys=(canonical_key(doc),),
                note="制度里点名项目的阈值条款",
                meta={"item_code": code},
            )
        )

    # —— F3 制度里的流程条款（通知时限 / 响应 / 回执 / 到期重推）——
    for number, (text, clause_no) in enumerate(FLOW_CLAUSES, start=1):
        matched = [doc for doc in policy_docs if doc.stable_id == clause_no]
        if not matched:
            continue
        queries.append(
            EvalQuery(
                query_id="pol-flow-{}".format(number),
                kind="policy_flow_lookup",
                text=text,
                expected_keys=(canonical_key(matched[0]),),
                note="制度流程条款（与项目无关）",
                meta={"clause_no": clause_no},
            )
        )

    # —— F4 阈值 + 响应时限（两个期望条目）——
    flow_response = [doc for doc in policy_docs if doc.stable_id == "第八条"]
    for doc in sorted(item_clauses, key=lambda d: d.stable_id):
        code = doc.stable_id.split("#", 1)[1]
        name = name_by_code.get(code, code)
        expected = [canonical_key(doc)]
        if flow_response:
            expected.append(canonical_key(flow_response[0]))
        queries.append(
            EvalQuery(
                query_id="pol-multi-{}".format(code),
                kind="policy_multi_expect",
                text="{name}（{code}）危急值的参考区间与阈值怎么规定，检验科发出报告后"
                     "临床科室要在多久内响应？".format(name=name, code=code),
                expected_keys=tuple(expected),
                note="同时要阈值依据与临床响应时限两条",
                meta={"item_code": code},
            )
        )

    # —— F5 语料条款（真值来自生成器侧车）——
    truth_rows = load_truth(truth_path)
    groups: dict[tuple[str, str, int, int], list[dict[str, Any]]] = {}
    for row in truth_rows:
        if row["template_index"] in TEMPLATES_WITHOUT_DEPT:
            continue
        key = (row["item_code"], row["dept_code"], row["template_index"], row["deadline_minutes"])
        groups.setdefault(key, []).append(row)
    ordered = sorted(groups.items(), key=lambda item: item[0])
    if corpus_queries > 0 and ordered:
        step = max(1, len(ordered) // corpus_queries)
        picked = ordered[::step][:corpus_queries]
        for number, (key, rows) in enumerate(picked, start=1):
            item_code, dept_code, template_index, deadline_minutes = key
            first = rows[0]
            template = CORPUS_QUERY_TEMPLATES[template_index % len(CORPUS_QUERY_TEMPLATES)]
            text = template.format(
                dept=first["dept_name"],
                name=first["item_name"],
                code=item_code,
                minutes=deadline_minutes,
            )
            expected = tuple(
                canonical_key(corpus_docs[row["clause_id"]])
                for row in rows
                if row["clause_id"] in corpus_docs
            )
            if not expected:
                continue
            queries.append(
                EvalQuery(
                    query_id="corpus-{:03d}-{}".format(number, item_code),
                    kind="corpus_clause_lookup",
                    text=text,
                    expected_keys=expected,
                    note="语料条款（真值：项目+科室+时限+句式）",
                    meta={
                        "item_code": item_code,
                        "dept_code": dept_code,
                        "template_index": template_index,
                        "deadline_minutes": deadline_minutes,
                    },
                )
            )

    # —— F6 负例（材料里没有这些项目）——
    known_names = {doc.field("item_name") for doc in threshold_docs}
    known_codes = {doc.field("item_code") for doc in threshold_docs}
    for number, (name, code) in enumerate(NEGATIVE_ITEMS, start=1):
        if name in known_names or code in known_codes:
            continue
        queries.append(
            EvalQuery(
                query_id="neg-{:02d}-{}".format(number, code),
                kind="negative_no_material",
                text="{name}危急值的通知对象、上报时限和记录去向分别是什么？".format(name=name),
                expected_keys=(),
                note="材料里没有这个项目 —— 期望是不产出依据（负例，不进命中率分母）",
                meta={"probe_name": name, "probe_code": code},
            )
        )

    meta = {
        "generator": "scripts/build_eval_set.py",
        "eval_set_version": "1",
        "materials": [
            {"key": source.key, "name": Path(source.path).name, "kind": source.kind, "sha256": source.sha256}
            for source in sources
        ],
        "counts": {
            "total": len(queries),
            "positive": sum(1 for query in queries if not query.is_negative),
            "negative": sum(1 for query in queries if query.is_negative),
            "by_kind": _count_by_kind(queries),
        },
        "defaults": {"depth": 10, "top_n": 5},
        "notes": (
            "期望命中条目来自生成器真值（语料侧车 policy_corpus.truth.jsonl），"
            "不是人工手挑；重跑本脚本得到逐字节相同的文件。"
        ),
    }
    return EvalSet(queries=tuple(queries), meta=meta)


def _count_by_kind(queries: list[EvalQuery]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for query in queries:
        counts[query.kind] = counts.get(query.kind, 0) + 1
    return dict(sorted(counts.items()))


def write_sample(eval_set: EvalSet, path: Path, limit: int = 12) -> Path:
    """写一份**脱敏小样例**到 ``docs/examples/``（随仓库提交，方便别人看懂格式）。

    每种查询类型各取一条（先看懂格式比看全量更重要），剩下的名额按类型轮换补齐 ——
    样本要能代表整套评测集，而不是清一色阈值查表。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    by_kind: dict[str, list[EvalQuery]] = {}
    for query in eval_set.queries:
        by_kind.setdefault(query.kind, []).append(query)

    picked: list[EvalQuery] = []
    for kind in sorted(by_kind):
        picked.append(by_kind[kind][0])
    kinds = sorted(by_kind)
    round_no = 1
    while len(picked) < limit and kinds:
        added = False
        for kind in kinds:
            group = by_kind[kind]
            if round_no < len(group):
                picked.append(group[round_no])
                added = True
                if len(picked) >= limit:
                    break
        if not added:
            break
        round_no += 1

    with path.open("w", encoding="utf-8") as handle:
        for query in picked[:limit]:
            handle.write(json.dumps(query.to_dict(), ensure_ascii=False, sort_keys=True))
            handle.write("\n")
    return path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="构造召回评测集（标注查询集）")
    parser.add_argument("--out", default=str(DEFAULT_EVAL_SET))
    parser.add_argument("--sample-out", default=str(DEFAULT_SAMPLE))
    parser.add_argument("--threshold", default=str(DEFAULT_THRESHOLD))
    parser.add_argument("--policy", default=str(DEFAULT_POLICY))
    parser.add_argument("--corpus", default=str(DEFAULT_CORPUS))
    parser.add_argument("--truth", default=str(DEFAULT_TRUTH))
    parser.add_argument("--corpus-queries", type=int, default=72)
    parser.add_argument("--print-sample", action="store_true")
    args = parser.parse_args(argv)

    corpus_path = Path(args.corpus)
    truth_path = Path(args.truth)
    corpus_state = ensure_corpus(corpus_path, truth_path)
    print("[build_eval_set] 语料 {}".format(corpus_state["status"]))

    eval_set = build_eval_set(
        threshold_path=Path(args.threshold),
        policy_path=Path(args.policy),
        corpus_path=corpus_path,
        truth_path=truth_path,
        corpus_queries=args.corpus_queries,
    )

    out_path = write_eval_set(eval_set, args.out)
    sample_path = write_sample(eval_set, Path(args.sample_out))

    counts = eval_set.meta["counts"]
    print("[build_eval_set] 评测集 {} 条（正例 {} / 负例 {}）→ {}".format(
        counts["total"], counts["positive"], counts["negative"], out_path))
    for kind, number in counts["by_kind"].items():
        print("[build_eval_set]   {} {}".format(kind, number))
    print("[build_eval_set] 小样例 → {}".format(sample_path))

    if args.print_sample:
        for query in eval_set.queries[:5]:
            print(json.dumps(query.to_dict(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
