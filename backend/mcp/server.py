"""独立 MCP 进程骨架（T22）。

独立于 FastAPI 应用运行（`python -m mcp.server`），通过行分隔 JSON
（NDJSON）在 stdin/stdout 上提供两个方法：

- `tools/list`：返回受控工具目录（元数据；模型可见）；
- `tools/call`：调用工具（须由**服务端身份提供方**注入受信上下文）。

本进程自身不连接数据库、不导入 Repository/ORM；业务执行由适配器绑定
Handler（T23/T24）。未配置身份提供方时，`tools/call` 一律拒绝
（`MCP_IDENTITY_UNAVAILABLE`）——模型输出不是授权。
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterable, Mapping
from typing import Any, TextIO

from app.core.errors import DomainError
from mcp.errors import McpIdentityUnavailableError
from mcp.gateway import McpGateway
from mcp.identity import McpContext
from mcp.registry import ToolRegistry, build_default_registry

# 身份提供方：由宿主（AiGateway / 网关进程）注入，签出受信 McpContext
IdentityProvider = Any  # Callable[[Mapping[str, Any]], McpContext]，避免运行时强约束


def _error_payload(exc: DomainError) -> dict[str, Any]:
    return {"ok": False, "error": {"code": exc.code, "message": exc.message,
                                   "details": exc.details}}


def handle_request(
    request: Mapping[str, Any],
    *,
    gateway: McpGateway,
    identity_provider: IdentityProvider | None,
) -> dict[str, Any]:
    """处理单条 NDJSON 请求（纯函数，便于契约测试）。"""
    method = request.get("method")
    if method == "tools/list":
        return {"ok": True, "tools": gateway.list_tools()}

    if method == "tools/call":
        if identity_provider is None:
            return _error_payload(McpIdentityUnavailableError())
        try:
            context: McpContext = identity_provider(request)
            result = gateway.execute(
                tool_name=str(request.get("tool", "")),
                arguments=dict(request.get("arguments") or {}),
                context=context,
                idempotency_key=request.get("idempotency_key"),
            )
            return {"ok": True, **result.to_dict()}
        except DomainError as exc:
            return _error_payload(exc)

    return {
        "ok": False,
        "error": {"code": "MCP_METHOD_UNKNOWN", "message": "不支持的方法",
                  "details": {"method": method}},
    }


def serve(
    *,
    registry: ToolRegistry,
    stdin: Iterable[str],
    stdout: TextIO,
    identity_provider: IdentityProvider | None = None,
) -> None:
    """行分隔 JSON 服务循环。"""
    gateway = McpGateway(registry)
    for raw_line in stdin:
        line = raw_line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            stdout.write(json.dumps(
                {"ok": False, "error": {"code": "MCP_REQUEST_INVALID",
                                        "message": "请求不是合法 JSON", "details": {}}},
                ensure_ascii=False) + "\n")
            stdout.flush()
            continue
        if not isinstance(request, Mapping):
            response = {"ok": False, "error": {"code": "MCP_REQUEST_INVALID",
                                               "message": "请求必须是 JSON 对象",
                                               "details": {}}}
        else:
            response = handle_request(
                request, gateway=gateway, identity_provider=identity_provider
            )
        stdout.write(json.dumps(response, ensure_ascii=False, default=str) + "\n")
        stdout.flush()


def main() -> int:  # pragma: no cover - 进程入口
    """进程入口：注册表启动期校验失败即非零退出（拒绝启动）。"""
    try:
        registry = build_default_registry()
    except DomainError as exc:
        sys.stderr.write(json.dumps(_error_payload(exc), ensure_ascii=False) + "\n")
        return 2
    serve(registry=registry, stdin=sys.stdin, stdout=sys.stdout,
          identity_provider=None)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
