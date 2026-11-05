"""MCP 工具权限网关（T22 / PRD §5.1–§5.4）。

调用顺序（任一不满足即拒绝，且**模型输出从不构成授权**）：

1. 白名单：工具未注册 → `MCP_TOOL_NOT_REGISTERED`；
2. 功能开关：关闭 → `MCP_FEATURE_DISABLED`；
3. 角色：当前 actor.role_code 不在允许角色内 → `MCP_ROLE_NOT_ALLOWED`；
4. 数据范围：声明要求的工具必须显式 PRODUCTION → `MCP_DATA_SCOPE_NOT_PRODUCTION`；
5. 参数净化：剥离模型自带的 actor/role/region/brand/dataset 等权限字段（忽略而非采信）；
6. Schema 校验：不符 → `MCP_SCHEMA_INVALID`；
7. 幂等：写工具缺 `idempotency_key` → `MCP_IDEMPOTENCY_KEY_REQUIRED`；
8. 执行器：未绑定 → `MCP_TOOL_NOT_IMPLEMENTED`（Handler 由 T23/T24 适配器绑定）。

网关自身**不访问数据库**：把受信上下文与净化后的业务参数交给 Handler。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.core.config import Settings
from mcp.errors import (
    McpDataScopeError,
    McpFeatureDisabledError,
    McpIdempotencyKeyRequiredError,
    McpRoleNotAllowedError,
    McpSchemaInvalidError,
)
from mcp.feature_flags import is_enabled
from mcp.identity import PRODUCTION_SCOPE, McpContext, sanitize_arguments
from mcp.registry import ToolRegistry
from mcp.schema import validate as validate_schema
from mcp.spec import ToolSpec
from mcp.write_flow import execute_write


@dataclass(frozen=True)
class ToolCallResult:
    """工具调用结果封装（不直接暴露 ORM 对象）。"""

    tool: str
    result: dict[str, Any] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"tool": self.tool, "status": "OK", "result": self.result, "meta": self.meta}


class McpGateway:
    """工具权限网关：鉴权、参数净化、Schema 校验、幂等门禁与执行。"""

    def __init__(self, registry: ToolRegistry, *, settings: Settings | None = None) -> None:
        self.registry = registry
        self._settings = settings

    # -------- 目录 --------

    def list_tools(self) -> list[dict[str, Any]]:
        return self.registry.public_catalog()

    # -------- 执行 --------

    def execute(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        context: McpContext,
        idempotency_key: str | None = None,
    ) -> ToolCallResult:
        spec = self.registry.get(tool_name)
        self._assert_feature_enabled(spec)
        self._assert_role_allowed(spec, context)
        self._assert_data_scope(spec, context)

        sanitized = sanitize_arguments(arguments)
        schema_errors = validate_schema(sanitized.business, spec.input_schema)
        if schema_errors:
            raise McpSchemaInvalidError(spec.name, schema_errors)

        if spec.requires_idempotency_key and not (idempotency_key or "").strip():
            raise McpIdempotencyKeyRequiredError(spec.name)

        # 审批矩阵内的写/草稿工具：两阶段受控写入（执行预览 → 结构化确认令牌 →
        # 幂等执行）。矩阵外的 A1「建议/组稿」工具（write_matrix 为空）不产生
        # 业务后果，直接交给草稿 Handler 落独立暂存表，不要求确认令牌。
        if spec.write_matrix is not None:
            raw_result = execute_write(
                spec=spec,
                context=context,
                business=sanitized.business,
                confirmation=sanitized.confirmation,
                idempotency_key=(idempotency_key or "").strip(),
            )
            return ToolCallResult(
                tool=spec.name,
                result=dict(raw_result),
                meta=self._build_meta(spec, context, idempotency_key, sanitized),
            )

        handler = self.registry.handler_for(spec.name)
        raw_result = handler(context, sanitized.business)

        meta = self._build_meta(spec, context, idempotency_key, sanitized)
        return ToolCallResult(tool=spec.name, result=dict(raw_result), meta=meta)

    # -------- 元数据 --------

    @staticmethod
    def _build_meta(spec: ToolSpec, context: McpContext, idempotency_key: str | None,
                    sanitized) -> dict[str, Any]:
        meta: dict[str, Any] = {
            "tool": spec.name,
            "risk_level": spec.risk_level.value,
            "actor_user_id": str(context.user_id),
            "actor_role": context.role_code,
            "session_id": context.session_id,
            "request_id": context.request_id,
            "ignored_identity_params": list(sanitized.ignored_identity_params),
            "requires_idempotency_key": spec.requires_idempotency_key,
            "idempotency_key": idempotency_key,
            "confirmed": context.confirmed,
        }
        if spec.requires_data_scope:
            meta["data_scope"] = PRODUCTION_SCOPE
        if spec.write_matrix is not None:
            meta["executor"] = spec.write_matrix.executor
            meta["approver_roles"] = sorted(spec.write_matrix.approver_roles)
            meta["final_state"] = list(spec.write_matrix.final_state)
            meta["feature_flag"] = spec.write_matrix.feature_flag
        return meta

    # -------- 门禁 --------

    def _assert_feature_enabled(self, spec: ToolSpec) -> None:
        flag = spec.feature_flag
        if flag is None:
            return
        if not is_enabled(flag, settings=self._settings):
            raise McpFeatureDisabledError(spec.name, flag)

    @staticmethod
    def _assert_role_allowed(spec: ToolSpec, context: McpContext) -> None:
        if context.role_code not in spec.allowed_roles:
            raise McpRoleNotAllowedError(
                spec.name, context.role_code, sorted(spec.allowed_roles)
            )

    @staticmethod
    def _assert_data_scope(spec: ToolSpec, context: McpContext) -> None:
        if not spec.requires_data_scope:
            return
        if (context.data_scope or "").upper() != PRODUCTION_SCOPE:
            raise McpDataScopeError(spec.name, context.data_scope)
