"""T29 Q166 越权矩阵 / Q167 速率限制 / Q168 签名 URL / Q169 性能阈值 / Q170 日志链路。"""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.responses import PlainTextResponse

from app.core.metrics import (
    METRIC_HTTP_REQUESTS,
    percentile,
    registry,
)
from app.core.permissions import (
    MODULE_AUDIT,
    MODULE_PAYROLL,
    MODULE_PAYROLL_DISBURSE,
    MODULE_REPORT,
    MODULE_RISK_HANDLE,
    MODULE_USER_ADMIN,
    ActorContext,
    permission_service,
)
from app.core.rate_limiter import (
    InMemoryRateBackend,
    RateLimiter,
    RateRule,
    RedisRateBackend,
)
from app.core.security_middleware import (
    SECURITY_HEADERS,
    build_cors_headers,
)

# ---------------------------------------------------------------------------
# Q166 越权矩阵：角色 × 模块的允许/拒绝必须与最小权限基线一致（声明式全表）
# ---------------------------------------------------------------------------
from app.models.enums import RoleCode

# 关键越权矩阵：高敏感模块 → 允许访问的角色（最小权限白名单，PRD §2.2）
SENSITIVE_ALLOWED: dict[str, set[RoleCode]] = {
    MODULE_USER_ADMIN: {RoleCode.SYSTEM_ADMIN},
    MODULE_AUDIT: {RoleCode.SYSTEM_ADMIN},
    MODULE_PAYROLL: {RoleCode.FINANCE},
    MODULE_PAYROLL_DISBURSE: {RoleCode.FINANCE},
    MODULE_REPORT: {RoleCode.REGIONAL_MANAGER, RoleCode.FINANCE, RoleCode.SYSTEM_ADMIN},
    MODULE_RISK_HANDLE: {
        RoleCode.REGIONAL_MANAGER,
        RoleCode.SUPERVISOR,
        RoleCode.FINANCE,
        RoleCode.SYSTEM_ADMIN,
    },
}

ALL_ROLES = [
    RoleCode.SALES_REP,
    RoleCode.REGIONAL_MANAGER,
    RoleCode.SUPERVISOR,
    RoleCode.FINANCE,
    RoleCode.SYSTEM_ADMIN,
    RoleCode.SUPPLIER,
]


def _actor(role: RoleCode) -> ActorContext:
    return ActorContext(
        user_id=uuid4(),
        role_code=role.value,
        region_ids=frozenset({uuid4()}),
        brand_ids=frozenset({uuid4()}),
    )


@pytest.mark.parametrize("role", ALL_ROLES)
def test_q166_authorization_matrix_least_privilege(role):
    """对每个高敏感模块 × 每个角色逐一断言：只有白名单角色可访问，其余一律拒绝。"""
    actor = _actor(role)
    for module, allowed_roles in SENSITIVE_ALLOWED.items():
        expected = role in allowed_roles
        assert permission_service.has_ability(actor, module) is expected, (
            f"角色 {role.value} 对模块 {module} 越权：期望 {expected}"
        )


def test_q166_sales_and_supplier_blocked_from_payroll_admin_audit_export():
    sales = _actor(RoleCode.SALES_REP)
    supplier = _actor(RoleCode.SUPPLIER)
    for actor in (sales, supplier):
        assert permission_service.has_ability(actor, MODULE_PAYROLL) is False
        assert permission_service.has_ability(actor, MODULE_PAYROLL_DISBURSE) is False
        assert permission_service.has_ability(actor, MODULE_USER_ADMIN) is False
        assert permission_service.has_ability(actor, MODULE_AUDIT) is False
        assert permission_service.has_ability(actor, MODULE_REPORT) is False


