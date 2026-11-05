"""AI 助手会话持久化：assistant_sessions + assistant_messages。

- assistant_sessions：按员工（user_id）隔离的会话，title 取首条用户消息，
  facts 保存每轮确定性解析的需求状态（最近工具/周期/检索词）；
- assistant_messages：会话内按 seq 排序的完整消息（user/assistant），
  assistant 消息的 meta JSONB 记录工具调用摘要，供继续对话时重建上下文。

Revision ID: d8f3b02e4a16
Revises: c7e2a91f3d05
Create Date: 2026-10-20
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "d8f3b02e4a16"
down_revision = "c7e2a91f3d05"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "assistant_sessions",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("title", sa.String(length=120), nullable=False, server_default=""),
        sa.Column(
            "facts",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "message_count", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column(
            "last_message_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "message_count >= 0", name="assistant_sessions_count_nonneg"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_assistant_sessions_user_recent",
        "assistant_sessions",
        ["user_id", "last_message_at"],
    )

    op.create_table(
        "assistant_messages",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "meta",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "role IN ('user','assistant')", name="assistant_messages_role_check"
        ),
        sa.ForeignKeyConstraint(
            ["session_id"], ["assistant_sessions.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_assistant_messages_session_seq",
        "assistant_messages",
        ["session_id", "seq"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_assistant_messages_session_seq", table_name="assistant_messages")
    op.drop_table("assistant_messages")
    op.drop_index("ix_assistant_sessions_user_recent", table_name="assistant_sessions")
    op.drop_table("assistant_sessions")
