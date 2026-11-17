"""版本一致性闸门（任务 id 26）。

**一次处置只能引用同一套自洽的材料版本。** 危急值处置卡上同时写着
「阈值依据行（阈值表第几行）」与「通知对象 / 上报时限 / 记录去向
（制度第几条）」—— 这两处依据一旦来自改版前后的不同版本，值班人看到的
口径就自相矛盾：按旧阈值判的危急值，配的却是新版制度的时限。

闸门拦两种情况：

1. **同类材料混版** —— 一张卡里阈值行引了 ``示例 v1.0``、另一处又引了
   ``示例 v2.0``。这是实实在在的自相矛盾，**拒绝出卡**。
2. **跨版本绑定不符** —— 院内声明了「项目码 → 阈值表版本 + 制度版本」
   的绑定（任务 id 5 落表），卡片引用的版本对必须落在绑定对内，
   否则**拒绝出卡**，报 ``version_mismatch``。

**版本号字符串不跨类硬比**：阈值表 ``v2.0`` 与制度 ``v1.0`` 是两份独立
材料，各自编号合法 —— 没有绑定声明时，跨类版本号不同只标「需人工核对」
（``version_unverifiable``），不当成跨版本拦下；院内若确实用统一发布号
（两份材料同号），可用 ``strict_pair=True`` 打开硬比。

版本一致性对应源参考 ``agent/component/retrieval.py`` 里
``assert len(embd_nms) == 1, "Knowledge bases use different embedding models."``
的语义 —— 同一次处置只允许用同一套底座，改写成「只允许用同一套材料版本」。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

from critval.models import Citation

__all__ = [
    "VERSION_CONSISTENT",
    "VERSION_MISMATCH",
    "VERSION_UNVERIFIABLE",
    "VersionBinding",
    "VersionConsistency",
    "VersionMismatchError",
    "check_version_consistency",
    "collect_versions",
    "load_bindings",
]

#: 一致性结论码。
VERSION_CONSISTENT = "consistent"
VERSION_MISMATCH = "version_mismatch"
VERSION_UNVERIFIABLE = "version_unverifiable"

#: 出处类型 → 材料族。阈值行与制度条款是两类材料，各自独立编号。
_FAMILY_BY_KIND = {
    "threshold_row": "threshold",
    "policy_clause": "policy",
}


@dataclass(frozen=True)
class VersionBinding:
    """项目码 → （阈值表版本 + 制度版本）的绑定。

    绑定表由院内声明（任务 id 5 落表）：某项目当前生效的阈值表版本与
    制度版本必须配套使用，处置卡引用别的一对就是跨版本。
    """

    item_code: str
    threshold_version: str
    policy_version: str
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "item_code": self.item_code,
            "threshold_version": self.threshold_version,
            "policy_version": self.policy_version,
            "note": self.note,
        }


def load_bindings(path: str | Path) -> tuple[VersionBinding, ...]:
    """从 JSON 读版本绑定表。

    文件格式::

        {"bindings": [
          {"item_code": "718-7", "threshold_version": "示例 v1.0",
           "policy_version": "示例 v1.0", "note": "检验科 2026-01 确认"}
        ]}

    读不出来（文件缺失 / 格式错）返回空绑定 —— 绑定表是**可选**的加固项，
    读不到不该把整个服务拖垮，但要留痕（调用方自行记录告警）。
    """
    target = Path(path)
    if not target.exists():
        return ()
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ()
    items = payload.get("bindings") if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        return ()
    bindings: list[VersionBinding] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        item_code = str(item.get("item_code", "")).strip()
        if not item_code:
            continue
        bindings.append(
            VersionBinding(
                item_code=item_code,
                threshold_version=str(item.get("threshold_version", "")).strip(),
                policy_version=str(item.get("policy_version", "")).strip(),
                note=str(item.get("note", "")).strip(),
            )
        )
    return tuple(bindings)


@dataclass(frozen=True)
class VersionConsistency:
    """一次处置的版本一致性结论。"""

    ok: bool
    code: str
    reason: str
    threshold_version: str = ""
    policy_version: str = ""
    expected_threshold_version: str = ""
    expected_policy_version: str = ""
    #: 同一类材料里出现过的多个版本（混版时非空）。
    mixed_versions: tuple[str, ...] = ()
    #: 需要人工核对（版本号缺失或跨类版本号不同但未声明绑定）。
    needs_review: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "code": self.code,
            "reason": self.reason,
            "threshold_version": self.threshold_version,
            "policy_version": self.policy_version,
            "expected_threshold_version": self.expected_threshold_version,
            "expected_policy_version": self.expected_policy_version,
            "mixed_versions": list(self.mixed_versions),
            "needs_review": self.needs_review,
        }


class VersionMismatchError(RuntimeError):
    """版本不一致 —— 拒绝出卡。

    ``code`` 固定为 ``version_mismatch``，值班台与台账按它分流。
    """

    code = VERSION_MISMATCH

    def __init__(self, consistency: VersionConsistency) -> None:
        super().__init__(consistency.reason)
        self.consistency = consistency


def collect_versions(citations: Iterable[Citation]) -> dict[str, set[str]]:
    """按材料族收集出处上的版本号。"""
    families: dict[str, set[str]] = {}
    for citation in citations:
        version = (citation.file_version or "").strip()
        if not version:
            continue
        family = _FAMILY_BY_KIND.get(citation.kind, citation.kind or "other")
        families.setdefault(family, set()).add(version)
    return families


def check_version_consistency(
    citations: Sequence[Citation] = (),
    *,
    binding: Optional[VersionBinding] = None,
    strict_pair: bool = False,
) -> VersionConsistency:
    """核对一次处置引用的材料版本是否自洽。

    ``citations`` 是这次处置引用的全部出处（阈值行 + 制度条款 + 附加引用）。
    ``binding`` 是院内声明的版本绑定（有则按绑定核对）。
    ``strict_pair`` 为真时，跨类版本号不同也算不一致（统一发布号的院内用）。
    """
    families = collect_versions(citations)
    threshold_versions = families.get("threshold", set())
    policy_versions = families.get("policy", set())

    mixed = tuple(
        sorted(version for versions in families.values() if len(versions) > 1 for version in versions)
    )
    if mixed:
        return VersionConsistency(
            ok=False,
            code=VERSION_MISMATCH,
            reason=(
                f"同一次处置混用了同一类材料的多个版本（{'、'.join(mixed)}），"
                f"依据口径自相矛盾，拒绝出卡"
            ),
            threshold_version=next(iter(threshold_versions), ""),
            policy_version=next(iter(policy_versions), ""),
            mixed_versions=mixed,
        )

    threshold_version = next(iter(threshold_versions), "")
    policy_version = next(iter(policy_versions), "")

    if binding is not None:
        problems: list[str] = []
        if (
            threshold_version
            and binding.threshold_version
            and threshold_version != binding.threshold_version
        ):
            problems.append(
                f"阈值表版本 {threshold_version} ≠ 绑定版本 {binding.threshold_version}"
            )
        if policy_version and binding.policy_version and policy_version != binding.policy_version:
            problems.append(f"制度版本 {policy_version} ≠ 绑定版本 {binding.policy_version}")
        if problems:
            return VersionConsistency(
                ok=False,
                code=VERSION_MISMATCH,
                reason=(
                    f"跨版本绑定不一致（项目 {binding.item_code}）："
                    + "；".join(problems)
                    + "，拒绝出卡"
                ),
                threshold_version=threshold_version,
                policy_version=policy_version,
                expected_threshold_version=binding.threshold_version,
                expected_policy_version=binding.policy_version,
            )
        if not (threshold_version and policy_version):
            return VersionConsistency(
                ok=True,
                code=VERSION_UNVERIFIABLE,
                reason=(
                    f"材料缺少版本号（阈值表 {threshold_version or '未标注'} / "
                    f"制度 {policy_version or '未标注'}），无法与绑定 "
                    f"{binding.threshold_version} + {binding.policy_version} 核对，需人工核对"
                ),
                threshold_version=threshold_version,
                policy_version=policy_version,
                expected_threshold_version=binding.threshold_version,
                expected_policy_version=binding.policy_version,
                needs_review=True,
            )
        return VersionConsistency(
            ok=True,
            code=VERSION_CONSISTENT,
            reason=(
                f"版本落在绑定对内（阈值表 {threshold_version} + 制度 {policy_version}）"
            ),
            threshold_version=threshold_version,
            policy_version=policy_version,
            expected_threshold_version=binding.threshold_version,
            expected_policy_version=binding.policy_version,
        )

    if threshold_version and policy_version:
        if threshold_version == policy_version:
            return VersionConsistency(
                ok=True,
                code=VERSION_CONSISTENT,
                reason=f"阈值表与制度同为版本 {threshold_version}",
                threshold_version=threshold_version,
                policy_version=policy_version,
            )
        if strict_pair:
            return VersionConsistency(
                ok=False,
                code=VERSION_MISMATCH,
                reason=(
                    f"阈值表版本 {threshold_version} 与制度版本 {policy_version} 不一致，"
                    f"拒绝出卡"
                ),
                threshold_version=threshold_version,
                policy_version=policy_version,
            )
        return VersionConsistency(
            ok=True,
            code=VERSION_UNVERIFIABLE,
            reason=(
                f"阈值表版本 {threshold_version} 与制度版本 {policy_version} 不同"
                f"（未声明版本绑定），无法自动判定是否跨版本，需人工核对"
            ),
            threshold_version=threshold_version,
            policy_version=policy_version,
            needs_review=True,
        )

    return VersionConsistency(
        ok=True,
        code=VERSION_UNVERIFIABLE,
        reason=(
            f"材料缺少版本号（阈值表 {threshold_version or '未标注'} / "
            f"制度 {policy_version or '未标注'}），无法核对版本一致性，需人工核对"
        ),
        threshold_version=threshold_version,
        policy_version=policy_version,
        needs_review=True,
    )
