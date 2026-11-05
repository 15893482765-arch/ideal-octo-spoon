"""T09 合同申请、审批与版本

Revision ID: a91b2e6c4d80
Revises: f2d9c4a71b30
Create Date: 2026-10-13

变更：
1) 重建 contracts：T03 遗留的是极简台账结构（code/start_date/end_date/
   monthly_rent_fen/is_active，status ∈ DRAFT/PENDING_APPROVAL/ACTIVE/EXPIRED/
   TERMINATED），与 T09 需要的“申请-审批-版本”合同模型不符，且这些旧列尚无业务
   引用（首版合同模块未落地）。此处重建为 PRD §4.4 的版本化合同表，保留同名
   contracts，复用 T03 已有的 display_threshold_pct 口径。
2) 新增 contract_versions：不可变版本快照，首次批准 v1，变更生成新版本。
3) R38/RK-6：task 幂等唯一索引纳入 assignee_id，使续签评估可为责任业务员与
   片区经理各生成一条（同对象同类型同责任人未关闭仍唯一）。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a91b2e6c4d80"
down_revision: str | None = "f2d9c4a71b30"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# T03 旧 contracts 结构（downgrade 时复原）
_OLD_FK_NAMES = ("contracts_merchant_id_fkey",)


def upgrade() -> None:
    # ---- 3) task 幂等索引纳入 assignee_id ----
    op.drop_index(
        "uq_task_items_idempotent",
        table_name="task_items",
        postgresql_where=sa.text("status IN ('OPEN','IN_PROGRESS')"),
    )
    op.create_index(
        "uq_task_items_idempotent",
        "task_items",
        ["related_object_type", "related_object_id", "task_kind", "assignee_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('OPEN','IN_PROGRESS')"),
    )

    # ---- 1) 重建 contracts（旧极简结构无业务数据，直接重建）----
    # 先移除 T03 遗留、无 ORM/无业务引用的死表（持有指向 contracts 的外键）。
    op.drop_index("ix_contract_approvals_contract", table_name="contract_approvals", if_exists=True)
    op.drop_index("ix_contract_approvals_approver", table_name="contract_approvals", if_exists=True)
    op.drop_table("contract_approvals")
    op.drop_index("ix_contract_changes_contract", table_name="contract_changes", if_exists=True)
    op.drop_table("contract_changes")

    op.drop_index("ix_contracts_merchant", table_name="contracts")
    op.drop_index("ix_contracts_expire_at", if_exists=True)
    op.drop_table("contracts")

    op.create_table(
        "contracts",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("merchant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("contract_no", sa.String(length=32), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("party_name", sa.String(length=200), nullable=False),
        # 24 容纳 TERMINATION_PENDING(19) / PENDING_APPROVAL(16)
        sa.Column("status", sa.String(length=24), server_default=sa.text("'DRAFT'"), nullable=False),
        sa.Column("template_version", sa.String(length=16), nullable=True),
        sa.Column("signed_at", sa.Date(), nullable=True),
        sa.Column("effective_from", sa.Date(), nullable=True),
        sa.Column("expire_at", sa.Date(), nullable=True),
        sa.Column("commission_rate", sa.Numeric(6, 4), nullable=True),
        sa.Column("display_subsidy_amount", sa.BigInteger(), nullable=True),
        sa.Column(
            "display_threshold_pct", sa.Integer(), server_default=sa.text("50"), nullable=False
        ),
        sa.Column("commission_rule_version", sa.String(length=32), nullable=True),
        sa.Column("current_version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("sales_rep_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("manager_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("attachment_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("renewal_generated_for_expire_at", sa.Date(), nullable=True),
        sa.Column("extra", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "dataset_type",
            sa.String(length=16),
            server_default=sa.text("'PRODUCTION'"),
            nullable=False,
        ),
        sa.Column(
            "dataset_id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('DRAFT','PENDING_APPROVAL','RETURNED','REJECTED','APPROVED',"
            "'EFFECTIVE','TERMINATION_PENDING','TERMINATED')",
            name="contracts_status_check",
        ),
        sa.CheckConstraint(
            "display_threshold_pct >= 0 AND display_threshold_pct <= 100",
            name="contracts_display_threshold_range",
        ),
        sa.CheckConstraint(
            "expire_at IS NULL OR effective_from IS NULL OR expire_at >= effective_from",
            name="contracts_date_range",
        ),
        sa.CheckConstraint(
            "display_subsidy_amount IS NULL OR display_subsidy_amount >= 0",
            name="contracts_display_subsidy_nonneg",
        ),
        sa.ForeignKeyConstraint(["merchant_id"], ["merchants.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["sales_rep_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["manager_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("contract_no", name="uq_contracts_contract_no"),
    )
    op.create_index("ix_contracts_merchant", "contracts", ["merchant_id"])
    op.create_index("ix_contracts_status", "contracts", ["status"])
    op.create_index("ix_contracts_expire_at", "contracts", ["expire_at"])
    op.create_index("ix_contracts_dataset_id", "contracts", ["dataset_id"])

    # ---- 2) 版本快照表 ----
    op.create_table(
        "contract_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("contract_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("change_type", sa.String(length=16), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("party_name", sa.String(length=200), nullable=False),
        sa.Column("template_version", sa.String(length=16), nullable=True),
        sa.Column("signed_at", sa.Date(), nullable=True),
        sa.Column("effective_from", sa.Date(), nullable=True),
        sa.Column("expire_at", sa.Date(), nullable=True),
        sa.Column("commission_rate", sa.Numeric(6, 4), nullable=True),
        sa.Column("display_subsidy_amount", sa.BigInteger(), nullable=True),
        sa.Column("display_threshold_pct", sa.Integer(), nullable=True),
        sa.Column("commission_rule_version", sa.String(length=32), nullable=True),
        sa.Column("attachment_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "dataset_type",
            sa.String(length=16),
            server_default=sa.text("'PRODUCTION'"),
            nullable=False,
        ),
        sa.Column(
            "dataset_id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.ForeignKeyConstraint(["contract_id"], ["contracts.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("contract_id", "version", name="uq_contract_versions_no"),
    )
    op.create_index("ix_contract_versions_contract_id", "contract_versions", ["contract_id"])
    op.create_index("ix_contract_versions_dataset_id", "contract_versions", ["dataset_id"])


def downgrade() -> None:
    op.drop_index("ix_contract_versions_dataset_id", table_name="contract_versions")
    op.drop_index("ix_contract_versions_contract_id", table_name="contract_versions")
    op.drop_table("contract_versions")

    op.drop_index("ix_contracts_dataset_id", table_name="contracts")
    op.drop_index("ix_contracts_expire_at", table_name="contracts")
    op.drop_index("ix_contracts_status", table_name="contracts")
    op.drop_index("ix_contracts_merchant", table_name="contracts")
    op.drop_table("contracts")

    # 复原 T03 极简结构
    op.create_table(
        "contracts",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("merchant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(length=16), server_default=sa.text("'DRAFT'"), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("display_threshold_pct", sa.Integer(), server_default=sa.text("50"), nullable=False),
        sa.Column("monthly_rent_fen", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("commission_rate", sa.Numeric(precision=5, scale=4), server_default=sa.text("0"), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("dataset_type", sa.String(length=16), server_default=sa.text("'PRODUCTION'"), nullable=False),
        sa.Column("dataset_id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.CheckConstraint(
            "status IN ('DRAFT','PENDING_APPROVAL','ACTIVE','EXPIRED','TERMINATED')",
            name="contracts_status_check",
        ),
        sa.CheckConstraint("display_threshold_pct >= 0 AND display_threshold_pct <= 100", name="contracts_display_threshold_range"),
        sa.CheckConstraint("end_date >= start_date", name="contracts_date_range"),
        sa.CheckConstraint("monthly_rent_fen >= 0", name="contracts_monthly_rent_nonneg"),
        sa.ForeignKeyConstraint(["merchant_id"], ["merchants.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("dataset_type", "dataset_id", "code", name="uq_contracts_dataset_code"),
    )
    op.create_index("ix_contracts_merchant", "contracts", ["merchant_id"])
    # T03 旧表的 (status, end_date) 索引，T03 downgrade 会 drop，需一并复原
    op.create_index(
        "ix_contracts_status_end", "contracts", ["status", "end_date"]
    )

    # 复原 T03 遗留死表（持有 contracts 外键）
    op.create_table(
        "contract_approvals",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("contract_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("decision", sa.String(length=16), nullable=False),
        sa.Column("approver_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("comment", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.ForeignKeyConstraint(["approver_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["contract_id"], ["contracts.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_contract_approvals_contract", "contract_approvals", ["contract_id"])
    op.create_table(
        "contract_changes",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("contract_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("change_type", sa.String(length=32), nullable=False),
        sa.Column("before_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("after_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("reason", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.ForeignKeyConstraint(["contract_id"], ["contracts.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_contract_changes_contract", "contract_changes", ["contract_id"])

    # 还原 T05 旧幂等索引（不含 assignee_id）
    op.drop_index(
        "uq_task_items_idempotent",
        table_name="task_items",
        postgresql_where=sa.text("status IN ('OPEN','IN_PROGRESS')"),
    )
    op.create_index(
        "uq_task_items_idempotent",
        "task_items",
        ["related_object_type", "related_object_id", "task_kind"],
        unique=True,
        postgresql_where=sa.text("status IN ('OPEN','IN_PROGRESS')"),
    )
