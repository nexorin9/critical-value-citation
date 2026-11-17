"""本地台账 ↔ 登记系统回执对账（任务 id 38）。

**问题**：本地台账说「已确认 / 已回写」，登记系统未必已受理。两边说法
不一致却没人察觉，纸质登记本答不了这个问题 —— 它只记「谁确认过」，不记
「对端收没收到」。

**本模块做什么**：按幂等键 ``message_control_id`` 比对两边，产出**差异
清单**（每行带可执行的建议动作），并提供**显式处理**入口。

**差异分类（四类）**：

=====================  ====================================================
差异类型               含义
=====================  ====================================================
``missing_local``      登记系统有回执，本地台账查无此消息控制号 —— 本地漏记
``missing_remote``     本地已确认 / 已回写，登记系统查无回执 —— 远端没收到
``status_mismatch``    两边状态对不上（远端已受理本地还停在「已确认」，
                       或本地说已回写而远端说拒收）
``field_mismatch``     两边字段不一致（项目码 / 结果值 / 依据行 / 科室 /
                       通知对象 / 上报时限）
=====================  ====================================================

**建议动作（三个）**：

* **补写** —— 把缺的一边补齐（补到登记系统，或补登记本地台账状态）；
* **重放** —— 以本地台账为准，用同一幂等键重放一次回执（登记系统按幂等键
  覆盖，不会生成第二条）；
* **标记人工处理** —— 两边说法冲突且不能自动判定，转人裁决。

**差异不自动抹平**：``reconcile()`` 是**只读**的 —— 它只读两边、只出清单，
不碰任何本地状态，也不发任何写请求。要动状态必须显式 ``resolve()``
（CLI 上是 ``--resolve``），并且每一次处理都留审计。
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from critval.writeback.client import (
    ReceiptRecord,
    RegistrationSystemClient,
    build_receipt_payload,
    merge_receipts,
)

__all__ = [
    "ACTION_MANUAL",
    "ACTION_REPLAY",
    "ACTION_WRITEBACK",
    "DIFF_FIELD_MISMATCH",
    "DIFF_LABELS",
    "DIFF_MISSING_LOCAL",
    "DIFF_MISSING_REMOTE",
    "DIFF_STATUS_MISMATCH",
    "ReconcileDiff",
    "ReconcileError",
    "ReconcileReport",
    "Reconciler",
    "ResolveOutcome",
    "TARGET_LOCAL",
    "TARGET_REGISTRY",
    "WRITE_SCOPE_STATUSES",
]


class ReconcileError(RuntimeError):
    """对账做不了 —— **不是「两边一致」，是「没问到」**。

    登记系统整体不可达 / 鉴权被拒时，**不能**出一份差异清单：查不到回执
    与「对端没有这条回执」是两件事。把前者写成后者，就会凭空造出一堆
    「待补写」，值班人照着补写一遍才发现对面根本连不上。

    所以这里**报错退出**，让人先处理通道问题。
    """



# ---------------------------------------------------------------------------
# 差异类型与动作
# ---------------------------------------------------------------------------
DIFF_MISSING_LOCAL = "missing_local"
DIFF_MISSING_REMOTE = "missing_remote"
DIFF_STATUS_MISMATCH = "status_mismatch"
DIFF_FIELD_MISMATCH = "field_mismatch"

#: 差异类型 → 人话（清单与摘要里给人看）。
DIFF_LABELS: dict[str, str] = {
    DIFF_MISSING_LOCAL: "登记系统有回执，本地台账没有",
    DIFF_MISSING_REMOTE: "本地已确认，登记系统查无回执",
    DIFF_STATUS_MISMATCH: "两边状态对不上",
    DIFF_FIELD_MISMATCH: "两边字段不一致",
}

#: 三个可执行动作。
ACTION_WRITEBACK = "补写"
ACTION_REPLAY = "重放"
ACTION_MANUAL = "标记人工处理"

#: 动作作用在哪一侧。
TARGET_REGISTRY = "登记系统"
TARGET_LOCAL = "本地台账"

#: 本地处于这些状态的事件**应该**在登记系统里有一条回执（对账范围）。
WRITE_SCOPE_STATUSES: tuple[str, ...] = (
    "acknowledged",
    "writeback_failed",
    "registered",
    "archived",
)

#: 远端回执状态：已受理 / 被拒收。
_REMOTE_ACCEPTED = frozenset(
    {"accepted", "received", "registered", "ok", "success", "已受理", "已回写"}
)
_REMOTE_REJECTED = frozenset(
    {"rejected", "voided", "cancelled", "failed", "error", "拒收", "作废", "已作废"}
)

#: 比对哪些字段（本地列名 → 远端字段名）。
_COMPARED_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("item_code", "item_code", "项目码"),
    ("value", "value", "结果值"),
    ("threshold_row", "threshold_row", "阈值依据行"),
    ("dept_code", "notified_dept", "已通知科室"),
    ("notify_target", "duty_person", "通知对象"),
    ("due_minutes", "due_minutes", "上报时限(分钟)"),
)


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ReconcileDiff:
    """一行差异。"""

    message_control_id: str
    diff_kind: str
    action: str
    target: str
    suggestion: str
    event_id: str = ""
    item_code: str = ""
    dept_code: str = ""
    local_status: str = ""
    remote_status: str = ""
    mismatched_fields: tuple[str, ...] = ()
    detail: str = ""

    @property
    def kind_label(self) -> str:
        return DIFF_LABELS.get(self.diff_kind, self.diff_kind)

    def to_dict(self) -> dict[str, Any]:
        return {
            "message_control_id": self.message_control_id,
            "event_id": self.event_id,
            "item_code": self.item_code,
            "dept_code": self.dept_code,
            "diff_kind": self.diff_kind,
            "diff_label": self.kind_label,
            "local_status": self.local_status,
            "remote_status": self.remote_status,
            "mismatched_fields": list(self.mismatched_fields),
            "action": self.action,
            "target": self.target,
            "suggestion": self.suggestion,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class ResolveOutcome:
    """一次显式处理的结果。"""

    message_control_id: str
    event_id: str
    action: str
    target: str
    ok: bool
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "message_control_id": self.message_control_id,
            "event_id": self.event_id,
            "action": self.action,
            "target": self.target,
            "ok": self.ok,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class ReconcileReport:
    """一次对账的结论。"""

    rows: tuple[ReconcileDiff, ...] = ()
    local_scanned: int = 0
    remote_count: int = 0
    listing_supported: bool = True
    notes: tuple[str, ...] = ()
    generated_at: str = ""

    def counts(self) -> dict[str, int]:
        """四类差异各几条。"""
        out = {
            DIFF_MISSING_LOCAL: 0,
            DIFF_MISSING_REMOTE: 0,
            DIFF_STATUS_MISMATCH: 0,
            DIFF_FIELD_MISMATCH: 0,
        }
        for row in self.rows:
            out[row.diff_kind] = out.get(row.diff_kind, 0) + 1
        return out

    def actions(self) -> dict[str, int]:
        """三个动作各建议了几次。"""
        out = {ACTION_WRITEBACK: 0, ACTION_REPLAY: 0, ACTION_MANUAL: 0}
        for row in self.rows:
            out[row.action] = out.get(row.action, 0) + 1
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "local_scanned": self.local_scanned,
            "remote_count": self.remote_count,
            "listing_supported": self.listing_supported,
            "counts": self.counts(),
            "actions": self.actions(),
            "notes": list(self.notes),
            "rows": [row.to_dict() for row in self.rows],
        }

    def to_text(self) -> str:
        """人读摘要。"""
        counts = self.counts()
        lines = [
            f"对账摘要：本地待对账 {self.local_scanned} 条 · "
            f"登记系统回执 {self.remote_count} 条 · 差异 {len(self.rows)} 条"
        ]
        for kind in (DIFF_MISSING_LOCAL, DIFF_MISSING_REMOTE, DIFF_STATUS_MISMATCH, DIFF_FIELD_MISMATCH):
            lines.append(f" · {kind} {counts.get(kind, 0)} 条 —— {DIFF_LABELS[kind]}")
        actions = self.actions()
        lines.append(
            "建议动作："
            + " · ".join(f"{name} {actions.get(name, 0)} 次" for name in (ACTION_WRITEBACK, ACTION_REPLAY, ACTION_MANUAL))
        )
        for note in self.notes:
            lines.append(f"！{note}")
        if self.rows:
            lines.append("")
            lines.append("差异清单：")
            for row in self.rows:
                fields = ("（不一致字段：" + "、".join(row.mismatched_fields) + "）") if row.mismatched_fields else ""
                lines.append(
                    f" · {row.message_control_id}  {row.diff_kind}  "
                    f"{row.action} → {row.target}{fields}"
                )
                lines.append(f"     {row.suggestion}")
        return "\n".join(lines)

    def write_csv(self, path: str | Path) -> Path:
        """差异清单落 CSV（UTF-8 BOM，Excel 直开）。

        **清单里不放生成时刻** —— 同一份台账与同一个登记系统状态，重新对账
        必须得到**逐字节相同**的清单，才能拿去比对两次结果有没有变化。
        """
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        header = [
            "消息控制号",
            "事件号",
            "项目码",
            "科室",
            "差异类型",
            "本地状态",
            "登记系统回执状态",
            "不一致字段",
            "建议动作",
            "处理对象",
            "处理建议",
            "明细",
        ]
        with target.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(header)
            for row in self.rows:
                writer.writerow(
                    [
                        row.message_control_id,
                        row.event_id,
                        row.item_code,
                        row.dept_code,
                        row.diff_kind,
                        row.local_status,
                        row.remote_status,
                        "、".join(row.mismatched_fields),
                        row.action,
                        row.target,
                        row.suggestion,
                        row.detail,
                    ]
                )
        return target


# ---------------------------------------------------------------------------
# 对账
# ---------------------------------------------------------------------------
def _text(value: Any) -> str:
    return str(value or "").strip()


def _same_value(local: Any, remote: Any) -> bool:
    """两个结果值是否一致（数值按十进制比，文本按去空白后的字面比）。"""
    left = _text(local)
    right = _text(remote)
    if left == right:
        return True
    if not left or not right:
        return False
    try:
        return Decimal(left) == Decimal(right)
    except (InvalidOperation, ValueError):
        return False


class Reconciler:
    """对账器：读两边 → 出差异清单 → （显式）处理差异。

    * ``store`` —— 本地台账（``critval.ledger.store.LedgerStore``）；
    * ``client`` —— 登记系统出站客户端（``critval.writeback.client``）。
    """

    def __init__(
        self,
        store: Any,
        client: RegistrationSystemClient,
        *,
        clock: Optional[Any] = None,
    ) -> None:
        self.store = store
        self.client = client
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    # ---- 只读：对账 -------------------------------------------------------
    def reconcile(
        self,
        *,
        event_ids: Sequence[str] = (),
        since: str = "",
        limit: int = 0,
    ) -> ReconcileReport:
        """对账并产出差异清单。

        **本方法不改任何本地状态，也不发任何写请求** —— 它只读两边。
        """
        local_rows = self._load_local(event_ids=event_ids, since=since, limit=limit)
        notes: list[str] = []
        remote_index, remote_count, listing_supported, unknown_keys = self._load_remote(
            local_rows, notes
        )

        local_by_mcid: dict[str, dict[str, Any]] = {}
        for row in local_rows:
            local_by_mcid.setdefault(_text(row.get("message_control_id")), row)

        diffs: list[ReconcileDiff] = []
        for row in local_rows:
            mcid = _text(row.get("message_control_id"))
            status = _text(row.get("status"))
            if status not in WRITE_SCOPE_STATUSES:
                # 不在回写范围内（还没确认 / 不危急 / 转人工…），但远端若有
                # 回执 —— 那说明**回执先于人工确认出现**，是硬异常。
                receipt = remote_index.get(mcid)
                if receipt is not None:
                    diffs.append(
                        ReconcileDiff(
                            message_control_id=mcid,
                            event_id=_text(row.get("event_id")),
                            item_code=_text(row.get("item_code")),
                            dept_code=_text(row.get("dept_code")),
                            diff_kind=DIFF_STATUS_MISMATCH,
                            action=ACTION_MANUAL,
                            target=TARGET_LOCAL,
                            suggestion=(
                                "登记系统已有回执，本地这条却还没走到「已确认」"
                                "（回执不该先于人工确认出现）→ 转人工核查"
                            ),
                            local_status=status,
                            remote_status=receipt.receipt_status,
                            detail=(
                                f"本地状态 {status or '空'}；登记系统回执状态 "
                                f"{receipt.receipt_status or '空'}"
                            ),
                        )
                    )
                continue
            receipt = remote_index.get(mcid)
            if mcid in unknown_keys:
                # 没问到 ≠ 没有。这条**不判差异**（远端状态未判定），
                # 摘要里已经写明有几条没问到。
                continue
            diff = self._diff_for(row, receipt)
            if diff is not None:
                diffs.append(diff)

        if listing_supported:
            for mcid, receipt in sorted(remote_index.items()):
                if mcid in local_by_mcid:
                    continue
                diffs.append(
                    ReconcileDiff(
                        message_control_id=mcid,
                        event_id="",
                        item_code=receipt.item_code,
                        dept_code=receipt.notified_dept,
                        diff_kind=DIFF_MISSING_LOCAL,
                        action=ACTION_MANUAL,
                        target=TARGET_LOCAL,
                        suggestion=(
                            "登记系统有这条回执，本地台账查无此消息控制号"
                            "（本地漏记）→ 人工补录或排查漏采集"
                        ),
                        local_status="",
                        remote_status=receipt.receipt_status,
                        detail=(
                            f"登记系统回执时刻 {receipt.receipt_at or '空'}；"
                            f"依据行 {receipt.threshold_row or '空'}"
                        ),
                    )
                )
        else:
            notes.append(
                "登记系统不支持列表查询（无法列全部回执），"
                "「登记系统有、本地没有」这一类差异本轮未检出"
            )

        diffs.sort(key=lambda d: (d.message_control_id, d.diff_kind, d.event_id))
        return ReconcileReport(
            rows=tuple(diffs),
            local_scanned=len(local_rows),
            remote_count=remote_count,
            listing_supported=listing_supported,
            notes=tuple(notes),
            generated_at=self._clock().isoformat(timespec="seconds"),
        )

    # ---- 显式处理 ---------------------------------------------------------
    def resolve(
        self,
        report: ReconcileReport,
        *,
        actor: str,
        kinds: Sequence[str] = (),
        event_ids: Sequence[str] = (),
    ) -> list[ResolveOutcome]:
        """按建议动作**显式**处理差异。

        没有 ``--resolve`` 就不会走到这里 —— 差异清单本身是只读的。

        每一次处理都留审计（``reconcile_*``），处理完本地状态与登记系统
        都看得见。
        """
        who = _text(actor) or "未具名处理人"
        outcomes: list[ResolveOutcome] = []
        for row in report.rows:
            if kinds and row.diff_kind not in kinds:
                continue
            if event_ids and row.event_id not in event_ids:
                continue
            outcomes.append(self._resolve_row(row, actor=who))
        return outcomes

    # ---- 内部 -------------------------------------------------------------
    def _load_local(
        self,
        *,
        event_ids: Sequence[str] = (),
        since: str = "",
        limit: int = 0,
    ) -> list[dict[str, Any]]:
        """取本地台账里带消息控制号的事件行（**参数化 SQL**）。

        条件值一律进参数位 —— 消息控制号里带引号、分号也只是值。
        """
        sql = "SELECT * FROM events WHERE message_control_id != ''"
        params: list[Any] = []
        if event_ids:
            placeholders = ",".join("?" for _ in event_ids)
            sql += f" AND event_id IN ({placeholders})"
            params.extend(str(e) for e in event_ids)
        if since:
            sql += " AND observed_at >= ?"
            params.append(since)
        sql += " ORDER BY message_control_id, event_id"
        if limit and int(limit) > 0:
            sql += " LIMIT ?"
            params.append(int(limit))
        return self.store.query_rows(sql, params)

    def _load_remote(
        self,
        local_rows: Sequence[Mapping[str, Any]],
        notes: list[str],
    ) -> tuple[dict[str, ReceiptRecord], int, bool, set[str]]:
        """取登记系统侧的回执。

        先试**列全部**（能检出「远端有、本地没有」）；对端不支持时**降级**为
        按消息控制号逐条查，并把降级写进摘要 —— 少查一类差异可以，假装查全
        了不行。

        返回 ``(回执索引, 回执条数, 是否支持列表, 查不到因而状态未知的键)``。
        **「查不到」不等于「没有」** —— 查询失败的键进第四个返回值，它们在
        对账时**不判 missing_remote**（见 :meth:`reconcile`）。
        """
        listing = self.client.list_receipts()
        if listing.ok:
            index = merge_receipts(listing.receipts)
            return index, len(index), True, set()

        notes.append(
            f"登记系统列表查询失败（{listing.error or listing.status}），"
            "已降级为按消息控制号逐条查询"
        )
        index: dict[str, ReceiptRecord] = {}
        unknown: set[str] = set()
        attempted = 0
        failed = 0
        for row in local_rows:
            mcid = _text(row.get("message_control_id"))
            if not mcid or mcid in index or mcid in unknown:
                continue
            attempted += 1
            result = self.client.fetch_receipt(mcid)
            if result.ok:
                for receipt in result.receipts:
                    if receipt.message_control_id:
                        index[receipt.message_control_id] = receipt
                continue
            if result.status == 404:
                # 对端明确说「没有这条」—— 这是**确定的没有**，与「查不到」不同。
                continue
            failed += 1
            unknown.add(mcid)
            notes.append(f"回执查询失败：{mcid} —— {result.error}")

        if attempted and failed == attempted:
            last = notes[-1] if notes else "登记系统不可达"
            raise ReconcileError(f"登记系统回执查询全部失败（{last}）")
        if unknown:
            notes.append(
                f"有 {len(unknown)} 条消息控制号没问到（查询失败），"
                "这些条目的远端状态**未判定**，不计入差异"
            )
        return index, len(index), False, unknown

    def _diff_for(
        self,
        row: Mapping[str, Any],
        receipt: Optional[ReceiptRecord],
    ) -> Optional[ReconcileDiff]:
        """比对一条本地事件与它的远端回执。"""
        mcid = _text(row.get("message_control_id"))
        event_id = _text(row.get("event_id"))
        item_code = _text(row.get("item_code"))
        dept_code = _text(row.get("dept_code"))
        local_status = _text(row.get("status"))

        if receipt is None:
            already_written = local_status in ("registered", "archived")
            if already_written:
                action = ACTION_REPLAY
                suggestion = (
                    "本地已记为「已回写」，登记系统却查无这条回执"
                    "（远端可能丢记录或被删）→ 用同一 message_control_id 重放一次回执"
                )
            else:
                action = ACTION_WRITEBACK
                suggestion = (
                    "本地已确认但登记系统查无回执 → 补写一次回执到登记系统"
                )
            return ReconcileDiff(
                message_control_id=mcid,
                event_id=event_id,
                item_code=item_code,
                dept_code=dept_code,
                diff_kind=DIFF_MISSING_REMOTE,
                action=action,
                target=TARGET_REGISTRY,
                suggestion=suggestion,
                local_status=local_status,
                remote_status="",
                detail=(
                    f"本地状态 {local_status or '空'}；确认时刻 "
                    f"{_text(row.get('acknowledged_at')) or '空'}；"
                    f"回写时刻 {_text(row.get('registered_at')) or '空'}"
                ),
            )

        mismatched, empty_remote = self._compare_fields(row, receipt)
        if mismatched:
            return ReconcileDiff(
                message_control_id=mcid,
                event_id=event_id,
                item_code=item_code,
                dept_code=dept_code,
                diff_kind=DIFF_FIELD_MISMATCH,
                action=ACTION_REPLAY,
                target=TARGET_REGISTRY,
                suggestion=(
                    "两边字段不一致（"
                    + "、".join(mismatched)
                    + "）→ 以本地台账为准重放一次回执（幂等键不变）"
                ),
                local_status=local_status,
                remote_status=receipt.receipt_status,
                mismatched_fields=tuple(mismatched),
                detail=(
                    f"远端未回填的字段（不参与比对）：{'、'.join(empty_remote) or '无'}；"
                    f"依据行 本地「{_text(row.get('threshold_row')) or '空'}」"
                    f" 远端「{receipt.threshold_row or '空'}」"
                ),
            )

        remote_status = receipt.receipt_status
        if remote_status and remote_status.lower() in _REMOTE_REJECTED:
            return ReconcileDiff(
                message_control_id=mcid,
                event_id=event_id,
                item_code=item_code,
                dept_code=dept_code,
                diff_kind=DIFF_STATUS_MISMATCH,
                action=ACTION_MANUAL,
                target=TARGET_LOCAL,
                suggestion=(
                    "本地说已回写，登记系统却说这条被拒收 / 作废"
                    "（两边说法冲突）→ 转人工核查，本轮不改写任何一边"
                ),
                local_status=local_status,
                remote_status=remote_status,
                detail=f"登记系统回执时刻 {receipt.receipt_at or '空'}",
            )

        if local_status in ("acknowledged", "writeback_failed"):
            return ReconcileDiff(
                message_control_id=mcid,
                event_id=event_id,
                item_code=item_code,
                dept_code=dept_code,
                diff_kind=DIFF_STATUS_MISMATCH,
                action=ACTION_WRITEBACK,
                target=TARGET_LOCAL,
                suggestion=(
                    "登记系统已受理这条回执，本地还停在「已确认」"
                    "→ 补登记本地状态为「已回写」"
                ),
                local_status=local_status,
                remote_status=remote_status,
                detail=f"登记系统回执时刻 {receipt.receipt_at or '空'}",
            )

        return None

    def _compare_fields(
        self,
        row: Mapping[str, Any],
        receipt: ReceiptRecord,
    ) -> tuple[list[str], list[str]]:
        """逐字段比对（返回「不一致字段」与「远端未回填字段」）。

        **远端没回填的字段不判不一致** —— 判了就是把「对方没给」当成
        「对方给错了」。这些字段单独列在明细里，让人看得见。
        """
        mismatched: list[str] = []
        empty: list[str] = []
        remote_values = receipt.to_dict()
        for local_key, remote_key, label in _COMPARED_FIELDS:
            remote_value = remote_values.get(remote_key)
            if remote_value in (None, ""):
                empty.append(label)
                continue
            local_value = row.get(local_key)
            if local_key == "notify_target":
                # 通知对象：本地是「科室 / 岗位」的人话，远端是当班人字段。
                # 本地包含远端值即视为一致（口径不同但说的是同一个人）。
                left = _text(local_value)
                right = _text(remote_value)
                if left and right and (right in left or left in right):
                    continue
                if left != right:
                    mismatched.append(label)
                continue
            if local_key == "due_minutes":
                left_value = row.get(local_key)
                try:
                    same = int(left_value) == int(remote_value)
                except (TypeError, ValueError):
                    same = _text(left_value) == _text(remote_value)
                if not same:
                    mismatched.append(label)
                continue
            if not _same_value(local_value, remote_value):
                mismatched.append(label)
        return mismatched, empty

    def _resolve_row(self, row: ReconcileDiff, *, actor: str) -> ResolveOutcome:
        """执行一次显式处理。"""
        if row.action == ACTION_MANUAL:
            detail = f"转人工处理：{row.suggestion}"
            self._audit(
                "reconcile_manual_review",
                row,
                actor=actor,
                detail=detail,
            )
            return ResolveOutcome(
                message_control_id=row.message_control_id,
                event_id=row.event_id,
                action=row.action,
                target=row.target,
                ok=True,
                detail=detail,
            )

        if row.target == TARGET_LOCAL:
            return self._resolve_local(row, actor=actor)
        return self._resolve_registry(row, actor=actor)

    def _resolve_local(self, row: ReconcileDiff, *, actor: str) -> ResolveOutcome:
        """补齐**本地台账**一侧（登记系统已受理，本地状态落后）。"""
        local = self.store.get_event(row.event_id) if row.event_id else None
        if local is None:
            detail = "本地查不到这条事件，无法补登记"
            self._audit("reconcile_resolve_failed", row, actor=actor, detail=detail)
            return ResolveOutcome(
                row.message_control_id,
                row.event_id,
                row.action,
                row.target,
                False,
                detail,
            )
        current = _text(local.get("status"))
        if current == "archived":
            detail = "本地已是终态「已归档」，不再改动"
            self._audit("reconcile_resolve_skipped", row, actor=actor, detail=detail)
            return ResolveOutcome(
                row.message_control_id,
                row.event_id,
                row.action,
                row.target,
                True,
                detail,
            )
        if current in ("registered",):
            detail = "本地已是「已回写」，无需补登记"
            self._audit("reconcile_resolve_skipped", row, actor=actor, detail=detail)
            return ResolveOutcome(
                row.message_control_id,
                row.event_id,
                row.action,
                row.target,
                True,
                detail,
            )
        changed = self.store.mark_registered(
            row.event_id,
            actor=actor,
            detail="对账补登记：登记系统已受理该回执（依据远端回执事实补齐本地状态）",
        )
        detail = "已补登记本地状态为「已回写」" if changed else "本地状态未变化"
        self._audit(
            "reconcile_local_synced",
            row,
            actor=actor,
            detail=detail,
        )
        return ResolveOutcome(
            row.message_control_id,
            row.event_id,
            row.action,
            row.target,
            True,
            detail,
        )

    def _resolve_registry(self, row: ReconcileDiff, *, actor: str) -> ResolveOutcome:
        """补写 / 重放到**登记系统**（真实出站调用，幂等键不变）。"""
        local = self.store.get_event(row.event_id) if row.event_id else None
        if local is None:
            detail = "本地查不到这条事件，没有可回写的台账内容"
            self._audit("reconcile_resolve_failed", row, actor=actor, detail=detail)
            return ResolveOutcome(
                row.message_control_id,
                row.event_id,
                row.action,
                row.target,
                False,
                detail,
            )
        payload = build_receipt_payload(local)
        if not payload.get("message_control_id"):
            detail = "台账行缺消息控制键，无法按幂等键回写"
            self._audit("reconcile_resolve_failed", row, actor=actor, detail=detail)
            return ResolveOutcome(
                row.message_control_id,
                row.event_id,
                row.action,
                row.target,
                False,
                detail,
            )

        result = self.client.post_receipt(payload)
        if not result.ok:
            detail = f"回写失败：{result.error or result.status}"
            self._audit(
                "reconcile_writeback_failed",
                row,
                actor=actor,
                detail=detail,
            )
            return ResolveOutcome(
                row.message_control_id,
                row.event_id,
                row.action,
                row.target,
                False,
                detail,
            )

        changed = False
        current = _text(local.get("status"))
        if current not in ("registered", "archived"):
            # 只有「还没写成」的才推进本地状态。**已经记着「已回写」的，
            # 重放不覆盖原来的回写时刻** —— 那个时刻是「第一次写进去」的
            # 事实，重放一次不该把它改成重放的时刻。
            changed = self.store.mark_registered(
                row.event_id,
                actor=actor,
                detail=f"对账{row.action}：回执已写入登记系统（幂等键 {payload['message_control_id']}）",
            )
        verb = "补写" if row.action == ACTION_WRITEBACK else "重放"
        detail = f"{verb}成功，登记系统已受理（HTTP {result.status}）"
        if changed:
            detail += "；本地已登记为「已回写」"
        elif current == "registered":
            detail += "；本地已是「已回写」，保留原回写时刻"
        self._audit(
            "reconcile_writeback_succeeded",
            row,
            actor=actor,
            detail=detail,
        )
        return ResolveOutcome(
            row.message_control_id,
            row.event_id,
            row.action,
            row.target,
            True,
            detail,
        )

    def _audit(
        self,
        action: str,
        row: ReconcileDiff,
        *,
        actor: str,
        detail: str,
    ) -> None:
        """处理动作写审计（append-only）。"""
        self.store.append_audit(
            action,
            actor=actor,
            event_id=row.event_id,
            dept_code=row.dept_code,
            item_code=row.item_code,
            message_control_id=row.message_control_id,
            detail=f"{row.diff_kind}：{detail}",
        )
