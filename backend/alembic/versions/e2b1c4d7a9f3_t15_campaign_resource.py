"""T15: campaign lifecycle, participants, resource occupancy & sample ledger

Revision ID: e2b1c4d7a9f3
Revises: c5a3f9b4d2e1
Create Date: 2026-10-13 00:00:00.000000

- activities 增补：region_id（行级片区范围）、取消原因/取消留痕、复盘留痕、
  样品成本 sample_cost_fen（BIGINT 存分，计入活动费用，不进确认销量与库存，R39）；
- activity_participants：活动参与人员（发布时为每人生成 CAMPAIGN 资源占用）；
- task_items.task_kind CHECK 增补 CAMPAIGN_SAMPLE_RETURN（复盘时样品未归还提醒）。
冲突业务口径仍只由 ScheduleConflictService 定义；CAMPAIGN 占用走 T13 通用
resource_reservations（source_type='CAMPAIGN'），GiST 排他约束不变。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e2b1c4d7a9f3"
down_revision: str | Sequence[str] | None = "c5a3f9b4d2e1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ---- activities 增补字段 ----
    # region_id 先以可空加列；activities 在 T03 建表但无任何 service 写入（为空表），
    # 加 FK 后直接收紧为 NOT NULL（生产若有历史行需先回填，首版无此数据）。
    op.add_column(
        "activities",
        sa.Column("region_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_activities_region",
        "activities", "regions",
        ["region_id"], ["id"], ondelete="RESTRICT",
    )
    op.alter_column("activities", "region_id", nullable=False)
    op.add_column(
        "activities",
        sa.Column("cancel_reason", sa.Text(), nullable=True),
    )
    op.add_column(
        "activities",
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "activities",
        sa.Column("cancelled_by_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "activities",
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "activities",
        sa.Column("reviewed_by_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    # 样品成本（分）：计入活动费用；样品不进确认销量与库存（R39）
    op.add_column(
        "activities",
        sa.Column("sample_cost_fen", sa.BigInteger(), nullable=False,
                  server_default=sa.text("0")),
    )
    op.alter_column("activities", "sample_cost_fen", server_default=sa.text("0"))
    op.create_check_constraint(
        "activities_sample_cost_nonneg",
        "activities", "sample_cost_fen >= 0",
    )
    op.create_index("ix_activities_region_status", "activities", ["region_id", "status"])

    # ---- 活动参与人员 ----
    op.create_table(
        "activity_participants",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("activity_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("activities.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("dataset_type", sa.String(length=16), nullable=False,
                  server_default=sa.text("'PRODUCTION'")),
        sa.Column("dataset_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.UniqueConstraint("activity_id", "user_id",
                            name="uq_activity_participants_activity_user"),
    )
    op.create_index("ix_activity_participants_activity",
                    "activity_participants", ["activity_id"])
    op.create_index("ix_activity_participants_user",
                    "activity_participants", ["user_id"])

    # ---- task_kind 白名单增补 CAMPAIGN_SAMPLE_RETURN ----
    op.drop_constraint("task_items_task_kind_check", "task_items", type_="check")
    op.create_check_constraint(
        "task_items_task_kind_check",
        "task_items",
        "task_kind IN ('VISIT_PLANNED','CONTRACT_RENEWAL_REVIEW','FREEZER_RECALL',"
        "'FREEZER_ACCEPTANCE','FREEZER_DEPOSIT_REFUND','REPLENISHMENT_REVIEW',"
        "'PROBLEM_RECTIFICATION','SUPERVISION_RECTIFICATION','PROBLEM_RECTIFICATION_VERIFY',"
        "'CONTRACT_RENEWAL_VERIFY','CONTRACT_EXPIRING_RENEWAL_EVAL',"
        "'CAMPAIGN_SAMPLE_RETURN','OTHER')",
    )


def downgrade() -> None:
    op.drop_constraint("task_items_task_kind_check", "task_items", type_="check")
    op.create_check_constraint(
        "task_items_task_kind_check",
        "task_items",
        "task_kind IN ('VISIT_PLANNED','CONTRACT_RENEWAL_REVIEW','FREEZER_RECALL',"
        "'FREEZER_ACCEPTANCE','FREEZER_DEPOSIT_REFUND','REPLENISHMENT_REVIEW',"
        "'PROBLEM_RECTIFICATION','SUPERVISION_RECTIFICATION','PROBLEM_RECTIFICATION_VERIFY',"
        "'CONTRACT_RENEWAL_VERIFY','CONTRACT_EXPIRING_RENEWAL_EVAL','OTHER')",
    )

    op.drop_index("ix_activity_participants_user", table_name="activity_participants")
    op.drop_index("ix_activity_participants_activity", table_name="activity_participants")
    op.drop_table("activity_participants")

    op.drop_index("ix_activities_region_status", table_name="activities")
    op.drop_constraint("activities_sample_cost_nonneg", "activities", type_="check")
    op.drop_column("activities", "sample_cost_fen")
    op.drop_column("activities", "reviewed_by_id")
    op.drop_column("activities", "reviewed_at")
    op.drop_column("activities", "cancelled_by_id")
    op.drop_column("activities", "cancelled_at")
    op.drop_column("activities", "cancel_reason")
    op.drop_constraint("fk_activities_region", "activities", type_="foreignkey")
    op.drop_column("activities", "region_id")
