"""合同、合同变更、合同审批。"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import AuditMixin, Base, DatasetMixin, ImmutableMixin, pk_uuid
from app.models.enums import ContractStatus


class Contract(Base, ImmutableMixin, AuditMixin, DatasetMixin):
    __tablename__ = "contracts"
    __table_args__ = (
        UniqueConstraint(
            "dataset_type", "dataset_id", "code", name="uq_contracts_dataset_code"
        ),
        CheckConstraint(
            "status IN ('DRAFT','PENDING_APPROVAL','ACTIVE','EXPIRED','TERMINATED')",
            name="contracts_status_check",
        ),
        CheckConstraint("end_date >= start_date", name="contracts_date_range"),
        CheckConstraint("monthly_rent_fen >= 0", name="contracts_monthly_rent_nonneg"),
        # 陈列达标阈值（百分比 0-100）
        CheckConstraint(
            "display_threshold_pct >= 0 AND display_threshold_pct <= 100",
            name="contracts_display_threshold_range",
        ),
        Index("ix_contracts_merchant", "merchant_id"),
        Index("ix_contracts_status_end", "status", "end_date"),
    )

    id: Mapped[UUID] = pk_uuid()
    code: Mapped[str] = mapped_column(String(32), nullable=False)
    merchant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("merchants.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=ContractStatus.DRAFT.value,
        server_default=text("'DRAFT'"),
    )
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    display_threshold_pct: Mapped[int] = mapped_column(
        Integer, nullable=False, default=50, server_default=text("50")
    )
    monthly_rent_fen: Mapped[int] = mapped_column(
        nullable=False, default=0, server_default=text("0")
    )
    # 提成比例（0-1，DECIMAL(5,4) 保留 4 位小数）
    commission_rate: Mapped[Decimal] = mapped_column(
        Numeric(5, 4), nullable=False, default=Decimal("0"), server_default=text("0")
    )


class ContractChange(Base, AuditMixin):
    """合同变更/终止：物理历史，不删改（PRD §1.5）。"""

    __tablename__ = "contract_changes"
    __table_args__ = (
        Index("ix_contract_changes_contract", "contract_id"),
    )

    id: Mapped[UUID] = pk_uuid()
    contract_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("contracts.id", ondelete="RESTRICT"), nullable=False
    )
    change_type: Mapped[str] = mapped_column(String(32), nullable=False)
    before_snapshot: Mapped[dict[str, Any]] = mapped_column(nullable=False)
    after_snapshot: Mapped[dict[str, Any]] = mapped_column(nullable=False)
    reason: Mapped[str] = mapped_column(String(255), nullable=False)


class ContractApproval(Base, AuditMixin):
    __tablename__ = "contract_approvals"
    __table_args__ = (
        Index("ix_contract_approvals_contract", "contract_id"),
    )

    id: Mapped[UUID] = pk_uuid()
    contract_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("contracts.id", ondelete="RESTRICT"), nullable=False
    )
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    approver_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    comment: Mapped[str | None] = mapped_column(String(255), nullable=True)