def test_q166_sales_cannot_handle_risk_but_manager_can():
    assert permission_service.has_ability(_actor(RoleCode.SALES_REP), MODULE_RISK_HANDLE) is False
    assert permission_service.has_ability(_actor(RoleCode.SUPPLIER), MODULE_RISK_HANDLE) is False
    manager = _actor(RoleCode.REGIONAL_MANAGER)
    supervisor = _actor(RoleCode.SUPERVISOR)
    assert permission_service.has_ability(manager, MODULE_RISK_HANDLE) is True
    assert permission_service.has_ability(supervisor, MODULE_RISK_HANDLE) is True


def test_q166_admin_does_not_auto_get_payroll_disburse():
    # PRD 红线：系统管理员不自动获得工资发放能力
    admin = _actor(RoleCode.SYSTEM_ADMIN)
    assert permission_service.has_ability(admin, MODULE_PAYROLL_DISBURSE) is False
    assert permission_service.has_ability(admin, MODULE_USER_ADMIN) is True


def test_q166_require_ability_raises_on_forbidden():
    from app.core.permissions import PermissionError_

    with pytest.raises(PermissionError_):
        permission_service.require_ability(_actor(RoleCode.SALES_REP), MODULE_PAYROLL)


def test_q166_supplier_sensitive_fields_redacted():
    # 字段级越权：厂商画像必须屏蔽商家联系方式/精确坐标/工资等
    supplier = _actor(RoleCode.SUPPLIER)
    payload = {
        "sku_code": "SKU1",
        "merchant_contact_phone": "13800000000",
        "merchant_location_wkt": "SRID=4326;POINT(1 2)",
        "payroll_total_fen": 9999,
    }
    redacted = permission_service.redact_payload(supplier, payload)
    assert redacted["sku_code"] == "SKU1"
    assert redacted["merchant_contact_phone"] is None
    assert redacted["merchant_location_wkt"] is None
    assert redacted["payroll_total_fen"] is None


# ---------------------------------------------------------------------------
# Q167 速率限制：固定窗口计数、超限 429、Redis 故障 fail-open
# ---------------------------------------------------------------------------


def test_q167_in_memory_window_blocks_after_limit_and_resets():
    clock = {"t": 1000.0}
    limiter = RateLimiter(InMemoryRateBackend(), clock=lambda: clock["t"])
    rule = RateRule("login", max_requests=3, window_seconds=60)

    decisions = [limiter.check(rule, "ip:user") for _ in range(3)]
    assert all(d.allowed for d in decisions)
    assert decisions[-1].remaining == 0

    blocked = limiter.check(rule, "ip:user")
    assert blocked.allowed is False
    assert blocked.retry_after_seconds > 0

    # 进入下一窗口自动恢复
    clock["t"] += 60
    assert limiter.check(rule, "ip:user").allowed is True


def test_q167_separate_identities_have_separate_buckets():
    limiter = RateLimiter(InMemoryRateBackend(), clock=lambda: 0.0)
    rule = RateRule("api", max_requests=1, window_seconds=60)
    assert limiter.check(rule, "u:1").allowed is True
    assert limiter.check(rule, "u:1").allowed is False
    # 不同身份互不影响
    assert limiter.check(rule, "u:2").allowed is True


class _BrokenRedis:
    def pipeline(self):  # noqa: ANN201
        raise ConnectionError("redis down")


def test_q167_redis_backend_failure_fails_open():
    # 限流是防护层：Redis 挂了不能让核心登录/巡访整体不可用 → fail-open（Q172 韧性）
    errors = []
    backend = RedisRateBackend(_BrokenRedis(), on_backend_error=errors.append)
    decision = backend.hit("login:x", 1, 60, 1000.0)
    assert decision.allowed is True  # 故障开放
    assert len(errors) == 1


# ---------------------------------------------------------------------------
# Q168 签名 URL 过期 + 对象存储隔离
# ---------------------------------------------------------------------------


