"""append-only 审计流水：只落标本号 / 项目码 / 时间 / 科室 / 角色。

**审计是什么**：这条流水线回答的是「事后有人问『这条危急值当时是怎么
处理的』，我们拿得出什么」。问的通常就是四句话：

1. **按哪条依据** —— 命中阈值表的哪一行（文件名 + 行号 + 版本）；
2. **通知了谁** —— 哪个科室的谁；
3. **几分钟内** —— 上报时限；
4. **谁确认的** —— 值班人姓名（角色）+ 确认时刻。

这四问由 :meth:`AuditLog.trace` 一次答完，答案全部来自**已落库的
事实**（事件行 + 引用表 + 通知记录 + 审计流水），不是现场拼出来的。

**append-only 是硬约束**：审计行只追加，不改、不删。SQLite 层用两个
触发器兜住（``audit_no_update`` / ``audit_no_delete``）—— 触发器是**库
级**的约束，绕过 Python 代码直接改库同样会被拦下。

**但 append-only 不等于不可篡改**（任务 id 40）：触发器只挡「还握着
库连接的人」，挡不住「拿到库文件的人」—— 他可以先 ``DROP TRIGGER``
再改一条，或者直接编辑文件。所以每条审计行另带**链式哈希**：本条内容
哈希 + 上一条的链哈希。改一条、改顺序、删一条，链都对不上；
``cli audit-verify`` 会报出**首个断链位置**。链哈希证明的是「这一串
审计自打头到尾没被动过」，不是「审计里写的内容是真的」—— 后者靠的是
写入口径与四问断言，不是哈希。

**审计不落患者明细**：审计只记标本号 / 项目码 / 科室 / 角色 / 时刻 /
消息控制号，以及一段说明文字。说明文字也过一遍患者标识扫描 ——
把患者姓名写进 ``detail`` 这种「看起来只是说明」的路子，一样堵死。
"""

from __future__ import annotations

import csv
import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

__all__ = [
    "AUDIT_ACTIONS",
    "AUDIT_PAYLOAD_FIELDS",
    "AuditDetailLeak",
    "AuditLog",
    "ChainBreak",
    "ChainReport",
    "GENESIS_HASH",
    "PATIENT_DETAIL_PATTERNS",
    "TraceAnswers",
    "assert_detail_clean",
    "audit_chain_hash",
    "audit_payload_hash",
    "canonical_audit_payload",
    "verify_audit_chain",
]


#: 审计动作名白名单 —— 新增动作要先在这里登记，避免出现拼错的动作名
#: 混进流水里（事后按动作名统计会漏）。
AUDIT_ACTIONS: frozenset[str] = frozenset(
    {
        "status_transition",
        "event_duplicate",
        "event_received",
        "event_superseded",
        "supersede_conflict",
        "ambiguous_observation",
        "retrieve_empty_no_citation",
        "policy_conflict",
        "notify_sent",
        "notify_attempted",
        "notify_retry",
        "notify_retry_exhausted",
        "notify_failed",
        "event_acknowledged",
        "writeback_succeeded",
        "writeback_failed",
        # 崩溃恢复（task id 39）：续跑推进了什么、为什么没能推进。
        "writeback_attempted",
        "recovery_resumed",
        "recovery_deferred",
        "unparsed_event",
        "inbound_unauthorized",
        "material_ingested",
        "parse_report_reviewed",
        # 处置链（task id 12）：链上跑到哪一步、为什么停下、往哪儿落。
        "flow_stopped",
        "flow_param_invalid",
        "flow_node_failed",
        "flow_loop_aborted",
        "flow_transferred_to_human",
        # 处置链（task id 44）：节点契约不满足（输入拿不到 / 产出形状不对）。
        "flow_contract_violation",
    }
)


#: 说明文字里的患者标识特征（命中即拒）。
PATIENT_DETAIL_PATTERNS: tuple[str, ...] = (
    "患者姓名",
    "病人姓名",
    "姓名：",
    "身份证",
    "住院号",
    "住院流水号",
    "病案号",
    "门诊号",
    "家庭住址",
    "联系电话",
)

#: 18 位身份证号（末位可能是 X）。
_ID_NUMBER_RE = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")


