"""MCP 服务骨架（T22 / PRD §5.1–§5.4）。

这是一个**独立进程/包**，与 FastAPI 应用（`app/`）解耦：

- 只依赖 `app.core`（错误、权限上下文）与 `app.core.config`（特性开关）；
- **不得**导入 `app.repositories` / `app.db` / ORM 会话实现 / SQLAlchemy
  （PRD §9.2「MCP Adapter 仅调用受控 Service，不可直连数据库」、§5.4.4）；
- 工具元数据（Schema、允许角色、数据范围、字段权限、风险等级、确认策略）与
  写工具审批矩阵（PRD §5.3.1）在此声明；业务执行由 T23/T24 的适配器绑定
  Handler（调用既有 Service），本骨架不实现业务逻辑。

红线（PRD §5.4）：
1. 工具参数不得接受 `actor_id`/`role`/`region_id` 等权限字段——统一由服务端
   JWT/session 注入（`mcp.identity`）；
2. 写工具必须携带 `idempotency_key`；
3. 不得暴露通用 SQL / 任意 HTTP / 服务器命令 / 全量文件读取 / 密码查询
   （`mcp.registry.FORBIDDEN_CAPABILITIES`）；
4. 附件、备注、合同文本属不可信业务内容，不作为越权指令。
"""

from __future__ import annotations

from mcp.client import MockMcpClient
from mcp.draft_handlers import DRAFT_HANDLERS, bind_draft_handlers
from mcp.errors import (
    McpCapabilityForbiddenError,
    McpConfirmationConsumedError,
    McpConfirmationExpiredError,
    McpConfirmationInvalidError,
    McpConfirmationRequiredError,
    McpFeatureDisabledError,
    McpIdempotencyKeyRequiredError,
    McpMatrixIncompleteError,
    McpRegistrationRejectedError,
    McpRoleNotAllowedError,
    McpSchemaInvalidError,
    McpToolNotImplementedError,
    McpToolNotRegisteredError,
)
from mcp.gateway import McpGateway
from mcp.levels import ConfirmLevel, ToolKind
from mcp.read_handlers import READ_HANDLERS, bind_read_handlers
from mcp.registry import ToolRegistry, build_default_registry
from mcp.suggest_handlers import SUGGEST_HANDLERS, bind_suggest_handlers

__all__ = [
    "ConfirmLevel",
    "DRAFT_HANDLERS",
    "SUGGEST_HANDLERS",
    "McpCapabilityForbiddenError",
    "McpConfirmationConsumedError",
    "McpConfirmationExpiredError",
    "McpConfirmationInvalidError",
    "McpConfirmationRequiredError",
    "McpFeatureDisabledError",
    "McpGateway",
    "McpIdempotencyKeyRequiredError",
    "McpMatrixIncompleteError",
    "McpRegistrationRejectedError",
    "McpRoleNotAllowedError",
    "McpSchemaInvalidError",
    "McpToolNotImplementedError",
    "McpToolNotRegisteredError",
    "MockMcpClient",
    "READ_HANDLERS",
    "ToolKind",
    "ToolRegistry",
    "bind_draft_handlers",
    "bind_read_handlers",
    "bind_suggest_handlers",
    "build_default_registry",
]
