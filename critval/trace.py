"""环节轨迹：四个环节记录「当时生效的是哪份配置」。

判定 / 召回 / 推送 / 回写四个环节各自留一条轨迹，轨迹里带：

* ``config_version`` —— 该环节执行时生效的配置快照版本号；
* ``config_fingerprint`` —— 该配置快照的指纹（键值对 sha256）；
* ``ruleset_snapshot_id`` —— 该环节所用规则集快照 ID（判定 / 召回有，
  推送 / 回写沿用同一条事件的判定结果）。

为什么要这个：**配置会在运行期热载入**。一条事件从入站到回写要跨过几分钟，
中间若运维改了环境变量，事后再看这条事件时，光看「现在是第几版配置」
是答不出「当时用的是第几版」的。轨迹在**环节执行的那一刻**把版本号按下来，
事件落库后即可回答「这条判定 / 这条推送当时用的是哪份配置」。

轨迹内容只带版本号与指纹，**不带任何凭据**，也不带患者可识别明细。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from critval.config import Config

__all__ = [
    "STAGE_JUDGE",
    "STAGE_NOTIFY",
    "STAGE_RETRIEVE",
    "STAGE_WRITEBACK",
    "EventTrace",
    "StageTrace",
    "capture_stage",
]

#: 四个关键环节名。
STAGE_JUDGE = "judge"  # 判定
STAGE_RETRIEVE = "retrieve"  # 召回
STAGE_NOTIFY = "notify"  # 推送
STAGE_WRITEBACK = "writeback"  # 回写

STAGE_LABELS: dict[str, str] = {
    STAGE_JUDGE: "判定",
    STAGE_RETRIEVE: "召回",
    STAGE_NOTIFY: "推送",
    STAGE_WRITEBACK: "回写",
}


@dataclass(frozen=True)
class StageTrace:
    """一个环节的执行轨迹。"""

    stage: str
    config_version: int
    config_fingerprint: str
    ruleset_snapshot_id: str = ""
    at: str = ""
    detail: str = ""

    @property
    def stage_label(self) -> str:
        return STAGE_LABELS.get(self.stage, self.stage)

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "stage_label": self.stage_label,
            "config_version": self.config_version,
            "config_fingerprint": self.config_fingerprint,
            "ruleset_snapshot_id": self.ruleset_snapshot_id,
            "at": self.at,
            "detail": self.detail,
        }


@dataclass
class EventTrace:
    """一条事件上累积的环节轨迹。

    用法：处理事件时随环节推进逐条 ``record()``；事件落库时
    ``attach(record)`` 把最后一次的配置版本与规则快照 ID 写进台账记录。
    """

    event_id: str
    stages: list[StageTrace] = field(default_factory=list)

    def record(
        self,
        stage: str,
        *,
        ruleset_snapshot_id: str = "",
        detail: str = "",
    ) -> StageTrace:
        """记录一个环节 —— 版本号在此刻按下来，不在落库时回读。"""
        snapshot = Config.snapshot()
        trace = StageTrace(
            stage=stage,
            config_version=snapshot.version,
            config_fingerprint=snapshot.fingerprint,
            ruleset_snapshot_id=ruleset_snapshot_id,
            at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            detail=detail,
        )
        self.stages.append(trace)
        return trace

    def attach(self, record: Any) -> Any:
        """把最后一次环节的配置版本与规则快照 ID 写进台账记录。

        ``record`` 是 ``critval.models.LedgerRecord``（或任何带
        ``config_version`` / ``ruleset_snapshot_id`` 属性的对象）。
        """
        if not self.stages:
            return record
        last = self.stages[-1]
        if hasattr(record, "config_version"):
            record.config_version = last.config_version
        if last.ruleset_snapshot_id and hasattr(record, "ruleset_snapshot_id"):
            record.ruleset_snapshot_id = last.ruleset_snapshot_id
        return record

    def versions(self) -> dict[str, int]:
        """各环节生效的配置版本（用于复核时对照）。"""
        return {trace.stage: trace.config_version for trace in self.stages}

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "stages": [trace.to_dict() for trace in self.stages],
        }


def capture_stage(
    stage: str,
    *,
    ruleset_snapshot_id: str = "",
    detail: str = "",
) -> StageTrace:
    """记录单个环节（无归属事件的散装场景用）。"""
    snapshot = Config.snapshot()
    return StageTrace(
        stage=stage,
        config_version=snapshot.version,
        config_fingerprint=snapshot.fingerprint,
        ruleset_snapshot_id=ruleset_snapshot_id,
        at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        detail=detail,
    )
