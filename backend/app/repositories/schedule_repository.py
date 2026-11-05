"""周排班持久化（Repository 只做持久化，不含业务裁决）。"""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from geoalchemy2 import functions as gfunc
from sqlalchemy import String, cast, func, literal, or_, select
from sqlalchemy.orm import Session

from app.models import (
    Merchant,
    MerchantAssignment,
    Region,
    ReplenishmentOrder,
    ResourceReservation,
    Role,
    SchedulePlanBatch,
    SchedulePlanEntry,
    ScheduleTemplate,
    User,
    UserRegionBrandAuth,
    VisitRecord,
    VisitTask,
)
from app.models.enums import ReplenishmentStatus, VisitRecordStatus

SOURCE_TYPE_SCHEDULE = "SCHEDULE"
SOURCE_OBJECT_BATCH = "SCHEDULE_PLAN_BATCH"
ACTIVE_RESERVATION = "ACTIVE"


class ScheduleRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    # ---------------- 批次 / 条目 ----------------

    def get_region(self, region_id: UUID) -> Region | None:
        return self.session.get(Region, region_id)

    def add_batch(self, batch: SchedulePlanBatch) -> SchedulePlanBatch:
        self.session.add(batch)
        self.session.flush()
        return batch

    def get_batch(self, batch_id: UUID) -> SchedulePlanBatch | None:
        return self.session.get(SchedulePlanBatch, batch_id)

    def find_published_batch(
        self, region_id: UUID, week_start: date, dataset_id: UUID
    ) -> SchedulePlanBatch | None:
        return self.session.scalars(
            select(SchedulePlanBatch)
            .where(SchedulePlanBatch.region_id == region_id)
            .where(SchedulePlanBatch.week_start_date == week_start)
            .where(SchedulePlanBatch.dataset_id == dataset_id)
            .where(SchedulePlanBatch.status == "PUBLISHED")
        ).first()

    def list_entries(self, batch_id: UUID) -> list[SchedulePlanEntry]:
        return list(
            self.session.scalars(
                select(SchedulePlanEntry)
                .where(SchedulePlanEntry.batch_id == batch_id)
                .order_by(SchedulePlanEntry.planned_date.asc(),
                          SchedulePlanEntry.priority.asc())
            ).all()
        )

    # ---------------- 每周循环模板 ----------------

    def list_templates(
        self, region_id: UUID, dataset_id: UUID
    ) -> list[tuple[ScheduleTemplate, Merchant, User]]:
        rows = self.session.execute(
            select(ScheduleTemplate, Merchant, User)
            .join(Merchant, Merchant.id == ScheduleTemplate.merchant_id)
            .join(User, User.id == ScheduleTemplate.assigned_user_id)
            .where(ScheduleTemplate.region_id == region_id)
            .where(ScheduleTemplate.dataset_id == dataset_id)
            .where(ScheduleTemplate.is_active.is_(True))
            .order_by(
                ScheduleTemplate.weekday.asc(),
                ScheduleTemplate.start_time.asc(),
                Merchant.name.asc(),
            )
        ).all()
        return [(tpl, merchant, user) for tpl, merchant, user in rows]

    def get_template(self, template_id: UUID) -> ScheduleTemplate | None:
        return self.session.get(ScheduleTemplate, template_id)

    def list_templates_detail(
        self, region_ids: set[UUID] | None, dataset_id: UUID | None = None
    ) -> list[tuple[ScheduleTemplate, Merchant, User, str]]:
        """模板列表（跨片区）：带片区名，供「全部可见片区」视图使用。"""
        stmt = (
            select(ScheduleTemplate, Merchant, User, Region.name)
            .join(Merchant, Merchant.id == ScheduleTemplate.merchant_id)
            .join(User, User.id == ScheduleTemplate.assigned_user_id)
            .join(Region, Region.id == ScheduleTemplate.region_id)
            .where(ScheduleTemplate.is_active.is_(True))
            .order_by(
                Region.name.asc(),
                ScheduleTemplate.weekday.asc(),
                ScheduleTemplate.start_time.asc(),
                Merchant.name.asc(),
            )
        )
        if dataset_id is not None:
            stmt = stmt.where(ScheduleTemplate.dataset_id == dataset_id)
        if region_ids is not None:
            stmt = stmt.where(ScheduleTemplate.region_id.in_(region_ids))
        return list(self.session.execute(stmt).all())

    def list_templates_raw(
        self, region_id: UUID, dataset_id: UUID
    ) -> list[ScheduleTemplate]:
        """模板同步用：含停用行（同步时可重新启用），不做 Merchant/User 联表。"""
        return list(
            self.session.scalars(
                select(ScheduleTemplate)
                .where(ScheduleTemplate.region_id == region_id)
                .where(ScheduleTemplate.dataset_id == dataset_id)
            ).all()
        )

    def find_template_by_merchant(
        self, region_id: UUID, merchant_id: UUID
    ) -> ScheduleTemplate | None:
        return self.session.scalars(
            select(ScheduleTemplate)
            .where(ScheduleTemplate.region_id == region_id)
            .where(ScheduleTemplate.merchant_id == merchant_id)
        ).first()

    def add_template(self, template: ScheduleTemplate) -> ScheduleTemplate:
        self.session.add(template)
        self.session.flush()
        return template

    def lock_entries(self, batch_id: UUID) -> list[SchedulePlanEntry]:
        """发布时对批内条目加行锁（FOR UPDATE），串行化同批并发发布。"""
        return list(
            self.session.scalars(
                select(SchedulePlanEntry)
                .where(SchedulePlanEntry.batch_id == batch_id)
                .with_for_update()
            ).all()
        )

    # ---------------- 资源占用 ----------------

    def add_reservation(self, r: ResourceReservation) -> ResourceReservation:
        self.session.add(r)
        self.session.flush()
        return r

    def add_visit_task(self, t: VisitTask) -> VisitTask:
        self.session.add(t)
        self.session.flush()
        return t

    def acquire_region_week_lock(self, region_id: UUID, week_start: date) -> None:
        """Postgres 事务级咨询锁：串行化 (region, 业务周) 的发布。

        key 由 region_id 与 ISO 周序号派生（bigint）。锁在事务提交/回滚时自动释放。
        """
        key = self._advisory_key(region_id, week_start)
        self.session.execute(
            func.pg_advisory_xact_lock(key)
        )

    @staticmethod
    def _advisory_key(region_id: UUID, week_start: date) -> int:
        iso = week_start.isocalendar()
        # region 低 31 位 XOR 年份*100+周序，落在 int64 安全范围
        return (int(region_id.int & 0x7FFFFFFF)) ^ (iso[0] * 100 + iso[1]) * 7919

    # ---------------- 周视图 / 建议数据 ----------------

    def primary_assignees(self, merchant_ids: list[UUID]) -> dict[UUID, UUID]:
        """商家 → 当前主负责人 owner_user_id（PRIMARY 区间包含当前时刻）。"""
        if not merchant_ids:
            return {}
        rows = self.session.execute(
            select(
                MerchantAssignment.merchant_id,
                MerchantAssignment.owner_user_id,
            )
            .where(MerchantAssignment.merchant_id.in_(merchant_ids))
            .where(MerchantAssignment.assignment_type == "PRIMARY")
            .where(MerchantAssignment.valid_range.op("@>")(func.now()))
        ).all()
        return {m: u for m, u in rows}

    def latest_valid_visit_at(
        self, merchant_ids: list[UUID]
    ) -> dict[UUID, datetime]:
        """商家最近一次**有效**巡访的打卡时刻（覆盖率/逾期判定口径）。

        有效只认 VisitValidityService 裁决落库的 RECOGNIZED；督导巡访（source 非
        WEEKLY_SCHEDULE/SELF_PLANNED）不计入覆盖率。
        """
        if not merchant_ids:
            return {}
        stmt = (
            select(
                VisitRecord.merchant_id,
                func.max(VisitRecord.checkin_at),
            )
            .where(VisitRecord.merchant_id.in_(merchant_ids))
            .where(VisitRecord.status == VisitRecordStatus.RECOGNIZED.value)
            .where(VisitRecord.source.in_(["WEEKLY_SCHEDULE", "SELF_PLANNED"]))
            .group_by(VisitRecord.merchant_id)
        )
        return {m: at for m, at in self.session.execute(stmt).all()}

    def merchants_with_valid_visit_in_week(
        self, region_id: UUID, week_start_inclusive: datetime,
        week_end_exclusive: datetime, dataset_id: UUID,
    ) -> set[UUID]:
        """该业务周 [start,end) 内已有有效巡访的商家集合（覆盖缺口用）。"""
        rows = self.session.scalars(
            select(VisitRecord.merchant_id)
            .join(Merchant, Merchant.id == VisitRecord.merchant_id)
            .where(Merchant.region_id == region_id)
            .where(VisitRecord.dataset_id == dataset_id)
            .where(VisitRecord.status == VisitRecordStatus.RECOGNIZED.value)
            .where(VisitRecord.source.in_(["WEEKLY_SCHEDULE", "SELF_PLANNED"]))
            .where(VisitRecord.checkin_at >= week_start_inclusive)
            .where(VisitRecord.checkin_at < week_end_exclusive)
            .distinct()
        ).all()
        return set(rows)

    def merchants_scheduled_in_week(
        self, region_id: UUID, week_start: date, dataset_id: UUID
    ) -> set[UUID]:
        """该业务周已发布排班里的商家集合（避免重复安排 / 覆盖判定）。"""
        rows = self.session.scalars(
            select(SchedulePlanEntry.merchant_id)
            .join(SchedulePlanBatch, SchedulePlanBatch.id == SchedulePlanEntry.batch_id)
            .where(SchedulePlanBatch.region_id == region_id)
            .where(SchedulePlanBatch.dataset_id == dataset_id)
            .where(SchedulePlanBatch.status == "PUBLISHED")
            .where(SchedulePlanBatch.week_start_date == week_start)
            .distinct()
        ).all()
        return set(rows)

    def pending_replenishment_merchants(
        self, merchant_ids: list[UUID]
    ) -> set[UUID]:
        """存在未完成补货（DRAFT/SUBMITTED/CONFIRMED/PARTIALLY_FULFILLED 等）的商家。"""
        if not merchant_ids:
            return set()
        open_statuses = [
            ReplenishmentStatus.DRAFT.value,
            ReplenishmentStatus.SUBMITTED.value,
            ReplenishmentStatus.REQUESTED.value,
            ReplenishmentStatus.CONFIRMED.value,
            ReplenishmentStatus.PARTIALLY_FULFILLED.value,
            ReplenishmentStatus.IN_TRANSIT.value,
            ReplenishmentStatus.OVERDUE.value,
        ]
        rows = self.session.scalars(
            select(ReplenishmentOrder.merchant_id)
            .where(ReplenishmentOrder.merchant_id.in_(merchant_ids))
            .where(ReplenishmentOrder.status.in_(open_statuses))
            .distinct()
        ).all()
        return set(rows)

    def campaign_occupied_user_ids(
        self,
        user_ids: list[UUID],
        week_start_inclusive: datetime,
        week_end_exclusive: datetime,
        dataset_id: UUID,
    ) -> set[UUID]:
        """业务周 [start,end) 内被 ACTIVE 的 CAMPAIGN 占用的业务员集合（T25 活动避让）。

        活动占用唯一来自 ResourceReservation(source_type='CAMPAIGN')；本方法只做
        持久化查询，不重算区间重叠（半开区间相交条件与 ScheduleConflictService 一致）。
        """
        if not user_ids:
            return set()
        rows = self.session.scalars(
            select(ResourceReservation.resource_user_id)
            .where(ResourceReservation.dataset_id == dataset_id)
            .where(ResourceReservation.status == ACTIVE_RESERVATION)
            .where(ResourceReservation.source_type == "CAMPAIGN")
            .where(ResourceReservation.resource_user_id.in_(user_ids))
            .where(ResourceReservation.start_at < week_end_exclusive)
            .where(ResourceReservation.end_at > week_start_inclusive)
            .distinct()
        ).all()
        return set(rows)

    def list_active_merchants(self, region_ids, dataset_id: UUID) -> list[Merchant]:
        stmt = (
            select(Merchant)
            .where(Merchant.dataset_id == dataset_id)
            .where(Merchant.status == "ACTIVE")
        )
        if region_ids is not None:
            stmt = stmt.where(Merchant.region_id.in_(region_ids))
        return list(self.session.scalars(stmt.order_by(Merchant.created_at.asc())).all())

    def merchant_distances(
        self, merchants: list[Merchant], lng: float, lat: float
    ) -> dict[UUID, float]:
        """商家到锚点的球面距离（米），PostGIS ST_DistanceSphere。"""
        if not merchants:
            return {}
        point = cast(
            func.ST_SetSRID(gfunc.ST_MakePoint(literal(lng), literal(lat)), 4326),
            String,
        )
        rows = self.session.execute(
            select(
                Merchant.id,
                func.ST_DistanceSphere(Merchant.location, func.ST_GeomFromText(point, 4326)),
            ).where(Merchant.id.in_([m.id for m in merchants]))
        ).all()
        return {mid: float(dist) for mid, dist in rows}

    def week_view(
        self,
        region_ids,
        week_start: date,
        dataset_id: UUID | None,
        assigned_user_id: UUID | None = None,
    ) -> list[tuple[SchedulePlanEntry, Merchant, User, SchedulePlanBatch]]:
        """周视图：已发布批次的条目 + 商家 + 负责人。dataset_id 为 None 时不按数据集过滤。

        ``assigned_user_id`` 用于业务员只看自己的排班（行级范围由 Service 判定后传入），
        片区经理也可按业务员筛选。
        """
        stmt = (
            select(SchedulePlanEntry, Merchant, User, SchedulePlanBatch)
            .join(SchedulePlanBatch, SchedulePlanBatch.id == SchedulePlanEntry.batch_id)
            .join(Merchant, Merchant.id == SchedulePlanEntry.merchant_id)
            .join(User, User.id == SchedulePlanEntry.assigned_user_id)
            .where(SchedulePlanBatch.week_start_date == week_start)
            .where(SchedulePlanBatch.status == "PUBLISHED")
            .order_by(SchedulePlanEntry.planned_date.asc(),
                      SchedulePlanEntry.start_at.asc().nullsfirst())
        )
        if dataset_id is not None:
            stmt = stmt.where(SchedulePlanEntry.dataset_id == dataset_id)
        if region_ids is not None:
            stmt = stmt.where(SchedulePlanBatch.region_id.in_(region_ids))
        if assigned_user_id is not None:
            stmt = stmt.where(SchedulePlanEntry.assigned_user_id == assigned_user_id)
        return list(self.session.execute(stmt).all())

    def get_entry(self, entry_id: UUID) -> SchedulePlanEntry | None:
        return self.session.get(SchedulePlanEntry, entry_id)

    def get_visit_task(self, task_id: UUID) -> VisitTask | None:
        return self.session.get(VisitTask, task_id)

    def get_reservation(self, reservation_id: UUID) -> ResourceReservation | None:
        return self.session.get(ResourceReservation, reservation_id)

    def active_reservations_for_users(
        self,
        user_ids: set[UUID],
        window_start: datetime,
        window_end: datetime,
        dataset_id: UUID,
    ) -> list[ResourceReservation]:
        """一组业务员在时间窗内的 ACTIVE 占用（排班自动分时段的避让依据）。"""
        if not user_ids:
            return []
        stmt = (
            select(ResourceReservation)
            .where(ResourceReservation.dataset_id == dataset_id)
            .where(ResourceReservation.status == ACTIVE_RESERVATION)
            .where(ResourceReservation.resource_user_id.in_(user_ids))
            .where(ResourceReservation.start_at < window_end)
            .where(ResourceReservation.end_at > window_start)
            .order_by(ResourceReservation.start_at.asc())
        )
        return list(self.session.scalars(stmt).all())

    def list_active_merchants_with_region(
        self, region_ids: set[UUID] | None, dataset_id: UUID | None
    ) -> list[tuple[Merchant, str]]:
        """排班候选商家：可见片区内的启用商家（含片区名）。

        新启用的商家只要 status=ACTIVE 就会被下一次查询带出来，不需要额外配置。
        """
        stmt = (
            select(Merchant, Region.name)
            .join(Region, Region.id == Merchant.region_id)
            .where(Merchant.status == "ACTIVE")
            .order_by(Region.name.asc(), Merchant.name.asc())
        )
        if dataset_id is not None:
            stmt = stmt.where(Merchant.dataset_id == dataset_id)
        if region_ids is not None:
            stmt = stmt.where(Merchant.region_id.in_(region_ids))
        return [(merchant, region_name) for merchant, region_name in self.session.execute(stmt).all()]

    def primary_owner_map(self, merchant_ids: set[UUID]) -> dict[UUID, UUID]:
        """商家当前主责任人（排班草稿的默认业务员）。"""
        if not merchant_ids:
            return {}
        today = date.today()
        stmt = (
            select(MerchantAssignment.merchant_id, MerchantAssignment.owner_user_id)
            .where(MerchantAssignment.merchant_id.in_(merchant_ids))
            .where(MerchantAssignment.assignment_type == "PRIMARY")
            .where(func.lower(MerchantAssignment.valid_range) <= today)
            .where(
                or_(
                    func.upper_inf(MerchantAssignment.valid_range),
                    func.upper(MerchantAssignment.valid_range) > today,
                )
            )
        )
        return {mid: owner for mid, owner in self.session.execute(stmt).all()}

    def scheduled_merchant_ids(
        self,
        region_ids: set[UUID] | None,
        week_start: date,
        dataset_id: UUID | None,
    ) -> set[UUID]:
        """某业务周内已排班（已发布）的商家 id，用于标记“本周待排/已排”。"""
        stmt = (
            select(SchedulePlanEntry.merchant_id)
            .join(SchedulePlanBatch, SchedulePlanBatch.id == SchedulePlanEntry.batch_id)
            .where(SchedulePlanBatch.week_start_date == week_start)
            .where(SchedulePlanBatch.status == "PUBLISHED")
            .distinct()
        )
        if dataset_id is not None:
            stmt = stmt.where(SchedulePlanBatch.dataset_id == dataset_id)
        if region_ids is not None:
            stmt = stmt.where(SchedulePlanBatch.region_id.in_(region_ids))
        return set(self.session.scalars(stmt).all())

    def has_published_week(
        self,
        region_ids: set[UUID] | None,
        week_start: date,
        dataset_id: UUID | None,
    ) -> bool:
        """该业务周（在给定范围内）是否已经发布过排班。

        未发布周才允许展示「模板预排」；已发布周不能拿模板冒充排班，
        否则业务员会看到"当日有排班"却没有任何巡访任务（模板预排不落任务）。
        """
        stmt = (
            select(SchedulePlanBatch.id)
            .where(SchedulePlanBatch.week_start_date == week_start)
            .where(SchedulePlanBatch.status == "PUBLISHED")
            .limit(1)
        )
        if dataset_id is not None:
            stmt = stmt.where(SchedulePlanBatch.dataset_id == dataset_id)
        if region_ids is not None:
            stmt = stmt.where(SchedulePlanBatch.region_id.in_(region_ids))
        return self.session.scalars(stmt).first() is not None

    def entries_for_merchant_after(
        self,
        merchant_id: UUID,
        assigned_user_id: UUID,
        from_date: date,
    ) -> list[SchedulePlanEntry]:
        """某商家在 from_date（含）之后仍排给指定业务员的已发布条目。"""
        return list(
            self.session.scalars(
                select(SchedulePlanEntry)
                .join(
                    SchedulePlanBatch,
                    SchedulePlanBatch.id == SchedulePlanEntry.batch_id,
                )
                .where(SchedulePlanEntry.merchant_id == merchant_id)
                .where(SchedulePlanEntry.assigned_user_id == assigned_user_id)
                .where(SchedulePlanEntry.planned_date >= from_date)
                .where(SchedulePlanBatch.status == "PUBLISHED")
                .order_by(
                    SchedulePlanEntry.planned_date.asc(),
                    SchedulePlanEntry.start_at.asc().nullsfirst(),
                )
            ).all()
        )

    def reassign_templates(
        self, merchant_id: UUID, from_user_id: UUID, to_user_id: UUID
    ) -> int:
        """每周循环模板同步改派：避免下周又排回原负责人。"""
        templates = self.session.scalars(
            select(ScheduleTemplate)
            .where(ScheduleTemplate.merchant_id == merchant_id)
            .where(ScheduleTemplate.assigned_user_id == from_user_id)
        ).all()
        for template in templates:
            template.assigned_user_id = to_user_id
        return len(templates)

    def list_sales_reps(
        self, dataset_id: UUID | None, region_ids: set[UUID] | None = None
    ) -> list[tuple[User, str | None]]:
        """排班可选的业务员：在职 SALES_REP + 其已授权片区名。

        ``region_ids`` 为经理可见片区（系统管理员传 None 表示不限）：
        经理可以给“自己管得到的任何片区”的业务员排班，而不是只看当前选中的片区。
        """
        stmt = (
            select(User, Region.name)
            .join(Role, Role.id == User.role_id)
            .outerjoin(
                UserRegionBrandAuth, UserRegionBrandAuth.user_id == User.id
            )
            .outerjoin(Region, Region.id == UserRegionBrandAuth.region_id)
            .where(User.status == "ACTIVE")
            .where(Role.role_code == "SALES_REP")
            .order_by(User.display_name.asc())
        )
        if dataset_id is not None:
            stmt = stmt.where(User.dataset_id == dataset_id)
        if region_ids is not None:
            stmt = stmt.where(
                or_(
                    UserRegionBrandAuth.region_id.in_(region_ids),
                    UserRegionBrandAuth.id.is_(None),
                )
            )
        return [(user, region_name) for user, region_name in self.session.execute(stmt).all()]

    def latest_published_entries(
        self, region_id: UUID, dataset_id: UUID, week_start: date | None = None
    ) -> list[SchedulePlanEntry]:
        """每个商家最新一条已发布排班条目（按业务周、计划日期倒序取首条）。

        ``week_start`` 指定时只取该业务周（「用本周排班覆盖模板」的精确语义），
        否则取历史上最新一周——注意"最新"可能是未来周，容易与用户当前查看的
        这周不一致。
        """
        stmt = (
            select(SchedulePlanEntry)
            .join(SchedulePlanBatch, SchedulePlanBatch.id == SchedulePlanEntry.batch_id)
            .where(SchedulePlanBatch.region_id == region_id)
            .where(SchedulePlanBatch.dataset_id == dataset_id)
            .where(SchedulePlanBatch.status == "PUBLISHED")
            .order_by(
                SchedulePlanBatch.week_start_date.desc(),
                SchedulePlanEntry.planned_date.desc(),
                SchedulePlanEntry.created_at.desc(),
            )
        )
        if week_start is not None:
            stmt = stmt.where(SchedulePlanBatch.week_start_date == week_start)
        latest: dict[UUID, SchedulePlanEntry] = {}
        for entry in self.session.scalars(stmt).all():
            latest.setdefault(entry.merchant_id, entry)
        return list(latest.values())

    def list_templates_in_regions(
        self, region_ids, dataset_id: UUID
    ) -> list[tuple[ScheduleTemplate, Merchant, User]]:
        """按可见片区取每周模板（周视图在未发布周做“模板预排”）。"""
        stmt = (
            select(ScheduleTemplate, Merchant, User)
            .join(Merchant, Merchant.id == ScheduleTemplate.merchant_id)
            .join(User, User.id == ScheduleTemplate.assigned_user_id)
            .where(ScheduleTemplate.dataset_id == dataset_id)
            .where(ScheduleTemplate.is_active.is_(True))
            .order_by(
                ScheduleTemplate.weekday.asc(),
                ScheduleTemplate.start_time.asc(),
                Merchant.name.asc(),
            )
        )
        if region_ids is not None:
            stmt = stmt.where(ScheduleTemplate.region_id.in_(region_ids))
        rows = self.session.execute(stmt).all()
        return [(tpl, merchant, user) for tpl, merchant, user in rows]

    def region_managers(
        self, region_id: UUID, dataset_id: UUID, brand_id: UUID | None = None
    ) -> list[User]:
        stmt = (
            select(User)
            .join(Role, Role.id == User.role_id)
            .join(UserRegionBrandAuth, UserRegionBrandAuth.user_id == User.id)
            .where(Role.role_code == "REGIONAL_MANAGER")
            .where(User.status == "ACTIVE")
            .where(User.dataset_id == dataset_id)
            .where(UserRegionBrandAuth.region_id == region_id)
        )
        if brand_id is not None:
            stmt = stmt.where(UserRegionBrandAuth.brand_id == brand_id)
        return list(self.session.scalars(stmt.order_by(User.created_at.asc())).all())
