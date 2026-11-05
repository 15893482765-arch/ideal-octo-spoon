"""T13: weekly recurring schedule templates

Revision ID: c1a5f7e93b28
Revises: b7d4e2c9a130
Create Date: 2026-10-19 00:00:00.000000

排班从"按日期排一周"改为"每周循环模板"：
- schedule_templates：排给谁 + 商家 + 周几（1-7）+ 时段，不绑定日期；
- 同一片区同一商家只出现一次（发布批次按 batch+merchant 唯一）；
- 同一天可放多家不同商家；生成任务时按目标业务周把 weekday 换算成日期，
  仍复用 T13 的冲突裁决与全有或全无发布。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c1a5f7e93b28"
down_revision: str | Sequence[str] | None = "b7d4e2c9a130"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "schedule_templates",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "region_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("regions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "merchant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("merchants.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "assigned_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("weekday", sa.Integer(), nullable=False),
        sa.Column("start_time", sa.Time(), nullable=False),
        sa.Column("end_time", sa.Time(), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False, server_default=sa.text("2")),
        sa.Column(
            "is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("dataset_type", sa.String(length=16), nullable=False),
        sa.Column("dataset_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.CheckConstraint(
            "weekday BETWEEN 1 AND 7", name="schedule_templates_weekday_check"
        ),
        sa.CheckConstraint(
            "end_time > start_time", name="schedule_templates_time_order"
        ),
        sa.CheckConstraint(
            "priority BETWEEN 1 AND 3", name="schedule_templates_priority_range"
        ),
        sa.UniqueConstraint(
            "region_id", "merchant_id", name="uq_schedule_templates_region_merchant"
        ),
    )
    op.create_index(
        "ix_schedule_templates_region_weekday",
        "schedule_templates",
        ["region_id", "weekday"],
    )
    op.create_index(
        "ix_schedule_templates_user", "schedule_templates", ["assigned_user_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_schedule_templates_user", table_name="schedule_templates")
    op.drop_index(
        "ix_schedule_templates_region_weekday", table_name="schedule_templates"
    )
    op.drop_table("schedule_templates")
