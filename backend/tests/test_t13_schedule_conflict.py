"""T13 周排班与冲突原子校验（PRD §4.7）。

- Q068 人员时间重叠：半开区间 [start,end)，端点相接不冲突，相交才冲突；
- Q069 通用 ResourceReservation 冲突夹具：CAMPAIGN 等任意来源占用都参与避让；
- Q070 批内重复商家：同人同商家同日冲突；
- Q071 一项冲突整批零写入（全有或全无）；
- Q072 并发发布只成功一次（咨询锁 + 已发布唯一 + GiST 排他兜底）；
- Q073 周覆盖缺口：ACTIVE 商家 7 天至少一次，未排/未巡者计入缺口。

冲突口径唯一来自 ScheduleConflictService，本测试不自算规则。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker
from tests.test_t09_contract import _ctx, _merchant, _user, _world
from tests.test_t11_visit import _assign_primary

from app.core.permissions import ActorContext
from app.models import (
    ResourceReservation,
    SchedulePlanBatch,
    SchedulePlanEntry,
    UserRegionBrandAuth,
    VisitTask,
)
from app.models.enums import RoleCode
from app.services.schedule_conflict_service import (
    Occupancy,
    ScheduleConflictService,
)
from app.services.schedule_service import (
    ScheduleConflictError,
    ScheduleService,
    monday_of,
)

TZ = ZoneInfo("Asia/Shanghai")


# ----------------------------- 造数辅助 -----------------------------


def _mgr_actor(session, w, tag="mgr13"):
    mgr = _user(session, w["ds"], RoleCode.REGIONAL_MANAGER, tag)
    session.add(UserRegionBrandAuth(
        user_id=mgr.id, region_id=w["region"].id, brand_id=w["brand"].id))
    session.flush()
    actor = _ctx(mgr, RoleCode.REGIONAL_MANAGER.value, frozenset({w["region"].id}))
    return mgr, actor


def _make_merchants(session, w, n: int, *, grade="B"):
    out = []
    for i in range(n):
        m = _merchant(session, w, name=f"T13店-{i}-{uuid4().hex[:5]}")
        m.grade = grade
        session.flush()
        out.append(m)
    return out


def _entry(merchant_id, user_id, planned: date, hour=9):
    start = datetime.combine(planned, time(hour, 0), tzinfo=TZ).astimezone(UTC)
    end = datetime.combine(planned, time(hour, 30), tzinfo=TZ).astimezone(UTC)
    return {
        "merchant_id": merchant_id,
        "assigned_user_id": user_id,
        "planned_date": planned,
        "start_at": start,
        "end_at": end,
        "priority": 2,
    }


def _cleanup_committed(engine, cleanup: dict) -> None:
    """删除 Q072 真实提交产生的夹具数据（仅测试清理），按 FK 依赖顺序。"""
    from sqlalchemy import delete as sa_delete

    from app.models import (
        AuditLog,
        Brand,
        Organization,
        Region,
    )
    from app.models import (
        Merchant as M,
    )
    from app.models import (
        MerchantAssignment as MA,
    )
    from app.models import (
        ResourceReservation as RR,
    )
    from app.models import (
        Role as RoleModel,
    )
    from app.models import (
        SchedulePlanBatch as SPB,
    )
    from app.models import (
        SchedulePlanEntry as SPE,
    )
    from app.models import (
        ScheduleTemplate as ST,
    )
    from app.models import (
        User as U,
    )
    from app.models import (
        UserRegionBrandAuth as A,
    )
    from app.models import (
        VisitTask as VT,
    )
    s = sessionmaker(bind=engine, future=True)()
    try:
        mids = cleanup["merchants"]
        region_id = cleanup["region_id"]
        batch_ids = select(SPB.id).where(SPB.region_id == region_id)
        # 审计日志 actor_id RESTRICT users：先删本测试批次与操作者相关的审计行
        s.execute(sa_delete(AuditLog).where(
            (AuditLog.object_type == "SCHEDULE_PLAN_BATCH")
            & AuditLog.object_id.in_(batch_ids)))
        # 子表先删（RR 按本测试批次的 dataset 删除）
        s.execute(sa_delete(RR).where(RR.dataset_id.in_(
            select(SPB.dataset_id).where(SPB.region_id == region_id))))
        s.execute(sa_delete(VT).where(VT.merchant_id.in_(mids)))
        # 发布时会自动沉淀每周循环模板（引用 user/merchant），清理时必须先删
        s.execute(sa_delete(ST).where(ST.region_id == region_id))
        s.execute(sa_delete(SPE).where(SPE.merchant_id.in_(mids)))
        s.execute(sa_delete(SPB).where(SPB.region_id == region_id))
        s.execute(sa_delete(MA).where(MA.merchant_id.in_(mids)))
        s.execute(sa_delete(A).where(A.region_id == region_id))
        s.execute(sa_delete(M).where(M.id.in_(mids)))
        s.execute(sa_delete(AuditLog).where(
            AuditLog.actor_id.in_(cleanup["users"])))
        s.execute(sa_delete(U).where(U.id.in_(cleanup["users"])))
        # world 层按 FK 顺序：先 Region（引用 Organization），再 Brand/Role，最后 Organization
        world = {model: ds for model, ds in cleanup["world"]}
        for model in (Region, Brand, RoleModel, Organization):
            s.execute(sa_delete(model).where(model.dataset_id == world[model]))
        s.commit()
    finally:
        s.close()


def _draft(session, actor, w, entries):
    return ScheduleService(session).create_draft(
        actor,
        region_id=w["region"].id,
        week_start=monday_of(entries[0]["planned_date"]),
        entries=entries,
    )


# ----------------------------- Q068 -----------------------------


def test_q068_personnel_time_overlap_half_open(session: Session) -> None:
    """同一业务员半开区间重叠才冲突；端点相接（09:00-10:00 vs 10:00-11:00）不冲突。"""
    w = _world(session)
    rep = _user(session, w["ds"], RoleCode.SALES_REP, "rep13a")
    u = rep.id
    base = datetime(2026, 12, 14, 1, 0, tzinfo=UTC)  # 09:00 上海

    def occ(key, h0, h1):
        return Occupancy(key=key, resource_user_id=u,
                         start_at=base + timedelta(hours=h0),
                         end_at=base + timedelta(hours=h1))

    svc = ScheduleConflictService(session)
    # 相交 15 分钟 → 冲突
    c = svc.detect([occ("a", 0, 1), occ("b", 0.75, 2)], dataset_id=w["ds"])
    assert [x.type for x in c] == ["TIME_OVERLAP"]
    assert {c[0].left_key, c[0].right_key} == {"a", "b"}

    # 端点相接（半开）→ 不冲突
    c2 = svc.detect([occ("a", 0, 1), occ("b", 1, 2)], dataset_id=w["ds"])
    assert c2 == []

    # 不同业务员 → 不冲突
    other = uuid4()
    c3 = svc.detect([
        Occupancy(key="a", resource_user_id=u, start_at=base, end_at=base + timedelta(hours=1)),
        Occupancy(key="b", resource_user_id=other, start_at=base, end_at=base + timedelta(hours=1)),
    ], dataset_id=w["ds"])
    assert c3 == []


# ----------------------------- Q069 -----------------------------


def test_q069_generic_resource_reservation_conflict(session: Session) -> None:
    """ResourceReservation 是通用占用源：CAMPAIGN 占用也会让排班候选检出时间冲突。"""
    w = _world(session)
    rep = _user(session, w["ds"], RoleCode.SALES_REP, "rep13b")
    shop = _make_merchants(session, w, 1)[0]
    monday = date(2026, 12, 14)
    start = datetime.combine(monday, time(9, 0), tzinfo=TZ).astimezone(UTC)
    end = datetime.combine(monday, time(12, 0), tzinfo=TZ).astimezone(UTC)

    # 既有活动占用（T15 才正式接入活动实体；这里直接验证通用源可承载 CAMPAIGN）
    session.add(ResourceReservation(
        resource_user_id=rep.id,
        start_at=start, end_at=end,
        occupied_range=func.tstzrange(start, end, "[)"),
        source_type="CAMPAIGN", status="ACTIVE",
        merchant_id=shop.id,
        source_object_type="CAMPAIGN", source_object_id=uuid4(),
        dataset_type="PRODUCTION", dataset_id=w["ds"],
    ))
    session.flush()

    candidate = Occupancy(
        key="cand", resource_user_id=rep.id,
        start_at=datetime.combine(monday, time(11, 0), tzinfo=TZ).astimezone(UTC),
        end_at=datetime.combine(monday, time(11, 30), tzinfo=TZ).astimezone(UTC),
        merchant_id=shop.id, planned_date=monday,
    )
    conflicts = ScheduleConflictService(session).detect(
        [candidate], dataset_id=w["ds"],
        window_start=start - timedelta(days=1), window_end=end + timedelta(days=1),
    )
    types = {c.type for c in conflicts}
    assert "TIME_OVERLAP" in types  # 与 CAMPAIGN 占用时间重叠
    # 冲突右侧指向既有占用 id（通用源不区分来源类型即可避让）；按 dataset 收窄
    existing = session.scalars(
        select(ResourceReservation).where(ResourceReservation.dataset_id == w["ds"])
    ).one()
    assert any(c.right_key == str(existing.id) for c in conflicts)


# ----------------------------- Q070 -----------------------------


def test_q070_duplicate_merchant_in_batch(session: Session) -> None:
    """批内同一业务员同一天被安排同一商家两次 → DUPLICATE_MERCHANT 冲突。"""
    w = _world(session)
    rep = _user(session, w["ds"], RoleCode.SALES_REP, "rep13c")
    shop = _make_merchants(session, w, 1)[0]
    d = date(2026, 12, 15)
    c1 = Occupancy(key="1", resource_user_id=rep.id,
                   start_at=datetime.combine(d, time(9, 0), tzinfo=TZ).astimezone(UTC),
                   end_at=datetime.combine(d, time(9, 30), tzinfo=TZ).astimezone(UTC),
                   merchant_id=shop.id, planned_date=d)
    c2 = Occupancy(key="2", resource_user_id=rep.id,
                   start_at=datetime.combine(d, time(15, 0), tzinfo=TZ).astimezone(UTC),
                   end_at=datetime.combine(d, time(15, 30), tzinfo=TZ).astimezone(UTC),
                   merchant_id=shop.id, planned_date=d)
    conflicts = ScheduleConflictService(session).detect([c1, c2], dataset_id=w["ds"])
    assert [c.type for c in conflicts] == ["DUPLICATE_MERCHANT"]


# ----------------------------- Q071 -----------------------------


def test_q071_one_conflict_whole_batch_zero_writes(session: Session) -> None:
    """发布批次含任意一条冲突 → 整批拒绝，VisitTask/占用零写入，批次停留 DRAFT。"""
    w = _world(session)
    mgr, actor = _mgr_actor(session, w)
    rep = _user(session, w["ds"], RoleCode.SALES_REP, "rep13d")
    shops = _make_merchants(session, w, 3)
    for s in shops:
        _assign_primary(session, s, rep)
    monday = date(2026, 12, 14)

    # 先发布一个无冲突批次，占用 rep 周一 09:00-09:30（店0）
    batch1 = _draft(session, actor, w, [_entry(shops[0].id, rep.id, monday, 9)])
    ScheduleService(session).publish(actor, batch_id=batch1.id)
    session.flush()
    tasks_after_first = session.scalar(select(func.count()).select_from(VisitTask))

    # 第二批：店1 与既有占用时间重叠；店2 本来合法，但整批必须零写入
    batch2 = _draft(session, actor, w, [
        _entry(shops[1].id, rep.id, monday, 9),      # 与已发布占用冲突
        _entry(shops[2].id, rep.id, monday + timedelta(days=2), 14),
    ])
    # 用 SAVEPOINT 精确模拟 Router 的“请求事务”：异常只回滚第二批，第一批保留
    with pytest.raises(ScheduleConflictError) as exc:
        with session.begin_nested():
            ScheduleService(session).publish(actor, batch_id=batch2.id)

    assert any(c.type == "TIME_OVERLAP" for c in exc.value.conflicts)
    # 全有或全无：任务/占用没有因为第二批而增加
    session.expire_all()
    assert session.scalar(select(func.count()).select_from(VisitTask)) == tasks_after_first
    assert session.scalar(
        select(func.count()).select_from(ResourceReservation)
        .where(ResourceReservation.merchant_id == shops[2].id)
    ) == 0
    b2 = session.get(SchedulePlanBatch, batch2.id)
    assert b2.status == "DRAFT"


# ----------------------------- Q073 -----------------------------


def test_q073_week_coverage_gap(session: Session) -> None:
    """周覆盖缺口：4 个 ACTIVE 商家，只排 1 个 → gap=3、rate=0.25，missing 含未排三家。"""
    w = _world(session)
    mgr, actor = _mgr_actor(session, w, "mgr13gap")
    rep = _user(session, w["ds"], RoleCode.SALES_REP, "rep13gap")
    shops = _make_merchants(session, w, 4)
    for s in shops:
        _assign_primary(session, s, rep)
    monday = date(2026, 12, 14)

    batch = _draft(session, actor, w, [_entry(shops[0].id, rep.id, monday, 10)])
    ScheduleService(session).publish(actor, batch_id=batch.id)
    session.flush()

    gap = ScheduleService(session).coverage_gap(
        actor, region_id=w["region"].id, week_start=monday
    )
    assert gap.total_active == 4
    assert gap.covered == 1
    assert gap.gap == 3
    assert gap.rate == pytest.approx(0.25, abs=1e-4)
    assert shops[0].id not in gap.missing_merchant_ids
    assert {s.id for s in shops[1:]} == set(gap.missing_merchant_ids)


def test_suggest_does_not_autopublish(session: Session) -> None:
    """建议只读：suggest 不得产生任何批次/任务/占用（红线：不把建议当自动发布）。"""
    w = _world(session)
    mgr, actor = _mgr_actor(session, w, "mgr13sug")
    _make_merchants(session, w, 2)
    monday = date(2026, 12, 14)
    result = ScheduleService(session).suggest(
        actor, region_id=w["region"].id, week_start=monday
    )
    assert len(result) == 2
    ds = w["ds"]
    assert session.scalar(
        select(func.count()).select_from(SchedulePlanBatch)
        .where(SchedulePlanBatch.dataset_id == ds)
    ) == 0
    assert session.scalar(
        select(func.count()).select_from(SchedulePlanEntry)
        .where(SchedulePlanEntry.dataset_id == ds)
    ) == 0
    assert session.scalar(
        select(func.count()).select_from(VisitTask)
        .where(VisitTask.dataset_id == ds)
    ) == 0


# ----------------------------- Q072（并发，真实独立连接） -----------------------------


def test_zzz_q072_concurrent_publish_single_success(applied_engine) -> None:
    """并发发布同一 (region,周)：两事务都尝试，最终只有一批成功（单次成功）。

    使用独立连接 + 真实提交（咨询锁在事务内、提交后释放），不依赖外层 savepoint。
    """
    from app.models import Brand, Organization, Region
    from app.models import Role as RoleModel

    maker = sessionmaker(bind=applied_engine, expire_on_commit=False, future=True)
    setup = maker()
    cleanup: dict[str, list] = {}
    try:
        w = _world(setup)
        cleanup["world"] = [
            (Organization, w["ds"]), (Brand, w["ds"]), (Region, w["ds"]),
            (RoleModel, w["ds"]),
        ]
        mgr = _user(setup, w["ds"], RoleCode.REGIONAL_MANAGER, "mgr13conc")
        setup.add(UserRegionBrandAuth(
            user_id=mgr.id, region_id=w["region"].id, brand_id=w["brand"].id))
        rep = _user(setup, w["ds"], RoleCode.SALES_REP, "rep13conc")
        cleanup["users"] = [mgr.id, rep.id]
        setup_shops = _make_merchants(setup, w, 2)
        cleanup["merchants"] = [s.id for s in setup_shops]
        cleanup["region_id"] = w["region"].id
        for s in setup_shops:
            _assign_primary(setup, s, rep)
        mgr_actor = _ctx(mgr, RoleCode.REGIONAL_MANAGER.value, frozenset({w["region"].id}))
        monday = date(2026, 12, 14)

        # 两个不同批次，排不同商家不同时段（彼此无业务冲突），竞争同一周发布权
        b1 = ScheduleService(setup).create_draft(
            mgr_actor, region_id=w["region"].id, week_start=monday,
            entries=[_entry(setup_shops[0].id, rep.id, monday, 9)])
        b2 = ScheduleService(setup).create_draft(
            mgr_actor, region_id=w["region"].id, week_start=monday,
            entries=[_entry(setup_shops[1].id, rep.id, monday, 11)])
        setup.commit()
        region_id, ds_id = w["region"].id, w["ds"]
        mgr_id, b1_id, b2_id = mgr.id, b1.id, b2.id
    finally:
        setup.close()

    import threading

    outcomes: list[str] = []
    barrier = threading.Barrier(2)  # 两线程都就绪后同时抢同一把 (region,周) 咨询锁

    def publish(batch_id: UUID) -> None:
        s = maker()
        try:
            barrier.wait(timeout=5)  # 真正并发：同时发起，谁先拿锁由 PG 调度决定
            actor = ActorContext(
                user_id=mgr_id,
                role_code=RoleCode.REGIONAL_MANAGER.value,
                region_ids=frozenset({region_id}),
                brand_ids=frozenset(),
            )
            ScheduleService(s).publish(actor, batch_id=batch_id)
            s.commit()
            outcomes.append("committed")
        except Exception as exc:  # noqa: BLE001
            s.rollback()
            outcomes.append(f"rejected:{getattr(exc, 'code', type(exc).__name__)}")
        finally:
            s.close()

    # 咨询锁串行化并发发布；先到者提交，后到者在持锁后读到同周已发布而拒绝
    t1 = threading.Thread(target=publish, args=(b1_id,))
    t2 = threading.Thread(target=publish, args=(b2_id,))
    t1.start()
    t2.start()
    t1.join(timeout=15)
    t2.join(timeout=15)
    assert not t1.is_alive() and not t2.is_alive()

    assert sorted(outcomes).count("committed") == 1, outcomes
    assert len(outcomes) == 2
    rejected = [o for o in outcomes if o.startswith("rejected:")]
    assert len(rejected) == 1

    # 校验落库：该区域该周恰好一个已发布批次
    verify = maker()
    try:
        published = verify.scalars(
            select(SchedulePlanBatch)
            .where(SchedulePlanBatch.region_id == region_id,
                   SchedulePlanBatch.dataset_id == ds_id,
                   SchedulePlanBatch.week_start_date == monday,
                   SchedulePlanBatch.status == "PUBLISHED")
        ).all()
        assert len(published) == 1
        # 只有成功那批生成了任务/占用（失败批零写入）
        task_count = verify.scalar(
            select(func.count()).select_from(VisitTask).where(VisitTask.dataset_id == ds_id)
        )
        res_count = verify.scalar(
            select(func.count()).select_from(ResourceReservation)
            .where(ResourceReservation.dataset_id == ds_id)
        )
        assert task_count == 1
        assert res_count == 1
    finally:
        verify.close()

    # 夹具清理：Q072 用真实提交（非 savepoint），删除本测试 dataset 的持久化数据，
    # 避免重复运行/全量套件累积污染（仅测试数据清理，非业务物理删除）。
    _cleanup_committed(applied_engine, cleanup)
