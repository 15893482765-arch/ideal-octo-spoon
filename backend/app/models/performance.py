"""绩效与工资。"""

from __future__ import annotations

from datetime import date
from typing import Any
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import AuditMixin, Base, DatasetMixin, pk_uuid
from app.models.enums import PayrollStatus


class PerformanceRecord(Base, AuditMixin, DatasetMixin):
    __tablename__ = "performance_records"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "period_start", "period_end", name="uq_performance_user_period"
        ),
        CheckConstraint("period_end >= period_start", name="performance_period_range"),
        # 商家主动取消默认不扣款（v1.2 R40）：扣减必须引用审批依据
        # 扣减字段允许 0 或正，负值由业务层校验
        CheckConstraint("deduction_fen <= 0", name="performance_deduction_nonpositive"),
        Index("ix_performance_user_period", "user_id", "period_start"),
    )

    id: Mapped[UUID] = pk_uuid()
    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    new_contracts_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    confirmed_sales_amount_fen: Mapped[int] = mapped_column(
        nullable=False, default=0, server_default=text("0")
    )
    commission_fen: Mapped[int] = mapped_column(
        nullable=False, default=0, server_default=text("0")
    )
    # 责任扣款（<=0 分；商家主动取消默认 0，须由审批单据产生）
    deduction_fen: Mapped[int] = mapped_column(
        nullable=False, default=0, server_default=text("0")
    )
    # 扣款依据单据（审批单 ID）；扣款为 0 时可空，扣款 < 0 时必填
    deduction_approval_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("approval_requests.id", ondelete="RESTRICT"),
        nullable=True,
    )
    snapshot_json: Mapped[dict[str, Any]] = mapped_column(
        nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )


class PayrollRecord(Base, AuditMixin, DatasetMixin):
    __tablename__ = "payroll_records"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "period_start", "period_end", name="uq_payroll_user_period"
        ),
        CheckConstraint("period_end >= period_start", name="payroll_period_range"),
        CheckConstraint(
            "status IN ('DRAFT','REVIEWED','APPROVED','PAID')",
            name="payroll_status_check",
        ),
        CheckConstraint("base_salary_fen >= 0", name="payroll_base_nonneg"),
        Index("ix_payroll_user_period", "user_id", "period_start"),
    )

    id: Mapped[UUID] = pk_uuid()
    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    base_salary_fen: Mapped[int] = mapped_column(
        nullable=False, default=0, server_default=text("0")
    )
    performance_fen: Mapped[int] = mapped_column(
        nullable=False, default=0, server_default=text("0")
    )
    allowance_fen: Mapped[int] = mapped_column(
        nullable=False, default=0, server_default=text("0")
    )
    social_insurance_fen: Mapped[int] = mapped_column(
        nullable=False, default=0, server_default=text("0")
    )
    tax_fen: Mapped[int] = mapped_column(nullable=False, default=0, server_default=text("0"))
    # 个税/社保/补贴/底薪/绩效/其他调整均记录在 JSONB（避免字段膨胀）
    adjustments_json: Mapped[dict[str, Any]] = mapped_column(
        nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=PayrollStatus.DRAFT.value,
        server_default=text("'DRAFT'"),
    )
