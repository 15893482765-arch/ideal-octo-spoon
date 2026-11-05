"""可观测性端点（T29 / PRD §9.4）。

GET /metrics：Prometheus 文本格式（0.0.4）。
- 默认仅在显式开启或非生产环境暴露；生产应由内网/采集器访问（运维网络策略保证）；
- 不携带任何业务数据与敏感标签；序列化异常返回 500，不影响其它路由。
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse

from app.core.config import get_settings
from app.core.errors import DomainError
from app.core.metrics import registry

router = APIRouter(tags=["observability"])


@router.get("/metrics", response_class=PlainTextResponse, include_in_schema=False)
def metrics() -> PlainTextResponse:
    # 生产默认关闭裸暴露，需由运维在受控网络显式打开（metrics_enabled=true）
    enabled = getattr(get_settings(), "metrics_enabled", True)
    if get_settings().environment == "production" and not enabled:
        raise DomainError(code="METRICS_DISABLED", message="指标端点未开启")
    body = registry.render_prometheus()
    return PlainTextResponse(
        body,
        media_type="text/plain; version=0.0.4; charset=utf-8",
        headers={"Cache-Control": "no-store"},
    )
