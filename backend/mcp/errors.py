"""MCP 域错误码（T22）。

统一继承 `app.core.errors.DomainError`，保持 `{code, message, details}` 结构；
details 只放工具名/字段名/角色等非敏感信息，禁止放入参数原文、密钥或堆栈。
"""

from __future__ import annotations

from typing import Any

from app.core.errors import DomainError


class McpError(DomainError):
    """MCP 域错误基类。"""


class McpToolNotRegisteredError(McpError):
    """工具不在白名单/未注册（PRD §5.3、§5.4.4）。"""

    def __init__(self, tool_name: str) -> None:
        super().__init__(
            code="MCP_TOOL_NOT_REGISTERED",
            message="该工具不在受控白名单内，已拒绝调用",
            details={"tool": tool_name},
        )


class McpRegistrationRejectedError(McpError):
    """工具注册被拒（白名单外、Schema 含权限字段、声明了禁用能力）。"""

    def __init__(self, tool_name: str, reason: str, details: dict[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {"tool": tool_name, "reason": reason}
        if details:
            payload.update(details)
        super().__init__(
            code="MCP_TOOL_REGISTRATION_REJECTED",
            message=f"工具注册被拒：{reason}",
            details=payload,
        )


class McpMatrixIncompleteError(McpError):
    """写工具审批矩阵字段缺失/非法（PRD §5.4.8）。"""

    def __init__(self, tool_name: str, missing: list[str]) -> None:
        super().__init__(
            code="MCP_MATRIX_INCOMPLETE",
            message="写工具审批矩阵不完整，拒绝注册（缺字段即拒绝启动）",
            details={"tool": tool_name, "missing": sorted(missing)},
        )


class McpSelfApprovalForbiddenError(McpError):
    """关键流程默认禁止自审：存在 approver 却声明允许自审。"""

    def __init__(self, tool_name: str) -> None:
        super().__init__(
            code="MCP_SELF_APPROVAL_FORBIDDEN",
            message="关键流程默认禁止自审，禁止用更宽松默认值覆盖审批矩阵",
            details={"tool": tool_name},
        )


class McpCapabilityForbiddenError(McpError):
    """工具声明了禁用能力（SQL/HTTP/命令/文件/密码查询）。"""

    def __init__(self, tool_name: str, forbidden: list[str]) -> None:
        super().__init__(
            code="MCP_CAPABILITY_FORBIDDEN",
            message="工具声明了受禁能力（通用 SQL / 任意 HTTP / 服务器命令 / 文件读取 / 密码查询）",
            details={"tool": tool_name, "forbidden_capabilities": sorted(forbidden)},
        )


class McpSchemaInvalidError(McpError):
    """入参不符合工具声明的输入 Schema（或含权限字段）。"""

    def __init__(self, tool_name: str, errors: list[str]) -> None:
        super().__init__(
            code="MCP_SCHEMA_INVALID",
            message="工具入参不符合声明的 Schema",
            details={"tool": tool_name, "errors": errors[:20]},
        )


class McpIdempotencyKeyRequiredError(McpError):
    """写工具缺少幂等键（PRD §5.4.2）。"""

    def __init__(self, tool_name: str) -> None:
        super().__init__(
            code="MCP_IDEMPOTENCY_KEY_REQUIRED",
            message="写工具必须携带 idempotency_key，避免模型重试产生多张单据",
            details={"tool": tool_name},
        )


class McpRoleNotAllowedError(McpError):
    """当前角色不在工具默认允许角色内。"""

    def __init__(self, tool_name: str, role_code: str, allowed: list[str]) -> None:
        super().__init__(
            code="MCP_ROLE_NOT_ALLOWED",
            message="当前角色无权调用该工具",
            details={"tool": tool_name, "role": role_code, "allowed_roles": sorted(allowed)},
        )


class McpFeatureDisabledError(McpError):
    """工具对应功能开关关闭。"""

    def __init__(self, tool_name: str, feature_flag: str) -> None:
        super().__init__(
            code="MCP_FEATURE_DISABLED",
            message="该工具对应的功能开关已关闭",
            details={"tool": tool_name, "feature_flag": feature_flag},
        )


class McpToolNotImplementedError(McpError):
    """工具已注册元数据但尚未绑定执行器（Handler 由 T23/T24 适配器提供）。"""

    def __init__(self, tool_name: str) -> None:
        super().__init__(
            code="MCP_TOOL_NOT_IMPLEMENTED",
            message="该工具已注册但尚未绑定执行器（实现随 T23/T24 适配器接入）",
            details={"tool": tool_name},
        )


class McpIdentityUnavailableError(McpError):
    """未提供受信身份（actor 只能由服务端注入，不接受模型指定）。"""

    def __init__(self) -> None:
        super().__init__(
            code="MCP_IDENTITY_UNAVAILABLE",
            message="未提供受信身份上下文，仅可查询工具目录",
            details={},
        )


class McpDataScopeError(McpError):
    """工具声明要求正式口径，但上下文未显式提供 PRODUCTION（PRD R34）。"""

    def __init__(self, tool_name: str, scope: str | None) -> None:
        super().__init__(
            code="MCP_DATA_SCOPE_NOT_PRODUCTION",
            message="该工具只接受显式 PRODUCTION 数据域，缺省/ALL/DEMO 一律拒绝",
            details={"tool": tool_name, "scope": scope},
        )


class McpSessionUnavailableError(McpError):
    """受信上下文未携带数据库会话，无法执行只读查询（T23 适配器前置条件）。"""

    def __init__(self, tool_name: str) -> None:
        super().__init__(
            code="MCP_SESSION_UNAVAILABLE",
            message="受信上下文未注入数据库会话，无法执行只读查询",
            details={"tool": tool_name},
        )


class McpConfirmationRequiredError(McpError):
    """写工具首次调用只返回执行预览，必须经结构化确认 UI 二次提交（T24）。"""

    def __init__(self, tool_name: str) -> None:
        super().__init__(
            code="MCP_CONFIRMATION_REQUIRED",
            message="该写操作需要结构化确认：请在确认界面核对执行预览后确认，"
                    "自由文本“确认”无效",
            details={"tool": tool_name},
        )


class McpConfirmationInvalidError(McpError):
    """确认令牌缺失/非法/绑定要素不符（actor/session/tool/参数/幂等键）。"""

    def __init__(self, tool_name: str, reason_code: str, message: str) -> None:
        super().__init__(
            code="MCP_CONFIRMATION_INVALID",
            message=message,
            details={"tool": tool_name, "reason_code": reason_code},
        )


class McpConfirmationExpiredError(McpError):
    """确认令牌已过期，需重新生成执行预览。"""

    def __init__(self, tool_name: str) -> None:
        super().__init__(
            code="MCP_CONFIRMATION_EXPIRED",
            message="确认令牌已过期，请重新生成执行预览后再确认",
            details={"tool": tool_name},
        )


class McpConfirmationConsumedError(McpError):
    """确认令牌已被使用（一次性），重复确认拒绝。"""

    def __init__(self, tool_name: str) -> None:
        super().__init__(
            code="MCP_CONFIRMATION_CONSUMED",
            message="确认令牌只能使用一次，请重新生成执行预览",
            details={"tool": tool_name},
        )
