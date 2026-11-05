"""T14: merchant responsibility transfer orders

Revision ID: c5a3f9b4d2e1
Revises: b4f2e8a31c09
Create Date: 2026-10-13 00:00:00.000000

- merchant_transfers：商家责任调拨单（PRD §4.7 / R9 / A8），承载原/新负责人、
  生效日（含）、状态机、撤回与反向调拨回链、交接快照。
- 归属本身仍由 merchant_assignments 的 tstzrange 区间表达；本表只承载流程与分界。
- 分界口径：生效日（含）后未完成巡访任务与待办转新负责人；历史事实不回写。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c5a3f9b4d2e1"
down_revision: str | Sequence[str] | None = "b4f2e8a31c09"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "merchant_transfers",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("merchant_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("merchants.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("from_user_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("to_user_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("effective_date", sa.Date(), nullable=False),
        sa.Column("effective_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("effective_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False,
                  server_default=sa.text("'PENDING'")),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("withdrawn_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("withdrawn_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("withdraw_reason", sa.Text(), nullable=True),
        sa.Column("reversed_by_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("merchant_transfers.id", ondelete="RESTRICT"),
                  nullable=True),
        sa.Column("reverses_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("merchant_transfers.id", ondelete="RESTRICT"),
                  nullable=True),
        sa.Column("handover_json", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("dataset_type", sa.String(length=16), nullable=False),
        sa.Column("dataset_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('PENDING','SCHEDULED','EFFECTIVE','WITHDRAWN','SUPERSEDED')",
            name="merchant_transfers_status_check"),
        sa.CheckConstraint("from_user_id <> to_user_id",
                           name="merchant_transfers_distinct_users"),
        sa.CheckConstraint(
            "status <> 'EFFECTIVE' OR effective_at IS NOT NULL",
            name="merchant_transfers_effective_has_at"),
    )
    op.create_index("ix_merchant_transfers_merchant",
                    "merchant_transfers", ["merchant_id"])
    op.create_index("ix_merchant_transfers_from_user",
                    "merchant_transfers", ["from_user_id"])
    op.create_index("ix_merchant_transfers_to_user",
                    "merchant_transfers", ["to_user_id"])
    # 同一商家：同时只允许一张未完成（PENDING/SCHEDULED）调拨单，避免生效归属竞争
    op.create_index(
        "uq_merchant_transfers_one_open",
        "merchant_transfers",
        ["merchant_id", "dataset_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('PENDING','SCHEDULED')"),
    )


def downgrade() -> None:
    op.drop_index("uq_merchant_transfers_one_open", table_name="merchant_transfers")
    op.drop_index("ix_merchant_transfers_to_user", table_name="merchant_transfers")
    op.drop_index("ix_merchant_transfers_from_user", table_name="merchant_transfers")
    op.drop_index("ix_merchant_transfers_merchant", table_name="merchant_transfers")
    op.drop_table("merchant_transfers")
