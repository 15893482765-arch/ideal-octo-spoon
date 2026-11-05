"""/api/supervision 路由：督导跟访计划/现场记录/整改闭环（PRD §4.8，T16）。

Router 只鉴权、校验、装配；状态机与每周覆盖口径全在 SupervisionService。
- 督导/经理：建计划、提交现场、验证/关闭整改、查周覆盖；
- 业务员：处理自己名下整改（开始/提交）。模块能力在各 Service 方法内分别校验，
  故路由不做统一 require_ability 门禁。
"""

from __future__ import annotations

# ruff: noqa: B008  FastAPI Depends() 需出现在默认值
from datetime import date
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.deps import current_actor, db_session
from app.core.permissions import ActorContext
from app.schemas.supervision import (
    OptionListView,
    OptionView,
    Pagination,
    RectificationCloseRequest,
    RectificationListItem,
    RectificationListResponse,
    RectificationSubmitRequest,
    RectificationVerifyRequest,
    RectificationView,
    SupervisionVisitListItem,
    SupervisionVisitListResponse,
    SupervisionVisitView,
    VisitPlanRequest,
    VisitSubmitRequest,
    WeeklyCoverageView,
)
from app.services.supervision_service import SupervisionService

router = APIRouter(prefix="/api/supervision", tags=["supervision"])


def _visit_view(detail: dict) -> SupervisionVisitView:
    v = detail["visit"]
    return SupervisionVisitView(
        id=v.id,
        region_id=v.region_id,
        status=v.status,
        supervisor_id=v.supervisor_id,
        shadowed_user_id=v.shadowed_user_id,
        merchant_id=v.merchant_id,
        planned_date=v.planned_date,
        visited_at=v.visited_at,
        photo_count=v.photo_count,
        action_score=v.action_score,
        display_issue=v.display_issue,
        service_issue=v.service_issue,
        equipment_issue=v.equipment_issue,
        note=v.note,
        rectifications=[
            RectificationView(
                id=r.id,
                supervision_visit_id=r.supervision_visit_id,
                merchant_id=r.merchant_id,
                region_id=r.region_id,
                category=r.category,
                description=r.description,
                owner_user_id=r.owner_user_id,
                due_date=r.due_date,
                status=r.status,
                submitter_id=r.submitter_id,
                verifier_id=r.verifier_id,
            )
            for r in detail["rectifications"]
        ],
    )


def _visit_list_item(row: dict) -> SupervisionVisitListItem:
    v = row["visit"]
    return SupervisionVisitListItem(
        id=v.id,
        region_id=v.region_id,
        region_name=row["region_name"],
        status=v.status,
        supervisor_id=v.supervisor_id,
        supervisor_name=row["supervisor_name"],
        shadowed_user_id=v.shadowed_user_id,
        shadowed_user_name=row["shadowed_user_name"],
        merchant_id=v.merchant_id,
        merchant_name=row["merchant_name"],
        planned_date=v.planned_date,
        visited_at=v.visited_at,
        photo_count=v.photo_count,
        action_score=v.action_score,
        note=v.note,
        rectification_count=row["rectification_count"],
        pending_rectification_count=row["pending_rectification_count"],
    )


def _rect_list_item(row: dict) -> RectificationListItem:
    r = row["rectification"]
    return RectificationListItem(
        id=r.id,
        region_id=r.region_id,
        region_name=row["region_name"],
        supervision_visit_id=r.supervision_visit_id,
        merchant_id=r.merchant_id,
        merchant_name=row["merchant_name"],
        category=r.category,
        description=r.description,
        owner_user_id=r.owner_user_id,
        owner_name=row["owner_name"],
        due_date=r.due_date,
        status=r.status,
        submitter_id=r.submitter_id,
        verifier_id=r.verifier_id,
        overdue=row["overdue"],
    )


