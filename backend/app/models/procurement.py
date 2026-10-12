"""采购单与到货。"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Numeric,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import AuditMixin, Base, DatasetMixin, pk_uuid
from app.models.enums import ProcurementStatus


class ProcurementOrder(Base, AuditMixin, DatasetMixin):
    __tablename__ = "procurement_orders"
    __table_args__ = (
        UniqueConstraint(
            "dataset_type", "dataset_id", "code", name="uq_procurement_orders_dataset_code"
        ),
        CheckConstraint(
            "status IN ('REQUESTED','APPROVED','ORDERED','RECEIVED','CANCELLED')",
            name="procurement_orders_status_check",
        ),
        CheckConstraint("quantity_cases > 0", name="procurement_orders_qty_positive"),
        CheckConstraint("unit_price_fen >= 0", name="procurement_orders_price_nonneg"),
    )

    id: Mapped[UUID] = pk_uuid()
    code: Mapped[str] = mapped_column(String(32), nullable=False)
    sku_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("skus.id", ondelete="RESTRICT"), nullable=False
    )
    supplier_name: Mapped[str] = mapped_column(String(128), nullable=False)
    quantity_cases: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    unit_price_fen: Mapped[int] = mapped_column(
        nullable=False, default=0, server_default=text("0")
    )
    expected_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=ProcurementStatus.REQUESTED.value,
        server_default=text("'REQUESTED'"),
    )


class ProcurementDelivery(Base, AuditMixin):
    __tablename__ = "procurement_deliveries"
    __table_args__ = (
        Index("ix_procurement_deliveries_order", "order_id"),
    )

    id: Mapped[UUID] = pk_uuid()
    order_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("procurement_orders.id", ondelete="RESTRICT"),
        nullable=False,
    )
    received_cases: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    received_date: Mapped[date] = mapped_column(Date, nullable=False)
    receiver_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    # 串号批次：仅记录，不作业务字段
    batch_serial: Mapped[str | None] = mapped_column(String(64), nullable=True)
