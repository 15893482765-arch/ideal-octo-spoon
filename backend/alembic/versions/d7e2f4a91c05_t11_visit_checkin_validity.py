"""T11 visit checkin, evidence and validity

Revision ID: d7e2f4a91c05
Revises: b3f1a9c7e2d4
Create Date: 2026-10-13

巡访任务来源/时间窗、打卡到离店/精度/设备与服务端时间、照片先传后绑、
修订追加、VisitValidityService 有效性裁决字段（PRD §4.6/§4.8）。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "d7e2f4a91c05"
down_revision: str | None = "b3f1a9c7e2d4"
branch_labels = None
depends_on = None

_VISIT_SOURCES = "('WEEKLY_SCHEDULE','AD_HOC','RECTIFICATION','SUPERVISION')"


def upgrade() -> None:
    # ---------- visit_tasks：来源 / 优先级 / 计划时间窗 ----------
    op.add_column(
        "visit_tasks",
        sa.Column(
            "window_start_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "visit_tasks",
        sa.Column("window_end_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "visit_tasks",
        sa.Column(
            "priority",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("2"),
        ),
    )
    op.add_column(
        "visit_tasks",
        sa.Column(
            "source",
            sa.String(length=20),
            nullable=False,
            server_default=sa.text("'WEEKLY_SCHEDULE'"),
        ),
    )
    op.create_check_constraint(
        "visit_tasks_source_check", "visit_tasks", sa.text(f"source IN {_VISIT_SOURCES}")
    )
    op.create_check_constraint(
        "visit_tasks_priority_range",
        "visit_tasks",
        sa.text("priority BETWEEN 1 AND 3"),
    )
    op.create_check_constraint(
        "visit_tasks_window_order",
        "visit_tasks",
        sa.text(
            "window_end_at IS NULL OR window_start_at IS NULL "
            "OR window_end_at >= window_start_at"
        ),
    )

    # ---------- visit_records：打卡 / 裁决字段 ----------
    # status 由 String(16) 扩到 String(20)（容纳 ABNORMAL_PENDING）
    op.alter_column(
        "visit_records",
        "status",
        existing_type=sa.String(length=16),
        type_=sa.String(length=20),
        existing_nullable=False,
        existing_server_default=sa.text("'SUBMITTED'"),
    )
    op.add_column(
        "visit_records",
        sa.Column("source", sa.String(length=20), nullable=False,
                  server_default=sa.text("'WEEKLY_SCHEDULE'")),
    )
    op.add_column(
        "visit_records",
        sa.Column("checkin_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "visit_records",
        sa.Column("checkout_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "visit_records",
        sa.Column("device_checkin_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "visit_records", sa.Column("accuracy_meters", sa.Integer(), nullable=True)
    )
    op.add_column(
        "visit_records", sa.Column("distance_meters", sa.Float(), nullable=True)
    )
    op.add_column(
        "visit_records",
        sa.Column(
            "photo_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.add_column(
        "visit_records",
        sa.Column(
            "validity",
            sa.String(length=20),
            nullable=False,
            server_default=sa.text("'ABNORMAL_PENDING'"),
        ),
    )
    op.add_column(
        "visit_records",
        sa.Column("judged_by_id", sa.UUID(), nullable=True),
    )
    op.add_column(
        "visit_records",
        sa.Column("judged_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "visit_records", sa.Column("judge_reason", sa.Text(), nullable=True)
    )
    op.create_foreign_key(
        "fk_visit_records_judged_by",
        "visit_records",
        "users",
        ["judged_by_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    # 放宽既有 status CHECK，纳入 ABNORMAL_PENDING
    op.drop_constraint("visit_records_status_check", "visit_records", type_="check")
    op.create_check_constraint(
        "visit_records_status_check",
        "visit_records",
        sa.text(
            "status IN ('DRAFT','SUBMITTED','RECOGNIZED','ABNORMAL_PENDING',"
            "'ABNORMAL','REJECTED')"
        ),
    )
    op.create_check_constraint(
        "visit_records_validity_check",
        "visit_records",
        sa.text("validity IN ('VALID','ABNORMAL_PENDING','INVALID')"),
    )
    op.create_check_constraint(
        "visit_records_accuracy_nonneg",
        "visit_records",
        sa.text("accuracy_meters IS NULL OR accuracy_meters >= 0"),
    )
    op.create_check_constraint(
        "visit_records_distance_nonneg",
        "visit_records",
        sa.text("distance_meters IS NULL OR distance_meters >= 0"),
    )
    op.create_check_constraint(
        "visit_records_time_order",
        "visit_records",
        sa.text(
            "checkout_at IS NULL OR checkin_at IS NULL "
            "OR checkout_at >= checkin_at"
        ),
    )
    op.create_index(
        "ix_visit_records_user_time",
        "visit_records",
        ["user_id", "checkin_at"],
    )
    # 一个任务至多一条正式记录（T03 起存在的 is_official 语义）
    op.create_index(
        "uq_visit_records_official_per_task",
        "visit_records",
        ["visit_task_id"],
        unique=True,
        postgresql_where=sa.text("is_official"),
    )
    op.create_unique_constraint(
        "uq_visit_records_idempotency_key", "visit_records", ["idempotency_key"]
    )

    # ---------- visit_photos：照片先上传后绑定（只增不改） ----------
    op.create_table(
        "visit_photos",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"),
                  nullable=False),
        sa.Column("visit_record_id", sa.UUID(), nullable=False),
        sa.Column("file_id", sa.UUID(), nullable=False),
        sa.Column("photo_kind", sa.String(length=16), nullable=False,
                  server_default=sa.text("'STOREFRONT'")),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("created_by_id", sa.UUID(), nullable=True),
        sa.Column("updated_by_id", sa.UUID(), nullable=True),
        sa.Column("dataset_type", sa.String(length=16),
                  server_default=sa.text("'PRODUCTION'"), nullable=False),
        sa.Column("dataset_id", sa.UUID(),
                  server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.CheckConstraint(
            "photo_kind IN ('STOREFRONT','FREEZER','OTHER')",
            name="visit_photos_kind_check",
        ),
        sa.ForeignKeyConstraint(["visit_record_id"], ["visit_records.id"],
                                ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["file_id"], ["file_objects.id"],
                                ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("visit_record_id", "file_id",
                            name="uq_visit_photos_record_file"),
    )
    op.create_index(
        "ix_visit_photos_record", "visit_photos", ["visit_record_id"]
    )

    # ---------- visit_revisions：补充说明追加 ----------
    op.create_table(
        "visit_revisions",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"),
                  nullable=False),
        sa.Column("visit_record_id", sa.UUID(), nullable=False),
        sa.Column("author_id", sa.UUID(), nullable=False),
        sa.Column("supplement", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("created_by_id", sa.UUID(), nullable=True),
        sa.Column("updated_by_id", sa.UUID(), nullable=True),
        sa.Column("dataset_type", sa.String(length=16),
                  server_default=sa.text("'PRODUCTION'"), nullable=False),
        sa.Column("dataset_id", sa.UUID(),
                  server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.ForeignKeyConstraint(["visit_record_id"], ["visit_records.id"],
                                ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["author_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_visit_revisions_record", "visit_revisions", ["visit_record_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_visit_revisions_record", table_name="visit_revisions")
    op.drop_table("visit_revisions")

    op.drop_index("ix_visit_photos_record", table_name="visit_photos")
    op.drop_table("visit_photos")

    op.drop_constraint(
        "uq_visit_records_idempotency_key", "visit_records", type_="unique"
    )
    op.drop_index("uq_visit_records_official_per_task", table_name="visit_records")
    op.drop_index("ix_visit_records_user_time", table_name="visit_records")
    op.drop_constraint(
        "visit_records_time_order", "visit_records", type_="check"
    )
    op.drop_constraint(
        "visit_records_distance_nonneg", "visit_records", type_="check"
    )
    op.drop_constraint(
        "visit_records_accuracy_nonneg", "visit_records", type_="check"
    )
    op.drop_constraint(
        "visit_records_validity_check", "visit_records", type_="check"
    )
    # 复原 T03 时代的 status CHECK（无 ABNORMAL_PENDING）
    op.drop_constraint("visit_records_status_check", "visit_records", type_="check")
    op.create_check_constraint(
        "visit_records_status_check",
        "visit_records",
        sa.text(
            "status IN ('DRAFT','SUBMITTED','RECOGNIZED','ABNORMAL','REJECTED')"
        ),
    )
    op.drop_constraint("fk_visit_records_judged_by", "visit_records",
                       type_="foreignkey")
    for col in (
        "judge_reason",
        "judged_at",
        "judged_by_id",
        "validity",
        "photo_count",
        "distance_meters",
        "accuracy_meters",
        "device_checkin_at",
        "checkout_at",
        "checkin_at",
        "source",
    ):
        op.drop_column("visit_records", col)
    op.alter_column(
        "visit_records",
        "status",
        existing_type=sa.String(length=20),
        type_=sa.String(length=16),
        existing_nullable=False,
        existing_server_default=sa.text("'SUBMITTED'"),
    )

    op.drop_constraint(
        "visit_tasks_window_order", "visit_tasks", type_="check"
    )
    op.drop_constraint(
        "visit_tasks_priority_range", "visit_tasks", type_="check"
    )
    op.drop_constraint(
        "visit_tasks_source_check", "visit_tasks", type_="check"
    )
    for col in ("source", "priority", "window_end_at", "window_start_at"):
        op.drop_column("visit_tasks", col)
