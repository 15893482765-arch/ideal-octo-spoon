"""T10 freezer recall request: 撤回申请（业务员发起 → 片区经理审批）

Revision ID: c8d3f2a1e506
Revises: a7c2e9f4b105
Create Date: 2026-10-19

PRD §4.5：
- `freezer_assets` 增加撤回申请字段（状态/计划回收日期/申请人/审批人/审批意见）；
- `freezer_movements` 流水类型增加 RECALL_REQUEST / RECALL_REJECT / RECALL_RESCHEDULE，
  审批通过仍沿用既有 RECALL 流水（撤柜单）。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c8d3f2a1e506"
down_revision: str | None = "a7c2e9f4b105"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NEW_MOVEMENT_CHECK = (
    "movement_type IN ('PURCHASE','DEPLOYMENT_ORDER','DEPLOY','RECALL',"
    "'RECALL_REQUEST','RECALL_REJECT','RECALL_RESCHEDULE','PICKUP',"
    "'ACCEPT','REPAIR_START','REPAIR_END','SCRAP',"
    "'DEPOSIT_COLLECTED','DEPOSIT_REFUNDED')"
)
_OLD_MOVEMENT_CHECK = (
    "movement_type IN ('PURCHASE','DEPLOYMENT_ORDER','DEPLOY','RECALL','PICKUP',"
    "'ACCEPT','REPAIR_START','REPAIR_END','SCRAP',"
    "'DEPOSIT_COLLECTED','DEPOSIT_REFUNDED')"
)


def upgrade() -> None:
    # ---- freezer_assets：撤回申请字段 ----
    op.add_column(
        "freezer_assets", sa.Column("recall_status", sa.String(length=16), nullable=True)
    )
    op.add_column(
        "freezer_assets", sa.Column("recall_planned_date", sa.Date(), nullable=True)
    )
    op.add_column(
        "freezer_assets", sa.Column("recall_reason", sa.String(length=255), nullable=True)
    )
    op.add_column(
        "freezer_assets", sa.Column("recall_requested_by", sa.UUID(), nullable=True)
    )
    op.add_column(
        "freezer_assets",
        sa.Column("recall_requested_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "freezer_assets", sa.Column("recall_decided_by", sa.UUID(), nullable=True)
    )
    op.add_column(
        "freezer_assets",
        sa.Column("recall_decided_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "freezer_assets",
        sa.Column("recall_decision_note", sa.String(length=255), nullable=True),
    )
    op.create_foreign_key(
        "fk_freezer_assets_recall_requested_by",
        "freezer_assets",
        "users",
        ["recall_requested_by"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_freezer_assets_recall_decided_by",
        "freezer_assets",
        "users",
        ["recall_decided_by"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "freezer_assets_recall_status_check",
        "freezer_assets",
        "recall_status IS NULL OR recall_status IN ('PENDING','APPROVED','REJECTED')",
    )

    # ---- freezer_movements：新增撤回相关流水类型 ----
    op.drop_constraint(
        "freezer_movements_type_check", "freezer_movements", type_="check"
    )
    op.create_check_constraint(
        "freezer_movements_type_check", "freezer_movements", _NEW_MOVEMENT_CHECK
    )


def downgrade() -> None:
    op.drop_constraint(
        "freezer_movements_type_check", "freezer_movements", type_="check"
    )
    op.create_check_constraint(
        "freezer_movements_type_check", "freezer_movements", _OLD_MOVEMENT_CHECK
    )

    op.drop_constraint(
        "freezer_assets_recall_status_check", "freezer_assets", type_="check"
    )
    op.drop_constraint(
        "fk_freezer_assets_recall_decided_by", "freezer_assets", type_="foreignkey"
    )
    op.drop_constraint(
        "fk_freezer_assets_recall_requested_by", "freezer_assets", type_="foreignkey"
    )
    op.drop_column("freezer_assets", "recall_decision_note")
    op.drop_column("freezer_assets", "recall_decided_at")
    op.drop_column("freezer_assets", "recall_decided_by")
    op.drop_column("freezer_assets", "recall_requested_at")
    op.drop_column("freezer_assets", "recall_requested_by")
    op.drop_column("freezer_assets", "recall_reason")
    op.drop_column("freezer_assets", "recall_planned_date")
    op.drop_column("freezer_assets", "recall_status")
