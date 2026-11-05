"""T11 巡访任务、定位、照片与有效性（PRD §4.6 / §4.8；红线：距离/覆盖率/绩效）。

覆盖：
- Q055 200 米内 + 有照片 → 有效巡访（VALID/RECOGNIZED）
- Q056 200 米外 → 异常待判定（ABNORMAL_PENDING，不计绩效）
- Q057 缺照片 → 异常待判定（ABNORMAL_PENDING）
- Q058 非责任人提交 → 403（PermissionDeniedError + HTTP 403）
- Q059 原始证据不可覆盖（重复提交 409；无更新接口；原始值不变）
- Q060 经理判定留审计（STATE_TRANSITION + reason + actor）
另含：任务来源（周排班/临时/整改/督导）、督导跟访标记不计覆盖率、提交幂等、
距离由服务端 PostGIS 重算（前端上报距离不作数）。
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from uuid import uuid4

import pytest
from geoalchemy2 import WKTElement
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from tests.test_t09_contract import _ctx, _merchant, _user, _world

from app.core.errors import ConflictError, PermissionDeniedError
from app.models import (
    AuditLog,
    FileObject,
    MerchantAssignment,
    VisitPhoto,
    VisitRecord,
    VisitRecordStatus,
    VisitRevision,
    VisitTask,
)
from app.models.enums import RoleCode
from app.schemas.visit import (
    VisitJudge,
    VisitPhotoInput,
    VisitRevisionCreate,
    VisitSubmit,
    VisitTaskCreate,
)
from app.services.visit_service import VisitService
from app.services.visit_validity_service import (
    ABNORMAL_PENDING,
    INVALID,
    VALID,
)

# 上海人民广场附近：纬度 1°≈111 km → 200 m ≈ 0.0018°
MERCHANT_LNG_LAT = (121.4737, 31.2304)
NEAR_LNG_LAT = (121.4737, 31.2314)   # ≈111 m，阈值内
FAR_LNG_LAT = (121.4737, 31.2334)    # ≈333 m，超阈值


# ----------------------------- 造数辅助 -----------------------------


def _merchant_at(session: Session, w: dict, lng: float, lat: float):
    m = _merchant(session, w, name="沁爽便利店")
    m.location = WKTElement(f"POINT({lng} {lat})", srid=4326)
    session.flush()
    return m


def _assign_primary(session: Session, merchant, user) -> MerchantAssignment:
    """建立主责任归属（生效区间开放至今），供行级范围校验。"""
    a = MerchantAssignment(
        merchant_id=merchant.id,
        owner_user_id=user.id,
        assignment_type="PRIMARY",
        valid_range=func.tstzrange(
            func.to_timestamp(datetime(2026, 1, 1, tzinfo=UTC).timestamp()), None, "[)"
        ),
        dataset_type="PRODUCTION",
        dataset_id=merchant.dataset_id,
    )
    session.add(a)
    session.flush()
    return a


def _photo_file(
    session: Session, w: dict, owner, *, content_type: str = "image/jpeg"
) -> FileObject:
    """模拟"先上传"：FileService 已持久化的图片文件（这里直接落元数据）。"""
    f = FileObject(
        object_key=f"PHOTO/{uuid4().hex}/storefront.jpg",
        original_filename="storefront.jpg",
        content_type=content_type,
        size_bytes=204800,
        sha256=uuid4().hex + uuid4().hex,
        biz_type="PHOTO",
        owner_user_id=owner.id,
        scan_status="CLEAN",
        dataset_type="PRODUCTION",
        dataset_id=w["ds"],
    )
    session.add(f)
    session.flush()
    return f


def _setup(session: Session, *, lng: float, lat: float):
    """建世界 + 业务员 + 经理 + 带坐标商家 + 主责任归属。"""
    w = _world(session)
    rep = _user(session, w["ds"], RoleCode.SALES_REP, "rep")
    mgr = _user(session, w["ds"], RoleCode.REGIONAL_MANAGER, "mgr")
    merchant = _merchant_at(session, w, lng, lat)
    _assign_primary(session, merchant, rep)
    rep_actor = _ctx(rep, RoleCode.SALES_REP.value, frozenset({w["region"].id}))
    mgr_actor = _ctx(mgr, RoleCode.REGIONAL_MANAGER.value, frozenset({w["region"].id}))
    return w, rep, mgr, merchant, rep_actor, mgr_actor


def _task(session: Session, actor, merchant, **overrides):
    payload = dict(
        merchant_id=merchant.id,
        planned_date=date(2026, 10, 13),
        window_start_at=datetime(2026, 10, 13, 1, 0, tzinfo=UTC),
        window_end_at=datetime(2026, 10, 13, 9, 0, tzinfo=UTC),
        priority=2,
    )
    payload.update(overrides)
    return VisitService(session).create_task(actor, VisitTaskCreate(**payload))


def _submit_payload(lng: float, lat: float, photos: list[VisitPhotoInput] | None = None,
                    **overrides) -> VisitSubmit:
    data = dict(lng=lng, lat=lat, accuracy_meters=12, photos=photos or [])
    data.update(overrides)
    return VisitSubmit(**data)


# ----------------------------- Q055 -----------------------------


def test_q055_visit_within_200m_is_valid(session: Session) -> None:
    """200 米内 + 有效照片 → 有效巡访，任务完成。"""
    w, rep, _mgr, merchant, rep_actor, _mgr_actor = _setup(
        session, lng=MERCHANT_LNG_LAT[0], lat=MERCHANT_LNG_LAT[1]
    )
    task = _task(session, rep_actor, merchant)
    photo = _photo_file(session, w, rep)

    record = VisitService(session).submit(
        rep_actor,
        task.id,
        _submit_payload(
            *NEAR_LNG_LAT,
            photos=[VisitPhotoInput(file_id=photo.id, photo_kind="STOREFRONT")],
            idempotency_key=f"q055-{uuid4().hex[:8]}",
        ),
    )
    session.flush()

    assert record.validity == VALID
    assert record.status == VisitRecordStatus.RECOGNIZED.value
    assert record.distance_meters is not None
    assert record.distance_meters <= 200
    # 距离由服务端重算，不接受前端上报
    assert record.checkin_at is not None
    assert isinstance(record.checkin_at, datetime)
    assert task.status == "COMPLETED"
    assert record.is_official is True  # 一任务至多一条正式记录
    assert session.get(VisitRecord, record.id).photo_count == 1


# ----------------------------- Q056 -----------------------------


def test_q056_visit_beyond_200m_is_abnormal_pending(session: Session) -> None:
    """200 米外 → 异常待经理判定，判定前不计绩效/覆盖率。"""
    w, rep, _mgr, merchant, rep_actor, _mgr_actor = _setup(
        session, lng=MERCHANT_LNG_LAT[0], lat=MERCHANT_LNG_LAT[1]
    )
    task = _task(session, rep_actor, merchant)
    photo = _photo_file(session, w, rep)

    record = VisitService(session).submit(
        rep_actor,
        task.id,
        _submit_payload(
            *FAR_LNG_LAT,
            photos=[VisitPhotoInput(file_id=photo.id)],
        ),
    )
    session.flush()

    assert record.validity == ABNORMAL_PENDING
    assert record.status == VisitRecordStatus.ABNORMAL_PENDING.value
    assert record.distance_meters is not None and record.distance_meters > 200
    # 未判定前不得计入有效（绩效/覆盖率口径）
    assert record.validity != VALID
    assert task.status == "ABNORMAL"


# ----------------------------- Q057 -----------------------------


def test_q057_visit_without_photo_is_abnormal_pending(session: Session) -> None:
    """缺照片 → 异常待判定（即使位置在阈值内）。"""
    _w, _rep, _mgr, merchant, rep_actor, _mgr_actor = _setup(
        session, lng=MERCHANT_LNG_LAT[0], lat=MERCHANT_LNG_LAT[1]
    )
    task = _task(session, rep_actor, merchant)

    record = VisitService(session).submit(
        rep_actor, task.id, _submit_payload(*NEAR_LNG_LAT, photos=[])
    )
    session.flush()

    assert record.validity == ABNORMAL_PENDING
    assert record.status == VisitRecordStatus.ABNORMAL_PENDING.value
    assert record.photo_count == 0
    assert record.distance_meters is not None and record.distance_meters <= 200


# ----------------------------- Q058 -----------------------------


def test_q058_non_assignee_submit_forbidden(session: Session) -> None:
    """非责任人提交被拒（403）：服务端按任务责任人校验，不采信客户端身份。"""
    w = _world(session)
    rep = _user(session, w["ds"], RoleCode.SALES_REP, "rep")
    other = _user(session, w["ds"], RoleCode.SALES_REP, "other")
    merchant = _merchant_at(session, w, *MERCHANT_LNG_LAT)
    _assign_primary(session, merchant, rep)
    rep_actor = _ctx(rep, RoleCode.SALES_REP.value, frozenset({w["region"].id}))
    other_actor = _ctx(other, RoleCode.SALES_REP.value, frozenset({w["region"].id}))

    task = _task(session, rep_actor, merchant)
    photo = _photo_file(session, w, rep)

    with pytest.raises(PermissionDeniedError) as exc:
        VisitService(session).submit(
            other_actor,
            task.id,
            _submit_payload(*NEAR_LNG_LAT, photos=[VisitPhotoInput(file_id=photo.id)]),
        )
    assert exc.value.code == "VISIT_NOT_ASSIGNEE"
    # 未产生任何记录
    assert session.scalars(
        select(VisitRecord).where(VisitRecord.visit_task_id == task.id)
    ).first() is None


def test_q058_http_non_assignee_returns_403(session: Session, test_env: None) -> None:
    """HTTP 层：非责任人提交返回 403 FORBIDDEN（统一错误体）。"""
    from fastapi.testclient import TestClient

    from app.core.deps import current_actor, db_session
    from app.main import create_app

    w = _world(session)
    rep = _user(session, w["ds"], RoleCode.SALES_REP, "rep")
    other = _user(session, w["ds"], RoleCode.SALES_REP, "other")
    merchant = _merchant_at(session, w, *MERCHANT_LNG_LAT)
    _assign_primary(session, merchant, rep)
    rep_actor = _ctx(rep, RoleCode.SALES_REP.value, frozenset({w["region"].id}))
    other_actor = _ctx(other, RoleCode.SALES_REP.value, frozenset({w["region"].id}))
    task = _task(session, rep_actor, merchant)

    app = create_app()
    app.dependency_overrides[current_actor] = lambda: other_actor

    def _session_override():
        yield session

    app.dependency_overrides[db_session] = _session_override
    with TestClient(app) as client:
        resp = client.post(
            f"/api/visits/tasks/{task.id}/submit",
            json={"lng": NEAR_LNG_LAT[0], "lat": NEAR_LNG_LAT[1], "photos": []},
        )
    app.dependency_overrides.clear()

    assert resp.status_code == 403
    body = resp.json()
    assert body["code"] == "VISIT_NOT_ASSIGNEE"
    assert "request_id" in body


# ----------------------------- Q059 -----------------------------


def test_q059_original_evidence_immutable(session: Session) -> None:
    """原始证据不可覆盖：重复提交 409；无更新接口；原始值保持不变；补充只能追加。"""
    w, rep, _mgr, merchant, rep_actor, _mgr_actor = _setup(
        session, lng=MERCHANT_LNG_LAT[0], lat=MERCHANT_LNG_LAT[1]
    )
    task = _task(session, rep_actor, merchant)
    photo = _photo_file(session, w, rep)
    service = VisitService(session)

    record = service.submit(
        rep_actor,
        task.id,
        _submit_payload(*NEAR_LNG_LAT, photos=[VisitPhotoInput(file_id=photo.id)]),
    )
    session.flush()
    original = (record.checkin_at, record.distance_meters, record.photo_count)
    assert record.location is not None

    # 重复提交（换位置/换照片）不得覆盖既有记录
    with pytest.raises(ConflictError) as exc:
        service.submit(
            rep_actor,
            task.id,
            _submit_payload(*FAR_LNG_LAT, photos=[]),
        )
    assert exc.value.code == "VISIT_ALREADY_SUBMITTED"

    refreshed = session.get(VisitRecord, record.id)
    session.refresh(refreshed)
    assert (
        refreshed.checkin_at,
        refreshed.distance_meters,
        refreshed.photo_count,
    ) == original
    assert refreshed.location is not None
    assert refreshed.distance_meters is not None and refreshed.distance_meters <= 200

    # Service 不提供任何更新原始证据的方法（无 update/patch/overwrite 接口）
    assert not [m for m in dir(service) if m.startswith(("update_", "patch_", "overwrite"))]

    # 补充说明只能以修订记录追加，且追加与照片都不写回原记录字段
    revision = service.add_revision(
        rep_actor, record.id, VisitRevisionCreate(supplement="当日货架已补齐，照片稍后补传")
    )
    session.flush()
    assert session.get(VisitRevision, revision.id) is not None
    assert session.get(VisitRecord, record.id).photo_count == 1
    assert len(session.scalars(
        select(VisitRevision).where(VisitRevision.visit_record_id == record.id)
    ).all()) == 1


# ----------------------------- Q060 -----------------------------


def test_q060_manager_judgement_writes_audit(session: Session) -> None:
    """经理判定异常巡访：写审计（事件/原因/操作人），结论落记录。"""
    w, rep, mgr, merchant, rep_actor, mgr_actor = _setup(
        session, lng=MERCHANT_LNG_LAT[0], lat=MERCHANT_LNG_LAT[1]
    )
    task = _task(session, rep_actor, merchant)
    photo = _photo_file(session, w, rep)
    service = VisitService(session)

    # 超阈值 → 待判定
    record = service.submit(
        rep_actor,
        task.id,
        _submit_payload(*FAR_LNG_LAT, photos=[VisitPhotoInput(file_id=photo.id)]),
    )
    session.flush()
    assert record.status == VisitRecordStatus.ABNORMAL_PENDING.value

    judged = service.judge(
        mgr_actor,
        record.id,
        VisitJudge(recognize=False, reason="定位偏差过大，本次不计有效巡访"),
    )
    session.flush()

    assert judged.status == VisitRecordStatus.ABNORMAL.value
    assert judged.validity == INVALID
    assert judged.judged_by_id == mgr.id
    assert judged.judged_at is not None
    assert judged.judge_reason == "定位偏差过大，本次不计有效巡访"

    log = session.scalars(
        select(AuditLog).where(
            AuditLog.object_type == "VISIT_RECORD",
            AuditLog.object_id == record.id,
            AuditLog.event_type == "STATE_TRANSITION",
        )
    ).first()
    assert log is not None
    assert log.actor_id == mgr.id
    assert log.reason == "定位偏差过大，本次不计有效巡访"
    assert log.after_json["status"] == VisitRecordStatus.ABNORMAL.value
    assert log.before_json["status"] == VisitRecordStatus.ABNORMAL_PENDING.value

    # 提交（CREATE）与判定（STATE_TRANSITION）各留一条审计，审计只增不改
    events = session.scalars(
        select(AuditLog.event_type).where(
            AuditLog.object_type == "VISIT_RECORD", AuditLog.object_id == record.id
        )
    ).all()
    assert sorted(events) == ["CREATE", "STATE_TRANSITION"]


def test_q060_judge_recognize_counts_as_valid(session: Session) -> None:
    """经理认可缺照片/超阈值的异常 → 计为有效（RECOGNIZED）。"""
    _w, _rep, _mgr, merchant, rep_actor, mgr_actor = _setup(
        session, lng=MERCHANT_LNG_LAT[0], lat=MERCHANT_LNG_LAT[1]
    )
    task = _task(session, rep_actor, merchant)
    service = VisitService(session)
    record = service.submit(rep_actor, task.id, _submit_payload(*NEAR_LNG_LAT, photos=[]))
    session.flush()
    assert record.status == VisitRecordStatus.ABNORMAL_PENDING.value

    judged = service.judge(
        mgr_actor, record.id,
        VisitJudge(recognize=True, reason="现场已核验，照片后续补传"),
    )
    session.flush()
    assert judged.status == VisitRecordStatus.RECOGNIZED.value
    assert judged.validity == VALID
    assert session.get(VisitTask, task.id).status == "COMPLETED"


# ----------------------------- 其他红线 -----------------------------


def test_visit_sources_supported(session: Session) -> None:
    """巡访任务来源：周排班/临时/整改/督导均可建；业务员只能给自己排班。"""
    w = _world(session)
    rep = _user(session, w["ds"], RoleCode.SALES_REP, "rep")
    mgr = _user(session, w["ds"], RoleCode.REGIONAL_MANAGER, "mgr")
    supervisor = _user(session, w["ds"], RoleCode.SUPERVISOR, "sup")
    merchant = _merchant_at(session, w, *MERCHANT_LNG_LAT)
    _assign_primary(session, merchant, rep)
    rep_actor = _ctx(rep, RoleCode.SALES_REP.value, frozenset({w["region"].id}))
    mgr_actor = _ctx(mgr, RoleCode.REGIONAL_MANAGER.value, frozenset({w["region"].id}))
    sup_actor = _ctx(supervisor, RoleCode.SUPERVISOR.value, frozenset({w["region"].id}))

    for source in ("WEEKLY_SCHEDULE", "AD_HOC", "RECTIFICATION"):
        task = _task(session, rep_actor, merchant, source=source)
        assert task.source == source
        assert task.assigned_user_id == rep.id  # 服务端注入，业务员不能替他人排

    # 经理可指派他人；督导跟访由督导/经理建
    assigned = VisitService(session).create_task(
        mgr_actor,
        VisitTaskCreate(
            merchant_id=merchant.id,
            assigned_user_id=rep.id,
            planned_date=date(2026, 10, 14),
            source="SUPERVISION",
        ),
    )
    assert assigned.source == "SUPERVISION"
    sup_task = _task(session, sup_actor, merchant, source="SUPERVISION")
    assert sup_task.assigned_user_id == supervisor.id


def test_supervision_visit_flagged_not_for_coverage(session: Session) -> None:
    """督导跟访独立标记（source=SUPERVISION），指标侧据此排除，不计业务员覆盖率。"""
    w = _world(session)
    rep = _user(session, w["ds"], RoleCode.SALES_REP, "rep")
    supervisor = _user(session, w["ds"], RoleCode.SUPERVISOR, "sup")
    merchant = _merchant_at(session, w, *MERCHANT_LNG_LAT)
    _assign_primary(session, merchant, rep)
    sup_actor = _ctx(supervisor, RoleCode.SUPERVISOR.value, frozenset({w["region"].id}))
    photo = _photo_file(session, w, supervisor)
    service = VisitService(session)

    task = service.create_task(
        sup_actor,
        VisitTaskCreate(
            merchant_id=merchant.id,
            planned_date=date(2026, 10, 13),
            source="SUPERVISION",
        ),
    )
    record = service.submit(
        sup_actor,
        task.id,
        _submit_payload(*NEAR_LNG_LAT, photos=[VisitPhotoInput(file_id=photo.id)]),
    )
    session.flush()

    assert record.source == "SUPERVISION"
    assert service.validity.is_supervision(record) is True
    # 记录对象仍是责任人（督导本人），业务员记录数为 0
    assert session.scalars(
        select(VisitRecord).where(
            VisitRecord.merchant_id == merchant.id, VisitRecord.user_id == rep.id
        )
    ).all() == []


def test_visit_submit_is_idempotent(session: Session) -> None:
    """同 idempotency_key 重复提交返回同一条记录，不产生第二条。"""
    w, rep, _mgr, merchant, rep_actor, _mgr_actor = _setup(
        session, lng=MERCHANT_LNG_LAT[0], lat=MERCHANT_LNG_LAT[1]
    )
    task = _task(session, rep_actor, merchant)
    photo = _photo_file(session, w, rep)
    key = f"idem-{uuid4().hex[:8]}"
    payload = _submit_payload(
        *NEAR_LNG_LAT, photos=[VisitPhotoInput(file_id=photo.id)], idempotency_key=key
    )
    service = VisitService(session)
    first = service.submit(rep_actor, task.id, payload)
    session.flush()
    second = service.submit(rep_actor, task.id, payload)

    assert first.id == second.id
    assert len(session.scalars(
        select(VisitRecord).where(VisitRecord.visit_task_id == task.id)
    ).all()) == 1
    photos = session.scalars(
        select(VisitPhoto).where(VisitPhoto.visit_record_id == first.id)
    ).all()
    assert len(photos) == 1


def test_distance_is_server_computed_ignoring_client_value(session: Session) -> None:
    """距离一律由服务端 PostGIS 重算；客户端即使上报"很近"也不影响裁决。"""
    _w, _rep, _mgr, merchant, rep_actor, _mgr_actor = _setup(
        session, lng=MERCHANT_LNG_LAT[0], lat=MERCHANT_LNG_LAT[1]
    )
    task = _task(session, rep_actor, merchant)
    # schema 不接受距离字段（多传被忽略）
    payload = VisitSubmit.model_validate(
        {"lng": FAR_LNG_LAT[0], "lat": FAR_LNG_LAT[1], "photos": [], "distance_meters": 1}
    )
    record = VisitService(session).submit(rep_actor, task.id, payload)
    session.flush()
    # 客户端上报 distance_meters=1 被忽略；服务端重算为 ~333 m → 异常待判定
    assert record.distance_meters is not None and record.distance_meters > 200
    assert record.validity == ABNORMAL_PENDING
    assert record.status == VisitRecordStatus.ABNORMAL_PENDING.value
