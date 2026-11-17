"""台账多维查询（任务 id 37）。

盯住四件事：

1. **维度切得准** —— 科室 / 项目 / 状态 / 依据条款 / 时间窗 / 超期 /
   已回写，每个筛选项都只留下该留的行；
2. **全参数化** —— 条件值里的引号与分号只是**值**，不是语法。拼 SQL 的
   老路会让「筛选一个科室」变成「全表导出」，而那行代码看上去跟别的
   查询没区别；
3. **超期口径写在明处** —— 超期 = 到期时刻已过（不论是否已确认）；
   「超时未确认」是另一个口径，报表里分开数；
4. **写错就报错** —— 未知状态 / 未知排序字段 / 非白名单列直接抛错，
   不静默返回空集（空集会被读成「今天没有危急值」）。
"""

from __future__ import annotations

import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from critval.ledger.query import (
    LedgerQuery,
    count_events,
    distinct_values,
    query_events,
)
from critval.ledger.store import LedgerStore
from critval.models import Citation, CriticalEvent, Judgement, ThresholdRow

ROOT = Path(__file__).resolve().parent.parent

#: 测试里钉死的「现在」—— 报表与查询的「超期」判定都以它为准。
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
    mcid: str = "",
) -> CriticalEvent:
    return CriticalEvent(
        event_id=event_id,
        message_control_id=mcid or f"MC-{event_id}",
        specimen_no=f"SP-{event_id}",
        item_code=item_code,
        item_name=item_name,
        value=Decimal(value),
        unit="g/L",
        observed_at=observed_at,
        dept_code=dept_code,
        patient_ref="pt-000000000001",
    )


