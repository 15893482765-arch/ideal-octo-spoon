"""T33: sales_confirmations 增加来源履约事件列（送达自动确认销量幂等）

Revision ID: e6f7a8b9c0d1
Revises: d5e6f7a8b9c0
Create Date: 2026-10-22 00:10:00.000000

- source_fulfillment_id：补货送达自动创建销量确认时回写履约事件 id
  （跨域弱引用，无 FK；手工/种子数据为 NULL）；
- 唯一约束：一条履约事件只产生一条销量确认（与履约幂等键防线叠加）。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e6f7a8b9c0d1"
down_revision: str | Sequence[str] | None = "d5e6f7a8b9c0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "sales_confirmations",
        sa.Column("source_fulfillment_id", postgresql.UUID(as_uuid=True),
                  nullable=True),
    )
    op.create_unique_constraint(
        "uq_sales_confirmations_fulfillment",
        "sales_confirmations",
        ["source_fulfillment_id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_sales_confirmations_fulfillment", "sales_confirmations", type_="unique"
    )
    op.drop_column("sales_confirmations", "source_fulfillment_id")
