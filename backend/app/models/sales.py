"""销量确认。"""

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
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import AuditMixin, Base, DatasetMixin, pk_uuid


class SalesConfirmation(Base, AuditMixin, DatasetMixin):
    __tablename__ = "sales_confirmations"
    __table_args__ = (
        CheckConstraint("confirmed_boxes >= 0", name="sales_confirmations_boxes_nonneg"),
        CheckConstraint("amount_fen >= 0", name="sales_confirmations_amount_nonneg"),
        Index("ix_sales_confirmations_merchant_date", "merchant_id", "biz_date"),
    )

    id: Mapped[UUID] = pk_uuid()
    merchant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("merchants.id", ondelete="RESTRICT"), nullable=False
    )
    sku_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("skus.id", ondelete="RESTRICT"), nullable=False
    )
    biz_date: Mapped[date] = mapped_column(Date, nullable=False)
    confirmed_boxes: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    amount_fen: Mapped[int] = mapped_column(nullable=False, default=0, server_default=text("0"))
    confirmer_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
