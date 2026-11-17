#!/usr/bin/env python3
"""生成单测用的脱敏 ER7 帧（可重跑）。

三帧覆盖三条不同的入站路径：

* **正常帧** —— 字段齐备，血红蛋白 25 g/L 低于低危急值 30，应判危急；
* **缺字段帧** —— ``OBX-3`` 项目码与 ``OBX-14`` 观察时刻都缺，
  应当落 ``unparsed``（**不静默丢弃**）；
* **修正结果帧** —— ``OBR-25 = C``（修正），结果值 249.29 g/L 高于高危急值 200。

帧里所有标识符都是脱敏占位（``FIX-*``），不含任何真实患者信息；
段间分隔符是 ER7 的 ``\\r``，末尾不带 MLLP 组帧符（组帧在测试里现拼）。

    python3 tests/fixtures/build_hl7_fixtures.py
"""

from __future__ import annotations

from pathlib import Path

SEGMENT_SEPARATOR = "\r"

MSH = (
    "MSH|^~\\&|FIXTURE_LIS|FIXTURE_HOSP|CRITVAL|FIXTURE_HOSP|{stamp}"
    "||ORU^R01|{control_id}|P|2.5.1|||AL|NE|CHN"
)

PID = "PID|1||FIX-PAT-000001^^^^^FIXTURE_HOSP||||19700101|M"

PV1 = (
    "PV1|1|I|测试科室^测试科室^WARD|E|||测试科室^WARD||||||||||||"
    "FIXVISIT0001|||||||||||||||||||||||"
)

OBR_NORMAL = (
    "OBR|1|FIX-REQ-000001|FIX-ORD-000001|718-7^血红蛋白^718-7||{stamp}|{stamp}"
    "|||||||{stamp}|||||||||||F"
)

OBX_NORMAL = (
    "OBX|1|NM|718-7^血红蛋白^718-7|1|25|g/L|115-150|H|||F|||{stamp}"
)

# 缺字段帧：OBX-3 项目码空、OBX-14 观察时刻空。
OBR_MISSING = (
    "OBR|1|FIX-REQ-000002|FIX-ORD-000002|6690-2^白细胞计数^6690-2||{stamp}|{stamp}"
    "|||||||{stamp}|||||||||||F"
)

OBX_MISSING = "OBX|1|NM||1|2.76|10*9/L|3.5-9.5|L|||F|||"

OBR_CORRECTED = (
    "OBR|1|FIX-REQ-000003|FIX-ORD-000003|718-7^血红蛋白^718-7||{stamp}|{stamp}"
    "|||||||{stamp}|||||||||||C"
)

OBX_CORRECTED = (
    "OBX|1|NM|718-7^血红蛋白^718-7|1|249.29|g/L|115-150|HH|||F|||{stamp}"
)

FRAMES = {
    "脱敏样例ER7_正常.hl7": ("20260102083000", "FIXMSG000001", MSH, PID, PV1, OBR_NORMAL, OBX_NORMAL),
    "脱敏样例ER7_缺字段.hl7": ("20260102083500", "FIXMSG000002", MSH, PID, PV1, OBR_MISSING, OBX_MISSING),
    "脱敏样例ER7_修正结果.hl7": ("20260102084000", "FIXMSG000003", MSH, PID, PV1, OBR_CORRECTED, OBX_CORRECTED),
}


def build(dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    for name, (stamp, control_id, *segments) in FRAMES.items():
        rendered = [segment.format(stamp=stamp, control_id=control_id) for segment in segments]
        (dest.parent / name).write_text(SEGMENT_SEPARATOR.join(rendered), encoding="utf-8")
    return dest


if __name__ == "__main__":
    here = Path(__file__).resolve().parent
    for filename in FRAMES:
        target = here / filename
        build(target)
        print("已生成", target)