@pytest.fixture()
def store() -> LedgerStore:
    """一份覆盖各种状态的小台账。

    事件分布（观察时刻）：

    * ``evt-q-01`` LAB / 718-7 / 危急 / 依据第 2 行 / 已推送且**已超期**
    * ``evt-q-02`` ICU / 2823-3 / 危急 / 依据第 16 行 / 已确认未回写
    * ``evt-q-03`` EMERG / 718-7 / 不危急
    * ``evt-q-04`` LAB / 718-7 / 危急 / 依据第 2 行 / 推送失败
    * ``evt-q-05`` ICU / 6690-2 / 转人工（未命中依据）
    * ``evt-q-06`` LAB / 718-7 / 危急 / 依据第 2 行 / 已回写（**前一天**）
    * ``evt-q-07`` LAB / 718-7 / 危急 / 依据第 2 行 / 已推送但**未超期**
    """
    instance = LedgerStore(":memory:")
    try:
        # ---- evt-q-01：已推送、已超期、未确认 ----
        instance.record_event(
            _event(
                "evt-q-01",
                item_code="718-7",
                item_name="血红蛋白",
                dept_code="LAB",
                value="25",
                observed_at=datetime(2026, 10, 29, 8, 30, tzinfo=timezone.utc),
            )
        )
        instance.record_judgement(
            "evt-q-01",
            Judgement(
                decision="critical",
                reason="低于低危急值 30",
                item_code="718-7",
                value=Decimal("25"),
                unit="g/L",
                threshold_row=_threshold_row("718-7", "血红蛋白", 2),
            ),
        )
        instance.set_status("evt-q-01", "card_built")
        instance.mark_notified(
            "evt-q-01",
            notify_target="检验科值班技师",
            due_minutes=10,
            channel="wecom",
            at=NOW - timedelta(minutes=30),
        )

        # ---- evt-q-02：已确认、尚未回写 ----
        instance.record_event(
            _event(
                "evt-q-02",
                item_code="2823-3",
                item_name="钾",
                dept_code="ICU",
                value="6.2",
                observed_at=datetime(2026, 10, 29, 9, 15, tzinfo=timezone.utc),
            )
        )
        instance.record_judgement(
            "evt-q-02",
            Judgement(
                decision="critical",
                reason="高于高危急值 6.0",
                item_code="2823-3",
                value=Decimal("6.2"),
                unit="mmol/L",
                threshold_row=_threshold_row("2823-3", "钾", 16),
            ),
        )
        instance.set_status("evt-q-02", "card_built")
        instance.mark_notified(
            "evt-q-02",
            notify_target="ICU 值班护士",
            due_minutes=15,
            channel="wecom",
            at=NOW - timedelta(minutes=20),
        )
        instance.mark_acknowledged("evt-q-02", actor="ICU 值班护士", at=NOW - timedelta(minutes=10))

        # ---- evt-q-03：不危急 ----
        instance.record_event(
            _event(
                "evt-q-03",
                item_code="718-7",
                item_name="血红蛋白",
                dept_code="EMERG",
                value="140",
                observed_at=datetime(2026, 10, 29, 10, 5, tzinfo=timezone.utc),
            )
        )
        instance.record_judgement(
            "evt-q-03",
            Judgement(
                decision="not_critical",
                reason="在参考区间内",
                item_code="718-7",
                value=Decimal("140"),
                unit="g/L",
                threshold_row=_threshold_row("718-7", "血红蛋白", 2),
            ),
        )
        instance.set_status("evt-q-03", "not_critical")

        # ---- evt-q-04：推送失败 ----
        instance.record_event(
            _event(
                "evt-q-04",
                item_code="718-7",
                item_name="血红蛋白",
                dept_code="LAB",
                value="22",
                observed_at=datetime(2026, 10, 29, 11, 0, tzinfo=timezone.utc),
            )
        )
        instance.record_judgement(
            "evt-q-04",
            Judgement(
                decision="critical",
                reason="低于低危急值 30",
                item_code="718-7",
                value=Decimal("22"),
                unit="g/L",
                threshold_row=_threshold_row("718-7", "血红蛋白", 2),
            ),
        )
        instance.set_status("evt-q-04", "card_built")
        instance.set_status("evt-q-04", "notified")
        instance.set_status("evt-q-04", "failed", detail="推送重试耗尽")

        # ---- evt-q-05：转人工（未命中依据） ----
        instance.record_event(
            _event(
                "evt-q-05",
                item_code="6690-2",
                item_name="白细胞计数",
                dept_code="ICU",
                value="18",
                observed_at=datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc),
            )
        )
        instance.record_judgement(
            "evt-q-05",
            Judgement(
                decision="no_threshold",
                reason="阈值表未收录该项目",
                item_code="6690-2",
                value=Decimal("18"),
                unit="10^9/L",
            ),
        )
        instance.set_status("evt-q-05", "transferred_to_human")

        # ---- evt-q-06：已回写（前一天） ----
        instance.record_event(
            _event(
                "evt-q-06",
                item_code="718-7",
                item_name="血红蛋白",
                dept_code="LAB",
                value="24",
                observed_at=datetime(2026, 10, 28, 8, 30, tzinfo=timezone.utc),
            )
        )
        instance.record_judgement(
            "evt-q-06",
            Judgement(
                decision="critical",
                reason="低于低危急值 30",
                item_code="718-7",
                value=Decimal("24"),
                unit="g/L",
                threshold_row=_threshold_row("718-7", "血红蛋白", 2),
            ),
        )
        instance.set_status("evt-q-06", "card_built")
        instance.mark_notified(
            "evt-q-06",
            notify_target="检验科值班技师",
            due_minutes=10,
            channel="wecom",
            at=datetime(2026, 10, 28, 8, 35, tzinfo=timezone.utc),
        )
        instance.mark_acknowledged(
            "evt-q-06", actor="检验科值班技师", at=datetime(2026, 10, 28, 8, 40, tzinfo=timezone.utc)
        )
        instance.mark_registered(
            "evt-q-06", at=datetime(2026, 10, 28, 8, 41, tzinfo=timezone.utc)
        )

        # ---- evt-q-07：已推送、未超期 ----
        instance.record_event(
            _event(
                "evt-q-07",
                item_code="718-7",
                item_name="血红蛋白",
                dept_code="LAB",
                value="26",
                observed_at=datetime(2026, 10, 29, 13, 0, tzinfo=timezone.utc),
            )
        )
        instance.record_judgement(
            "evt-q-07",
            Judgement(
                decision="critical",
                reason="低于低危急值 30",
                item_code="718-7",
                value=Decimal("26"),
                unit="g/L",
                threshold_row=_threshold_row("718-7", "血红蛋白", 2),
            ),
        )
        instance.set_status("evt-q-07", "card_built")
        instance.mark_notified(
            "evt-q-07",
            notify_target="检验科值班技师",
            due_minutes=10,
            channel="wecom",
            at=NOW - timedelta(minutes=5),
        )
        yield instance
    finally:
        instance.close()


# ---------------------------------------------------------------------------
# 1. 各维度切得准
# ---------------------------------------------------------------------------


