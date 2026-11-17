"""复核报表：日报 / 周报与导出（任务 id 37）。

盯住五件事：

1. **报表按观察日期切** —— 复核的是「昨天发生的事」，不是「导出那一刻
   之前 24 小时」；
2. **三个视角切得对** —— 按科室 / 按项目 / 按依据条款，三刀切同一批数据，
   每刀的合计都等于总数（漏一条就是口径出了岔子）；
3. **三类异常计数准** —— 超时未确认 / 待回写 / 失败；
4. **超期与超时未确认是两个口径** —— 超期 = 到期时刻已过（不论是否已确认），
   超时未确认 = 超期 **且** 仍未确认；混了这两个数，报表会把「已确认但
   超时」的老账算成「现在还没人管」；
5. **导出的表能直接打开** —— CSV 带 UTF-8 BOM（Excel 直开），xlsx 有表头
   样式与「列说明」页。
"""

from __future__ import annotations

import csv
import subprocess
import sys
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from critval.ledger.export import (
    export_detail_csv,
    export_report_xlsx,
    export_summary_csv,
)
from critval.ledger.report import (
    DETAIL_COLUMNS,
    SUMMARY_COLUMNS,
    build_daily_report,
    build_report,
    build_weekly_report,
    detail_rows,
    summary_rows,
)
from critval.ledger.store import LedgerStore
from critval.models import Citation, CriticalEvent, Judgement, ThresholdRow

ROOT = Path(__file__).resolve().parent.parent

#: 钉死的「现在」—— 超期判定以它为准，报表因此可复算。
NOW = datetime(2026, 10, 30, 9, 0, tzinfo=timezone.utc)


def _threshold_row(item_code: str, item_name: str, line_no: int) -> ThresholdRow:
    return ThresholdRow(
        item_code=item_code,
        item_name=item_name,
        unit="g/L",
        low_critical=Decimal("30"),
        high_critical=Decimal("200"),
        reference_range_raw="115-150",
        effective_from=date(2026, 1, 1),
        version="示例 v1.0",
        citation=Citation(
            source_name="示例危急值阈值表.xlsx",
            source_sha256="sha-threshold-table",
            kind="threshold_row",
            line_no=line_no,
            file_version="示例 v1.0",
        ),
        row_no=line_no,
    )


def _event(
    event_id: str,
    *,
    item_code: str,
    item_name: str,
    dept_code: str,
    value: str,
    observed_at: datetime,
) -> CriticalEvent:
    return CriticalEvent(
        event_id=event_id,
        message_control_id=f"MC-{event_id}",
        specimen_no=f"SP-{event_id}",
        item_code=item_code,
        item_name=item_name,
        value=Decimal(value),
        unit="g/L",
        observed_at=observed_at,
        dept_code=dept_code,
        patient_ref="pt-000000000001",
    )


