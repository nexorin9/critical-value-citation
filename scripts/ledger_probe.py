#!/usr/bin/env python3
"""台账探针：往台账里投一条事件（并发 / 跨进程写竞争的运维入口）。

守护进程与运维 CLI 会**同时**开同一个库（WAL + busy_timeout 就是为这一天
准备的）。要验证这套加固真的有效，最直接的办法就是让多个进程同时往
同一个库里投同一帧 —— 这条脚本就是那个「另一个进程」：

    python3 scripts/ledger_probe.py --db /tmp/t.db --mcid MSG-1 --item 718-7 \
        --value 25 --unit g/dL --observed-at 2026-01-01T10:00:00

同一条参数跑 N 遍（N 个进程同时跑），台账里应该**只有一条**事件 —— 幂等键
是 ``MSH-10 + OBX-3 + OBX-14`` 的 sha256，跨进程也成立。

输出（每进程一行 JSON）::

    {"event_id": "evt-…", "recorded": true, "duplicate": false, "events": 3}

``recorded=true`` 表示这一条是**新事件**；``false`` 表示幂等键已存在
（重复到达，既有事实不被覆盖）。N 个进程里应当**恰好一个**是 true。

用法::

    python3 scripts/ledger_probe.py --db /tmp/t.db --mcid MSG-1 --item 718-7 \\
        --value 25 --unit g/dL --observed-at 2026-01-01T10:00:00
    python3 scripts/ledger_probe.py --db /tmp/t.db --mcid MSG-1 --item 718-7 \\
        --value 25 --unit g/dL --observed-at 2026-01-01T10:00:00 --json
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from critval.ledger.idempotency import build_idempotency_key, event_id_for_key  # noqa: E402
from critval.ledger.store import LedgerStore  # noqa: E402
from critval.models import CriticalEvent  # noqa: E402


def build_event(args: argparse.Namespace) -> CriticalEvent:
    observed_at = datetime.fromisoformat(args.observed_at)
    key = build_idempotency_key(args.mcid, args.item, observed_at)
    return CriticalEvent(
        event_id=event_id_for_key(key),
        message_control_id=args.mcid,
        specimen_no=args.specimen or args.mcid,
        item_code=args.item,
        item_name=args.name or args.item,
        value=Decimal(str(args.value)),
        unit=args.unit,
        observed_at=observed_at,
        dept_code=args.dept,
        source_channel=args.channel,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="往台账里投一条事件（并发探针）")
    parser.add_argument("--db", default=str(PROJECT_ROOT / "data" / "critval.db"), help="SQLite 台账路径")
    parser.add_argument("--mcid", required=True, help="消息控制号（幂等键的一部分）")
    parser.add_argument("--item", required=True, help="检验项目码（幂等键的一部分）")
    parser.add_argument("--value", required=True, help="观察值")
    parser.add_argument("--unit", default="", help="单位")
    parser.add_argument("--observed-at", required=True, help="观察时刻 ISO 文本（幂等键的一部分）")
    parser.add_argument("--specimen", default="", help="标本号")
    parser.add_argument("--name", default="", help="项目名")
    parser.add_argument("--dept", default="", help="科室")
    parser.add_argument("--channel", default="probe", help="来源通道")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args(argv)

    event = build_event(args)
    with LedgerStore(args.db) as store:
        recorded = store.record_event(event)
        payload = {
            "event_id": event.event_id,
            "recorded": bool(recorded),
            "duplicate": not recorded,
            "events": store.count_events(),
        }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        state = "新事件" if payload["recorded"] else "重复到达（幂等挡下）"
        print(f"{payload['event_id']} · {state} · 台账事件数 {payload['events']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