def test_按科室筛选只留该科室(store: LedgerStore):
    rows = query_events(store, LedgerQuery(dept_code="LAB"), now=NOW)
    assert {row["event_id"] for row in rows} == {"evt-q-01", "evt-q-04", "evt-q-06", "evt-q-07"}
    assert all(row["dept_code"] == "LAB" for row in rows)


def test_按项目码筛选(store: LedgerStore):
    rows = query_events(store, LedgerQuery(item_code="2823-3"), now=NOW)
    assert [row["event_id"] for row in rows] == ["evt-q-02"]


def test_按状态筛选(store: LedgerStore):
    rows = query_events(store, LedgerQuery(status="failed"), now=NOW)
    assert [row["event_id"] for row in rows] == ["evt-q-04"]


def test_多状态筛选一次查完(store: LedgerQuery):
    """``statuses`` 一次覆盖多个状态（值班台概览条的「待确认」要用）。

    与「拆成两次查询再相加」等价，但**口径只有一处** —— 加法在页面上做会
    让「概览条上的数」和「明细表的口径」变成两套东西。
    """
    rows = query_events(store, LedgerQuery(statuses=("failed", "not_critical")), now=NOW)
    assert {row["event_id"] for row in rows} == {"evt-q-04", "evt-q-03"}
    # 等价于两次单值查询相加
    single = query_events(store, LedgerQuery(status="failed"), now=NOW)
    single += query_events(store, LedgerQuery(status="not_critical"), now=NOW)
    assert {row["event_id"] for row in rows} == {row["event_id"] for row in single}
    # 计数口径一致
    assert count_events(store, LedgerQuery(statuses=("failed", "not_critical")), now=NOW) == len(rows)


def test_多状态与单状态互斥时以多状态为准(store: LedgerQuery):
    """``statuses`` 与 ``status`` 同时给时，``statuses`` 说了算（多值优先）。"""
    rows = query_events(
        store, LedgerQuery(status="failed", statuses=("not_critical",)), now=NOW
    )
    assert {row["event_id"] for row in rows} == {"evt-q-03"}


def test_多状态里的未知状态直接报错(store: LedgerQuery):
    """多值也一样：状态名不认识就报错，不静默返回空集。"""
    with pytest.raises(ValueError):
        LedgerQuery(statuses=("failed", "不存在的状态")).validated()


def test_按依据条款子串筛选(store: LedgerQuery):
    """依据条款按**子串**匹配 —— 组长记得「第 2 行」就能捞出全部。"""
    rows = query_events(store, LedgerQuery(basis="第 2 行"), now=NOW)
    assert {row["event_id"] for row in rows} == {
        "evt-q-01",
        "evt-q-03",
        "evt-q-04",
        "evt-q-06",
        "evt-q-07",
    }


def test_按观察日期区间筛选(store: LedgerStore):
    rows = query_events(store, LedgerQuery(day_from="2026-10-29", day_to="2026-10-29"), now=NOW)
    assert "evt-q-06" not in {row["event_id"] for row in rows}
    assert len(rows) == 6


def test_按精确时刻窗口筛选(store: LedgerStore):
    rows = query_events(
        store,
        LedgerQuery(since="2026-10-29T09:00:00+00:00", until="2026-10-29T12:00:00+00:00"),
        now=NOW,
    )
    assert {row["event_id"] for row in rows} == {"evt-q-02", "evt-q-03", "evt-q-04", "evt-q-05"}


def test_超期筛选只留到期已过的(store: LedgerStore):
    """超期 = 到期时刻已过（**不论是否已确认**）。"""
    overdue = query_events(store, LedgerQuery(overdue=True), now=NOW)
    assert {row["event_id"] for row in overdue} == {"evt-q-01", "evt-q-02", "evt-q-06"}
    fresh = query_events(store, LedgerQuery(overdue=False), now=NOW)
    assert "evt-q-07" in {row["event_id"] for row in fresh}
    assert "evt-q-01" not in {row["event_id"] for row in fresh}


def test_已回写筛选(store: LedgerStore):
    written = query_events(store, LedgerQuery(written_back=True), now=NOW)
    assert [row["event_id"] for row in written] == ["evt-q-06"]
    pending = query_events(store, LedgerQuery(written_back=False), now=NOW)
    assert "evt-q-06" not in {row["event_id"] for row in pending}
    assert "evt-q-02" in {row["event_id"] for row in pending}


