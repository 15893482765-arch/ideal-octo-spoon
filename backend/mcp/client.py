"""Mock MCP 客户端（T22 / PRD §5.4「建 mock MCP client 与契约测试」）。

在进程内直接驱动 `McpGateway`，用于：

- 契约测试：验证工具目录、角色门禁、Schema、幂等门禁与参数净化；
- 开发联调：不依赖真实模型与外部 MCP 传输层。

客户端只能传**业务参数**：身份/权限字段即便传入也会被网关忽略
（`q123`），客户端无法伪造 actor/role/region。
"""

from __future__ import annotations

from typing import Any

from mcp.gateway import McpGateway
from mcp.identity import McpContext


class MockMcpClient:
    """进程内 mock 客户端。"""

    def __init__(self, gateway: McpGateway, context: McpContext) -> None:
        self.gateway = gateway
        self.context = context
        self.calls: list[dict[str, Any]] = []

    # -------- 目录 --------

    def list_tools(self) -> list[dict[str, Any]]:
        return self.gateway.list_tools()

    def tool_names(self) -> list[str]:
        return [tool["name"] for tool in self.list_tools()]

    def tool(self, tool_name: str) -> dict[str, Any] | None:
        for tool in self.list_tools():
            if tool["name"] == tool_name:
                return tool
        return None

    # -------- 调用 --------

    def call(
        self,
        tool_name: str,
        *,
        idempotency_key: str | None = None,
        **arguments: Any,
    ) -> dict[str, Any]:
        """调用工具（关键字形式传业务参数）。"""
        return self.call_with(
            tool_name, arguments=arguments, idempotency_key=idempotency_key
        )

    def call_with(
        self,
        tool_name: str,
        *,
        arguments: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        payload = dict(arguments or {})
        self.calls.append(
            {"tool": tool_name, "arguments": payload, "idempotency_key": idempotency_key}
        )
        result = self.gateway.execute(
            tool_name=tool_name,
            arguments=payload,
            context=self.context,
            idempotency_key=idempotency_key,
        )
        return result.to_dict()
