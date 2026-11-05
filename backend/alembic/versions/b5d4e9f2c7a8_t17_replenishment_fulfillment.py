"""T17: replenishment confirmation & fulfillment loop

Revision ID: b5d4e9f2c7a8
Revises: f3c2d8e5b1a4
Create Date: 2026-10-13 00:00:00.000000

PRD §4.9 / R8 / R16 / A11 / A17 / RK-5：
- 补货状态链改为 DRAFT→SUBMITTED→CONFIRMED→PARTIALLY_FULFILLED/FULFILLED→
  CLOSED/CANCELLED（旧 REQUESTED/IN_TRANSIT/DELIVERED/OVERDUE 保留兼容历史台账）；
- replenishment_orders 增加 fulfilled_boxes（累计满足）、确认人/确认日/调整原因、
  取消人/取消原因；CHECK 保证 0≤fulfilled≤committed、取消必填原因、已确认态 committed>0；
- 新表 replenishment_fulfillments：送达凭证履约事件，(dataset, idempotency_key)
  唯一保证重复回调幂等（Q092/A17），单事件 delivered_boxes>0。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b5d4e9f2c7a8"
down_revision: str | Sequence[str] | None = "f3c2d8e5b1a4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ---- 1) replenishment_orders：状态列扩长 + 数量/确认/取消字段 ----
    op.alter_column(
        "replenishment_orders", "status",
        existing_type=sa.String(length=16),
        type_=sa.String(length=20),
        existing_nullable=False,
        existing_server_default=sa.text("'REQUESTED'"),
    )
    op.add_column(
        "replenishment_orders",
        sa.Column("fulfilled_boxes", sa.Numeric(12, 2), nullable=False,
                  server_default=sa.text("0")),
    )
    op.add_column(
        "replenishment_orders",
        sa.Column("confirmer_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=True),
    )
    op.add_column(
        "replenishment_orders",
        sa.Column("confirmed_at", sa.Date(), nullable=True),
    )
    op.add_column(
        "replenishment_orders",
        sa.Column("adjust_reason", sa.String(length=500), nullable=True),
    )
    op.add_column(
        "replenishment_orders",
        sa.Column("cancel_reason", sa.String(length=500), nullable=True),
    )
    op.add_column(
        "replenishment_orders",
        sa.Column("cancelled_by", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=True),
    )

    # 替换状态/数量约束（旧约束把 SUBMITTED 视为非草稿而要求 committed>0，与新流程冲突）
    op.drop_constraint(
        "replenishment_orders_status_check", "replenishment_orders")
    op.create_check_constraint(
        "replenishment_orders_status_check", "replenishment_orders",
        "status IN ('DRAFT','SUBMITTED','CONFIRMED','PARTIALLY_FULFILLED',"
        "'FULFILLED','CLOSED','CANCELLED','REQUESTED','IN_TRANSIT',"
        "'DELIVERED','OVERDUE')",
    )
    op.drop_constraint(
        "replenishment_orders_boxes_positive", "replenishment_orders")
    op.create_check_constraint(
        "replenishment_orders_boxes_positive", "replenishment_orders",
        "status NOT IN ('CONFIRMED','PARTIALLY_FULFILLED','FULFILLED','CLOSED',"
        "'IN_TRANSIT','DELIVERED') OR committed_boxes > 0",
    )
    op.create_check_constraint(
        "replenishment_orders_committed_nonneg", "replenishment_orders",
        "committed_boxes >= 0",
    )
    op.create_check_constraint(
        "replenishment_orders_fulfilled_within_committed",
        "replenishment_orders",
        "fulfilled_boxes >= 0 AND fulfilled_boxes <= committed_boxes",
    )
    op.create_check_constraint(
        "replenishment_orders_cancel_reason_required", "replenishment_orders",
        "status <> 'CANCELLED' OR cancel_reason IS NOT NULL",
    )

    # ---- 2) 履约事件表 ----
    op.create_table(
        "replenishment_fulfillments",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("order_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("replenishment_orders.id", ondelete="RESTRICT"),
                  nullable=False),
        sa.Column("delivered_boxes", sa.Numeric(12, 2), nullable=False),
        sa.Column("delivered_on", sa.Date(), nullable=False),
        sa.Column("proof_file_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("file_objects.id", ondelete="RESTRICT"),
                  nullable=True),
        sa.Column("idempotency_key", sa.String(length=64), nullable=False),
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
        sa.CheckConstraint("delivered_boxes > 0",
                           name="repl_fulfillment_boxes_positive"),
        sa.UniqueConstraint("dataset_type", "dataset_id", "idempotency_key",
                            name="uq_repl_fulfillment_idempotency"),
    )
    op.create_index("ix_repl_fulfillment_order",
                    "replenishment_fulfillments", ["order_id"])


def downgrade() -> None:
    op.drop_index("ix_repl_fulfillment_order", table_name="replenishment_fulfillments")
    op.drop_table("replenishment_fulfillments")

    op.drop_constraint(
        "replenishment_orders_cancel_reason_required", "replenishment_orders")
    op.drop_constraint(
        "replenishment_orders_fulfilled_within_committed", "replenishment_orders")
    op.drop_constraint(
        "replenishment_orders_committed_nonneg", "replenishment_orders")
    op.drop_constraint(
        "replenishment_orders_boxes_positive", "replenishment_orders")
    op.create_check_constraint(
        "replenishment_orders_boxes_positive", "replenishment_orders",
        "status = 'DRAFT' OR committed_boxes > 0",
    )
    op.drop_constraint(
        "replenishment_orders_status_check", "replenishment_orders")
    op.create_check_constraint(
        "replenishment_orders_status_check", "replenishment_orders",
        "status IN ('DRAFT','REQUESTED','CONFIRMED','IN_TRANSIT',"
        "'DELIVERED','OVERDUE','CANCELLED')",
    )
    op.drop_column("replenishment_orders", "cancelled_by")
    op.drop_column("replenishment_orders", "cancel_reason")
    op.drop_column("replenishment_orders", "adjust_reason")
    op.drop_column("replenishment_orders", "confirmed_at")
    op.drop_column("replenishment_orders", "confirmer_id")
    op.drop_column("replenishment_orders", "fulfilled_boxes")
    op.alter_column(
        "replenishment_orders", "status",
        existing_type=sa.String(length=20),
        type_=sa.String(length=16),
        existing_nullable=False,
        existing_server_default=sa.text("'REQUESTED'"),
    )
