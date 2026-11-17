#!/usr/bin/env python3
"""召回参数敏感度分析：网格扫描 + 可复现调参对比表。

在任务 id 29 生成的评测集上遍历「相似度阈值 × 结构化路权重」网格，每个网格点
跑一遍**线上同一条召回主路径**（两路候选 → 融合 → 综合分硬闸门 → 规则重排 →
截断），输出三个指标（命中率 / Top-N 命中率 / 出处覆盖率），并按固定规则挑出
推荐参数。

产物：

* ``reports/recall_grid.csv``      —— 机器读（每个网格点一行）
* ``docs/examples/recall_tuning.md`` —— 人读（扫描规模 + 三指标表 + 推荐理由），随仓库提交

用法::

    python3 scripts/tune_recall.py                      # 默认网格
    python3 scripts/tune_recall.py --thresholds 0,0.1,0.2 --structured-weights 0.3,0.5,0.7
    python3 scripts/tune_recall.py --limit 40           # 只取前 40 条查询（快速试跑）
    python3 scripts/tune_recall.py --json               # 只打 JSON 到标准输出
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from critval.evaluate import EvalSet, default_material_sources, load_eval_set  # noqa: E402
from critval.tune import (  # noqa: E402
    DEFAULT_STRUCTURED_WEIGHTS,
    DEFAULT_THRESHOLDS,
    build_index,
    recommend,
    scan_grid,
    write_grid_csv,
    write_tuning_markdown,
)

DEFAULT_EVAL_SET = PROJECT_ROOT / "data" / "eval" / "queries.jsonl"
DEFAULT_CSV = PROJECT_ROOT / "reports" / "recall_grid.csv"
DEFAULT_MD = PROJECT_ROOT / "docs" / "examples" / "recall_tuning.md"


def _parse_floats(raw: str) -> tuple[float, ...]:
    """把 ``0,0.1,0.2`` 这样的命令行取值解析成浮点元组。"""
    values: list[float] = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            values.append(float(chunk))
        except ValueError:
            print("取值不是数字：{}".format(chunk), file=sys.stderr)
            raise SystemExit(2)
    return tuple(values)


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="召回参数敏感度分析（网格扫描 + 可复现调参对比表）")
    parser.add_argument("--eval-set", default=str(DEFAULT_EVAL_SET))
    parser.add_argument(
        "--thresholds",
        default=",".join(str(value) for value in DEFAULT_THRESHOLDS),
        help="相似度阈值网格（逗号分隔）",
    )
    parser.add_argument(
        "--structured-weights",
        default=",".join(str(value) for value in DEFAULT_STRUCTURED_WEIGHTS),
        help="结构化路权重网格（词法路权重 = 1 - 本值）",
    )
    parser.add_argument("--depth", type=int, default=10, help="检索深度（参与命中率判定）")
    parser.add_argument("--top-n", type=int, default=5, help="前 N 名（参与 Top-N 命中率判定）")
    parser.add_argument("--limit", type=int, default=0, help="只取前 N 条查询（0 = 全部）")
    parser.add_argument("--citation-mode", default="kind_aware", choices=("kind_aware", "strict"))
    parser.add_argument("--out-csv", default=str(DEFAULT_CSV))
    parser.add_argument("--out-md", default=str(DEFAULT_MD))
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
    missing = [source.path for source in sources if not Path(source.path).exists()]
    if missing:
        print(
            "材料缺失：{}\n先跑 `python3 scripts/fetch_data.py` 生成可再生语料。".format("、".join(missing)),
            file=sys.stderr,
        )
        return 2

    thresholds = _parse_floats(args.thresholds)
    structured_weights = _parse_floats(args.structured_weights)

    eval_set = load_eval_set(eval_set_path)
    if args.limit and args.limit > 0:
        eval_set = EvalSet(
            queries=eval_set.queries[: args.limit],
            meta=dict(eval_set.meta),
        )

    index = build_index(sources, PROJECT_ROOT)
    try:
        points = scan_grid(
            eval_set,
            index,
            thresholds=thresholds,
            structured_weights=structured_weights,
            depth=args.depth,
            top_n=args.top_n,
            citation_mode=args.citation_mode,
        )
    except ValueError as error:
        print("参数不合法：{}".format(error), file=sys.stderr)
        return 2

    recommendation, eligible, best_overall = recommend(points)
    meta = {
        "query_count": len(eval_set.queries),
        "materials": [
            {"name": Path(source.path).name, "kind": source.kind, "sha256": source.sha256}
            for source in sources
        ],
        "eval_set_sha256": _sha256_of(eval_set_path),
        "notes": _notes(points, recommendation, best_overall),
    }

    csv_path = write_grid_csv(points, args.out_csv)
    md_path = write_tuning_markdown(
        points,
        recommendation,
        args.out_md,
        meta=meta,
        best_overall=best_overall,
        eligible=eligible,
    )

    payload = {
        "points": [point.to_dict() for point in points],
        "recommendation": recommendation.to_dict() if recommendation else None,
        "best_overall": best_overall.to_dict() if best_overall else None,
        "eligible_count": len(eligible),
        "meta": meta,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    print(
        "召回参数敏感度扫描：{} 个网格点（合格 {} · 排除 {}）｜ 查询 {} 条 · 检索深度 {} · Top-{}".format(
            len(points),
            len(eligible),
            len(points) - len(eligible),
            len(eval_set.queries),
            args.depth,
            args.top_n,
        )
    )
    if recommendation is None:
        print("  没有合格点 —— 没有同时满足「可交付」与「出处覆盖率 100%」的网格点，按规则不推荐。")
    else:
        print(
            "  推荐参数：阈值 {:.2f} · 结构化路权重 {:.2f} · 词法路权重 {:.2f}".format(
                recommendation.threshold,
                recommendation.structured_weight,
                recommendation.lexical_weight,
            )
        )
        print(
            "    命中率 {:.1%}（{}/{}）｜ Top-{} 命中率 {:.1%}（{}/{}）｜ 出处覆盖率 {:.1%}｜ 负例噪声 {}".format(
                recommendation.hit_rate,
                recommendation.hit_total,
                recommendation.expected_total,
                recommendation.top_n,
                recommendation.top_n_hit_rate,
                recommendation.top_n_hit_total,
                recommendation.expected_total,
                recommendation.citation_coverage,
                recommendation.negative_retrieved,
            )
        )
    if best_overall is not None and best_overall is not recommendation:
        print(
            "  指标最优（不可交付）：阈值 {:.2f} · 结构化权重 {:.2f}（Top-{} 命中率 {:.1%}）—— {}".format(
                best_overall.threshold,
                best_overall.structured_weight,
                best_overall.top_n,
                best_overall.top_n_hit_rate,
                best_overall.excluded_reason or "未通过推荐规则",
            )
        )
    print("  对比表 → {} ｜ {}".format(csv_path, md_path))
    return 0


def _notes(
    points: list,
    recommendation,
    best_overall,
) -> list[str]:
    """写进报告的「已知短板与后续」条目（由本轮数字推出，不写死结论）。"""
    notes: list[str] = []
    if not points:
        return notes
    tiers = sorted({(round(point.top_n_hit_rate, 6)) for point in points}, reverse=True)
    if len(tiers) > 1:
        notes.append(
            "命中率在本评测集上**分辨力有限**：{} 个不同档位（最高 {:.1%}）。"
            "阈值与权重在合格区间内的微调多数不改变「期望条目是否落进前 N」，"
            "因此推荐规则用「闸门截断更少 / 结构化权重更大」作为平手裁决键，"
            "而不是宣称某个点明显更好。".format(len(tiers), tiers[0])
        )
    else:
        notes.append(
            "本轮全部网格点的 Top-N 命中率相同（{:.1%}）—— 阈值与权重在这段区间里"
            "没有改变「期望条目是否落进前 N」。".format(tiers[0])
        )
    if best_overall is not None and recommendation is not None and best_overall is not recommendation:
        notes.append(
            "指标最优的点（{label}，Top-{n} 命中率 {rate:.1%}）把两路权重推到边界、"
            "把阈值压到 0：它买到了「结构化精确命中的制度条款排到 BM25 高分语料行前面」，"
            "但代价是**综合分硬闸门失效**（召回永远不为空，「没依据就说没依据、转人工」"
            "走不到），且只被词法路命中的依据综合分恒为 0。它已按规则排除，"
            "但结论写在这里，不藏。".format(
                label=best_overall.label,
                n=best_overall.top_n,
                rate=best_overall.top_n_hit_rate,
            )
        )
    notes.append(
        "负例噪声列（材料里没有该项目的查询召回了多少条）是**唯一的噪声旁证**："
        "三个指标只奖励「找到」不惩罚「多召回」，只看命中率会把阈值一路压到 0。"
    )
    if recommendation is not None and recommendation.by_kind:
        weak = [
            (kind, item)
            for kind, item in recommendation.by_kind.items()
            if int(item.get("expected", 0)) > 0
            and float(item.get("top_n_hit_rate", 0.0)) < 1.0
        ]
        if weak:
            worst_kind, worst_item = min(weak, key=lambda pair: pair[1]["top_n_hit_rate"])
            notes.append(
                "推荐参数下拖后腿的查询族：**{kind}**（Top-{n} 命中率 {rate:.1%}，"
                "期望条目 {exp} 条）—— 这一族一条查询要**多处依据**（如通知对象与上报时限"
                "分属不同条款），两处要同时落进前 N；而 BM25 得分更高的条款语料行会把"
                "精确命中的制度条款挤出前 N。差异归因留给后续的「双路召回差异归因」任务。".format(
                    kind=worst_kind,
                    n=recommendation.top_n,
                    rate=float(worst_item["top_n_hit_rate"]),
                    exp=worst_item.get("expected", 0),
                )
            )
    return notes


if __name__ == "__main__":
    raise SystemExit(main())
