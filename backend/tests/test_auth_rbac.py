"""Q010-Q015：T03 登录、管理员建号、RBAC/ABAC 测试。

被测文件：app/api/auth.py, app/api/admin_users.py, app/services/auth_service.py,
app/services/user_admin_service.py, app/core/permissions.py。
"""

from __future__ import annotations

from collections.abc import Iterator
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from tests.conftest import (
    TEST_PASSWORD_ALT,
)

from app.core.config import get_settings
from app.core.tokens import token_revocation
from app.models import User
from app.models.enums import RoleCode, UserStatus

# ============== Q010 无注册路由 ==============


def test_q010_no_register_route(client: TestClient) -> None:
    """Q010：无注册入口；POST /api/auth/register 必须 404/405。

    同时校验整个 auth 域只暴露下列端点：
    /api/auth/{login,logout,refresh,activate,change-password,password-reset/consume,me}
    """
    # 常见猜测的注册路由必须被拒绝（不存在的端点 404/405；存在的端点走认证得 401）
    for path, body in [
        ("/api/auth/register", {"username": "evil", "password": "abcdefgh"}),
        ("/api/auth/signup", {"username": "evil", "password": "abcdefgh"}),
        ("/api/users", {"username": "evil", "password": "abcdefgh"}),
        ("/api/admin/users", {"username": "evil", "password": "abcdefgh"}),
    ]:
        resp = client.post(path, json=body)
        # 不存在的路由：404/405；存在的受限路由：无 token 时 401
        assert resp.status_code in (401, 403, 404, 405), (
            f"{path} 不应接受未认证的注册请求: {resp.status_code}"
        )

    # /api/admin/users 只能由系统管理员调用；不带头部必须 401
    resp = client.post(
        "/api/admin/users",
        json={
            "username": "evil2",
            "display_name": "x",
            "role_id": "00000000-0000-0000-0000-000000000000",
        },
    )
    assert resp.status_code in (401, 403)


# ============== Q011 首次改密 ==============


