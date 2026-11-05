"""T24: mcp confirmation tokens (structured write confirmation)

Revision ID: a1c4e7b9023f
Revises: f9d8a4b6c2e7
Create Date: 2026-10-14 00:00:00.000000

PRD §5.2–§5.4 执行预览 + 结构化确认：
- mcp_confirmation_tokens：写工具两阶段提交的一次性确认令牌；
- 绑定 actor / session / tool / params_hash / idempotency_key / 有效期；
- 只存令牌与参数的 SHA-256 摘要，不存令牌明文；
- (tool_name, token_hash) 唯一，消费置 consumed_at，保证一次性。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a1c4e7b9023f"
down_revision: str | Sequence[str] | None = "f9d8a4b6c2e7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "mcp_confirmation_tokens",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("tool_name", sa.String(length=64), nullable=False),
        sa.Column("actor_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("session_id", sa.String(length=128), nullable=False,
                  server_default=sa.text("''")),
        sa.Column("params_hash", sa.CHAR(length=64), nullable=False),
        sa.Column("token_hash", sa.CHAR(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("preview", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False,
                  server_default=sa.text("'ISSUED'")),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("request_id", sa.String(length=64), nullable=True),
        sa.CheckConstraint("status IN ('ISSUED','CONSUMED')",
                           name="mcp_confirmation_tokens_status_check"),
        sa.UniqueConstraint("tool_name", "token_hash",
                            name="uq_mcp_confirm_tool_token"),
    )
    op.create_index("ix_mcp_confirm_actor", "mcp_confirmation_tokens", ["actor_id"])
    op.create_index("ix_mcp_confirm_expires", "mcp_confirmation_tokens", ["expires_at"])
    op.create_index("ix_mcp_confirm_idem", "mcp_confirmation_tokens", ["idempotency_key"])

    # T24 AI 草稿暂存（6 类草稿，与正式业务表隔离，不进正式指标）
    op.create_table(
        "mcp_drafts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("kind", sa.String(length=24), nullable=False),
        sa.Column("owner_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("session_id", sa.String(length=128), nullable=False,
                  server_default=sa.text("''")),
        sa.Column("merchant_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False,
                  server_default=sa.text("'OPEN'")),
        sa.Column("idempotency_key", sa.String(length=128), nullable=True),
        sa.Column("materialized_type", sa.String(length=48), nullable=True),
        sa.Column("materialized_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("materialized_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("discarded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("params_hash", sa.CHAR(length=64), nullable=True),
        sa.CheckConstraint(
            "kind IN ('merchant','visit','replenishment','contract','campaign',"
            "'supervision')", name="mcp_drafts_kind_check"),
        sa.CheckConstraint("status IN ('OPEN','MATERIALIZED','DISCARDED')",
                           name="mcp_drafts_status_check"),
        sa.UniqueConstraint("idempotency_key", name="uq_mcp_drafts_idem"),
    )
    op.create_index("ix_mcp_drafts_owner_kind", "mcp_drafts",
                    ["owner_user_id", "kind", "status"])
    op.create_index("ix_mcp_drafts_session", "mcp_drafts", ["session_id"])
    op.create_index("ix_mcp_drafts_merchant", "mcp_drafts", ["merchant_id"])


def downgrade() -> None:
    op.drop_index("ix_mcp_drafts_merchant", table_name="mcp_drafts")
    op.drop_index("ix_mcp_drafts_session", table_name="mcp_drafts")
    op.drop_index("ix_mcp_drafts_owner_kind", table_name="mcp_drafts")
    op.drop_table("mcp_drafts")

    op.drop_index("ix_mcp_confirm_idem", table_name="mcp_confirmation_tokens")
    op.drop_index("ix_mcp_confirm_expires", table_name="mcp_confirmation_tokens")
    op.drop_index("ix_mcp_confirm_actor", table_name="mcp_confirmation_tokens")
    op.drop_table("mcp_confirmation_tokens")
