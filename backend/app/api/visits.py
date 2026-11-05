"""/api/visits 路由：巡访任务、打卡、有效性与修订（PRD §4.6 / §4.8 / T11）。

Router 只做鉴权、参数校验与调用 Service；有效性裁决、200 米阈值、行级范围、
审计留痕等业务规则全部在 VisitService / VisitValidityService。
照片走"先上传后绑定"：前端先经文件服务拿到 file_id，再随打卡提交传给巡访记录。
"""

from __future__ import annotations

# FastAPI 的 Depends() 必须出现在参数默认值中，统一豁免 B008。
# ruff: noqa: B008
from datetime import date
from uuid import UUID

from fastapi import APIRouter, Depends, File, Query, UploadFile, status
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.deps import current_actor, db_session, require_ability
from app.core.errors import DomainError
from app.core.permissions import MODULE_VISIT, ActorContext
from app.models import User
from app.schemas.visit import (
    VisitJudge,
    VisitPhotoView,
    VisitRecordView,
    VisitReplenishmentLineView,
    VisitRevisionCreate,
    VisitRevisionView,
    VisitSalesLineView,
    VisitSkuOption,
    VisitSubmit,
    VisitTaskCreate,
    VisitTaskView,
)
from app.services.file_service import BIZ_PHOTO, FileService
from app.services.storage import get_storage_backend
from app.services.visit_service import VisitService

router = APIRouter(
    prefix="/api/visits",
    tags=["visits"],
    dependencies=[Depends(require_ability(MODULE_VISIT))],
)


def _record_view(
    service: VisitService, record, photos, revisions, lng_lat,
    sales_lines=(), replenishments=(),
) -> VisitRecordView:
    lng, lat = (lng_lat if lng_lat else (None, None))
    return VisitRecordView(
        id=record.id,
        visit_task_id=record.visit_task_id,
        merchant_id=record.merchant_id,
        user_id=record.user_id,
        source=record.source,
        checkin_at=record.checkin_at,
        checkout_at=record.checkout_at,
        device_checkin_at=record.device_checkin_at,
        lng=lng,
        lat=lat,
        accuracy_meters=record.accuracy_meters,
        distance_meters=record.distance_meters,
        display_pct=record.display_pct,
        display_threshold_pct=record.display_threshold_pct,
        display_met=record.display_met,
        freezer_status=record.freezer_status,
        device_issue=record.device_issue,
        conclusion=record.conclusion,
        photo_count=record.photo_count,
        note=record.note,
        is_official=record.is_official,
        status=record.status,
        validity=record.validity,
        judged_by_id=record.judged_by_id,
        judged_at=record.judged_at,
        judge_reason=record.judge_reason,
        submitted_at=record.submitted_at,
        photos=[
            VisitPhotoView(id=p.id, file_id=p.file_id, photo_kind=p.photo_kind)
            for p in photos
        ],
        revisions=[
            VisitRevisionView(
                id=r.id, author_id=r.author_id, supplement=r.supplement,
                created_at=r.created_at,
            )
            for r in revisions
        ],
        sales_lines=[
            VisitSalesLineView(
                id=s.id, sku_id=s.sku_id, reported_boxes=s.reported_boxes,
                feedback=s.feedback, out_of_stock=s.out_of_stock,
            )
            for s in sales_lines
        ],
        replenishment_lines=[
            VisitReplenishmentLineView(
                order_id=o.id, code=o.code, sku_id=o.sku_id,
                requested_boxes=o.requested_boxes,
                expected_delivery_date=o.promised_delivery_date,
                status=o.status,
            )
            for o in replenishments
        ],
    )


# ---------------- 任务 ----------------
@router.post("/tasks", response_model=VisitTaskView, status_code=status.HTTP_201_CREATED)
def create_task(
    body: VisitTaskCreate,
    actor: ActorContext = Depends(current_actor),
    session: Session = Depends(db_session),
) -> VisitTaskView:
    """创建巡访任务（周排班/临时/整改/督导）。"""
    service = VisitService(session)
    task = service.create_task(actor, body)
    session.commit()
    return VisitTaskView.model_validate(task, from_attributes=True)


@router.get("/skus", response_model=list[VisitSkuOption])
def list_visit_skus(
    merchant_id: UUID,
    actor: ActorContext = Depends(current_actor),
    session: Session = Depends(db_session),
):
    """巡访提报补货/销量行可选 SKU（同品牌、同数据范围；无主数据返回空列表）。"""
    skus = VisitService(session).list_sku_options(actor, merchant_id=merchant_id)
    return [VisitSkuOption(id=s.id, code=s.code, name=s.name) for s in skus]


@router.get("/tasks", response_model=list[VisitTaskView])
def list_tasks(
    on_date: date | None = Query(default=None, description="计划日期（默认全部）"),
    actor: ActorContext = Depends(current_actor),
    session: Session = Depends(db_session),
) -> list[VisitTaskView]:
    """责任人的巡访任务列表（移动端工作台：时间窗 + 导航点）。"""
    service = VisitService(session)
    tasks = service.list_my_tasks(actor, on_date)
    return [VisitTaskView.model_validate(t, from_attributes=True) for t in tasks]


