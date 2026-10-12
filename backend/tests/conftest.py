"""全局测试夹具：建库、跑迁移、事务回滚的 session、HTTP 客户端。"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

# T02 测试库连接（CI/本机一致）
TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+psycopg2://qinshuang:change-me-strong-password@localhost:5432/qinshuang_test",
)
TEST_DATABASE_URL_ASYNC = TEST_DATABASE_URL.replace("postgresql+psycopg2://", "postgresql+asyncpg://")


def _ensure_extensions() -> None:
    """确保 postgis/pgcrypto 已启用（幂等）。"""
    eng = create_engine(TEST_DATABASE_URL, isolation_level="AUTOCOMMIT")
    with eng.connect() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS postgis"))
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS pgcrypto"))
    eng.dispose()


@pytest.fixture(scope="session")
def applied_engine() -> Iterator[Engine]:
    """整次测试会话共享：建扩展 → 跑迁移 → 提供引擎。"""
    _ensure_extensions()
    eng = create_engine(TEST_DATABASE_URL, future=True)
    # 通过 alembic 跑迁移
    from alembic import command
    from alembic.config import Config

    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    command.upgrade(cfg, "head")
    yield eng
    eng.dispose()


@pytest.fixture
def engine(applied_engine: Engine) -> Iterator[Engine]:
    yield applied_engine


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    """每个测试用 SAVEPOINT 包裹，结束后回滚：互不污染。"""
    connection = engine.connect()
    trans = connection.begin()
    SessionLocal = sessionmaker(bind=connection, expire_on_commit=False, future=True)
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()
        trans.rollback()
        connection.close()


# ===== T01 健康/配置/错误测试夹具（保留） =====

TEST_ENV = {
    "ENVIRONMENT": "test",
    "SECRET_KEY": "test-secret-key-not-for-production",
    "DATABASE_URL": TEST_DATABASE_URL_ASYNC,
    "REDIS_URL": "redis://localhost:6379/0",
    "S3_ENDPOINT_URL": "http://localhost:9000",
    "S3_ACCESS_KEY": "test-access-key",
    "S3_SECRET_KEY": "test-secret-value",
    "DEMO_DATA_ENABLED": "false",
}

_OPTIONAL_ENV_KEYS = ["APP_NAME", "APP_VERSION", "S3_BUCKET", "LOG_LEVEL"]


def _isolate_env_file(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.core.config as config_module

    merged = {**dict(config_module.Settings.model_config), "env_file": None}
    monkeypatch.setattr(config_module.Settings, "model_config", merged)


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for key in list(TEST_ENV) + _OPTIONAL_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    _isolate_env_file(monkeypatch)
    yield


@pytest.fixture
def test_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for key, value in TEST_ENV.items():
        monkeypatch.setenv(key, value)
    _isolate_env_file(monkeypatch)
    import app.core.config as config_module

    config_module.get_settings.cache_clear()
    yield
    config_module.get_settings.cache_clear()


@pytest.fixture
def client(test_env: None) -> Iterator[TestClient]:
    from app.main import create_app

    with TestClient(create_app()) as c:
        yield c
