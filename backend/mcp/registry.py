"""工具注册白名单与启动期校验（T22 / PRD §5.3、§5.4.4、§5.4.8）。

- **白名单**：只有 PRD §5.3 目录内的工具名可注册，其余一律拒绝；
- **禁用能力**：通用 SQL / 任意 HTTP / 服务器命令 / 全量文件读取 / 密码查询，
  既禁止在工具声明中出现，也禁止出现在工具名中；
- **写工具**：必须携带 §5.3.1 矩阵（7 字段齐备），否则拒绝注册；
- 任何一条不满足即抛错（进程启动期即暴露，而不是运行期才发现）。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from mcp.approval_matrix import MATRIX_TOOL_NAMES, MATRIX_TOOL_RISK_LEVELS, matrix_tool_kind
from mcp.catalog import ALL_TOOL_SPECS, PRD_TOOL_CATALOG
from mcp.errors import (
    McpCapabilityForbiddenError,
    McpRegistrationRejectedError,
    McpToolNotImplementedError,
    McpToolNotRegisteredError,
)
from mcp.identity import McpContext, is_reserved_identity_param
from mcp.schema import is_well_formed_schema, schema_property_names
from mcp.spec import ToolSpec

# 受禁能力标签（PRD §5.4.4）：通用 SQL / 任意 HTTP / 服务器命令 / 全量文件读取 / 密码查询
FORBIDDEN_CAPABILITIES: frozenset[str] = frozenset(
    {
        "sql",
        "raw_sql",
        "query_builder",
        "database",
        "db",
        "repository",
        "orm",
        "http",
        "http_request",
        "fetch",
        "webhook",
        "shell",
        "exec",
        "subprocess",
        "command",
        "os_shell",
        "file_read",
        "fs",
        "full_file_read",
        "any_file_read",
        "secret_read",
        "credential_read",
        "password_query",
        "password_read",
        "user_password",
    }
)

# 工具名中不得出现的危险词（防止「伪工具」绕过白名单语义）
FORBIDDEN_NAME_FRAGMENTS: frozenset[str] = frozenset(
    {
        "sql", "exec", "shell", "command", "subprocess", "http", "fetch", "fs",
        "file", "secret", "password",
    }
)

# 执行器签名：受信上下文 + 净化后的业务参数 → 结构化结果
Handler = Callable[[McpContext, dict[str, Any]], dict[str, Any]]


class ToolRegistry:
    """工具注册表：白名单 + 元数据校验 + Handler 绑定。"""

    def __init__(self, *, catalog: frozenset[str] | None = None) -> None:
        self._catalog: frozenset[str] = PRD_TOOL_CATALOG if catalog is None else catalog
        self._specs: dict[str, ToolSpec] = {}
        self._handlers: dict[str, Handler] = {}

    # -------- 查询 --------

    @property
    def catalog(self) -> frozenset[str]:
        return self._catalog

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._specs))

    def specs(self) -> tuple[ToolSpec, ...]:
        return tuple(self._specs[name] for name in self.names())

    def write_tools(self) -> tuple[ToolSpec, ...]:
        return tuple(spec for spec in self.specs() if spec.write_matrix is not None)

    def get(self, tool_name: str) -> ToolSpec:
        spec = self._specs.get(tool_name)
        if spec is None:
            raise McpToolNotRegisteredError(tool_name)
        return spec

    def public_catalog(self) -> list[dict[str, Any]]:
        """对外工具目录（模型可见）：只暴露元数据，不含 Handler 与内部标识。"""
        return [spec.to_public_dict() for spec in self.specs()]

    # -------- 注册 --------

    def register(self, spec: ToolSpec) -> None:
        self._validate_spec(spec)
        self._specs[spec.name] = spec

    def _validate_spec(self, spec: ToolSpec) -> None:
        name = spec.name
        if name not in self._catalog:
            raise McpRegistrationRejectedError(name, "工具不在 PRD §5.3 白名单目录内")
        if any(fragment in name.lower() for fragment in FORBIDDEN_NAME_FRAGMENTS):
            raise McpRegistrationRejectedError(name, "工具名含受禁能力关键词")
        if name in self._specs:
            raise McpRegistrationRejectedError(name, "工具已注册，禁止覆盖")

        forbidden_caps = sorted(set(spec.capabilities) & FORBIDDEN_CAPABILITIES)
        if forbidden_caps:
            raise McpCapabilityForbiddenError(name, forbidden_caps)

        # Schema 必须 well-formed，且不得声明权限字段（模型不得指定 actor/role/region/...）
        if not is_well_formed_schema(spec.input_schema):
            raise McpRegistrationRejectedError(name, "输入 Schema 非法", {"schema": "input"})
        if not is_well_formed_schema(spec.output_schema):
            raise McpRegistrationRejectedError(name, "输出 Schema 非法", {"schema": "output"})
        reserved = [
            p
            for p in schema_property_names(spec.input_schema)
            if is_reserved_identity_param(p)
        ]
        if reserved:
            raise McpRegistrationRejectedError(
                name,
                "输入 Schema 声明了权限字段（只能由服务端 JWT/session 注入）",
                {"reserved_fields": sorted(reserved)},
            )

        if not spec.allowed_roles:
            raise McpRegistrationRejectedError(name, "未声明默认允许角色")

        matrix = spec.write_matrix
        if matrix is None:
            if spec.is_write:
                raise McpRegistrationRejectedError(name, "写工具必须声明审批矩阵（§5.3.1 七字段）")
            return

        # 写工具：矩阵完整性 + 与目录声明强一致
        matrix.validate(name)
        if name not in MATRIX_TOOL_NAMES:
            raise McpRegistrationRejectedError(name, "声明了审批矩阵但不在 §5.3.1 矩阵内")
        if spec.risk_level is not MATRIX_TOOL_RISK_LEVELS[name]:
            raise McpRegistrationRejectedError(
                name,
                "风险等级与 §5.3.1 矩阵不一致",
                {"declared": spec.risk_level.value,
                 "expected": MATRIX_TOOL_RISK_LEVELS[name].value},
            )
        if spec.kind is not matrix_tool_kind(name):
            raise McpRegistrationRejectedError(
                name,
                "工具种类与 §5.3.1 矩阵不一致",
                {"declared": spec.kind.value, "expected": matrix_tool_kind(name).value},
            )
        if frozenset(spec.allowed_roles) != frozenset(matrix.initiator):
            raise McpRegistrationRejectedError(
                name,
                "允许角色必须等于矩阵 initiator（不得用更宽松默认值覆盖）",
                {"declared": sorted(spec.allowed_roles),
                 "expected": sorted(matrix.initiator)},
            )

    # -------- Handler 绑定（T23/T24 适配器接入点）--------

    def bind_handler(self, tool_name: str, handler: Handler) -> None:
        spec = self.get(tool_name)  # 未注册工具直接拒绝
        self._handlers[spec.name] = handler

    def has_handler(self, tool_name: str) -> bool:
        return tool_name in self._handlers

    def handler_for(self, tool_name: str) -> Handler:
        spec = self.get(tool_name)
        handler = self._handlers.get(spec.name)
        if handler is None:
            raise McpToolNotImplementedError(spec.name)
        return handler


def build_default_registry() -> ToolRegistry:
    """构建默认注册表：PRD 白名单 + 全量工具声明；缺 Matrix 覆盖即启动失败。"""
    registry = ToolRegistry()
    for spec in ALL_TOOL_SPECS:
        registry.register(spec)

    missing = MATRIX_TOOL_NAMES - set(registry.names())
    if missing:
        raise McpRegistrationRejectedError(
            "*", "§5.3.1 审批矩阵存在未注册的工具", {"missing": sorted(missing)}
        )
    for spec in registry.write_tools():
        if spec.feature_flag is None:
            raise McpRegistrationRejectedError(spec.name, "写工具缺少功能开关声明")
    return registry


def assert_catalog_matches_prd(registry: ToolRegistry, catalog: Iterable[str]) -> None:
    """目录一致性断言：注册表工具集合必须与白名单目录完全一致。"""
    declared = set(catalog)
    registered = set(registry.names())
    if declared != registered:
        raise McpRegistrationRejectedError(
            "*",
            "工具目录与注册表不一致",
            {"missing": sorted(declared - registered), "extra": sorted(registered - declared)},
        )
