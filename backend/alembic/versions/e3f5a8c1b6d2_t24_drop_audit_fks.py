"""T24: drop residual users FK on audit/draft tables

Revision ID: e3f5a8c1b6d2
Revises: a1c4e7b9023f
Create Date: 2026-10-14 00:30:00.000000

安全审计表 actor_id / owner_user_id 与 users 解耦（对齐 idempotency_keys.actor_id
可空 UUID 口径）：用户删除 / 跨域造数不应被外键阻塞。

模型与建表迁移 a1c4e7b9023f 已去除外键，但在该迁移被原地修改前已 applied 的库里
仍残留物理约束，故在此显式删除。使用 IF EXISTS：对已无约束的干净库同样安全幂等。
"""

from collections.abc import Sequence

from alembic import op

revision: str = "e3f5a8c1b6d2"
down_revision: str | Sequence[str] | None = "a1c4e7b9023f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE mcp_confirmation_tokens "
        "DROP CONSTRAINT IF EXISTS mcp_confirmation_tokens_actor_id_fkey"
    )
    op.execute(
        "ALTER TABLE mcp_drafts "
        "DROP CONSTRAINT IF EXISTS mcp_drafts_owner_user_id_fkey"
    )


def downgrade() -> None:
    # 不恢复外键：审计/草稿身份列刻意与 users 解耦，downgrade 保持无约束。
    pass
