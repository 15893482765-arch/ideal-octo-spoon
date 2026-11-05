"""HTTP 中间件：request_id 生成/透传、访问日志、API 延迟与状态指标（T29 增强）。

指标安全：
- route 标签使用**路由模板**（/api/merchants/{id}），不把 UUID/业务编号当标签，避免基数爆炸；
- 指标记录是旁路，异常被吞掉（Q172），绝不影响业务响应。
"""

from __future__ import annotations

import re
import time

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from app.core.context import get_request_id, new_request_id, set_request_id
from app.core.logging import get_logger
from app.core.metrics import METRIC_HTTP_LATENCY, METRIC_HTTP_REQUESTS, registry

logger = get_logger("app.access")

REQUEST_ID_HEADER = "X-Request-Id"

# 把路径中的 UUID / 长数字段归一化为占位符，防止标签基数膨胀。
_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
_LONG_NUM_RE = re.compile(r"/\d{3,}(?=/|$)")


def normalize_route(request: Request) -> str:
    """优先用匹配到的路由模板；拿不到时对原始路径做 ID 归一化。"""
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    if path:
        return str(path)
    raw = request.url.path
    normalized = _UUID_RE.sub("{id}", raw)
    normalized = _LONG_NUM_RE.sub("/{id}", normalized)
    return normalized


def status_class(status_code: int) -> str:
    return f"{status_code // 100}xx"


class RequestContextMiddleware(BaseHTTPMiddleware):
    """为每个请求生成或沿用 request_id，写回响应头并记录访问日志与指标。"""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        incoming = request.headers.get(REQUEST_ID_HEADER)
        request_id = incoming or new_request_id()
        set_request_id(request_id)
        start = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
        except Exception:
            logger.exception(
                "request_failed",
                extra={"extra_fields": {"method": request.method, "path": request.url.path}},
            )
            raise
        finally:
            elapsed_ms = round((time.perf_counter() - start) * 1000, 2)
            route = normalize_route(request)
            logger.info(
                "request_completed",
                extra={
                    "extra_fields": {
                        "method": request.method,
                        "path": request.url.path,
                        "route": route,
                        "status": status_code,
                        "elapsed_ms": elapsed_ms,
                        "request_id": get_request_id(),
                    }
                },
            )
            _record_metrics(request, route, status_code, elapsed_ms / 1000.0)
        response.headers[REQUEST_ID_HEADER] = request_id
        return response


def _record_metrics(request: Request, route: str, status_code: int, elapsed_seconds: float) -> None:
    # 健康检查不进业务指标，避免噪声
    if request.url.path in {"/healthz", "/metrics"}:
        return
    try:
        registry.counter(METRIC_HTTP_REQUESTS, "").inc(
            method=request.method,
            route=route,
            status_class=status_class(status_code),
        )
        registry.histogram(METRIC_HTTP_LATENCY, "").observe(
            elapsed_seconds, method=request.method, route=route
        )
    except Exception:  # noqa: BLE001 - 观测旁路永不影响响应
        logger.debug("metrics_record_failed", exc_info=True)
