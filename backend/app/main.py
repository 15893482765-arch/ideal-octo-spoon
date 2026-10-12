"""应用入口：装配配置、日志、中间件、异常处理器与路由。

启动失败策略（T01 Q002）：必填配置缺失时在应用构造阶段抛 ValidationError，
进程拒绝启动，且任何输出不包含密钥明文。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.health import router as health_router
from app.core.config import get_settings
from app.core.handlers import register_exception_handlers
from app.core.logging import configure_logging
from app.core.middleware import RequestContextMiddleware


def create_app() -> FastAPI:
    settings = get_settings()  # 缺失必填配置时在此失败
    configure_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield

    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        lifespan=lifespan,
        docs_url="/docs",
        openapi_url="/openapi.json",
    )
    app.add_middleware(RequestContextMiddleware)
    app.include_router(health_router)
    register_exception_handlers(app)
    return app


app = create_app()
