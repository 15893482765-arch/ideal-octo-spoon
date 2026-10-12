"""补货需求与确认。"""

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
from app.models.enums import ReplenishmentStatus


class ReplenishmentOrder(Base, AuditMixin, DatasetMixin):
    __tablename__ = "replenishment_orders"
    __table_args__ = (
        UniqueConstraint(
            "dataset_type", "dataset_id", "code", name="uq_replenishment_orders_dataset_code"
        ),
        CheckConstraint(
            "status IN ('REQUESTED','CONFIRMED','IN_TRANSIT','DELIVERED','OVERDUE','CANCELLED')",
            name="replenishment_orders_status_check",
        ),
        CheckConstraint("committed_boxes > 0", name="replenishment_orders_boxes_positive"),
        CheckConstraint(
            "promised_delivery_date IS NULL OR promised_delivery_date >= request_date",
            name="replenishment_orders_date_order",
        ),
        Index("ix_replenishment_orders_merchant", "merchant_id"),
        Index("ix_replenishment_orders_status_promised", "status", "promised_delivery_date"),
    )

    id: Mapped[UUID] = pk_uuid()
    code: Mapped[str] = mapped_column(String(32), nullable=False)
    merchant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("merchants.id", ondelete="RESTRICT"), nullable=False
    )
    sku_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("skus.id", ondelete="RESTRICT"), nullable=False
    )
    requested_boxes: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    committed_boxes: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    request_date: Mapped[date] = mapped_column(Date, nullable=False)
    promised_delivery_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    actual_delivery_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=ReplenishmentStatus.REQUESTED.value,
        server_default=text("'REQUESTED'"),
    )


class ReplenishmentConfirm(Base, AuditMixin):
    __tablename__ = "replenishment_confirms"
    __table_args__ = (
        UniqueConstraint("order_id", name="uq_replenishment_confirm_order"),
        Index("ix_replenishment_confirms_confirmer", "confirmer_id"),
    )

    id: Mapped[UUID] = pk_uuid()
    order_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("replenishment_orders.id", ondelete="RESTRICT"),
        nullable=False,
    )
    confirmer_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    confirmed_boxes: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)
