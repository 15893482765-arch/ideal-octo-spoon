"""安全响应头与 CORS 基线中间件（T29 / PRD §9.4 OWASP 基础防护）。

- 统一注入安全响应头（不泄漏服务端框架、限制 iframe、HSTS 仅在 https 下生效）；
- CORS 走**白名单**（来自配置，逗号分隔），默认不放行任意 Origin，也不放行凭据通配；
- 该中间件不做业务鉴权（鉴权仍在路由依赖），只负责传输层基线。
"""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "X-XSS-Protection": "0",  # 现代浏览器建议关闭旧过滤器，靠 CSP
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-site",
}


def build_cors_headers(origin: str | None, allowed_origins: set[str]) -> dict[str, str]:
    """根据请求 Origin 计算 CORS 响应头；不在白名单则返回空（浏览器会拦截）。"""
    if not origin or not allowed_origins or origin not in allowed_origins:
        return {}
    return {
        "Access-Control-Allow-Origin": origin,
        "Vary": "Origin",
        "Access-Control-Allow-Credentials": "true",
        "Access-Control-Allow-Methods": "GET,POST,PATCH,PUT,DELETE,OPTIONS",
        "Access-Control-Allow-Headers": "Authorization,Content-Type,Idempotency-Key,X-Request-Id",
        "Access-Control-Max-Age": "600",
    }


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp, *, allowed_origins: set[str] | None = None) -> None:
        super().__init__(app)
        self._allowed_origins = allowed_origins or set()

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.method == "OPTIONS":
            # 预检请求：不进业务，直接按白名单回应
            cors = build_cors_headers(request.headers.get("origin"), self._allowed_origins)
            preflight = Response(status_code=204, headers=cors)
            return _with_security_headers(preflight, request.url.is_secure)

        response = await call_next(request)
        for key, value in SECURITY_HEADERS.items():
            response.headers.setdefault(key, value)
        if request.url.is_secure:
            # HSTS 只在 HTTPS 下声明，max-age 至少半年；preload 由运维决定
            response.headers.setdefault("Strict-Transport-Security", "max-age=15768000")
        for key, value in build_cors_headers(
            request.headers.get("origin"), self._allowed_origins
        ).items():
            response.headers.append(key, value)
        return response


def _with_security_headers(response: Response, secure: bool) -> Response:
    for key, value in SECURITY_HEADERS.items():
        response.headers.setdefault(key, value)
    if secure:
        response.headers.setdefault("Strict-Transport-Security", "max-age=15768000")
    return response
