"""值班台 JSON 接口：台账列表与事件详情（任务 id 49）。

**为什么要有这一层**

值班台页面是给人看的（服务端渲染 HTML）；这一层是给**别的系统**看的 ——
院内大屏、科主任的看板、第三方核查工具，都要拿一份**结构化**的台账。
两者共用同一份台账与同一份处置卡快照，但**出口必须分开**：页面走 HTML，
接口走 JSON，且 JSON 的字段是**白名单**而不是「把那一行整条倒出去」。

**白名单为什么必须写死**

台账事件行上有 ``patient_ref`` 这一列 —— 它是事件标识层的患者引用。
页面层不展示它（处置卡正文本身已被 ``card/renderer.py`` 的白名单闸门过滤
过），但 JSON 接口是**机器出口**：一旦把整行倒出去，患者引用就跟着出去了，
而且「接口后来被人改成 ``SELECT *``」这种改动在页面上看不出来。

所以这里的口径是：

* **列表接口**只给「这条事件在业务上是什么、现在到哪一步了」；
* **详情接口**在列表字段之上追加「依据出处 / 推送记录 / 审计流水 /
  处置卡快照」；
* **患者标识层字段一个都不给**（``patient_ref`` / ``specimen_no`` 里的
  标本号是业务标识不是患者标识，但明细里仍带患者引用 —— 这里连标本号也
  一并不给，接口只回答业务问题）。

**与页面同源**：列表与详情的数据都来自 ``events`` 表与**落库的处置卡快照**
（``CardSnapshotStore``）—— 与值班台页面读的是同一份，接口不会给出与页面
不一样的依据。

**口径与任务 id 37 一致**：筛选条件走 ``critval.ledger.query.LedgerQuery``，
所以「值班台看到的」与「次日复核报表导出的」是同一套筛选语义。
"""

from __future__ import annotations

import json
from typing import Any, Mapping, Optional, Sequence

__all__ = [
    "API_DETAIL_EXTRA_FIELDS",
    "API_EVENT_FIELDS",
    "API_PATH",
    "API_PATH_PREFIX",
    "build_event_detail",
    "build_event_list",
    "build_event_row",
    "json_response",
]


#: 列表接口的字段白名单（**顺序即 JSON 里的键序**，便于人工比对）。
API_EVENT_FIELDS: tuple[str, ...] = (
    "event_id",
    "message_control_id",
    "item_code",
    "item_name",
    "value",
    "unit",
    "dept_code",
    "observed_at",
    "received_at",
    "status",
    "judgement_decision",
    "judgement_reason",
    "threshold_row",
    "notify_target",
    "due_minutes",
    "due_at",
    "notified_at",
    "acknowledged_by",
    "acknowledged_at",
    "registered_at",
    "card_id",
    "config_version",
    "ruleset_snapshot_id",
)

#: 详情接口在列表字段之上追加的字段。
API_DETAIL_EXTRA_FIELDS: tuple[str, ...] = (
    "citations",
    "notifications",
    "audits",
    "card",
)

#: 列表接口路径。
API_PATH = "/api/events"

#: 列表与详情接口的路径前缀。
API_PATH_PREFIX = "/api/events"


def _pick(row: Mapping[str, Any], fields: Sequence[str]) -> dict[str, Any]:
    """按白名单挑字段（**缺字段留空串，不报错**）。"""
    out: dict[str, Any] = {}
    for name in fields:
        value = row.get(name, "")
        out[name] = "" if value is None else value
    return out


def build_event_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """一行台账 → 列表接口的字典（**白名单裁剪**）。"""
    return _pick(row, API_EVENT_FIELDS)


