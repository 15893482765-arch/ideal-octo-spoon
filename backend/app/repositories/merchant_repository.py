"""商家中心持久化层（PRD §4.3）。

只负责查询/落库，不做鉴权与业务裁决；行级范围由 Service 传入已裁剪好的
region_ids / 本人商家集合，Repository 不自行推断 actor。
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from geoalchemy2.types import Geometry
from sqlalchemy import ColumnElement, cast, func, select
from sqlalchemy.orm import Session

from app.models import (
    AuditLog,
    Contract,
    FreezerAsset,
    Merchant,
    MerchantAssignment,
    MerchantGradeChange,
    ReplenishmentOrder,
    SupervisorIssue,
    VisitRecord,
)

# location 是 geography(Point,4326)，ST_X/ST_Y 需先转 geometry
_LOC_GEOM = cast(Merchant.location, Geometry(geometry_type="POINT", srid=4326))
# ST_Y=纬度, ST_X=经度；无点时为 NULL
_LAT = func.ST_Y(_LOC_GEOM).label("lat")
_LNG = func.ST_X(_LOC_GEOM).label("lng")

# 全片区可见角色：系统/片区管理员、督导、财务（财务仅聚合但商家中心可查档）
WIDE_SCOPE_ROLES = {
    "SYSTEM_ADMIN",
    "REGION_ADMIN",
    "SUPERVISOR",
    "FINANCE",
    "MANUFACTURER",
}


class MerchantRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    # ----- 商家主档 -----
    def get_by_id(self, merchant_id: UUID) -> Merchant | None:
        return self.session.get(Merchant, merchant_id)

    def get_by_code(self, code: str) -> Merchant | None:
        stmt = select(Merchant).where(Merchant.code == code)
        return self.session.scalars(stmt).first()

    def add(self, merchant: Merchant) -> Merchant:
        self.session.add(merchant)
        self.session.flush()
        return merchant

    def search(
        self,
        *,
        region_ids: list[UUID] | None = None,
        merchant_ids: list[UUID] | None = None,
        status: str | None = None,
        grade: str | None = None,
        keyword: str | None = None,
        order: str | None = None,
        region_id: UUID | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[Merchant], int]:
        conditions: list[ColumnElement[bool]] = []
        if region_ids is not None:
            conditions.append(Merchant.region_id.in_(region_ids))
        if merchant_ids is not None:
            if not merchant_ids:
                return [], 0
            conditions.append(Merchant.id.in_(merchant_ids))
        if status:
            conditions.append(Merchant.status == status.upper())
        if grade:
            conditions.append(Merchant.grade == grade.upper())
        if region_id:
            conditions.append(Merchant.region_id == region_id)
        if keyword:
            like = f"%{keyword.strip()}%"
            conditions.append(
                (Merchant.name.ilike(like)) | (Merchant.code.ilike(like))
            )

        base = select(Merchant)
        for cond in conditions:
            base = base.where(cond)

        count_stmt = select(func.count()).select_from(base.subquery())
        total = self.session.scalar(count_stmt) or 0

        order_col = Merchant.created_at.desc()
        if order == "name":
            order_col = Merchant.name.asc()
        elif order == "grade":
            # A/B/C 升序，NULL 最后
            order_col = Merchant.grade.asc().nulls_last()

        page_stmt = (
            base.add_columns(_LAT, _LNG)
            .order_by(order_col, Merchant.code.asc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        rows = self.session.execute(page_stmt).all()
        return [(r[0], r[1], r[2]) for r in rows], total

    def coords_of(self, merchant_id: UUID) -> tuple[float | None, float | None]:
        row = self.session.execute(
            select(_LAT, _LNG).where(Merchant.id == merchant_id)
        ).first()
        if row is None:
            return None, None
        return row[0], row[1]

    # ----- 同片区近似候选（重复提示用） -----
    def same_region_candidates(self, region_id: UUID, exclude_id: UUID | None = None):
        stmt = select(Merchant).where(Merchant.region_id == region_id)
        if exclude_id is not None:
            stmt = stmt.where(Merchant.id != exclude_id)
        return list(self.session.scalars(stmt).all())

    # ----- 责任归属区间 -----
    def add_assignment(self, assignment: MerchantAssignment) -> MerchantAssignment:
        self.session.add(assignment)
        self.session.flush()
        return assignment

    def assignments_for_merchant(self, merchant_id: UUID):
        stmt = (
            select(MerchantAssignment)
            .where(MerchantAssignment.merchant_id == merchant_id)
            .order_by(MerchantAssignment.valid_range.desc())
        )
        return list(self.session.scalars(stmt).all())

    def overlapping_primary(
        self,
        merchant_id: UUID,
        lower: datetime,
        upper: datetime | None,
        exclude_id: UUID | None = None,
    ) -> list[MerchantAssignment]:
        """与 [lower, upper) 时间相交的主责任归属（同一商家）。"""
        rng = func.tstzrange(
            func.to_timestamp(lower.timestamp()),
            func.to_timestamp(upper.timestamp()) if upper else None,
            "[)",
        )
        stmt = select(MerchantAssignment).where(
            MerchantAssignment.merchant_id == merchant_id,
            MerchantAssignment.assignment_type == "PRIMARY",
            MerchantAssignment.valid_range.op("&&")(rng),
        )
        if exclude_id is not None:
            stmt = stmt.where(MerchantAssignment.id != exclude_id)
        return list(self.session.scalars(stmt).all())

    def active_assignments(
        self, merchant_ids: list[UUID], at: datetime
    ) -> list[MerchantAssignment]:
        """给定商家集合、在某时刻生效的全部归属（主+临时）。"""
        if not merchant_ids:
            return []
        point = func.tstzrange(
            func.to_timestamp(at.timestamp()),
            func.to_timestamp(at.timestamp()),
            "[]",
        )
        stmt = select(MerchantAssignment).where(
            MerchantAssignment.merchant_id.in_(merchant_ids),
            MerchantAssignment.valid_range.op("@>")(
                func.lower(point)
            ),
        )
        return list(self.session.scalars(stmt).all())

    def merchant_ids_for_owner(self, user_id: UUID, at: datetime) -> list[UUID]:
        """某用户在 at 时刻承担主责任或临时授权的商家 id。"""
        point = func.tstzrange(
            func.to_timestamp(at.timestamp()),
            func.to_timestamp(at.timestamp()),
            "[]",
        )
        stmt = select(MerchantAssignment.merchant_id).where(
            MerchantAssignment.owner_user_id == user_id,
            MerchantAssignment.valid_range.op("@>")(func.lower(point)),
        )
        return list(self.session.scalars(stmt).all())

    # ----- 等级变更 -----
    def add_grade_change(self, change: MerchantGradeChange) -> MerchantGradeChange:
        self.session.add(change)
        self.session.flush()
        return change

    def grade_changes_for_merchant(
        self, merchant_id: UUID, limit: int = 20
    ) -> list[MerchantGradeChange]:
        stmt = (
            select(MerchantGradeChange)
            .where(MerchantGradeChange.merchant_id == merchant_id)
            .order_by(
                MerchantGradeChange.effective_from.desc(),
                MerchantGradeChange.created_at.desc(),
            )
            .limit(limit)
        )
        return list(self.session.scalars(stmt).all())

    def grade_on(self, merchant_id: UUID, on_date) -> str | None:
        """商家在 on_date 当日适用的等级（生效日 <= on_date 的最近一次）。

        供后续排班/预警/绩效按历史口径取等级（PRD §4.6 不回溯）。
        """
        stmt = (
            select(MerchantGradeChange.new_grade)
            .where(
                MerchantGradeChange.merchant_id == merchant_id,
                MerchantGradeChange.effective_from <= on_date,
            )
            .order_by(
                MerchantGradeChange.effective_from.desc(),
                MerchantGradeChange.created_at.desc(),
            )
            .limit(1)
        )
        return self.session.scalars(stmt).first()

    # ----- 详情聚合（只读计数/摘要） -----
    def aggregate_detail(self, merchant_id: UUID) -> dict:
        def _count(model) -> int:
            return self.session.scalar(
                select(func.count())
                .select_from(model)
                .where(model.merchant_id == merchant_id)
            ) or 0

        # 多列查询必须用 execute() 取 Row；scalars() 只回第一列，
        # 之后 c[0]/c[1] 会退化成字符串首字符（「H」「T」）——详情聚合全错的根因
        contracts = list(
            self.session.execute(
                select(Contract.contract_no, Contract.status)
                .where(Contract.merchant_id == merchant_id)
                .order_by(Contract.created_at.desc())
            ).all()
        )
        freezers = list(
            self.session.execute(
                select(FreezerAsset.asset_code, FreezerAsset.status).where(
                    FreezerAsset.current_merchant_id == merchant_id
                )
            ).all()
        )
        replenishments = list(
            self.session.execute(
                select(ReplenishmentOrder.code, ReplenishmentOrder.status)
                .where(ReplenishmentOrder.merchant_id == merchant_id)
                .order_by(ReplenishmentOrder.request_date.desc())
                .limit(20)
            ).all()
        )
        issues = list(
            self.session.execute(
                select(SupervisorIssue.id, SupervisorIssue.issue_type, SupervisorIssue.status)
                .where(SupervisorIssue.merchant_id == merchant_id)
                .order_by(SupervisorIssue.due_date.desc())
                .limit(20)
            ).all()
        )
        visit_count = self.session.scalar(
            select(func.count())
            .select_from(VisitRecord)
            .where(VisitRecord.merchant_id == merchant_id)
        ) or 0
        audits = list(
            self.session.scalars(
                select(AuditLog)
                .where(
                    AuditLog.object_type == "MERCHANT",
                    AuditLog.object_id == merchant_id,
                )
                .order_by(AuditLog.occurred_at.desc())
                .limit(20)
            ).all()
        )
        return {
            "contracts": [{"contract_no": c[0], "status": c[1]} for c in contracts],
            "freezers": [{"asset_code": f[0], "status": f[1]} for f in freezers],
            "replenishments": [
                {"code": r[0], "status": r[1]} for r in replenishments
            ],
            "issues": [
                {"id": str(i[0]), "issue_type": i[1], "status": i[2]}
                for i in issues
            ],
            "activities": [],  # PRD §4.8 活动参与：T17 表落地后聚合
            "visit_count": visit_count,
            "audits": audits,
        }
