"""/api/admin/audit-logs 路由：审计日志**只读**查询。

背景：T04 只落地了审计写入（`AuditService`）与模型，`docs/audit-idempotency-files.md`
把「审计查询 / 导出接口」列为后续卡（T18/T27）事项。本路由为「审计日志」页面补齐最小
只读查询能力：
- `GET /api/admin/audit-logs`：业务审计（`audit_logs`）分页 + 多条件过滤；
- `GET /api/admin/audit-logs/auth-events`：认证域事件（`auth_events`，PRD §2.1 / R30）；
- `GET /api/admin/audit-logs/facets`：可选值（对象类型 distinct + 事件类型枚举）。

红线：
- **只读**：本路由不提供任何 UPDATE/DELETE 路径，审计表只增不改（PRD §5/R30）；
- 能力门禁 `audit`：按 PRD 角色职责，审计日志只对系统管理员开放；
- `before_json / after_json / payload` 在写入前已过 `redact()`，此处原样返回，
  不回补、不推导任何被脱敏的敏感值；
- 分页上限 200 条/页，避免整表拉取。
"""

# FastAPI 的 Query()/Depends() 必须出现在参数默认值中，统一豁免 B008。
# ruff: noqa: B008
from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session, aliased

from app.core.deps import db_session, require_ability
from app.core.errors import DomainError
from app.core.permissions import MODULE_AUDIT, ActorContext
from app.models import AuditLog, AuthEvent, User
from app.models.enums import AuthEventType
from app.services.audit_service import (
    EVENT_CREATE,
    EVENT_DELETE_DENIED,
    EVENT_STATE_TRANSITION,
    EVENT_UPDATE,
)

router = APIRouter(prefix="/api/admin/audit-logs", tags=["admin/audit"])

MAX_PAGE_SIZE = 200
# 与 audit_logs_event_type_check 约束一致
_AUDIT_EVENT_TYPES = (
    EVENT_CREATE,
    EVENT_UPDATE,
    EVENT_STATE_TRANSITION,
    EVENT_DELETE_DENIED,
)
_AUTH_EVENT_TYPES = tuple(e.value for e in AuthEventType)


# ============== Schemas ==============


class AuditLogOut(BaseModel):
    id: str
    object_type: str
    object_id: str
    event_type: str
    actor_id: str | None
    actor_username: str | None
    actor_name: str | None
    occurred_at: datetime
    request_id: str | None
    reason: str | None
    before_json: dict | None
    after_json: dict | None
    payload: dict


class AuditLogPageOut(BaseModel):
    items: list[AuditLogOut] = Field(default_factory=list)
    total: int
    limit: int
    offset: int


class AuthEventOut(BaseModel):
    id: str
    event_type: str
    user_id: str | None
    user_username: str | None
    user_name: str | None
    actor_id: str | None
    actor_username: str | None
    actor_name: str | None
    occurred_at: datetime
    request_id: str | None
    payload: dict


class AuthEventPageOut(BaseModel):
    items: list[AuthEventOut] = Field(default_factory=list)
    total: int
    limit: int
    offset: int


class AuditFacetsOut(BaseModel):
    object_types: list[str] = Field(default_factory=list)
    event_types: list[str] = Field(default_factory=list)
    auth_event_types: list[str] = Field(default_factory=list)


# ============== 内部辅助 ==============


def _page_size(limit: int) -> int:
    if limit > MAX_PAGE_SIZE:
        raise DomainError(
            code="AUDIT_PAGE_TOO_LARGE",
            message=f"单页最多 {MAX_PAGE_SIZE} 条",
            details={"limit": limit},
        )
    return limit


# ============== 端点 ==============


