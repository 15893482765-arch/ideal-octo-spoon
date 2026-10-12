"""FastAPI 依赖：解析 Bearer token、构建 ActorContext、做能力校验。

红线（PRD §2.4）：行级数据范围（actor、role、region、brand）只能由服务端 JWT/session
注入；禁止路由参数或模型字段指定。
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import Depends, Header
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import (
    InvalidCredentialsError,
    PermissionDeniedError,
)
from app.core.permissions import ActorContext, PermissionError_, permission_service
from app.core.tokens import TokenError, decode_token
from app.db.session import session_scope
from app.models import Role, User, UserRegionBrandAuth
from app.models.enums import UserStatus


def db_session() -> Any:
    """路由层 DB 会话依赖：每个请求一个 session_scope。"""
    with session_scope() as s:
        yield s


def _bearer(auth_header: str | None) -> str:
    if not auth_header or not auth_header.lower().startswith("bearer "):
        raise InvalidCredentialsError(
            code="MISSING_BEARER", message="缺少 Bearer 访问令牌"
        )
    return auth_header.split(" ", 1)[1].strip()


def current_actor(
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    session: Session = Depends(db_session),  # noqa: B008
) -> ActorContext:
    """解析 Bearer access token，组装 ActorContext（注入 region/brand 行级范围）。

    路径：JWT -> users/roles/user_region_brand_auths -> ActorContext。
    """
    token = _bearer(authorization)
    try:
        claims = decode_token(token, expected_kind="ACCESS")
    except TokenError as exc:
        raise InvalidCredentialsError(
            code="INVALID_ACCESS_TOKEN", message="访问令牌无效或已过期"
        ) from exc

    user = session.get(User, claims.user_id)
    if user is None or user.status != UserStatus.ACTIVE.value:
        raise InvalidCredentialsError(code="INVALID_ACCESS_TOKEN", message="访问令牌无效或已过期")
    role = session.get(Role, user.role_id)
    if role is None:
        raise InvalidCredentialsError(code="INVALID_ACCESS_TOKEN", message="访问令牌无效或已过期")

    region_ids: set[UUID] = set()
    brand_ids: set[UUID] = set()
    supplier_region_ids: set[UUID] = set()
    supplier_brand_ids: set[UUID] = set()

    rows = session.execute(
        select(UserRegionBrandAuth).where(UserRegionBrandAuth.user_id == user.id)
    ).scalars().all()
    for r in rows:
        region_ids.add(r.region_id)
        brand_ids.add(r.brand_id)
        # 厂商：以 role_code 区分；此处把 actor 全部授权都收下，PermissionService 再切片
        supplier_region_ids.add(r.region_id)
        supplier_brand_ids.add(r.brand_id)

    return ActorContext(
        user_id=user.id,
        role_code=role.role_code,
        region_ids=frozenset(region_ids),
        brand_ids=frozenset(brand_ids),
        supplier_brand_ids=frozenset(supplier_brand_ids),
        supplier_region_ids=frozenset(supplier_region_ids),
    )


def require_ability(module: str) -> Any:
    """依赖工厂：校验 actor 拥有指定模块能力。"""

    def _checker(actor: ActorContext = Depends(current_actor)) -> ActorContext:  # noqa: B008
        try:
            permission_service.require_ability(actor, module)
        except PermissionError_ as exc:
            raise PermissionDeniedError(
                code="PERMISSION_DENIED",
                message="当前角色无权访问该接口",
                details={"required_module": module, "role": actor.role_code},
            ) from exc
        return actor

    return _checker