#: 审计链的**创世哈希** —— 第一条审计行的 ``prev_hash`` 取这个值。
#: 固定常量不随库变化：链头一旦确定，整条链的哈希就定死了。
GENESIS_HASH = hashlib.sha256("critval-audit-chain-genesis-v1".encode("utf-8")).hexdigest()


#: 参与**内容哈希**的字段与顺序。顺序固定 —— 换顺序等于换口径，
#: 老链会整条校验不过，所以这个元组只增不改。
AUDIT_PAYLOAD_FIELDS: tuple[str, ...] = (
    "seq",
    "at",
    "action",
    "event_id",
    "actor",
    "dept_code",
    "specimen_no",
    "item_code",
    "message_control_id",
    "detail",
)


def canonical_audit_payload(values: Mapping[str, Any]) -> str:
    """把一条审计的内容摊成**无歧义**的规范文本。

    每个字段写成 ``名字:字节数:内容``，字段之间用换行隔开。加长度前缀
    是为了消歧义：不然 ``detail="AB" + action="C"`` 和 ``detail="A" +
    action="BC"`` 拼出来的文本一样，哈希也就一样 —— 那是能被利用的。

    ``seq`` 也在内容里，所以**任何一条记录都能脱离上下文独立校验**：
    拿到这一行就能重算它的内容哈希，不必先找到上一行。
    """
    parts: list[str] = []
    for name in AUDIT_PAYLOAD_FIELDS:
        raw = values.get(name, "")
        text = "" if raw is None else str(raw)
        parts.append(f"{name}:{len(text.encode('utf-8'))}:{text}")
    return "\n".join(parts)


def audit_payload_hash(values: Mapping[str, Any]) -> str:
    """一条审计行的**内容哈希**（sha256 十六进制）。"""
    canonical = canonical_audit_payload(values)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def audit_chain_hash(prev_hash: str, payload_hash: str) -> str:
    """链哈希 = H(上一条的链哈希 + 本条的内容哈希)。

    把上一条的链哈希接进来，断链就再也接不上了：改中间任何一条，
    从那一条起往后全部对不上。
    """
    return hashlib.sha256(f"{prev_hash}:{payload_hash}".encode("utf-8")).hexdigest()


#: 断链类型 —— 校验失败时说明**坏在哪一种**。
CHAIN_BREAK_PREV = "prev_mismatch"
CHAIN_BREAK_PAYLOAD = "payload_mismatch"
CHAIN_BREAK_CHAIN = "chain_mismatch"
CHAIN_BREAK_GAP = "seq_gap"
CHAIN_BREAK_UNANCHORED = "unanchored"

CHAIN_BREAK_LABELS: dict[str, str] = {
    CHAIN_BREAK_PREV: "与上一条的链哈希对不上",
    CHAIN_BREAK_PAYLOAD: "本条内容与内容哈希不符（记录被改过）",
    CHAIN_BREAK_CHAIN: "链哈希与内容对不上（链哈希被改过）",
    CHAIN_BREAK_GAP: "流水号不连续（中间有记录被删掉或改了号）",
    CHAIN_BREAK_UNANCHORED: "这条没链上（老库补链前的记录）",
}


@dataclass(frozen=True)
class ChainBreak:
    """一处断链。``seq`` 是断点所在的流水号。"""

    seq: int
    kind: str
    expected: str = ""
    found: str = ""
    detail: str = ""

    @property
    def label(self) -> str:
        return CHAIN_BREAK_LABELS.get(self.kind, self.kind)

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "kind": self.kind,
            "label": self.label,
            "expected": self.expected,
            "found": self.found,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class ChainReport:
    """整条审计链的校验结论。

    ``ok`` 只有在**既无断链、又无未上链记录**时才为真 —— 挂着几条没
    上链的老记录就说「通过」，等于给自己开后门。
    """

    ok: bool
    total: int
    verified: int
    head_hash: str
    genesis: str
    breaks: tuple[ChainBreak, ...] = ()

    @property
    def first_break(self) -> Optional[ChainBreak]:
        """首个断链位置 —— 复核时先看这里。"""
        return self.breaks[0] if self.breaks else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "total": self.total,
            "verified": self.verified,
            "head_hash": self.head_hash,
            "genesis": self.genesis,
            "breaks": [item.to_dict() for item in self.breaks],
            "first_break": self.first_break.to_dict() if self.first_break else None,
        }


