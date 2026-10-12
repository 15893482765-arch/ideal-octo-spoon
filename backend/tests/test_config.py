"""Q002 配置缺失时启动失败且不打印密钥。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError


def _import_settings() -> type:
    from app.core.config import Settings

    return Settings


def test_missing_required_config_fails_fast(clean_env: None) -> None:
    settings_cls = _import_settings()
    with pytest.raises(ValidationError) as exc_info:
        settings_cls()
    msg = str(exc_info.value)
    # 必填字段全部被点名
    for field in ("environment", "secret_key", "database_url", "redis_url"):
        assert field in msg


def test_secrets_never_leak_in_repr(clean_env: None) -> None:
    settings_cls = _import_settings()
    s = settings_cls(
        environment="test",
        secret_key="super-secret-value-123",
        database_url="postgresql+asyncpg://u:p@h:5432/db",
        redis_url="redis://localhost:6379/0",
        s3_endpoint_url="http://localhost:9000",
        s3_access_key="AKIAEXAMPLE",
        s3_secret_key="s3-super-secret",
    )
    dumped = repr(s) + str(s.model_dump(mode="json"))
    assert "super-secret-value-123" not in dumped
    assert "s3-super-secret" not in dumped
    assert "AKIAEXAMPLE" not in dumped
    assert "**********" in dumped  # SecretStr 掩码


def test_error_message_contains_no_secret_values(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """部分配置缺失时报错只点名缺失字段，不回显已提供密钥的明文。"""
    monkeypatch.setenv("SECRET_KEY", "another-secret-xyz")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@h/db")
    settings_cls = _import_settings()
    with pytest.raises(ValidationError) as exc_info:
        settings_cls()
    msg = str(exc_info.value)
    assert "another-secret-xyz" not in msg
    assert "environment" in msg  # 缺失项仍被点名


def test_invalid_environment_rejected(clean_env: None) -> None:
    settings_cls = _import_settings()
    with pytest.raises(ValidationError):
        settings_cls(
            environment="staging",  # 只允许 dev/test/production
            secret_key="x",
            database_url="postgresql+asyncpg://u:p@h/db",
            redis_url="redis://localhost:6379/0",
            s3_endpoint_url="http://localhost:9000",
            s3_access_key="a",
            s3_secret_key="b",
        )
