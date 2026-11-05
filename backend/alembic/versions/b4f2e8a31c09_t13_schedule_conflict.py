"""T13: weekly schedule plan batches/entries + generic resource reservations

Revision ID: b4f2e8a31c09
Revises: a8c3d91e5f07
Create Date: 2026-10-13 00:00:00.000000

- schedule_plan_batches：周排班批次（DRAFT/PUBLISHED）；
- schedule_plan_entries：批次条目（发布后回链 VisitTask/ResourceReservation）；
- resource_reservations：人员 [start_at,end_at) 半开区间通用占用，
  以 tstzrange GiST 排他约束兜底同一资源时间重叠（btree_gist 已在 T08 启用）。
冲突业务口径由 ScheduleConflictService 定义；本约束只做并发兜底。
旧 slot 粒度 schedules 表保留不动（无 service/router 使用）。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b4f2e8a31c09"
down_revision: str | Sequence[str] | None = "a8c3d91e5f07"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # btree_gist 已在 T08（e8d1c6a4f3b2）CREATE EXTENSION，提供 UUID 等值操作族
    op.create_table(
        "schedule_plan_batches",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("week_start_date", sa.Date(), nullable=False),
        sa.Column("week_range", postgresql.TSTZRANGE(), nullable=False),
        sa.Column("region_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("regions.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False,
                  server_default=sa.text("'DRAFT'")),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("idempotency_key", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("dataset_type", sa.String(length=16), nullable=False),
        sa.Column("dataset_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.CheckConstraint("status IN ('DRAFT','PUBLISHED','CANCELLED','COMPLETED')",
                           name="schedule_plan_batches_status_check"),
        sa.CheckConstraint("lower(week_range) IS NOT NULL",
                           name="schedule_plan_batches_week_lower_notnull"),
    )
    op.create_index("ix_schedule_plan_batches_region_week",
                    "schedule_plan_batches", ["region_id", "week_start_date"])
    # 同一数据范围 + 同片区 + 同业务周至多一个已发布批次（幂等/防并发重复发布）
    op.create_index(
        "uq_schedule_plan_batches_published_week",
        "schedule_plan_batches",
        ["region_id", "week_start_date", "dataset_id"],
        unique=True,
        postgresql_where=sa.text("status = 'PUBLISHED'"),
    )

    op.create_table(
        "schedule_plan_entries",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("batch_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("schedule_plan_batches.id", ondelete="RESTRICT"),
                  nullable=False),
        sa.Column("merchant_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("merchants.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("assigned_user_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("planned_date", sa.Date(), nullable=False),
        sa.Column("start_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("end_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("priority", sa.Integer(), nullable=False, server_default=sa.text("2")),
        sa.Column("visit_task_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("reservation_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("dataset_type", sa.String(length=16), nullable=False),
        sa.Column("dataset_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.CheckConstraint(
            "end_at IS NULL OR start_at IS NULL OR end_at > start_at",
            name="schedule_plan_entries_window_order",
        ),
        sa.CheckConstraint("priority BETWEEN 1 AND 3",
                           name="schedule_plan_entries_priority_range"),
        sa.UniqueConstraint("batch_id", "merchant_id",
                            name="uq_schedule_plan_entries_batch_merchant"),
    )
    op.create_index("ix_schedule_plan_entries_batch",
                    "schedule_plan_entries", ["batch_id"])
    op.create_index("ix_schedule_plan_entries_merchant",
                    "schedule_plan_entries", ["merchant_id"])
    op.create_index("ix_schedule_plan_entries_user",
                    "schedule_plan_entries", ["assigned_user_id"])

    op.create_table(
        "resource_reservations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("resource_user_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("occupied_range", postgresql.TSTZRANGE(), nullable=False),
        sa.Column("source_type", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False,
                  server_default=sa.text("'ACTIVE'")),
        sa.Column("merchant_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("merchants.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("source_object_type", sa.String(length=32), nullable=False),
        sa.Column("source_object_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("dataset_type", sa.String(length=16), nullable=False),
        sa.Column("dataset_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.CheckConstraint("source_type IN ('SCHEDULE','CAMPAIGN')",
                           name="resource_reservations_source_type_check"),
        sa.CheckConstraint("status IN ('ACTIVE','RELEASED')",
                           name="resource_reservations_status_check"),
        sa.CheckConstraint("end_at > start_at",
                           name="resource_reservations_window_order"),
        postgresql.ExcludeConstraint(
            ("resource_user_id", "="),
            ("occupied_range", "&&"),
            using="gist",
            where=sa.text("status = 'ACTIVE'"),
            name="excl_resource_reservations_active_range",
        ),
    )
    op.create_index("ix_resource_reservations_resource_time",
                    "resource_reservations", ["resource_user_id", "start_at", "end_at"])
    op.create_index("ix_resource_reservations_merchant",
                    "resource_reservations", ["merchant_id"])


def downgrade() -> None:
    op.drop_index("ix_resource_reservations_merchant", table_name="resource_reservations")
    op.drop_index("ix_resource_reservations_resource_time",
                  table_name="resource_reservations")
    op.drop_table("resource_reservations")

    op.drop_index("ix_schedule_plan_entries_user", table_name="schedule_plan_entries")
    op.drop_index("ix_schedule_plan_entries_merchant", table_name="schedule_plan_entries")
    op.drop_index("ix_schedule_plan_entries_batch", table_name="schedule_plan_entries")
    op.drop_table("schedule_plan_entries")

    op.drop_index("uq_schedule_plan_batches_published_week",
                  table_name="schedule_plan_batches")
    op.drop_index("ix_schedule_plan_batches_region_week",
                  table_name="schedule_plan_batches")
    op.drop_table("schedule_plan_batches")
