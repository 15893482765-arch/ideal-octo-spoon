"""通用 API 速率限制中间件（T29 / PRD §9.4）。

- 仅作用于 /api 路径；健康检查与 /metrics 不限流；
- 身份键：已认证请求用 JWT 主题（sub，服务端解析，不信客户端传参），否则用对端 IP；
- 超窗返回 429 + Retry-After；被限流计入 app_http_rate_limited_total；
- 默认关闭（`api_rate_limit_enabled=False`），生产在配置中显式打开，避免开发/测试互相干扰。
"""

from __future__ import annotations

from collections.abc import Callable

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

from app.core.config import get_settings
from app.core.errors import error_response_body
from app.core.metrics import METRIC_RATE_LIMITED, registry
from app.core.rate_limiter import RateLimiter, RateRule

API_RULE_NAME = "api.global"


class ApiRateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(
        self,
        app: ASGIApp,
        limiter: RateLimiter,
        *,
        enabled: bool | None = None,
        identity_resolver: Callable[[Request], str] | None = None,
    ) -> None:
        super().__init__(app)
        self._limiter = limiter
        self._enabled = enabled
        self._identity_resolver = identity_resolver

    def _is_enabled(self) -> bool:
        if self._enabled is not None:
            return self._enabled
        return bool(get_settings().environment == "production")

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        path = request.url.path
        if (
            not self._is_enabled()
            or not path.startswith("/api")
            or path in {"/healthz", "/metrics"}
        ):
            return await call_next(request)

        settings = get_settings()
        identity = (
            self._identity_resolver(request)
            if callable(self._identity_resolver)
            else _default_identity(request)
        )
        decision = self._limiter.check(
            RateRule(
                API_RULE_NAME,
                settings.rate_limit_api_max,
                settings.rate_limit_api_window_seconds,
            ),
            str(identity),
        )
        if not decision.allowed:
            registry.counter(METRIC_RATE_LIMITED, "").inc(bucket=API_RULE_NAME)
            return JSONResponse(
                status_code=429,
                headers={"Retry-After": str(decision.retry_after_seconds)},
                content=error_response_body(
                    "RATE_LIMITED",
                    "请求过于频繁，请稍后再试",
                    {"retry_after_seconds": decision.retry_after_seconds},
                ),
            )
        return await call_next(request)


def _default_identity(request: Request) -> str:
    # 已认证：取 JWT 的 sub（服务端验签后的载荷）；未认证：对端 IP。
    user = getattr(request.scope.get("state"), "user", None) if request.scope.get("state") else None
    sub = None
    try:
        sub = getattr(user, "id", None)
    except Exception:  # noqa: BLE001
        sub = None
    if sub:
        return f"u:{sub}"
    return f"ip:{request.client.host if request.client else 'unknown'}"
