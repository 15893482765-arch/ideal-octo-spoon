"""测试夹具：隔离环境变量，避免读取开发者本机 .env 导致测试不确定。"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

TEST_ENV = {
    "ENVIRONMENT": "test",
    "SECRET_KEY": "test-secret-key-not-for-production",
    "DATABASE_URL": "postgresql+asyncpg://user:pass@localhost:5432/testdb",
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
    """清空相关环境变量并屏蔽 .env 文件，保证 Settings 从零开始校验。"""
    for key in list(TEST_ENV) + _OPTIONAL_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    _isolate_env_file(monkeypatch)
    yield


@pytest.fixture
def test_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """注入测试所需的最小合法配置，并重置配置单例缓存。"""
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
