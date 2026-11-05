"""T14 商家调拨持久化（Repository 仅持久化，不写业务规则）。"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    Contract,
    Merchant,
    MerchantAssignment,
    MerchantTransfer,
    Role,
    TaskItem,
    User,
    UserRegionBrandAuth,
    VisitTask,
)
from app.models.merchant_transfer import (
    TRANSFER_OPEN_STATUSES,
)

# 归属区间类型（与 merchant_assignments.assignment_type CHECK 对齐）
ASSIGNMENT_PRIMARY = "PRIMARY"
ASSIGNMENT_TEMPORARY = "TEMPORARY"
# 巡访任务“未完成”状态（PRD §4.6）：这些在调拨生效时转交新负责人
_OPEN_VISIT_STATUSES = ("PENDING", "IN_PROGRESS", "OVERDUE", "ABNORMAL")
# 对外公开：调拨与排班改派共用同一套“未完成”口径，避免两处状态集漂移
OPEN_VISIT_STATUSES = _OPEN_VISIT_STATUSES


def _tstz_point(at: datetime):
    """把 Python aware datetime 包成 timestamptz 标量（tstzrange @> timestamptz）。"""
    return func.lower(func.tstzrange(
        func.to_timestamp(at.timestamp()),
        func.to_timestamp(at.timestamp()), "[]"))


class TransferRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get_merchant(self, merchant_id: UUID) -> Merchant | None:
        return self.session.get(Merchant, merchant_id)

    def get_user(self, user_id: UUID) -> User | None:
        return self.session.get(User, user_id)

    def get_transfer(self, transfer_id: UUID) -> MerchantTransfer | None:
        return self.session.get(MerchantTransfer, transfer_id)

    def find_open_transfer(self, merchant_id: UUID, dataset_id: UUID) -> MerchantTransfer | None:
        """该商家当前是否存在未完成（PENDING/SCHEDULED）调拨单。"""
        return self.session.scalars(
            select(MerchantTransfer)
            .where(
                MerchantTransfer.merchant_id == merchant_id,
                MerchantTransfer.dataset_id == dataset_id,
                MerchantTransfer.status.in_(tuple(TRANSFER_OPEN_STATUSES)),
            )
            .order_by(MerchantTransfer.created_at.desc())
        ).first()

    def list_transfer_candidates(
        self, dataset_id: UUID, region_id: UUID
    ) -> list[User]:
        """调拨可选的新负责人：本片区已授权的在职业务员（供表单下拉）。"""
        auth_ids = select(UserRegionBrandAuth.user_id).where(
            UserRegionBrandAuth.region_id == region_id
        )
        return list(
            self.session.scalars(
                select(User)
                .join(Role, Role.id == User.role_id)
                .where(User.status == "ACTIVE")
                .where(User.dataset_id == dataset_id)
                .where(Role.role_code == "SALES_REP")
                .where(User.id.in_(auth_ids))
                .order_by(User.display_name.asc())
            ).all()
        )

    # ---- 归属区间 ----
    def primary_assignment(
        self, merchant_id: UUID, dataset_id: UUID, at: datetime
    ) -> MerchantAssignment | None:
        return self.session.scalars(
            select(MerchantAssignment)
            .where(
                MerchantAssignment.merchant_id == merchant_id,
                MerchantAssignment.dataset_id == dataset_id,
                MerchantAssignment.assignment_type == ASSIGNMENT_PRIMARY,
                MerchantAssignment.valid_range.op("@>")(_tstz_point(at)),
            )
            .order_by(MerchantAssignment.created_at.desc())
        ).first()

    def active_primary_open_ended(
        self, merchant_id: UUID, owner_user_id: UUID, dataset_id: UUID
    ) -> MerchantAssignment | None:
        """找到旧负责人当前开放（上界无限）的主归属区间。"""
        return self.session.scalars(
            select(MerchantAssignment)
            .where(
                MerchantAssignment.merchant_id == merchant_id,
                MerchantAssignment.owner_user_id == owner_user_id,
                MerchantAssignment.dataset_id == dataset_id,
                MerchantAssignment.assignment_type == ASSIGNMENT_PRIMARY,
                func.upper_inf(MerchantAssignment.valid_range).is_(True),
            )
        ).first()

    def current_primary_interval(
        self, merchant_id: UUID, owner_user_id: UUID,
        dataset_id: UUID, at: datetime,
    ) -> MerchantAssignment | None:
        """旧负责人在生效时点命中的主区间（上界可能开放，也可能是反向调拨时的有限界）。"""
        return self.session.scalars(
            select(MerchantAssignment)
            .where(
                MerchantAssignment.merchant_id == merchant_id,
                MerchantAssignment.owner_user_id == owner_user_id,
                MerchantAssignment.dataset_id == dataset_id,
                MerchantAssignment.assignment_type == ASSIGNMENT_PRIMARY,
                MerchantAssignment.valid_range.op("@>")(_tstz_point(at)),
            )
            .order_by(MerchantAssignment.created_at.desc())
        ).first()

    # ---- 未完成事实 ----
    def open_visit_tasks(
        self, merchant_id: UUID, from_user_id: UUID
    ) -> list[VisitTask]:
        return list(self.session.scalars(
            select(VisitTask).where(
                VisitTask.merchant_id == merchant_id,
                VisitTask.assigned_user_id == from_user_id,
                VisitTask.status.in_(_OPEN_VISIT_STATUSES),
            )
        ).all())

    def open_tasks(self, merchant_id: UUID, from_user_id: UUID) -> list[TaskItem]:
        """待交接的未关闭待办（OPEN / IN_PROGRESS；CLOSED 不可交接、不转移）。"""
        return list(self.session.scalars(
            select(TaskItem).where(
                TaskItem.related_object_type == "MERCHANT",
                TaskItem.related_object_id == merchant_id,
                TaskItem.assignee_id == from_user_id,
                TaskItem.status.in_(("OPEN", "IN_PROGRESS")),
            )
        ).all())

    def active_temporary_assignments(
        self, merchant_id: UUID, at: datetime
    ) -> list[MerchantAssignment]:
        """生效时点仍有效的临时协作授权（未到期）。"""
        return list(self.session.scalars(
            select(MerchantAssignment)
            .where(
                MerchantAssignment.merchant_id == merchant_id,
                MerchantAssignment.assignment_type == ASSIGNMENT_TEMPORARY,
                MerchantAssignment.valid_range.op("@>")(_tstz_point(at)),
            )
        ).all())

    # ---- 合同分界（预览/快照）----
    def contracts_boundary(
        self, merchant_id: UUID, dataset_id: UUID
    ) -> list[Contract]:
        return list(self.session.scalars(
            select(Contract).where(
                Contract.merchant_id == merchant_id,
                Contract.dataset_id == dataset_id,
                Contract.status.in_(("APPROVED", "EFFECTIVE")),
            )
        ).all())

    # ---- 生效写入 ----
    def close_primary_interval(
        self, assignment: MerchantAssignment, effective_at: datetime
    ) -> None:
        """把旧负责人开放主归属区间截断为 [..., effective_at)（半开，生效日含起归新负责人）。"""
        assignment.valid_range = func.tstzrange(
            func.lower(MerchantAssignment.valid_range), effective_at, "[)",
        )
        self.session.add(assignment)

    def insert_primary_interval(
        self, *, merchant_id: UUID, owner_user_id: UUID, dataset_id: UUID,
        start: datetime, dataset_type: str = "PRODUCTION",
    ) -> MerchantAssignment:
        """新负责人自主归属：[start, ∞)；数据域随商家（默认 PRODUCTION）。"""
        a = MerchantAssignment(
            merchant_id=merchant_id,
            owner_user_id=owner_user_id,
            assignment_type=ASSIGNMENT_PRIMARY,
            valid_range=func.tstzrange(start, None, "[)"),
            dataset_type=dataset_type,
            dataset_id=dataset_id,
        )
        self.session.add(a)
        self.session.flush()
        return a
