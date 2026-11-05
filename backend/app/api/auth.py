"""/api/auth 路由：登录、激活、改密、登出、刷新、重置。

PRD §2.1 + §9.4 约束：
- 无注册入口（POST /api/auth/register 一律 404，由 Q010 验证）；
- 明文密码仅在请求体中进出，不得写日志/错误；
- 一次性凭据只在首次成功响应中返回（前端用完即弃）。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.deps import current_actor, db_session
from app.core.errors import (
    AuthError,
)
from app.core.metrics import (
    METRIC_AUTH_LOGIN_FAILURE,
    METRIC_RATE_LIMITED,
    registry,
)
from app.core.permissions import ActorContext
from app.core.rate_limiter import RateRule, rate_limiter
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


LOGIN_RULE_NAME = "auth.login"


def _client_ip(request: Request) -> str:
    # 只取直连对端；受信反向代理应在网关覆写为 X-Forwarded-For 末跳，避免客户端伪造
    return request.client.host if request.client else "unknown"


def login_rate_guard(request: Request, payload: LoginIn) -> None:
    """登录接口限流（Q167）：按 IP+用户名窗口计数，超限返回 429。

    与"账号锁定"互补：锁定针对单账号撞库成功路径，限流针对匿名爆破流量。
    """
    settings = get_settings()
    decision = rate_limiter.check(
        RateRule(
            LOGIN_RULE_NAME,
            settings.rate_limit_login_max,
            settings.rate_limit_login_window_seconds,
        ),
        f"{_client_ip(request)}:{payload.username.lower()}",
    )
    if not decision.allowed:
        registry.counter(METRIC_RATE_LIMITED, "").inc(bucket=LOGIN_RULE_NAME)
        raise LoginRateLimited(retry_after=decision.retry_after_seconds)


class LoginRateLimited(Exception):
    """登录触发速率限制；由专用处理器转 429。"""

    def __init__(self, *, retry_after: int) -> None:
        self.retry_after = retry_after


@router.post("/login", response_model=LoginOut)
def login(
    request: Request,
    payload: LoginIn,
    session: Session = Depends(db_session),  # noqa: B008
) -> LoginOut:
    login_rate_guard(request, payload)
    svc = AuthService(session)
    try:
        result = svc.login(username=payload.username, password=payload.password)
    except AuthError as exc:
        # 登录失败监控（PRD §9.4）：按错误码计数，不记录用户名/密码/IP 明文
        registry.counter(METRIC_AUTH_LOGIN_FAILURE, "").inc(reason=exc.code)
        raise
    return _to_login_out(result)


def register_login_rate_handler(app: FastAPI) -> None:
    """由 main 调用：把登录限流异常转成 429 JSON。"""

    @app.exception_handler(LoginRateLimited)
    async def _handler(_: Request, exc: LoginRateLimited) -> JSONResponse:
        from app.core.errors import error_response_body

        return JSONResponse(
            status_code=429,
            headers={"Retry-After": str(exc.retry_after)},
            content=error_response_body(
                "RATE_LIMITED",
                "登录尝试过于频繁，请稍后再试",
                {"retry_after_seconds": exc.retry_after},
            ),
        )


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
