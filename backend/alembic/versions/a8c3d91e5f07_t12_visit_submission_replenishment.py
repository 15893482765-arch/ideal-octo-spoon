"""T12 巡访提交、销量与补货联动（PRD §4.6 提交表单 / §4.9 补货状态链）。

- visit_records：新增冰柜状态、设备异常、当日结论、陈列阈值快照与达标判定；
- 新表 visit_sales_lines：巡访上报 SKU 销量/反馈行（上报口径，不入正式指标）；
- replenishment_orders：状态 CHECK 增 DRAFT（对齐 PRD §4.9 DRAFT→SUBMITTED→…），
  committed_boxes 约束放宽（草稿可为 0），增巡访来源回链列。

Revision ID: a8c3d91e5f07
Revises: d7e2f4a91c05
Create Date: 2026-10-13
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a8c3d91e5f07"
down_revision = "d7e2f4a91c05"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ---- visit_records：T12 提交表单字段 ----
    op.add_column(
        "visit_records",
        sa.Column("freezer_status", sa.String(length=16), server_default="'NORMAL'"),
    )
    op.add_column("visit_records", sa.Column("device_issue", sa.Text(), nullable=True))
    op.add_column(
        "visit_records",
        sa.Column("conclusion", sa.String(length=24), server_default="'NORMAL'"),
    )
    op.add_column(
        "visit_records", sa.Column("display_threshold_pct", sa.Integer(), nullable=True
        )
    )
    op.add_column("visit_records", sa.Column("display_met", sa.Boolean(), nullable=True))
    op.create_check_constraint(
        "visit_records_freezer_status_check",
        "visit_records",
        "freezer_status IS NULL OR freezer_status IN "
        "('NORMAL','ABSENT','POWERED_OFF','DIRTY','DAMAGED','OTHER')",
    )
    op.create_check_constraint(
        "visit_records_conclusion_check",
        "visit_records",
        "conclusion IS NULL OR conclusion IN "
        "('NORMAL','NEEDS_REPLENISHMENT','NEEDS_RECTIFICATION',"
        "'RISK_MERCHANT','PROPOSE_TERMINATION')",
    )
    op.create_check_constraint(
        "visit_records_threshold_range",
        "visit_records",
        "display_threshold_pct IS NULL OR (display_threshold_pct BETWEEN 0 AND 100)",
    )

    # ---- 新表：visit_sales_lines（上报销量/反馈行） ----
    op.create_table(
        "visit_sales_lines",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"),
                  nullable=False),
        sa.Column(
            "visit_record_id", sa.UUID(), nullable=False
        ),
        sa.Column("sku_id", sa.UUID(), nullable=False),
        sa.Column("reported_boxes", sa.Numeric(12, 2), nullable=False),
        sa.Column("feedback", sa.Text(), nullable=True),
        sa.Column(
            "out_of_stock", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by_id", sa.UUID(), nullable=True),
        sa.Column("updated_by_id", sa.UUID(), nullable=True),
        sa.Column(
            "dataset_type", sa.String(length=16), server_default=sa.text("'PRODUCTION'"),
            nullable=False,
        ),
        sa.Column(
            "dataset_id", sa.UUID(), server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.CheckConstraint("reported_boxes >= 0", name="visit_sales_lines_boxes_nonneg"),
        sa.CheckConstraint(
            "reported_boxes > 0 OR feedback IS NOT NULL OR out_of_stock = TRUE",
            name="visit_sales_lines_meaningful",
        ),
        sa.ForeignKeyConstraint(
            ["visit_record_id"], ["visit_records.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["sku_id"], ["skus.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_visit_sales_lines_record", "visit_sales_lines", ["visit_record_id"]
    )
    op.create_index("ix_visit_sales_lines_sku", "visit_sales_lines", ["sku_id"])
    op.create_unique_constraint(
        "uq_visit_sales_lines_record_sku", "visit_sales_lines",
        ["visit_record_id", "sku_id"],
    )

    # ---- replenishment_orders：DRAFT 状态 + 巡访回链 ----
    op.drop_constraint("replenishment_orders_status_check", "replenishment_orders",
                      type_="check")
    op.create_check_constraint(
        "replenishment_orders_status_check",
        "replenishment_orders",
        "status IN ('DRAFT','REQUESTED','CONFIRMED','IN_TRANSIT',"
        "'DELIVERED','OVERDUE','CANCELLED')",
    )
    op.drop_constraint(
        "replenishment_orders_boxes_positive", "replenishment_orders", type_="check"
    )
    op.create_check_constraint(
        "replenishment_orders_boxes_positive",
        "replenishment_orders",
        "status = 'DRAFT' OR committed_boxes > 0",
    )
    op.add_column(
        "replenishment_orders",
        sa.Column("source_visit_record_id", sa.UUID(), nullable=True),
    )
    op.create_foreign_key(
        "replenishment_orders_source_visit_fk",
        "replenishment_orders",
        "visit_records",
        ["source_visit_record_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_replenishment_orders_source_visit",
        "replenishment_orders",
        ["source_visit_record_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_replenishment_orders_source_visit", table_name="replenishment_orders"
    )
    op.drop_constraint(
        "replenishment_orders_source_visit_fk", "replenishment_orders", type_="foreignkey"
    )
    op.drop_column("replenishment_orders", "source_visit_record_id")
    op.drop_constraint(
        "replenishment_orders_boxes_positive", "replenishment_orders", type_="check"
    )
    op.create_check_constraint(
        "replenishment_orders_boxes_positive",
        "replenishment_orders",
        "committed_boxes > 0",
    )
    op.drop_constraint("replenishment_orders_status_check", "replenishment_orders",
                       type_="check")
    op.create_check_constraint(
        "replenishment_orders_status_check",
        "replenishment_orders",
        "status IN ('REQUESTED','CONFIRMED','IN_TRANSIT','DELIVERED','OVERDUE',"
        "'CANCELLED')",
    )
    op.drop_constraint(
        "uq_visit_sales_lines_record_sku", "visit_sales_lines", type_="unique"
    )
    op.drop_index("ix_visit_sales_lines_sku", table_name="visit_sales_lines")
    op.drop_index("ix_visit_sales_lines_record", table_name="visit_sales_lines")
    op.drop_table("visit_sales_lines")
    op.drop_constraint("visit_records_threshold_range", "visit_records", type_="check")
    op.drop_constraint("visit_records_conclusion_check", "visit_records", type_="check")
    op.drop_constraint(
        "visit_records_freezer_status_check", "visit_records", type_="check"
    )
    op.drop_column("visit_records", "display_met")
    op.drop_column("visit_records", "display_threshold_pct")
    op.drop_column("visit_records", "conclusion")
    op.drop_column("visit_records", "device_issue")
    op.drop_column("visit_records", "freezer_status")