def verify_audit_chain(rows: Iterable[Mapping[str, Any]]) -> ChainReport:
    """逐条校验审计链，返回结论。

    每条行查三件事：

    1. **内容哈希对不对** —— 重算这条的内容哈希，和行里存的对不对得上；
    2. **上一条的链哈希对不对** —— ``prev_hash`` 是不是前一条的链哈希
       （第一条对创世哈希）；
    3. **链哈希对不对** —— 用存下来的两个哈希重算链哈希，和行里存的
       对不对得上。

    流水号连续性也查一遍：中间少了一条（被删）或者被改了号，都会在
    这里露出来。
    """
    ordered = list(rows)
    breaks: list[ChainBreak] = []
    prev_chain = GENESIS_HASH
    expected_seq: Optional[int] = None
    verified = 0

    for row in ordered:
        try:
            seq = int(row.get("seq") or 0)
        except (TypeError, ValueError):
            seq = 0

        if expected_seq is not None and seq != expected_seq:
            breaks.append(
                ChainBreak(
                    seq=seq,
                    kind=CHAIN_BREAK_GAP,
                    expected=str(expected_seq),
                    found=str(seq),
                    detail=(
                        f"流水号从 {expected_seq} 直接跳到 {seq} —— "
                        f"中间少了一条记录，或记录被改了号"
                    ),
                )
            )

        found_payload = str(row.get("payload_hash") or "")
        found_prev = str(row.get("prev_hash") or "")
        found_chain = str(row.get("chain_hash") or "")

        if not found_payload or not found_chain or not found_prev:
            breaks.append(
                ChainBreak(
                    seq=seq,
                    kind=CHAIN_BREAK_UNANCHORED,
                    expected="",
                    found=found_chain,
                    detail="这条没带链哈希 —— 校验不到它是否被动过",
                )
            )
        else:
            computed_payload = audit_payload_hash(row)
            if computed_payload != found_payload:
                breaks.append(
                    ChainBreak(
                        seq=seq,
                        kind=CHAIN_BREAK_PAYLOAD,
                        expected=computed_payload,
                        found=found_payload,
                        detail=(
                            "按行内内容重算的内容哈希与存下来的对不上 —— "
                            "这条记录的内容被改过"
                        ),
                    )
                )
            if found_prev != prev_chain:
                breaks.append(
                    ChainBreak(
                        seq=seq,
                        kind=CHAIN_BREAK_PREV,
                        expected=prev_chain,
                        found=found_prev,
                        detail=(
                            "本条的 prev_hash 不是上一条的链哈希 —— "
                            "上一条被换掉了，或本条被挪了位置"
                        ),
                    )
                )
            computed_chain = audit_chain_hash(found_prev, found_payload)
            if computed_chain != found_chain:
                breaks.append(
                    ChainBreak(
                        seq=seq,
                        kind=CHAIN_BREAK_CHAIN,
                        expected=computed_chain,
                        found=found_chain,
                        detail="用行内的两个哈希重算链哈希对不上 —— 链哈希本身被改过",
                    )
                )

        verified += 1
        prev_chain = found_chain or prev_chain
        expected_seq = seq + 1

    return ChainReport(
        ok=not breaks,
        total=len(ordered),
        verified=verified,
        head_hash=prev_chain,
        genesis=GENESIS_HASH,
        breaks=tuple(breaks),
    )


class AuditDetailLeak(RuntimeError):
    """审计说明文字里出现了患者可识别信息。"""


def assert_detail_clean(detail: str) -> str:
    """检查一段审计说明文字里没有患者可识别信息。

    命中就抛 :class:`AuditDetailLeak` —— **宁可审计写不进去**，也不让
    患者姓名混进留痕。做法是拒写而不是脱敏：脱敏后的「张*」这类残缺
    信息反而更容易被误读成真名。
    """
    text = str(detail or "")
    for pattern in PATIENT_DETAIL_PATTERNS:
        if pattern in text:
            raise AuditDetailLeak(
                f"审计说明文字出现患者可识别信息「{pattern}」—— 审计只落标本号 / 项目码 / 时间 / 科室 / 角色"
            )
    if _ID_NUMBER_RE.search(text):
        raise AuditDetailLeak("审计说明文字出现 18 位身份证号形态的串")
    return text


