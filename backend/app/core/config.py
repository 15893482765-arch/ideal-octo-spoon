"""应用配置。

安全约束（PRD §9.4 / T01 红线）：
- 密钥一律 SecretStr，repr/日志/JSON 序列化只显示掩码，不出现明文；
- 必填配置缺失时启动即失败（fail-fast），且错误信息不包含任何密钥值。
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """环境变量驱动的应用配置。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_name: str = "沁爽饮料分销管理平台"
    app_version: str = "0.1.0"
    environment: Literal["dev", "test", "production"]

    # 认证密钥：只允许 SecretStr，任何输出路径都不得出现明文
    secret_key: SecretStr

    # 数据层（PostgreSQL 16 + PostGIS）
    database_url: str

    # 缓存/幂等/分布式锁
    redis_url: str

    # S3 兼容对象存储（本地 MinIO）
    s3_endpoint_url: str
    s3_access_key: SecretStr
    s3_secret_key: SecretStr
    s3_bucket: str = "qinshuang"

    # 演示数据总开关：生产环境必须为 false（PRD §8 存储约束）
    demo_data_enabled: bool = False

    log_level: str = "INFO"

    @property
    def database_url_sync(self) -> str:
        """Alembic 与同步测试使用的 URL：把 asyncpg 驱动替换为 psycopg2。"""
        return self.database_url.replace("postgresql+asyncpg://", "postgresql+psycopg2://")


@lru_cache
def get_settings() -> Settings:
    """返回进程级单例配置；缺失必填项时在首次调用处直接抛错。"""
    # pydantic-settings 从环境变量/.env 注入必填字段，mypy 无法静态推断
    return Settings()  # type: ignore[call-arg]
