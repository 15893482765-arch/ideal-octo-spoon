"""AuthService：登录、改密、激活、重置、登出、刷新。

PRD §2.1 约束：
- 无自助注册；首次激活强制改密；一次性凭据只展示一次；
- access/refresh token；改密 / 角色变更后旧 token 失效；
- 登录失败限流；账号锁定。

实现要点：
- 写 `auth_events` 时不写明文；payload 只放事件前后结构化字段；
- 撤销通过 `token_revocation.bump_user_version()` 一次性作废该用户所有 token；
- 限流计数器在内存；T04 迁到 Redis。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.context import get_request_id
from app.core.errors import (
    AccountLockedError,
    ActivationRequiredError,
    DomainError,
    InvalidCredentialsError,
)
from app.core.permissions import (
    PermissionService,
    permission_service,
    supplier_portal_enabled,
)
from app.core.rate_limit import login_rate_limiter
from app.core.security import hash_password, needs_rehash, verify_password
from app.core.tokens import (
    TokenError,
    decode_token,
    issue_token,
    token_revocation,
)
from app.models import AuthEvent, Role, User
from app.models.enums import (
    AuthEventType,
    RoleCode,
    UserStatus,
)


@dataclass(frozen=True)
class LoginResult:
    access_token: str
    refresh_token: str
    user: dict[str, Any]
    must_change_password: bool


@dataclass(frozen=True)
class ActivationResult:
    user_id: UUID
    username: str
    one_time_credential: str  # 明文只在这里返回，调用方**必须**用完即弃


@dataclass(frozen=True)
class ResetResult:
    user_id: UUID
    username: str
    one_time_credential: str


class AuthService:
    """所有认证操作的事务边界与审计入口。"""

    def __init__(self, session: Session, *, permissions: PermissionService | None = None) -> None:
        self.session = session
        self.permissions = permissions or permission_service

    # ============ 登录 ============

    def login(self, *, username: str, password: str) -> LoginResult:
        settings = get_settings()
        username_norm = username.strip().lower()

        if login_rate_limiter.is_locked(username=username_norm):
            remaining = login_rate_limiter.remaining_lock_seconds(username=username_norm)
            self._record_event(
                user_id=None,
                actor_id=None,
                event_type=AuthEventType.LOGIN_FAILED,
                payload={"username": username_norm, "reason": "locked", "remaining": remaining},
            )
            raise AccountLockedError(
                code="ACCOUNT_LOCKED",
                message=f"账号已被锁定，请 {remaining} 秒后重试",
                details={"remaining_seconds": remaining},
            )

        user = self._load_user_by_username(username_norm)
        if user is None:
            # 写一行失败审计但不暴露"用户不存在"
            self._record_event(
                user_id=None,
                actor_id=None,
                event_type=AuthEventType.LOGIN_FAILED,
                payload={"username": username_norm, "reason": "no_such_user"},
            )
            self._maybe_lock(username=username_norm, user_id=None, settings=settings)
            raise InvalidCredentialsError(
                code="INVALID_CREDENTIALS",
                message="账号或密码错误",
            )

        if user.status == UserStatus.INACTIVE.value:
            self._record_event(
                user_id=user.id,
                actor_id=user.id,
                event_type=AuthEventType.LOGIN_FAILED,
                payload={"username": username_norm, "reason": "inactive"},
            )
            raise AccountLockedError(
                code="ACCOUNT_INACTIVE",
                message="账号已停用，请联系系统管理员",
            )

        if user.status == UserStatus.PENDING_ACTIVATION.value:
            self._record_event(
                user_id=user.id,
                actor_id=user.id,
                event_type=AuthEventType.LOGIN_FAILED,
                payload={"username": username_norm, "reason": "pending_activation"},
            )
            raise ActivationRequiredError(
                code="ACCOUNT_PENDING_ACTIVATION",
                message="账号尚未激活，请使用一次性凭据完成首次登录",
            )

        if not verify_password(password, user.password_hash):
            self._record_event(
                user_id=user.id,
                actor_id=user.id,
                event_type=AuthEventType.LOGIN_FAILED,
                payload={"username": username_norm, "reason": "bad_password"},
            )
            self._maybe_lock(username=username_norm, user_id=user.id, settings=settings)
            raise InvalidCredentialsError(
                code="INVALID_CREDENTIALS",
                message="账号或密码错误",
            )

        # 厂商功能开关：开启时才能登录；关闭时直接拒绝
        role = self.session.get(Role, user.role_id)
        if role is None:
            raise InvalidCredentialsError(code="INVALID_CREDENTIALS", message="账号或密码错误")
        if role.role_code == RoleCode.SUPPLIER.value and not supplier_portal_enabled():
            self._record_event(
                user_id=user.id,
                actor_id=user.id,
                event_type=AuthEventType.LOGIN_FAILED,
                payload={"username": username_norm, "reason": "supplier_portal_disabled"},
            )
            raise AccountLockedError(
                code="SUPPLIER_PORTAL_DISABLED",
                message="厂商协作方功能尚未开启",
            )

        # 通过：清空失败计数、刷新 last_login_at、必要时 rehash
        login_rate_limiter.reset(username=username_norm, user_id=user.id)
        user.failed_login_count = 0
        user.locked_until = None
        user.last_login_at = datetime.now(tz=UTC)
        if needs_rehash(user.password_hash):
            user.password_hash = hash_password(password)

        access = issue_token(
            user_id=user.id,
            username=user.username,
            role_code=role.role_code,
            password_version=user.password_version,
            kind="ACCESS",
        )
        refresh = issue_token(
            user_id=user.id,
            username=user.username,
            role_code=role.role_code,
            password_version=user.password_version,
            kind="REFRESH",
        )

        self._record_event(
            user_id=user.id,
            actor_id=user.id,
            event_type=AuthEventType.LOGIN_SUCCESS,
            payload={"username": user.username, "role": role.role_code},
        )
        self.session.commit()

        return LoginResult(
            access_token=access,
            refresh_token=refresh,
            user={
                "id": str(user.id),
                "username": user.username,
                "display_name": user.display_name,
                "role_code": role.role_code,
                "role_name": role.name,
            },
            must_change_password=user.must_change_password,
        )

    # ============ 登出 / 刷新 ============

    def logout(self, *, access_token: str) -> None:
        try:
            claims = decode_token(access_token, expected_kind="ACCESS")
        except TokenError:
            return  # 已失效也按"登出成功"语义
        token_revocation.revoke_jti(claims.jti)
        self._record_event(
            user_id=claims.user_id,
            actor_id=claims.user_id,
            event_type=AuthEventType.LOGOUT,
            payload={"jti": claims.jti},
        )
        self.session.commit()

    def refresh(self, *, refresh_token: str) -> LoginResult:
        try:
            claims = decode_token(refresh_token, expected_kind="REFRESH")
        except TokenError as exc:
            raise InvalidCredentialsError(
                code="INVALID_REFRESH_TOKEN", message="刷新令牌无效或已过期"
            ) from exc

        user = self.session.get(User, claims.user_id)
        if user is None or user.status != UserStatus.ACTIVE.value:
            raise AccountLockedError(code="ACCOUNT_INACTIVE", message="账号不可用")
        role = self.session.get(Role, user.role_id)
        if role is None:
            raise InvalidCredentialsError(code="INVALID_CREDENTIALS", message="账号或密码错误")
        if role.role_code == RoleCode.SUPPLIER.value and not supplier_portal_enabled():
            raise AccountLockedError(
                code="SUPPLIER_PORTAL_DISABLED", message="厂商协作方功能尚未开启"
            )

        # 撤销旧 refresh 的 jti（一次性消费）
        token_revocation.revoke_jti(claims.jti)
        access = issue_token(
            user_id=user.id,
            username=user.username,
            role_code=role.role_code,
            password_version=user.password_version,
            kind="ACCESS",
        )
        new_refresh = issue_token(
            user_id=user.id,
            username=user.username,
            role_code=role.role_code,
            password_version=user.password_version,
            kind="REFRESH",
        )
        return LoginResult(
            access_token=access,
            refresh_token=new_refresh,
            user={
                "id": str(user.id),
                "username": user.username,
                "display_name": user.display_name,
                "role_code": role.role_code,
                "role_name": role.name,
            },
            must_change_password=user.must_change_password,
        )

    # ============ 首次激活 / 改密 ============

    def activate(
        self,
        *,
        activation_token: str,
        new_password: str,
    ) -> None:
        try:
            claims = decode_token(activation_token, expected_kind="ACTIVATION")
        except TokenError as exc:
            raise ActivationRequiredError(
                code="INVALID_ACTIVATION_TOKEN", message="激活凭据无效或已过期"
            ) from exc

        user = self.session.get(User, claims.user_id)
        if user is None:
            raise ActivationRequiredError(
                code="INVALID_ACTIVATION_TOKEN", message="激活凭据无效或已过期"
            )
        if user.status == UserStatus.INACTIVE.value:
            raise AccountLockedError(code="ACCOUNT_INACTIVE", message="账号已停用")

        # 一次性消费：撤销 token
        token_revocation.revoke_jti(claims.jti)
        token_revocation.bump_user_version(user.id, user.password_version + 1)

        user.password_hash = hash_password(new_password)
        user.status = UserStatus.ACTIVE.value
        user.must_change_password = False
        user.password_changed_at = datetime.now(tz=UTC)
        user.password_version = user.password_version + 1
        user.failed_login_count = 0
        user.locked_until = None

        self._record_event(
            user_id=user.id,
            actor_id=user.id,
            event_type=AuthEventType.ACTIVATION_CONSUMED,
            payload={"username": user.username},
        )
        self.session.commit()

    def change_password(
        self,
        *,
        actor_user_id: UUID,
        old_password: str,
        new_password: str,
    ) -> None:
        user = self.session.get(User, actor_user_id)
        if user is None:
            raise InvalidCredentialsError(code="INVALID_CREDENTIALS", message="账号或密码错误")
        if not verify_password(old_password, user.password_hash):
            raise InvalidCredentialsError(code="INVALID_CREDENTIALS", message="原密码错误")
        if old_password == new_password:
            raise DomainError(
                code="PASSWORD_REUSED",
                message="新密码不得与原密码相同",
            )

        user.password_hash = hash_password(new_password)
        user.must_change_password = False
        user.password_changed_at = datetime.now(tz=UTC)
        user.password_version = user.password_version + 1
        # 改密即踢出该用户所有 token
        token_revocation.bump_user_version(user.id, user.password_version)

        self._record_event(
            user_id=user.id,
            actor_id=user.id,
            event_type=AuthEventType.PASSWORD_CHANGED,
            payload={"username": user.username},
        )
        self.session.commit()

    # ============ 密码重置（管理员 / 一次性凭据）============

    def issue_password_reset(
        self,
        *,
        admin_user_id: UUID,
        target_username: str,
    ) -> ResetResult:
        target = self._load_user_by_username(target_username)
        if target is None:
            raise DomainError(code="USER_NOT_FOUND", message="目标账号不存在")
        token = issue_token(
            user_id=target.id,
            username=target.username,
            role_code=self._role_code(target),
            password_version=target.password_version,
            kind="PASSWORD_RESET",
        )
        self._record_event(
            user_id=target.id,
            actor_id=admin_user_id,
            event_type=AuthEventType.PASSWORD_RESET_ISSUED,
            payload={"target_username": target.username},
        )
        self.session.commit()
        return ResetResult(user_id=target.id, username=target.username, one_time_credential=token)

    def consume_password_reset(self, *, reset_token: str, new_password: str) -> None:
        try:
            claims = decode_token(reset_token, expected_kind="PASSWORD_RESET")
        except TokenError as exc:
            raise ActivationRequiredError(
                code="INVALID_RESET_TOKEN", message="重置凭据无效或已过期"
            ) from exc
        user = self.session.get(User, claims.user_id)
        if user is None or user.status == UserStatus.INACTIVE.value:
            raise ActivationRequiredError(
                code="INVALID_RESET_TOKEN", message="重置凭据无效或已过期"
            )

        token_revocation.revoke_jti(claims.jti)
        token_revocation.bump_user_version(user.id, user.password_version + 1)
        user.password_hash = hash_password(new_password)
        user.must_change_password = True
        user.password_changed_at = datetime.now(tz=UTC)
        user.password_version = user.password_version + 1
        user.failed_login_count = 0
        user.locked_until = None

        self._record_event(
            user_id=user.id,
            actor_id=user.id,
            event_type=AuthEventType.PASSWORD_RESET_CONSUMED,
            payload={"username": user.username},
        )
        self.session.commit()

    # ============ 内部辅助 ============

    def _load_user_by_username(self, username: str) -> User | None:
        stmt = select(User).where(User.username == username)
        return self.session.execute(stmt).scalar_one_or_none()

    def _role_code(self, user: User) -> str:
        role = self.session.get(Role, user.role_id)
        return role.role_code if role is not None else ""

    def _maybe_lock(
        self, *, username: str, user_id: UUID | None, settings: Any
    ) -> None:
        locked = login_rate_limiter.record_failure(
            username=username,
            user_id=user_id,
            max_failures=settings.max_login_failures,
            lockout_seconds=settings.login_lockout_seconds,
        )
        if locked and user_id is not None:
            user = self.session.get(User, user_id)
            if user is not None:
                user.is_active = user.is_active  # 占位，确保 audit 字段保持
                self._record_event(
                    user_id=user.id,
                    actor_id=None,
                    event_type=AuthEventType.ACCOUNT_LOCKED,
                    payload={"username": username},
                )

    def _record_event(
        self,
        *,
        user_id: UUID | None,
        actor_id: UUID | None,
        event_type: AuthEventType,
        payload: dict[str, Any],
    ) -> None:
        evt = AuthEvent(
            user_id=user_id,
            actor_id=actor_id,
            event_type=event_type.value,
            request_id=get_request_id() or None,
            payload=payload,
        )
        self.session.add(evt)
