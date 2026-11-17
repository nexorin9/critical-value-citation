#!/usr/bin/env python3
"""召回评测：跑一遍评测集，输出三个指标（命中率 / Top-N 命中率 / 出处覆盖率）。

评测集由 ``scripts/build_eval_set.py`` 生成（期望命中条目取自生成器真值），
本脚本负责**跑**它并把结果落成可复算的证据：

* ``reports/eval_report.md``   —— 人读的 Markdown 报告（三个指标 + 分类指标 + 未命中清单）；
* ``reports/eval_report.json`` —— 机器读的完整结果（含逐条查询的命中情况）。

参数面（结构化路权重 / 词法路权重 / 检索深度 / Top-N）都可以在命令行上改 ——
任务 id 31 的网格扫描复用同一套入口。

用法::

    python3 scripts/run_eval.py                       # 默认参数面
    python3 scripts/run_eval.py --top-n 3 --depth 20  # 换检索深度与 Top-N
    python3 scripts/run_eval.py --json                # 只打 JSON 到标准输出
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from critval.evaluate import (  # noqa: E402
    default_material_sources,
    load_eval_set,
    render_markdown,
    run_evaluation,
)
from critval.retrieve.fusion import FusionWeights  # noqa: E402

DEFAULT_EVAL_SET = PROJECT_ROOT / "data" / "eval" / "queries.jsonl"
DEFAULT_REPORT_MD = PROJECT_ROOT / "reports" / "eval_report.md"
DEFAULT_REPORT_JSON = PROJECT_ROOT / "reports" / "eval_report.json"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="召回评测（命中率 / Top-N 命中率 / 出处覆盖率）")
    parser.add_argument("--eval-set", default=str(DEFAULT_EVAL_SET))
    parser.add_argument("--threshold", default="")
    parser.add_argument("--policy", default="")
    parser.add_argument("--corpus", default="")
    parser.add_argument("--depth", type=int, default=10, help="检索深度（参与命中率判定）")
    parser.add_argument("--top-n", type=int, default=5, help="前 N 名（参与 Top-N 命中率判定）")
    parser.add_argument("--structured-weight", type=float, default=0.5)
    parser.add_argument("--lexical-weight", type=float, default=0.5)
    parser.add_argument("--candidate-depth", type=int, default=50, help="词法路候选深度")
    parser.add_argument("--citation-mode", default="kind_aware", choices=("kind_aware", "strict"))
    parser.add_argument("--out", default=str(DEFAULT_REPORT_MD))
    parser.add_argument("--json-out", default=str(DEFAULT_REPORT_JSON))
    parser.add_argument("--json", action="store_true", help="只把 JSON 打到标准输出")
    args = parser.parse_args(argv)

    eval_set_path = Path(args.eval_set)
    if not eval_set_path.exists():
        print(
            "评测集不存在：{}\n先跑 `python3 scripts/build_eval_set.py` 生成标注查询集。".format(eval_set_path),
            file=sys.stderr,
        )
        return 2

    sources = default_material_sources(PROJECT_ROOT)
    if args.threshold:
        sources[0] = sources[0].__class__.from_path(args.threshold, kind="threshold_table", key="threshold")
    if args.policy:
        sources[1] = sources[1].__class__.from_path(args.policy, kind="policy_document", key="policy")
    if args.corpus:
        sources[2] = sources[2].__class__.from_path(args.corpus, kind="corpus", key="corpus")
    missing = [source.path for source in sources if not Path(source.path).exists()]
    if missing:
        print("材料缺失：{}\n先跑 `python3 scripts/fetch_data.py` 生成可再生语料。".format("、".join(missing)),
              file=sys.stderr)
        return 2

    eval_set = load_eval_set(eval_set_path)
    weights = FusionWeights(structured=args.structured_weight, lexical=args.lexical_weight)
    try:
        weights.validate()
    except ValueError as error:
        print("参数不合法：{}".format(error), file=sys.stderr)
        return 2

    report = run_evaluation(
        eval_set,
        materials=sources,
        depth=args.depth,
        top_n=args.top_n,
        weights=weights,
        candidate_depth=args.candidate_depth,
        citation_mode=args.citation_mode,
    )

    markdown = render_markdown(report)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(markdown, encoding="utf-8")
    json_path = Path(args.json_out)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )

    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    print("召回评测（{} 条查询：正例 {} / 负例 {}）".format(
        report.query_count, report.positive_query_count, report.negative_query_count))
    print("参数面：结构化路权重 {:.2f} + 词法路权重 {:.2f} ｜ 检索深度 {} ｜ Top-{} ｜ 索引条目 {}".format(
        weights.structured, weights.lexical, report.depth, report.top_n,
        report.meta.get("index_documents", 0)))
    print("  命中率        {:.1%}（命中 {} / 期望 {}）".format(
        report.hit_rate, report.hit_total, report.expected_total))
    print("  Top-{} 命中率  {:.1%}（前 {} 名命中 {} / 期望 {}）".format(
        report.top_n, report.top_n_hit_rate, report.top_n, report.top_n_hit_total, report.expected_total))
    print("  出处覆盖率    {:.1%}（出处齐备 {} / 带出处引用 {}）".format(
        report.citation_coverage, report.citation_complete, report.cited_total))
    print("  补充：Top-{} 全中率 {:.1%} ｜ 出处覆盖率·严格口径 {:.1%} ｜ 结果带出处比例 {:.1%}".format(
        report.top_n, report.top_n_full_hit_rate, report.citation_coverage_strict,
        report.citation_presence_rate))
    if report.meta.get("materials_match") is False:
        print("  ⚠️ 材料指纹与评测集记录不一致（材料改版或语料重建），本轮数字仅供参考。")
    print("报告 → {} ｜ {}".format(out_path, json_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
