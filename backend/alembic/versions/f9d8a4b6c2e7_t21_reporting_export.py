"""T21: reporting export jobs (metrics/risk/report)

Revision ID: f9d8a4b6c2e7
Revises: e8c7f3a5b1d6
Create Date: 2026-10-14 00:00:00.000000

PRD §4.10 经营报表 / §5.3 report.export_request / §9.4 导出 / R34：
- export_jobs 异步导出任务（Excel/CSV/PDF）；scope CHECK 强制 PRODUCTION；
- 记录导出人/角色/条件/行级范围快照/文件对象键/有效期（R28）；
- 任务只增不改（成功/失败均保留）。
风险项 risk_items 表在 T03 已建，本卡不重复建表（RiskService 复用）。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f9d8a4b6c2e7"
down_revision: str | Sequence[str] | None = "e8c7f3a5b1d6"
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
    # T21：放行新任务类型 RISK_HANDLING（风险→片区经理待办，PRD §4.11）
    op.drop_constraint("task_items_task_kind_check", "task_items", type_="check")
    op.create_check_constraint(
        "task_items_task_kind_check",
        "task_items",
        "task_kind IN ('VISIT_PLANNED','CONTRACT_RENEWAL_REVIEW','FREEZER_RECALL',"
        "'FREEZER_ACCEPTANCE','FREEZER_DEPOSIT_REFUND','REPLENISHMENT_REVIEW',"
        "'PROBLEM_RECTIFICATION','SUPERVISION_RECTIFICATION',"
        "'PROBLEM_RECTIFICATION_VERIFY','CONTRACT_RENEWAL_VERIFY',"
        "'CONTRACT_EXPIRING_RENEWAL_EVAL','CAMPAIGN_SAMPLE_RETURN',"
        "'RISK_HANDLING','OTHER')",
    )

    op.create_table(
        "export_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("job_no", sa.String(length=32), nullable=False, unique=True),
        sa.Column("report_key", sa.String(length=32), nullable=False),
        sa.Column("format", sa.String(length=8), nullable=False),
        sa.Column("filters_json", postgresql.JSONB(astext_type=sa.Text()),
                  nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("columns_json", postgresql.JSONB(astext_type=sa.Text()),
                  nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("scope", sa.String(length=16), nullable=False,
                  server_default=sa.text("'PRODUCTION'")),
        sa.Column("status", sa.String(length=16), nullable=False,
                  server_default=sa.text("'QUEUED'")),
        sa.Column("requested_by_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("requested_role_code", sa.String(length=32), nullable=False),
        sa.Column("row_scope", sa.String(length=24), nullable=False),
        sa.Column("object_key", sa.String(length=255), nullable=True),
        sa.Column("content_type", sa.String(length=128), nullable=True),
        sa.Column("row_count", sa.Integer(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        *_audit_dataset_cols(),
        sa.CheckConstraint("format IN ('XLSX','CSV','PDF')",
                           name="export_jobs_format_check"),
        sa.CheckConstraint("status IN ('QUEUED','RUNNING','SUCCEEDED','FAILED')",
                           name="export_jobs_status_check"),
        sa.CheckConstraint("scope = 'PRODUCTION'",
                           name="export_jobs_scope_production_only"),
        sa.CheckConstraint("row_count IS NULL OR row_count >= 0",
                           name="export_jobs_row_count_nonneg"),
    )
    op.create_index("ix_export_jobs_requester", "export_jobs",
                    ["requested_by_id", "created_at"])
    op.create_index("ix_export_jobs_status", "export_jobs", ["status"])


def downgrade() -> None:
    op.drop_index("ix_export_jobs_status", table_name="export_jobs")
    op.drop_index("ix_export_jobs_requester", table_name="export_jobs")
    op.drop_table("export_jobs")
    # 还原任务类型白名单（移除 RISK_HANDLING）
    op.drop_constraint("task_items_task_kind_check", "task_items", type_="check")
    op.create_check_constraint(
        "task_items_task_kind_check",
        "task_items",
        "task_kind IN ('VISIT_PLANNED','CONTRACT_RENEWAL_REVIEW','FREEZER_RECALL',"
        "'FREEZER_ACCEPTANCE','FREEZER_DEPOSIT_REFUND','REPLENISHMENT_REVIEW',"
        "'PROBLEM_RECTIFICATION','SUPERVISION_RECTIFICATION',"
        "'PROBLEM_RECTIFICATION_VERIFY','CONTRACT_RENEWAL_VERIFY',"
        "'CONTRACT_EXPIRING_RENEWAL_EVAL','CAMPAIGN_SAMPLE_RETURN','OTHER')",
    )