@router.get("", response_model=AuditLogPageOut)
def list_audit_logs(
    object_type: str | None = Query(default=None, max_length=48),
    object_id: UUID | None = Query(default=None),
    event_type: str | None = Query(default=None, max_length=24),
    actor_keyword: str | None = Query(default=None, max_length=64),
    occurred_from: datetime | None = Query(default=None),
    occurred_to: datetime | None = Query(default=None),
    keyword: str | None = Query(default=None, max_length=128),
    limit: int = Query(default=50, ge=1),
    offset: int = Query(default=0, ge=0),
    actor: ActorContext = Depends(require_ability(MODULE_AUDIT)),  # noqa: B008
    session: Session = Depends(db_session),  # noqa: B008
) -> AuditLogPageOut:
    """业务审计分页查询：对象 / 事件 / 操作人 / 时间区间 / 原因关键字。"""
    limit = _page_size(limit)
    if event_type is not None and event_type not in _AUDIT_EVENT_TYPES:
        raise DomainError(
            code="AUDIT_EVENT_TYPE_INVALID",
            message="审计事件类型非法",
            details={"allowed": list(_AUDIT_EVENT_TYPES)},
        )

    conditions = []
    if object_type:
        conditions.append(AuditLog.object_type == object_type)
    if object_id:
        conditions.append(AuditLog.object_id == object_id)
    if event_type:
        conditions.append(AuditLog.event_type == event_type)
    if occurred_from:
        conditions.append(AuditLog.occurred_at >= occurred_from)
    if occurred_to:
        conditions.append(AuditLog.occurred_at <= occurred_to)
    if keyword:
        conditions.append(AuditLog.reason.ilike(f"%{keyword}%"))

    stmt = (
        select(AuditLog, User.username, User.display_name)
        .outerjoin(User, User.id == AuditLog.actor_id)
        .where(*conditions)
    )
    if actor_keyword:
        like = f"%{actor_keyword}%"
        stmt = stmt.where(User.username.ilike(like) | User.display_name.ilike(like))

    total = session.scalar(
        select(func.count()).select_from(stmt.subquery())
    ) or 0
    rows = session.execute(
        stmt.order_by(AuditLog.occurred_at.desc(), AuditLog.id.desc())
        .limit(limit)
        .offset(offset)
    ).all()

    return AuditLogPageOut(
        items=[
            AuditLogOut(
                id=str(log.id),
                object_type=log.object_type,
                object_id=str(log.object_id),
                event_type=log.event_type,
                actor_id=str(log.actor_id) if log.actor_id else None,
                actor_username=username,
                actor_name=display_name,
                occurred_at=log.occurred_at,
                request_id=log.request_id,
                reason=log.reason,
                before_json=log.before_json,
                after_json=log.after_json,
                payload=log.payload or {},
            )
            for log, username, display_name in rows
        ],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/auth-events", response_model=AuthEventPageOut)
def list_auth_events(
    event_type: str | None = Query(default=None, max_length=32),
    user_keyword: str | None = Query(default=None, max_length=64),
    occurred_from: datetime | None = Query(default=None),
    occurred_to: datetime | None = Query(default=None),
    limit: int = Query(default=50, ge=1),
    offset: int = Query(default=0, ge=0),
    actor: ActorContext = Depends(require_ability(MODULE_AUDIT)),  # noqa: B008
    session: Session = Depends(db_session),  # noqa: B008
) -> AuthEventPageOut:
    """认证域事件分页查询（登录失败/改密/激活凭据签发与消费/锁定/踢出）。"""
    limit = _page_size(limit)
    if event_type is not None and event_type not in _AUTH_EVENT_TYPES:
        raise DomainError(
            code="AUTH_EVENT_TYPE_INVALID",
            message="认证事件类型非法",
            details={"allowed": list(_AUTH_EVENT_TYPES)},
        )

    target = aliased(User)
    actor_user = aliased(User)

    conditions = []
    if event_type:
        conditions.append(AuthEvent.event_type == event_type)
    if occurred_from:
        conditions.append(AuthEvent.occurred_at >= occurred_from)
    if occurred_to:
        conditions.append(AuthEvent.occurred_at <= occurred_to)

    stmt = (
        select(
            AuthEvent,
            target.username,
            target.display_name,
            actor_user.username,
            actor_user.display_name,
        )
        .outerjoin(target, target.id == AuthEvent.user_id)
        .outerjoin(actor_user, actor_user.id == AuthEvent.actor_id)
        .where(*conditions)
    )
    if user_keyword:
        like = f"%{user_keyword}%"
        stmt = stmt.where(
            target.username.ilike(like)
            | target.display_name.ilike(like)
            | actor_user.username.ilike(like)
            | actor_user.display_name.ilike(like)
        )

    total = session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = session.execute(
        stmt.order_by(AuthEvent.occurred_at.desc(), AuthEvent.id.desc())
        .limit(limit)
        .offset(offset)
    ).all()

    return AuthEventPageOut(
        items=[
            AuthEventOut(
                id=str(evt.id),
                event_type=evt.event_type,
                user_id=str(evt.user_id) if evt.user_id else None,
                user_username=t_username,
                user_name=t_name,
                actor_id=str(evt.actor_id) if evt.actor_id else None,
                actor_username=a_username,
                actor_name=a_name,
                occurred_at=evt.occurred_at,
                request_id=evt.request_id,
                payload=evt.payload or {},
            )
            for evt, t_username, t_name, a_username, a_name in rows
        ],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/facets", response_model=AuditFacetsOut)
def audit_facets(
    actor: ActorContext = Depends(require_ability(MODULE_AUDIT)),  # noqa: B008
    session: Session = Depends(db_session),  # noqa: B008
) -> AuditFacetsOut:
    """筛选项：对象类型取库中出现过的值，事件类型取受控枚举（前端下拉用）。"""
    object_types = session.execute(
        select(AuditLog.object_type).distinct().order_by(AuditLog.object_type.asc())
    ).scalars().all()
    return AuditFacetsOut(
        object_types=list(object_types),
        event_types=list(_AUDIT_EVENT_TYPES),
        auth_event_types=list(_AUTH_EVENT_TYPES),
    )
