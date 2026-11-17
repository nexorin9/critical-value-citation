#!/usr/bin/env python3
"""召回基线对比：纯 BM25 单路 vs 融合引擎（同一份材料 · 同一份评测集 · 同一把尺子）。

## 这张表回答什么

「把结构化精确路与规则重排拿掉，只剩经典单路词法检索，到底丢了多少？」

对比的是**同一个评测器**（``critval.evaluate.evaluate``）跑出来的两个参赛者：

* **基线**：纯 BM25 单路召回（``critval.retrieve.baseline.BM25BaselineRetriever``）
  —— 不做结构化精确匹配、不做规则重排、不做综合分硬闸门；
* **融合**：产品主路径的混合召回引擎（``critval.retrieve.engine.HybridRetriever``
  ，经 ``HybridEvalRetriever`` 接到评测协议上）。

两侧跑的是**同一份材料、同一份评测集、同样的检索深度与 Top-N**，指标由同一个
函数算 —— 差在哪儿只有召回做法这一处。编排逻辑在 ``critval.compare``，
运维 CLI 的 ``recall-baseline`` 子命令调的是同一份代码。

## 产物

* ``docs/examples/recall_baseline_compare.md`` —— 人读的对比表（含分类指标与复算命令）；
* ``reports/recall_baseline_compare.csv`` —— 机器读的两行数据。

两个产物都**不含时间戳**：同一命令跑两次应得到逐字节相同的文件，否则
「可复算」无从谈起。

用法::

    python3 scripts/compare_baseline.py                      # 默认参数面
    python3 scripts/compare_baseline.py --depth 20 --top-n 5  # 换检索深度与 Top-N
    python3 scripts/compare_baseline.py --json               # 只打 JSON 到标准输出
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from critval.compare import (  # noqa: E402
    compare_baselines,
    render_comparison_markdown,
    write_comparison_csv,
)
from critval.evaluate import default_material_sources, load_eval_set  # noqa: E402
from critval.retrieve.params import RetrievalParams  # noqa: E402

DEFAULT_EVAL_SET = PROJECT_ROOT / "data" / "eval" / "queries.jsonl"
DEFAULT_MD = PROJECT_ROOT / "docs" / "examples" / "recall_baseline_compare.md"
DEFAULT_CSV = PROJECT_ROOT / "reports" / "recall_baseline_compare.csv"

#: 两侧共用的材料范围键（阈值表 / 制度 / 条款语料）。
MATERIAL_KEYS = ("threshold", "policy", "corpus")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="召回基线对比（纯 BM25 单路 vs 融合引擎）"
    )
    parser.add_argument("--eval-set", default=str(DEFAULT_EVAL_SET))
    parser.add_argument("--threshold", default="")
    parser.add_argument("--policy", default="")
    parser.add_argument("--corpus", default="")
    parser.add_argument("--depth", type=int, default=10, help="检索深度（参与命中率判定）")
    parser.add_argument("--top-n", type=int, default=5, help="前 N 名（参与 Top-N 命中率判定）")
    parser.add_argument("--similarity-threshold", type=float, default=0.2, help="融合侧综合分硬闸门")
    parser.add_argument("--structured-weight", type=float, default=0.5, help="结构化精确路权重")
    parser.add_argument("--lexical-weight", type=float, default=0.5, help="BM25 词法路权重")
    parser.add_argument("--candidate-depth", type=int, default=50, help="两侧词法路候选深度")
    parser.add_argument("--out", default=str(DEFAULT_MD))
    parser.add_argument("--csv-out", default=str(DEFAULT_CSV))
    parser.add_argument("--json", action="store_true", help="只把 JSON 打到标准输出")
    args = parser.parse_args(argv)

    eval_set_path = Path(args.eval_set)
    if not eval_set_path.exists():
        print(
            "评测集不存在：{}\n先跑 `python3 scripts/build_eval_set.py` 生成标注查询集。".format(
                eval_set_path
            ),
            file=sys.stderr,
        )
        return 2

    sources = default_material_sources(PROJECT_ROOT)
    if args.threshold:
        sources[0] = sources[0].__class__.from_path(
            args.threshold, kind="threshold_table", key="threshold"
        )
    if args.policy:
        sources[1] = sources[1].__class__.from_path(
            args.policy, kind="policy_document", key="policy"
        )
    if args.corpus:
        sources[2] = sources[2].__class__.from_path(
            args.corpus, kind="corpus", key="corpus"
        )
    missing = [source.path for source in sources if not Path(source.path).exists()]
    if missing:
        print(
            "材料缺失：{}\n先跑 `python3 scripts/fetch_data.py` 生成可再生语料。".format(
                "、".join(missing)
            ),
            file=sys.stderr,
        )
        return 2

    total = args.structured_weight + args.lexical_weight
    if abs(total - 1.0) > 1e-6:
        print(
            "结构化路权重与词法路权重必须互补（和为 1）："
            "结构化 {:.2f} + 词法 {:.2f} = {:.2f}".format(
                args.structured_weight, args.lexical_weight, total
            ),
            file=sys.stderr,
        )
        return 2

    try:
        params = RetrievalParams(
            similarity_threshold=args.similarity_threshold,
            keywords_similarity_weight=args.lexical_weight,
            top_n=args.depth,
            top_k=1024,
            kb_ids=MATERIAL_KEYS,
        ).check()
    except Exception as error:  # noqa: BLE001 - 参数面报错要落到人话
        print("参数不合法：{}".format(error), file=sys.stderr)
        return 2

    eval_set = load_eval_set(eval_set_path)
    comparison = compare_baselines(
        eval_set,
        sources,
        depth=args.depth,
        top_n=args.top_n,
        params=params,
        candidate_depth=args.candidate_depth,
        kb_ids=MATERIAL_KEYS,
    )

    meta = {
        "positive": len(eval_set.positive_queries),
        "negative": len(eval_set.negative_queries),
    }
    markdown = render_comparison_markdown(comparison, eval_meta=meta)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(markdown, encoding="utf-8")
    csv_path = write_comparison_csv(comparison, Path(args.csv_out))

    payload = comparison.to_dict()
    payload["out"] = str(out_path)
    payload["csv_out"] = str(csv_path)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    rows = comparison.rows
    print(
        "召回基线对比（{} 条查询 · 索引条目 {} · 深度 {} · Top-{}）".format(
            comparison.query_count, comparison.index_documents, args.depth, args.top_n
        )
    )
    for row in rows:
        print(
            "  {:<26} 命中项数 {:>3} / {}（{:.1%}）｜ Top-{} 命中率 {:.1%} ｜ 出处覆盖率 {:.1%}".format(
                row["参赛者"],
                row["命中项数"],
                row["期望条目数"],
                row["命中率"],
                args.top_n,
                row["Top-N命中率"],
                row["出处覆盖率"],
            )
        )
    print(
        "  差值：命中项数 {:+d} 条 ｜ Top-{} 命中率 {:+.1%} ｜ 出处覆盖率 {:+.1%}".format(
            rows[1]["命中项数"] - rows[0]["命中项数"],
            args.top_n,
            rows[1]["Top-N命中率"] - rows[0]["Top-N命中率"],
            rows[1]["出处覆盖率"] - rows[0]["出处覆盖率"],
        )
    )
    print("对比表 → {} ｜ {}".format(out_path, csv_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
