"""/api/schedules 路由：周视图/地图列表/建议/批量草稿/发布（PRD §4.7，T13）。

Router 只做鉴权、参数校验、调用 Service；业务规则、冲突裁决、状态机、事务边界
全在 ScheduleService / ScheduleConflictService。db_session 在退出时按异常决定
回滚或提交，故这里不手动 commit；发布任意冲突由服务层抛出后整体回滚（零写入）。
"""

from __future__ import annotations

# FastAPI 的 Depends() 必须出现在参数默认值中，统一豁免 B008。
# ruff: noqa: B008
from datetime import date
from uuid import UUID

from fastapi import APIRouter, Depends, Response
from geoalchemy2.types import Geometry
from sqlalchemy import cast, func, select
from sqlalchemy.orm import Session

from app.core.deps import current_actor, db_session, require_ability
from app.core.permissions import MODULE_SCHEDULE, ActorContext
from app.models import Merchant, User
from app.schemas.schedule import (
    AppendEntriesRequest,
    BatchView,
    CoverageGapView,
    DraftBatchCreate,
    ScheduleEntryMove,
    ScheduleEntryView,
    ScheduleMerchantItem,
    ScheduleUserOption,
    SuggestionView,
    TemplateApplyRequest,
    TemplateBulkCreate,
    TemplateSyncRequest,
    TemplateSyncResult,
    TemplateView,
    WeekView,
)
from app.services.schedule_service import ScheduleService
from app.services.schedule_template_service import ScheduleTemplateService

router = APIRouter(
    prefix="/api/schedules",
    tags=["schedules"],
    dependencies=[Depends(require_ability(MODULE_SCHEDULE))],
)


def _entry_view(
    entry,
    merchant: Merchant,
    user: User,
    *,
    projected: bool = False,
    batch_id: UUID | None = None,
    region_id: UUID | None = None,
) -> ScheduleEntryView:
    # User 没有 name 列（姓名落在 display_name）；写成 user.name 会在「有任何条目」时
    # 直接 AttributeError → week-view / map-list 500（空数据时侥幸不报）。
    return ScheduleEntryView(
        entry_id=entry.id,
        batch_id=batch_id,
        region_id=region_id,
        merchant_id=merchant.id,
        merchant_name=merchant.name,
        assigned_user_id=user.id,
        assigned_user_name=user.display_name,
        planned_date=entry.planned_date,
        start_at=entry.start_at,
        end_at=entry.end_at,
        priority=entry.priority,
        projected=projected,
    )


def _attach_coordinates(session: Session, entries: list[ScheduleEntryView]) -> None:
    """批量补齐商家经纬度（PostGIS ST_X/ST_Y），供地图打点。"""
    if not entries:
        return
    mids = {e.merchant_id for e in entries}
    geom = cast(Merchant.location, Geometry(geometry_type="POINT", srid=4326))
    rows = session.execute(
        select(Merchant.id, func.ST_X(geom), func.ST_Y(geom))
        .where(Merchant.id.in_(mids), Merchant.location.isnot(None))
    ).all()
    coord = {mid: (float(x), float(y)) for mid, x, y in rows}
    for e in entries:
        if e.merchant_id in coord:
            e.lng, e.lat = coord[e.merchant_id]


def _template_view(
    template, merchant: Merchant, user: User, region_name: str | None = None
) -> TemplateView:
    return TemplateView(
        id=template.id,
        region_id=template.region_id,
        region_name=region_name,
        weekday=template.weekday,
        merchant_id=merchant.id,
        merchant_name=merchant.name,
        assigned_user_id=user.id,
        assigned_user_name=user.display_name,
        start_time=template.start_time,
        end_time=template.end_time,
        priority=template.priority,
    )


@router.get("/options/users", response_model=list[ScheduleUserOption])
def schedule_user_options(
    region_id: UUID | None = None,
    session: Session = Depends(db_session),
    actor: ActorContext = Depends(current_actor),
):
    """排班人员下拉：经理可见片区内的全部在职业务员（含所属片区名）。

    不传 ``region_id`` 时跨可见片区返回（页面默认“全部可见片区”）。
    """
    rows = ScheduleService(session).list_assignee_options(actor, region_id=region_id)
    return [
        ScheduleUserOption(
            id=user.id,
            code=user.username,
            name=user.display_name,
            region_names=names,
        )
        for user, names in rows
    ]


@router.get("/merchants", response_model=list[ScheduleMerchantItem])
def schedule_merchant_candidates(
    region_id: UUID | None = None,
    week_start: date | None = None,
    session: Session = Depends(db_session),
    actor: ActorContext = Depends(current_actor),
):
    """排班候选商家：可见片区全部启用商家 + 本周是否已排。

    不传 ``region_id`` 时跨片区返回，避免“草稿页只看到当前片区 6 家”；
    新启用（ACTIVE）的商家下次查询即出现。
    """
    rows = ScheduleService(session).list_merchant_candidates(
        actor, region_id=region_id, week_start=week_start
    )
    return [
        ScheduleMerchantItem(
            id=merchant.id,
            code=merchant.code,
            name=merchant.name,
            grade=merchant.grade,
            region_id=merchant.region_id,
            region_name=region_name,
            current_owner_id=owner_id,
            scheduled=scheduled,
        )
        for merchant, region_name, scheduled, owner_id in rows
    ]


