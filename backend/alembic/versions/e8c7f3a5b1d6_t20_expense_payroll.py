"""T20: expenses, payroll statements & disbursement status

Revision ID: e8c7f3a5b1d6
Revises: d7b6e2f4c9a3
Create Date: 2026-10-14 00:00:00.000000

PRD §4.10 / §6.5：
- expenses 费用单（采购/冰柜购置/维修/活动/其他；凭证、归属月、审批）；未审批不进
  损益（仅 APPROVED 计入），不接发票；
- payroll_statements 月度工资单（底薪 + T19 已复核绩效 + 审批调整；
  DRAFT→REVIEWED→APPROVED→PAID；系统管理员无默认发放权；生产财务双人复核）；
- payroll_statement_lines 构成行（底薪/绩效/调整），只增不改供反查；
- payroll_statement_adjustments 工资调整；PAID 后录入即更正单（is_correction），
  原四项金额固化、原单保留；调整必须引用已审批依据单据。
旧 v12 payroll_records 台账（finance.py，从未落业务）保持不动。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e8c7f3a5b1d6"
down_revision: str | Sequence[str] | None = "d7b6e2f4c9a3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _audit_dataset_cols(*, with_created_by: bool = True) -> list[sa.Column]:
    cols = [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
    ]
    if with_created_by:
        cols.append(sa.Column("created_by_id", postgresql.UUID(as_uuid=True),
                              nullable=True))
    cols += [
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("dataset_type", sa.String(length=16), nullable=False,
                  server_default=sa.text("'PRODUCTION'")),
        sa.Column("dataset_id", postgresql.UUID(as_uuid=True), nullable=False),
    ]
    return cols


def upgrade() -> None:
    # ---- expenses ----
    op.create_table(
        "expenses",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("expense_no", sa.String(length=32), nullable=False, unique=True),
        sa.Column("type", sa.String(length=24), nullable=False),
        sa.Column("title", sa.String(length=128), nullable=False),
        sa.Column("amount_fen", sa.BigInteger(), nullable=False),
        sa.Column("incurred_on", sa.Date(), nullable=False),
        sa.Column("period_month", sa.String(length=7), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False,
                  server_default=sa.text("'PENDING_APPROVAL'")),
        sa.Column("voucher_file_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("file_objects.id", ondelete="RESTRICT"),
                  nullable=False),
        sa.Column("source_object_type", sa.String(length=32), nullable=True),
        sa.Column("source_object_id", postgresql.UUID(as_uuid=True),
                  nullable=True),
        sa.Column("remark", sa.Text(), nullable=True),
        sa.Column("approval_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("approval_requests.id", ondelete="RESTRICT"),
                  nullable=True),
        sa.Column("approved_by_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rejected_reason", sa.Text(), nullable=True),
        *_audit_dataset_cols(),
        sa.CheckConstraint(
            "type IN ('PROCUREMENT','FREEZER_PURCHASE','FREEZER_REPAIR',"
            "'ACTIVITY','OTHER')", name="expense_type_check"),
        sa.CheckConstraint(
            "status IN ('PENDING_APPROVAL','APPROVED','REJECTED')",
            name="expense_status_check"),
        sa.CheckConstraint("amount_fen > 0", name="expense_amount_positive"),
        sa.CheckConstraint("char_length(period_month) = 7",
                           name="expense_period_format"),
        # 审批通过必须有审批人与审批时间（财务审批动作即凭据；
        # approval_id 可空——需要走审批流时才挂审批单证据）
        sa.CheckConstraint(
            "status <> 'APPROVED' OR "
            "(approved_by_id IS NOT NULL AND approved_at IS NOT NULL)",
            name="expense_approved_requires_approver"),
    )
    op.create_index("ix_expense_period", "expenses",
                    ["dataset_id", "period_month"])

    # ---- payroll_statements ----
    op.create_table(
        "payroll_statements",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("statement_no", sa.String(length=32), nullable=False,
                  unique=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("period_month", sa.String(length=7), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("performance_snapshot_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("performance_monthly_snapshots.id",
                                ondelete="RESTRICT"), nullable=True),
        sa.Column("base_salary_fen", sa.BigInteger(), nullable=False,
                  server_default=sa.text("0")),
        sa.Column("performance_fen", sa.BigInteger(), nullable=False,
                  server_default=sa.text("0")),
        sa.Column("adjustment_fen", sa.BigInteger(), nullable=False,
                  server_default=sa.text("0")),
        sa.Column("gross_pay_fen", sa.BigInteger(), nullable=False,
                  server_default=sa.text("0")),
        sa.Column("correction_fen", sa.BigInteger(), nullable=False,
                  server_default=sa.text("0")),
        sa.Column("corrected_pay_fen", sa.BigInteger(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False,
                  server_default=sa.text("'DRAFT'")),
        sa.Column("confirmed_by_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reviewed_by_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("paid_by_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("paid_on", sa.Date(), nullable=True),
        sa.Column("corrected_by_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("corrected_at", sa.DateTime(timezone=True), nullable=True),
        *_audit_dataset_cols(),
        sa.UniqueConstraint("user_id", "period_month",
                            name="uq_payroll_user_month"),
        sa.CheckConstraint("status IN ('DRAFT','REVIEWED','APPROVED','PAID')",
                           name="payroll_status_check"),
        sa.CheckConstraint("char_length(period_month) = 7",
                           name="payroll_period_format"),
        sa.CheckConstraint("base_salary_fen >= 0",
                           name="payroll_base_nonneg"),
        sa.CheckConstraint("gross_pay_fen >= 0", name="payroll_gross_nonneg"),
        sa.CheckConstraint(
            "corrected_pay_fen IS NULL OR corrected_pay_fen >= 0",
            name="payroll_corrected_nonneg"),
    )
    op.create_index("ix_payroll_period", "payroll_statements",
                    ["dataset_id", "period_month"])

    # ---- payroll_statement_lines ----
    op.create_table(
        "payroll_statement_lines",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("statement_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("payroll_statements.id", ondelete="RESTRICT"),
                  nullable=False),
        sa.Column("line_type", sa.String(length=16), nullable=False),
        sa.Column("source_object_type", sa.String(length=32), nullable=False),
        sa.Column("source_object_id", postgresql.UUID(as_uuid=True),
                  nullable=False),
        sa.Column("amount_fen", sa.BigInteger(), nullable=False),
        sa.Column("basis_json", postgresql.JSONB(astext_type=sa.Text()),
                  nullable=False, server_default=sa.text("'{}'::jsonb")),
        *_audit_dataset_cols(),
        sa.CheckConstraint(
            "line_type IN ('BASE_SALARY','PERFORMANCE','ADJUSTMENT')",
            name="payroll_line_type_check"),
        sa.UniqueConstraint("statement_id", "line_type", "source_object_type",
                            "source_object_id", name="uq_payroll_line_fact"),
    )

    # ---- payroll_statement_adjustments ----
    op.create_table(
        "payroll_statement_adjustments",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("statement_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("payroll_statements.id", ondelete="RESTRICT"),
                  nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("amount_fen", sa.BigInteger(), nullable=False),
        sa.Column("approval_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("approval_requests.id", ondelete="RESTRICT"),
                  nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("is_correction", sa.Boolean(), nullable=False,
                  server_default=sa.text("false")),
        # 创建人即 AuditMixin.created_by_id（NOT NULL，外键 users）
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        *_audit_dataset_cols(with_created_by=False),
        sa.CheckConstraint("kind IN ('BONUS','PENALTY')",
                           name="payroll_adjust_kind_check"),
        sa.CheckConstraint(
            "(kind = 'BONUS' AND amount_fen >= 0) OR "
            "(kind = 'PENALTY' AND amount_fen < 0)",
            name="payroll_adjust_amount_sign"),
    )
    op.create_index("ix_payroll_adjust_statement",
                    "payroll_statement_adjustments", ["statement_id"])


def downgrade() -> None:
    op.drop_index("ix_payroll_adjust_statement",
                 table_name="payroll_statement_adjustments")
    op.drop_table("payroll_statement_adjustments")
    op.drop_table("payroll_statement_lines")
    op.drop_index("ix_payroll_period", table_name="payroll_statements")
    op.drop_table("payroll_statements")
    op.drop_index("ix_expense_period", table_name="expenses")
    op.drop_table("expenses")
