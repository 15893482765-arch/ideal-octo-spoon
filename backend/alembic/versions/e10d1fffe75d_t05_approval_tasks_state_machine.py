"""t05_approval_tasks_state_machine

T05：审批 / 任务 / 状态机内核
- approval_requests：增加 action、request_payload 列；PENDING 部分唯一索引；
  状态 CHECK 扩展 RETURNED
- task_items：增加 requires_related_document / completion_source / closed_at /
  closed_by_id；幂等部分唯一索引；新增 task_kind / completion_source CHECK
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e10d1fffe75d"
down_revision: str | None = "4c97893f84d4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- approval_requests ---
    op.add_column(
        "approval_requests",
        sa.Column(
            "action",
            sa.String(length=48),
            server_default=sa.text("''"),
            nullable=False,
        ),
    )
    op.add_column(
        "approval_requests",
        sa.Column(
            "request_payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
    op.drop_constraint("approval_status_check", "approval_requests", type_="check")
    op.create_check_constraint(
        "approval_status_check",
        "approval_requests",
        "status IN ('PENDING','APPROVED','REJECTED','RETURNED','WITHDRAWN')",
    )
    op.create_index(
        "uq_approvals_pending_target",
        "approval_requests",
        ["target_object_type", "target_object_id", "action"],
        unique=True,
        postgresql_where=sa.text("status = 'PENDING'"),
    )

    # --- task_items ---
    op.add_column(
        "task_items",
        sa.Column(
            "requires_related_document",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )
    op.add_column(
        "task_items",
        sa.Column("completion_source", sa.String(length=24), nullable=True),
    )
    op.add_column(
        "task_items",
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "task_items",
        sa.Column("closed_by_id", sa.UUID(), nullable=True),
    )
    op.create_index(
        "uq_task_items_idempotent",
        "task_items",
        ["related_object_type", "related_object_id", "task_kind"],
        unique=True,
        postgresql_where=sa.text("status IN ('OPEN','IN_PROGRESS')"),
    )
    op.create_foreign_key(
        "task_items_closed_by_id_fkey",
        "task_items",
        "users",
        ["closed_by_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "task_items_task_kind_check",
        "task_items",
        (
            "task_kind IN ('VISIT_PLANNED','CONTRACT_RENEWAL_REVIEW','FREEZER_RECALL',"
            "'FREEZER_ACCEPTANCE','FREEZER_DEPOSIT_REFUND','REPLENISHMENT_REVIEW',"
            "'PROBLEM_RECTIFICATION','SUPERVISION_RECTIFICATION','PROBLEM_RECTIFICATION_VERIFY',"
            "'CONTRACT_RENEWAL_VERIFY','CONTRACT_EXPIRING_RENEWAL_EVAL','OTHER')"
        ),
    )
    op.create_check_constraint(
        "task_items_completion_source_check",
        "task_items",
        "completion_source IN ('DIRECT','RELATED_DOCUMENT','SYSTEM','OTHER') OR completion_source IS NULL",
    )


def downgrade() -> None:
    op.drop_constraint("task_items_completion_source_check", "task_items", type_="check")
    op.drop_constraint("task_items_task_kind_check", "task_items", type_="check")
    op.drop_constraint(
        "task_items_closed_by_id_fkey", "task_items", type_="foreignkey"
    )
    op.drop_index(
        "uq_task_items_idempotent",
        table_name="task_items",
        postgresql_where=sa.text("status IN ('OPEN','IN_PROGRESS')"),
    )
    op.drop_column("task_items", "closed_by_id")
    op.drop_column("task_items", "closed_at")
    op.drop_column("task_items", "completion_source")
    op.drop_column("task_items", "requires_related_document")
    op.drop_index(
        "uq_approvals_pending_target",
        table_name="approval_requests",
        postgresql_where=sa.text("status = 'PENDING'"),
    )
    op.drop_constraint("approval_status_check", "approval_requests", type_="check")
    op.create_check_constraint(
        "approval_status_check",
        "approval_requests",
        "status IN ('PENDING','APPROVED','REJECTED','WITHDRAWN')",
    )
    op.drop_column("approval_requests", "request_payload")
    op.drop_column("approval_requests", "action")