@router.post("/templates/sync", response_model=TemplateSyncResult)
def sync_templates(
    body: TemplateSyncRequest,
    session: Session = Depends(db_session),
    actor: ActorContext = Depends(current_actor),
):
    """把已发布排班的业务员/商家/周几回填到每周模板（历史数据补齐）。

    ``week_start`` 指定源业务周：页面「用本周排班覆盖模板」传当前查看周，
    避免后端默认取"历史最新一周"（可能已提前生成下周）导致模板与本周不一致。
    """
    result = ScheduleTemplateService(session).sync_from_published(
        actor,
        region_id=body.region_id,
        overwrite=body.overwrite,
        week_start=body.week_start,
    )
    return TemplateSyncResult(**result)


@router.get("/templates", response_model=list[TemplateView])
def list_templates(
    region_id: UUID | None = None,
    session: Session = Depends(db_session),
    actor: ActorContext = Depends(current_actor),
):
    """每周循环排班模板。

    不传 ``region_id`` 时返回经理可见片区的全部模板（带片区名），
    避免页面默认选中第一个片区而看不到其他片区（如金水 3 条）。
    """
    rows = ScheduleTemplateService(session).list_templates(actor, region_id=region_id)
    return [_template_view(*row) for row in rows]


@router.post("/templates", response_model=list[TemplateView], status_code=201)
def create_templates(
    body: TemplateBulkCreate,
    session: Session = Depends(db_session),
    actor: ActorContext = Depends(current_actor),
):
    """新增模板条目：同一天可多家商家；一周内同一商家只排一次。"""
    service = ScheduleTemplateService(session)
    service.create_entries(
        actor,
        region_id=body.region_id,
        entries=[e.model_dump() for e in body.entries],
    )
    rows = service.list_templates(actor, region_id=body.region_id)
    return [_template_view(*row) for row in rows]


@router.post("/templates/apply", response_model=BatchView)
def apply_templates(
    body: TemplateApplyRequest,
    session: Session = Depends(db_session),
    actor: ActorContext = Depends(current_actor),
):
    """按模板生成目标业务周的任务并原子发布（冲突整体拒绝、零写入）。

    - 该周未排班：整份模板生成；
    - 该周已发布且未传 ``replace``：增量补齐模板中尚未排入的商家；
    - ``replace=true``：撤销该周排班后整周重排（任务已开始执行则整体拒绝）。
    """
    batch, created_count, total_count = ScheduleTemplateService(session).apply(
        actor,
        region_id=body.region_id,
        week_start=body.week_start,
        replace=body.replace,
    )
    return BatchView(
        id=batch.id,
        week_start=batch.week_start_date,
        region_id=batch.region_id,
        status=batch.status,
        published_at=batch.published_at,
        entry_count=total_count,
        created_count=created_count,
    )


@router.delete("/templates/{template_id}", status_code=204)
def delete_template(
    template_id: UUID,
    session: Session = Depends(db_session),
    actor: ActorContext = Depends(current_actor),
):
    ScheduleTemplateService(session).delete_entry(actor, template_id)
    return Response(status_code=204)


@router.post("/draft-batches", response_model=BatchView, status_code=201)
def create_draft_batch(
    body: DraftBatchCreate,
    session: Session = Depends(db_session),
    actor: ActorContext = Depends(current_actor),
):
    service = ScheduleService(session)
    entries = [e.model_dump() for e in body.entries]
    batch = service.create_draft(
        actor,
        region_id=body.region_id,
        week_start=body.week_start,
        entries=entries,
        idempotency_key=body.idempotency_key,
    )
    return BatchView(
        id=batch.id, week_start=batch.week_start_date, region_id=batch.region_id,
        status=batch.status, published_at=batch.published_at, entry_count=len(entries),
    )


@router.post("/draft-batches/{batch_id}/publish", response_model=BatchView)
def publish_batch(
    batch_id: UUID,
    session: Session = Depends(db_session),
    actor: ActorContext = Depends(current_actor),
):
    service = ScheduleService(session)
    batch, _conflicts = service.publish(actor, batch_id=batch_id)
    count = len(service.repo.list_entries(batch.id))
    return BatchView(
        id=batch.id, week_start=batch.week_start_date, region_id=batch.region_id,
        status=batch.status, published_at=batch.published_at, entry_count=count,
    )