def seed(store: LedgerStore) -> LedgerStore:
    """一份覆盖各种状态的小台账（与查询测试同一套场景）。"""
    # evt-r-01：已推送、已超期、未确认（LAB / 依据第 2 行）
    store.record_event(
        _event(
            "evt-r-01",
            item_code="718-7",
            item_name="血红蛋白",
            dept_code="LAB",
            value="25",
            observed_at=datetime(2026, 10, 29, 8, 30, tzinfo=timezone.utc),
        )
    )
    store.record_judgement(
        "evt-r-01",
        Judgement(
            decision="critical",
            reason="低于低危急值 30",
            item_code="718-7",
            value=Decimal("25"),
            unit="g/L",
            threshold_row=_threshold_row("718-7", "血红蛋白", 2),
        ),
    )
    store.set_status("evt-r-01", "card_built")
    store.mark_notified(
        "evt-r-01",
        notify_target="检验科值班技师",
        due_minutes=10,
        channel="wecom",
        at=datetime(2026, 10, 29, 8, 35, tzinfo=timezone.utc),
    )

    # evt-r-02：已确认未回写（ICU / 依据第 16 行）
    store.record_event(
        _event(
            "evt-r-02",
            item_code="2823-3",
            item_name="钾",
            dept_code="ICU",
            value="6.2",
            observed_at=datetime(2026, 10, 29, 9, 15, tzinfo=timezone.utc),
        )
    )
    store.record_judgement(
        "evt-r-02",
        Judgement(
            decision="critical",
            reason="高于高危急值 6.0",
            item_code="2823-3",
            value=Decimal("6.2"),
            unit="mmol/L",
            threshold_row=_threshold_row("2823-3", "钾", 16),
        ),
    )
    store.set_status("evt-r-02", "card_built")
    store.mark_notified(
        "evt-r-02",
        notify_target="ICU 值班护士",
        due_minutes=15,
        channel="wecom",
        at=datetime(2026, 10, 29, 9, 20, tzinfo=timezone.utc),
    )
    store.mark_acknowledged(
        "evt-r-02", actor="ICU 值班护士", at=datetime(2026, 10, 29, 9, 30, tzinfo=timezone.utc)
    )

    # evt-r-03：不危急（EMERG）
    store.record_event(
        _event(
            "evt-r-03",
            item_code="718-7",
            item_name="血红蛋白",
            dept_code="EMERG",
            value="140",
            observed_at=datetime(2026, 10, 29, 10, 5, tzinfo=timezone.utc),
        )
    )
    store.record_judgement(
        "evt-r-03",
        Judgement(
            decision="not_critical",
            reason="在参考区间内",
            item_code="718-7",
            value=Decimal("140"),
            unit="g/L",
            threshold_row=_threshold_row("718-7", "血红蛋白", 2),
        ),
    )
    store.set_status("evt-r-03", "not_critical")

    # evt-r-04：推送失败（LAB）
    store.record_event(
        _event(
            "evt-r-04",
            item_code="718-7",
            item_name="血红蛋白",
            dept_code="LAB",
            value="22",
            observed_at=datetime(2026, 10, 29, 11, 0, tzinfo=timezone.utc),
        )
    )
    store.record_judgement(
        "evt-r-04",
        Judgement(
            decision="critical",
            reason="低于低危急值 30",
            item_code="718-7",
            value=Decimal("22"),
            unit="g/L",
            threshold_row=_threshold_row("718-7", "血红蛋白", 2),
        ),
    )
    store.set_status("evt-r-04", "card_built")
    store.set_status("evt-r-04", "notified")
    store.set_status("evt-r-04", "failed", detail="推送重试耗尽")

    # evt-r-05：转人工、未命中依据（ICU）
    store.record_event(
        _event(
            "evt-r-05",
            item_code="6690-2",
            item_name="白细胞计数",
            dept_code="ICU",
            value="18",
            observed_at=datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc),
        )
    )
    store.record_judgement(
        "evt-r-05",
        Judgement(
            decision="no_threshold",
            reason="阈值表未收录该项目",
            item_code="6690-2",
            value=Decimal("18"),
            unit="10^9/L",
        ),
    )
    store.set_status("evt-r-05", "transferred_to_human")

    # evt-r-06：已回写（LAB / 前一天）
    store.record_event(
        _event(
            "evt-r-06",
            item_code="718-7",
            item_name="血红蛋白",
            dept_code="LAB",
            value="24",
            observed_at=datetime(2026, 10, 28, 8, 30, tzinfo=timezone.utc),
        )
    )
    store.record_judgement(
        "evt-r-06",
        Judgement(
            decision="critical",
            reason="低于低危急值 30",
            item_code="718-7",
            value=Decimal("24"),
            unit="g/L",
            threshold_row=_threshold_row("718-7", "血红蛋白", 2),
        ),
    )
    store.set_status("evt-r-06", "card_built")
    store.mark_notified(
        "evt-r-06",
        notify_target="检验科值班技师",
        due_minutes=10,
        channel="wecom",
        at=datetime(2026, 10, 28, 8, 35, tzinfo=timezone.utc),
    )
    store.mark_acknowledged(
        "evt-r-06", actor="检验科值班技师", at=datetime(2026, 10, 28, 8, 40, tzinfo=timezone.utc)
    )
    store.mark_registered("evt-r-06", at=datetime(2026, 10, 28, 8, 41, tzinfo=timezone.utc))

    # evt-r-07：已推送但**没有到期时刻**（LAB / 依据第 2 行）—— 制度未写明
    # 上报时限时**不猜**一个到期时刻，也就没有「超期」可言
    store.record_event(
        _event(
            "evt-r-07",
            item_code="718-7",
            item_name="血红蛋白",
            dept_code="LAB",
            value="26",
            observed_at=datetime(2026, 10, 29, 13, 0, tzinfo=timezone.utc),
        )
    )
    store.record_judgement(
        "evt-r-07",
        Judgement(
            decision="critical",
            reason="低于低危急值 30",
            item_code="718-7",
            value=Decimal("26"),
            unit="g/L",
            threshold_row=_threshold_row("718-7", "血红蛋白", 2),
        ),
    )
    store.set_status("evt-r-07", "card_built")
    store.mark_notified(
        "evt-r-07",
        notify_target="检验科值班技师",
        due_minutes=None,
        channel="wecom",
        at=datetime(2026, 10, 29, 13, 5, tzinfo=timezone.utc),
    )
    return store


@pytest.fixture()
def store() -> LedgerStore:
    instance = LedgerStore(":memory:")
    try:
        yield seed(instance)
    finally:
        instance.close()


