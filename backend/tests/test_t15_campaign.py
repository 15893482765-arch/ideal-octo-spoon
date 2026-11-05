"""T15 地推活动与资源占用（PRD §4.7 / R39）。

- Q079 已发布活动占用人员时段，阻止重叠的巡访排班；
- Q080 与既有活动占用冲突的活动发布失败（全有或全无，零写入）；
- Q081 取消必须填原因，并释放活动未来时段占用；
- Q082 活动预算按分（BIGINT）精确存储，不丢精度；
- Q083 回改 T13：通用占用源接入 CAMPAIGN 后重跑 Q068–Q073（见 test_t13 套件）；
- Q182 样品领用/消耗/归还台账完整，样品不进确认销量，未归还生成提醒待办。

冲突口径唯一来自 ScheduleConflictService；样品只走 ActivitySampleLedger（不做完整 WMS）。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from decimal import Decimal
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from tests.test_t09_contract import _ctx, _merchant, _user, _world
from tests.test_t11_visit import _assign_primary
from tests.test_t12_visit_submission import _sku

from app.core.errors import DomainError
from app.models import (
    Activity,
    ActivitySampleLedger,
    ResourceReservation,
    SalesConfirmation,
    Sku,
    TaskItem,
    UserRegionBrandAuth,
    VisitTask,
)
from app.models.enums import RoleCode
from app.repositories.activity_repository import (
    ACTIVE_RESERVATION,
    RELEASED_RESERVATION,
)
from app.services.activity_service import ActivityConflictError, ActivityService
from app.services.schedule_service import (
    ScheduleConflictError,
    ScheduleService,
    monday_of,
)

TZ = ZoneInfo("Asia/Shanghai")


# ----------------------------- 造数辅助 -----------------------------


def _mgr_actor(session, w, tag="mgr15"):
    mgr = _user(session, w["ds"], RoleCode.REGIONAL_MANAGER, tag)
    session.add(UserRegionBrandAuth(
        user_id=mgr.id, region_id=w["region"].id, brand_id=w["brand"].id))
    session.flush()
    actor = _ctx(mgr, RoleCode.REGIONAL_MANAGER.value, frozenset({w["region"].id}))
    return mgr, actor


def _rep(session, w, tag):
    return _user(session, w["ds"], RoleCode.SALES_REP, tag)


def _at(day: date, h: int, m: int = 0) -> datetime:
    return datetime.combine(day, time(h, m), tzinfo=TZ).astimezone(UTC)


def _draft_campaign(session, actor, w, rep_id, day, *, h0=9, h1=12, samples=None,
                    budget_fen=0, name=None, extra_participants=None):
    return ActivityService(session).create_draft(
        actor,
        region_id=w["region"].id,
        name=name or f"地推-{uuid4().hex[:6]}",
        start_at=_at(day, h0),
        end_at=_at(day, h1),
        location="市中心广场",
        owner_user_id=rep_id,
        participant_user_ids=[rep_id] + (extra_participants or []),
        budget_fen=budget_fen,
        samples=samples or [],
    )


def _schedule_entry(merchant_id, user_id, day, h=9, minutes=30):
    return {
        "merchant_id": merchant_id,
        "assigned_user_id": user_id,
        "planned_date": day,
        "start_at": _at(day, h, 0),
        "end_at": _at(day, h, minutes),
        "priority": 2,
    }


# ----------------------------- Q079 -----------------------------


def test_q079_campaign_blocks_overlapping_visit_schedule(session: Session) -> None:
    """已发布活动占用 rep 周一 09:00-12:00；重叠的巡访排班发布必须被冲突服务拒绝。"""
    w = _world(session)
    mgr, actor = _mgr_actor(session, w)
    rep = _rep(session, w, "rep15a")
    shop = _merchant(session, w, name="T15店-079")
    _assign_primary(session, shop, rep)
    monday = date(2026, 12, 14)

    campaign = _draft_campaign(session, actor, w, rep.id, monday)
    ActivityService(session).publish(actor, activity_id=campaign.id)
    session.flush()
    # 活动占用已落库且为 ACTIVE CAMPAIGN
    res = session.scalars(
        select(ResourceReservation).where(
            ResourceReservation.source_object_id == campaign.id)
    ).one()
    assert res.source_type == "CAMPAIGN"
    assert res.status == ACTIVE_RESERVATION

    # 巡访排班排在周一 09:00-09:30，与活动占用重叠 → 整批拒绝
    batch = ScheduleService(session).create_draft(
        actor, region_id=w["region"].id, week_start=monday_of(monday),
        entries=[_schedule_entry(shop.id, rep.id, monday, 9)])
    with pytest.raises(ScheduleConflictError) as exc:
        with session.begin_nested():
            ScheduleService(session).publish(actor, batch_id=batch.id)
    assert any(c.type == "TIME_OVERLAP" for c in exc.value.conflicts)

    # 端点相接（12:00 起）不冲突，可正常发布
    later = _merchant(session, w, name="T15店-079b")
    _assign_primary(session, later, rep)
    batch2 = ScheduleService(session).create_draft(
        actor, region_id=w["region"].id, week_start=monday_of(monday),
        entries=[{
            "merchant_id": later.id, "assigned_user_id": rep.id,
            "planned_date": monday, "start_at": _at(monday, 12),
            "end_at": _at(monday, 12, 30), "priority": 2,
        }])
    ScheduleService(session).publish(actor, batch_id=batch2.id)
    session.flush()
    b2 = session.get(type(batch2), batch2.id)
    assert b2.status == "PUBLISHED"


# ----------------------------- Q080 -----------------------------


def test_q080_conflicting_campaign_publish_fails_zero_write(session: Session) -> None:
    """与既有活动占用时间冲突的活动发布失败，零写入（不生成任何 CAMPAIGN 占用）。"""
    w = _world(session)
    mgr, actor = _mgr_actor(session, w, "mgr15b")
    rep = _rep(session, w, "rep15b")
    monday = date(2026, 12, 14)

    first = _draft_campaign(session, actor, w, rep.id, monday, h0=9, h1=12)
    ActivityService(session).publish(actor, activity_id=first.id)
    session.flush()

    second = _draft_campaign(session, actor, w, rep.id, monday, h0=11, h1=13,
                            name="冲突地推")
    with pytest.raises(ActivityConflictError) as exc:
        with session.begin_nested():
            ActivityService(session).publish(actor, activity_id=second.id)
    assert exc.value.code == "ACTIVITY_CONFLICT"
    assert any(c.type == "TIME_OVERLAP" for c in exc.value.conflicts)

    # 失败活动仍为草稿，且没有为它写任何占用
    session.expire_all()
    assert session.get(Activity, second.id).status == "DRAFT"
    count = session.scalar(
        select(func.count()).select_from(ResourceReservation)
        .where(ResourceReservation.source_object_id == second.id)
    )
    assert count == 0


# ----------------------------- Q081 -----------------------------


def test_q081_cancel_requires_reason_and_releases_future(session: Session) -> None:
    """取消活动必须填原因；取消后释放活动未来时段占用（置 RELEASED）。"""
    w = _world(session)
    mgr, actor = _mgr_actor(session, w, "mgr15c")
    rep = _rep(session, w, "rep15c")
    # 用未来日期，保证占用时段在当前时刻之后
    future_day = date(2027, 6, 7)  # 周一
    campaign = _draft_campaign(session, actor, w, rep.id, future_day, h0=9, h1=12)
    ActivityService(session).publish(actor, activity_id=campaign.id)
    session.flush()

    svc = ActivityService(session)
    # 缺原因被拒
    with pytest.raises(DomainError) as exc_reason:
        svc.cancel(actor, activity_id=campaign.id, reason="   ")
    assert exc_reason.value.code == "ACTIVITY_CANCEL_REASON_REQUIRED"

    activity = svc.cancel(actor, activity_id=campaign.id, reason="天气恶劣改期")
    assert activity.status == "CANCELLED"
    assert activity.cancel_reason == "天气恶劣改期"
    assert activity.cancelled_at is not None

    res = session.scalars(
        select(ResourceReservation).where(
            ResourceReservation.source_object_id == campaign.id)
    ).one()
    assert res.status == RELEASED_RESERVATION

    # 幂等：重复取消直接返回 CANCELLED，不报错
    again = ActivityService(session).cancel(
        actor, activity_id=campaign.id, reason="再次取消")
    assert again.status == "CANCELLED"


# ----------------------------- Q082 -----------------------------


def test_q082_budget_precision_in_fen(session: Session) -> None:
    """活动预算以分（BIGINT）精确存储：大金额、带分值均不丢精度（禁 float）。"""
    w = _world(session)
    mgr, actor = _mgr_actor(session, w, "mgr15d")
    rep = _rep(session, w, "rep15d")
    monday = date(2026, 12, 14)
    budget = 12_3456_7890  # 1,234,567.890 元，单位分，精确整数
    campaign = _draft_campaign(session, actor, w, rep.id, monday,
                               budget_fen=budget)
    session.expire_all()
    saved = session.get(Activity, campaign.id)
    assert saved.budget_fen == budget
    assert isinstance(saved.budget_fen, int)


# ----------------------------- Q182 -----------------------------


def test_q182_sample_ledger_and_return_reminder(session: Session) -> None:
    """样品领用/消耗/归还台账完整；样品成本计活动费用、不进确认销量；未归还生成待办。"""
    w = _world(session)
    mgr, actor = _mgr_actor(session, w, "mgr15e")
    rep = _rep(session, w, "rep15e")
    rep2 = _rep(session, w, "rep15f")
    monday = date(2026, 12, 14)

    sku = _sku(session, w, code="T15", name="金桔柠檬500ml")
    sku.purchase_price_fen = 10000  # 100 元/瓶（分），箱规 24 瓶/箱
    session.flush()

    campaign = _draft_campaign(
        session, actor, w, rep.id, monday,
        extra_participants=[rep2.id],
        samples=[{"sku_id": sku.id, "boxes": Decimal("10"), "note": "活动领用"}],
    )
    ActivityService(session).publish(actor, activity_id=campaign.id)
    session.flush()

    # 复盘：6 消耗、3 归还、1 缺失（合计 10 = 领用）
    reviewed = ActivityService(session).review(
        actor,
        activity_id=campaign.id,
        sample_results=[
            {"sku_id": sku.id, "stage": "CONSUMED", "boxes": Decimal("6")},
            {"sku_id": sku.id, "stage": "RETURNED", "boxes": Decimal("3")},
            {"sku_id": sku.id, "stage": "MISSING", "boxes": Decimal("1")},
        ],
    )
    assert reviewed.status == "REVIEWED"
    # 样品成本 = (6 消耗 + 1 缺失) 箱 × 24 瓶 × 10000 分/瓶 = 1680000 分
    assert reviewed.sample_cost_fen == 1_680_000

    # 台账四阶段齐备
    ledger = session.scalars(
        select(ActivitySampleLedger)
        .where(ActivitySampleLedger.activity_id == campaign.id)
        .order_by(ActivitySampleLedger.created_at.asc())
    ).all()
    stages = {row.stage: row.boxes for row in ledger}
    assert stages == {
        "TAKEN_OUT": Decimal("10"),
        "CONSUMED": Decimal("6"),
        "RETURNED": Decimal("3"),
        "MISSING": Decimal("1"),
    }

    # 未归还（消耗+缺失）→ 两个参与人各一条提醒待办，幂等维度不重复
    tasks = session.scalars(
        select(TaskItem).where(TaskItem.related_object_id == campaign.id)
    ).all()
    assignees = {t.assignee_id for t in tasks}
    assert assignees == {rep.id, rep2.id}
    assert all(t.task_kind == "CAMPAIGN_SAMPLE_RETURN" for t in tasks)
    assert len(tasks) == 2

    # 样品不进确认销量（本数据范围零条 SalesConfirmation）
    sales_count = session.scalar(
        select(func.count()).select_from(SalesConfirmation)
        .where(SalesConfirmation.dataset_id == w["ds"])
    )
    assert sales_count == 0


def test_q182_sample_unbalanced_review_rejected(session: Session) -> None:
    """复盘样品不平（领用 != 消耗+归还+缺失）被拒，活动停留原态，不结算成本。"""
    w = _world(session)
    mgr, actor = _mgr_actor(session, w, "mgr15g")
    rep = _rep(session, w, "rep15g")
    monday = date(2026, 12, 14)
    sku = _sku(session, w, code="T15U", name="样品不平")
    sku.purchase_price_fen = 10000
    session.flush()

    campaign = _draft_campaign(
        session, actor, w, rep.id, monday,
        samples=[{"sku_id": sku.id, "boxes": Decimal("10")}],
    )
    ActivityService(session).publish(actor, activity_id=campaign.id)
    session.flush()

    with pytest.raises(DomainError) as exc:
        with session.begin_nested():
            ActivityService(session).review(
                actor, activity_id=campaign.id,
                sample_results=[
                    {"sku_id": sku.id, "stage": "CONSUMED", "boxes": Decimal("4")},
                    {"sku_id": sku.id, "stage": "RETURNED", "boxes": Decimal("3")},
                ],  # 合计 7 != 10
            )
    assert exc.value.code == "ACTIVITY_SAMPLE_UNBALANCED"

    session.expire_all()
    activity = session.get(Activity, campaign.id)
    assert activity.status == "PUBLISHED"
    assert activity.sample_cost_fen == 0


def test_q182_balanced_no_unreturned_no_reminder(session: Session) -> None:
    """样品全部归还（无消耗/缺失）时不生成未归还提醒待办，样品成本为 0。"""
    w = _world(session)
    mgr, actor = _mgr_actor(session, w, "mgr15h")
    rep = _rep(session, w, "rep15h")
    monday = date(2026, 12, 14)
    sku = _sku(session, w, code="T15R", name="全归还")
    sku.purchase_price_fen = 10000
    session.flush()

    campaign = _draft_campaign(
        session, actor, w, rep.id, monday,
        samples=[{"sku_id": sku.id, "boxes": Decimal("5")}],
    )
    ActivityService(session).publish(actor, activity_id=campaign.id)
    session.flush()
    reviewed = ActivityService(session).review(
        actor, activity_id=campaign.id,
        sample_results=[
            {"sku_id": sku.id, "stage": "RETURNED", "boxes": Decimal("5")},
        ],
    )
    assert reviewed.status == "REVIEWED"
    assert reviewed.sample_cost_fen == 0
    task_count = session.scalar(
        select(func.count()).select_from(TaskItem)
        .where(TaskItem.related_object_id == campaign.id)
    )
    assert task_count == 0
    # 活动样品同样不产生任何巡访任务/销量
    assert session.scalar(
        select(func.count()).select_from(VisitTask)
        .where(VisitTask.dataset_id == w["ds"])
    ) == 0
    assert session.scalar(
        select(func.count()).select_from(Sku).where(Sku.dataset_id == w["ds"])
    ) >= 1
