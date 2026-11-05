"""t09 contract renewal apply

Revision ID: b7d4e2c9a130
Revises: c8d3f2a1e506
Create Date: 2026-10-19 00:00:00.000000

T09 合同续约申请：
- contracts 增加 renewed_from_contract_id（自引用：新续约合同 → 被续约的旧合同）；
- 新合同独立走 DRAFT → 申请 → 审批 → 生效，生效日必须晚于旧合同到期日，
  旧合同不改状态（到期自然失效），因此无需"先终止再重签"。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b7d4e2c9a130"
down_revision: str | None = "c8d3f2a1e506"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "contracts",
        sa.Column(
            "renewed_from_contract_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_contracts_renewed_from_contract_id",
        "contracts",
        ["renewed_from_contract_id"],
    )
    op.create_foreign_key(
        "fk_contracts_renewed_from_contract_id",
        "contracts",
        "contracts",
        ["renewed_from_contract_id"],
        ["id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_contracts_renewed_from_contract_id", "contracts", type_="foreignkey"
    )
    op.drop_index("ix_contracts_renewed_from_contract_id", table_name="contracts")
    op.drop_column("contracts", "renewed_from_contract_id")