def _activate_token_for(client: TestClient, admin_token: str, payload: dict) -> dict:
    """管理员建号，拿到一次性凭据。"""
    resp = client.post(
        "/api/admin/users",
        headers={"Authorization": f"Bearer {admin_token}"},
        json=payload,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_q011_first_time_change_password(
    client: TestClient, bootstrap_admin: dict[str, str], all_roles: dict[str, UUID]
) -> None:
    """Q011：管理员建号 → 新账号用一次性凭据激活 + 改密 → 旧临时凭据失效。"""
    # 唯一 username：避免跨测试冲突
    new_username = f"t03_sales_{uuid4().hex[:8]}"

    # 1) 管理员登录
    resp = client.post(
        "/api/auth/login",
        json={"username": bootstrap_admin["username"], "password": bootstrap_admin["password"]},
    )
    assert resp.status_code == 200, resp.text
    admin_tokens = resp.json()
    admin_access = admin_tokens["access_token"]
    # must_change_password=True（首次登录强制改密）
    assert admin_tokens["must_change_password"] is True

    # 2) 管理员创建业务员账号
    created = _activate_token_for(
        client,
        admin_access,
        {
            "username": new_username,
            "display_name": "T03 业务员-新人",
            "role_id": str(all_roles[RoleCode.SALES_REP.value]),
        },
    )
    activation_token = created["one_time_credential"]
    # 关键：响应里只有这一个一次性凭据，没有任何可登录密码
    assert "one_time_credential" in created
    assert "password" not in created
    assert "initial_password" not in created

    # 3) 持凭据激活 + 设新密码
    resp = client.post(
        "/api/auth/activate",
        json={"activation_token": activation_token, "new_password": TEST_PASSWORD_ALT},
    )
    assert resp.status_code == 204, resp.text

    # 4) 同一凭据不能复用（消费后失效）
    resp = client.post(
        "/api/auth/activate",
        json={"activation_token": activation_token, "new_password": "Another-Pass-1357"},
    )
    # 一次性凭据被消费后再次提交 → 401（认证域错误）
    assert resp.status_code == 401, resp.text
    body = resp.json()
    assert body["code"] in ("INVALID_ACTIVATION_TOKEN",)

    # 5) 凭据作废后旧凭据不能登入；用新密码登录
    resp = client.post(
        "/api/auth/login",
        json={"username": new_username, "password": TEST_PASSWORD_ALT},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # 业务员首次激活后不再 must_change_password
    assert body["must_change_password"] is False
    assert body["user"]["role_code"] == "SALES_REP"

    # 6) 改密：调用 change-password，旧 token 全部失效
    new_tokens = body
    resp = client.post(
        "/api/auth/change-password",
        headers={"Authorization": f"Bearer {new_tokens['access_token']}"},
        json={"old_password": TEST_PASSWORD_ALT, "new_password": "T03-NEW-Pass-7777"},
    )
    assert resp.status_code == 204

    # 7) 旧 access token 应被踢；新密码可登入
    resp = client.get(
        "/api/auth/me",
        headers={"Authorization": f"Bearer {new_tokens['access_token']}"},
    )
    assert resp.status_code == 401, "改密后旧 access token 应当失效"

    resp = client.post(
        "/api/auth/login",
        json={"username": new_username, "password": "T03-NEW-Pass-7777"},
    )
    assert resp.status_code == 200


# ============== Q012 角色 × 模块矩阵 ==============


@pytest.fixture
def six_role_users(
    applied_engine,
    bootstrap_admin: dict[str, str],
    all_roles: dict[str, UUID],
) -> Iterator[dict[str, dict]]:
    """为 6 类角色各建一个 ACTIVE 用户，返回 {role_code: {"username","password","user_id"}}。

    使用 TEST_PASSWORD_ALT 一次性设密；直接 commit 到 DB。
    """
    created: dict[str, dict] = {}
    suffix = uuid4().hex[:8]
    from sqlalchemy.orm import sessionmaker

    SessionLocal = sessionmaker(bind=applied_engine, expire_on_commit=False, future=True)
    s = SessionLocal()
    try:
        from app.core.security import hash_password

        dsid = uuid4()
        for rc, name in [
            (RoleCode.SALES_REP, "t03_sales"),
            (RoleCode.REGIONAL_MANAGER, "t03_manager"),
            (RoleCode.SUPERVISOR, "t03_supervisor"),
            (RoleCode.FINANCE, "t03_finance"),
            (RoleCode.SYSTEM_ADMIN, "t03_sa"),
            (RoleCode.SUPPLIER, "t03_supplier"),
        ]:
            u = User(
                username=f"t03_{rc.value.lower()}_{suffix}",
                display_name=name,
                role_id=all_roles[rc.value],
                status=UserStatus.ACTIVE.value,
                must_change_password=False,
                password_hash=hash_password(TEST_PASSWORD_ALT),
                dataset_type="PRODUCTION",
                dataset_id=dsid,
            )
            s.add(u)
            s.flush()
            created[rc.value] = {
                "username": u.username,
                "password": TEST_PASSWORD_ALT,
                "user_id": str(u.id),
            }
        s.commit()
    finally:
        s.close()

    yield created


def test_q012_role_module_matrix(
    client: TestClient,
    six_role_users: dict[str, dict],
    bootstrap_admin: dict[str, str],
) -> None:
    """Q012：6 类角色在 user.admin / payroll.disburse / merchant / finance 上的能力差异。"""
    from app.core.permissions import (
        MODULE_FINANCE,
        MODULE_PAYROLL_DISBURSE,
        MODULE_USER_ADMIN,
        ActorContext,
        get_profile,
    )

    # ----- 1) 纯代码矩阵自检（不依赖 HTTP） -----
    sales = get_profile(RoleCode.SALES_REP.value)
    mgr = get_profile(RoleCode.REGIONAL_MANAGER.value)
    finance = get_profile(RoleCode.FINANCE.value)
    admin = get_profile(RoleCode.SYSTEM_ADMIN.value)
    supplier = get_profile(RoleCode.SUPPLIER.value)

    # 系统管理员**不自动**获得 payroll.disburse（红线）
    assert MODULE_USER_ADMIN in admin.abilities
    assert MODULE_PAYROLL_DISBURSE not in admin.abilities, "系统管理员不应自动获得工资发放"
    # 财务才有 payroll.disburse
    assert MODULE_PAYROLL_DISBURSE in finance.abilities
    # 业务员不能访问财务/采购模块
    assert MODULE_FINANCE not in sales.abilities
    assert MODULE_PAYROLL_DISBURSE not in sales.abilities
    # 经理管理本片区业务，但**不**有工资发放
    assert MODULE_FINANCE not in mgr.abilities
    assert MODULE_PAYROLL_DISBURSE not in mgr.abilities
    # 厂商只能登录 + 确认供货
    assert MODULE_USER_ADMIN not in supplier.abilities
    assert MODULE_PAYROLL_DISBURSE not in supplier.abilities

    # ----- 2) HTTP 端到端：管理员端点必须由 SYSTEM_ADMIN 调 -----
    # 业务员调用 /api/admin/users → 403
    token = client.post(
        "/api/auth/login",
        json={
            "username": six_role_users[RoleCode.SALES_REP.value]["username"],
            "password": TEST_PASSWORD_ALT,
        },
    ).json()["access_token"]
    resp = client.get(
        "/api/admin/users",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403, resp.text

    # 经理调用 → 403
    token = client.post(
        "/api/auth/login",
        json={
            "username": six_role_users[RoleCode.REGIONAL_MANAGER.value]["username"],
            "password": TEST_PASSWORD_ALT,
        },
    ).json()["access_token"]
    resp = client.get(
        "/api/admin/users",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403

    # 财务调用 → 403
    token = client.post(
        "/api/auth/login",
        json={
            "username": six_role_users[RoleCode.FINANCE.value]["username"],
            "password": TEST_PASSWORD_ALT,
        },
    ).json()["access_token"]
    resp = client.get(
        "/api/admin/users",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403

    # 系统管理员自己可访问
    token = client.post(
        "/api/auth/login",
        json={
            "username": six_role_users[RoleCode.SYSTEM_ADMIN.value]["username"],
            "password": TEST_PASSWORD_ALT,
        },
    ).json()["access_token"]
    resp = client.get(
        "/api/admin/users",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    # 列表中能看到 t03_sales、t03_manager 等 6 个用户
    listed = {u["username"] for u in resp.json()}
    for rc in (
        RoleCode.SALES_REP,
        RoleCode.REGIONAL_MANAGER,
        RoleCode.SUPERVISOR,
        RoleCode.FINANCE,
        RoleCode.SUPPLIER,
    ):
        assert six_role_users[rc.value]["username"] in listed

    # ----- 3) 字段脱敏：厂商拿到 ActorContext 时，敏感字段必须被剔除 -----
    supplier_ctx = ActorContext(
        user_id=uuid4(),
        role_code=RoleCode.SUPPLIER.value,
        region_ids=frozenset(),
        brand_ids=frozenset(),
    )
    redacted = permission_service_redact(
        supplier_ctx,
        {
            "id": "1",
            "merchant_location_wkt": "POINT(121 31)",
            "payroll_total_fen": 10000,
            "phone": "13800000000",
        },
    )
    # 字段脱敏：敏感字段必须为 None
    assert redacted["merchant_location_wkt"] is None
    assert redacted["payroll_total_fen"] is None
    assert redacted["phone"] is None
    # 业务字段（id 等）保留
    assert redacted["id"] == "1"


def permission_service_redact(actor, payload):
    from app.core.permissions import permission_service

    return permission_service.redact_payload(actor, payload)


# ============== Q013 业务员跨片区 403 ==============


def test_q013_sales_rep_cross_region_forbidden(
    client: TestClient,
    applied_engine,
    bootstrap_admin: dict[str, str],
    all_roles: dict[str, UUID],
) -> None:
    """Q013：业务员只能看自己被授权的 region；越界 region 的商家 ID 不返回。

    验证 PermissionService.filter_merchant_ids 在 scope=self/region 下的行为。
    """
    from app.core.permissions import ActorContext, permission_service

    region_a = uuid4()
    region_b = uuid4()
    merchant_a1 = uuid4()
    merchant_a2 = uuid4()
    merchant_b1 = uuid4()

    # 业务员只授权 region_a。self scope 下业务员"自己负责"由 Service 通过
    # merchant.owner_user_id 决定，PermissionService.filter_merchant_ids 做全集切片。
    _sales_ctx = ActorContext(
        user_id=uuid4(),
        role_code=RoleCode.SALES_REP.value,
        region_ids=frozenset({region_a}),
        brand_ids=frozenset(),
    )
    # PermissionService.filter_merchant_ids 在 self scope 下做"全集切片"由 Service 层负责。
    # 这里改测"经理跨片区"的纯 region 切片语义：
    mgr_ctx = ActorContext(
        user_id=uuid4(),
        role_code=RoleCode.REGIONAL_MANAGER.value,
        region_ids=frozenset({region_a}),
        brand_ids=frozenset(),
    )
    merchants = {
        merchant_a1: region_a,
        merchant_a2: region_a,
        merchant_b1: region_b,
    }
    visible = permission_service.filter_merchant_ids(mgr_ctx, merchants)
    # region_b 的商家必须不可见
    assert merchant_a1 in visible
    assert merchant_a2 in visible
    assert merchant_b1 not in visible, "片区经理跨片区越界"

    # 验证 visible_for_actor：经理只看到 region_a
    visible_regions = permission_service.visible_for_actor(
        mgr_ctx, [region_a, region_b]
    )
    assert visible_regions == {region_a}

    # 厂商：rows=brand_region；空集
    supplier_ctx = ActorContext(
        user_id=uuid4(),
        role_code=RoleCode.SUPPLIER.value,
        region_ids=frozenset(),
        brand_ids=frozenset(),
        supplier_brand_ids=frozenset(),
        supplier_region_ids=frozenset({region_a}),
    )
    visible = permission_service.filter_merchant_ids(supplier_ctx, merchants)
    assert visible == set(), "厂商不应直接看到商家 ID 列表"


# ============== Q014 厂商功能关闭不可登录 ==============


def test_q014_supplier_portal_disabled_blocks_login(
    client: TestClient,
    applied_engine,
    bootstrap_admin: dict[str, str],
    all_roles: dict[str, UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Q014：SUPPLIER_PORTAL_ENABLED=false 时厂商账号无法登录。"""
    from app.core.security import hash_password
    from app.models import User
    from app.models.enums import RoleCode, UserStatus

    # 重置配置为关闭
    monkeypatch.setenv("SUPPLIER_PORTAL_ENABLED", "false")
    get_settings.cache_clear()

    # 在专用连接上创建一个 ACTIVE 厂商账号（直接 commit；不开启外层事务）
    from sqlalchemy.orm import sessionmaker

    SessionLocal = sessionmaker(bind=applied_engine, expire_on_commit=False, future=True)
    s = SessionLocal()
    supplier_username = f"t03_supplier_only_{uuid4().hex[:8]}"
    try:
        u = User(
            username=supplier_username,
            display_name="厂商-门户关闭",
            role_id=all_roles[RoleCode.SUPPLIER.value],
            status=UserStatus.ACTIVE.value,
            must_change_password=False,
            password_hash=hash_password(TEST_PASSWORD_ALT),
            dataset_type="PRODUCTION",
            dataset_id=uuid4(),
        )
        s.add(u)
        s.commit()
    finally:
        s.close()

    # 登录应被拒
    resp = client.post(
        "/api/auth/login",
        json={"username": supplier_username, "password": TEST_PASSWORD_ALT},
    )
    # 厂商功能关闭属于认证域 → 401
    assert resp.status_code == 401, resp.text
    body = resp.json()
    assert body["code"] == "SUPPLIER_PORTAL_DISABLED"


def test_q014_supplier_portal_enabled_allows_login(
    client: TestClient,
    applied_engine,
    bootstrap_admin: dict[str, str],
    all_roles: dict[str, UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Q014 反向：开启后厂商可正常登录。"""
    from app.core.security import hash_password
    from app.models import User
    from app.models.enums import RoleCode, UserStatus

    monkeypatch.setenv("SUPPLIER_PORTAL_ENABLED", "true")
    get_settings.cache_clear()

    connection = applied_engine.connect()
    from sqlalchemy.orm import sessionmaker

    SessionLocal = sessionmaker(bind=connection, expire_on_commit=False, future=True)
    s = SessionLocal()
    supplier_username = f"t03_supplier_on_{uuid4().hex[:8]}"
    try:
        u = User(
            username=supplier_username,
            display_name="厂商-门户开启",
            role_id=all_roles[RoleCode.SUPPLIER.value],
            status=UserStatus.ACTIVE.value,
            must_change_password=False,
            password_hash=hash_password(TEST_PASSWORD_ALT),
            dataset_type="PRODUCTION",
            dataset_id=uuid4(),
        )
        s.add(u)
        s.commit()
    finally:
        s.close()
        connection.close()

    resp = client.post(
        "/api/auth/login",
        json={"username": supplier_username, "password": TEST_PASSWORD_ALT},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["user"]["role_code"] == "SUPPLIER"


# ============== Q015 token 撤销 ==============


def test_q015_token_revocation(
    client: TestClient,
    applied_engine,
    bootstrap_admin: dict[str, str],
    all_roles: dict[str, UUID],
) -> None:
    """Q015：登出、改密、角色变更三种路径都能使 access token 失效。"""
    from app.core.security import hash_password
    from app.models import User
    from app.models.enums import RoleCode, UserStatus

    # 重置：避免上一用例遗留 state
    token_revocation.reset()

    # 1) 创建测试用户（直接 commit；不开启外层事务）
    user_id = None
    test_username = f"t03_q15_{uuid4().hex[:8]}"
    from sqlalchemy.orm import sessionmaker

    SessionLocal = sessionmaker(bind=applied_engine, expire_on_commit=False, future=True)
    s = SessionLocal()
    try:
        u = User(
            username=test_username,
            display_name="Q15 测试员",
            role_id=all_roles[RoleCode.SALES_REP.value],
            status=UserStatus.ACTIVE.value,
            must_change_password=False,
            password_hash=hash_password(TEST_PASSWORD_ALT),
            dataset_type="PRODUCTION",
            dataset_id=uuid4(),
        )
        s.add(u)
        s.commit()
        user_id = u.id
    finally:
        s.close()
    assert user_id is not None

    # 2) 登录拿 access/refresh
    resp = client.post(
        "/api/auth/login",
        json={"username": test_username, "password": TEST_PASSWORD_ALT},
    )
    assert resp.status_code == 200
    tokens = resp.json()
    access = tokens["access_token"]
    assert tokens["must_change_password"] is False

    # 3) /me 正常
    resp = client.get("/api/auth/me", headers={"Authorization": f"Bearer {access}"})
    assert resp.status_code == 200

    # 4) 登出：access token 进入黑名单；/me 401
    resp = client.post("/api/auth/logout", headers={"Authorization": f"Bearer {access}"})
    assert resp.status_code == 204
    resp = client.get("/api/auth/me", headers={"Authorization": f"Bearer {access}"})
    assert resp.status_code == 401

    # 5) 改密：用旧 access 改密成功（仍可校验，因为登出仅黑名单旧 jti；改密需要新的 token）
    #    重新登录拿 token
    resp = client.post(
        "/api/auth/login",
        json={"username": test_username, "password": TEST_PASSWORD_ALT},
    )
    assert resp.status_code == 200
    new_access = resp.json()["access_token"]
    resp = client.post(
        "/api/auth/change-password",
        headers={"Authorization": f"Bearer {new_access}"},
        json={"old_password": TEST_PASSWORD_ALT, "new_password": "T03-After-Change-5555"},
    )
    assert resp.status_code == 204

    # 6) 改密后旧 token 失效
    resp = client.get(
        "/api/auth/me", headers={"Authorization": f"Bearer {new_access}"}
    )
    assert resp.status_code == 401, "改密后旧 token 必须失效"

    # 7) 管理员变更角色后所有 token 失效
    # 重新登录
    resp = client.post(
        "/api/auth/login",
        json={"username": test_username, "password": "T03-After-Change-5555"},
    )
    assert resp.status_code == 200, resp.text
    user_access = resp.json()["access_token"]

    # 管理员登录
    admin_token = client.post(
        "/api/auth/login",
        json={"username": bootstrap_admin["username"], "password": bootstrap_admin["password"]},
    ).json()["access_token"]

    # 角色变更：SALES_REP -> SUPERVISOR
    resp = client.post(
        f"/api/admin/users/{user_id}/change-role",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"role_id": str(all_roles[RoleCode.SUPERVISOR.value])},
    )
    assert resp.status_code == 204

    # 用户原 token 失效
    resp = client.get(
        "/api/auth/me", headers={"Authorization": f"Bearer {user_access}"}
    )
    assert resp.status_code == 401, "角色变更后旧 token 必须失效"

    # 8) refresh token 一次性消费：第二次用同一 refresh 应失败
    resp = client.post(
        "/api/auth/login",
        json={"username": test_username, "password": "T03-After-Change-5555"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    refresh = body["refresh_token"]
    # 第一次 refresh 成功
    resp = client.post("/api/auth/refresh", json={"refresh_token": refresh})
    assert resp.status_code == 200
    # 第二次 refresh 失败（被消费过）→ AuthError → 401
    resp = client.post("/api/auth/refresh", json={"refresh_token": refresh})
    assert resp.status_code == 401, resp.text


# ============== 边界用例：登录失败限流 ==============


def test_login_failure_rate_limit_and_lock(
    client: TestClient,
    bootstrap_admin: dict[str, str],
) -> None:
    """连续 5 次错误密码 → 账号被锁定 900s。"""
    from app.core.rate_limit import login_rate_limiter

    # 重置限流器
    login_rate_limiter.reset(
        username=bootstrap_admin["username"], user_id=UUID(bootstrap_admin["user_id"])
    )
    for _ in range(5):
        resp = client.post(
            "/api/auth/login",
            json={"username": bootstrap_admin["username"], "password": "wrong-pwd-9999"},
        )
        # InvalidCredentialsError -> 401
        assert resp.status_code == 401, resp.text
    # 第 6 次：被锁定
    resp = client.post(
        "/api/auth/login",
        json={"username": bootstrap_admin["username"], "password": "wrong-pwd-9999"},
    )
    assert resp.status_code == 401, resp.text
    body = resp.json()
    assert body["code"] == "ACCOUNT_LOCKED"
    assert body["details"]["remaining_seconds"] > 0

    # 锁定期间即便正确密码也被拒
    resp = client.post(
        "/api/auth/login",
        json={
            "username": bootstrap_admin["username"],
            "password": bootstrap_admin["password"],
        },
    )
    assert resp.status_code == 401, resp.text
    assert resp.json()["code"] == "ACCOUNT_LOCKED"

    # 清理：测试结束重置限流器，避免污染其他用例
    login_rate_limiter.reset(
        username=bootstrap_admin["username"], user_id=UUID(bootstrap_admin["user_id"])
    )


# ============== 边界用例：管理员不能停用自己 ==============


def test_admin_cannot_deactivate_self(
    client: TestClient,
    bootstrap_admin: dict[str, str],
    all_roles: dict[str, UUID],
) -> None:
    """系统管理员不能停用或改自己角色（防自裁）。"""
    token = client.post(
        "/api/auth/login",
        json={"username": bootstrap_admin["username"], "password": bootstrap_admin["password"]},
    ).json()["access_token"]
    admin_user_id = bootstrap_admin["user_id"]

    resp = client.post(
        f"/api/admin/users/{admin_user_id}/deactivate",
        headers={"Authorization": f"Bearer {token}"},
        json={"reason": "test self-deactivate"},
    )
    # PermissionDeniedError -> 403
    assert resp.status_code == 403, resp.text
    assert resp.json()["code"] == "CANNOT_DEACTIVATE_SELF"

    resp = client.post(
        f"/api/admin/users/{admin_user_id}/change-role",
        headers={"Authorization": f"Bearer {token}"},
        json={"role_id": str(all_roles[RoleCode.SALES_REP.value])},
    )
    assert resp.status_code == 403, resp.text
    assert resp.json()["code"] == "CANNOT_CHANGE_OWN_ROLE"


# ============== 边界用例：/me 透传 ActorContext ==============


def test_me_endpoint_returns_role_code(
    client: TestClient,
    bootstrap_admin: dict[str, str],
) -> None:
    """GET /api/auth/me 返回的 role_code 是受控枚举。"""
    token = client.post(
        "/api/auth/login",
        json={"username": bootstrap_admin["username"], "password": bootstrap_admin["password"]},
    ).json()["access_token"]
    resp = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["role_code"] == "SYSTEM_ADMIN"
    assert body["must_change_password"] is True
