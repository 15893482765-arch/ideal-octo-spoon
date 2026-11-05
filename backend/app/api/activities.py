"""/api/activities 路由：地推活动草稿/发布/执行/取消/复盘（PRD §4.7 / R39，T15）。

Router 只做鉴权、参数校验、调用 Service；冲突裁决、状态机、样品台账、事务边界全在
ActivityService（冲突口径只来自 ScheduleConflictService）。db_session 退出时按异常
决定回滚/提交，故这里不手动 commit：发布冲突整体回滚（零写入）。
"""

from __future__ import annotations

# FastAPI 的 Depends() 必须出现在参数默认值中，统一豁免 B008。
# ruff: noqa: B008
from datetime import date
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.deps import current_actor, db_session, require_ability
from app.core.permissions import MODULE_ACTIVITY, ActorContext
from app.schemas.activity import (
    ActivityCancelRequest,
    ActivityCreate,
    ActivityListItem,
    ActivityListResponse,
    ActivityReviewRequest,
    ActivityView,
    OptionListView,
    OptionView,
    Pagination,
    SampleLedgerView,
)
from app.services.activity_service import ActivityService

router = APIRouter(
    prefix="/api/activities",
    tags=["activities"],
    dependencies=[Depends(require_ability(MODULE_ACTIVITY))],
)


def _to_view(detail: dict) -> ActivityView:
    a = detail["activity"]
    return ActivityView(
        id=a.id,
        name=a.name,
        status=a.status,
        region_id=a.region_id,
        start_at=a.start_at,
        end_at=a.end_at,
        location=a.location,
        owner_user_id=a.owner_user_id,
        budget_fen=a.budget_fen,
        sample_cost_fen=a.sample_cost_fen,
        notes=a.notes,
        cancel_reason=a.cancel_reason,
        cancelled_at=a.cancelled_at,
        reviewed_at=a.reviewed_at,
        participant_user_ids=detail["participant_user_ids"],
        active_reservation_ids=detail["reservation_ids"],
        sample_ledger=[
            SampleLedgerView(
                sku_id=row.sku_id, stage=row.stage, boxes=row.boxes,
                operator_id=row.operator_id, note=row.note,
            )
            for row in detail["ledger"]
        ],
    )


def _list_item(row: dict) -> ActivityListItem:
    a = row["activity"]
    return ActivityListItem(
        id=a.id,
        name=a.name,
        status=a.status,
        region_id=a.region_id,
        region_name=row["region_name"],
        owner_user_id=a.owner_user_id,
        owner_name=row["owner_name"],
        start_at=a.start_at,
        end_at=a.end_at,
        location=a.location,
        budget_fen=a.budget_fen,
        sample_cost_fen=a.sample_cost_fen,
        participant_count=row["participant_count"],
        active_reservation_count=row["active_reservation_count"],
        notes=a.notes,
        cancel_reason=a.cancel_reason,
        reviewed_at=a.reviewed_at,
    )


@router.get("", response_model=ActivityListResponse)
def list_activities(
    region_id: UUID | None = Query(default=None),
    status: str | None = Query(default=None, description="受控状态值，单值筛选"),
    keyword: str | None = Query(default=None, max_length=128, description="活动名称模糊匹配"),
    start_from: date | None = Query(default=None, description="活动开始日（含，业务时区）"),
    start_to: date | None = Query(default=None, description="活动开始日（含，业务时区）"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    session: Session = Depends(db_session),
    actor: ActorContext = Depends(current_actor),
):
    """地推活动列表（含草稿）：行级范围与受控状态校验由服务层裁决。"""
    items, total, applied = ActivityService(session).list_activities(
        actor,
        region_id=region_id,
        status=status,
        keyword=keyword,
        start_from=start_from,
        start_to=start_to,
        page=page,
        page_size=page_size,
    )
    return ActivityListResponse(
        items=[_list_item(i) for i in items],
        pagination=Pagination(page=page, page_size=page_size, total=total),
        applied_filters=applied,
    )


@router.get("/options/users", response_model=OptionListView)
def user_options(
    region_id: UUID = Query(...),
    session: Session = Depends(db_session),
    actor: ActorContext = Depends(current_actor),
):
    """活动负责人/参与人下拉：仅返回该片区内已授权的在职用户。"""
    users = ActivityService(session).list_user_options(actor, region_id=region_id)
    return OptionListView(
        items=[OptionView(id=u.id, code=u.username, name=u.display_name) for u in users]
    )


@router.get("/options/skus", response_model=OptionListView)
def sku_options(
    region_id: UUID = Query(...),
    session: Session = Depends(db_session),
    actor: ActorContext = Depends(current_actor),
):
    """样品 SKU 下拉：与片区数据范围一致（未导入主数据时为空列表，不报错）。"""
    skus = ActivityService(session).list_sku_options(actor, region_id=region_id)
    return OptionListView(
        items=[OptionView(id=s.id, code=s.code, name=s.name) for s in skus]
    )


@router.post("", response_model=ActivityView, status_code=201)
def create_draft(
    body: ActivityCreate,
    session: Session = Depends(db_session),
    actor: ActorContext = Depends(current_actor),
):
    service = ActivityService(session)
    activity = service.create_draft(
        actor,
        region_id=body.region_id,
        name=body.name,
        start_at=body.start_at,
        end_at=body.end_at,
        location=body.location,
        owner_user_id=body.owner_user_id,
        participant_user_ids=[u for u in body.participant_user_ids],
        budget_fen=body.budget_fen,
        notes=body.notes,
        samples=[s.model_dump() for s in body.samples],
    )
    return _to_view(service.get_detail(actor, activity_id=activity.id))


@router.post("/{activity_id}/publish", response_model=ActivityView)
def publish(
    activity_id: UUID,
    session: Session = Depends(db_session),
    actor: ActorContext = Depends(current_actor),
):
    service = ActivityService(session)
    service.publish(actor, activity_id=activity_id)
    return _to_view(service.get_detail(actor, activity_id=activity_id))


@router.post("/{activity_id}/start", response_model=ActivityView)
def start(
    activity_id: UUID,
    session: Session = Depends(db_session),
    actor: ActorContext = Depends(current_actor),
):
    service = ActivityService(session)
    service.start(actor, activity_id=activity_id)
    return _to_view(service.get_detail(actor, activity_id=activity_id))


@router.post("/{activity_id}/cancel", response_model=ActivityView)
def cancel(
    activity_id: UUID,
    body: ActivityCancelRequest,
    session: Session = Depends(db_session),
    actor: ActorContext = Depends(current_actor),
):
    service = ActivityService(session)
    service.cancel(actor, activity_id=activity_id, reason=body.reason)
    return _to_view(service.get_detail(actor, activity_id=activity_id))


@router.post("/{activity_id}/review", response_model=ActivityView)
def review(
    activity_id: UUID,
    body: ActivityReviewRequest,
    session: Session = Depends(db_session),
    actor: ActorContext = Depends(current_actor),
):
    service = ActivityService(session)
    service.review(
        actor,
        activity_id=activity_id,
        sample_results=[line.model_dump() for line in body.sample_results],
    )
    return _to_view(service.get_detail(actor, activity_id=activity_id))


@router.get("/{activity_id}", response_model=ActivityView)
def detail(
    activity_id: UUID,
    session: Session = Depends(db_session),
    actor: ActorContext = Depends(current_actor),
):
    return _to_view(ActivityService(session).get_detail(actor, activity_id=activity_id))
