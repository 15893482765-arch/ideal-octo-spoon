"""统一错误结构：{code, message, details, request_id}。

PRD §9.2：业务错误抛领域异常，由全局异常处理器转换；
不得把堆栈、SQL、密钥返回前端。
"""

from __future__ import annotations

from typing import Any

from app.core.context import get_request_id


class DomainError(Exception):
    """业务领域异常基类。

    code: 机器可读错误码，如 MERCHANT_NOT_FOUND
    message: 面向用户的中文说明，以业务后果解释状态（PRD §9.4 文案要求）
    details: 结构化补充信息，禁止包含密钥/堆栈/SQL
    """

    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


class ConfigurationError(DomainError):
    """配置缺失/非法时启动失败的领域异常。"""


class AuthError(DomainError):
    """认证域错误基类（登录失败、未激活、锁定、凭据过期等）。"""


class InvalidCredentialsError(AuthError):
    """用户名或密码错误。"""


class AccountLockedError(AuthError):
    """账号被锁定或待激活，登录被拒。"""


class ActivationRequiredError(AuthError):
    """账号尚未激活或一次性凭据已消费。"""


class PermissionDeniedError(DomainError):
    """权限不足（行级范围或能力权限）。"""


class ConflictError(DomainError):
    """资源冲突：幂等同键异参、并发占位等（HTTP 409）。"""


class FileAccessError(DomainError):
    """文件域错误：类型/大小不合规、越权访问、签名失效、隔离中。"""


def error_response_body(
    code: str, message: str, details: dict[str, Any] | None = None
) -> dict[str, Any]:
    """构造统一错误响应体。"""
    return {
        "code": code,
        "message": message,
        "details": details or {},
        "request_id": get_request_id(),
    }
