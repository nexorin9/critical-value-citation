"""处置卡快照：渲染结果 + 要素数据 + 版本信息，随事件落盘且**不可变**。

为什么要有快照：事后复核时如果重新渲染，拿到的是**此刻**的卡片 ——
阈值表改版了、制度改了版、配置热载入了，重渲染出来的内容就与当时推给
值班人的那份不是一回事。快照把「当时推出去的原文」原样存下来，复核读
快照，不重新渲染。

不变量的位置：

* **不可变** —— 快照 ID = 内容 sha256；同内容重复保存得到同一个 ID，
  文件不重写；内容变了就是新 ID，旧快照仍在。
* **可校验** —— 落盘时记录各渠道正文的 sha256，复核时能发现快照被改过
  （``verify``）。
* **版本可追溯** —— 快照同时记录配置版本、阈值表版本、制度版本与规则集
  快照 ID，事后能回答「这张卡当时是在哪一版依据与配置下出出来的」。

落盘位置默认 ``data/card_snapshots/``（可再生运行数据，不进仓库）。
台账库（SQLite）由 ``critval/ledger`` 负责，本模块只管**卡片快照**这一件事。
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Optional

from critval.card.renderer import (
    CHANNELS,
    build_render_payload,
    render_html,
    render_markdown,
    render_text,
)
from critval.models import DispositionCard, to_jsonable

__all__ = [
    "CardSnapshot",
    "CardSnapshotStore",
    "SnapshotConflict",
    "SnapshotNotFound",
    "SnapshotVerification",
    "default_snapshot_dir",
]


class SnapshotNotFound(FileNotFoundError):
    """快照不存在。"""


class SnapshotConflict(RuntimeError):
    """同一 ID 下内容不一致 —— 意味着有人动过已落盘的快照。"""


def default_snapshot_dir() -> Path:
    """默认快照目录（运行期数据，可再生，不进仓库）。"""
    return Path("data") / "card_snapshots"


def _canonical(payload: Any) -> str:
    """规范化 JSON（键排序、无多余空白）—— 哈希必须稳定。"""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _threshold_version(card: DispositionCard) -> str:
    row = card.judgement.threshold_row
    return row.version if row is not None and row.version else ""


def _policy_version(card: DispositionCard) -> str:
    """制度版本：优先取制度条款出处上的版本号。"""
    for citation in card.extra_citations:
        if citation.kind == "policy_clause" and citation.file_version:
            return citation.file_version
    for element in card.elements:
        if element.source is not None and element.source.file_version:
            return element.source.file_version
    return ""


@dataclass(frozen=True)
class CardSnapshot:
    """一张处置卡在某一时刻的不可变快照。"""

    snapshot_id: str
    content_hash: str
    created_at: str
    card_id: str
    event_id: str
    message_control_id: str
    item_code: str
    config_version: int
    threshold_version: str
    policy_version: str
    ruleset_snapshot_id: str
    payload: Mapping[str, Any]
    renderings: Mapping[str, str]
    rendering_hashes: Mapping[str, str] = field(default_factory=dict)

    def rendering(self, channel: str) -> str:
        """取某个渠道的正文。"""
        if channel not in self.renderings:
            raise KeyError(f"快照里没有 {channel!r} 渠道的正文")
        return self.renderings[channel]

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "content_hash": self.content_hash,
            "created_at": self.created_at,
            "card_id": self.card_id,
            "event_id": self.event_id,
            "message_control_id": self.message_control_id,
            "item_code": self.item_code,
            "config_version": self.config_version,
            "threshold_version": self.threshold_version,
            "policy_version": self.policy_version,
            "ruleset_snapshot_id": self.ruleset_snapshot_id,
            "payload": dict(self.payload),
            "renderings": dict(self.renderings),
            "rendering_hashes": dict(self.rendering_hashes),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "CardSnapshot":
        return cls(
            snapshot_id=data["snapshot_id"],
            content_hash=data["content_hash"],
            created_at=data["created_at"],
            card_id=data["card_id"],
            event_id=data["event_id"],
            message_control_id=data["message_control_id"],
            item_code=data["item_code"],
            config_version=int(data.get("config_version", 0)),
            threshold_version=data.get("threshold_version", ""),
            policy_version=data.get("policy_version", ""),
            ruleset_snapshot_id=data.get("ruleset_snapshot_id", ""),
            payload=data.get("payload", {}),
            renderings=data.get("renderings", {}),
            rendering_hashes=data.get("rendering_hashes", {}),
        )


@dataclass(frozen=True)
class SnapshotVerification:
    """快照完整性校验结论。"""

    snapshot_id: str
    ok: bool
    problems: tuple[str, ...] = ()
    checked_at: str = ""

    def report_text(self) -> str:
        lines = [
            f"快照编号  : {self.snapshot_id}",
            f"校验时刻  : {self.checked_at}",
            f"校验结论  : {'通过（内容与落盘时逐字一致）' if self.ok else '未通过'}",
        ]
        if self.problems:
            lines.append("──────────────────────────────")
            lines.extend(f"问题 : {problem}" for problem in self.problems)
        return "\n".join(lines)


class CardSnapshotStore:
    """处置卡快照仓库（文件系统实现，内容寻址 + 只写不改）。

    目录结构::

        <root>/snap-<内容指纹前 16 位>.json   # 快照正文，一份一个文件
        <root>/index.jsonl                    # 追加式索引（事件 → 快照）
    """

    def __init__(self, root: Path | str | None = None) -> None:
        self.root = Path(root) if root is not None else default_snapshot_dir()
        self.index_path = self.root / "index.jsonl"

    # ---- 落盘 ------------------------------------------------------------
    def save(self, card: DispositionCard, confirm_url: str = "") -> CardSnapshot:
        """保存一张处置卡的快照，返回已落盘的快照。

        同内容重复保存 → 同一个 ID，**文件不重写**（幂等）；内容变了 →
        新 ID，旧快照原样保留。
        """
        payload = to_jsonable(build_render_payload(card))
        renderings = {
            "markdown": render_markdown(card, confirm_url),
            "html": render_html(card, confirm_url),
            "text": render_text(card, confirm_url),
        }
        identity = {
            "card_id": card.card_id,
            "event_id": card.event.event_id,
            "message_control_id": card.event.message_control_id,
            "item_code": card.event.item_code,
            "config_version": card.config_version,
            "threshold_version": _threshold_version(card),
            "policy_version": _policy_version(card),
            "ruleset_snapshot_id": card.judgement.ruleset_snapshot_id,
        }
        content_hash = _sha256(_canonical({"identity": identity, "payload": payload, "renderings": renderings}))
        snapshot_id = f"snap-{content_hash[:16]}"

        existing = self._load_if_exists(snapshot_id)
        if existing is not None:
            if existing.content_hash != content_hash:
                raise SnapshotConflict(
                    f"快照 {snapshot_id} 已存在且内容指纹不一致 —— 落盘目录可能被改动过"
                )
            return existing

        snapshot = CardSnapshot(
            snapshot_id=snapshot_id,
            content_hash=content_hash,
            created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            payload=payload,
            renderings=renderings,
            rendering_hashes={channel: _sha256(text) for channel, text in renderings.items()},
            **identity,
        )
        self._write_snapshot(snapshot)
        self._append_index(snapshot)
        return snapshot

    def _write_snapshot(self, snapshot: CardSnapshot) -> Path:
        """原子写：先写临时文件再 rename，避免半份快照。"""
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / f"{snapshot.snapshot_id}.json"
        body = json.dumps(snapshot.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)
        handle = tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=str(self.root), prefix=".tmp-", suffix=".json", delete=False
        )
        try:
            with handle:
                handle.write(body)
            os.replace(handle.name, path)
        except BaseException:
            Path(handle.name).unlink(missing_ok=True)
            raise
        return path

    def _append_index(self, snapshot: CardSnapshot) -> None:
        """追加索引行（追加式；同一快照重复保存不重复写行）。"""
        self.root.mkdir(parents=True, exist_ok=True)
        line = json.dumps(
            {
                "snapshot_id": snapshot.snapshot_id,
                "event_id": snapshot.event_id,
                "card_id": snapshot.card_id,
                "item_code": snapshot.item_code,
                "created_at": snapshot.created_at,
                "config_version": snapshot.config_version,
                "threshold_version": snapshot.threshold_version,
                "policy_version": snapshot.policy_version,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        with self.index_path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    # ---- 读取 ------------------------------------------------------------
    def _load_if_exists(self, snapshot_id: str) -> Optional[CardSnapshot]:
        path = self.root / f"{snapshot_id}.json"
        if not path.exists():
            return None
        return self.load(snapshot_id)

    def load(self, snapshot_id: str) -> CardSnapshot:
        """按 ID 取快照。"""
        path = self.root / f"{snapshot_id}.json"
        if not path.exists():
            raise SnapshotNotFound(f"快照不存在：{path}")
        return CardSnapshot.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def iter_snapshots(self) -> Iterator[CardSnapshot]:
        """按落盘顺序遍历全部快照。"""
        if not self.root.exists():
            return
        for path in sorted(self.root.glob("snap-*.json")):
            yield CardSnapshot.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def find_by_event(self, event_id: str) -> Optional[CardSnapshot]:
        """按事件 ID 取**最近一份**快照。

        先走索引（快），索引缺失时回退扫目录（慢但准）。
        """
        if self.index_path.exists():
            for line in reversed(self.index_path.read_text(encoding="utf-8").splitlines()):
                if not line.strip():
                    continue
                entry = json.loads(line)
                if entry.get("event_id") == event_id:
                    try:
                        return self.load(entry["snapshot_id"])
                    except SnapshotNotFound:
                        continue
        for snapshot in self.iter_snapshots():
            if snapshot.event_id == event_id:
                return snapshot
        return None

    def find_by_card(self, card_id: str) -> Optional[CardSnapshot]:
        """按卡片 ID 取最近一份快照。"""
        for snapshot in reversed(list(self.iter_snapshots())):
            if snapshot.card_id == card_id:
                return snapshot
        return None

    # ---- 校验 ------------------------------------------------------------
    def verify(self, snapshot_id: str) -> SnapshotVerification:
        """校验快照完整性：内容指纹与各渠道正文哈希是否与落盘时一致。"""
        checked_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        try:
            snapshot = self.load(snapshot_id)
        except SnapshotNotFound as exc:
            return SnapshotVerification(snapshot_id, False, (str(exc),), checked_at)

        problems: list[str] = []
        identity = {
            "card_id": snapshot.card_id,
            "event_id": snapshot.event_id,
            "message_control_id": snapshot.message_control_id,
            "item_code": snapshot.item_code,
            "config_version": snapshot.config_version,
            "threshold_version": snapshot.threshold_version,
            "policy_version": snapshot.policy_version,
            "ruleset_snapshot_id": snapshot.ruleset_snapshot_id,
        }
        recomputed = _sha256(
            _canonical(
                {
                    "identity": identity,
                    "payload": dict(snapshot.payload),
                    "renderings": dict(snapshot.renderings),
                }
            )
        )
        if recomputed != snapshot.content_hash:
            problems.append("快照内容指纹与落盘时不一致（正文被改动过）")
        for channel, expected in snapshot.rendering_hashes.items():
            actual = _sha256(snapshot.renderings.get(channel, ""))
            if actual != expected:
                problems.append(f"{channel} 渠道正文哈希与落盘时不一致")
        missing = [channel for channel in CHANNELS if channel not in snapshot.renderings]
        if missing:
            problems.append("缺少渠道正文：" + "、".join(missing))
        return SnapshotVerification(snapshot_id, not problems, tuple(problems), checked_at)
