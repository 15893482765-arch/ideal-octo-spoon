"""督导跟访/整改持久化（Repository 只做持久化，不含业务裁决）。"""

from __future__ import annotations

from datetime import date
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session, aliased

from app.models import (
    Merchant,
    Rectification,
    Region,
    Role,
    SupervisionVisit,
    User,
    UserRegionBrandAuth,
)
from app.models.enums import RoleCode


class SupervisionRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    # ---------------- 跟访 ----------------

    def get_visit(self, visit_id: UUID) -> SupervisionVisit | None:
        return self.session.get(SupervisionVisit, visit_id)

    def add_visit(self, visit: SupervisionVisit) -> SupervisionVisit:
        self.session.add(visit)
        self.session.flush()
        return visit

    def list_visits_in_week(
        self,
        *,
        region_id: UUID,
        week_start: date,
        week_end_exclusive: date,
        supervisor_id: UUID | None = None,
    ) -> list[SupervisionVisit]:
        stmt = (
            select(SupervisionVisit)
            .where(SupervisionVisit.region_id == region_id)
            .where(SupervisionVisit.planned_date >= week_start)
            .where(SupervisionVisit.planned_date < week_end_exclusive)
        )
        if supervisor_id is not None:
            stmt = stmt.where(SupervisionVisit.supervisor_id == supervisor_id)
        stmt = stmt.order_by(SupervisionVisit.planned_date.asc())
        return list(self.session.scalars(stmt).all())

    def find_planned_visits_for_merchant(
        self,
        merchant_id: UUID,
        planned_date: date,
        *,
        supervisor_id: UUID | None = None,
    ) -> list[SupervisionVisit]:
        """按商家+计划日查 PLANNED 跟访（T24 MCP 受控定位）。"""
        stmt = (
            select(SupervisionVisit)
            .where(SupervisionVisit.merchant_id == merchant_id)
            .where(SupervisionVisit.planned_date == planned_date)
            .where(SupervisionVisit.status == "PLANNED")
            .order_by(SupervisionVisit.created_at.desc())
        )
        if supervisor_id is not None:
            stmt = stmt.where(SupervisionVisit.supervisor_id == supervisor_id)
        return list(self.session.scalars(stmt).all())

    # ---------------- 整改 ----------------

    def get_rectification(self, rect_id: UUID) -> Rectification | None:
        return self.session.get(Rectification, rect_id)

    def add_rectification(self, r: Rectification) -> Rectification:
        self.session.add(r)
        self.session.flush()
        return r

    def list_rectifications_for_visit(
        self, visit_id: UUID
    ) -> list[Rectification]:
        return list(
            self.session.scalars(
                select(Rectification)
                .where(Rectification.supervision_visit_id == visit_id)
                .order_by(Rectification.created_at.asc())
            ).all()
        )

    def lock_rectification(self, rect_id: UUID) -> Rectification | None:
        return self.session.scalars(
            select(Rectification)
            .where(Rectification.id == rect_id)
            .with_for_update()
        ).first()

    # ---------------- 列表 / 下拉选项 ----------------

    def list_visits(
        self,
        *,
        region_ids: set[UUID] | None = None,
        status: str | None = None,
        supervisor_id: UUID | None = None,
        shadowed_user_id: UUID | None = None,
        planned_from: date | None = None,
        planned_to: date | None = None,
        offset: int = 0,
        limit: int = 20,
    ) -> tuple[list[tuple[SupervisionVisit, Region, User | None, User | None, Merchant]], int]:
        """跟访列表（含片区/督导/被跟访人/商家名称，一次 join 取齐）。

        `region_ids=None` 表示不限制片区；空集合表示任何片区都不允许（返回空）。
        督导与被跟访业务员是两次 User join，必须用别名区分。
        """
        supervisor = aliased(User)
        shadowed = aliased(User)

        conditions = []
        if region_ids is not None:
            conditions.append(SupervisionVisit.region_id.in_(region_ids))
        if status is not None:
            conditions.append(SupervisionVisit.status == status)
        if supervisor_id is not None:
            conditions.append(SupervisionVisit.supervisor_id == supervisor_id)
        if shadowed_user_id is not None:
            conditions.append(SupervisionVisit.shadowed_user_id == shadowed_user_id)
        if planned_from is not None:
            conditions.append(SupervisionVisit.planned_date >= planned_from)
        if planned_to is not None:
            conditions.append(SupervisionVisit.planned_date <= planned_to)

        stmt = (
            select(SupervisionVisit, Region, supervisor, shadowed, Merchant)
            .join(Region, Region.id == SupervisionVisit.region_id)
            # 人员可能已停用：外连接，避免整行从列表消失
            .outerjoin(supervisor, supervisor.id == SupervisionVisit.supervisor_id)
            .outerjoin(shadowed, shadowed.id == SupervisionVisit.shadowed_user_id)
            .join(Merchant, Merchant.id == SupervisionVisit.merchant_id)
            .where(*conditions)
            .order_by(
                SupervisionVisit.planned_date.desc(),
                SupervisionVisit.created_at.desc(),
            )
            .offset(max(offset, 0))
            .limit(limit)
        )
        rows = [tuple(r) for r in self.session.execute(stmt).all()]

        count_stmt = (
            select(func.count())
            .select_from(SupervisionVisit)
            .join(Region, Region.id == SupervisionVisit.region_id)
            .join(Merchant, Merchant.id == SupervisionVisit.merchant_id)
            .where(*conditions)
        )
        total = self.session.scalar(count_stmt) or 0
        return rows, int(total)

    def rectification_status_counts(
        self, visit_ids
    ) -> dict[UUID, tuple[int, int]]:
        """每个跟访的整改单数 (总数, 未闭环数)；未闭环 = OPEN/IN_PROGRESS/SUBMITTED。"""
        ids = set(visit_ids)
        if not ids:
            return {}
        rows = self.session.execute(
            select(
                Rectification.supervision_visit_id,
                Rectification.status,
                func.count(),
            )
            .where(Rectification.supervision_visit_id.in_(ids))
            .group_by(Rectification.supervision_visit_id, Rectification.status)
        ).all()
        agg: dict[UUID, tuple[int, int]] = {}
        pending_states = {"OPEN", "IN_PROGRESS", "SUBMITTED"}
        for visit_id, status, count in rows:
            total, pending = agg.get(visit_id, (0, 0))
            total += int(count)
            if status in pending_states:
                pending += int(count)
            agg[visit_id] = (total, pending)
        return agg

    def list_rectifications(
        self,
        *,
        region_ids: set[UUID] | None = None,
        status: str | None = None,
        owner_user_id: UUID | None = None,
        offset: int = 0,
        limit: int = 20,
    ) -> tuple[list[tuple[Rectification, Region, Merchant, User | None]], int]:
        """整改列表（含片区/商家/责任人名称）。"""
        owner = aliased(User)

        conditions = []
        if region_ids is not None:
            conditions.append(Rectification.region_id.in_(region_ids))
        if status is not None:
            conditions.append(Rectification.status == status)
        if owner_user_id is not None:
            conditions.append(Rectification.owner_user_id == owner_user_id)

        stmt = (
            select(Rectification, Region, Merchant, owner)
            .join(Region, Region.id == Rectification.region_id)
            .join(Merchant, Merchant.id == Rectification.merchant_id)
            .outerjoin(owner, owner.id == Rectification.owner_user_id)
            .where(*conditions)
            .order_by(Rectification.due_date.asc(), Rectification.created_at.desc())
            .offset(max(offset, 0))
            .limit(limit)
        )
        rows = [tuple(r) for r in self.session.execute(stmt).all()]

        count_stmt = (
            select(func.count())
            .select_from(Rectification)
            .join(Region, Region.id == Rectification.region_id)
            .join(Merchant, Merchant.id == Rectification.merchant_id)
            .where(*conditions)
        )
        total = self.session.scalar(count_stmt) or 0
        return rows, int(total)

    def list_region_users_by_role(
        self, dataset_id: UUID, region_id: UUID, role_code: str
    ) -> list[User]:
        """某片区内指定角色、已授权的在职用户（计划表单的人员下拉）。"""
        auth_ids = select(UserRegionBrandAuth.user_id).where(
            UserRegionBrandAuth.region_id == region_id
        )
        return list(
            self.session.scalars(
                select(User)
                .join(Role, Role.id == User.role_id)
                .where(User.status == "ACTIVE")
                .where(User.dataset_id == dataset_id)
                .where(Role.role_code == role_code)
                .where(User.id.in_(auth_ids))
                .order_by(User.display_name.asc())
            ).all()
        )

    # ---------------- 校验辅助 ----------------

    def get_region(self, region_id: UUID) -> Region | None:
        return self.session.get(Region, region_id)

    def get_merchant(self, merchant_id: UUID) -> Merchant | None:
        return self.session.get(Merchant, merchant_id)

    def get_active_user(self, user_id: UUID, dataset_id: UUID) -> User | None:
        return self.session.scalars(
            select(User)
            .where(User.id == user_id)
            .where(User.status == "ACTIVE")
            .where(User.dataset_id == dataset_id)
        ).first()

    def count_reps_in_region(self, region_id: UUID, dataset_id: UUID) -> int:
        """片区内在职业务员数量（业务员的片区归属来自 UserRegionBrandAuth）。"""
        stmt = (
            select(func.count(func.distinct(User.id)))
            .join(Role, Role.id == User.role_id)
            .join(UserRegionBrandAuth, UserRegionBrandAuth.user_id == User.id)
            .where(User.status == "ACTIVE")
            .where(User.dataset_id == dataset_id)
            .where(Role.role_code == RoleCode.SALES_REP.value)
            .where(UserRegionBrandAuth.region_id == region_id)
        )
        return int(self.session.scalar(stmt) or 0)
