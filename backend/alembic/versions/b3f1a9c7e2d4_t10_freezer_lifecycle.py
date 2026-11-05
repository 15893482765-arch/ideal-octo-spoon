"""T10 freezer lifecycle: v1.2 states, documents, deposit flows

Revision ID: b3f1a9c7e2d4
Revises: a91b2e6c4d80
Create Date: 2026-10-13

PRD §4.5 / R11 / R12 / R13 / R37：
- 冰柜状态机扩展为 IN_STOCK/RESERVED/DEPLOYED/PENDING_PICKUP/
  PENDING_ACCEPTANCE/REPAIRING/SCRAPPED（保留 IN_TRANSIT/UNDER_MAINTENANCE 旧值）；
- 金额列加宽为 BIGINT（分）；新增最近投放/撤回时间、维修次数与商家绑定 CHECK；
- 流水新增单号/合同/迁移前后状态/发生时间/凭证附件列与押金关联约束。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b3f1a9c7e2d4"
down_revision: str | None = "a91b2e6c4d80"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NEW_STATUS_CHECK = (
    "status IN ('IN_STOCK','RESERVED','DEPLOYED','PENDING_PICKUP',"
    "'PENDING_ACCEPTANCE','REPAIRING','SCRAPPED','IN_TRANSIT','UNDER_MAINTENANCE')"
)
_OLD_STATUS_CHECK = (
    "status IN ('IN_STOCK','IN_TRANSIT','DEPLOYED','UNDER_MAINTENANCE','SCRAPPED')"
)
_NEW_MOVEMENT_CHECK = (
    "movement_type IN ('PURCHASE','DEPLOYMENT_ORDER','DEPLOY','RECALL','PICKUP',"
    "'ACCEPT','REPAIR_START','REPAIR_END','SCRAP',"
    "'DEPOSIT_COLLECTED','DEPOSIT_REFUNDED')"
)
_OLD_MOVEMENT_CHECK = (
    "movement_type IN ('PURCHASE','DEPLOY','RECALL','REPAIR_START','REPAIR_END',"
    "'SCRAP','DEPOSIT_COLLECTED','DEPOSIT_REFUNDED')"
)


def upgrade() -> None:
    # ---- freezer_assets：金额 BIGINT、台账字段、状态/绑定约束 ----
    op.alter_column(
        "freezer_assets",
        "purchase_price_fen",
        existing_type=sa.Integer(),
        type_=sa.BigInteger(),
        existing_nullable=False,
        existing_server_default=sa.text("0"),
    )
    op.alter_column(
        "freezer_assets",
        "deposit_fen",
        existing_type=sa.Integer(),
        type_=sa.BigInteger(),
        existing_nullable=True,
    )
    op.add_column(
        "freezer_assets",
        sa.Column(
            "last_deployed_at", sa.DateTime(timezone=True), nullable=True
        ),
    )
    op.add_column(
        "freezer_assets",
        sa.Column(
            "last_recalled_at", sa.DateTime(timezone=True), nullable=True
        ),
    )
    op.add_column(
        "freezer_assets",
        sa.Column(
            "repair_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.drop_constraint(
        "freezer_assets_status_check", "freezer_assets", type_="check"
    )
    op.create_check_constraint(
        "freezer_assets_status_check", "freezer_assets", _NEW_STATUS_CHECK
    )
    op.create_check_constraint(
        "freezer_assets_repair_count_nonneg",
        "freezer_assets",
        "repair_count >= 0",
    )
    op.create_check_constraint(
        "freezer_assets_merchant_binding_check",
        "freezer_assets",
        "status IN ('DEPLOYED','PENDING_PICKUP') = (current_merchant_id IS NOT NULL)",
    )

    # ---- freezer_movements：单据化 + 押金关联约束 ----
    op.alter_column(
        "freezer_movements",
        "amount_fen",
        existing_type=sa.Integer(),
        type_=sa.BigInteger(),
        existing_nullable=True,
    )
    op.add_column(
        "freezer_movements",
        sa.Column("doc_no", sa.String(length=40), nullable=True),
    )
    op.add_column(
        "freezer_movements",
        sa.Column("contract_id", sa.UUID(), nullable=True),
    )
    op.add_column(
        "freezer_movements",
        sa.Column("from_status", sa.String(length=24), nullable=True),
    )
    op.add_column(
        "freezer_movements",
        sa.Column("to_status", sa.String(length=24), nullable=True),
    )
    op.add_column(
        "freezer_movements",
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.add_column(
        "freezer_movements",
        sa.Column("attachment_id", sa.UUID(), nullable=True),
    )
    op.create_foreign_key(
        "fk_freezer_movements_contract",
        "freezer_movements",
        "contracts",
        ["contract_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_freezer_movements_attachment",
        "freezer_movements",
        "file_objects",
        ["attachment_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_unique_constraint(
        "uq_freezer_movements_doc_no", "freezer_movements", ["doc_no"]
    )
    op.create_index(
        "ix_freezer_movements_contract",
        "freezer_movements",
        ["contract_id"],
        unique=False,
    )
    op.drop_constraint(
        "freezer_movements_type_check", "freezer_movements", type_="check"
    )
    op.create_check_constraint(
        "freezer_movements_type_check", "freezer_movements", _NEW_MOVEMENT_CHECK
    )
    op.create_check_constraint(
        "freezer_movements_amount_nonneg",
        "freezer_movements",
        "amount_fen IS NULL OR amount_fen >= 0",
    )
    op.create_check_constraint(
        "freezer_movements_deposit_related_check",
        "freezer_movements",
        "movement_type NOT IN ('DEPOSIT_COLLECTED','DEPOSIT_REFUNDED') "
        "OR related_movement_id IS NOT NULL",
    )


def downgrade() -> None:
    # ---- freezer_movements ----
    op.drop_constraint(
        "freezer_movements_deposit_related_check",
        "freezer_movements",
        type_="check",
    )
    op.drop_constraint(
        "freezer_movements_amount_nonneg",
        "freezer_movements",
        type_="check",
    )
    op.drop_constraint(
        "freezer_movements_type_check", "freezer_movements", type_="check"
    )
    op.create_check_constraint(
        "freezer_movements_type_check",
        "freezer_movements",
        _OLD_MOVEMENT_CHECK,
    )
    op.drop_index("ix_freezer_movements_contract", table_name="freezer_movements")
    op.drop_constraint(
        "uq_freezer_movements_doc_no", "freezer_movements", type_="unique"
    )
    op.drop_constraint(
        "fk_freezer_movements_attachment", "freezer_movements", type_="foreignkey"
    )
    op.drop_constraint(
        "fk_freezer_movements_contract", "freezer_movements", type_="foreignkey"
    )
    op.drop_column("freezer_movements", "attachment_id")
    op.drop_column("freezer_movements", "occurred_at")
    op.drop_column("freezer_movements", "to_status")
    op.drop_column("freezer_movements", "from_status")
    op.drop_column("freezer_movements", "contract_id")
    op.drop_column("freezer_movements", "doc_no")
    op.alter_column(
        "freezer_movements",
        "amount_fen",
        existing_type=sa.BigInteger(),
        type_=sa.Integer(),
        existing_nullable=True,
    )

    # ---- freezer_assets ----
    op.drop_constraint(
        "freezer_assets_merchant_binding_check",
        "freezer_assets",
        type_="check",
    )
    op.drop_constraint(
        "freezer_assets_repair_count_nonneg",
        "freezer_assets",
        type_="check",
    )
    op.drop_constraint(
        "freezer_assets_status_check", "freezer_assets", type_="check"
    )
    op.create_check_constraint(
        "freezer_assets_status_check", "freezer_assets", _OLD_STATUS_CHECK
    )
    op.drop_column("freezer_assets", "repair_count")
    op.drop_column("freezer_assets", "last_recalled_at")
    op.drop_column("freezer_assets", "last_deployed_at")
    op.alter_column(
        "freezer_assets",
        "deposit_fen",
        existing_type=sa.BigInteger(),
        type_=sa.Integer(),
        existing_nullable=True,
    )
    op.alter_column(
        "freezer_assets",
        "purchase_price_fen",
        existing_type=sa.BigInteger(),
        type_=sa.Integer(),
        existing_nullable=False,
        existing_server_default=sa.text("0"),
    )
