"""T27: notification_reads (per-user read state for derived notifications)

Revision ID: a7c2e9f4b105
Revises: f4a6b9d2c7e3
Create Date: 2026-10-15 09:00:00.000000

PRD §4.2 通知中心：通知由任务/待审批/风险预警实时聚合（派生视图），
已读状态独立存放，标记已读不得改变来源对象状态。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a7c2e9f4b105"
down_revision: str | Sequence[str] | None = "f4a6b9d2c7e3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "notification_reads",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("notification_key", sa.String(length=96), nullable=False),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.CheckConstraint(
            "char_length(notification_key) BETWEEN 1 AND 96",
            name="notification_read_key_len_check",
        ),
        sa.UniqueConstraint("user_id", "notification_key",
                            name="uq_notification_read_user_key"),
    )
    op.create_index("ix_notification_read_user", "notification_reads", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_notification_read_user", table_name="notification_reads")
    op.drop_table("notification_reads")