def _citation_payload(items: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """依据出处（文件名 / 行号 / 版本 / 材料指纹 / 章节）。"""
    out = []
    for item in items or []:
        out.append(
            {
                "kind": str(item.get("kind", "") or ""),
                "source_name": str(item.get("source_name", "") or ""),
                "line_no": item.get("line_no", ""),
                "paragraph_no": item.get("paragraph_no", ""),
                "file_version": str(item.get("file_version", "") or ""),
                "source_sha256": str(item.get("source_sha256", "") or ""),
                "section_path": str(item.get("section_path", "") or ""),
                "locator": str(item.get("locator", "") or ""),
            }
        )
    return out


def _notification_payload(items: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """推送记录（通道 / 轮次 / 尝试 / 时刻）。"""
    out = []
    for item in items or []:
        out.append(
            {
                "kind": str(item.get("kind", "") or ""),
                "channel": str(item.get("channel", "") or ""),
                "attempt": item.get("attempt", ""),
                "notified_at": str(item.get("notified_at", "") or ""),
                "due_at": str(item.get("due_at", "") or ""),
                "detail": str(item.get("detail", "") or ""),
            }
        )
    return out


def _audit_payload(items: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """审计流水（时刻 / 动作 / 操作者 / 说明）。"""
    out = []
    for item in items or []:
        out.append(
            {
                "audit_no": item.get("audit_no", ""),
                "at": str(item.get("at", "") or ""),
                "action": str(item.get("action", "") or ""),
                "actor": str(item.get("actor", "") or ""),
                "detail": str(item.get("detail", "") or ""),
            }
        )
    return out


def _card_payload(snapshot: Any) -> dict[str, Any]:
    """处置卡快照摘要（**取自快照，不重新渲染**）。

    给出快照 ID 与版本三元组（阈值表 / 制度 / 配置）+ 四要素 + 口径冲突 ——
    复核的人拿到这份 JSON 时，看到的与当时推给值班人的是同一份依据。
    """
    if snapshot is None:
        return {}
    payload = dict(getattr(snapshot, "payload", {}) or {})
    elements = []
    for element in payload.get("elements", []) or []:
        source = dict(element.get("source") or {})
        elements.append(
            {
                "key": str(element.get("key", "") or ""),
                "label": str(element.get("label", "") or ""),
                "value": str(element.get("value", "") or ""),
                "status": str(element.get("status", "") or ""),
                "reason": str(element.get("reason", "") or ""),
                "note": str(element.get("note", "") or ""),
                "locator": str(element.get("locator", "") or ""),
                "source": {
                    "source_name": str(source.get("source_name", "") or ""),
                    "line_no": source.get("line_no", ""),
                    "file_version": str(source.get("file_version", "") or ""),
                    "source_sha256": str(source.get("source_sha256", "") or ""),
                    "section_path": str(source.get("section_path", "") or ""),
                },
            }
        )
    conflicts = []
    for conflict in payload.get("conflicts", []) or []:
        conflicts.append(
            {
                "conflict_id": str(conflict.get("conflict_id", "") or ""),
                "kind": str(conflict.get("kind", "") or ""),
                "kind_label": str(conflict.get("kind_label", "") or ""),
                "field": str(conflict.get("field", "") or ""),
                "field_label": str(conflict.get("field_label", "") or ""),
                "description": str(conflict.get("description", "") or ""),
                "status": str(conflict.get("status", "") or ""),
                "sides": [
                    {
                        "path": str(side.get("path", "") or ""),
                        "path_label": str(side.get("path_label", "") or ""),
                        "role": str(side.get("role", "") or ""),
                        "locator": str(side.get("locator", "") or ""),
                        "value": str(side.get("value", "") or ""),
                        "version": str(side.get("version", "") or ""),
                    }
                    for side in (conflict.get("sides") or [])
                ],
            }
        )
    return {
        "snapshot_id": str(getattr(snapshot, "snapshot_id", "") or ""),
        "card_id": str(getattr(snapshot, "card_id", "") or ""),
        "created_at": str(getattr(snapshot, "created_at", "") or ""),
        "threshold_version": str(getattr(snapshot, "threshold_version", "") or ""),
        "policy_version": str(getattr(snapshot, "policy_version", "") or ""),
        "config_version": int(getattr(snapshot, "config_version", 0) or 0),
        "ruleset_snapshot_id": str(getattr(snapshot, "ruleset_snapshot_id", "") or ""),
        "elements": elements,
        "conflicts": conflicts,
        "missing_elements": list(payload.get("missing_elements", []) or []),
        "degraded_elements": list(payload.get("degraded_elements", []) or []),
    }


def build_event_list(
    rows: Sequence[Mapping[str, Any]],
    *,
    total: int = 0,
    page: int = 1,
    per_page: int = 20,
    filters: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """列表接口的响应体。

    ``total`` 是**同一套筛选条件下的总条数**（不受分页影响），
    ``page`` / ``per_page`` 让调用方知道自己在第几页。
    """
    return {
        "items": [build_event_row(row) for row in rows or []],
        "total": int(total),
        "page": int(page),
        "per_page": int(per_page),
        "filters": dict(filters or {}),
        "fields": list(API_EVENT_FIELDS),
    }


def build_event_detail(
    row: Mapping[str, Any],
    *,
    citations: Sequence[Mapping[str, Any]] = (),
    notifications: Sequence[Mapping[str, Any]] = (),
    audits: Sequence[Mapping[str, Any]] = (),
    snapshot: Any = None,
) -> dict[str, Any]:
    """详情接口的响应体（列表字段 + 四类追加内容）。"""
    detail = build_event_row(row)
    detail["citations"] = _citation_payload(citations)
    detail["notifications"] = _notification_payload(notifications)
    detail["audits"] = _audit_payload(audits)
    detail["card"] = _card_payload(snapshot)
    return detail


def json_response(payload: Mapping[str, Any]) -> str:
    """响应体序列化（**UTF-8 原样输出中文**，不转义成 \\uXXXX）。"""
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False)