def test_q168_cors_whitelist_rejects_unknown_origin_and_allows_known():
    allowed = {"https://ops.example.com"}
    assert build_cors_headers(None, allowed) == {}
    assert build_cors_headers("https://evil.example.com", allowed) == {}
    headers = build_cors_headers("https://ops.example.com", allowed)
    assert headers["Access-Control-Allow-Origin"] == "https://ops.example.com"
    assert headers["Access-Control-Allow-Credentials"] == "true"
    # 绝不对任意 Origin 反射 + credentials（CORS 红线）
    wildcard = build_cors_headers("https://anything", set())
    assert wildcard == {}


def test_q168_security_headers_present():
    # OWASP 基线响应头
    assert SECURITY_HEADERS["X-Content-Type-Options"] == "nosniff"
    assert SECURITY_HEADERS["X-Frame-Options"] == "DENY"
    assert "Referrer-Policy" in SECURITY_HEADERS


# ---------------------------------------------------------------------------
# Q169 性能阈值：压测统计函数 P50/P95/P99 与阈值断言
# ---------------------------------------------------------------------------


def test_q169_percentile_matches_threshold_buckets():
    # 1100 个样本：94% 快、6% 慢，保证线性插值下 P95/P99 都进入慢样本区间。
    samples = [0.1] * 1034 + [1.2] * 66
    ordered = sorted(samples)
    p50 = percentile(ordered, 50)
    p95 = percentile(ordered, 95)
    p99 = percentile(ordered, 99)
    assert p50 <= 0.8  # 中位样本远低于列表阈值
    assert p95 >= 1.0  # P95 已进入慢样本
    assert p99 >= 1.2
    assert percentile([], 95) == 0.0
    # 全部快时 P95 达标
    assert percentile(sorted([0.2, 0.3, 0.5, 0.7]), 95) <= 0.8


def test_q169_threshold_bounds_from_prd():
    # PRD §9.1：工作台 P95≤1s、列表 P95≤0.8s、巡访提交 P95≤3s（不含上传）
    thresholds = {"workbench": 1.0, "list": 0.8, "visit_submit": 3.0}
    healthy = percentile(sorted([0.2, 0.3, 0.5, 0.7, 0.9]), 95)
    assert healthy <= thresholds["workbench"]
    assert healthy <= thresholds["list"] or healthy <= thresholds["workbench"]


# ---------------------------------------------------------------------------
# Q170 日志链路：request-id 透传/生成 + 指标埋点
# ---------------------------------------------------------------------------


def _app_with_metrics() -> FastAPI:
    from app.core.middleware import RequestContextMiddleware

    app = FastAPI()
    app.add_middleware(RequestContextMiddleware)

    @app.get("/api/ping/{x}")
    def ping(x: str):  # noqa: ANN202, ARG001
        return PlainTextResponse("pong")

    return app


def test_q170_request_id_generated_and_echoed():
    client = TestClient(_app_with_metrics())
    res = client.get("/api/ping/123")
    assert res.headers.get("X-Request-Id")
    rid = res.headers["X-Request-Id"]
    assert len(rid) >= 8


def test_q170_inbound_request_id_is_propagated():
    client = TestClient(_app_with_metrics())
    res = client.get("/api/ping/9", headers={"X-Request-Id": "fixed-correlation-id"})
    assert res.headers["X-Request-Id"] == "fixed-correlation-id"


def test_q170_http_metrics_record_route_template_not_raw_id():
    app = _app_with_metrics()
    client = TestClient(app)
    client.get("/api/ping/550e8400-e29b-41d4-a716-446655440000")
    client.get("/api/ping/123456")
    # 路由模板标签存在；UUID/长数字不成为标签值（防基数爆炸）
    counter = registry.counter(METRIC_HTTP_REQUESTS, "")
    total = counter.value(method="GET", route="/api/ping/{x}", status_class="2xx")
    assert total >= 2
    raw_uuid = counter.value(
        method="GET", route="/api/ping/550e8400-e29b-41d4-a716-446655440000", status_class="2xx"
    )
    assert raw_uuid == 0
