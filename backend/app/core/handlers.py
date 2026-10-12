"""全局异常处理器注册：统一错误体 {code,message,details,request_id}。

错误码到 HTTP 状态的映射：
- AuthError（认证域）        -> 401 UNAUTHORIZED
- PermissionDeniedError（权限）-> 403 FORBIDDEN
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

_INTERNAL_ERROR_CODE = "INTERNAL_ERROR"
_INTERNAL_ERROR_MESSAGE = "服务暂时不可用，请稍后重试或联系管理员"


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(PermissionDeniedError)
    async def permission_denied_handler(
        _: Request, exc: PermissionDeniedError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=403,
            content=error_response_body(exc.code, exc.message, exc.details),
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
