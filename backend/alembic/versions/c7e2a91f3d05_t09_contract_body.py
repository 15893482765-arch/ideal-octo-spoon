"""T09 合同纸质正文：店长姓名 + 模板渲染正文（创建草稿时一并生成）。

- contracts：新增 store_manager_name（乙方签署人）、body_text（A4 合同正文）；
- contract_versions：同步新增两列，正文随版本快照固化，历史版本不可覆盖。

Revision ID: c7e2a91f3d05
Revises: c1a5f7e93b28
Create Date: 2026-10-19
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c7e2a91f3d05"
down_revision = "c1a5f7e93b28"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "contracts", sa.Column("store_manager_name", sa.String(length=64), nullable=True)
    )
    op.add_column("contracts", sa.Column("body_text", sa.Text(), nullable=True))
    op.add_column(
        "contract_versions",
        sa.Column("store_manager_name", sa.String(length=64), nullable=True),
    )
    op.add_column("contract_versions", sa.Column("body_text", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("contract_versions", "body_text")
    op.drop_column("contract_versions", "store_manager_name")
    op.drop_column("contracts", "body_text")
    op.drop_column("contracts", "store_manager_name")
