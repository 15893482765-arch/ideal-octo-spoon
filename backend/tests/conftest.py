"""全局测试夹具：建库、跑迁移、事务回滚的 session、HTTP 客户端。"""

from __future__ import annotations

import os
from collections.abc import Iterator
from uuid import UUID, uuid4

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
    from alembic import command
    from alembic.config import Config

    # env.py 用 settings.database_url_sync（读 DATABASE_URL/.env）覆盖
    # set_main_option，必须把环境变量也指到测试库，否则迁移会误作用于 dev 库。
    os.environ["DATABASE_URL"] = TEST_DATABASE_URL_ASYNC
    from app.core.config import get_settings

    get_settings.cache_clear()

    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    command.upgrade(cfg, "head")
    yield eng
    eng.dispose()


@pytest.fixture
def engine(applied_engine: Engine, test_env: None) -> Iterator[Engine]:
    # 依赖 test_env：确保只读 fixture 也跑在测试配置下（例如 AI_CHAT_PROVIDER=mock，
    # 否则本地 .env 配了真实模型时，DB 测试会意外发网络请求）。
    yield applied_engine


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    """每个测试用 SAVEPOINT 包裹，结束后回滚：互不污染。

    注意：rollback 只回滚到 SAVEPOINT；T03 启用了 bootstrap 事务里 commit 的管理员
    会持续存在于同 session 范围内，但每个测试函数拿到的 connection 是新的，所以
    bootstrap 类的全局副作用用专门的 fixture（`bootstrap_admin`）显式管理。
    """
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
    # 测试绝不发真实模型请求：显式钉住 mock（本地 .env 可能配了 moonshot）
    "AI_CHAT_PROVIDER": "mock",
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
    from app.core.rate_limiter import rate_limiter
    from app.main import create_app

    # T29：每个 HTTP 测试用例前清空限流计数，避免跨用例的窗口污染
    rate_limiter.reset()
    with TestClient(create_app()) as c:
        yield c


@pytest.fixture
def isolated_session(applied_engine: Engine) -> Iterator[Session]:
    """外层事务 + SAVEPOINT 的 session：`commit()` 只释放保存点，用例结束整体回滚。

    与 `session` 的差别仅在 `join_transaction_mode="create_savepoint"`：它让被测代码里
    的 `commit()` 不真正落库，适合"服务层确实会 commit、但测试不该留痕"的联调。
    """
    connection = applied_engine.connect()
    trans = connection.begin()
    SessionLocal = sessionmaker(
        bind=connection,
        expire_on_commit=False,
        future=True,
        join_transaction_mode="create_savepoint",
    )
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()
        trans.rollback()
        connection.close()


@pytest.fixture
def isolated_client(
    test_env: None, isolated_session: Session
) -> Iterator[TestClient]:
    """把路由的 DB 依赖替换成 `isolated_session`，使 HTTP 层的 commit 也不留痕。

    普通 `client` 会把服务端 `commit()` 真实写进测试库，污染依赖全表计数的用例；
    需要"能 commit 但不落库"的接口联调（主数据导入、审计查询）请用本 fixture。
    """
    from app.core.deps import db_session
    from app.core.rate_limiter import rate_limiter
    from app.main import create_app

    app = create_app()

    def _override() -> Iterator[Session]:
        yield isolated_session

    app.dependency_overrides[db_session] = _override
    rate_limiter.reset()
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


# ===== T03 测试夹具：角色矩阵、admin 引导 =====


# T03 默认测试密码（明文）；仅在内存中存在，绝不落库
TEST_PASSWORD = "T03-Test-Pass-9876"
TEST_PASSWORD_ALT = "T03-Alt-Pass-2468"


def _make_role(session: Session, code: str, name: str, role_code: str, dataset_id: UUID) -> None:
    from app.models import Role
    from app.models.enums import RoleCode

    # 校验 role_code 在受控枚举内
    RoleCode(role_code)  # 非法时抛 ValueError
    role = Role(
        code=code,
        name=name,
        role_code=role_code,
        dataset_type="PRODUCTION",
        dataset_id=dataset_id,
    )
    session.add(role)
    session.flush()
    return role  # type: ignore[return-value]


