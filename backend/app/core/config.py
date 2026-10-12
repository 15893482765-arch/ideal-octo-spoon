"""应用配置。

安全约束（PRD §9.4 / T01 红线 / T03 红线）：
- 密钥一律 SecretStr，repr/日志/JSON 序列化只显示掩码，不出现明文；
- 必填配置缺失时启动即失败（fail-fast），且错误信息不包含任何密钥值；
- 认证域参数（TTL、限流、厂商开关）集中在本文件，禁止散落在路由/Service。
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

    # 演示数据总开关：生产环境必须为 false（PRD §8 存储约束 / R24）
    demo_data_enabled: bool = False
    # 生产数据集硬锁：当 environment=="production" 时，启动期硬性要求
    # `demo_data_enabled=False` 且 `production_dataset_lock=True`，禁止任何
    # `dataset_type=DEMO` 批次进入（PRD §10.2、T06 Q031）。
    production_dataset_lock: bool = True
    # 显式启用 `python -m app.cli.db seed-check` 类 CLI：默认 dev/test 可用
    enable_seed_cli: bool = True

    # 厂商协作方总开关（PRD §2.3）：默认关闭，开启后厂商可登录
    supplier_portal_enabled: bool = False

    log_level: str = "INFO"

    # ===== 认证域（T03）=====
    # access token 寿命：短期，便于被踢
    access_token_ttl_seconds: int = 900
    # refresh token 寿命：较长（30 天），但被踢/改密后立即失效
    refresh_token_ttl_seconds: int = 60 * 60 * 24 * 30
    # 连续登录失败上限；达到后锁定账号
    max_login_failures: int = 5
    # 锁定持续时间（秒）
    login_lockout_seconds: int = 900
    # 一次性激活凭据寿命（秒）：默认 7 天
    activation_ttl_seconds: int = 60 * 60 * 24 * 7
    # 一次性密码重置凭据寿命（秒）：默认 24 小时
    password_reset_ttl_seconds: int = 60 * 60 * 24

    # ===== 一次性 bootstrap（首次初始化管理员）=====
    # 仅当环境中存在下列 3 个变量时启用，且仅在没有系统管理员时执行一次：
    # INITIAL_ADMIN_USERNAME / INITIAL_ADMIN_PASSWORD / INITIAL_ADMIN_DISPLAY_NAME
    # 生产部署后应立即从 env 移除 INITIAL_ADMIN_PASSWORD。
    initial_admin_username: str | None = None
    initial_admin_password: SecretStr | None = None
    initial_admin_display_name: str | None = None
    initial_admin_bootstrap: bool = False

    # ===== 文件域（T04 / PRD §8：S3 兼容存储 + 短期签名 URL）=====
    # 上传 MIME 白名单（逗号分隔）；不在名单内的类型直接拒绝
    file_upload_whitelist: str = "image/jpeg,image/png,image/webp,application/pdf"
    # 单文件大小上限（字节）：默认 10MB
    file_max_size_bytes: int = 10 * 1024 * 1024
    # 签名下载 URL 有效期（秒）：短期，默认 5 分钟
    file_signed_url_ttl_seconds: int = 300
    # 存储后端：local（本地文件系统，开发/测试）/ s3（MinIO 等 S3 兼容）
    storage_backend: Literal["local", "s3"] = "local"
    local_storage_dir: str = "./.data/storage"

    # ===== 幂等域（T04 / PRD §5.4）=====
    # 幂等键保留期（小时）：过期后后台清理，窗口外同键视为新请求
    idempotency_retention_hours: int = 24 * 7

    @property
    def database_url_sync(self) -> str:
        """Alembic 与同步测试使用的 URL：把 asyncpg 驱动替换为 psycopg2。"""
        return self.database_url.replace("postgresql+asyncpg://", "postgresql+psycopg2://")


@lru_cache
def get_settings() -> Settings:
    """返回进程级单例配置；缺失必填项时在首次调用处直接抛错。"""
    # pydantic-settings 从环境变量/.env 注入必填字段，mypy 无法静态推断
    return Settings()  # type: ignore[call-arg]