@router.get("/visits", response_model=SupervisionVisitListResponse)
def list_visits(
    region_id: UUID | None = Query(default=None),
    status: str | None = Query(default=None, description="PLANNED / SUBMITTED"),
    supervisor_id: UUID | None = Query(default=None),
    shadowed_user_id: UUID | None = Query(default=None),
    planned_from: date | None = Query(default=None, description="计划日（含）"),
    planned_to: date | None = Query(default=None, description="计划日（含）"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    session: Session = Depends(db_session),
    actor: ActorContext = Depends(current_actor),
):
    """跟访计划/记录列表：行级范围与状态校验由服务层裁决。"""
    items, total, applied = SupervisionService(session).list_visits(
        actor,
        region_id=region_id,
        status=status,
        supervisor_id=supervisor_id,
        shadowed_user_id=shadowed_user_id,
        planned_from=planned_from,
        planned_to=planned_to,
        page=page,
        page_size=page_size,
    )
    return SupervisionVisitListResponse(
        items=[_visit_list_item(i) for i in items],
        pagination=Pagination(page=page, page_size=page_size, total=total),
        applied_filters=applied,
    )


@router.get("/rectifications", response_model=RectificationListResponse)
def list_rectifications(
    region_id: UUID | None = Query(default=None),
    status: str | None = Query(default=None, description="OPEN/IN_PROGRESS/SUBMITTED/VERIFIED/CLOSED"),
    owner_user_id: UUID | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    session: Session = Depends(db_session),
    actor: ActorContext = Depends(current_actor),
):
    """片区整改单列表（督导/经理）：业务员查自己的仍走 /my-rectifications。"""
    items, total, applied = SupervisionService(session).list_rectifications(
        actor,
        region_id=region_id,
        status=status,
        owner_user_id=owner_user_id,
        page=page,
        page_size=page_size,
    )
    return RectificationListResponse(
        items=[_rect_list_item(i) for i in items],
        pagination=Pagination(page=page, page_size=page_size, total=total),
        applied_filters=applied,
    )


@router.get("/options/users", response_model=OptionListView)
def user_options(
    region_id: UUID = Query(...),
    role_code: str = Query(default="SALES_REP", description="SALES_REP / SUPERVISOR"),
    session: Session = Depends(db_session),
    actor: ActorContext = Depends(current_actor),
):
    """建计划人员下拉：该片区内已授权的在职业务员或督导。"""
    users = SupervisionService(session).list_user_options(
        actor, region_id=region_id, role_code=role_code
    )
    return OptionListView(
        items=[OptionView(id=u.id, code=u.username, name=u.display_name) for u in users]
    )


@router.post("/visits", response_model=SupervisionVisitView, status_code=201)
def plan_visit(
    body: VisitPlanRequest,
    session: Session = Depends(db_session),
    actor: ActorContext = Depends(current_actor),
):
    service = SupervisionService(session)
    visit = service.plan_visit(
        actor,
        region_id=body.region_id,
        shadowed_user_id=body.shadowed_user_id,
        merchant_id=body.merchant_id,
        planned_date=body.planned_date,
    )
    return _visit_view(service.get_visit_detail(actor, visit_id=visit.id))


@router.post("/visits/{visit_id}/submit", response_model=SupervisionVisitView)
def submit_visit(
    visit_id: UUID,
    body: VisitSubmitRequest,
    session: Session = Depends(db_session),
    actor: ActorContext = Depends(current_actor),
):
    service = SupervisionService(session)
    service.submit_visit(
        actor,
        visit_id=visit_id,
        action_score=body.action_score,
        photo_count=body.photo_count,
        visited_at=body.visited_at,
        display_issue=body.display_issue,
        service_issue=body.service_issue,
        equipment_issue=body.equipment_issue,
        note=body.note,
        issues=[i.model_dump() for i in body.issues],
    )
    return _visit_view(service.get_visit_detail(actor, visit_id=visit_id))


@router.get("/visits/{visit_id}", response_model=SupervisionVisitView)
def visit_detail(
    visit_id: UUID,
    session: Session = Depends(db_session),
    actor: ActorContext = Depends(current_actor),
):
    return _visit_view(
        SupervisionService(session).get_visit_detail(actor, visit_id=visit_id)
    )


@router.post("/rectifications/{rect_id}/start", response_model=RectificationView)
def start_rectification(
    rect_id: UUID,
    session: Session = Depends(db_session),
    actor: ActorContext = Depends(current_actor),
):
    r = SupervisionService(session).start_rectification(
        actor, rectification_id=rect_id
    )
    return _rect_view(r)


@router.post("/rectifications/{rect_id}/submit", response_model=RectificationView)
def submit_rectification(
    rect_id: UUID,
    body: RectificationSubmitRequest,
    session: Session = Depends(db_session),
    actor: ActorContext = Depends(current_actor),
):
    r = SupervisionService(session).submit_rectification(
        actor, rectification_id=rect_id, note=body.note
    )
    return _rect_view(r)


@router.post("/rectifications/{rect_id}/verify", response_model=RectificationView)
def verify_rectification(
    rect_id: UUID,
    body: RectificationVerifyRequest,
    session: Session = Depends(db_session),
    actor: ActorContext = Depends(current_actor),
):
    r = SupervisionService(session).verify_rectification(
        actor, rectification_id=rect_id, passed=body.passed, note=body.note
    )
    return _rect_view(r)


@router.post("/rectifications/{rect_id}/close", response_model=RectificationView)
def close_rectification(
    rect_id: UUID,
    body: RectificationCloseRequest,
    session: Session = Depends(db_session),
    actor: ActorContext = Depends(current_actor),
):
    r = SupervisionService(session).close_rectification(
        actor, rectification_id=rect_id, note=body.note
    )
    return _rect_view(r)


@router.get("/my-rectifications", response_model=list[RectificationView])
def my_rectifications(
    status: str | None = Query(default=None),
    session: Session = Depends(db_session),
    actor: ActorContext = Depends(current_actor),
):
    rows = SupervisionService(session).list_my_rectifications(actor, status=status)
    return [_rect_view(r) for r in rows]


@router.get("/weekly-coverage", response_model=list[WeeklyCoverageView])
def weekly_coverage(
    region_id: UUID,
    week_start: date = Query(..., description="业务周内任意日期（按上海周一归整）"),
    supervisor_id: UUID | None = Query(default=None),
    session: Session = Depends(db_session),
    actor: ActorContext = Depends(current_actor),
):
    result = SupervisionService(session).weekly_coverage(
        actor,
        region_id=region_id,
        week_start=week_start,
        supervisor_id=supervisor_id,
    )
    return [
        WeeklyCoverageView(
            region_id=c.region_id,
            supervisor_id=c.supervisor_id,
            week_start=c.week_start,
            visit_count=c.visit_count,
            distinct_rep_count=c.distinct_rep_count,
            compliant=c.compliant,
            warnings=list(c.warnings),
        )
        for c in result
    ]


def _rect_view(r) -> RectificationView:
    return RectificationView(
        id=r.id,
        supervision_visit_id=r.supervision_visit_id,
        merchant_id=r.merchant_id,
        region_id=r.region_id,
        category=r.category,
        description=r.description,
        owner_user_id=r.owner_user_id,
        due_date=r.due_date,
        status=r.status,
        submitter_id=r.submitter_id,
        verifier_id=r.verifier_id,
    )