def _hash(plaintext: str) -> str:
    from app.core.security import hash_password

    return hash_password(plaintext)


@pytest.fixture
def bootstrap_admin(applied_engine: Engine) -> Iterator[dict[str, str]]:
    """在专用 session 上创建首位系统管理员（直接 commit，不开外层事务）。"""
    from app.core.tokens import token_revocation
    from app.models import User
    from app.models.enums import UserStatus
    from app.services.bootstrap import ensure_system_admin_role

    # 重置撤销名单 + 限流器，避免上一个测试遗留状态污染
    token_revocation.reset()

    SessionLocal = sessionmaker(bind=applied_engine, expire_on_commit=False, future=True)
    s = SessionLocal()
    try:
        dsid = uuid4()
        role = ensure_system_admin_role(s, dataset_id=dsid)
        unique_suffix = uuid4().hex[:8]
        user = User(
            username=f"t03_admin_{unique_suffix}",
            display_name="T03 管理员",
            role_id=role.id,
            status=UserStatus.ACTIVE.value,
            must_change_password=True,
            password_hash=_hash(TEST_PASSWORD),
            dataset_type="PRODUCTION",
            dataset_id=dsid,
        )
        s.add(user)
        s.commit()
        s.refresh(user)
        yield {
            "username": user.username,
            "password": TEST_PASSWORD,
            "user_id": str(user.id),
        }
    finally:
        s.close()


@pytest.fixture
def all_roles(applied_engine: Engine, bootstrap_admin: dict[str, str]) -> Iterator[dict[str, UUID]]:
    """建齐 6 类角色（直接 commit），返回 role_id 字典。

    每个调用使用新 dataset_id，跨测试隔离。
    """
    from app.models import Role
    from app.models.enums import RoleCode

    SessionLocal = sessionmaker(bind=applied_engine, expire_on_commit=False, future=True)
    s = SessionLocal()
    try:
        dsid = uuid4()
        roles: dict[str, UUID] = {}
        for rc, name in [
            (RoleCode.SALES_REP, "业务员"),
            (RoleCode.REGIONAL_MANAGER, "片区经理"),
            (RoleCode.SUPERVISOR, "督导"),
            (RoleCode.FINANCE, "财务"),
            (RoleCode.SYSTEM_ADMIN, "系统管理员"),
            (RoleCode.SUPPLIER, "厂商"),
        ]:
            r = Role(
                code=f"T03-{rc.value}",
                name=name,
                role_code=rc.value,
                dataset_type="PRODUCTION",
                dataset_id=dsid,
            )
            s.add(r)
            s.flush()
            roles[rc.value] = r.id
        s.commit()
        yield roles
    finally:
        s.close()


def activate_user_and_change_password(
    applied_engine: Engine, *, user_id: UUID, new_password: str = TEST_PASSWORD_ALT
) -> None:
    """跳过 HTTP 直接把账号置为 ACTIVE、设密码、清除 must_change_password。

    测试中部分用例需要快速让账号"可用"以测后续流程。
    """
    from app.core.security import hash_password
    from app.core.tokens import token_revocation
    from app.models import User
    from app.models.enums import UserStatus

    token_revocation.bump_user_version(user_id, 999)  # 强制踢掉此前 token

    connection = applied_engine.connect()
    trans = connection.begin()
    SessionLocal = sessionmaker(bind=connection, expire_on_commit=False, future=True)
    s = SessionLocal()
    try:
        u = s.get(User, user_id)
        if u is None:
            raise RuntimeError(f"user {user_id} not found")
        u.password_hash = hash_password(new_password)
        u.status = UserStatus.ACTIVE.value
        u.must_change_password = False
        u.password_changed_at = None
        u.password_version = 1
        s.commit()
        trans.commit()
    finally:
        s.close()
        connection.close()
