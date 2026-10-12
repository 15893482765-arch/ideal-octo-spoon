"""/api/auth 路由：登录、激活、改密、登出、刷新、重置。

PRD §2.1 + §9.4 约束：
- 无注册入口（POST /api/auth/register 一律 404，由 Q010 验证）；
- 明文密码仅在请求体中进出，不得写日志/错误；
- 一次性凭据只在首次成功响应中返回（前端用完即弃）。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.deps import current_actor, db_session
from app.core.errors import (
    AuthError,
)
from app.core.permissions import ActorContext
from app.services import AuthService
from app.services.auth_service import LoginResult

router = APIRouter(prefix="/api/auth", tags=["auth"])


# ============== Request / Response 模型 ==============


class LoginIn(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=8, max_length=128)


class LoginOut(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "Bearer"
    user: dict[str, Any]
    must_change_password: bool


class RefreshIn(BaseModel):
    refresh_token: str


class ActivateIn(BaseModel):
    activation_token: str
    new_password: str = Field(min_length=8, max_length=128)


class ChangePasswordIn(BaseModel):
    old_password: str = Field(min_length=8, max_length=128)
    new_password: str = Field(min_length=8, max_length=128)


class ResetConsumeIn(BaseModel):
    reset_token: str
    new_password: str = Field(min_length=8, max_length=128)


class MeOut(BaseModel):
    id: str
    username: str
    display_name: str
    role_code: str
    role_name: str
    must_change_password: bool


def _to_login_out(r: LoginResult) -> LoginOut:
    return LoginOut(
        access_token=r.access_token,
        refresh_token=r.refresh_token,
        user=r.user,
        must_change_password=r.must_change_password,
    )


# ============== 端点 ==============


@router.post("/login", response_model=LoginOut)
def login(payload: LoginIn, session: Session = Depends(db_session)) -> LoginOut:  # noqa: B008
    svc = AuthService(session)
    return _to_login_out(svc.login(username=payload.username, password=payload.password))


@router.post("/logout", status_code=204)
def logout(
    request: Request,
    session: Session = Depends(db_session),  # noqa: B008
) -> None:
    auth = request.headers.get("Authorization", "")
    if not auth.lower().startswith("bearer "):
        return None
    token = auth.split(" ", 1)[1].strip()
    AuthService(session).logout(access_token=token)


@router.post("/refresh", response_model=LoginOut)
def refresh(payload: RefreshIn, session: Session = Depends(db_session)) -> LoginOut:  # noqa: B008
    svc = AuthService(session)
    return _to_login_out(svc.refresh(refresh_token=payload.refresh_token))


@router.post("/activate", status_code=204)
def activate(payload: ActivateIn, session: Session = Depends(db_session)) -> None:  # noqa: B008
    AuthService(session).activate(
        activation_token=payload.activation_token, new_password=payload.new_password
    )


@router.post("/change-password", status_code=204)
def change_password(
    payload: ChangePasswordIn,
    actor: ActorContext = Depends(current_actor),  # noqa: B008
    session: Session = Depends(db_session),  # noqa: B008
) -> None:
    AuthService(session).change_password(
        actor_user_id=actor.user_id,
        old_password=payload.old_password,
        new_password=payload.new_password,
    )


@router.post("/password-reset/consume", status_code=204)
def consume_reset(payload: ResetConsumeIn, session: Session = Depends(db_session)) -> None:  # noqa: B008
    AuthService(session).consume_password_reset(
        reset_token=payload.reset_token, new_password=payload.new_password
    )


@router.get("/me", response_model=MeOut)
def me(
    actor: ActorContext = Depends(current_actor),  # noqa: B008
    session: Session = Depends(db_session),  # noqa: B008
) -> MeOut:
    from app.models import Role, User

    user = session.get(User, actor.user_id)
    if user is None:
        raise AuthError(code="USER_NOT_FOUND", message="账号不存在")
    role = session.get(Role, user.role_id)
    if role is None:
        raise AuthError(code="ROLE_NOT_FOUND", message="角色不存在")
    return MeOut(
        id=str(user.id),
        username=user.username,
        display_name=user.display_name,
        role_code=role.role_code,
        role_name=role.name,
        must_change_password=user.must_change_password,
    )
