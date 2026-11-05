"""商家责任归属区间与等级变更模型（PRD §4.3 / §4.6）。

- 责任归属一律走 MerchantAssignment 生效区间，不直接覆盖商家 owner 字段
  （Merchant 本身也没有 owner 列）。同一商家的「主责任」区间不得重叠；
  「临时授权」允许多人并行，不做主责任互斥。
- 等级 A/B/C 由片区经理维护，调整写入 MerchantGradeChange 留审计，且只影响
  生效日之后的排班/预警，覆盖率与绩效按发生时口径快照，不回溯（§4.6/R35）。
"""

from datetime import date
from typing import Any
from uuid import UUID

from sqlalchemy import CheckConstraint, Date, ForeignKey, String, text
from sqlalchemy.dialects.postgresql import TSTZRANGE, ExcludeConstraint
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import AuditMixin, Base, DatasetMixin, pk_uuid


class MerchantAssignment(Base, AuditMixin, DatasetMixin):
    """责任归属生效区间（半开区间 [lower, upper)，upper 为 NULL 表示至今）。"""

    __tablename__ = "merchant_assignments"
    __table_args__ = (
        CheckConstraint(
            "lower(valid_range) IS NOT NULL", name="merch_assign_range_lower_notnull"
        ),
        CheckConstraint(
            "assignment_type IN ('PRIMARY','TEMPORARY')",
            name="merch_assign_type_check",
        ),
        # 同一商家的主责任区间互不重叠（btree_gist 提供 UUID 等值 + 区间相交）。
        # 临时授权不纳入该排他约束，允许并行协作。
        ExcludeConstraint(
            ("merchant_id", "="),
            ("valid_range", "&&"),
            using="gist",
            postgresql_where=text("assignment_type = 'PRIMARY'"),
            name="excl_primary_assignment_range",
        ),
    )

    id: Mapped[UUID] = pk_uuid()
    merchant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("merchants.id", ondelete="RESTRICT"),
        nullable=False,
    )
    owner_user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    assignment_type: Mapped[str] = mapped_column(String(16), nullable=False)
    valid_range: Mapped[Any] = mapped_column(TSTZRANGE, nullable=False)
    reason: Mapped[str | None] = mapped_column(String(255), nullable=True)


class MerchantGradeChange(Base, AuditMixin, DatasetMixin):
    """商家等级调整历史（PRD §4.6）：只影响生效日之后，不回溯历史。"""

    __tablename__ = "merchant_grade_changes"
    __table_args__ = (
        CheckConstraint(
            "new_grade IN ('A','B','C')", name="merch_grade_change_new_check"
        ),
        CheckConstraint(
            "old_grade IN ('A','B','C') OR old_grade IS NULL",
            name="merch_grade_change_old_check",
        ),
    )

    id: Mapped[UUID] = pk_uuid()
    merchant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("merchants.id", ondelete="RESTRICT"),
        nullable=False,
    )
    old_grade: Mapped[str | None] = mapped_column(String(1), nullable=True)
    new_grade: Mapped[str] = mapped_column(String(1), nullable=False)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    reason: Mapped[str] = mapped_column(String(255), nullable=False)
