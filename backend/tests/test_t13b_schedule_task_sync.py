"""排班 / 巡访任务一致性回归（缺陷：业务员"当日有排班"但巡访计划看不到）。

两个真实成因，各有用例：
1. 商家调拨只转 `VisitTask`，不同步 `SchedulePlanEntry`/`ResourceReservation`/
   `ScheduleTemplate`：原负责人在排班周视图里仍"有排班"，任务却已归新负责人；
2. 已发布周的「每周模板预排」被当成排班展示：模板预排不落任务，业务员看着有排班
   却永远等不到巡访任务。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session
from tests.test_t09_contract import _ctx, _user, _world
from tests.test_t11_visit import _assign_primary
from tests.test_t13_schedule_conflict import _make_merchants, _mgr_actor

from app.models import (
    ResourceReservation,
    SchedulePlanEntry,
    ScheduleTemplate,
    VisitTask,
)
from app.models.enums import RoleCode
from app.services.schedule_service import ScheduleService, tstzrange
from app.services.schedule_template_service import ScheduleTemplateService
from app.services.transfer_service import TransferService
from app.services.visit_service import VisitService

TZ = ZoneInfo("Asia/Shanghai")
MONDAY = date(2026, 12, 14)


def _at(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime.combine(day, time(hour, minute), tzinfo=TZ).astimezone(UTC)


def _day_entry(merchant_id, user_id, planned: date) -> dict:
    return {
        "merchant_id": merchant_id,
        "assigned_user_id": user_id,
        "planned_date": planned,
        "start_at": None,
        "end_at": None,
        "priority": 2,
    }


def _publish_one(session: Session, w, actor, rep, shop, planned: date):
    svc = ScheduleService(session)
    batch = svc.create_draft(
        actor,
        region_id=w["region"].id,
        week_start=MONDAY,
        entries=[_day_entry(shop.id, rep.id, planned)],
    )
    svc.publish(actor, batch_id=batch.id)
    session.flush()
    entry = session.scalars(
        select(SchedulePlanEntry).where(SchedulePlanEntry.batch_id == batch.id)
    ).one()
    return svc, entry


# ------------------------- 1. 调拨同步改派排班 -------------------------


def test_transfer_reassigns_future_schedule_and_task(session: Session) -> None:
    """调拨生效：排班条目 / 占用 / 模板 / 任务一起改派，原负责人不再"有排班无任务"。"""
    w = _world(session)
    _mgr, actor = _mgr_actor(session, w, "mgr-sync-1")
    rep_a = _user(session, w["ds"], RoleCode.SALES_REP, "rep-sync-a1")
    rep_b = _user(session, w["ds"], RoleCode.SALES_REP, "rep-sync-b1")
    shop = _make_merchants(session, w, 1)[0]
    _assign_primary(session, shop, rep_a)

    planned = MONDAY + timedelta(days=2)
    svc, entry = _publish_one(session, w, actor, rep_a, shop, planned)
    task = session.get(VisitTask, entry.visit_task_id)
    reservation = session.get(ResourceReservation, entry.reservation_id)
    assert task is not None and task.assigned_user_id == rep_a.id

    transfer = TransferService(session).create_transfer(
        actor,
        merchant_id=shop.id,
        from_user_id=rep_a.id,
        to_user_id=rep_b.id,
        effective_date=MONDAY,
        reason="片区调整，交接商家",
    )
    TransferService(session).apply(actor, transfer_id=transfer.id)
    session.flush()

    assert entry.assigned_user_id == rep_b.id
    assert task.assigned_user_id == rep_b.id
    assert reservation is not None and reservation.resource_user_id == rep_b.id
    template = session.scalars(
        select(ScheduleTemplate).where(ScheduleTemplate.merchant_id == shop.id)
    ).one()
    assert template.assigned_user_id == rep_b.id

    rep_a_actor = _ctx(rep_a, RoleCode.SALES_REP, frozenset({w["region"].id}))
    rep_b_actor = _ctx(rep_b, RoleCode.SALES_REP, frozenset({w["region"].id}))
    visit = VisitService(session)

    # 原负责人：排班与巡访计划都不再有该商家（不再是"有排班没任务"）
    assert svc.week_view(
        rep_a_actor, week_start=MONDAY, region_id=w["region"].id
    ) == []
    assert visit.list_my_tasks(rep_a_actor, planned) == []

    # 新负责人：排班与巡访计划都在
    assert len(
        svc.week_view(rep_b_actor, week_start=MONDAY, region_id=w["region"].id)
    ) == 1
    assert [t.id for t in visit.list_my_tasks(rep_b_actor, planned)] == [task.id]

    # 交接快照可追溯改派结果
    assert transfer.handover_json["schedule_reassigned"]["entries"] == 1


def test_transfer_reassign_moves_to_free_slot_when_owner_busy(
    session: Session,
) -> None:
    """新负责人同时段已占用：改派顺延到当天第一个空档，避免占用区间重叠。"""
    w = _world(session)
    _mgr, actor = _mgr_actor(session, w, "mgr-sync-2")
    rep_a = _user(session, w["ds"], RoleCode.SALES_REP, "rep-sync-a2")
    rep_b = _user(session, w["ds"], RoleCode.SALES_REP, "rep-sync-b2")
    shops = _make_merchants(session, w, 2)
    _assign_primary(session, shops[0], rep_a)

    planned = MONDAY + timedelta(days=1)
    svc, entry = _publish_one(session, w, actor, rep_a, shops[0], planned)
    original_start = entry.start_at
    assert original_start == _at(planned, 9)

    # B 当天 09:00–09:30 已有别的排班占用
    busy_start, busy_end = _at(planned, 9), _at(planned, 9, 30)
    session.add(
        ResourceReservation(
            resource_user_id=rep_b.id,
            start_at=busy_start,
            end_at=busy_end,
            occupied_range=tstzrange(busy_start, busy_end),
            source_type="SCHEDULE",
            status="ACTIVE",
            merchant_id=shops[1].id,
            source_object_type="SCHEDULE_PLAN_BATCH",
            source_object_id=entry.batch_id,
            dataset_type="PRODUCTION",
            dataset_id=w["ds"],
        )
    )
    session.flush()

    transfer = TransferService(session).create_transfer(
        actor,
        merchant_id=shops[0].id,
        from_user_id=rep_a.id,
        to_user_id=rep_b.id,
        effective_date=MONDAY,
        reason="片区调整，交接商家",
    )
    TransferService(session).apply(actor, transfer_id=transfer.id)
    session.flush()

    assert entry.assigned_user_id == rep_b.id
    assert entry.start_at == _at(planned, 9, 30)
    task = session.get(VisitTask, entry.visit_task_id)
    assert task is not None and task.window_start_at == _at(planned, 9, 30)


def test_transfer_keeps_finished_task_schedule(session: Session) -> None:
    """已完成任务不被转移，其排班条目也保持原负责人（历史事实不改写）。"""
    w = _world(session)
    _mgr, actor = _mgr_actor(session, w, "mgr-sync-3")
    rep_a = _user(session, w["ds"], RoleCode.SALES_REP, "rep-sync-a3")
    rep_b = _user(session, w["ds"], RoleCode.SALES_REP, "rep-sync-b3")
    shop = _make_merchants(session, w, 1)[0]
    _assign_primary(session, shop, rep_a)

    planned = MONDAY + timedelta(days=1)
    _svc, entry = _publish_one(session, w, actor, rep_a, shop, planned)
    task = session.get(VisitTask, entry.visit_task_id)
    task.status = "COMPLETED"
    session.flush()

    transfer = TransferService(session).create_transfer(
        actor,
        merchant_id=shop.id,
        from_user_id=rep_a.id,
        to_user_id=rep_b.id,
        effective_date=MONDAY,
        reason="片区调整，交接商家",
    )
    TransferService(session).apply(actor, transfer_id=transfer.id)
    session.flush()

    assert entry.assigned_user_id == rep_a.id
    assert session.get(VisitTask, task.id).assigned_user_id == rep_a.id
    assert transfer.handover_json["schedule_reassigned"]["skipped"] == [
        {"entry_id": str(entry.id), "reason": "TASK_FINISHED"}
    ]


def test_reconcile_fixes_legacy_mismatch(session: Session) -> None:
    """存量数据修复：任务负责人已变、排班没变时，以任务为准回填条目与占用。"""
    w = _world(session)
    _mgr, actor = _mgr_actor(session, w, "mgr-sync-5")
    rep_a = _user(session, w["ds"], RoleCode.SALES_REP, "rep-sync-a5")
    rep_b = _user(session, w["ds"], RoleCode.SALES_REP, "rep-sync-b5")
    shop = _make_merchants(session, w, 1)[0]
    _assign_primary(session, shop, rep_a)

    planned = MONDAY + timedelta(days=1)
    svc, entry = _publish_one(session, w, actor, rep_a, shop, planned)
    task = session.get(VisitTask, entry.visit_task_id)
    # 模拟旧版调拨：只把任务改给 B，排班与占用仍停在 A
    task.assigned_user_id = rep_b.id
    session.flush()

    dry = svc.reconcile_schedule_assignees(apply=False)
    assert dry["mismatched"] == 1 and dry["fixed"] == 1 and dry["dry_run"] is True
    assert entry.assigned_user_id == rep_a.id

    result = svc.reconcile_schedule_assignees(apply=True)
    assert result["mismatched"] == 1
    assert result["fixed"] == 1
    assert result["conflicts"] == []
    assert entry.assigned_user_id == rep_b.id
    reservation = session.get(ResourceReservation, entry.reservation_id)
    assert reservation is not None and reservation.resource_user_id == rep_b.id

    # 幂等：再跑一次没有可修的行
    assert svc.reconcile_schedule_assignees(apply=True)["mismatched"] == 0


# ------------------------- 2. 模板预排 ≠ 排班 -------------------------


def test_projected_week_hidden_when_week_published(session: Session) -> None:
    """已发布周不再回落到模板预排：预排不落任务，不能冒充排班。"""
    w = _world(session)
    _mgr, actor = _mgr_actor(session, w, "mgr-sync-4")
    rep = _user(session, w["ds"], RoleCode.SALES_REP, "rep-sync-4")
    shops = _make_merchants(session, w, 2)
    for shop in shops:
        _assign_primary(session, shop, rep)

    ScheduleTemplateService(session).create_entries(
        actor,
        region_id=w["region"].id,
        entries=[
            {
                "merchant_id": shops[0].id,
                "assigned_user_id": rep.id,
                "weekday": MONDAY.isoweekday(),
                "start_time": time(9, 0),
                "end_time": time(9, 30),
            },
            {
                "merchant_id": shops[1].id,
                "assigned_user_id": rep.id,
                "weekday": MONDAY.isoweekday(),
                "start_time": time(10, 0),
                "end_time": time(10, 30),
            },
        ],
    )
    svc = ScheduleService(session)

    # 未发布周：模板预排可见（只读）
    assert svc.week_has_published(actor, week_start=MONDAY) is False
    assert len(svc.projected_week(actor, week_start=MONDAY)) == 2

    # 发布其中一家后：预排整体不再展示（避免"有排班无任务"的假象）
    batch = svc.create_draft(
        actor,
        region_id=w["region"].id,
        week_start=MONDAY,
        entries=[_day_entry(shops[0].id, rep.id, MONDAY)],
    )
    svc.publish(actor, batch_id=batch.id)
    session.flush()

    assert svc.week_has_published(actor, week_start=MONDAY) is True
    assert svc.projected_week(actor, week_start=MONDAY) == []
    rows = svc.week_view(actor, week_start=MONDAY, region_id=w["region"].id)
    assert len(rows) == 1
    assert rows[0][3].status == "PUBLISHED"
