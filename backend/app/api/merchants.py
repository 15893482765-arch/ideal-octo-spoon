"""/api/merchants 路由：商家中心（PRD §4.3）。

Router 只做鉴权、参数校验与调用 Service；行级范围、脱敏、重复判定、归属区间、
等级审计等业务规则全部在 MerchantService。
"""

from __future__ import annotations

# FastAPI 的 Query()/Depends() 必须出现在参数默认值中，统一豁免 B008。
# ruff: noqa: B008
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.deps import current_actor, db_session, require_ability
from app.core.permissions import MODULE_MERCHANT, ActorContext
from app.schemas.merchant import (
    AssignmentCreateRequest,
    AssignmentView,
    GradeChangeRequest,
    GradeChangeView,
    MerchantCreateRequest,
    MerchantCreateResponse,
    MerchantDetailResponse,
    MerchantListQuery,
    MerchantListResponse,
    MerchantStatusChangeRequest,
    MerchantStatusChangeView,
    Pagination,
)
from app.services.merchant_service import MerchantService

router = APIRouter(
    prefix="/api/merchants",
    tags=["merchants"],
    dependencies=[Depends(require_ability(MODULE_MERCHANT))],
)


@router.get("", response_model=MerchantListResponse)
def list_merchants(
    status: str | None = Query(default=None),
    grade: str | None = Query(default=None),
    region_id: UUID | None = Query(default=None),
    keyword: str | None = Query(default=None),
    order: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    actor: ActorContext = Depends(current_actor),
    session: Session = Depends(db_session),
) -> MerchantListResponse:
    query = MerchantListQuery(
        status=status,
        grade=grade,
        region_id=region_id,
        keyword=keyword,
        order=order,
        page=page,
        page_size=page_size,
    )
    service = MerchantService(session)
    items, total, applied = service.list_merchants(actor, query)
    return MerchantListResponse(
        items=items,
        pagination=Pagination(page=query.page, page_size=query.page_size, total=total),
        applied_filters=applied,
    )


@router.post("", response_model=MerchantCreateResponse, status_code=201)
def create_merchant(
    body: MerchantCreateRequest,
    actor: ActorContext = Depends(current_actor),
    session: Session = Depends(db_session),
) -> MerchantCreateResponse:
    service = MerchantService(session)
    merchant, warnings = service.create_merchant(actor, body)
    session.commit()
    return MerchantCreateResponse(
        id=merchant.id, code=merchant.code, duplicate_warnings=warnings
    )


@router.get("/{merchant_id}", response_model=MerchantDetailResponse)
def get_merchant(
    merchant_id: UUID,
    actor: ActorContext = Depends(current_actor),
    session: Session = Depends(db_session),
) -> MerchantDetailResponse:
    service = MerchantService(session)
    detail = service.get_merchant_detail(actor, merchant_id)
    return MerchantDetailResponse(**detail)


@router.post(
    "/{merchant_id}/assignments", response_model=AssignmentView, status_code=201
)
def assign_owner(
    merchant_id: UUID,
    body: AssignmentCreateRequest,
    actor: ActorContext = Depends(current_actor),
    session: Session = Depends(db_session),
) -> AssignmentView:
    service = MerchantService(session)
    assignment = service.assign(actor, merchant_id, body)
    session.commit()
    return MerchantService._assignment_view(assignment)


@router.post(
    "/{merchant_id}/status-change",
    response_model=MerchantStatusChangeView,
    status_code=200,
)
def change_status(
    merchant_id: UUID,
    body: MerchantStatusChangeRequest,
    actor: ActorContext = Depends(current_actor),
    session: Session = Depends(db_session),
) -> MerchantStatusChangeView:
    """负责业务员变更商家状态（PRD §4.3 状态机）；其他角色只读。"""
    service = MerchantService(session)
    merchant, old_status = service.change_status(actor, merchant_id, body)
    session.commit()
    return MerchantStatusChangeView(
        merchant_id=merchant.id,
        old_status=old_status,
        new_status=merchant.status,
        reason=body.reason,
    )


@router.post(
    "/{merchant_id}/grade-change", response_model=GradeChangeView, status_code=201
)
def change_grade(
    merchant_id: UUID,
    body: GradeChangeRequest,
    actor: ActorContext = Depends(current_actor),
    session: Session = Depends(db_session),
) -> GradeChangeView:
    service = MerchantService(session)
    change = service.change_grade(actor, merchant_id, body)
    session.commit()
    return MerchantService._grade_view(change)
