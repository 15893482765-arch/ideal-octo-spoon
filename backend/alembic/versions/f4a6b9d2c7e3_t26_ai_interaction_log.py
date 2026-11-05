"""T26: ai_interaction_logs (AI audit, redaction, degradation)

Revision ID: f4a6b9d2c7e3
Revises: e3f5a8c1b6d2
Create Date: 2026-10-14 01:10:00.000000

PRD §5.4-6：所有 AI 调用写 ai_interaction_logs——脱敏问题、意图、允许工具、
数据范围、参数摘要、结果引用、确认引用、模型、耗时、错误。原始问题默认不落库；
不记密码/完整电话/精确坐标。user_id 刻意不加 users 外键（审计在用户停用后仍保留）。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f4a6b9d2c7e3"
down_revision: str | Sequence[str] | None = "e3f5a8c1b6d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ai_interaction_logs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("role_code", sa.String(length=32), nullable=True),
        sa.Column("session_id", sa.String(length=64), nullable=True),
        sa.Column("request_id", sa.String(length=64), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("intent", sa.String(length=32), nullable=False),
        sa.Column("allowed_tools", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "scope_json", postgresql.JSONB(astext_type=sa.Text()),
            nullable=False, server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("prompt_redacted", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "params_summary_json", postgresql.JSONB(astext_type=sa.Text()),
            nullable=False, server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("result_ref", sa.String(length=128), nullable=True),
        sa.Column("confirm_ref", sa.String(length=128), nullable=True),
        sa.Column("model", sa.String(length=64), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("degraded", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("degraded_reason", sa.String(length=64), nullable=True),
        sa.Column("injection_blocked", sa.Boolean(), nullable=False,
                  server_default=sa.text("false")),
        sa.Column(
            "blocked_directives_json", postgresql.JSONB(astext_type=sa.Text()),
            nullable=False, server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="OK"),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "intent IN ('VISIT_COMPOSE','SCHEDULE_SUGGEST','BUSINESS_INSIGHT',"
            "'REPLENISHMENT_AGGREGATE','QA','OTHER')",
            name="ai_interaction_logs_intent_check",
        ),
        sa.CheckConstraint(
            "status IN ('OK','DEGRADED','REFUSED','ERROR')",
            name="ai_interaction_logs_status_check",
        ),
        sa.CheckConstraint("latency_ms >= 0", name="ai_interaction_logs_latency_nonneg"),
    )
    op.create_index("ix_ai_interaction_user_time", "ai_interaction_logs",
                    ["user_id", "occurred_at"])
    op.create_index("ix_ai_interaction_intent_time", "ai_interaction_logs",
                    ["intent", "occurred_at"])
    op.create_index("ix_ai_interaction_request", "ai_interaction_logs", ["request_id"])


def downgrade() -> None:
    op.drop_index("ix_ai_interaction_request", table_name="ai_interaction_logs")
    op.drop_index("ix_ai_interaction_intent_time", table_name="ai_interaction_logs")
    op.drop_index("ix_ai_interaction_user_time", table_name="ai_interaction_logs")
    op.drop_table("ai_interaction_logs")
