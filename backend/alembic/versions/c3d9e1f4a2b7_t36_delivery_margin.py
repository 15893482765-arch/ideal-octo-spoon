"""T36：销量确认固化成本与毛利（建议零售价 − 参考进价）

Revision ID: c3d9e1f4a2b7
Revises: f7a8b9c0d1e2
Create Date: 2026-10-29

`sales_confirmations` 增加 `cost_fen` / `profit_fen`：
- 成本 = 送达箱数 × SKU 参考进价（`skus.purchase_price_fen`）；
- 毛利 = 收入（送达箱数 × 建议零售价）− 成本。
历史行按同一口径回填（进价缺失视为 0，毛利等于收入），不删除任何列。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c3d9e1f4a2b7"
down_revision: str | None = "f7a8b9c0d1e2"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "sales_confirmations",
        sa.Column("cost_fen", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
    )
    op.add_column(
        "sales_confirmations",
        sa.Column("profit_fen", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
    )
    op.create_check_constraint(
        "sales_confirmations_cost_nonneg",
        "sales_confirmations",
        "cost_fen >= 0",
    )
    # 历史行回填：成本 = 箱数 × 参考进价；毛利 = 收入 − 成本
    op.execute(
        sa.text(
            """
            UPDATE sales_confirmations AS sc
            SET cost_fen = COALESCE(
                    ROUND(sc.confirmed_boxes * COALESCE(s.purchase_price_fen, 0)), 0
                )::bigint,
                profit_fen = sc.amount_fen - COALESCE(
                    ROUND(sc.confirmed_boxes * COALESCE(s.purchase_price_fen, 0)), 0
                )::bigint
            FROM skus AS s
            WHERE s.id = sc.sku_id
            """
        )
    )


def downgrade() -> None:
    op.drop_constraint(
        "sales_confirmations_cost_nonneg", "sales_confirmations", type_="check"
    )
    op.drop_column("sales_confirmations", "profit_fen")
    op.drop_column("sales_confirmations", "cost_fen")
