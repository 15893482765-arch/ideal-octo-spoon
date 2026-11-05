"""t08 merchant center: assignment ranges + grade changes + profile columns

Revision ID: f2d9c4a71b30
Revises: c4a1e7b90210
Create Date: 2026-10-12 00:00:00.000000

T08 商家中心与责任归属：
- merchants 增加主联系人姓名、营业时间档案列
- merchant_assignments 责任归属生效区间（主责任排他约束，需 btree_gist）
- merchant_grade_changes 等级调整历史（只影响生效日之后，不回溯）
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f2d9c4a71b30"
down_revision: str | None = "c4a1e7b90210"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "merchants",
        sa.Column("primary_contact_name", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "merchants",
        sa.Column("business_hours", sa.String(length=128), nullable=True),
    )

    # btree_gist 让 GiST 支持 UUID 等值，供主责任区间排他约束使用
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")

    op.create_table(
        "merchant_assignments",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("merchant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("owner_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("assignment_type", sa.String(length=16), nullable=False),
        sa.Column("valid_range", postgresql.TSTZRANGE(), nullable=False),
        sa.Column("reason", sa.String(length=255), nullable=True),
        sa.Column(
            "dataset_type",
            sa.String(length=16),
            server_default=sa.text("'PRODUCTION'"),
            nullable=False,
        ),
        sa.Column("dataset_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.ForeignKeyConstraint(["merchant_id"], ["merchants.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.CheckConstraint(
            "lower(valid_range) IS NOT NULL",
            name="merch_assign_range_lower_notnull",
        ),
        sa.CheckConstraint(
            "assignment_type IN ('PRIMARY','TEMPORARY')",
            name="merch_assign_type_check",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_merch_assign_merchant", "merchant_assignments", ["merchant_id"]
    )
    op.create_index(
        "ix_merch_assign_owner", "merchant_assignments", ["owner_user_id"]
    )
    op.execute(
        """
        CREATE INDEX ix_merch_assign_range ON merchant_assignments
        USING gist (valid_range)
        """
    )
    # 同一商家主责任区间互斥；临时授权可并行（WHERE 子句排除）
    op.execute(
        """
        ALTER TABLE merchant_assignments
        ADD CONSTRAINT excl_primary_assignment_range
        EXCLUDE USING gist (
            merchant_id WITH =,
            valid_range WITH &&
        ) WHERE (assignment_type = 'PRIMARY')
        """
    )

    op.create_table(
        "merchant_grade_changes",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("merchant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("old_grade", sa.String(length=1), nullable=True),
        sa.Column("new_grade", sa.String(length=1), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("reason", sa.String(length=255), nullable=False),
        sa.Column(
            "dataset_type",
            sa.String(length=16),
            server_default=sa.text("'PRODUCTION'"),
            nullable=False,
        ),
        sa.Column("dataset_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.ForeignKeyConstraint(["merchant_id"], ["merchants.id"], ondelete="RESTRICT"),
        sa.CheckConstraint(
            "new_grade IN ('A','B','C')", name="merch_grade_change_new_check"
        ),
        sa.CheckConstraint(
            "old_grade IN ('A','B','C') OR old_grade IS NULL",
            name="merch_grade_change_old_check",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_merch_grade_change_merchant",
        "merchant_grade_changes",
        ["merchant_id"],
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_merch_grade_change_once
        ON merchant_grade_changes (merchant_id, effective_from, new_grade)
        """
    )


def downgrade() -> None:
    op.drop_table("merchant_grade_changes")
    op.drop_table("merchant_assignments")
    op.drop_column("merchants", "business_hours")
    op.drop_column("merchants", "primary_contact_name")
