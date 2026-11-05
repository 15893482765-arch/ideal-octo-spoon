"""T35: 财务流水来源扩展 + 幂等唯一索引（金额只记流水）

Revision ID: f7a8b9c0d1e2
Revises: e6f7a8b9c0d1
Create Date: 2026-10-28 00:10:00.000000

- finance_transactions.source_kind 扩展 SALES_REVENUE / PAYROLL / ASSET /
  COMPANY_PURCHASE（旧值保留，历史数据可读）；
- 新增唯一索引 (related_object_type, related_object_id, tx_type, source_kind)：
  同一业务单据同一来源只允许一笔流水，重复触发幂等回读；
- 只加列/加约束，不删除任何历史列（绩效四项恒 0 的兼容策略同批处理）。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f7a8b9c0d1e2"
down_revision: str | Sequence[str] | None = "e6f7a8b9c0d1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_KINDS = "('OPEX','PROCUREMENT','RENT','COMMISSION','OTHER')"
_NEW_KINDS = (
    "('OPEX','PROCUREMENT','RENT','COMMISSION','OTHER','SALES_REVENUE',"
    "'PAYROLL','ASSET','COMPANY_PURCHASE')"
)


def upgrade() -> None:
    op.drop_constraint(
        "finance_tx_source_kind_check", "finance_transactions", type_="check"
    )
    op.create_check_constraint(
        "finance_tx_source_kind_check",
        "finance_transactions",
        f"source_kind IN {_NEW_KINDS}",
    )
    op.create_index(
        "uq_finance_tx_source_object",
        "finance_transactions",
        ["related_object_type", "related_object_id", "tx_type", "source_kind"],
        unique=True,
    )

    # ---- 薪资配置：按员工维护月薪与生效日期，支持历史版本（T35）----
    op.create_table(
        "salary_configs",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("base_salary_fen", sa.BigInteger(), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_to", sa.Date(), nullable=True),
        sa.Column(
            "is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False
        ),
        sa.Column("note", sa.Text(), nullable=True),
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
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_salary_configs_user",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id", "effective_from", name="uq_salary_config_user_from"
        ),
        sa.CheckConstraint(
            "base_salary_fen >= 0", name="salary_config_base_nonnegative"
        ),
        sa.CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from",
            name="salary_config_effective_range",
        ),
    )
    op.create_index("ix_salary_config_user", "salary_configs", ["user_id"])


def downgrade() -> None:
    # 索引随表一起删除，避免重复 DROP INDEX 报 UnderlyingObject 不存在
    op.drop_table("salary_configs")
    op.drop_index("uq_finance_tx_source_object", table_name="finance_transactions")
    op.drop_constraint(
        "finance_tx_source_kind_check", "finance_transactions", type_="check"
    )
    op.create_check_constraint(
        "finance_tx_source_kind_check",
        "finance_transactions",
        f"source_kind IN {_OLD_KINDS}",
    )