@pytest.fixture()
def report(store: LedgerStore):
    return build_daily_report(store, "2026-10-29", now=NOW)


# ---------------------------------------------------------------------------
# 1. 报表按观察日期切
# ---------------------------------------------------------------------------


def test_日报只算当天的观察事件(store: LedgerStore):
    daily = build_daily_report(store, "2026-10-29", now=NOW)
    assert daily.total_events == 6
    assert daily.period_start == "2026-10-29"
    assert daily.period_end == "2026-10-29"
    # 前一天那条已回写的不进当天日报
    assert "evt-r-06" not in {row["event_id"] for row in daily.events}


def test_日报总量与判定计数(store: LedgerStore):
    daily = build_daily_report(store, "2026-10-29", now=NOW)
    assert daily.total_events == 6
    assert daily.critical == 4
    assert daily.not_critical == 1
    assert daily.transferred_to_human == 1


def test_周报覆盖七天(store: LedgerStore):
    weekly = build_weekly_report(store, "2026-10-29", now=NOW)
    assert weekly.period_start == "2026-10-23"
    assert weekly.period_end == "2026-10-29"
    assert weekly.total_events == 7
    assert "evt-r-06" in {row["event_id"] for row in weekly.events}


def test_未知口径直接报错(store: LedgerStore):
    with pytest.raises(ValueError):
        build_report(store, "monthly", "2026-10-29", now=NOW)


# ---------------------------------------------------------------------------
# 2. 三个视角
# ---------------------------------------------------------------------------


def test_按科室视角(report):
    buckets = {row.key: row for row in report.by_dept}
    assert set(buckets) == {"LAB", "ICU", "EMERG"}
    assert buckets["LAB"].total == 3
    assert buckets["ICU"].total == 2
    assert buckets["EMERG"].total == 1
    assert buckets["LAB"].critical == 3  # evt-r-01 / evt-r-04 / evt-r-07


def test_按项目视角(report):
    buckets = {row.key: row for row in report.by_item}
    assert buckets["718-7"].total == 4
    assert buckets["2823-3"].total == 1
    assert buckets["6690-2"].total == 1
    assert buckets["718-7"].label == "718-7 血红蛋白"


def test_按依据条款视角(report):
    buckets = {row.key: row for row in report.by_basis}
    assert buckets["示例危急值阈值表.xlsx · 第 2 行 · 示例 v1.0"].total == 4
    assert buckets["示例危急值阈值表.xlsx · 第 16 行 · 示例 v1.0"].total == 1
    assert buckets["（未命中依据）"].total == 1


def test_三个视角的合计都等于总数(report):
    for rows in (report.by_dept, report.by_item, report.by_basis):
        assert sum(row.total for row in rows) == report.total_events


def test_视角内分组按总数倒序且同分按键升序(report):
    totals = [row.total for row in report.by_dept]
    assert totals == sorted(totals, reverse=True)
    same = [row.key for row in report.by_item if row.total == 1]
    assert same == sorted(same)


# ---------------------------------------------------------------------------
# 3. 三类异常计数
# ---------------------------------------------------------------------------


def test_超时未确认只算没确认的(report):
    """evt-r-02 也超期，但它已确认 —— 不算「没人管」。"""
    assert report.anomalies.overdue_unconfirmed == 1
    assert report.by_dept[0].overdue_unconfirmed <= 1


def test_待回写只算已确认未回写的(report):
    assert report.anomalies.pending_writeback == 1


def test_失败计数区分推送失败与回写失败(report):
    assert report.anomalies.notify_failed == 1
    assert report.anomalies.writeback_failed == 0
    assert report.anomalies.failed == 1


def test_异常计数与视角内计数一致(report):
    """口径只有一处 —— 汇总表与总计数不会打架。"""
    for rows in (report.by_dept, report.by_item, report.by_basis):
        assert sum(row.overdue_unconfirmed for row in rows) == report.anomalies.overdue_unconfirmed
        assert sum(row.pending_writeback for row in rows) == report.anomalies.pending_writeback
        assert sum(row.failed for row in rows) == report.anomalies.failed


def test_人读摘要含三个视角与异常计数(report):
    text = report.to_text()
    assert "按科室" in text
    assert "按项目" in text
    assert "按依据条款" in text
    assert "超时未确认" in text
    assert "待回写" in text


# ---------------------------------------------------------------------------
# 4. 导出
# ---------------------------------------------------------------------------


def test_明细列就是白名单里的列(report):
    rows = detail_rows(report)
    assert len(rows) == report.total_events
    assert [column.key for column in DETAIL_COLUMNS][0] == "observed_at"
    assert "overdue" in {column.key for column in DETAIL_COLUMNS}


