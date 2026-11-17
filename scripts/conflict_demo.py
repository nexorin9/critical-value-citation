#!/usr/bin/env python3
"""双路召回差异归因 → 处置卡待人工裁决区块 → 审计留痕（一条命令可复核）。

用法::

    python3 scripts/conflict_demo.py
    python3 scripts/conflict_demo.py --item 718-7 --format text
    python3 scripts/conflict_demo.py --db /tmp/critval-conflict.db

跑的是产品主路径的一条完整切片：

1. 阈值表 xlsx + 制度 docx → 材料索引（结构化精确路 + 词法 BM25 路）；
2. 两路候选 → **差异归因**（口径冲突 / 版本冲突 / 范围冲突）；
3. 归因结果 → 处置卡**待人工裁决区块**（两方取值 + 两方出处行号）；
4. 冲突 → **审计流水**（可在台账里按事件查回）。

冲突未裁决时，卡片上相关要素标 ``degraded``（原因码
``conflict_pending_adjudication``），**不标「已解决」** —— 系统不替值班人
在两方说法里挑一个。

默认用样例材料跑。样例制度里第七条（10 分钟）与第九条（30 分钟）对同一个
项目给了不同时限，正是院内常见的"口径打架"场景。
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from critval.card.builder import build_card  # noqa: E402
from critval.card.renderer import render_card  # noqa: E402
from critval.config import Config  # noqa: E402
from critval.index.builder import MaterialIndex, MaterialSource  # noqa: E402
from critval.judge.critical import judge_event  # noqa: E402
from critval.kb.binding import BindingTable, load_binding_table  # noqa: E402
from critval.kb.loader import load_threshold_table  # noqa: E402
from critval.kb.policy import load_policy_document  # noqa: E402
from critval.ledger.store import LedgerStore  # noqa: E402
from critval.models import CriticalEvent  # noqa: E402
from critval.retrieve.conflict import (  # noqa: E402
    PATH_LEXICAL,
    PATH_STRUCTURED,
    attribute_conflicts,
    classify_conflicts,
    clause_evidence,
    describe_conflict,
    threshold_evidence,
    write_conflict_audit,
)
from critval.retrieve.engine import HybridRetriever  # noqa: E402
from critval.retrieve.params import RetrievalParams  # noqa: E402

DEFAULT_THRESHOLD = PROJECT_ROOT / "samples" / "threshold" / "示例危急值阈值表.xlsx"
DEFAULT_POLICY = PROJECT_ROOT / "samples" / "policy" / "示例危急值报告制度.docx"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="两路召回差异归因 → 处置卡待裁决区块 → 审计")
    parser.add_argument("--threshold", default=str(DEFAULT_THRESHOLD), help="阈值表 xlsx 路径")
    parser.add_argument("--policy", default=str(DEFAULT_POLICY), help="危急值报告制度 docx 路径")
    parser.add_argument("--item", default="718-7", help="检验项目码（默认 718-7 血红蛋白）")
    parser.add_argument("--value", default="25", help="观察值")
    parser.add_argument("--unit", default="g/L", help="上报单位")
    parser.add_argument("--dept", default="ICU", help="患者所在科室")
    parser.add_argument(
        "--query",
        default="危急值 上报时限 通知对象",
        help="词法路的自然语言查询",
    )
    parser.add_argument(
        "--format",
        default="markdown",
        choices=["markdown", "text", "html"],
        help="处置卡渲染渠道",
    )
    parser.add_argument("--db", default=":memory:", help="台账 SQLite 路径（默认内存库）")
    parser.add_argument(
        "--bindings",
        default=None,
        help="版本绑定表 JSON 路径（项目码 → 阈值表版本 + 制度版本；给了就按绑定召回）",
    )
    parser.add_argument(
        "--observed-at",
        default="2026-01-02 08:30:00",
        help="事件的观察时刻（固定值便于演示复现）",
    )
    args = parser.parse_args(argv)

    Config.init_env()

    threshold_path = Path(args.threshold)
    policy_path = Path(args.policy)
    for path in (threshold_path, policy_path):
        if not path.exists():
            print(f"材料不存在：{path}", file=sys.stderr)
            return 2

    table = load_threshold_table(threshold_path)
    policy_document = load_policy_document(policy_path)
    if policy_document.degraded:
        print(f"制度解析降级：{policy_document.degrade_reason}", file=sys.stderr)
        return 1

    index = MaterialIndex()
    index.rebuild(
        [
            MaterialSource.from_path(threshold_path, kind="threshold_table", key="threshold"),
            MaterialSource.from_path(policy_path, kind="policy_document", key="policy"),
        ]
    )
    params = RetrievalParams(
        kb_ids=("threshold", "policy"),
        top_n=8,
        top_k=64,
        similarity_threshold=0.2,
    ).check()
    bindings = BindingTable()
    if args.bindings:
        bindings = load_binding_table(args.bindings)
    retriever = HybridRetriever(index, params, bindings=bindings)
    result = retriever.retrieve(args.query, {"item_code": args.item})

    rows_by_line = {
        row.citation.line_no: row for row in table.rows if row.citation.line_no is not None
    }
    clauses_by_para = {
        clause.citation.paragraph_no: clause
        for clause in policy_document.clauses
        if clause.citation.paragraph_no is not None
    }

    structured_evidence = []
    lexical_evidence = []
    for hit in result.hits:
        citation = hit.citation
        if citation is None:
            continue
        if citation.kind == "threshold_row":
            row = rows_by_line.get(citation.line_no)
            if row is None:
                continue
            evidence = threshold_evidence(row, stable_key=hit.stable_key)
        elif citation.kind == "policy_clause":
            clause = clauses_by_para.get(citation.paragraph_no)
            if clause is None:
                continue
            evidence = clause_evidence(clause, stable_key=hit.stable_key)
        else:
            continue
        if PATH_STRUCTURED in hit.paths:
            structured_evidence.append(evidence)
        if PATH_LEXICAL in hit.paths:
            lexical_evidence.append(evidence)

    binding = bindings.for_item(args.item)
    conflicts = attribute_conflicts(
        args.item,
        structured_evidence,
        lexical_evidence,
        effective_threshold_version=binding.threshold_version if binding else "",
        effective_policy_version=binding.policy_version if binding else "",
    )

    row = table.first(args.item)
    if row is None:
        print(f"阈值表未收录项目 {args.item}", file=sys.stderr)
        return 1
    observed_at = datetime.strptime(args.observed_at, "%Y-%m-%d %H:%M:%S")
    event = CriticalEvent(
        event_id="demo-conflict-0001",
        message_control_id="CONFMSG0001",
        specimen_no="DEMO-SPEC-CONF-0001",
        item_code=args.item,
        item_name=row.item_name,
        value=Decimal(args.value),
        unit=args.unit,
        observed_at=observed_at,
        dept_code=args.dept,
        source_channel="demo",
    )
    judgement = judge_event(event, table.rows)
    clause = clauses_by_para.get(19) or policy_document.clauses[0]

    card = build_card(event, judgement, policy_clause=clause, conflicts=conflicts)

    store = LedgerStore(args.db)
    try:
        store.record_event(event)
        store.record_judgement(event.event_id, judgement)
        store.record_card(card)
        written = write_conflict_audit(
            store,
            conflicts,
            event_id=event.event_id,
            item_code=args.item,
            specimen_no=event.specimen_no,
            message_control_id=event.message_control_id,
            dept_code=args.dept,
        )
        audit_rows = store.audit_entries(event.event_id)
    finally:
        store.close()

    print(
        f"[材料] 阈值表 {threshold_path.name} · 制度 {policy_path.name} "
        f"（制度 {policy_document.version}）"
    )
    print(
        f"[召回] 结构化精确路 {len(structured_evidence)} 条 · "
        f"词法 BM25 路 {len(lexical_evidence)} 条 → "
        f"整体归因：{classify_conflicts(conflicts)} · 冲突 {len(conflicts)} 条"
    )
    if binding:
        print(
            f"[版本绑定] {binding.item_code} → 阈值表 {binding.threshold_version} + "
            f"制度 {binding.policy_version}"
        )
    for number, conflict in enumerate(conflicts, start=1):
        print(f"  {number}. {describe_conflict(conflict)}")

    print()
    print(render_card(card, args.format, "http://127.0.0.1:8848/events/demo-conflict-0001/ack"))
    print()
    print(
        f"[卡片] {card.card_id} · 判定 {judgement.decision}"
        f" · 缺项 {list(card.missing_elements()) or '无'}"
        f" · 降级 {list(card.degraded_elements()) or '无'}"
        f" · 待裁决 {list(card.conflicting_elements()) or '无'}"
    )
    print(
        f"[审计] 冲突留痕 {len(written)} 条 · 事件 {event.event_id} "
        f"的审计共 {len(audit_rows)} 条 → 台账 {args.db}"
    )
    for row in audit_rows:
        print(f"  · {row['action']} ｜ {row['detail']}")
    print("[提示] 冲突未裁决前，卡片上相关要素不标「已解决」；请在值班台确认页裁决后再确认回执。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
