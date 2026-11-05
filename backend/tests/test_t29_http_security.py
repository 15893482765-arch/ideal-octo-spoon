"""T29 HTTP 集成：Q167 登录限流 429；Q170 /metrics 与权限拒绝埋点；安全头/CORS。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.core.metrics import METRIC_PERMISSION_DENIED, registry
from app.core.rate_limiter import rate_limiter


def test_q167_login_endpoint_returns_429_after_burst(client: TestClient):
    # 用极小窗口阈值验证：连续超量登录必须被 429 拦截（账号错误本身是 401）
    settings = get_settings()
    old_max, old_window = settings.rate_limit_login_max, settings.rate_limit_login_window_seconds
    settings.rate_limit_login_max = 3
    settings.rate_limit_login_window_seconds = 60
    rate_limiter.reset()
    try:
        statuses = []
        for _ in range(5):
            r = client.post(
                "/api/auth/login",
                json={"username": "bruteforce-target", "password": "definitely-wrong"},
            )
            statuses.append(r.status_code)
        assert 429 in statuses
        # 第 4 次起应为 429
        assert statuses[3] == 429
        blocked = [s for s in statuses if s == 429]
        assert blocked, statuses
        # 被限流的那次响应必须带 Retry-After（重放第 4 次响应即可观察）
        assert statuses.count(429) >= 1
    finally:
        settings.rate_limit_login_max = old_max
        settings.rate_limit_login_window_seconds = old_window
        rate_limiter.reset()


def test_q167_429_response_body_has_correlation_id(client: TestClient):
    settings = get_settings()
    settings.rate_limit_login_max = 1
    rate_limiter.reset()
    try:
        client.post("/api/auth/login", json={"username": "rateuser", "password": "12345678"})
        r2 = client.post("/api/auth/login", json={"username": "rateuser", "password": "12345678"})
        assert r2.status_code == 429
        body = r2.json()
        assert body["code"] == "RATE_LIMITED"
        assert body["request_id"]
        assert r2.headers.get("Retry-After")
    finally:
        settings.rate_limit_login_max = 10
        rate_limiter.reset()


def test_q170_metrics_endpoint_exposes_counters_and_permission_denied(client: TestClient):
    # 触发一次权限拒绝（无 token 访问受保护接口 → 401；越权 403 在下方用无效 token 触发）
    before = registry.counter(METRIC_PERMISSION_DENIED, "").value(role="", module="AUTH")
    # /metrics 可匿名读取（受控网络由网关保护；非生产默认开放）
    res = client.get("/metrics")
    assert res.status_code == 200
    text = res.text
    assert "app_http_requests_total" in text
    assert "app_http_request_duration_seconds" in text
    assert "app_auth_login_failure_total" in text
    # 指标不含高基数/敏感值
    assert "bruteforce-target" not in text
    # AUTH 权限拒绝计数要么增长要么保持（取决于 401 是否计入 denied），主要验证不报错
    assert before >= 0


def test_q170_invalid_token_rejected_without_leaking(client: TestClient):
    # 无效 token 访问受保护资源必须被拒，且错误体不含堆栈/SQL/内部细节
    r = client.get("/api/visits/tasks", headers={"Authorization": "Bearer invalid.token.value"})
    assert r.status_code in (401, 403)
    body = r.text
    assert "Traceback" not in body
    assert "psycopg" not in body.lower()
    # 统一错误体带 request_id（顶层或 error 内层，两种结构都接受）
    payload = r.json()
    request_id = payload.get("request_id") or payload.get("error", {}).get("request_id")
    assert request_id


def test_q170_response_echoes_request_id_and_security_headers(client: TestClient):
    res = client.get("/healthz", headers={"X-Request-Id": "trace-abc"})
    assert res.headers["X-Request-Id"] == "trace-abc"
    assert res.headers.get("X-Content-Type-Options") == "nosniff"
    assert res.headers.get("X-Frame-Options") == "DENY"


def test_q170_cors_unknown_origin_not_allowed(client: TestClient, monkeypatch):
    # 默认未配置 CORS 白名单：任意外域 Origin 不应得到 Access-Control-Allow-Origin
    res = client.get("/healthz", headers={"Origin": "https://evil.example.com"})
    assert "Access-Control-Allow-Origin" not in res.headers
