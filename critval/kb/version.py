"""文件指纹、版本与生效期、解析报告。

三件事必须同时成立才算「材料被正确接收」：

1. **指纹**：同一份材料算出同一个 sha256，用于判���材料是否换过；
2. **版本 + 生效期**：判定时按事件观察时刻选生效版本，落在版本空档不取就近版本；
3. **解析报告**：未识别行 / 未识别列 / 字段缺失行都在报告里，
   **不静默跳过** —— 少读一行阈值表，等于少一条病人安全兜底。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional, Sequence

from critval.models import ThresholdRow

__all__ = [
    "EffectivePeriod",
    "MaterialVersion",
    "ParseReport",
    "UnrecognizedRow",
    "VERSION_ACTIVE",
    "VERSION_DRAFT",
    "VERSION_EXPIRED",
    "VERSION_GAP",
    "VersionSelection",
    "build_versions",
    "select_version",
    "sha256_bytes",
    "sha256_file",
]


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str | Path) -> str:
    """文件 sha256 指纹（分块读，大文件不吃内存）。"""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class EffectivePeriod:
    """版本生效期。``state`` 取 ``draft`` / ``active`` / ``expired`` / ``gap``。"""

    effective_from: Optional[date] = None
    effective_to: Optional[date] = None
    state: str = "active"

    @classmethod
    def classify(
        cls,
        effective_from: Optional[date],
        effective_to: Optional[date],
        at: Optional[date] = None,
    ) -> "EffectivePeriod":
        """按观察时刻 ``at`` 判断版本状态。"""
        moment = at or date.today()
        if effective_from is not None and moment < effective_from:
            return cls(effective_from, effective_to, "draft")
        if effective_to is not None and moment > effective_to:
            return cls(effective_from, effective_to, "expired")
        return cls(effective_from, effective_to, "active")

    def covers(self, at: date) -> bool:
        if self.effective_from is not None and at < self.effective_from:
            return False
        if self.effective_to is not None and at > self.effective_to:
            return False
        return True

    def to_dict(self) -> dict[str, Any]:
        return {
            "effective_from": self.effective_from.isoformat() if self.effective_from else None,
            "effective_to": self.effective_to.isoformat() if self.effective_to else None,
            "state": self.state,
        }


# ---------------------------------------------------------------------------
# 版本模型（任务 id 26）
# ---------------------------------------------------------------------------

#: 版本状态 —— 某一版在某个时刻的状态。
VERSION_DRAFT = "draft"  # 尚未生效
VERSION_ACTIVE = "active"  # 正在生效
VERSION_EXPIRED = "expired"  # 已失效
#: 「空档」不是某一版的状态，而是**选不出版本**的状态：观察时刻落在
#: 上一版失效之后、下一版生效之前。此时不取就近版本，转人工。
VERSION_GAP = "gap"


@dataclass(frozen=True)
class MaterialVersion:
    """材料的一个版本：版本号 + 生效期 + 指纹 + 覆盖的项目。

    同一项目可以**多版本共存**（旧版还没来得及撤、新版已经启用），
    院内按观察时刻选生效的那一版 —— 选不出来就不判定。
    """

    version_no: str
    effective_from: Optional[date] = None
    effective_to: Optional[date] = None
    source_name: str = ""
    source_sha256: str = ""
    item_codes: tuple[str, ...] = ()

    def covers(self, at: date) -> bool:
        """观察时刻是否落在这版的生效期内（含边界）。"""
        if self.effective_from is not None and at < self.effective_from:
            return False
        if self.effective_to is not None and at > self.effective_to:
            return False
        return True

    def state_at(self, at: date) -> str:
        """这版在给定时刻的状态：``draft`` / ``active`` / ``expired``。"""
        if self.effective_from is not None and at < self.effective_from:
            return VERSION_DRAFT
        if self.effective_to is not None and at > self.effective_to:
            return VERSION_EXPIRED
        return VERSION_ACTIVE

    @property
    def period_text(self) -> str:
        """生效期的人读描述。"""
        start = self.effective_from.isoformat() if self.effective_from else "未注明"
        end = self.effective_to.isoformat() if self.effective_to else "长期有效"
        return f"{start} ~ {end}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "version_no": self.version_no,
            "effective_from": self.effective_from.isoformat() if self.effective_from else None,
            "effective_to": self.effective_to.isoformat() if self.effective_to else None,
            "source_name": self.source_name,
            "source_sha256": self.source_sha256,
            "item_codes": list(self.item_codes),
            "period_text": self.period_text,
        }


@dataclass(frozen=True)
class VersionSelection:
    """按观察时刻选版本的结论。

    ``status`` 取值：

    * ``selected`` —— 命中唯一一个生效版本；
    * ``gap`` —— 落在两版之间的**版本空档**，不取就近版本；
    * ``conflict`` —— 同一时刻有多版同时生效（口径互斥）；
    * ``no_match`` —— 该项目没有可用版本（未收录 / 早于所有版本生效期）。

    后三种都必须转人工 —— 判不出危急值不等于「不危急」。
    """

    status: str
    version: Optional[MaterialVersion]
    reason: str
    candidates: tuple[MaterialVersion, ...] = ()

    @property
    def ok(self) -> bool:
        return self.status == "selected" and self.version is not None

    @property
    def needs_human(self) -> bool:
        """是否必须转人工。"""
        return not self.ok

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "version": self.version.version_no if self.version else "",
            "reason": self.reason,
            "candidates": [v.version_no for v in self.candidates],
        }


def build_versions(rows: Sequence[ThresholdRow]) -> tuple[MaterialVersion, ...]:
    """把阈值行按「版本号 + 生效期 + 指纹」聚成**版本**。

    同一项目的多行（按性别 / 年龄段分档）落在同一版里；
    改版后新旧两版**同时**存在，谁也不覆盖谁 —— 生效期择一交给
    ``select_version``。
    """
    buckets: dict[tuple[str, Optional[date], Optional[date], str], dict[str, Any]] = {}
    for row in rows:
        key = (
            row.version or "",
            row.effective_from,
            row.effective_to,
            row.citation.source_sha256 if row.citation else "",
        )
        bucket = buckets.setdefault(
            key,
            {
                "version_no": row.version or "",
                "effective_from": row.effective_from,
                "effective_to": row.effective_to,
                "source_name": row.citation.source_name if row.citation else "",
                "source_sha256": key[3],
                "item_codes": [],
            },
        )
        if row.item_code and row.item_code not in bucket["item_codes"]:
            bucket["item_codes"].append(row.item_code)

    versions = [
        MaterialVersion(
            version_no=bucket["version_no"],
            effective_from=bucket["effective_from"],
            effective_to=bucket["effective_to"],
            source_name=bucket["source_name"],
            source_sha256=bucket["source_sha256"],
            item_codes=tuple(bucket["item_codes"]),
        )
        for bucket in buckets.values()
    ]
    versions.sort(
        key=lambda v: (
            v.effective_from or date.min,
            v.effective_to or date.max,
            v.version_no,
        )
    )
    return tuple(versions)


def select_version(
    versions: Sequence[MaterialVersion],
    item_code: str,
    at: date,
) -> VersionSelection:
    """按观察时刻给项目选生效版本。

    口径：

    * 唯一一版覆盖 ``at`` → 选中；
    * 多版同时覆盖 → **口径互斥**，拒绝自动判定（转人工）；
    * 一版都不覆盖，但上一版已失效、下一版还没生效 → **版本空档**，
      不取就近版本，转人工；
    * 早于所有版本生效期 → 转人工。
    """
    candidates = tuple(v for v in versions if item_code in v.item_codes)
    if not candidates:
        return VersionSelection(
            status="no_match",
            version=None,
            reason=f"阈值表未收录项目 {item_code}，没有可用版本（转人工）",
        )

    covering = tuple(v for v in candidates if v.covers(at))
    if len(covering) == 1:
        version = covering[0]
        return VersionSelection(
            status="selected",
            version=version,
            reason=(
                f"项目 {item_code} 命中版本 {version.version_no or '无版本号'}"
                f"（生效期 {version.period_text}）"
            ),
            candidates=candidates,
        )
    if len(covering) > 1:
        detail = "；".join(
            f"{v.version_no or '无版本号'}（{v.period_text}）" for v in covering
        )
        return VersionSelection(
            status="conflict",
            version=None,
            reason=(
                f"观察时刻 {at.isoformat()} 同时落在 {len(covering)} 个版本生效期内，"
                f"口径互斥，拒绝自动判定（转人工）：{detail}"
            ),
            candidates=candidates,
        )

    # 一版都不覆盖 —— 分清「版本空档」与「早于所有版本」。
    ordered = sorted(
        candidates,
        key=lambda v: (v.effective_from or date.min, v.effective_to or date.max, v.version_no),
    )
    previous: Optional[MaterialVersion] = None
    for version in ordered:
        if version.effective_from is not None and version.effective_from <= at:
            previous = version
    if previous is not None and previous.effective_to is not None and at > previous.effective_to:
        following = next(
            (
                v
                for v in ordered
                if v.effective_from is not None and v.effective_from > previous.effective_to
            ),
            None,
        )
        if following is None:
            # 最后一版已失效、后面没有新版 —— 项目多半是**停用**了，
            # 不是「空档」：新版本压根没再收录这个项目。
            return VersionSelection(
                status="no_match",
                version=None,
                reason=(
                    f"项目 {item_code} 的最后一个版本（{previous.version_no or '无版本号'}）"
                    f"已于 {previous.effective_to.isoformat()} 失效，新版本未再收录该项目"
                    f"（项目已停用），不做判定（转人工）"
                ),
                candidates=candidates,
            )
        detail = f"{previous.version_no or '无版本号'} 于 {previous.effective_to.isoformat()} 失效"
        detail += f"，{following.version_no or '无版本号'} 于 {following.effective_from.isoformat()} 才生效"
        return VersionSelection(
            status=VERSION_GAP,
            version=None,
            reason=(
                f"观察时刻 {at.isoformat()} 落在版本空档（{detail}），"
                f"不取就近版本，转人工"
            ),
            candidates=candidates,
        )

    drafts = "、".join(v.version_no or "无版本号" for v in candidates)
    return VersionSelection(
        status="no_match",
        version=None,
        reason=(
            f"观察时刻 {at.isoformat()} 早于项目 {item_code} 的所有版本生效期"
            f"（现有版本：{drafts}），转人工"
        ),
        candidates=candidates,
    )


@dataclass(frozen=True)
class UnrecognizedRow:
    """一行没能解析成阈值行 —— 必须进报告待人工确认。"""

    row_no: int
    reason: str
    values: tuple[Any, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "row_no": self.row_no,
            "reason": self.reason,
            "values": [str(v) if v is not None else "" for v in self.values],
        }


@dataclass
class ParseReport:
    """一次材料解析的报告。

    ``recognized_rows`` / ``total_rows`` / ``unrecognized_rows`` /
    ``incomplete_rows`` 四个数一起看，才知道这份材料**是不是全被读进去了**。
    """

    source_name: str
    source_sha256: str = ""
    source_path: str = ""
    file_version: str = ""
    total_rows: int = 0
    recognized_rows: int = 0
    unrecognized_rows: list[UnrecognizedRow] = field(default_factory=list)
    incomplete_rows: list[dict[str, Any]] = field(default_factory=list)
    unrecognized_columns: list[str] = field(default_factory=list)
    #: 疑似合并单元格的行（项目码留空、同行其他字段有值）。
    merged_cell_rows: list[dict[str, Any]] = field(default_factory=list)
    #: 疑似多版本拼表的项目组。
    multi_version_groups: list[dict[str, Any]] = field(default_factory=list)
    #: 人工回填并**已生效**的覆盖（行号 → 字段 → 值）。
    applied_overrides: list[dict[str, Any]] = field(default_factory=list)
    #: 人工回填但**没生效**的覆盖（表里没有那一列 / 行号越界）。
    unapplied_overrides: list[dict[str, Any]] = field(default_factory=list)
    parsed_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))

    def add_unrecognized(self, row_no: int, reason: str, values: tuple[Any, ...] = ()) -> None:
        self.unrecognized_rows.append(UnrecognizedRow(row_no=row_no, reason=reason, values=values))

    def add_incomplete(self, row_no: int, reason: str, values: tuple[Any, ...] = ()) -> None:
        self.incomplete_rows.append(
            {"row_no": row_no, "reason": reason, "values": [str(v) if v is not None else "" for v in values]}
        )

    @property
    def ok(self) -> bool:
        """没有未识别行才算读全了。字段缺失行不算未识别，但会单独列出来。"""
        return not self.unrecognized_rows

    def summary(self) -> str:
        return (
            f"{self.source_name}：共 {self.total_rows} 行，识别 {self.recognized_rows} 行，"
            f"未识别 {len(self.unrecognized_rows)} 行，字段缺失 {len(self.incomplete_rows)} 行，"
            f"未识别列 {len(self.unrecognized_columns)} 个，"
            f"疑似合并单元格 {len(self.merged_cell_rows)} 行，"
            f"疑似多版本 {len(self.multi_version_groups)} 组"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_name": self.source_name,
            "source_sha256": self.source_sha256,
            "source_path": self.source_path,
            "file_version": self.file_version,
            "total_rows": self.total_rows,
            "recognized_rows": self.recognized_rows,
            "unrecognized_rows": [r.to_dict() for r in self.unrecognized_rows],
            "incomplete_rows": list(self.incomplete_rows),
            "unrecognized_columns": list(self.unrecognized_columns),
            "merged_cell_rows": list(self.merged_cell_rows),
            "multi_version_groups": list(self.multi_version_groups),
            "applied_overrides": list(self.applied_overrides),
            "unapplied_overrides": list(self.unapplied_overrides),
            "parsed_at": self.parsed_at,
            "ok": self.ok,
        }