@dataclass(frozen=True)
class TraceAnswers:
    """「按哪条依据、通知了谁、几分钟内、谁确认的」四问的答案。

    四个字段**都可以为空** —— 空意味着「台账里没有这项事实」，而不是
    「系统认为不需要」。已答与未答要分得清，否则复核时会把「没记录」
    读成「没问题」。
    """

    event_id: str
    basis: str = ""
    basis_citations: tuple[str, ...] = ()
    notified_who: str = ""
    dept_code: str = ""
    due_minutes: Optional[int] = None
    acknowledged_by: str = ""
    acknowledged_at: str = ""
    registered_at: str = ""
    status: str = ""
    answered: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    evidence: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "basis": self.basis,
            "basis_citations": list(self.basis_citations),
            "notified_who": self.notified_who,
            "dept_code": self.dept_code,
            "due_minutes": self.due_minutes,
            "acknowledged_by": self.acknowledged_by,
            "acknowledged_at": self.acknowledged_at,
            "registered_at": self.registered_at,
            "status": self.status,
            "answered": list(self.answered),
            "missing": list(self.missing),
            "evidence": [dict(item) for item in self.evidence],
        }


class AuditLog:
    """审计流水的读写门面（底层是 ``ledger`` 表）。

    用法::

        log = AuditLog(store)
        log.append("event_received", event_id=..., specimen_no=..., item_code=...)
        answers = log.trace(event_id)
    """

    def __init__(self, store: Any) -> None:
        self.store = store

    # ------------------------------------------------------------------
    # 写
    # ------------------------------------------------------------------
    def append(
        self,
        action: str,
        *,
        event_id: str = "",
        actor: str = "",
        dept_code: str = "",
        specimen_no: str = "",
        item_code: str = "",
        message_control_id: str = "",
        detail: str = "",
        at: Optional[datetime] = None,
    ) -> int:
        """追加一条审计流水，返回流水号。

        说明文字先过患者标识扫描 —— 命中就拒写（见
        :func:`assert_detail_clean`）。
        """
        safe_detail = assert_detail_clean(detail)
        return self.store.append_audit(
            action,
            event_id=event_id,
            actor=actor,
            dept_code=dept_code,
            specimen_no=specimen_no,
            item_code=item_code,
            message_control_id=message_control_id,
            detail=safe_detail,
            at=at,
        )

    # ------------------------------------------------------------------
    # 读
    # ------------------------------------------------------------------
    def entries(
        self,
        event_id: str = "",
        *,
        action: str = "",
        since: str = "",
        until: str = "",
        limit: int = 0,
    ) -> list[dict[str, Any]]:
        """查审计流水（可按事件 / 动作 / 时间窗过滤）。"""
        rows = self.store.audit_entries(event_id)
        result: list[dict[str, Any]] = []
        for row in rows:
            if action and str(row.get("action", "")) != action:
                continue
            at = str(row.get("at", ""))
            if since and at < since:
                continue
            if until and at > until:
                continue
            result.append(row)
        if limit and limit > 0:
            result = result[-limit:]
        return result

    def trace(self, event_id: str) -> TraceAnswers:
        """回答四问：按哪条依据 / 通知了谁 / 几分钟内 / 谁确认的。

        答案取自**已落库的事实**（事件行 + 引用表 + 通知记录 + 审计流水），
        不是现场拼的；取不到就留空，并列进 ``missing``，让复核的人知道
        「这项没记录」而不是「这项不需要」。
        """
        event = self.store.get_event(event_id) or {}
        citations = self.store.citations_of(event_id)
        notifications = self._notifications(event_id)
        audits = self.entries(event_id)

        basis = str(event.get("threshold_row", "") or "")
        basis_citations = tuple(
            str(row.get("locator", "") or "")
            for row in citations
            if row.get("kind") == "threshold_row" and row.get("locator")
        )
        if not basis and basis_citations:
            basis = basis_citations[0]

        notify_target = str(event.get("notify_target", "") or "")
        dept_code = str(event.get("dept_code", "") or "")
        notified_who = " / ".join(part for part in (dept_code, notify_target) if part)

        due_minutes = event.get("due_minutes")
        due_minutes = int(due_minutes) if due_minutes not in (None, "") else None

        acknowledged_by = str(event.get("acknowledged_by", "") or "")
        acknowledged_at = str(event.get("acknowledged_at", "") or "")
        registered_at = str(event.get("registered_at", "") or "")

        answered: list[str] = []
        missing: list[str] = []
        if basis or basis_citations:
            answered.append("basis")
        else:
            missing.append("basis")
        if notified_who or notifications:
            answered.append("notified_who")
        else:
            missing.append("notified_who")
        if due_minutes is not None:
            answered.append("due_minutes")
        else:
            missing.append("due_minutes")
        if acknowledged_by:
            answered.append("acknowledged_by")
        else:
            missing.append("acknowledged_by")

        return TraceAnswers(
            event_id=event_id,
            basis=basis,
            basis_citations=basis_citations,
            notified_who=notified_who,
            dept_code=dept_code,
            due_minutes=due_minutes,
            acknowledged_by=acknowledged_by,
            acknowledged_at=acknowledged_at,
            registered_at=registered_at,
            status=str(event.get("status", "") or ""),
            answered=tuple(answered),
            missing=tuple(missing),
            evidence=tuple(dict(row) for row in audits),
        )

    def _notifications(self, event_id: str) -> list[dict[str, Any]]:
        getter = getattr(self.store, "notifications_of", None)
        if getter is None:
            return []
        return list(getter(event_id))

    # ------------------------------------------------------------------
    # 链式哈希自检
    # ------------------------------------------------------------------
    def verify_chain(self) -> ChainReport:
        """校验整条审计链。

        这是「append-only 不等于不可篡改」的兑现处：库里那条
        ``audit_no_update`` 触发器只能拦还握着连接的人，拦不住先
        ``DROP TRIGGER`` 再改一条的人。链哈希把那种改动照出来。
        """
        return verify_audit_chain(self.store.audit_entries())

    # ------------------------------------------------------------------
    # 统计
    # ------------------------------------------------------------------
    def stats(
        self,
        *,
        since: str = "",
        until: str = "",
        dept_code: str = "",
    ) -> dict[str, Any]:
        """审计流水统计（按动作 / 科室 / 日期 / 事件）。"""
        rows = self.entries(since=since, until=until)
        if dept_code:
            rows = [row for row in rows if str(row.get("dept_code", "")) == dept_code]
        by_action: dict[str, int] = {}
        by_dept: dict[str, int] = {}
        by_day: dict[str, int] = {}
        events: set[str] = set()
        for row in rows:
            action = str(row.get("action", "") or "")
            by_action[action] = by_action.get(action, 0) + 1
            dept = str(row.get("dept_code", "") or "")
            if dept:
                by_dept[dept] = by_dept.get(dept, 0) + 1
            at = str(row.get("at", "") or "")
            day = at[:10]
            if day:
                by_day[day] = by_day.get(day, 0) + 1
            event_id = str(row.get("event_id", "") or "")
            if event_id:
                events.add(event_id)
        return {
            "total": len(rows),
            "events": len(events),
            "by_action": dict(sorted(by_action.items())),
            "by_dept": dict(sorted(by_dept.items())),
            "by_day": dict(sorted(by_day.items())),
        }

    # ------------------------------------------------------------------
    # 导出
    # ------------------------------------------------------------------
    def export_csv(self, path: str | Path) -> Path:
        """把审计流水导出成 CSV（复核与飞检材料用）。

        连链哈希一起导出 —— 导出的 CSV 拿到别处也能重算校验，
        不然导出这一手就把「可验证」丢在路上了。
        """
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        fieldnames = [
            "seq",
            "at",
            "action",
            "event_id",
            "actor",
            "dept_code",
            "specimen_no",
            "item_code",
            "message_control_id",
            "detail",
            "prev_hash",
            "payload_hash",
            "chain_hash",
        ]
        with target.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            for row in self.entries():
                writer.writerow({key: row.get(key, "") for key in fieldnames})
        return target


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
