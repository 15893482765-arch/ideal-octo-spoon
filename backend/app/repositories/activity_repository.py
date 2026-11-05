"""地推活动持久化（Repository 只做持久化，不含业务裁决）。"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    Activity,
    ActivityParticipant,
    ActivitySampleLedger,
    Region,
    ResourceReservation,
    Sku,
    User,
    UserRegionBrandAuth,
)

SOURCE_TYPE_CAMPAIGN = "CAMPAIGN"
SOURCE_OBJECT_ACTIVITY = "CAMPAIGN"
ACTIVE_RESERVATION = "ACTIVE"
RELEASED_RESERVATION = "RELEASED"

# 样品台账阶段（对齐 activity_samples_stage_check）
STAGE_TAKEN_OUT = "TAKEN_OUT"
STAGE_CONSUMED = "CONSUMED"
STAGE_RETURNED = "RETURNED"
STAGE_MISSING = "MISSING"


class ActivityRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    # ---------------- 活动 / 参与人 ----------------

    def get_activity(self, activity_id: UUID) -> Activity | None:
        return self.session.get(Activity, activity_id)

    def add_activity(self, activity: Activity) -> Activity:
        self.session.add(activity)
        self.session.flush()
        return activity

    def add_participant(self, p: ActivityParticipant) -> ActivityParticipant:
        self.session.add(p)
        self.session.flush()
        return p

    def list_participants(self, activity_id: UUID) -> list[ActivityParticipant]:
        return list(
            self.session.scalars(
                select(ActivityParticipant)
                .where(ActivityParticipant.activity_id == activity_id)
                .order_by(ActivityParticipant.created_at.asc())
            ).all()
        )

    def participant_user_ids(self, activity_id: UUID) -> set[UUID]:
        rows = self.session.scalars(
            select(ActivityParticipant.user_id)
            .where(ActivityParticipant.activity_id == activity_id)
        ).all()
        return set(rows)

    # ---------------- 列表 / 下拉选项 ----------------

    def list_activities(
        self,
        *,
        region_ids: set[UUID] | None = None,
        status: str | None = None,
        keyword: str | None = None,
        start_from: datetime | None = None,
        start_to: datetime | None = None,
        offset: int = 0,
        limit: int = 20,
    ) -> tuple[list[tuple[Activity, Region, User | None]], int]:
        """活动列表（含片区名与负责人）。

        - `region_ids=None` 表示不限制片区；空集合表示任何片区都不允许（返回空）；
        - 时间筛选按活动开始时间（`start_at`）落在给定 UTC 区间内；
        - 关键字只匹配活动名称（模糊），不匹配地点/备注，避免越权探测。
        """
        conditions = []
        if region_ids is not None:
            conditions.append(Activity.region_id.in_(region_ids))
        if status is not None:
            conditions.append(Activity.status == status)
        if keyword:
            conditions.append(Activity.name.ilike(f"%{keyword}%"))
        if start_from is not None:
            conditions.append(Activity.start_at >= start_from)
        if start_to is not None:
            conditions.append(Activity.start_at <= start_to)

        stmt = (
            select(Activity, Region, User)
            .join(Region, Region.id == Activity.region_id)
            # 负责人可能已停用：用外连接，避免整行从列表消失
            .outerjoin(User, User.id == Activity.owner_user_id)
            .where(*conditions)
            .order_by(Activity.start_at.desc(), Activity.created_at.desc())
            .offset(max(offset, 0))
            .limit(limit)
        )
        rows = [tuple(r) for r in self.session.execute(stmt).all()]

        count_stmt = (
            select(func.count())
            .select_from(Activity)
            .join(Region, Region.id == Activity.region_id)
            .where(*conditions)
        )
        total = self.session.scalar(count_stmt) or 0
        return rows, int(total)

    def participant_counts(self, activity_ids) -> dict[UUID, int]:
        ids = set(activity_ids)
        if not ids:
            return {}
        rows = self.session.execute(
            select(ActivityParticipant.activity_id, func.count())
            .where(ActivityParticipant.activity_id.in_(ids))
            .group_by(ActivityParticipant.activity_id)
        ).all()
        return {aid: int(count) for aid, count in rows}

    def active_reservation_counts(self, activity_ids) -> dict[UUID, int]:
        """本活动当前 ACTIVE 的人员占用条数（取消后未来时段释放，计数随之下降）。"""
        ids = set(activity_ids)
        if not ids:
            return {}
        rows = self.session.execute(
            select(ResourceReservation.source_object_id, func.count())
            .where(ResourceReservation.source_object_type == SOURCE_OBJECT_ACTIVITY)
            .where(ResourceReservation.source_object_id.in_(ids))
            .where(ResourceReservation.status == ACTIVE_RESERVATION)
            .group_by(ResourceReservation.source_object_id)
        ).all()
        return {aid: int(count) for aid, count in rows}

    def list_region_users(
        self, dataset_id: UUID, region_id: UUID, *, all_in_dataset: bool = False
    ) -> list[User]:
        """可选活动人员：同数据范围内、已授权该片区的在职用户。

        `all_in_dataset=True`（系统管理员）时不再按片区授权过滤。
        """
        stmt = (
            select(User)
            .where(User.status == "ACTIVE")
            .where(User.dataset_id == dataset_id)
        )
        if not all_in_dataset:
            auth_ids = select(UserRegionBrandAuth.user_id).where(
                UserRegionBrandAuth.region_id == region_id
            )
            stmt = stmt.where(User.id.in_(auth_ids))
        return list(
            self.session.scalars(stmt.order_by(User.display_name.asc())).all()
        )

    def list_dataset_skus(self, dataset_id: UUID) -> list[Sku]:
        """可选样品 SKU（主数据范围一致；未导入主数据时为空）。"""
        return list(
            self.session.scalars(
                select(Sku)
                .where(Sku.dataset_id == dataset_id)
                .order_by(Sku.code.asc())
            ).all()
        )

    def get_active_users(self, user_ids, dataset_id: UUID) -> list[User]:
        if not user_ids:
            return []
        return list(
            self.session.scalars(
                select(User)
                .where(User.id.in_(set(user_ids)))
                .where(User.status == "ACTIVE")
                .where(User.dataset_id == dataset_id)
            ).all()
        )

    # ---------------- 资源占用 ----------------

    def add_reservation(self, r: ResourceReservation) -> ResourceReservation:
        self.session.add(r)
        self.session.flush()
        return r

    def lock_campaign_reservations(self, activity_id: UUID) -> list[ResourceReservation]:
        """发布后本活动生成的 ACTIVE 占用，加行锁（取消时串行化）。"""
        return list(
            self.session.scalars(
                select(ResourceReservation)
                .where(ResourceReservation.source_object_type == SOURCE_OBJECT_ACTIVITY)
                .where(ResourceReservation.source_object_id == activity_id)
                .where(ResourceReservation.status == ACTIVE_RESERVATION)
                .with_for_update()
            ).all()
        )

    def active_campaign_reservations(self, activity_id: UUID) -> list[ResourceReservation]:
        return list(
            self.session.scalars(
                select(ResourceReservation)
                .where(ResourceReservation.source_object_type == SOURCE_OBJECT_ACTIVITY)
                .where(ResourceReservation.source_object_id == activity_id)
                .where(ResourceReservation.status == ACTIVE_RESERVATION)
                .order_by(ResourceReservation.start_at.asc())
            ).all()
        )

    # ---------------- 样品台账 ----------------

    def add_sample_ledger(self, row: ActivitySampleLedger) -> ActivitySampleLedger:
        self.session.add(row)
        self.session.flush()
        return row

    def list_sample_ledger(self, activity_id: UUID) -> list[ActivitySampleLedger]:
        return list(
            self.session.scalars(
                select(ActivitySampleLedger)
                .where(ActivitySampleLedger.activity_id == activity_id)
                .order_by(ActivitySampleLedger.created_at.asc())
            ).all()
        )

    def sku_price_map(self, sku_ids) -> dict[UUID, int]:
        """SKU → 参考进价（分/瓶）；缺价按 0（样品成本口径：有进价才计成本）。"""
        ids = set(sku_ids)
        if not ids:
            return {}
        rows = self.session.execute(
            select(Sku.id, Sku.purchase_price_fen).where(Sku.id.in_(ids))
        ).all()
        return {sid: (price or 0) for sid, price in rows}

    def sku_case_cost_map(self, sku_ids) -> dict[UUID, tuple[int, Decimal]]:
        """SKU → (参考进价 分/瓶, 箱规 瓶/箱)；缺价按 0、箱规未维护按 1。

        数量按箱、价格按瓶，样品成本必须乘箱规才能与采购/销售同口径。
        """
        ids = set(sku_ids)
        if not ids:
            return {}
        rows = self.session.execute(
            select(Sku.id, Sku.purchase_price_fen, Sku.case_spec).where(Sku.id.in_(ids))
        ).all()
        out: dict[UUID, tuple[int, Decimal]] = {}
        for sid, price, case_spec in rows:
            bottles = case_spec if case_spec and case_spec > 0 else Decimal("1")
            out[sid] = (price or 0, Decimal(bottles))
        return out
