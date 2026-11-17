"""HL7 层的两个原语：字节解码与患者标识脱敏。

这两个原语被 ``er7``（语法层）、``dialects``（方言层）与 ``normalize``
（规范化层）共同使用，所以单独放在一个没有上层依赖的模块里 —— 避免
``dialects`` 与 ``normalize`` 互相 import 形成环。

**``decode_frame``**：国内 LIS 常见 UTF-8 与 GB18030 两种编码，先试 UTF-8，
再退 GB18030，都不行才用 ``errors="replace"`` 并把编码名标成
``utf-8(replaced)``，让上层知道这条报文可能有乱码。

**``patient_reference``**：``PID-3`` 只允许落成**不可逆短哈希**
（``pt-<12 位>``）—— 事件之间要能关联到同一患者，但台账里不留可识别明细。
"""

from __future__ import annotations

import hashlib

__all__ = ["decode_frame", "patient_reference"]


def decode_frame(payload: bytes | str) -> tuple[str, str]:
    """解出报文文本，返回 ``(文本, 编码名)``。"""
    if isinstance(payload, str):
        return payload, "utf-8"
    for encoding in ("utf-8", "gb18030"):
        try:
            return payload.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    return payload.decode("utf-8", errors="replace"), "utf-8(replaced)"


def patient_reference(patient_id: str) -> str:
    """把 ``PID-3`` 转成不透明患者引用。

    只用于把同一患者的多条结果关联起来，**不落住院号 / 身份证明文**。
    """
    value = (patient_id or "").strip()
    if not value:
        return ""
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return f"pt-{digest[:12]}"
