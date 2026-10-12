"""UserAdminService：系统管理员的账号生命周期管理。

PRD §2.1：
- 无注册入口；只能由系统管理员建号、停用、启用、重置；
- 不能设置或查询初始密码；系统生成一次性激活凭据，仅展示一次；
- 角色变更后旧 token 失效；
- 管理员不能给自己停用/降权（防自裁）。

边界：本卡不实现演示数据导入流程（T07）；但接受 `dataset_type/dataset_id` 注入，
为后续按数据集隔离留口。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.context import get_request_id
from app.core.errors import DomainError, PermissionDeniedError
from app.core.permissions import (
    MODULE_USER_ADMIN,
    permission_service,
)
from app.core.tokens import issue_token, token_revocation
from app.models import AuthEvent, Role, User
from app.models.enums import (
    AuthEventType,
    RoleCode,
    UserStatus,
)


@dataclass(frozen=True)
class CreatedAccount:
    user_id: UUID
    username: str
    one_time_credential: str  # 明文只在这里返回，调用方**必须**用完即弃
    role_code: str


class UserAdminService:
    def __init__(self, session: Session) -> None:
        self.session = session

    # ===== 建号 =====

    def create_account(
        self,
        *,
        actor_user_id: UUID,
        username: str,
        display_name: str,
        role_id: UUID,
        phone: str | None = None,
        dataset_type: str = "PRODUCTION",
        dataset_id: UUID | None = None,
    ) -> CreatedAccount:
        actor = self._load_actor(actor_user_id)
        permission_service.require_ability(
            self._actor_context(actor), MODULE_USER_ADMIN
        )

        role = self.session.get(Role, role_id)
        if role is None:
            raise DomainError(code="ROLE_NOT_FOUND", message="角色不存在")
        if role.role_code not in {rc.value for rc in RoleCode}:
            raise DomainError(code="INVALID_ROLE", message="角色不在受控枚举内")

        username_norm = username.strip().lower()
        if len(username_norm) < 3:
            raise DomainError(code="USERNAME_TOO_SHORT", message="账号至少 3 个字符")

        # 必须先放一个合法 Argon2id 占位哈希，否则 password_hash 格式 CHECK 会拒绝 flush
        import secrets

        from app.core.security import hash_password

        placeholder_hash = hash_password(secrets.token_urlsafe(24))
        user = User(
            username=username_norm,
            display_name=display_name.strip(),
            phone=phone,
            role_id=role.id,
            status=UserStatus.PENDING_ACTIVATION.value,
            must_change_password=True,
            password_hash=placeholder_hash,
            dataset_type=dataset_type,
            dataset_id=dataset_id or __import__("uuid").uuid4(),
        )
        self.session.add(user)

        try:
            self.session.flush()
        except IntegrityError as exc:
            self.session.rollback()
            raise DomainError(
                code="USERNAME_TAKEN", message="账号已存在"
            ) from exc

        activation_token = issue_token(
            user_id=user.id,
            username=user.username,
            role_code=role.role_code,
            password_version=user.password_version,
            kind="ACTIVATION",
        )

        self._record_event(
            user_id=user.id,
            actor_id=actor_user_id,
            event_type=AuthEventType.PASSWORD_RESET_ISSUED,
            payload={
                "action": "create_account",
                "username": user.username,
                "role": role.role_code,
            },
        )
        self.session.commit()

        return CreatedAccount(
            user_id=user.id,
            username=user.username,
            one_time_credential=activation_token,
            role_code=role.role_code,
        )

    # ===== 停用 / 启用 =====

    def deactivate(self, *, actor_user_id: UUID, target_user_id: UUID, reason: str) -> None:
        actor = self._load_actor(actor_user_id)
        permission_service.require_ability(self._actor_context(actor), MODULE_USER_ADMIN)
        if actor_user_id == target_user_id:
            raise PermissionDeniedError(
                code="CANNOT_DEACTIVATE_SELF",
                message="管理员不能停用自己的账号，请改用其他管理员",
            )
        target = self.session.get(User, target_user_id)
        if target is None:
            raise DomainError(code="USER_NOT_FOUND", message="账号不存在")
        if target.status == UserStatus.INACTIVE.value:
            return  # 幂等
        target.status = UserStatus.INACTIVE.value
        target.password_version = target.password_version + 1
        token_revocation.bump_user_version(target.id, target.password_version)
        self._record_event(
            user_id=target.id,
            actor_id=actor_user_id,
            event_type=AuthEventType.TOKEN_REVOKED,
            payload={"action": "deactivate", "reason": reason, "username": target.username},
        )
        self.session.commit()

    def activate_account(self, *, actor_user_id: UUID, target_user_id: UUID) -> CreatedAccount:
        actor = self._load_actor(actor_user_id)
        permission_service.require_ability(self._actor_context(actor), MODULE_USER_ADMIN)
        target = self.session.get(User, target_user_id)
        if target is None:
            raise DomainError(code="USER_NOT_FOUND", message="账号不存在")
        if target.status == UserStatus.ACTIVE.value:
            raise DomainError(code="ALREADY_ACTIVE", message="账号已是激活状态")
        role = self.session.get(Role, target.role_id)
        if role is None:
            raise DomainError(code="ROLE_NOT_FOUND", message="角色不存在")
        target.status = UserStatus.PENDING_ACTIVATION.value
        target.must_change_password = True
        target.password_version = target.password_version + 1
        token_revocation.bump_user_version(target.id, target.password_version)
        token = issue_token(
            user_id=target.id,
            username=target.username,
            role_code=role.role_code,
            password_version=target.password_version,
            kind="ACTIVATION",
        )
        self._record_event(
            user_id=target.id,
            actor_id=actor_user_id,
            event_type=AuthEventType.PASSWORD_RESET_ISSUED,
            payload={"action": "reactivate", "username": target.username, "role": role.role_code},
        )
        self.session.commit()
        return CreatedAccount(
            user_id=target.id,
            username=target.username,
            one_time_credential=token,
            role_code=role.role_code,
        )

    # ===== 角色变更 =====

    def change_role(
        self, *, actor_user_id: UUID, target_user_id: UUID, new_role_id: UUID
    ) -> None:
        actor = self._load_actor(actor_user_id)
        permission_service.require_ability(self._actor_context(actor), MODULE_USER_ADMIN)
        if actor_user_id == target_user_id:
            # 防自裁：管理员不能直接改自己角色
            raise PermissionDeniedError(
                code="CANNOT_CHANGE_OWN_ROLE", message="管理员不能直接修改自己的角色"
            )
        new_role = self.session.get(Role, new_role_id)
        if new_role is None:
            raise DomainError(code="ROLE_NOT_FOUND", message="角色不存在")
        target = self.session.get(User, target_user_id)
        if target is None:
            raise DomainError(code="USER_NOT_FOUND", message="账号不存在")

        target.role_id = new_role.id
        target.password_version = target.password_version + 1  # 角色变更踢出旧 token
        token_revocation.bump_user_version(target.id, target.password_version)
        self._record_event(
            user_id=target.id,
            actor_id=actor_user_id,
            event_type=AuthEventType.TOKEN_REVOKED,
            payload={
                "action": "change_role",
                "new_role": new_role.role_code,
                "username": target.username,
            },
        )
        self.session.commit()

    # ===== 工资发放能力显式授予（默认不放给系统管理员）=====

    def grant_payroll_disburse(self, *, actor_user_id: UUID, target_user_id: UUID) -> None:
        """显式把"工资发放"能力授予某用户。系统管理员默认无；财务有；其他角色按需。

        本卡阶段只在 `UserRoleAbility` 不存在时把能力写进 role-ability 表（T04 实现）。
        为避免越界，本方法在这里只做"权限校验+审计"，实际能力存储由 T04 接管。
        T03 阶段调用此方法会被显式拒绝，强制走 T04 的实现。
        """
        raise DomainError(
            code="NOT_IMPLEMENTED_IN_T03",
            message="payroll.disburse 能力授予归属 T04 实施；T03 阶段系统管理员默认无此能力",
        )

    # ===== 内部辅助 =====

    def _load_actor(self, user_id: UUID) -> tuple[User, Role]:
        user = self.session.get(User, user_id)
        if user is None:
            raise DomainError(code="USER_NOT_FOUND", message="操作者账号不存在")
        role = self.session.get(Role, user.role_id)
        if role is None:
            raise DomainError(code="ROLE_NOT_FOUND", message="操作者角色不存在")
        return user, role

    def _actor_context(self, loaded: tuple[User, Role]) -> Any:
        from app.core.permissions import ActorContext

        user, role = loaded
        return ActorContext(
            user_id=user.id,
            role_code=role.role_code,
            region_ids=frozenset(),
            brand_ids=frozenset(),
        )

    def _record_event(
        self,
        *,
        user_id: UUID,
        actor_id: UUID,
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
