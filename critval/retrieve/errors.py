"""召回层共用异常。

单独一个模块，是为了 ``params`` 与 ``rerank`` 都能引用同一个
``ParamCheckError``，又不至于互相 import 形成环。
"""

from __future__ import annotations

from typing import Any

__all__ = ["ParamCheckError"]


class ParamCheckError(ValueError):
    """参数面板非法 —— 错误信息带**字段名**。

    ``field_name`` 是机器可读的字段名（``similarity_threshold`` /
    ``kb_ids`` / ``rerank_id`` …），调用方可以直接把它写进错误响应或审计，
    值班人看到的是"哪个旋钮配错了"，而不是一句"参数错误"。
    """

    def __init__(self, message: str, *, field_name: str = "") -> None:
        super().__init__(message)
        self.field_name = field_name

    def to_dict(self) -> dict[str, Any]:
        return {"field": self.field_name, "message": str(self)}