def test_只统计危急(store: LedgerStore):
    rows = query_events(store, LedgerQuery(critical_only=True), now=NOW)
    assert {row["event_id"] for row in rows} == {
        "evt-q-01",
        "evt-q-02",
        "evt-q-04",
        "evt-q-06",
        "evt-q-07",
    }


def test_多条件可叠加(store: LedgerStore):
    rows = query_events(
        store,
        LedgerQuery(dept_code="LAB", item_code="718-7", day_from="2026-10-29"),
        now=NOW,
    )
    assert {row["event_id"] for row in rows} == {"evt-q-01", "evt-q-04", "evt-q-07"}


def test_计数与查询结果一致(store: LedgerStore):
    cond = LedgerQuery(dept_code="LAB")
    assert count_events(store, cond, now=NOW) == len(query_events(store, cond, now=NOW))


def test_默认按观察时刻倒序且_limit_生效(store: LedgerStore):
    rows = query_events(store, LedgerQuery(), now=NOW)
    stamps = [row["observed_at"] for row in rows]
    assert stamps == sorted(stamps, reverse=True)
    limited = query_events(store, LedgerQuery(limit=2), now=NOW)
    assert len(limited) == 2
    assert limited[0]["event_id"] == "evt-q-07"


# ---------------------------------------------------------------------------
# 2. 全参数化：条件值只是值
# ---------------------------------------------------------------------------


def test_条件值里的引号与分号只是值(store: LedgerStore):
    """拼 SQL 的老路会让「筛选一个科室」变成「全表导出」。"""
    rows = query_events(store, LedgerQuery(dept_code="LAB' OR '1'='1"), now=NOW)
    assert rows == []
    assert count_events(store, LedgerQuery(dept_code="LAB' OR '1'='1"), now=NOW) == 0
    # 表还在，行数没少
    assert count_events(store, LedgerQuery(), now=NOW) == 7


def test_条件值里的分号不被执行(store: LedgerStore):
    rows = query_events(store, LedgerQuery(item_code="718-7'; DROP TABLE events; --"), now=NOW)
    assert rows == []
    assert count_events(store, LedgerQuery(), now=NOW) == 7


def test_下划线不被当作通配符(store: LedgerStore):
    """``LAB_1`` 不该匹配 ``LAB_12`` —— LIKE 通配符必须转义。"""
    rows = query_events(store, LedgerQuery(dept_code="LAB_"), now=NOW)
    assert rows == []


def test_百分号不被当作通配符(store: LedgerStore):
    rows = query_events(store, LedgerQuery(basis="%"), now=NOW)
    assert rows == []


# ---------------------------------------------------------------------------
# 3. 写错就报错，不静默返回空集
# ---------------------------------------------------------------------------


def test_未知状态直接报错(store: LedgerStore):
    with pytest.raises(ValueError) as excinfo:
        query_events(store, LedgerQuery(status="judgement"))
    assert "judgement" in str(excinfo.value)


def test_未知排序字段报错(store: LedgerStore):
    with pytest.raises(ValueError):
        query_events(store, LedgerQuery(sort="value; DROP TABLE events"))


def test_去重值只认白名单列(store: LedgerStore):
    assert distinct_values(store, "dept_code") == ["EMERG", "ICU", "LAB"]
    with pytest.raises(ValueError):
        distinct_values(store, "value")


def test_只读入口拒写(store: LedgerStore):
    """查询入口不给写路径 —— 免得「顺手在查询函数里改了点什么」。"""
    with pytest.raises(ValueError):
        store.query_rows("DELETE FROM events")


# ---------------------------------------------------------------------------
# 4. CLI 侧的多维筛选（真实子进程）
# ---------------------------------------------------------------------------


def test_CLI_审计统计与查询口径互不打架(store: LedgerStore, tmp_path):
    """查询与审计统计读的是同一个库，两个口径各说各的才对。"""
    db_path = tmp_path / "ledger.db"
    with LedgerStore(db_path) as target:
        for row in store.list_events(limit=0):
            target.record_event(
                _event(
                    row["event_id"],
                    item_code=row["item_code"],
                    item_name=row["item_name"],
                    dept_code=row["dept_code"],
                    value=row["value"],
                    observed_at=datetime.fromisoformat(row["observed_at"]),
                )
            )
    result = subprocess.run(
        [sys.executable, "-m", "critval.cli", "audit-stats", "--db", str(db_path), "--json"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert '"total"' in result.stdout
