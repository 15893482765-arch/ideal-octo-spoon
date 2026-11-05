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
    UniqueConstraint,
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
        # T36：送达毛利 = 建议零售价×箱数 − 进价×箱数，两列均由服务端固化
        CheckConstraint("cost_fen >= 0", name="sales_confirmations_cost_nonneg"),
        Index("ix_sales_confirmations_merchant_date", "merchant_id", "biz_date"),
        # T33：一条送达履约事件只产生一条确认（幂等；手工/种子数据为 NULL 不占位）
        UniqueConstraint(
            "source_fulfillment_id", name="uq_sales_confirmations_fulfillment"
        ),
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
    # T36：成本（进价 × 箱数）与毛利（建议零售价 − 进价 的差额），均为整数分
    cost_fen: Mapped[int] = mapped_column(nullable=False, default=0, server_default=text("0"))
    profit_fen: Mapped[int] = mapped_column(nullable=False, default=0, server_default=text("0"))
    confirmer_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    # T33：来源履约事件（补货送达自动确认销量时回写；无 FK，跨域弱引用）
    source_fulfillment_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True
    )
