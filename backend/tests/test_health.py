"""Q001 健康检查：/healthz 返回 200 与应用元信息，request_id 头存在。"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.errors import DomainError
from app.core.handlers import register_exception_handlers


def test_healthz_ok(client: TestClient) -> None:
    resp = client.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["app"] == "沁爽饮料分销管理平台"
    assert body["environment"] == "test"
    assert body["request_id"]


def test_request_id_header_echoed(client: TestClient) -> None:
    resp = client.get("/healthz", headers={"X-Request-Id": "fixed-id-001"})
    assert resp.status_code == 200
    assert resp.headers["X-Request-Id"] == "fixed-id-001"


def test_request_id_generated_when_missing(client: TestClient) -> None:
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert len(resp.headers["X-Request-Id"]) >= 16


def test_domain_error_returns_unified_structure(test_env: None) -> None:
    """统一错误体 {code,message,details,request_id}：领域异常经全局处理器转换。"""
    scratch = FastAPI()
    register_exception_handlers(scratch)

    @scratch.get("/boom-domain")
    def boom_domain() -> None:
        raise DomainError("SAMPLE_ERROR", "示例业务错误", {"field": "value"})

    with TestClient(scratch, raise_server_exceptions=False) as c:
        resp = c.get("/boom-domain")
    assert resp.status_code == 422
    body = resp.json()
    assert set(body) == {"code", "message", "details", "request_id"}
    assert body["code"] == "SAMPLE_ERROR"
    assert body["message"] == "示例业务错误"
    assert body["details"] == {"field": "value"}


def test_unhandled_error_returns_unified_structure(test_env: None) -> None:
    """未知异常返回 500 统一体，不暴露堆栈/SQL/密钥。"""
    scratch = FastAPI()
    register_exception_handlers(scratch)

    @scratch.get("/boom-unknown")
    def boom_unknown() -> None:
        raise RuntimeError("sql=SELECT secret FROM users")

    with TestClient(scratch, raise_server_exceptions=False) as c:
        resp = c.get("/boom-unknown")
    assert resp.status_code == 500
    body = resp.json()
    assert set(body) == {"code", "message", "details", "request_id"}
    assert body["code"] == "INTERNAL_ERROR"
    assert "sql" not in body["message"].lower()
    assert "secret" not in body["message"].lower()
