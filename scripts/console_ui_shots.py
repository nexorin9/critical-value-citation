#!/usr/bin/env python3
"""值班台界面截图采集（任务 id 50 / 14）。

**这个脚本干什么**

把值班台的**三态**（加载 / 空 / 错误）、**写操作反馈**（toast）与
**破坏性操作的二次确认模态框**，用**真实运行的值班台服务**渲染一遍并截图，
落到 ``docs/screenshots/``。

**三条纪律**

1. **截的是真页面**，不是设计稿：起的是真的 ``critval.console.app``
   服务，用真的台账数据（``data/demo/critval.db`` 的副本），走真的路由。
2. **不改仓库里的样例数据**：台账与快照目录都复制到临时目录再改。
3. **不引前端构建链**：截图靠 Playwright 驱动系统 Chromium，与值班台本身
   的部署无关 —— 值班机上仍然只要一个 Python。

跑法::

    python3 scripts/console_ui_shots.py --out docs/screenshots

依赖：Playwright（``pip install playwright`` + ``playwright install chromium``）。
没装就明确报错退出，**不产出占位图**。
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _fail(message: str) -> int:
    print(f"[console_ui_shots] {message}", file=sys.stderr)
    return 1


def _copy_demo(tmp: Path) -> tuple[Path, Path]:
    """复制样例台账与快照到临时目录（**不动仓库里的样例数据**）。"""
    db_src = PROJECT_ROOT / "data" / "demo" / "critval.db"
    snap_src = PROJECT_ROOT / "data" / "demo" / "card_snapshots"
    if not db_src.exists():
        raise FileNotFoundError(f"找不到样例台账：{db_src}")
    db_dst = tmp / "critval.db"
    shutil.copy2(db_src, db_dst)
    snap_dst = tmp / "card_snapshots"
    if snap_src.exists():
        shutil.copytree(snap_src, snap_dst)
    else:
        snap_dst.mkdir(parents=True, exist_ok=True)
    return db_dst, snap_dst


def _pick_events(db_path: Path) -> list[str]:
    """取两条事件号（一条用来截确认页，一条改成「已转人工」截 toast）。"""
    conn = sqlite3.connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT event_id FROM events ORDER BY observed_at"
        ).fetchall()
    finally:
        conn.close()
    return [str(row["event_id"]) for row in rows]


def _mark_transferred(db_path: Path, event_id: str) -> None:
    """把一条事件落成「已转人工」—— 用来截「转人工」那类 toast。

    走的是**真实状态跃迁**（``notified → transferred_to_human``），
    不是直接改库里的状态字段。
    """
    from critval.ledger.store import LedgerStore

    store = LedgerStore(str(db_path))
    try:
        store.set_status(
            event_id,
            "transferred_to_human",
            actor="截图采集脚本",
            detail="用于截「已转人工」反馈的样例状态",
        )
    finally:
        store.close()


def _serve(db_path: Path, snapshot_dir: Path, host: str, port: int):
    """起真的值班台服务（后台线程）。"""
    from critval.console.app import make_server
    from critval.console.pages import ConsoleRouter
    from critval.console.inbound import InboundService
    from critval.ledger.store import LedgerStore

    store = LedgerStore(str(db_path))
    service = InboundService(token="", store=store)
    service.console_router = ConsoleRouter(
        store,
        token="",
        snapshot_dir=str(snapshot_dir),
        base_url=f"http://{host}:{port}",
    )
    server = make_server(host, port, service=service)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.4)
    return server, store


def _shot(page, url: str, out: Path, *, width: int = 1440, height: int = 900) -> None:
    page.set_viewport_size({"width": width, "height": height})
    page.goto(url, wait_until="networkidle")
    page.screenshot(path=str(out))
    print(f"[console_ui_shots] 已截图：{out}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python3 scripts/console_ui_shots.py",
        description="值班台三态 / toast / 二次确认模态框的真实渲染截图",
    )
    parser.add_argument("--out", default="docs/screenshots", help="截图输出目录")
    parser.add_argument("--host", default="127.0.0.1", help="服务监听地址")
    parser.add_argument("--port", type=int, default=8799, help="服务监听端口")
    args = parser.parse_args(argv)

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return _fail(
            "没装 Playwright —— 先 pip install playwright && playwright install chromium。"
            "（不产出占位图：本脚本要么截真页面，要么明确失败。）"
        )

    out_dir = Path(args.out)
    if not out_dir.is_absolute():
        out_dir = PROJECT_ROOT / out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="cv-shots-") as tmp_name:
        tmp = Path(tmp_name)
        db_path, snap_dir = _copy_demo(tmp)
        events = _pick_events(db_path)
        if len(events) < 2:
            return _fail("样例台账里不足两条事件，截不了确认页与转人工反馈。")
        plain_event, transferred_event = events[0], events[1]
        _mark_transferred(db_path, transferred_event)

        # 空台账：另起一个**全新**的库文件给「空态」截图用。
        #
        # 不复制样例库再删行 —— 审计表是 append-only（库级触发器拦着），
        # 删审计行会被当场拒掉；建一个空库才是干净做法。
        empty_db = tmp / "empty.db"
        from critval.ledger.store import LedgerStore

        seed = LedgerStore(str(empty_db))
        seed.close()

        server, store = _serve(db_path, snap_dir, args.host, args.port)
        empty_server, empty_store = _serve(empty_db, snap_dir, args.host, args.port + 1)
        base = f"http://{args.host}:{args.port}"
        empty_base = f"http://{args.host}:{args.port + 1}"
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch()
                page = browser.new_page()

                # 1) 加载态：首屏骨架
                _shot(
                    page,
                    f"{base}/events?bootstrap=1",
                    out_dir / "console-states-加载态.png",
                )

                # 2) 空态：整张台账一条都没有
                _shot(
                    page,
                    f"{empty_base}/events",
                    out_dir / "console-states-空态.png",
                )

                # 3) 错误态：事件不存在（带错误码与可执行建议）
                _shot(
                    page,
                    f"{base}/events/evt-不存在的这条",
                    out_dir / "console-states-错误态.png",
                )

                # 4) 写操作反馈：转人工的 toast（琥珀）
                page.set_viewport_size({"width": 1440, "height": 900})
                page.goto(
                    f"{base}/events/{transferred_event}/ack", wait_until="networkidle"
                )
                page.screenshot(path=str(out_dir / "console-states-toast-转人工.png"))
                print(f"[console_ui_shots] 已截图：{out_dir / 'console-states-toast-转人工.png'}")

                # 5) 破坏性操作二次确认：点「转人工处理」弹出站内模态框
                page.goto(f"{base}/events/{plain_event}/ack", wait_until="networkidle")
                page.click("summary.cv-transfer__summary")
                page.click("button:has-text('转人工处理')")
                page.wait_for_selector("[data-cv-modal]:not([hidden])", timeout=5000)
                page.screenshot(
                    path=str(out_dir / "console-states-二次确认模态框.png"),
                    full_page=True,
                )
                print(
                    f"[console_ui_shots] 已截图：{out_dir / 'console-states-二次确认模态框.png'}"
                )

                browser.close()
        finally:
            server.shutdown()
            server.server_close()
            empty_server.shutdown()
            empty_server.server_close()
            store.close()
            empty_store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