def test_导出的CSV带BOM且列与行数正确(report, tmp_path):
    target = export_detail_csv(report, tmp_path / "明细.csv")
    raw = target.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf"), "Excel 直开需要 UTF-8 BOM"
    with target.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.reader(handle))
    assert rows[0] == [column.header for column in DETAIL_COLUMNS]
    assert len(rows) - 1 == report.total_events
    # 第一行明细就是当天第一条
    assert rows[1][0].startswith("2026-10-29")


def test_导出的汇总CSV是三个视角铺平的长表(report, tmp_path):
    target = export_summary_csv(report, tmp_path / "汇总.csv")
    raw = target.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    with target.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.reader(handle))
    assert rows[0] == [column.header for column in SUMMARY_COLUMNS]
    perspectives = {row[0] for row in rows[1:]}
    assert perspectives == {"按科室", "按项目", "按依据条款"}


def test_导出的xlsx有三个页签且含列说明(report, tmp_path):
    import openpyxl

    target = export_report_xlsx(report, tmp_path / "复核报表.xlsx")
    assert target.exists()
    workbook = openpyxl.load_workbook(target)
    assert workbook.sheetnames == ["复核汇总", "事件明细", "列说明"]

    detail_sheet = workbook["事件明细"]
    header = [cell.value for cell in detail_sheet[1]]
    assert header == [column.header for column in DETAIL_COLUMNS]
    assert detail_sheet.max_row == report.total_events + 1
    # 表头有样式（加粗 + 填充）
    assert detail_sheet["A1"].font.bold is True
    assert detail_sheet["A1"].fill.fgColor.rgb not in (None, "00000000")

    doc_sheet = workbook["列说明"]
    notes = [row[3] for row in doc_sheet.iter_rows(min_row=2, values_only=True) if row[3]]
    assert any("上报时限" in note for note in notes)
    assert len(notes) == len(DETAIL_COLUMNS) + len(SUMMARY_COLUMNS)


def test_导出的明细不含患者可识别字段(report, tmp_path):
    """台账不落患者明细，导出更不能落。"""
    target = export_detail_csv(report, tmp_path / "明细.csv")
    text = target.read_text(encoding="utf-8-sig")
    assert "pt-000000000001" not in text
    assert "住院号" not in text
    assert "身份证" not in text


def test_汇总行数与三个视角一致(report):
    rows = summary_rows(report)
    assert len(rows) == len(report.by_dept) + len(report.by_item) + len(report.by_basis)


# ---------------------------------------------------------------------------
# 5. CLI 真跑一条导出命令
# ---------------------------------------------------------------------------


def test_CLI导出日报xlsx产出可打开的表(tmp_path):
    import openpyxl

    db_path = tmp_path / "ledger.db"
    with LedgerStore(db_path) as target:
        seed(target)
    out_path = tmp_path / "日报.xlsx"

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "critval.cli",
            "export",
            "--scope",
            "daily",
            "--date",
            "2026-10-29",
            "--format",
            "xlsx",
            "--db",
            str(db_path),
            "--out",
            str(out_path),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert out_path.exists()
    workbook = openpyxl.load_workbook(out_path)
    assert workbook.sheetnames == ["复核汇总", "事件明细", "列说明"]
    assert workbook["事件明细"].max_row == 7  # 6 条明细 + 表头


def test_CLI导出CSV给两份文件(tmp_path):
    db_path = tmp_path / "ledger.db"
    with LedgerStore(db_path) as target:
        seed(target)
    out_path = tmp_path / "日报明细.csv"

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "critval.cli",
            "export",
            "--scope",
            "daily",
            "--date",
            "2026-10-29",
            "--format",
            "csv",
            "--db",
            str(db_path),
            "--out",
            str(out_path),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert out_path.exists()
    summary_path = out_path.with_name("日报_汇总.csv")
    assert summary_path.exists()
    assert out_path.read_bytes().startswith(b"\xef\xbb\xbf")


def test_CLI导出_JSON可被解析(tmp_path):
    db_path = tmp_path / "ledger.db"
    with LedgerStore(db_path) as target:
        seed(target)

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "critval.cli",
            "export",
            "--scope",
            "weekly",
            "--date",
            "2026-10-29",
            "--format",
            "csv",
            "--db",
            str(db_path),
            "--json",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    import json

    payload = json.loads(result.stdout)
    assert payload["scope"] == "weekly"
    assert payload["total_events"] == 7
    assert payload["anomalies"]["overdue_unconfirmed"] == 1
    assert len(payload["written"]) == 2


def test_CLI既不给事件也不给口径时报参数错(tmp_path):
    result = subprocess.run(
        [sys.executable, "-m", "critval.cli", "export"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "--scope" in result.stderr
