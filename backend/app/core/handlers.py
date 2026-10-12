"""全局异常处理器注册：统一错误体 {code,message,details,request_id}。"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.core.errors import DomainError, error_response_body

_INTERNAL_ERROR_CODE = "INTERNAL_ERROR"
_INTERNAL_ERROR_MESSAGE = "服务暂时不可用，请稍后重试或联系管理员"


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(DomainError)
    async def domain_error_handler(_: Request, exc: DomainError) -> JSONResponse:
        # 422 = HTTP_422_UNPROCESSABLE_ENTITY（新版更名为 _CONTENT，用字面量避免版本分叉）
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
