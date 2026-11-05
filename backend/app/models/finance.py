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

# 流水来源（T35：金额只记流水，不做在线支付）。
# 收入：SALES_REVENUE（送达确认）；支出：PROCUREMENT/PAYROLL/ASSET/
# COMPANY_PURCHASE/OPEX 等。旧值保留以兼容历史数据（不删除、不改写）。
SOURCE_INCOME_SALES = "SALES_REVENUE"
SOURCE_EXPENSE_PROCUREMENT = "PROCUREMENT"
SOURCE_EXPENSE_PAYROLL = "PAYROLL"
SOURCE_EXPENSE_ASSET = "ASSET"
SOURCE_EXPENSE_COMPANY = "COMPANY_PURCHASE"
SOURCE_EXPENSE_OPEX = "OPEX"
SOURCE_EXPENSE_OTHER = "OTHER"

SOURCE_KINDS: tuple[str, ...] = (
    "OPEX",
    "PROCUREMENT",
    "RENT",
    "COMMISSION",
    "OTHER",
    SOURCE_INCOME_SALES,
    SOURCE_EXPENSE_PAYROLL,
    SOURCE_EXPENSE_ASSET,
    SOURCE_EXPENSE_COMPANY,
)

_SOURCE_KIND_IN = ", ".join(f"'{kind}'" for kind in SOURCE_KINDS)


class FinanceTransaction(Base, AuditMixin, DatasetMixin):
    __tablename__ = "finance_transactions"
    __table_args__ = (
        CheckConstraint("amount_fen <> 0", name="finance_tx_amount_nonzero"),
        CheckConstraint(
            "tx_type IN ('EXPENSE','INCOME','ADJUSTMENT')",
            name="finance_tx_type_check",
        ),
        CheckConstraint(
            f"source_kind IN ({_SOURCE_KIND_IN})",
            name="finance_tx_source_kind_check",
        ),
        Index("ix_finance_tx_biz_date", "biz_date"),
        Index("ix_finance_tx_source", "source_kind"),
        # 幂等防线：同一单据 + 同一流水类型/来源只允许一笔，重复触发回读既有行
        Index(
            "uq_finance_tx_source_object",
            "related_object_type",
            "related_object_id",
            "tx_type",
            "source_kind",
            unique=True,
        ),
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
