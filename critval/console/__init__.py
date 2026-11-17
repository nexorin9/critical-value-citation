"""值班台：事件台账页、处置卡确认页与入站 HTTP 端点。

* ``critval/console/inbound.py`` —— 入站推送端点的**服务层**（鉴权 / 限流 /
  规范化 / 幂等），与 socket 无关，可直接单测；
* ``critval/console/app.py`` —— **服务承载**（标准库 ``http.server``）与
  路由骨架；
* ``critval/console/tokens.py`` —— **设计 token 单一真源**（色板 / 语义色 /
  栅格 / 字号），状态机的 13 个状态与语义色一一对应；
* ``critval/console/components.py`` —— **基础组件**（按钮四态 / 输入框 /
  表格三态 / 状态徽标 / 出处角标 / toast），服务端渲染，无前端构建链；
* ``critval/console/states_view.py`` —— **三态与写操作反馈**（加载 / 空 /
  错误三态各有独立模板 + 四类 toast + 错误码目录）；
* ``critval/console/templates/`` —— 页面骨架模板、三态模板与静态样式。
"""

from critval.console.inbound import (
    INBOUND_PATH,
    InboundRequest,
    InboundResponse,
    InboundService,
    LedgerInboundProcessor,
    RateLimiter,
    verify_bearer,
)
from critval.console.tokens import (
    SEMANTIC,
    STATUS_SEMANTIC,
    badge_class,
    css_variables,
    semantic_for_status,
)

__all__ = [
    "INBOUND_PATH",
    "InboundRequest",
    "InboundResponse",
    "InboundService",
    "LedgerInboundProcessor",
    "RateLimiter",
    "SEMANTIC",
    "STATUS_SEMANTIC",
    "badge_class",
    "css_variables",
    "semantic_for_status",
    "verify_bearer",
]
