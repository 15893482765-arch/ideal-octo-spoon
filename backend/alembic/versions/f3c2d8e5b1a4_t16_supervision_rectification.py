"""T16: supervision shadow visits & five-state rectification loop

Revision ID: f3c2d8e5b1a4
Revises: e2b1c4d7a9f3
Create Date: 2026-10-13 00:00:00.000000

PRD §4.8 / R14 / R15 / A10：
- supervision_visits：每周跟访计划 + 现场记录（随访业务员/商家/照片/动作评分/
  陈列·服务·设备问题）。督导记录独立于业务员巡访，不写 visit_*，不替代不计覆盖率。
- rectifications：整改单五态 OPEN→IN_PROGRESS→SUBMITTED→VERIFIED→CLOSED；
  DB 层 CHECK 强制 verifier IS DISTINCT FROM submitter（验证人≠提交人）。
T03 预留的 supervisor_issues 表首版无 service 写入，保留不动。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f3c2d8e5b1a4"
down_revision: str | Sequence[str] | None = "e2b1c4d7a9f3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "supervision_visits",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("region_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("regions.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False,
                  server_default=sa.text("'PLANNED'")),
        sa.Column("supervisor_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("shadowed_user_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("merchant_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("merchants.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("planned_date", sa.Date(), nullable=False),
        sa.Column("visited_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("photo_count", sa.Integer(), nullable=False,
                  server_default=sa.text("0")),
        sa.Column("action_score", sa.Integer(), nullable=True),
        sa.Column("display_issue", sa.Text(), nullable=True),
        sa.Column("service_issue", sa.Text(), nullable=True),
        sa.Column("equipment_issue", sa.Text(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("dataset_type", sa.String(length=16), nullable=False,
                  server_default=sa.text("'PRODUCTION'")),
        sa.Column("dataset_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.CheckConstraint("status IN ('PLANNED','SUBMITTED')",
                           name="supervision_visits_status_check"),
        sa.CheckConstraint("action_score BETWEEN 0 AND 100",
                           name="supervision_visits_score_range"),
        sa.CheckConstraint("photo_count >= 0",
                           name="supervision_visits_photo_nonneg"),
    )
    op.create_index("ix_supervision_visits_region_week",
                    "supervision_visits", ["region_id", "planned_date"])
    op.create_index("ix_supervision_visits_supervisor",
                    "supervision_visits", ["supervisor_id"])
    op.create_index("ix_supervision_visits_rep",
                    "supervision_visits", ["shadowed_user_id"])
    op.create_index("ix_supervision_visits_merchant",
                    "supervision_visits", ["merchant_id"])

    op.create_table(
        "rectifications",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("region_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("regions.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("supervision_visit_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("supervision_visits.id", ondelete="RESTRICT"),
                  nullable=False),
        sa.Column("merchant_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("merchants.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("category", sa.String(length=16), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("owner_user_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("due_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False,
                  server_default=sa.text("'OPEN'")),
        sa.Column("submitter_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("submit_note", sa.Text(), nullable=True),
        sa.Column("verifier_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("verify_note", sa.Text(), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("dataset_type", sa.String(length=16), nullable=False,
                  server_default=sa.text("'PRODUCTION'")),
        sa.Column("dataset_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.CheckConstraint("status IN ('OPEN','IN_PROGRESS','SUBMITTED','VERIFIED','CLOSED')",
                           name="rectifications_status_check"),
        sa.CheckConstraint(
            "category IN ('DISPLAY','SERVICE','EQUIPMENT','OTHER')",
            name="rectifications_category_check",
        ),
        sa.CheckConstraint(
            "(verifier_id IS NULL) OR (verifier_id <> submitter_id)",
            name="rectifications_verifier_not_submitter",
        ),
    )
    op.create_index("ix_rectifications_visit",
                    "rectifications", ["supervision_visit_id"])
    op.create_index("ix_rectifications_owner_status",
                    "rectifications", ["owner_user_id", "status"])
    op.create_index("ix_rectifications_region", "rectifications", ["region_id"])


def downgrade() -> None:
    op.drop_index("ix_rectifications_region", table_name="rectifications")
    op.drop_index("ix_rectifications_owner_status", table_name="rectifications")
    op.drop_index("ix_rectifications_visit", table_name="rectifications")
    op.drop_table("rectifications")

    op.drop_index("ix_supervision_visits_merchant", table_name="supervision_visits")
    op.drop_index("ix_supervision_visits_rep", table_name="supervision_visits")
    op.drop_index("ix_supervision_visits_supervisor", table_name="supervision_visits")
    op.drop_index("ix_supervision_visits_region_week", table_name="supervision_visits")
    op.drop_table("supervision_visits")
