"""财务流水（仅记录与损益；押金不在此处走账——v1.2 R37）。"""

from __future__ import annotations

from datetime import date
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    String,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import AuditMixin, Base, DatasetMixin, pk_uuid


class FinanceTransaction(Base, AuditMixin, DatasetMixin):
    __tablename__ = "finance_transactions"
    __table_args__ = (
        CheckConstraint("amount_fen <> 0", name="finance_tx_amount_nonzero"),
        CheckConstraint(
            "tx_type IN ('EXPENSE','INCOME','ADJUSTMENT')",
            name="finance_tx_type_check",
        ),
        CheckConstraint(
            "source_kind IN ('OPEX','PROCUREMENT','RENT','COMMISSION','OTHER')",
            name="finance_tx_source_kind_check",
        ),
        Index("ix_finance_tx_biz_date", "biz_date"),
        Index("ix_finance_tx_source", "source_kind"),
    )

    id: Mapped[UUID] = pk_uuid()
    biz_date: Mapped[date] = mapped_column(Date, nullable=False)
    tx_type: Mapped[str] = mapped_column(String(16), nullable=False)
    source_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    amount_fen: Mapped[int] = mapped_column(nullable=False)
    related_object_type: Mapped[str] = mapped_column(String(32), nullable=False)
    related_object_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)
    bookkeeper_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
