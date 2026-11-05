"""T18: procurement suggestions, purchase orders & optional supplier portal

Revision ID: c6a5f1d3b8e2
Revises: b5d4e9f2c7a8
Create Date: 2026-10-14 00:00:00.000000

PRD §4.9 / R17 / R18 / R33 / R34 / §5.1：
- suppliers 厂商业务档案；supplier_authorizations 厂商账号×品牌×片区授权；
- purchase_orders 采购单（头，total_amount_fen 由行服务端重算）；
- purchase_order_lines 采购行（箱数 NUMERIC(12,2) × 含税单价分 = 行金额分）；
- supply_confirmations 厂商供货确认（按行，量/日期/凭证，幂等；不付款不入库）。
旧 procurement_orders（v12 预留简化表）保持不动，供 T02/T06 旧测试与
SeedCheck 负例扫描兼容；新业务只写 purchase_*。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c6a5f1d3b8e2"
down_revision: str | Sequence[str] | None = "b5d4e9f2c7a8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _audit_dataset_cols() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("dataset_type", sa.String(length=16), nullable=False,
                  server_default=sa.text("'PRODUCTION'")),
        sa.Column("dataset_id", postgresql.UUID(as_uuid=True), nullable=False),
    ]


def upgrade() -> None:
    # ---- suppliers ----
    op.create_table(
        "suppliers",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("contact_name", sa.String(length=64), nullable=True),
        sa.Column("contact_phone", sa.String(length=32), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        *_audit_dataset_cols(),
        sa.UniqueConstraint("dataset_type", "dataset_id", "code",
                            name="uq_suppliers_dataset_code"),
        sa.CheckConstraint("char_length(name) > 0", name="suppliers_name_nonempty"),
    )
    op.create_index("ix_suppliers_dataset", "suppliers",
                    ["dataset_type", "dataset_id"])

    # ---- supplier_authorizations ----
    op.create_table(
        "supplier_authorizations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("supplier_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("suppliers.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("brand_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("brands.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("region_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("regions.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("dataset_type", sa.String(length=16), nullable=False,
                  server_default=sa.text("'PRODUCTION'")),
        sa.Column("dataset_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.UniqueConstraint(
            "supplier_id", "user_id", "brand_id", "region_id",
            name="uq_supplier_auth_scope"),
    )
    op.create_index("ix_supplier_auth_user", "supplier_authorizations", ["user_id"])

    # ---- purchase_orders ----
    op.create_table(
        "purchase_orders",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("supplier_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("suppliers.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("brand_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("brands.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("region_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("regions.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("expected_date", sa.Date(), nullable=True),
        sa.Column("total_amount_fen", sa.BigInteger(), nullable=False,
                  server_default=sa.text("0")),
        sa.Column("status", sa.String(length=24), nullable=False,
                  server_default=sa.text("'DRAFT'")),
        sa.Column("created_by_id_internal", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("confirmed_at", sa.Date(), nullable=True),
        sa.Column("confirmed_by_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("received_at", sa.Date(), nullable=True),
        sa.Column("cancel_reason", sa.String(length=500), nullable=True),
        sa.Column("cancelled_at", sa.Date(), nullable=True),
        sa.Column("note", sa.String(length=500), nullable=True),
        *_audit_dataset_cols(),
        sa.UniqueConstraint("dataset_type", "dataset_id", "code",
                            name="uq_purchase_orders_dataset_code"),
        sa.CheckConstraint(
            "status IN ('DRAFT','CONFIRMED','SUPPLIER_CONFIRMED',"
            "'SUPPLY_REJECTED','RECEIVED','CANCELLED')",
            name="purchase_orders_status_check"),
        sa.CheckConstraint("total_amount_fen >= 0",
                           name="purchase_orders_total_nonneg"),
        sa.CheckConstraint(
            "cancelled_at IS NULL OR cancel_reason IS NOT NULL",
            name="purchase_orders_cancel_reason_required"),
    )
    op.create_index("ix_purchase_orders_supplier", "purchase_orders", ["supplier_id"])
    op.create_index("ix_purchase_orders_brand_region", "purchase_orders",
                    ["brand_id", "region_id"])
    op.create_index("ix_purchase_orders_status", "purchase_orders", ["status"])

    # ---- purchase_order_lines ----
    op.create_table(
        "purchase_order_lines",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("order_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("purchase_orders.id", ondelete="RESTRICT"),
                  nullable=False),
        sa.Column("sku_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("skus.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("quantity_cases", sa.Numeric(12, 2), nullable=False),
        sa.Column("tax_inclusive_unit_price_fen", sa.BigInteger(), nullable=False),
        sa.Column("line_amount_fen", sa.BigInteger(), nullable=False,
                  server_default=sa.text("0")),
        sa.Column("source_confirmed_boxes", sa.Numeric(12, 2), nullable=False,
                  server_default=sa.text("0")),
        *_audit_dataset_cols(),
        sa.UniqueConstraint("order_id", "sku_id",
                            name="uq_purchase_lines_order_sku"),
        sa.CheckConstraint("quantity_cases > 0",
                           name="purchase_lines_qty_positive"),
        sa.CheckConstraint("tax_inclusive_unit_price_fen >= 0",
                           name="purchase_lines_price_nonneg"),
        sa.CheckConstraint("line_amount_fen >= 0",
                           name="purchase_lines_amount_nonneg"),
    )
    op.create_index("ix_purchase_lines_sku", "purchase_order_lines", ["sku_id"])

    # ---- supply_confirmations ----
    op.create_table(
        "supply_confirmations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("order_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("purchase_orders.id", ondelete="RESTRICT"),
                  nullable=False),
        sa.Column("order_line_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("purchase_order_lines.id", ondelete="RESTRICT"),
                  nullable=False),
        sa.Column("supplier_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("suppliers.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("supplier_user_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False,
                  server_default=sa.text("'CONFIRMED'")),
        sa.Column("confirmed_quantity_cases", sa.Numeric(12, 2), nullable=True),
        sa.Column("expected_delivery_date", sa.Date(), nullable=True),
        sa.Column("proof_file_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("file_objects.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("reject_reason", sa.String(length=500), nullable=True),
        sa.Column("note", sa.String(length=500), nullable=True),
        *_audit_dataset_cols(),
        sa.UniqueConstraint("order_line_id", "supplier_user_id",
                            name="uq_supply_confirm_line_supplier"),
        sa.CheckConstraint("status IN ('CONFIRMED','REJECTED')",
                           name="supply_confirmations_status_check"),
        sa.CheckConstraint(
            "status <> 'CONFIRMED' OR (confirmed_quantity_cases IS NOT NULL "
            "AND confirmed_quantity_cases > 0 "
            "AND expected_delivery_date IS NOT NULL)",
            name="supply_confirmations_confirmed_fields"),
        sa.CheckConstraint(
            "status <> 'REJECTED' OR reject_reason IS NOT NULL",
            name="supply_confirmations_reject_reason_required"),
    )
    op.create_index("ix_supply_confirmations_order", "supply_confirmations",
                    ["order_id"])


def downgrade() -> None:
    op.drop_index("ix_supply_confirmations_order", table_name="supply_confirmations")
    op.drop_table("supply_confirmations")
    op.drop_index("ix_purchase_lines_sku", table_name="purchase_order_lines")
    op.drop_table("purchase_order_lines")
    op.drop_index("ix_purchase_orders_status", table_name="purchase_orders")
    op.drop_index("ix_purchase_orders_brand_region", table_name="purchase_orders")
    op.drop_index("ix_purchase_orders_supplier", table_name="purchase_orders")
    op.drop_table("purchase_orders")
    op.drop_index("ix_supplier_auth_user", table_name="supplier_authorizations")
    op.drop_table("supplier_authorizations")
    op.drop_index("ix_suppliers_dataset", table_name="suppliers")
    op.drop_table("suppliers")