@router.post("/draft-batches/{batch_id}/append", response_model=BatchView)
def append_batch_entries(
    batch_id: UUID,
    body: AppendEntriesRequest,
    session: Session = Depends(db_session),
    actor: ActorContext = Depends(current_actor),
):
    """已发布周增量追加：保留原排班，再给已发布批次补排几家商家。

    与发布同一套冲突口径（自动分时段、任意冲突整体拒绝零写入）；
    追加的商家同时沉淀到每周模板，下周自动沿用。
    """
    service = ScheduleService(session)
    batch, _added = service.append_entries(
        actor, batch_id=batch_id, entries=[e.model_dump() for e in body.entries]
    )
    count = len(service.repo.list_entries(batch.id))
    return BatchView(
        id=batch.id, week_start=batch.week_start_date, region_id=batch.region_id,
        status=batch.status, published_at=batch.published_at, entry_count=count,
    )


@router.get("/week-view", response_model=WeekView)
def week_view(
    week_start: date,
    region_id: UUID | None = None,
    user_id: UUID | None = None,
    session: Session = Depends(db_session),
    actor: ActorContext = Depends(current_actor),
):
    """周视图：业务员只返回排给自己的条目；经理可按 user_id 筛选。"""
    service = ScheduleService(session)
    rows = service.week_view(
        actor, week_start=week_start, region_id=region_id, user_id=user_id
    )
    views = [_entry_view(e, m, u, batch_id=b.id, region_id=b.region_id)
             for e, m, u, b in rows]
    has_published = service.week_has_published(
        actor, week_start=week_start, region_id=region_id
    )
    if not views and not has_published:
        # 未发布周：按每周循环模板预排（只读，不写任务/占用）
        views = [
            _entry_view(p, m, u, projected=True)
            for p, m, u in service.projected_week(
                actor, week_start=week_start, region_id=region_id, user_id=user_id
            )
        ]
    _attach_coordinates(session, views)
    return WeekView(week_start=week_start, entries=views, has_published=has_published)


@router.get("/map-list", response_model=WeekView)
def map_list(
    week_start: date,
    region_id: UUID | None = None,
    user_id: UUID | None = None,
    session: Session = Depends(db_session),
    actor: ActorContext = Depends(current_actor),
):
    """地图列表：与周视图同源（含坐标），前端按坐标打点。"""
    service = ScheduleService(session)
    rows = service.map_list(
        actor, week_start=week_start, region_id=region_id, user_id=user_id
    )
    views = [_entry_view(e, m, u, batch_id=b.id, region_id=b.region_id)
             for e, m, u, b in rows]
    has_published = service.week_has_published(
        actor, week_start=week_start, region_id=region_id
    )
    if not views and not has_published:
        views = [
            _entry_view(p, m, u, projected=True)
            for p, m, u in service.projected_week(
                actor, week_start=week_start, region_id=region_id, user_id=user_id
            )
        ]
    _attach_coordinates(session, views)
    return WeekView(week_start=week_start, entries=views, has_published=has_published)


@router.post("/entries/{entry_id}/move", response_model=ScheduleEntryView)
def move_entry(
    entry_id: UUID,
    body: ScheduleEntryMove,
    session: Session = Depends(db_session),
    actor: ActorContext = Depends(current_actor),
):
    """改期：把已发布排班挪到同周另一天（同步巡访任务与人员占用）。

    典型场景：该业务员当天安排地推，经理把当天需拜访的商家整体调到其他天。
    """
    service = ScheduleService(session)
    entry = service.move_entry(
        actor,
        entry_id=entry_id,
        planned_date=body.planned_date,
        start_at=body.start_at,
        end_at=body.end_at,
    )
    merchant = session.get(Merchant, entry.merchant_id)
    user = session.get(User, entry.assigned_user_id)
    view = _entry_view(entry, merchant, user, batch_id=entry.batch_id)
    _attach_coordinates(session, [view])
    return view


@router.get("/suggestions", response_model=list[SuggestionView])
def suggestions(
    region_id: UUID,
    week_start: date,
    lng: float | None = None,
    lat: float | None = None,
    limit: int = 50,
    session: Session = Depends(db_session),
    actor: ActorContext = Depends(current_actor),
):
    service = ScheduleService(session)
    result = service.suggest(
        actor, region_id=region_id, week_start=week_start, lng=lng, lat=lat, limit=limit
    )
    return [
        SuggestionView(
            merchant_id=s.merchant.id,
            merchant_name=s.merchant.name,
            grade=s.grade,
            assignee_id=s.assignee_id,
            overdue_days=s.overdue_days,
            has_pending_replenishment=s.has_pending_replenishment,
            distance_meters=s.distance_meters,
            risk_pinned=s.risk_pinned,
            reasons=s.reasons,
        )
        for s in result
    ]


@router.get("/coverage-gap", response_model=CoverageGapView)
def coverage_gap(
    week_start: date,
    region_id: UUID | None = None,
    session: Session = Depends(db_session),
    actor: ActorContext = Depends(current_actor),
):
    """周覆盖缺口；不传 region_id 时按可见片区汇总。"""
    gap = ScheduleService(session).coverage_gap(
        actor, region_id=region_id, week_start=week_start
    )
    return CoverageGapView(
        total_active=gap.total_active,
        covered=gap.covered,
        gap=gap.gap,
        rate=gap.rate,
        missing_merchant_ids=gap.missing_merchant_ids,
    )
