#!/usr/bin/env python3
"""索引层规模基准：全量重建 vs 增量重建。

回答的是两个问题：

1. **建得起来吗** —— 数千行条款语料 + 阈值表 + 制度材料，一次性建成索引要多久；
2. **改版扛不扛得住** —— 院内把一份材料换了版本之后，**只重建变了的那一份**
   要多久（这决定共享目录里丢进新版阈值表时，值班台要等多久才恢复检索）。

两个口径必须同时成立才有意义：

* 增量重建**只处理内容变了的那份材料**（按文件 sha256 判定，判据见
  ``critval/index/builder.py``）；
* 增量重建耗时**低于**全量重建 —— 否则"增量"只是名字好听。

语料来源优先用 ``data/synth/policy_corpus.txt``（由 ``scripts/fetch_data.py`` /
``scripts/build_materials.py`` 生成的可再生大语料）；语料缺失时**不空转**，
改用本地确定性合成器并在报告里标注 ``source=synthetic``。

用法::

    python3 scripts/bench_index.py                    # 默认 8 份语料 + 阈值表 + 制度
    python3 scripts/bench_index.py --chunks 16        # 切得更碎
    python3 scripts/bench_index.py --json             # 机器可读
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from critval.index.builder import MaterialIndex, MaterialSource  # noqa: E402

DEFAULT_CORPUS = PROJECT_ROOT / "data" / "synth" / "policy_corpus.txt"
DEFAULT_THRESHOLD = PROJECT_ROOT / "samples" / "threshold" / "示例危急值阈值表.xlsx"
DEFAULT_POLICY = PROJECT_ROOT / "samples" / "policy" / "示例危急值报告制度.docx"
DEFAULT_REPORT = PROJECT_ROOT / "reports" / "index_bench.json"

#: 合成语料的兜底模板（只在真实语料缺失时启用，报告里会标 source=synthetic）。
_SYNTH_TEMPLATE = (
    "CLAUSE-{index:06d}\t第三章 报告流程与通知时限\t"
    "血红蛋白（718-7）危急值结果低于危急值下限 30 g/L 时，检验科值班技师"
    "应当在收到报告后 10 分钟内到岗处置，通知对象：检验科值班技师与检验科组长；"
    "上报时限：10 分钟内；记录去向：危急值登记系统，并同步记入护理交接班记录。"
)


def _synth_lines(count: int) -> list[str]:
    return [_SYNTH_TEMPLATE.format(index=index) for index in range(count)]


def load_corpus_lines(path: Path, fallback_lines: int = 3600) -> tuple[list[str], str]:
    """读语料；读不到就用确定性合成语料兜底（返回来源标记）。"""
    if path.exists():
        lines = [
            line.rstrip("\n").rstrip("\r")
            for line in path.read_text(encoding="utf-8").splitlines()
        ]
        lines = [line for line in lines if line.strip()]
        if lines:
            return lines, "synth-corpus"
    return _synth_lines(fallback_lines), "synthetic-fallback"


def split_corpus(lines: list[str], chunks: int, workdir: Path) -> list[Path]:
    """把语料切成若干份材料文件（一份文件 = 一份材料 = 一次增量重建的粒度）。"""
    chunks = max(1, chunks)
    size = max(1, len(lines) // chunks)
    paths: list[Path] = []
    for number in range(chunks):
        part = lines[number * size : (number + 1) * size] if number < chunks - 1 else lines[
            number * size :
        ]
        if not part:
            continue
        path = workdir / f"chunk-{number:02d}.tsv"
        path.write_text("\n".join(part) + "\n", encoding="utf-8")
        paths.append(path)
    return paths


def _sources(paths: list[Path], threshold: Path, policy: Path) -> list[MaterialSource]:
    sources = [
        MaterialSource.from_path(path, kind="corpus", key=path.name) for path in paths
    ]
    if threshold.exists():
        sources.append(
            MaterialSource.from_path(threshold, kind="threshold_table", key="阈值表")
        )
    if policy.exists():
        sources.append(
            MaterialSource.from_path(policy, kind="policy_document", key="制度")
        )
    return sources


def run_benchmark(
    corpus_path: Path,
    threshold: Path,
    policy: Path,
    chunks: int,
) -> dict[str, Any]:
    lines, corpus_source = load_corpus_lines(corpus_path)
    with tempfile.TemporaryDirectory(prefix="critval-index-bench-") as tmp:
        workdir = Path(tmp)
        chunk_paths = split_corpus(lines, chunks, workdir)
        sources = _sources(chunk_paths, threshold, policy)

        index = MaterialIndex()
        index.rebuild(sources)  # 预热：首次导入成本不计入对照

        started = time.perf_counter()
        full = index.full_rebuild(sources)
        full_seconds = time.perf_counter() - started

        # 改一份材料（改的是内容，不是文件名）→ 只该重建这一份
        changed = chunk_paths[len(chunk_paths) // 2]
        changed.write_text(
            changed.read_text(encoding="utf-8").replace("上报时限：10 分钟内", "上报时限：15 分钟内"),
            encoding="utf-8",
        )
        started = time.perf_counter()
        incremental = index.rebuild(_sources(chunk_paths, threshold, policy))
        incremental_seconds = time.perf_counter() - started

        stats = index.stats()
        result = {
            "corpus_source": corpus_source,
            "corpus_lines": len(lines),
            "materials": len(sources),
            "chunk_files": len(chunk_paths),
            "documents": index.doc_count(),
            "full_rebuild_documents": full.docs_after,
            "full_rebuild_seconds": round(full_seconds, 6),
            "incremental_rebuild_seconds": round(incremental_seconds, 6),
            "speedup": round(full_seconds / incremental_seconds, 3)
            if incremental_seconds > 0
            else None,
            "incremental_updated": incremental.updated,
            "incremental_unchanged": incremental.unchanged,
            "inverted": stats["inverted"],
            "structured": stats["structured"],
            "verdict": "ok" if incremental_seconds < full_seconds else "regression",
        }
    return result


def render(result: dict[str, Any]) -> str:
    lines = [
        "索引规模基准",
        f"  语料来源        {result['corpus_source']}（{result['corpus_lines']} 行 / "
        f"{result['chunk_files']} 份材料）",
        f"  入库条目        {result['documents']} 条（倒排词项 "
        f"{result['inverted']['term_count']} 个 / 结构化键 "
        f"{len(result['structured']['keys'])} 个）",
        f"  全量重建        {result['full_rebuild_seconds']:.3f}s",
        f"  增量重建        {result['incremental_rebuild_seconds']:.3f}s"
        f"（变更 {result['incremental_updated']} 份 / 未变 "
        f"{result['incremental_unchanged']} 份）",
        f"  加速比          {result['speedup']}×",
        f"  结论            {'增量重建快于全量重建 ✔' if result['verdict'] == 'ok' else '增量重建未跑赢全量重建 ✘'}",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="索引层规模基准（全量 vs 增量重建）")
    parser.add_argument("--corpus", default=str(DEFAULT_CORPUS), help="条款语料路径")
    parser.add_argument("--threshold", default=str(DEFAULT_THRESHOLD), help="阈值表 xlsx 路径")
    parser.add_argument("--policy", default=str(DEFAULT_POLICY), help="制度 docx 路径")
    parser.add_argument("--chunks", type=int, default=8, help="语料切成几份材料")
    parser.add_argument("--report", default=str(DEFAULT_REPORT), help="报告 JSON 落盘路径")
    parser.add_argument("--json", action="store_true", help="只输出 JSON")
    args = parser.parse_args(argv)

    result = run_benchmark(
        Path(args.corpus), Path(args.threshold), Path(args.policy), args.chunks
    )

    report = Path(args.report)
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(render(result))
        print(f"  报告            {report}")

    return 0 if result["verdict"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