@router.get("/tasks/{task_id}", response_model=dict)
def task_detail(
    task_id: UUID,
    actor: ActorContext = Depends(current_actor),
    session: Session = Depends(db_session),
) -> dict:
    """任务详情：含商家导航点（经纬度）与服务端计划时间窗。"""
    service = VisitService(session)
    task, merchant = service.get_task_detail(actor, task_id)
    lng_lat = service.repo.merchant_lng_lat(merchant)
    return {
        "task": VisitTaskView.model_validate(task, from_attributes=True).model_dump(mode="json"),
        "merchant": {
            "id": str(merchant.id),
            "code": merchant.code,
            "name": merchant.name,
            "lng": lng_lat[0] if lng_lat else None,
            "lat": lng_lat[1] if lng_lat else None,
        },
    }


# ---------------- 照片（先上传后绑定） ----------------
@router.post("/photos", status_code=status.HTTP_201_CREATED)
async def upload_visit_photo(
    file: UploadFile = File(...),
    actor: ActorContext = Depends(current_actor),
    session: Session = Depends(db_session),
) -> dict:
    """上传巡访照片，返回 file_id，随打卡提交的 `photos` 一起绑定。

    与合同附件同源（FileService 白名单 + SHA-256 + 不可覆盖）；照片只增不改，
    巡访记录一经提交不可覆盖（PRD §4.6 红线）。

    两个必须与巡访记录对齐的点（否则 `VisitService._bind_photo` 会拒绝绑定）：
    - **数据域**：随打卡记录一致，取上传人自身的 dataset（演示账号→DEMO 数据集）；
    - **扫描状态**：未启用扫描的环境直接置 SKIPPED（附录 A 首版无真实扫描），
      否则文件停在 PENDING，任何照片都无法绑定。
    """
    data = await file.read()
    owner = session.get(User, actor.user_id)
    if owner is None:
        raise DomainError(code="VISIT_PHOTO_OWNER_INVALID", message="上传人不存在")
    service = FileService(session, get_storage_backend())
    obj = service.save_upload(
        owner_user_id=actor.user_id,
        filename=file.filename or "photo.jpg",
        content_type=file.content_type or "image/jpeg",
        data=data,
        biz_type=BIZ_PHOTO,
        dataset_type=owner.dataset_type,
        dataset_id=owner.dataset_id,
    )
    if not get_settings().file_scan_enabled:
        service.mark_scan_status(file_id=obj.id, status="SKIPPED")
    session.commit()
    return FileService.describe(obj)


# ---------------- 打卡提交 ----------------
@router.post(
    "/tasks/{task_id}/submit",
    response_model=VisitRecordView,
    status_code=status.HTTP_201_CREATED,
)
def submit_visit(
    task_id: UUID,
    body: VisitSubmit,
    actor: ActorContext = Depends(current_actor),
    session: Session = Depends(db_session),
) -> VisitRecordView:
    """提交巡访打卡（visit.submit）：定位、到离店、照片绑定与有效性裁决。"""
    service = VisitService(session)
    record = service.submit(actor, task_id, body)
    photos = service.repo.list_photos(record.id)
    revisions = service.repo.list_revisions(record.id)
    lng_lat = service.repo.record_lng_lat(record)
    sales_lines = service.repo.list_sales_lines(record.id)
    replenishments = service.repo.list_replenishments_for_record(record.id)
    session.commit()
    return _record_view(
        service, record, photos, revisions, lng_lat, sales_lines, replenishments
    )


@router.get("/records/{record_id}", response_model=VisitRecordView)
def record_detail(
    record_id: UUID,
    actor: ActorContext = Depends(current_actor),
    session: Session = Depends(db_session),
) -> VisitRecordView:
    """巡访记录详情：原始证据（定位/时间/照片）+ 修订追加。"""
    service = VisitService(session)
    record, photos, revisions, lng_lat, sales_lines, replenishments = (
        service.get_record_detail(actor, record_id)
    )
    return _record_view(
        service, record, photos, revisions, lng_lat, sales_lines, replenishments
    )


# ---------------- 经理判定 / 修订 ----------------
@router.post("/records/{record_id}/judge", response_model=VisitRecordView)
def judge_record(
    record_id: UUID,
    body: VisitJudge,
    actor: ActorContext = Depends(current_actor),
    session: Session = Depends(db_session),
) -> VisitRecordView:
    """经理判定异常巡访：认可计有效或判异常不计（留审计）。"""
    service = VisitService(session)
    record = service.judge(actor, record_id, body)
    photos = service.repo.list_photos(record.id)
    revisions = service.repo.list_revisions(record.id)
    lng_lat = service.repo.record_lng_lat(record)
    sales_lines = service.repo.list_sales_lines(record.id)
    replenishments = service.repo.list_replenishments_for_record(record.id)
    session.commit()
    return _record_view(
        service, record, photos, revisions, lng_lat, sales_lines, replenishments
    )


@router.post(
    "/records/{record_id}/revisions",
    response_model=VisitRevisionView,
    status_code=status.HTTP_201_CREATED,
)
def add_revision(
    record_id: UUID,
    body: VisitRevisionCreate,
    actor: ActorContext = Depends(current_actor),
    session: Session = Depends(db_session),
) -> VisitRevisionView:
    """追加补充说明（原始证据不可覆盖）。"""
    service = VisitService(session)
    revision = service.add_revision(actor, record_id, body)
    session.commit()
    return VisitRevisionView.model_validate(revision, from_attributes=True)
