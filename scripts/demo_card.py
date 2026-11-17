#!/usr/bin/env python3
"""样例材料 → 一张带出处的处置卡（一条命令可复核）。

用法：

    python3 scripts/demo_card.py
    python3 scripts/demo_card.py --item 2823-3 --value 6.8 --unit mmol/L
    python3 scripts/demo_card.py --format text      # 离线纯文本版
    python3 scripts/demo_card.py --format html      # 值班台 HTML 片段

跑的是产品主路径的最窄一条：阈值表 xlsx → 行级条目 → 危急判定 →
处置卡四要素装配 → 三渠道渲染（企微 Markdown / 值班台 HTML / 离线纯文本）
→ 落一张**不可变快照**。输出里必须能读到：阈值表文件名、可读行号、
版本号、上报时限分钟数、记录去向。

制度条款的读取用的是本脚本内的**最小读取器**（只认「通知对象：…；
上报时限：…；记录去向：…」这种成对出现的行）；正式制度解析器在
``critval/kb`` 里（条款级条目 + 版本生效期 + 人工确认队列），
本脚本不冒充它。
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
from critval.card.provenance import (  # noqa: E402
    ORIGIN_CACHE,
    ORIGIN_HISTORICAL,
    retrieval_provenance,
    roster_provenance,
    threshold_provenance,
)
from critval.card.renderer import render_card  # noqa: E402
from critval.card.snapshot import CardSnapshotStore, default_snapshot_dir  # noqa: E402
from critval.config import Config  # noqa: E402
from critval.judge.critical import judge_event  # noqa: E402
from critval.kb.loader import load_threshold_table  # noqa: E402
from critval.models import CardProvenance, CriticalEvent  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _samplerefs import read_sample_clause  # noqa: E402

DEFAULT_THRESHOLD = PROJECT_ROOT / "samples" / "threshold" / "示例危急值阈值表.xlsx"
DEFAULT_POLICY = PROJECT_ROOT / "samples" / "policy" / "示例危急值报告制度.docx"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="从样例材料产出一张带出处的处置卡")
    parser.add_argument("--threshold", default=str(DEFAULT_THRESHOLD), help="阈值表 xlsx 路径")
    parser.add_argument("--policy", default=str(DEFAULT_POLICY), help="院内危急值报告制度 docx 路径")
    parser.add_argument("--item", default="718-7", help="检验项目码")
    parser.add_argument("--value", default="25", help="观察值")
    parser.add_argument("--unit", default="g/L", help="上报单位")
    parser.add_argument("--dept", default="ICU", help="患者所在科室")
    parser.add_argument("--sex", default=None, help="性别（用于参考区间分档）")
    parser.add_argument("--age", type=int, default=None, help="年龄（用于参考区间分档）")
    parser.add_argument(
        "--degrade",
        action="store_true",
        help="演示降级来源标注：当班名单来自缓存 / 阈值行取历史版本 / 制度召回走降级层级",
    )
    parser.add_argument(
        "--format",
        default="markdown",
        choices=["markdown", "text", "html"],
        help="输出渠道：markdown=企微推送原文 / text=离线纯文本 / html=值班台片段",
    )
    parser.add_argument(
        "--confirm-url",
        default="http://127.0.0.1:8848/events/demo-event-0001/ack",
        help="处置卡里的确认回执链接",
    )
    parser.add_argument(
        "--snapshot-dir",
        default=str(default_snapshot_dir()),
        help="处置卡快照落盘目录（默认 data/card_snapshots）",
    )
    parser.add_argument(
        "--no-snapshot",
        dest="snapshot",
        action="store_false",
        help="不落处置卡快照",
    )
    parser.add_argument(
        "--observed-at",
        default="2026-01-02 08:30:00",
        help="事件的观察时刻（默认取样例时刻，便于演示可复现）",
    )
    parser.set_defaults(snapshot=True)
    args = parser.parse_args(argv)

    Config.init_env()

    observed_at = datetime.strptime(args.observed_at, "%Y-%m-%d %H:%M:%S")

    table = load_threshold_table(args.threshold)
    print(f"[材料解析] {table.report.summary()}")
    if not table.report.ok:
        for row in table.report.unrecognized_rows:
            print(f"  ! 未识别行 第 {row.row_no} 行：{row.reason}")

    row = table.first(args.item)
    if row is None:
        print(f"[判定] 阈值表未收录项目 {args.item}，不出卡（转人工）")
        return 2

    event = CriticalEvent(
        event_id="demo-event-0001",
        message_control_id="DEMOMSG0001",
        specimen_no="DEMO-SPEC-0001",
        item_code=args.item,
        item_name=row.item_name,
        value=Decimal(args.value),
        unit=args.unit,
        observed_at=observed_at,
        dept_code=args.dept,
        source_channel="demo",
    )
    judgement = judge_event(event, table.rows, sex=args.sex, age=args.age)
    clause = read_sample_clause(Path(args.policy))

    duty_person = ""
    provenance = None
    if args.degrade:
        # 降级演示：把三处来源都换成降级来的，卡片头部会逐条标出来。
        duty_person = "李技师"
        version_note = f"版本 {row.version}"
        if row.effective_from:
            version_note += f"，生效期 {row.effective_from.isoformat()}"
        provenance = CardProvenance(
            duty_roster=roster_provenance(
                ORIGIN_CACHE,
                as_of=datetime.now().strftime("%Y-%m-%d %H:%M"),
            ),
            threshold_table=threshold_provenance(ORIGIN_HISTORICAL, detail=version_note),
            policy_retrieval=retrieval_provenance(level=2, detail="章节级兜底"),
        )

    card = build_card(
        event,
        judgement,
        policy_clause=clause,
        duty_person=duty_person or None,
        provenance=provenance,
    )
    print()
    print(render_card(card, args.format, args.confirm_url))
    print()
    print(
        f"[卡片] {card.card_id} · 判定 {judgement.decision}"
        f" · 缺项 {list(card.missing_elements()) or '无'}"
        f" · 降级 {list(card.degraded_elements()) or '无'}"
    )
    if args.snapshot:
        snapshot = CardSnapshotStore(args.snapshot_dir).save(card, args.confirm_url)
        print(
            f"[快照] {snapshot.snapshot_id} · 落盘 {args.snapshot_dir}/{snapshot.snapshot_id}.json"
            f" · 阈值表版本 {snapshot.threshold_version or '（无）'}"
            f" · 制度版本 {snapshot.policy_version or '（无）'}"
        )
        print(f"[复核] python3 -m critval.cli export --event {card.event.event_id} --format text --verify")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
