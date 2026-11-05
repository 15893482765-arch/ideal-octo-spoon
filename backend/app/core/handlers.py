"""全局异常处理器注册：统一错误体 {code,message,details,request_id}。

错误码到 HTTP 状态的映射：
- AuthError（认证域）        -> 401 UNAUTHORIZED
- PermissionDeniedError（权限）-> 403 FORBIDDEN
- PermissionError_（权限服务内部异常）-> 403 FORBIDDEN
- ConflictError（幂等冲突）    -> 409 CONFLICT
- DomainError（业务）         -> 422 UNPROCESSABLE_ENTITY
- 其它未捕获                  -> 500 INTERNAL_ERROR
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.core.errors import (
    AuthError,
    ConflictError,
    DomainError,
    PermissionDeniedError,
    error_response_body,
)
from app.core.metrics import (
    METRIC_PERMISSION_DENIED,
    registry,
)
from app.core.permissions import PermissionError_

_INTERNAL_ERROR_CODE = "INTERNAL_ERROR"
_INTERNAL_ERROR_MESSAGE = "服务暂时不可用，请稍后重试或联系管理员"
_PERMISSION_DENIED_CODE = "PERMISSION_DENIED"
_PERMISSION_DENIED_MESSAGE = "当前角色无权访问该接口"


def _actor_role(request: Request) -> str:
    """从请求上下文取角色码（仅用于监控维度，不含用户标识）。"""
    actor = getattr(request.scope.get("state"), "actor", None)
    return str(getattr(actor, "role_code", "") or "") if actor is not None else ""


def _record_permission_denied(role: str, module: str) -> None:
    try:
        registry.counter(METRIC_PERMISSION_DENIED, "").inc(role=role, module=module)
    except Exception:  # noqa: BLE001 - 观测旁路不影响响应
        pass


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(PermissionDeniedError)
    async def permission_denied_handler(
        request: Request, exc: PermissionDeniedError
    ) -> JSONResponse:
        # 权限拒绝监控（PRD §9.4）：只记角色/模块，不记用户标识与参数
        role = _actor_role(request)
        module = str((exc.details or {}).get("module", exc.code) or exc.code)
        _record_permission_denied(role, module)
        return JSONResponse(
            status_code=403,
            content=error_response_body(exc.code, exc.message, exc.details),
        )

    @app.exception_handler(PermissionError_)
    async def permission_service_error_handler(
        request: Request, exc: PermissionError_
    ) -> JSONResponse:
        """服务层能力校验异常（PermissionService.require_ability）。

        该异常不带 code/details，若无人接管会落到 500 分支：未挂 router 级能力依赖的
        模块（如督导、补货）会因此把「无权限」暴露成服务端错误。这里统一映射为 403，
        与 deps.require_ability 的依赖工厂保持同一文案与错误码，语义上仍是不越权。
        """
        role = _actor_role(request)
        _record_permission_denied(role, "unknown")
        return JSONResponse(
            status_code=403,
            content=error_response_body(
                _PERMISSION_DENIED_CODE,
                _PERMISSION_DENIED_MESSAGE,
                {"role": role} if role else None,
            ),
        )

    @app.exception_handler(ConflictError)
    async def conflict_error_handler(_: Request, exc: ConflictError) -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content=error_response_body(exc.code, exc.message, exc.details),
        )

    @app.exception_handler(AuthError)
    async def auth_error_handler(_: Request, exc: AuthError) -> JSONResponse:
        return JSONResponse(
            status_code=401,
            content=error_response_body(exc.code, exc.message, exc.details),
        )

    @app.exception_handler(DomainError)
    async def domain_error_handler(_: Request, exc: DomainError) -> JSONResponse:
        # 业务领域错误默认 422（"语义上不可处理"而非 400 参数格式错误）
        return JSONResponse(
            status_code=422,
            content=error_response_body(exc.code, exc.message, exc.details),
        )

    @app.exception_handler(Exception)
    async def unhandled_error_handler(_: Request, __: Exception) -> JSONResponse:
        # 不向客户端暴露堆栈/SQL/密钥；细节只进服务端日志
        return JSONResponse(
            status_code=500,
            content=error_response_body(_INTERNAL_ERROR_CODE, _INTERNAL_ERROR_MESSAGE),
        )
