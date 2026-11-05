"""应用入口：装配配置、日志、中间件、异常处理器与路由。

启动失败策略（T01 Q002）：必填配置缺失时在应用构造阶段抛 ValidationError，
进程拒绝启动，且任何输出不包含密钥明文。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response

from app.api.activities import router as activities_router
from app.api.admin_audit import router as admin_audit_router
from app.api.admin_master_data import router as admin_master_data_router
from app.api.admin_users import router as admin_users_router
from app.api.auth import register_login_rate_handler
from app.api.auth import router as auth_router
from app.api.contracts import router as contracts_router
from app.api.finance import router as finance_router
from app.api.freezers import router as freezers_router
from app.api.health import router as health_router
from app.api.master_data import router as master_data_router
from app.api.merchants import router as merchants_router
from app.api.observability import router as observability_router
from app.api.payroll import router as payroll_router
from app.api.performance import router as performance_router
from app.api.procurement import router as procurement_router
from app.api.replenishment import router as replenishment_router
from app.api.reporting import router as reporting_router
from app.api.schedules import router as schedules_router
from app.api.supervision import router as supervision_router
from app.api.transfers import router as transfers_router
from app.api.v1.assistant import router as assistant_router
from app.api.v1.seed import router as seed_router
from app.api.visits import router as visits_router
from app.api.workbench import router as workbench_router
from app.core.config import get_settings
from app.core.handlers import register_exception_handlers
from app.core.logging import configure_logging
from app.core.middleware import RequestContextMiddleware
from app.core.rate_limit_middleware import ApiRateLimitMiddleware
from app.core.rate_limiter import rate_limiter
from app.core.security_middleware import SecurityHeadersMiddleware

logger = logging.getLogger(__name__)


def _warm_embedding_model() -> None:
    """后台线程预热本地 embedding 模型。

    首次加载（import torch + 权重）约 30s：不预热则首个知识库查询同步
    阻塞同样时长，体感等同服务卡死/检索不稳定。失败不致命——检索侧
    会自动降级为关键词匹配。
    """
    try:
        from app.services.ai_embedding import get_embedding_client

        client = get_embedding_client()
        if client is None:
            return
        client.embed_query("知识库预热")
        logger.info("embedding 模型预热完成")
    except Exception:
        logger.warning(
            "embedding 模型预热失败（首次检索将按需加载或降级关键词匹配）",
            exc_info=True,
        )


def _frontend_dist_dir() -> Path:
    """已构建的前端产物目录（仓库根/frontend/dist）。"""
    return Path(__file__).resolve().parents[2] / "frontend" / "dist"


def _mount_spa(app: FastAPI, settings: Any) -> None:
    """同源托管已构建前端，并为前端深链做 SPA 回退。

    解决两个真实故障（前端用相对路径 `/api` 调后端）：
    1. 前端由独立静态服务器提供而未转发 `/api` → 登录等所有接口 404；
    2. 静态服务器不做 SPA 回退 → 直接访问/刷新 `/workspace` 等深链 404
       （表现为"今日工作台没有页面"）。

    规则：
    - 已注册的后端路由（`/api/**`、`/docs`、`/openapi.json`、`/healthz` 等）优先匹配；
    - 其余 GET：命中 `dist` 内真实文件则返回该文件，否则回退 `index.html`；
    - 保留路径前缀（api/docs/redoc/openapi.json/healthz）不接管，仍返回 JSON 404，
      避免前端吞掉接口 404 语义；
    - `dist` 未构建或显式关闭时静默跳过，后端可独立运行。
    """
    dist = _frontend_dist_dir()
    index = dist / "index.html"
    if not settings.serve_frontend or settings.environment == "test" or not index.is_file():
        return
    root = dist.resolve()
    reserved = ("api/", "docs", "redoc", "openapi.json", "healthz")

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa_fallback(full_path: str) -> Response:
        if full_path.startswith(reserved):
            raise HTTPException(status_code=404, detail="Not Found")
        candidate = (root / full_path).resolve()
        if full_path and candidate.is_file() and candidate.is_relative_to(root):
            return FileResponse(candidate)
        return FileResponse(index)


def create_app() -> FastAPI:
    settings = get_settings()  # 缺失必填配置时在此失败
    configure_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        from app.db.session import session_scope
        from app.services.bootstrap import bootstrap_initial_admin
        from app.services.demo_account_seeder import seed_demo_accounts_from_settings

        # 首次启动：若配置了 INITIAL_ADMIN_BOOTSTRAP 且没有系统管理员，自动建号
        with session_scope() as s:
            bootstrap_initial_admin(s, settings)
            # 非生产演示环境：把配置的账号工作簿播种为可直接登录的账号（幂等、失败不阻塞）
            seed_demo_accounts_from_settings(s, settings)
        # 后台预热 embedding（test 环境跳过：不加载真模型、不拖慢测试）
        if settings.environment != "test" and settings.embedding_enabled:
            threading.Thread(
                target=_warm_embedding_model,
                daemon=True,
                name="embedding-warmup",
            ).start()
        yield

    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        lifespan=lifespan,
        docs_url="/docs",
        openapi_url="/openapi.json",
    )
    # 中间件执行顺序：后 add 的先执行（最外层）。安全头/CORS 最外，限流次之，request_id 在内。
    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(
        ApiRateLimitMiddleware,
        limiter=rate_limiter,
        enabled=settings.rate_limit_backend == "redis" or settings.environment == "production",
    )
    app.add_middleware(
        SecurityHeadersMiddleware, allowed_origins=settings.cors_origins_set
    )
    register_login_rate_handler(app)
    app.include_router(observability_router)
    app.include_router(health_router)
    app.include_router(auth_router)
    app.include_router(admin_users_router)
    app.include_router(admin_master_data_router)
    app.include_router(admin_audit_router)
    app.include_router(seed_router)
    app.include_router(master_data_router)
    app.include_router(merchants_router)
    app.include_router(contracts_router)
    app.include_router(freezers_router)
    app.include_router(visits_router)
    app.include_router(schedules_router)
    app.include_router(activities_router)
    app.include_router(replenishment_router)
    app.include_router(procurement_router)
    app.include_router(performance_router)
    app.include_router(payroll_router)
    app.include_router(finance_router)
    app.include_router(reporting_router)
    app.include_router(supervision_router)
    app.include_router(transfers_router)
    app.include_router(workbench_router)
    app.include_router(assistant_router)
    # 同源托管前端（放在最后：仅在无后端路由匹配时兜底，不影响 /api 与文档路由）
    _mount_spa(app, settings)
    register_exception_handlers(app)
    return app


app = create_app()
