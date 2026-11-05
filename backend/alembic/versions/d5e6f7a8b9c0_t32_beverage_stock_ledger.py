"""T32: beverage stock ledger（饮料收发存台账）

Revision ID: d5e6f7a8b9c0
Revises: c1a8f5e3b207
Create Date: 2026-10-22 00:00:00.000000

补 Q100 首版无库存域的缺口：
- 新表 beverage_stock_entries：一条流水 = 某 SKU 一次入库（正，采购收货）
  或出库（负，补货送达），现存量 = 流水代数和；
- (source_type, source_id, sku_id) 唯一：重复收货 / 重复履约回调幂等；
- source_id 无 FK（purchase_orders.id / replenishment_fulfillments.id 双来源）。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d5e6f7a8b9c0"
down_revision: str | Sequence[str] | None = "c1a8f5e3b207"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "beverage_stock_entries",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("sku_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("skus.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("change_cases", sa.Numeric(12, 2), nullable=False),
        sa.Column("source_type", sa.String(length=24), nullable=False),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("biz_date", sa.Date(), nullable=False),
        sa.Column("note", sa.String(length=500), nullable=True),
        sa.Column("registered_by", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("dataset_type", sa.String(length=16), nullable=False,
                  server_default=sa.text("'PRODUCTION'")),
        sa.Column("dataset_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.CheckConstraint("change_cases <> 0",
                           name="stock_entries_change_nonzero"),
        sa.CheckConstraint(
            "source_type IN ('PURCHASE_RECEIVE','REPLENISH_FULFILL')",
            name="stock_entries_source_type_check",
        ),
        sa.UniqueConstraint("source_type", "source_id", "sku_id",
                            name="uq_stock_entries_source_sku"),
    )
    op.create_index("ix_stock_entries_sku", "beverage_stock_entries",
                    ["dataset_type", "dataset_id", "sku_id"])
    op.create_index("ix_stock_entries_biz_date", "beverage_stock_entries",
                    ["biz_date"])


def downgrade() -> None:
    op.drop_index("ix_stock_entries_biz_date", table_name="beverage_stock_entries")
    op.drop_index("ix_stock_entries_sku", table_name="beverage_stock_entries")
    op.drop_table("beverage_stock_entries")
