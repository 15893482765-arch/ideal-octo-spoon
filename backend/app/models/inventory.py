"""饮料库存流水（T32：收发存台账，补 Q100 首版无库存域的缺口）。

设计口径：
- 只记账、不做完整 WMS：一条流水 = 某 SKU 一次入库（正）或出库（负），
  现存量 = 流水代数和，不落冗余 balance 字段（防对不上账）；
- 入库来源：采购单收货（PURCHASE_RECEIVE，source_id = purchase_orders.id），
  数量取该行的厂商确认供货量（收货前置已校验每行均有 CONFIRMED 确认）；
- 出库来源：补货送达履约（REPLENISH_FULFILL，
  source_id = replenishment_fulfillments.id），数量 = 本次送达箱数；
- 幂等：(source_type, source_id, sku_id) 唯一——重复收货/重复履约回调
  不会重复记账（与状态机/幂等键防线叠加）；
- source_id 不设外键：一张表承接两种来源单据，FK 无法表达；
  一致性由写入方（InventoryService）与唯一约束保证。
"""

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
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import AuditMixin, Base, DatasetMixin, pk_uuid

SOURCE_PURCHASE_RECEIVE = "PURCHASE_RECEIVE"
SOURCE_REPLENISH_FULFILL = "REPLENISH_FULFILL"
_SOURCE_IN = f"'{SOURCE_PURCHASE_RECEIVE}','{SOURCE_REPLENISH_FULFILL}'"


class BeverageStockEntry(Base, AuditMixin, DatasetMixin):
    """饮料库存流水：正数入库（采购收货）、负数出库（补货送达）。"""

    __tablename__ = "beverage_stock_entries"
    __table_args__ = (
        UniqueConstraint(
            "source_type", "source_id", "sku_id",
            name="uq_stock_entries_source_sku",
        ),
        CheckConstraint("change_cases <> 0", name="stock_entries_change_nonzero"),
        CheckConstraint(
            f"source_type IN ({_SOURCE_IN})",
            name="stock_entries_source_type_check",
        ),
        Index("ix_stock_entries_sku", "dataset_type", "dataset_id", "sku_id"),
        Index("ix_stock_entries_biz_date", "biz_date"),
    )

    id: Mapped[UUID] = pk_uuid()
    sku_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("skus.id", ondelete="RESTRICT"),
        nullable=False,
    )
    # 正 = 入库（采购收货）；负 = 出库（补货送达商家）
    change_cases: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    source_type: Mapped[str] = mapped_column(String(24), nullable=False)
    # 来源单据：purchase_orders.id / replenishment_fulfillments.id（双来源不设 FK）
    source_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    biz_date: Mapped[date] = mapped_column(Date, nullable=False)
    note: Mapped[str | None] = mapped_column(String(500), nullable=True)
    registered_by: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
