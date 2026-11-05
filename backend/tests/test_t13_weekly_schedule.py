"""周排班增量需求回归（用户新增口径）。

- 一天给同一业务员排多家商家不算冲突：发布时自动分配互不重叠的 30 分钟时段；
- 业务员只看排给自己的排班，片区经理可看全部/按人筛选；
- 经理可把已发布排班改到同周另一天（同步巡访任务与人员占用）；
- 发布后自动沉淀每周模板，下周按模板预排，并可用 apply 一键生成（幂等）。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from tests.test_t09_contract import _ctx, _user, _world
from tests.test_t11_visit import _assign_primary
from tests.test_t13_schedule_conflict import _mgr_actor, _make_merchants

from app.core.errors import DomainError
from app.models import (
    ResourceReservation,
    SchedulePlanEntry,
    ScheduleTemplate,
    VisitTask,
)
from app.models.enums import RoleCode
from app.services.schedule_service import ScheduleConflictError, ScheduleService
from app.services.schedule_template_service import ScheduleTemplateService

TZ = ZoneInfo("Asia/Shanghai")
MONDAY = date(2026, 12, 14)


def _day_entry(merchant_id, user_id, planned: date, priority: int = 2) -> dict:
    """只给到“天”的条目（时间窗留空，由发布时自动分时段）。"""
    return {
        "merchant_id": merchant_id,
        "assigned_user_id": user_id,
        "planned_date": planned,
        "start_at": None,
        "end_at": None,
        "priority": priority,
    }


def test_multi_merchant_same_day_not_conflict(session: Session) -> None:
    """同一天同一业务员排 3 家商家：发布成功，时段 09:00/09:30/10:00 互不重叠。"""
    w = _world(session)
    _mgr, actor = _mgr_actor(session, w, "mgr-week-multi")
    rep = _user(session, w["ds"], RoleCode.SALES_REP, "rep-week-multi")
    shops = _make_merchants(session, w, 3)
    for shop in shops:
        _assign_primary(session, shop, rep)

    svc = ScheduleService(session)
    batch = svc.create_draft(
        actor,
        region_id=w["region"].id,
        week_start=MONDAY,
        entries=[_day_entry(s.id, rep.id, MONDAY) for s in shops],
    )
    svc.publish(actor, batch_id=batch.id)
    session.flush()

    tasks = list(
        session.scalars(
            select(VisitTask)
            .where(VisitTask.merchant_id.in_([s.id for s in shops]))
            .order_by(VisitTask.window_start_at.asc())
        ).all()
    )
    assert len(tasks) == 3
    starts = [t.window_start_at for t in tasks]
    assert starts == [
        datetime.combine(MONDAY, time(9, 0), tzinfo=TZ).astimezone(UTC),
        datetime.combine(MONDAY, time(9, 30), tzinfo=TZ).astimezone(UTC),
        datetime.combine(MONDAY, time(10, 0), tzinfo=TZ).astimezone(UTC),
    ]
    # 占用同步落库且互不重叠
    reservations = list(
        session.scalars(
            select(ResourceReservation)
            .where(ResourceReservation.merchant_id.in_([s.id for s in shops]))
            .order_by(ResourceReservation.start_at.asc())
        ).all()
    )
    assert len(reservations) == 3
    for prev, cur in zip(reservations, reservations[1:], strict=False):
        assert prev.end_at <= cur.start_at


def test_sales_rep_sees_only_own_schedule(session: Session) -> None:
    """业务员只看排给自己的条目；经理看全部，也能按业务员筛选。"""
    w = _world(session)
    _mgr, actor = _mgr_actor(session, w, "mgr-week-scope")
    rep_a = _user(session, w["ds"], RoleCode.SALES_REP, "rep-week-a")
    rep_b = _user(session, w["ds"], RoleCode.SALES_REP, "rep-week-b")
    shops = _make_merchants(session, w, 2)
    for shop in shops:
        _assign_primary(session, shop, rep_a)

    svc = ScheduleService(session)
    batch = svc.create_draft(
        actor,
        region_id=w["region"].id,
        week_start=MONDAY,
        entries=[
            _day_entry(shops[0].id, rep_a.id, MONDAY),
            _day_entry(shops[1].id, rep_b.id, MONDAY),
        ],
    )
    svc.publish(actor, batch_id=batch.id)
    session.flush()

    rep_actor = _ctx(rep_a, RoleCode.SALES_REP.value, frozenset({w["region"].id}))
    own = svc.week_view(rep_actor, week_start=MONDAY, region_id=w["region"].id)
    assert len(own) == 1
    assert own[0][0].assigned_user_id == rep_a.id

    all_rows = svc.week_view(actor, week_start=MONDAY, region_id=w["region"].id)
    assert len(all_rows) == 2

    filtered = svc.week_view(
        actor, week_start=MONDAY, region_id=w["region"].id, user_id=rep_b.id
    )
    assert len(filtered) == 1
    assert filtered[0][0].assigned_user_id == rep_b.id


def test_move_entry_to_other_day_syncs_task_and_reservation(session: Session) -> None:
    """改期：条目、巡访任务、人员占用一起挪到周三；改到被活动占满的一天则拒绝。"""
    w = _world(session)
    _mgr, actor = _mgr_actor(session, w, "mgr-week-move")
    rep = _user(session, w["ds"], RoleCode.SALES_REP, "rep-week-move")
    shops = _make_merchants(session, w, 2)
    for shop in shops:
        _assign_primary(session, shop, rep)

    svc = ScheduleService(session)
    batch = svc.create_draft(
        actor,
        region_id=w["region"].id,
        week_start=MONDAY,
        entries=[_day_entry(s.id, rep.id, MONDAY) for s in shops],
    )
    svc.publish(actor, batch_id=batch.id)
    session.flush()

    first = session.scalars(
        select(SchedulePlanEntry).where(SchedulePlanEntry.merchant_id == shops[0].id)
    ).one()
    tuesday = MONDAY + timedelta(days=1)
    wednesday = MONDAY + timedelta(days=2)
    moved = svc.move_entry(actor, entry_id=first.id, planned_date=tuesday)
    assert moved.planned_date == tuesday
    session.flush()

    task = session.get(VisitTask, first.visit_task_id)
    assert task.planned_date == tuesday
    assert task.window_start_at == first.start_at

    reservation = session.get(ResourceReservation, first.reservation_id)
    assert reservation.start_at == first.start_at
    assert reservation.end_at == first.end_at

    # 周三被地推占满：把第二条也调到周三必须被拒绝，且不改动原条目
    day_start = datetime.combine(wednesday, time(0, 0), tzinfo=TZ).astimezone(UTC)
    day_end = datetime.combine(wednesday, time(23, 59), tzinfo=TZ).astimezone(UTC)
    session.add(ResourceReservation(
        resource_user_id=rep.id,
        start_at=day_start,
        end_at=day_end,
        occupied_range=func.tstzrange(day_start, day_end, "[)"),
        source_type="CAMPAIGN",
        status="ACTIVE",
        merchant_id=None,
        source_object_type="CAMPAIGN",
        source_object_id=shops[0].id,
        dataset_type="PRODUCTION",
        dataset_id=w["ds"],
    ))
    session.flush()

    second = session.scalars(
        select(SchedulePlanEntry).where(SchedulePlanEntry.merchant_id == shops[1].id)
    ).one()
    with pytest.raises((ScheduleConflictError, DomainError)):
        svc.move_entry(actor, entry_id=second.id, planned_date=wednesday)
    session.expire_all()
    assert session.get(SchedulePlanEntry, second.id).planned_date == MONDAY


def test_publish_creates_weekly_templates_and_next_week_projects(session: Session) -> None:
    """发布自动沉淀模板：下周按模板预排，apply 生成任务且重复调用幂等。"""
    w = _world(session)
    _mgr, actor = _mgr_actor(session, w, "mgr-week-loop")
    rep = _user(session, w["ds"], RoleCode.SALES_REP, "rep-week-loop")
    shops = _make_merchants(session, w, 2)
    for shop in shops:
        _assign_primary(session, shop, rep)

    svc = ScheduleService(session)
    batch = svc.create_draft(
        actor,
        region_id=w["region"].id,
        week_start=MONDAY,
        entries=[_day_entry(s.id, rep.id, MONDAY) for s in shops],
    )
    svc.publish(actor, batch_id=batch.id)
    session.flush()

    templates = list(session.scalars(
        select(ScheduleTemplate).where(ScheduleTemplate.region_id == w["region"].id)
    ).all())
    assert len(templates) == 2
    assert {t.weekday for t in templates} == {1}
    assert all(t.assigned_user_id == rep.id for t in templates)

    next_monday = MONDAY + timedelta(days=7)
    projected = svc.projected_week(
        actor, week_start=next_monday, region_id=w["region"].id
    )
    assert len(projected) == 2
    assert {p.planned_date for p, _m, _u in projected} == {next_monday}

    template_svc = ScheduleTemplateService(session)
    published, created, total = template_svc.apply(
        actor, region_id=w["region"].id, week_start=next_monday
    )
    again, created_again, _total_again = template_svc.apply(
        actor, region_id=w["region"].id, week_start=next_monday
    )
    assert published.id == again.id
    assert published.status == "PUBLISHED"
    assert (created, total) == (2, 2)
    # 重复点击：该周已是最新，明确回报 0 新增（而不是静默无变化）
    assert created_again == 0

    session.flush()
    next_tasks = session.scalar(
        select(func.count()).select_from(VisitTask)
        .where(VisitTask.planned_date == next_monday)
    )
    assert next_tasks == 2


# ------------------------- 业务员下拉 / 模板回填 -------------------------


def test_assignee_options_cover_manager_regions(session: Session) -> None:
    """排班下拉返回经理可见片区内的全部在职业务员，而不是当前片区授权的 2 个人。"""
    from app.models import Region, UserRegionBrandAuth

    w = _world(session)
    mgr, _actor = _mgr_actor(session, w, "mgr-week-options")
    other_region = Region(
        code=f"reg2-{uuid4().hex[:6]}",
        name="二七片区",
        org_id=w["region"].org_id,
        dataset_type="PRODUCTION",
        dataset_id=w["ds"],
    )
    session.add(other_region)
    session.flush()

    rep_a = _user(session, w["ds"], RoleCode.SALES_REP, "rep-opt-a")
    rep_b = _user(session, w["ds"], RoleCode.SALES_REP, "rep-opt-b")
    rep_c = _user(session, w["ds"], RoleCode.SALES_REP, "rep-opt-c")
    for user, region in ((rep_a, w["region"]), (rep_b, w["region"]), (rep_c, other_region)):
        session.add(UserRegionBrandAuth(
            user_id=user.id, region_id=region.id, brand_id=w["brand"].id))
    session.flush()

    actor = _ctx(
        mgr,
        RoleCode.REGIONAL_MANAGER.value,
        frozenset({w["region"].id, other_region.id}),
    )
    rows = ScheduleService(session).list_assignee_options(
        actor, region_id=w["region"].id
    )
    assert {u.id for u, _names in rows} == {rep_a.id, rep_b.id, rep_c.id}
    names = {u.id: names for u, names in rows}
    assert names[rep_c.id] == ["二七片区"]
    # 当前片区的人排在前面
    assert {rows[0][0].id, rows[1][0].id} == {rep_a.id, rep_b.id}


def test_sync_templates_from_published_backfills_history(session: Session) -> None:
    """已发布排班回填模板：默认只补缺失，overwrite 时用最新排班覆盖。"""
    from sqlalchemy import delete as sa_delete

    w = _world(session)
    _mgr, actor = _mgr_actor(session, w, "mgr-week-sync")
    rep = _user(session, w["ds"], RoleCode.SALES_REP, "rep-week-sync")
    shops = _make_merchants(session, w, 2)
    for shop in shops:
        _assign_primary(session, shop, rep)

    svc = ScheduleService(session)
    batch = svc.create_draft(
        actor,
        region_id=w["region"].id,
        week_start=MONDAY,
        entries=[_day_entry(s.id, rep.id, MONDAY) for s in shops],
    )
    svc.publish(actor, batch_id=batch.id)
    session.flush()

    # 模拟“历史数据”：发布时还没接入自动沉淀，模板为空
    session.execute(sa_delete(ScheduleTemplate))
    session.flush()

    template_svc = ScheduleTemplateService(session)
    first = template_svc.sync_from_published(actor, region_id=w["region"].id)
    assert first == {"created": 2, "updated": 0, "skipped": 0, "source_entries": 2}

    second = template_svc.sync_from_published(actor, region_id=w["region"].id)
    assert second == {"created": 0, "updated": 0, "skipped": 2, "source_entries": 2}

    third = template_svc.sync_from_published(
        actor, region_id=w["region"].id, overwrite=True
    )
    assert third == {"created": 0, "updated": 2, "skipped": 0, "source_entries": 2}

    templates = list(session.scalars(
        select(ScheduleTemplate).where(ScheduleTemplate.region_id == w["region"].id)
    ).all())
    assert {t.merchant_id for t in templates} == {s.id for s in shops}
    assert {t.assigned_user_id for t in templates} == {rep.id}
    assert {t.weekday for t in templates} == {1}


def test_coverage_gap_without_region_aggregates_visible_regions(session: Session) -> None:
    """周视图默认“全部可见片区”：覆盖缺口要按可见片区汇总，而不是报 422/空。"""
    w = _world(session)
    _mgr, actor = _mgr_actor(session, w, "mgr-week-gap-all")
    rep = _user(session, w["ds"], RoleCode.SALES_REP, "rep-week-gap-all")
    shops = _make_merchants(session, w, 3)
    for shop in shops:
        _assign_primary(session, shop, rep)

    svc = ScheduleService(session)
    batch = svc.create_draft(
        actor,
        region_id=w["region"].id,
        week_start=MONDAY,
        entries=[_day_entry(shops[0].id, rep.id, MONDAY)],
    )
    svc.publish(actor, batch_id=batch.id)
    session.flush()

    gap = svc.coverage_gap(actor, region_id=None, week_start=MONDAY)
    assert gap.total_active == 3
    assert gap.covered == 1
    assert gap.gap == 2


def test_assignee_options_without_region_returns_all_visible(session: Session) -> None:
    """不选片区时人员下拉也要有数据（草稿页默认全部片区）。"""
    w = _world(session)
    _mgr, actor = _mgr_actor(session, w, "mgr-week-rep-all")
    rep_a = _user(session, w["ds"], RoleCode.SALES_REP, "rep-wrap-all-a")
    rep_b = _user(session, w["ds"], RoleCode.SALES_REP, "rep-wrap-all-b")

    rows = ScheduleService(session).list_assignee_options(actor)
    ids = {u.id for u, _names in rows}
    assert {rep_a.id, rep_b.id} <= ids


def test_merchant_candidates_mark_scheduled_and_include_new_active(
    session: Session,
) -> None:
    """草稿候选：跨片区返回启用商家、标记本周已排、新启用商家立即可查。"""
    w = _world(session)
    _mgr, actor = _mgr_actor(session, w, "mgr-week-candidates")
    rep = _user(session, w["ds"], RoleCode.SALES_REP, "rep-week-candidates")
    shops = _make_merchants(session, w, 3)
    for shop in shops:
        _assign_primary(session, shop, rep)

    svc = ScheduleService(session)
    batch = svc.create_draft(
        actor,
        region_id=w["region"].id,
        week_start=MONDAY,
        entries=[_day_entry(shops[0].id, rep.id, MONDAY)],
    )
    svc.publish(actor, batch_id=batch.id)
    session.flush()

    rows = svc.list_merchant_candidates(actor, week_start=MONDAY)
    by_id = {m.id: (scheduled, region_name, owner) for m, region_name, scheduled, owner in rows}
    assert set(shop.id for shop in shops) <= set(by_id)
    assert by_id[shops[0].id][0] is True
    assert by_id[shops[1].id][0] is False
    assert by_id[shops[1].id][1] == w["region"].name
    assert by_id[shops[1].id][2] == rep.id

    # 新启用的商家：下一次查询立即出现
    new_shop = _make_merchants(session, w, 1)[0]
    _assign_primary(session, new_shop, rep)
    session.flush()
    rows2 = svc.list_merchant_candidates(actor, week_start=MONDAY)
    assert new_shop.id in {m.id for m, _r, _s, _o in rows2}

    # 暂停的商家不再作为候选
    shops[2].status = "PAUSED"
    session.flush()
    rows3 = svc.list_merchant_candidates(actor, week_start=MONDAY)
    assert shops[2].id not in {m.id for m, _r, _s, _o in rows3}


def test_list_templates_without_region_returns_all_visible(session: Session) -> None:
    """模板列表不传 region_id 时返回可见片区全部模板（带片区名），不再被默认片区过滤掉。"""
    w = _world(session)
    _mgr, actor = _mgr_actor(session, w, "mgr-week-list-all")
    rep = _user(session, w["ds"], RoleCode.SALES_REP, "rep-week-list-all")
    shops = _make_merchants(session, w, 3)
    for shop in shops:
        _assign_primary(session, shop, rep)

    svc = ScheduleService(session)
    batch = svc.create_draft(
        actor,
        region_id=w["region"].id,
        week_start=MONDAY,
        entries=[_day_entry(s.id, rep.id, MONDAY) for s in shops],
    )
    svc.publish(actor, batch_id=batch.id)
    session.flush()

    rows = ScheduleTemplateService(session).list_templates(actor)
    assert len(rows) == 3
    assert {region_name for _tpl, _m, _u, region_name in rows} == {w["region"].name}


# ------------------------- 已发布周增量追加 -------------------------


def test_append_entries_to_published_week(session: Session) -> None:
    """已发布周继续追加商家：自动顺延时段、生成任务/占用，并沉淀模板。"""
    w = _world(session)
    _mgr, actor = _mgr_actor(session, w, "mgr-week-append")
    rep = _user(session, w["ds"], RoleCode.SALES_REP, "rep-week-append")
    shops = _make_merchants(session, w, 3)
    for shop in shops:
        _assign_primary(session, shop, rep)

    svc = ScheduleService(session)
    batch = svc.create_draft(
        actor,
        region_id=w["region"].id,
        week_start=MONDAY,
        entries=[_day_entry(shops[0].id, rep.id, MONDAY)],
    )
    svc.publish(actor, batch_id=batch.id)
    session.flush()

    svc.append_entries(
        actor,
        batch_id=batch.id,
        entries=[
            _day_entry(shops[1].id, rep.id, MONDAY),
            _day_entry(shops[2].id, rep.id, MONDAY),
        ],
    )
    session.flush()

    entries = svc.repo.list_entries(batch.id)
    assert len(entries) == 3
    assert all(e.visit_task_id and e.reservation_id for e in entries)

    reservations = list(session.scalars(
        select(ResourceReservation)
        .where(ResourceReservation.merchant_id.in_([s.id for s in shops]))
        .order_by(ResourceReservation.start_at.asc())
    ).all())
    assert len(reservations) == 3
    starts = [r.start_at for r in reservations]
    assert starts == [
        datetime.combine(MONDAY, time(9, 0), tzinfo=TZ).astimezone(UTC),
        datetime.combine(MONDAY, time(9, 30), tzinfo=TZ).astimezone(UTC),
        datetime.combine(MONDAY, time(10, 0), tzinfo=TZ).astimezone(UTC),
    ]

    # 追加的商家也进入每周模板，下周自动沿用
    templates = list(session.scalars(
        select(ScheduleTemplate).where(ScheduleTemplate.region_id == w["region"].id)
    ).all())
    assert {t.merchant_id for t in templates} == {s.id for s in shops}

    # 同一商家重复追加被拒绝
    with pytest.raises(DomainError) as exc:
        with session.begin_nested():
            svc.append_entries(
                actor,
                batch_id=batch.id,
                entries=[_day_entry(shops[1].id, rep.id, MONDAY)],
            )
    assert exc.value.code == "SCHEDULE_MERCHANT_ALREADY_SCHEDULED"


# ------------------------- 按模板重排/补齐本周 -------------------------


def _publish_week(session: Session, actor, region_id, entries) -> object:
    svc = ScheduleService(session)
    batch = svc.create_draft(
        actor, region_id=region_id, week_start=MONDAY, entries=entries
    )
    svc.publish(actor, batch_id=batch.id)
    session.flush()
    return batch


def test_apply_backfills_missing_merchants_on_published_week(session: Session) -> None:
    """已发布周：模板里新增的商家由 apply 增量补齐，而不是静默返回旧批次。"""
    w = _world(session)
    _mgr, actor = _mgr_actor(session, w, "mgr-week-apply-fill")
    rep = _user(session, w["ds"], RoleCode.SALES_REP, "rep-week-apply-fill")
    shops = _make_merchants(session, w, 2)
    for shop in shops:
        _assign_primary(session, shop, rep)

    batch = _publish_week(
        session, actor, w["region"].id, [_day_entry(shops[0].id, rep.id, MONDAY)]
    )

    # 模板补上第二家（本周尚未排入）
    ScheduleTemplateService(session).create_entries(
        actor,
        region_id=w["region"].id,
        entries=[
            {
                "merchant_id": shops[1].id,
                "assigned_user_id": rep.id,
                "weekday": 1,
                "start_time": time(10, 0),
                "end_time": time(10, 30),
                "priority": 2,
            }
        ],
    )
    session.flush()

    published, created, total = ScheduleTemplateService(session).apply(
        actor, region_id=w["region"].id, week_start=MONDAY
    )
    assert published.id == batch.id
    assert (created, total) == (1, 2)
    entries = ScheduleService(session).repo.list_entries(batch.id)
    assert {e.merchant_id for e in entries} == {s.id for s in shops}


def test_apply_replace_rebuilds_week_from_template(session: Session) -> None:
    """replace=True：撤销本周已发布排班，按模板整周重排。"""
    from sqlalchemy import delete as sa_delete

    w = _world(session)
    _mgr, actor = _mgr_actor(session, w, "mgr-week-apply-replace")
    rep = _user(session, w["ds"], RoleCode.SALES_REP, "rep-week-apply-replace")
    shops = _make_merchants(session, w, 2)
    for shop in shops:
        _assign_primary(session, shop, rep)

    batch = _publish_week(
        session,
        actor,
        w["region"].id,
        [_day_entry(s.id, rep.id, MONDAY) for s in shops],
    )

    # 模板改成：只保留第一家，并挪到周三
    tpl = session.scalars(
        select(ScheduleTemplate).where(ScheduleTemplate.merchant_id == shops[0].id)
    ).first()
    tpl.weekday = 3
    session.execute(
        sa_delete(ScheduleTemplate).where(ScheduleTemplate.merchant_id == shops[1].id)
    )
    session.flush()

    published, created, total = ScheduleTemplateService(session).apply(
        actor, region_id=w["region"].id, week_start=MONDAY, replace=True
    )
    assert published.id != batch.id
    assert (created, total) == (1, 1)
    entries = ScheduleService(session).repo.list_entries(published.id)
    assert len(entries) == 1
    assert entries[0].merchant_id == shops[0].id
    assert entries[0].planned_date == MONDAY + timedelta(days=2)


def test_apply_replace_rejected_when_visit_started(session: Session) -> None:
    """本周已有任务开始执行 → 拒绝整周重排，避免抹掉现场数据。"""
    w = _world(session)
    _mgr, actor = _mgr_actor(session, w, "mgr-week-apply-locked")
    rep = _user(session, w["ds"], RoleCode.SALES_REP, "rep-week-apply-locked")
    shops = _make_merchants(session, w, 1)
    _assign_primary(session, shops[0], rep)

    batch = _publish_week(
        session, actor, w["region"].id, [_day_entry(shops[0].id, rep.id, MONDAY)]
    )

    task = session.scalars(
        select(VisitTask).where(VisitTask.merchant_id == shops[0].id)
    ).first()
    task.status = "IN_PROGRESS"
    session.flush()

    with pytest.raises(DomainError) as exc:
        with session.begin_nested():
            ScheduleTemplateService(session).apply(
                actor, region_id=w["region"].id, week_start=MONDAY, replace=True
            )
    assert exc.value.code == "SCHEDULE_WEEK_LOCKED"
    # 原批次仍在（拒绝时零写入）
    assert ScheduleService(session).repo.get_batch(batch.id) is not None


def test_sync_templates_can_target_specific_week(session: Session) -> None:
    """「用本周排班覆盖模板」：指定源周，不被已提前生成的下周覆盖。"""
    w = _world(session)
    _mgr, actor = _mgr_actor(session, w, "mgr-week-sync-week")
    rep_a = _user(session, w["ds"], RoleCode.SALES_REP, "rep-week-sync-week-a")
    rep_b = _user(session, w["ds"], RoleCode.SALES_REP, "rep-week-sync-week-b")
    shops = _make_merchants(session, w, 1)
    shop = shops[0]
    _assign_primary(session, shop, rep_a)

    # 本周：rep_a 周一
    _publish_week(session, actor, w["region"].id, [_day_entry(shop.id, rep_a.id, MONDAY)])

    # 模拟「模板落后于本周实际排班」：模板被手工改成 rep_b 周三
    tpl = session.scalars(
        select(ScheduleTemplate).where(ScheduleTemplate.merchant_id == shop.id)
    ).first()
    tpl.assigned_user_id = rep_b.id
    tpl.weekday = 3
    session.flush()
    assert (tpl.assigned_user_id, tpl.weekday) == (rep_b.id, 3)

    # 用「本周」覆盖模板 → 回到 rep_a 周一
    result = ScheduleTemplateService(session).sync_from_published(
        actor, region_id=w["region"].id, overwrite=True, week_start=MONDAY
    )
    assert result["updated"] == 1
    session.flush()
    session.refresh(tpl)
    assert (tpl.assigned_user_id, tpl.weekday) == (rep_a.id, 1)


def test_api_week_view_exposes_batch_id_and_append(session: Session) -> None:
    """路由层：week-view 回传 batch_id（前端据此追加），append 返回最新条目数。"""
    from app.api.schedules import append_batch_entries, week_view as week_view_api
    from app.schemas.schedule import AppendEntriesRequest, ScheduleEntryInput

    w = _world(session)
    _mgr, actor = _mgr_actor(session, w, "mgr-week-api")
    rep = _user(session, w["ds"], RoleCode.SALES_REP, "rep-week-api")
    shops = _make_merchants(session, w, 2)
    for shop in shops:
        _assign_primary(session, shop, rep)

    svc = ScheduleService(session)
    batch = svc.create_draft(
        actor,
        region_id=w["region"].id,
        week_start=MONDAY,
        entries=[_day_entry(shops[0].id, rep.id, MONDAY)],
    )
    svc.publish(actor, batch_id=batch.id)
    session.flush()

    view = week_view_api(
        week_start=MONDAY,
        region_id=w["region"].id,
        user_id=None,
        session=session,
        actor=actor,
    )
    assert len(view.entries) == 1
    assert view.entries[0].batch_id == batch.id
    assert view.entries[0].projected is False

    updated = append_batch_entries(
        batch_id=batch.id,
        body=AppendEntriesRequest(
            entries=[
                ScheduleEntryInput(
                    merchant_id=shops[1].id,
                    assigned_user_id=rep.id,
                    planned_date=MONDAY,
                )
            ]
        ),
        session=session,
        actor=actor,
    )
    assert updated.entry_count == 2


def test_append_rejected_on_draft_batch(session: Session) -> None:
    """草稿批次不能走追加：应直接在草稿里补充后一次发布。"""
    w = _world(session)
    _mgr, actor = _mgr_actor(session, w, "mgr-week-append-draft")
    rep = _user(session, w["ds"], RoleCode.SALES_REP, "rep-week-append-draft")
    shops = _make_merchants(session, w, 2)
    for shop in shops:
        _assign_primary(session, shop, rep)

    svc = ScheduleService(session)
    batch = svc.create_draft(
        actor,
        region_id=w["region"].id,
        week_start=MONDAY,
        entries=[_day_entry(shops[0].id, rep.id, MONDAY)],
    )
    with pytest.raises(DomainError) as exc:
        svc.append_entries(
            actor,
            batch_id=batch.id,
            entries=[_day_entry(shops[1].id, rep.id, MONDAY)],
        )
    assert exc.value.code == "SCHEDULE_BATCH_NOT_PUBLISHED"
