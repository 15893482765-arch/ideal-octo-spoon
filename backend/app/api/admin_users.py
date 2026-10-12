"""/api/admin/users 路由：系统管理员的账号管理。

PRD §2.1 + §2.4：
- 只有拥有 user.admin 能力的角色（=SYSTEM_ADMIN）能进入；
- 管理员不能停用/降权自己；
- 建号 / 重新激活 返回的 `one_time_credential` 是 Activation token，只展示一次，
  后续接口无法再次获取。
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Body, Depends, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.deps import db_session, require_ability
from app.core.permissions import MODULE_USER_ADMIN, ActorContext
from app.models import Role, User
from app.models.enums import UserStatus
from app.services import UserAdminService

router = APIRouter(prefix="/api/admin/users", tags=["admin/users"])


# ============== Schemas ==============


class CreateUserIn(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    display_name: str = Field(min_length=1, max_length=128)
    phone: str | None = Field(default=None, max_length=32)
    role_id: UUID


class CreateUserOut(BaseModel):
    user_id: str
    username: str
    role_code: str
    # 一次性凭据：明文 Activation token，只在创建/重新激活时返回
    one_time_credential: str


class DeactivateIn(BaseModel):
    reason: str = Field(min_length=1, max_length=200)


class UserOut(BaseModel):
    id: str
    username: str
    display_name: str
    role_code: str
    status: str
    must_change_password: bool
    is_locked: bool


# ============== 端点 ==============


@router.post("", response_model=CreateUserOut, status_code=status.HTTP_201_CREATED)
def create_user(
    payload: CreateUserIn,
    actor: ActorContext = Depends(require_ability(MODULE_USER_ADMIN)),  # noqa: B008
    session: Session = Depends(db_session),  # noqa: B008
) -> CreateUserOut:
    created = UserAdminService(session).create_account(
        actor_user_id=actor.user_id,
        username=payload.username,
        display_name=payload.display_name,
        phone=payload.phone,
        role_id=payload.role_id,
    )
    return CreateUserOut(
        user_id=str(created.user_id),
        username=created.username,
        role_code=created.role_code,
        one_time_credential=created.one_time_credential,
    )


@router.post("/{user_id}/deactivate", status_code=204)
def deactivate(
    user_id: UUID,
    payload: DeactivateIn,
    actor: ActorContext = Depends(require_ability(MODULE_USER_ADMIN)),  # noqa: B008
    session: Session = Depends(db_session),  # noqa: B008
) -> None:
    UserAdminService(session).deactivate(
        actor_user_id=actor.user_id, target_user_id=user_id, reason=payload.reason
    )


@router.post("/{user_id}/reactivate", response_model=CreateUserOut)
def reactivate(
    user_id: UUID,
    actor: ActorContext = Depends(require_ability(MODULE_USER_ADMIN)),  # noqa: B008
    session: Session = Depends(db_session),  # noqa: B008
) -> CreateUserOut:
    created = UserAdminService(session).activate_account(
        actor_user_id=actor.user_id, target_user_id=user_id
    )
    return CreateUserOut(
        user_id=str(created.user_id),
        username=created.username,
        role_code=created.role_code,
        one_time_credential=created.one_time_credential,
    )


@router.post("/{user_id}/change-role", status_code=204)
def change_role(
    user_id: UUID,
    payload: dict = Body(...),  # noqa: B008 — FastAPI body
    actor: ActorContext = Depends(require_ability(MODULE_USER_ADMIN)),  # noqa: B008
    session: Session = Depends(db_session),  # noqa: B008
) -> None:
    UserAdminService(session).change_role(
        actor_user_id=actor.user_id,
        target_user_id=user_id,
        new_role_id=UUID(str(payload["role_id"])),
    )


@router.get("", response_model=list[UserOut])
def list_users(
    actor: ActorContext = Depends(require_ability(MODULE_USER_ADMIN)),  # noqa: B008
    session: Session = Depends(db_session),  # noqa: B008
) -> list[UserOut]:
    users = session.execute(select(User).order_by(User.created_at)).scalars().all()
    out: list[UserOut] = []
    role_cache: dict[UUID, str] = {}
    for u in users:
        if u.role_id not in role_cache:
            r = session.get(Role, u.role_id)
            role_cache[u.role_id] = r.role_code if r else ""
        out.append(
            UserOut(
                id=str(u.id),
                username=u.username,
                display_name=u.display_name,
                role_code=role_cache[u.role_id],
                status=u.status,
                must_change_password=u.must_change_password,
                is_locked=u.is_active is False or u.status == UserStatus.INACTIVE.value,
            )
        )
    return out
