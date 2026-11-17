"""危急值登记系统回写（任务 id 11 建通道 · 任务 id 43 加契约与冲突闸门）。

**对端**：院内危急值登记 / 护理执行平台。

=====================  =====================================================
出站调用               用途
=====================  =====================================================
``POST {CRITVAL_BASE_URL}/api/v1/critical-events``
                     回写一条已确认的回执（**写**，幂等键 ``message_control_id``）
``GET  {CRITVAL_BASE_URL}/api/v1/critical-events?message_control_id=…``
                     补写前的**远端探针**（可选）：登记系统里那条还在吗
=====================  =====================================================

**凭据从环境变量读**：``CRITVAL_BASE_URL``（基地址）与 ``CRITVAL_TOKEN``
（``Authorization: Bearer``）。代码内不出现密钥；**缺凭据位时在发请求之前
就报错** —— 不能「先发出去再说」，也不能拿本地替身顶上去。

**唯一允许写回的条件：值班人已确认。** 这是整条闭环的红线 ——

* 状态不是 ``acknowledged`` → **一次出站调用都不发**，只回报「跳过」；
* 值班人确认之后才 POST，登记系统侧那条记录才是**已生效事实**；
* 回写失败落 ``writeback_failed``，但**已确认的回执不丢**（台账行里的
  ``acknowledged_by`` / ``acknowledged_at`` 原样保留），等补写。

**幂等键是 ``message_control_id``**：登记系统按这个键覆盖同一条，补写与
重放不会在登记系统里生成第二条 —— 这是「敢重放」的前提。**但正因如此，
一次内容不同的重放就会覆盖掉登记系统里那条既有记录** —— 任务 id 43 的
幂等冲突检测就是守这一道。

=====================  =====================================================
三道闸门（都在**发出之前**）
=====================  =====================================================
#. **契约闸门**（:mod:`critval.writeback.contract`）—— 必填 / 类型 /
   枚举 / 长度上限 / 时刻格式。**不通过就一个字节都不发**，并给字段级原因。
#. **冲突闸门**（本模块）—— 同一 ``message_control_id`` 下内容与既有事实
   不一致 → **报冲突、转人工、既有事实一个字都不改**。
#. **失败分级**（:mod:`critval.writeback.failures`）—— ``4xx`` 契约错
   （**不重试**，人工修数据）；``5xx`` / 超时 / 不可达（**可重试**）。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Optional, Sequence

from critval.ledger.states import (
    STATUS_ACKNOWLEDGED,
    STATUS_REGISTERED,
    STATUS_TRANSFERRED_TO_HUMAN,
    STATUS_WRITEBACK_FAILED,
    validate_transition,
)
from critval.writeback.client import (
    RECEIPT_FIELDS,
    RegistrationSystemClient,
    build_receipt_payload,
)
from critval.writeback.contract import (
    ContractResult,
    FieldIssue,
    diff_payloads,
    identity_fingerprint,
    payload_fingerprint,
    validate_payload,
)
from critval.writeback.failures import (
    FAILURE_CONFLICT,
    FAILURE_CONTRACT,
    FAILURE_LABELS,
    FAILURE_VALIDATION,
    RETRYABLE_FAILURES,
    classify_failure,
    retry_decision,
)

__all__ = [
    "CONFLICT_ALREADY_WRITTEN",
    "CONFLICT_CONTENT_CHANGED",
    "CONFLICT_REMOTE_DIFFERS",
    "REPLAY_ELIGIBLE_STATUSES",
    "RECEIPT_FIELDS",
    "RegistryWriteback",
    "WRITEBACK_ELIGIBLE_STATUSES",
    "WritebackConflict",
    "WritebackOutcome",
]

logger = logging.getLogger("critval.writeback.registry")

#: **允许发起回写**的台账状态。只有值班人已确认的才在列。
WRITEBACK_ELIGIBLE_STATUSES: tuple[str, ...] = (STATUS_ACKNOWLEDGED,)

#: **允许补写 / 重放**的台账状态（上一轮回写失败的）。
REPLAY_ELIGIBLE_STATUSES: tuple[str, ...] = (STATUS_WRITEBACK_FAILED,)

#: 冲突的三个来源。
CONFLICT_ALREADY_WRITTEN = "already_written"
CONFLICT_CONTENT_CHANGED = "content_changed"
CONFLICT_REMOTE_DIFFERS = "remote_differs"

_CONFLICT_LABELS: dict[str, str] = {
    CONFLICT_ALREADY_WRITTEN: "本地已写进过同一消息控制号，内容与既有事实不同",
    CONFLICT_CONTENT_CHANGED: "上一次回写尝试是对端不可用（可能已写入），此后内容被改动",
    CONFLICT_REMOTE_DIFFERS: "登记系统里已有的那条内容与本次要写的不一致",
}


@dataclass(frozen=True)
class WritebackConflict:
    """一次幂等冲突。

    ``kind`` 说明冲突从哪来；``fields`` 是**不一致的字段名**（便于人
    一眼看出改了什么）；``existing`` / ``incoming`` 是两边的内容摘要。
    """

    kind: str
    message_control_id: str
    fields: tuple[str, ...] = ()
    existing: Mapping[str, Any] = field(default_factory=dict)
    incoming: Mapping[str, Any] = field(default_factory=dict)
    detail: str = ""

    @property
    def label(self) -> str:
        return _CONFLICT_LABELS.get(self.kind, self.kind)

    def summary(self) -> str:
        bits = [self.label]
        if self.fields:
            bits.append("不一致字段：" + "、".join(self.fields))
        if self.detail:
            bits.append(self.detail)
        return "；".join(bits)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "label": self.label,
            "message_control_id": self.message_control_id,
            "fields": list(self.fields),
            "detail": self.detail,
            "existing": dict(self.existing),
            "incoming": dict(self.incoming),
        }


@dataclass(frozen=True)
class WritebackOutcome:
    """一次回写的结论。

    ``skipped=True`` 表示**根本没发请求**（没确认 / 事件不存在 / 契约
    不通过 / 幂等冲突）—— 这与「发了但失败」是两回事，审计上要分得清。
    """

    ok: bool
    event_id: str = ""
    skipped: bool = False
    reason: str = ""
    error: str = ""
    status: str = ""
    http_status: int = 0
    attempts: int = 0
    payload: Mapping[str, Any] = field(default_factory=dict)
    #: 失败分类（``validation`` / ``contract`` / ``conflict`` / ``server`` …）。
    failure_class: str = ""
    #: 这一类失败**能不能重试**（分类直接给出，不靠调用方自己判断）。
    retryable: bool = False
    #: 下一步该谁做什么（人话）。
    next_action: str = ""
    #: 契约校验的字段级问题（不通过时非空）。
    issues: tuple[FieldIssue, ...] = ()
    #: 幂等冲突详情（冲突时非空）。
    conflict: Optional[WritebackConflict] = None

    @property
    def failure_label(self) -> str:
        return FAILURE_LABELS.get(self.failure_class, self.failure_class)

    @property
    def conflict_fields(self) -> tuple[str, ...]:
        return self.conflict.fields if self.conflict else ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "event_id": self.event_id,
            "skipped": self.skipped,
            "reason": self.reason,
            "error": self.error,
            "status": self.status,
            "http_status": self.http_status,
            "attempts": self.attempts,
            "failure_class": self.failure_class,
            "failure_label": self.failure_label,
            "retryable": self.retryable,
            "next_action": self.next_action,
            "issues": [issue.to_dict() for issue in self.issues],
            "conflict": self.conflict.to_dict() if self.conflict else None,
            "payload": dict(self.payload),
        }


class RegistryWriteback:
    """回执回写出口（登记系统）。

    * 客户端：:class:`critval.writeback.client.RegistrationSystemClient`
      （真实 HTTP 协议 + 真实凭据位）；
    * 台账：:class:`critval.ledger.store.LedgerStore` —— 状态推进、既有
      事实与审计都落在这里。

    ``write_back`` 只做**已经确认**的回执；``replay`` 补上一轮失败的。
    两者最终都走同一个 ``_write``，幂等键一样，因此**重复补写不会在登记
    系统里生成第二条**。
    """

    def __init__(
        self,
        client: RegistrationSystemClient,
        store: Any,
        *,
        actor: str = "writeback",
        probe_remote: bool = False,
        max_attempts: int = 1,
        backoff_seconds: float = 0.0,
        sleep: Optional[Callable[[float], None]] = None,
    ) -> None:
        self.client = client
        self.store = store
        self.actor = actor
        #: 补写前先 GET 一次登记系统，核对那边那条还在不在、内容一样吗。
        #: 默认关 —— 守护进程每次回写都多一次出站往返不值当；**人工显式
        #: 补写**时打开（那时人正担心「那边是不是已经有了」）。
        self.probe_remote = bool(probe_remote)
        #: 自动重试上限（只对**可重试**分类有效；契约错与冲突永不重试）。
        self.max_attempts = max(1, int(max_attempts))
        #: 退避基数（秒）：第 n 次重试等 ``backoff_seconds * 2**(n-1)``。
        self.backoff_seconds = max(0.0, float(backoff_seconds))
        self._sleep = sleep or time.sleep

    # ---- 构造 -------------------------------------------------------------
    @classmethod
    def from_config(
        cls,
        store: Any,
        *,
        timeout: float = 5.0,
        actor: str = "writeback",
        probe_remote: bool = False,
        max_attempts: int = 1,
        backoff_seconds: float = 0.0,
    ) -> "RegistryWriteback":
        """按环境变量装配（缺凭据位**立即报错**，不发请求）。"""
        client = RegistrationSystemClient.from_config(timeout=timeout)
        return cls(
            client,
            store,
            actor=actor,
            probe_remote=probe_remote,
            max_attempts=max_attempts,
            backoff_seconds=backoff_seconds,
        )

    # ---- 回写 -------------------------------------------------------------
    def write_back(
        self,
        event_id: str,
        *,
        receipt_status: str = STATUS_ACKNOWLEDGED,
        receipt_at: str = "",
    ) -> WritebackOutcome:
        """回写一条**已确认**的回执。

        不是 ``acknowledged`` 状态 → **一次出站调用都不发**，返回
        ``skipped=True``。这是本产品最重要的一条红线：未确认的回执不许
        变成登记系统里的已生效事实。
        """
        return self._write(
            event_id,
            eligible=WRITEBACK_ELIGIBLE_STATUSES,
            receipt_status=receipt_status,
            receipt_at=receipt_at,
            action="回写",
        )

    def replay(self, event_id: str) -> WritebackOutcome:
        """补写上一轮**回写失败**的回执。

        幂等键与首次回写一致（``message_control_id``），所以补写不会在
        登记系统里生成第二条 —— 补写只在「第一次确实没写成」时才有意义。

        **内容变了的补写会被幂等冲突闸门拦下**（转人工，不覆盖既有
        事实）；**契约错的补写**则允许带修正后的内容重发。
        """
        return self._write(
            event_id,
            eligible=REPLAY_ELIGIBLE_STATUSES,
            receipt_status=STATUS_ACKNOWLEDGED,
            receipt_at="",
            action="补写",
            explicit=True,
        )

    def retry_pending(
        self,
        event_ids: Optional[Sequence[str]] = None,
        *,
        limit: int = 100,
    ) -> list[WritebackOutcome]:
        """显式重放**待写队列**里失败的那几条（CLI ``writeback-retry``）。

        * 给 ``event_ids`` → 只重放这几条；
        * 不给 → 重放待写队列里的全部（最多 ``limit`` 条）。

        仍失败的**保留在待写队列里**并继续告警 —— 不静默丢弃，也不
        假装补上了。
        """
        if event_ids:
            targets = [str(eid).strip() for eid in event_ids if str(eid).strip()]
        else:
            targets = [str(row.get("event_id", "")) for row in self.pending(limit=limit)]
        outcomes: list[WritebackOutcome] = []
        for event_id in targets:
            if not event_id:
                continue
            outcomes.append(self.replay(event_id))
        return outcomes

    def pending(self, limit: int = 100) -> list[dict[str, Any]]:
        """待补写清单（台账里状态为 ``writeback_failed`` 的事件）。"""
        rows = self.store.list_events(limit=limit)
        return [
            row
            for row in rows
            if str(row.get("status", "") or "") == STATUS_WRITEBACK_FAILED
        ]

    def pending_detail(self, limit: int = 100) -> list[dict[str, Any]]:
        """待补写清单（带失败分类 / 尝试次数 / 下一步动作）。

        值班台与 CLI 看的是这份 —— 光有「失败了」不够，得知道**为什么
        失败**（能不能重试）以及**下一步该谁做什么**。
        """
        out: list[dict[str, Any]] = []
        for row in self.pending(limit=limit):
            key = str(row.get("message_control_id", "") or "").strip()
            last = self.store.last_writeback_attempt(key) or {}
            failure_class = str(last.get("failure_class", "") or "")
            attempts = self.store.writeback_attempt_count(key)
            decision = retry_decision(
                failure_class,
                attempts=attempts,
                explicit=True,
            )
            out.append(
                {
                    "event_id": str(row.get("event_id", "") or ""),
                    "message_control_id": key,
                    "item_code": str(row.get("item_code", "") or ""),
                    "dept_code": str(row.get("dept_code", "") or ""),
                    "failure_class": failure_class,
                    "failure_label": FAILURE_LABELS.get(failure_class, failure_class or "未知"),
                    "retryable": decision.retryable,
                    "attempts": attempts,
                    "last_error": str(last.get("error", "") or ""),
                    "next_action": decision.action,
                    "reason": decision.reason,
                }
            )
        return out

    # ---- 内部 -------------------------------------------------------------
    def _write(
        self,
        event_id: str,
        *,
        eligible: tuple[str, ...],
        receipt_status: str,
        receipt_at: str,
        action: str,
        explicit: bool = False,
    ) -> WritebackOutcome:
        row = self.store.get_event(event_id)
        if not row:
            return WritebackOutcome(
                ok=False,
                skipped=True,
                event_id=event_id,
                reason="台账里没有这条事件",
            )

        status = str(row.get("status", "") or "")
        if status not in eligible:
            return WritebackOutcome(
                ok=False,
                skipped=True,
                event_id=event_id,
                status=status,
                reason=(
                    f"当前状态 {status or '（空）'} 不允许{action}"
                    f"（只允许 {'/'.join(eligible)}）"
                ),
            )

        payload = build_receipt_payload(
            row, receipt_status=receipt_status, receipt_at=receipt_at
        )
        key = str(payload.get("message_control_id") or "").strip()
        if not key:
            return WritebackOutcome(
                ok=False,
                skipped=True,
                event_id=event_id,
                status=status,
                reason="缺 message_control_id（幂等键不能为空，不发请求）",
            )

        # ---- 闸门一：契约校验（发出前拦截，字段级原因） -------------------
        contract: ContractResult = validate_payload(payload)
        if not contract.ok:
            return self._reject_contract(
                event_id, row, key, payload, contract, action
            )

        # ---- 闸门二：幂等冲突（既有事实不许被覆盖） ------------------------
        conflict = self._detect_conflict(key, payload)
        if conflict is not None:
            return self._reject_conflict(event_id, row, key, payload, conflict)

        # ---- 闸门三：出站调用（失败按分类决定重试策略） --------------------
        return self._post_with_retry(
            event_id, row, key, payload, action=action, explicit=explicit
        )

    # ---- 契约闸门 ---------------------------------------------------------
    def _reject_contract(
        self,
        event_id: str,
        row: Mapping[str, Any],
        key: str,
        payload: Mapping[str, Any],
        contract: ContractResult,
        action: str,
    ) -> WritebackOutcome:
        """契约不过：**一个字节都不发**，落 ``writeback_failed`` 等人工改数据。"""
        moment = datetime.now(timezone.utc)
        detail = f"{action}被契约闸门拦下（未发出请求）：{contract.summary()}"
        self.store.append_audit(
            "writeback_contract_invalid",
            event_id=event_id,
            actor=self.actor,
            dept_code=str(row.get("dept_code", "") or ""),
            specimen_no=str(row.get("specimen_no", "") or ""),
            item_code=str(row.get("item_code", "") or ""),
            message_control_id=key,
            detail=detail,
            at=moment,
        )
        self.store.record_writeback_attempt(
            event_id,
            key,
            payload_hash=payload_fingerprint(payload),
            outcome="rejected",
            failure_class=FAILURE_VALIDATION,
            retryable=False,
            error=contract.summary(),
            detail="本地契约校验未通过，未发出请求",
            at=moment,
        )
        self._set_failed(event_id, row, key, detail, moment)
        decision = retry_decision(FAILURE_VALIDATION, attempts=1)
        return WritebackOutcome(
            ok=False,
            skipped=True,
            event_id=event_id,
            status=STATUS_WRITEBACK_FAILED,
            reason=detail,
            error=contract.summary(),
            failure_class=FAILURE_VALIDATION,
            retryable=False,
            next_action=decision.action,
            issues=contract.issues,
            payload=payload,
        )

    # ---- 冲突闸门 ---------------------------------------------------------
    def _detect_conflict(
        self, key: str, payload: Mapping[str, Any]
    ) -> Optional[WritebackConflict]:
        """幂等冲突检测（**在发出之前**）。

        三种来源：

        1. **本地已写进过**（``writeback_receipts``）—— 同一消息控制号下的
           内容与既有事实不同 → 冲突（既有事实一字不改）；
        2. **上一次尝试是对端不可用**（5xx / 超时）—— 那次可能其实写进去
           了，此后内容被改过 → 冲突（不能拿新内容盖掉可能已存在的那条）；
        3. **远端探针**（可选）—— 登记系统里那条内容与本次要写的不同。

        **上一次是契约错 / 校验错**不算冲突 —— 那是「人正在改数据」，
        改完重发是正当流程。
        """
        fingerprint = payload_fingerprint(payload)

        # ① 本地既有事实
        receipt = self.store.get_writeback_receipt(key)
        if receipt:
            old_hash = str(receipt.get("payload_hash", "") or "")
            if old_hash and old_hash != fingerprint:
                old_payload = self._payload_of(receipt)
                return WritebackConflict(
                    kind=CONFLICT_ALREADY_WRITTEN,
                    message_control_id=key,
                    fields=diff_payloads(old_payload, payload),
                    existing=old_payload,
                    incoming=dict(payload),
                    detail=(
                        f"本地既有事实写于 {receipt.get('written_at', '') or '（时间未知）'}，"
                        "既有事实保持原样，不被覆盖"
                    ),
                )
            # 指纹一致 → 同一份内容的重放，放行（登记系统按幂等键覆盖同一条）。

        # ② 上一次尝试
        last = self.store.last_writeback_attempt(key)
        if last:
            last_hash = str(last.get("payload_hash", "") or "")
            last_class = str(last.get("failure_class", "") or "")
            last_outcome = str(last.get("outcome", "") or "")
            if last_hash and last_hash != fingerprint:
                if last_outcome == "succeeded":
                    return WritebackConflict(
                        kind=CONFLICT_ALREADY_WRITTEN,
                        message_control_id=key,
                        detail="上一次已成功写入，内容此后被改动",
                    )
                if last_class in RETRYABLE_FAILURES:
                    return WritebackConflict(
                        kind=CONFLICT_CONTENT_CHANGED,
                        message_control_id=key,
                        detail=(
                            f"上一次尝试是「{FAILURE_LABELS.get(last_class, last_class)}」"
                            "（可能已写进登记系统），此后内容被改动；"
                            "先人工核对登记系统那条，再决定是否重写"
                        ),
                    )

        # ③ 远端探针（可选）
        if self.probe_remote:
            remote_conflict = self._probe_remote_conflict(key, payload)
            if remote_conflict is not None:
                return remote_conflict
        return None

    def _probe_remote_conflict(
        self, key: str, payload: Mapping[str, Any]
    ) -> Optional[WritebackConflict]:
        """远端探针：登记系统里那条还在吗、内容一样吗。

        探针**失败不拦写入**（对端抽风时 POST 大概也会失败，交给失败分类
        处理）；只有**明确读到一条内容不同的回执**才判冲突。
        """
        try:
            result = self.client.fetch_receipt(key)
        except Exception as exc:  # noqa: BLE001 —— 探针失败不改变写入意图
            logger.warning("远端探针失败（不拦写入）：%s", exc)
            return None
        if not result.ok or not result.receipts:
            return None
        remote = result.receipts[0]
        remote_payload = remote.to_dict()
        if identity_fingerprint(remote_payload) == identity_fingerprint(payload):
            return None
        return WritebackConflict(
            kind=CONFLICT_REMOTE_DIFFERS,
            message_control_id=key,
            fields=diff_payloads(remote_payload, payload),
            existing=remote_payload,
            incoming=dict(payload),
            detail="登记系统里已有的那条内容不同；既有事实不许被覆盖",
        )

    def _reject_conflict(
        self,
        event_id: str,
        row: Mapping[str, Any],
        key: str,
        payload: Mapping[str, Any],
        conflict: WritebackConflict,
    ) -> WritebackOutcome:
        """冲突：不发请求、转人工、既有事实一字不改。"""
        moment = datetime.now(timezone.utc)
        detail = f"幂等冲突，未发出请求：{conflict.summary()}"
        self.store.append_audit(
            "writeback_conflict",
            event_id=event_id,
            actor=self.actor,
            dept_code=str(row.get("dept_code", "") or ""),
            specimen_no=str(row.get("specimen_no", "") or ""),
            item_code=str(row.get("item_code", "") or ""),
            message_control_id=key,
            detail=detail,
            at=moment,
        )
        self.store.record_writeback_attempt(
            event_id,
            key,
            payload_hash=payload_fingerprint(payload),
            outcome="conflict",
            failure_class=FAILURE_CONFLICT,
            retryable=False,
            error=conflict.summary(),
            detail="幂等冲突，未发出请求；既有事实未被覆盖",
            at=moment,
        )
        self._transfer_to_human(event_id, row, detail, moment)
        decision = retry_decision(FAILURE_CONFLICT, attempts=1)
        return WritebackOutcome(
            ok=False,
            skipped=True,
            event_id=event_id,
            status=STATUS_TRANSFERRED_TO_HUMAN,
            reason=detail,
            error=conflict.summary(),
            failure_class=FAILURE_CONFLICT,
            retryable=False,
            next_action=decision.action,
            conflict=conflict,
            payload=payload,
        )

    # ---- 出站与重试 -------------------------------------------------------
    def _post_with_retry(
        self,
        event_id: str,
        row: Mapping[str, Any],
        key: str,
        payload: Mapping[str, Any],
        *,
        action: str,
        explicit: bool = False,
    ) -> WritebackOutcome:
        """发出回写；失败按分类决定**要不要再试**。

        * ``4xx`` 契约错 → **不重试**（重试只是把同一个错再发一次）；
        * ``5xx`` / 超时 / 不可达 → 退避后重试，直到 ``max_attempts``。

        ``explicit=True``（人工发起的补写）不受自动重试次数限制 —— 人点了
        就是再试一次；但**契约错与冲突依然不重试**。
        """
        attempts = 0
        last_result: Any = None
        while attempts < self.max_attempts:
            attempts += 1
            moment = datetime.now(timezone.utc)
            self.store.append_audit(
                "writeback_attempted",
                event_id=event_id,
                actor=self.actor,
                dept_code=str(row.get("dept_code", "") or ""),
                specimen_no=str(row.get("specimen_no", "") or ""),
                item_code=str(row.get("item_code", "") or ""),
                message_control_id=key,
                detail=f"{action}登记系统（幂等键 message_control_id）",
                at=moment,
            )
            last_result = self.client.post_receipt(payload)
            if last_result.ok:
                return self._on_success(
                    event_id, row, key, payload, last_result, moment, attempts
                )

            failure_class = classify_failure(
                last_result.status,
                last_result.error,
                timeout="超时" in (last_result.error or ""),
                network="不可达" in (last_result.error or ""),
            )
            decision = retry_decision(
                failure_class,
                attempts=attempts,
                max_attempts=self.max_attempts,
                explicit=explicit,
            )
            if decision.should_retry and attempts < self.max_attempts:
                delay = self.backoff_seconds * (2 ** (attempts - 1))
                logger.warning(
                    "回写失败可重试（%s）：%s；%.1f 秒后第 %d 次重试",
                    failure_class,
                    last_result.error,
                    delay,
                    attempts + 1,
                )
                if delay > 0:
                    self._sleep(delay)
                continue
            return self._on_failure(
                event_id,
                row,
                key,
                payload,
                last_result,
                failure_class,
                decision,
                moment,
                attempts,
                action,
                explicit=explicit,
            )

        # 理论上到不了这里（循环内必返回），留一个兜底别让调用方拿到 None。
        return WritebackOutcome(
            ok=False,
            event_id=event_id,
            reason=f"{action}未完成",
            failure_class=FAILURE_CONTRACT,
        )

    def _on_success(
        self,
        event_id: str,
        row: Mapping[str, Any],
        key: str,
        payload: Mapping[str, Any],
        result: Any,
        moment: datetime,
        attempts: int,
    ) -> WritebackOutcome:
        """回写成功：登记既有事实 + 推进状态。"""
        self.store.record_writeback_receipt(
            key,
            event_id,
            payload,
            payload_hash=payload_fingerprint(payload),
            http_status=int(getattr(result, "status", 200) or 200),
            at=moment,
        )
        self.store.record_writeback_attempt(
            event_id,
            key,
            payload_hash=payload_fingerprint(payload),
            outcome="succeeded",
            http_status=int(getattr(result, "status", 200) or 200),
            detail="回写成功（已生效事实）",
            at=moment,
        )
        self.store.mark_registered(
            event_id,
            actor=self.actor,
            detail="回写成功（已生效事实）",
            at=moment,
        )
        return WritebackOutcome(
            ok=True,
            event_id=event_id,
            status=STATUS_REGISTERED,
            http_status=int(getattr(result, "status", 200) or 200),
            attempts=attempts,
            payload=payload,
        )

    def _on_failure(
        self,
        event_id: str,
        row: Mapping[str, Any],
        key: str,
        payload: Mapping[str, Any],
        result: Any,
        failure_class: str,
        decision: Any,
        moment: datetime,
        attempts: int,
        action: str,
        *,
        explicit: bool = False,
    ) -> WritebackOutcome:
        """回写失败：落 ``writeback_failed``，**已确认回执不丢**。

        台账行里的 ``acknowledged_by`` / ``acknowledged_at`` 原样保留，
        等补写。
        """
        if explicit:
            # 人工显式补写仍失败 → 下一步是「留在待写队列里继续告警」，
            # 不是「再自动重试几次」（人自己会决定什么时候再点）。
            decision = replace(
                decision,
                action="留在待写队列 · 继续告警",
                reason="人工显式补写仍失败：事件保留在待写队列里，不被丢弃",
            )
        detail = (
            f"{action}失败：{result.error or '对端未接受'}"
            f"（{FAILURE_LABELS.get(failure_class, failure_class)}，"
            f"{'可重试' if decision.retryable else '不可重试'}）；"
            "已确认回执保留，等补写"
        )
        self.store.record_writeback_attempt(
            event_id,
            key,
            payload_hash=payload_fingerprint(payload),
            outcome="failed",
            failure_class=failure_class,
            retryable=bool(decision.retryable),
            http_status=int(getattr(result, "status", 0) or 0),
            error=str(result.error or ""),
            detail=detail,
            at=moment,
        )
        self._set_failed(event_id, row, key, detail, moment)
        self.store.append_audit(
            "writeback_failed",
            event_id=event_id,
            actor=self.actor,
            dept_code=str(row.get("dept_code", "") or ""),
            specimen_no=str(row.get("specimen_no", "") or ""),
            item_code=str(row.get("item_code", "") or ""),
            message_control_id=key,
            detail=detail,
            at=moment,
        )
        return WritebackOutcome(
            ok=False,
            event_id=event_id,
            status=STATUS_WRITEBACK_FAILED,
            error=result.error or "登记系统未接受回执",
            http_status=int(getattr(result, "status", 0) or 0),
            attempts=attempts,
            failure_class=failure_class,
            retryable=bool(decision.retryable),
            next_action=decision.action,
            payload=payload,
        )

    # ---- 状态落库 ---------------------------------------------------------
    def _set_failed(
        self,
        event_id: str,
        row: Mapping[str, Any],
        key: str,
        detail: str,
        moment: datetime,
    ) -> None:
        """落 ``writeback_failed``（同状态是幂等空操作，不算非法跃迁）。"""
        try:
            self.store.set_status(
                event_id,
                STATUS_WRITEBACK_FAILED,
                actor=self.actor,
                detail=detail,
                at=moment,
            )
        except Exception as exc:  # noqa: BLE001 —— 状态不让落也不丢事件
            logger.warning("落 %s 失败（事件保留在台账里）：%s", STATUS_WRITEBACK_FAILED, exc)

    def _transfer_to_human(
        self,
        event_id: str,
        row: Mapping[str, Any],
        detail: str,
        moment: datetime,
    ) -> None:
        """冲突 → 转人工（``transferred_to_human``）。

        既有事实**不动**：台账里那条已写进去的回执行由
        ``writeback_receipts`` 原样保留，这里只改**事件状态**并留审计。
        """
        current = str(row.get("status", "") or "")
        if current == STATUS_TRANSFERRED_TO_HUMAN:
            return
        try:
            validate_transition(current, STATUS_TRANSFERRED_TO_HUMAN)
        except Exception as exc:  # noqa: BLE001 —— 不让落就留告警，不硬改事实
            logger.warning("转人工被状态机拦下（事件保持原状）：%s", exc)
            return
        try:
            self.store.set_status(
                event_id,
                STATUS_TRANSFERRED_TO_HUMAN,
                actor=self.actor,
                detail=detail,
                at=moment,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("转人工落库失败（事件保持原状）：%s", exc)

    # ---- 小工具 -----------------------------------------------------------
    @staticmethod
    def _payload_of(receipt: Mapping[str, Any]) -> dict[str, Any]:
        """把既有回执行还原成载荷（用于逐字段比对）。"""
        import json

        raw = str(receipt.get("payload_json", "") or "")
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}
