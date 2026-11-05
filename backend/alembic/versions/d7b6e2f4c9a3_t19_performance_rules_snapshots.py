"""T19: performance rule versions & monthly snapshots

Revision ID: d7b6e2f4c9a3
Revises: c6a5f1d3b8e2
Create Date: 2026-10-14 00:00:00.000000

PRD §4.10 / §6.5 / R19 / R20 / R24 / R40：
- performance_rule_versions 规则版本（生效区间/参数/配置人/审核人）；
- performance_monthly_snapshots 月度冻结快照头（引用规则版本、分类金额、净额、
  DRAFT→CONFIRMED→REVIEWED）；
- performance_snapshot_lines 逐项已审核事实依据（新签/有效巡访/确认销量/活动达成/
  扣减/调整），只增不改，供反查；
- performance_snapshot_adjustments 确认/复核后的更正单（扣减或调整必须引用
  已审批依据单据；商家主动取消不默认扣款）。
旧 performance_records / payroll_records（v12 预留）保持不动。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d7b6e2f4c9a3"
down_revision: str | Sequence[str] | None = "c6a5f1d3b8e2"
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
    # ---- performance_rule_versions ----
    op.create_table(
        "performance_rule_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False,
                  server_default=sa.text("'DRAFT'")),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_to", sa.Date(), nullable=True),
        sa.Column("params_json", postgresql.JSONB(astext_type=sa.Text()),
                  nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("configured_by_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("reviewed_by_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("review_note", sa.Text(), nullable=True),
        *_audit_dataset_cols(),
        sa.UniqueConstraint("dataset_id", "code", "version",
                            name="uq_perf_rule_version_no"),
        sa.CheckConstraint("status IN ('DRAFT','ACTIVE','SUPERSEDED')",
                           name="perf_rule_status_check"),
        sa.CheckConstraint("version >= 1", name="perf_rule_version_positive"),
        sa.CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from",
            name="perf_rule_effective_range"),
        sa.CheckConstraint(
            "(status = 'ACTIVE') = (reviewed_by_id IS NOT NULL)",
            name="perf_rule_active_reviewed"),
    )
    op.create_index("ix_perf_rule_effective", "performance_rule_versions",
                    ["dataset_id", "effective_from"])

    # ---- performance_monthly_snapshots ----
    op.create_table(
        "performance_monthly_snapshots",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("period_month", sa.String(length=7), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("rule_version_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("performance_rule_versions.id",
                                ondelete="RESTRICT"), nullable=False),
        sa.Column("rule_code", sa.String(length=32), nullable=False),
        sa.Column("rule_version", sa.Integer(), nullable=False),
        sa.Column("new_contract_fen", sa.BigInteger(), nullable=False,
                  server_default=sa.text("0")),
        sa.Column("visit_bonus_fen", sa.BigInteger(), nullable=False,
                  server_default=sa.text("0")),
        sa.Column("sales_commission_fen", sa.BigInteger(), nullable=False,
                  server_default=sa.text("0")),
        sa.Column("activity_bonus_fen", sa.BigInteger(), nullable=False,
                  server_default=sa.text("0")),
        sa.Column("deduction_fen", sa.BigInteger(), nullable=False,
                  server_default=sa.text("0")),
        sa.Column("adjustment_fen", sa.BigInteger(), nullable=False,
                  server_default=sa.text("0")),
        sa.Column("net_performance_fen", sa.BigInteger(), nullable=False,
                  server_default=sa.text("0")),
        sa.Column("status", sa.String(length=16), nullable=False,
                  server_default=sa.text("'DRAFT'")),
        sa.Column("frozen_by_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("frozen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("confirmed_by_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reviewed_by_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        *_audit_dataset_cols(),
        sa.UniqueConstraint("user_id", "period_month",
                            name="uq_perf_snapshot_user_month"),
        sa.CheckConstraint("status IN ('DRAFT','CONFIRMED','REVIEWED')",
                           name="perf_snapshot_status_check"),
        sa.CheckConstraint("char_length(period_month) = 7",
                           name="perf_snapshot_period_format"),
        sa.CheckConstraint("new_contract_fen >= 0",
                           name="perf_snapshot_newc_nonneg"),
        sa.CheckConstraint("visit_bonus_fen >= 0",
                           name="perf_snapshot_visit_nonneg"),
        sa.CheckConstraint("sales_commission_fen >= 0",
                           name="perf_snapshot_sales_nonneg"),
        sa.CheckConstraint("activity_bonus_fen >= 0",
                           name="perf_snapshot_activity_nonneg"),
        sa.CheckConstraint("deduction_fen <= 0",
                           name="perf_snapshot_deduction_nonpos"),
        sa.CheckConstraint("adjustment_fen >= 0",
                           name="perf_snapshot_adjust_nonneg"),
    )
    op.create_index("ix_perf_snapshot_month", "performance_monthly_snapshots",
                    ["dataset_id", "period_month"])

    # ---- performance_snapshot_lines ----
    op.create_table(
        "performance_snapshot_lines",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("snapshot_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("performance_monthly_snapshots.id",
                                ondelete="RESTRICT"), nullable=False),
        sa.Column("line_type", sa.String(length=24), nullable=False),
        sa.Column("source_object_type", sa.String(length=32), nullable=False),
        sa.Column("source_object_id", postgresql.UUID(as_uuid=True),
                  nullable=False),
        sa.Column("merchant_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("merchants.id", ondelete="RESTRICT"),
                  nullable=True),
        sa.Column("owner_user_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("fact_date", sa.Date(), nullable=False),
        sa.Column("quantity", sa.Numeric(12, 2), nullable=True),
        sa.Column("amount_fen", sa.BigInteger(), nullable=False,
                  server_default=sa.text("0")),
        sa.Column("basis_json", postgresql.JSONB(astext_type=sa.Text()),
                  nullable=False, server_default=sa.text("'{}'::jsonb")),
        *_audit_dataset_cols(),
        sa.CheckConstraint(
            "line_type IN ('NEW_CONTRACT','VALID_VISIT','CONFIRMED_SALES',"
            "'ACTIVITY_ACHIEVED','DEDUCTION','ADJUSTMENT')",
            name="perf_snapshot_line_type_check"),
        sa.CheckConstraint(
            "amount_fen IS NULL OR amount_fen >= 0 OR line_type = 'DEDUCTION'",
            name="perf_snapshot_line_amount_sign"),
        sa.UniqueConstraint(
            "snapshot_id", "line_type", "source_object_type",
            "source_object_id", name="uq_perf_snapshot_line_fact"),
    )
    op.create_index("ix_perf_snapshot_line_snapshot",
                    "performance_snapshot_lines", ["snapshot_id"])

    # ---- performance_snapshot_adjustments ----
    op.create_table(
        "performance_snapshot_adjustments",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("snapshot_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("performance_monthly_snapshots.id",
                                ondelete="RESTRICT"), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False,
                  server_default=sa.text("'DEDUCTION'")),
        sa.Column("amount_fen", sa.BigInteger(), nullable=False),
        sa.Column("approval_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("approval_requests.id", ondelete="RESTRICT"),
                  nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        # 创建人即 AuditMixin.created_by_id（NOT NULL，外键 users）
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        *_audit_dataset_cols(with_created_by=False),
        sa.CheckConstraint("kind IN ('DEDUCTION','ADJUSTMENT')",
                           name="perf_adjust_kind_check"),
        sa.CheckConstraint(
            "(kind = 'DEDUCTION' AND amount_fen < 0) OR "
            "(kind = 'ADJUSTMENT' AND amount_fen >= 0)",
            name="perf_adjust_amount_sign"),
        sa.CheckConstraint("approval_id IS NOT NULL",
                           name="perf_adjust_approval_required"),
    )
    op.create_index("ix_perf_adjust_snapshot",
                    "performance_snapshot_adjustments", ["snapshot_id"])


def downgrade() -> None:
    op.drop_table("performance_snapshot_adjustments")
    op.drop_table("performance_snapshot_lines")
    op.drop_table("performance_monthly_snapshots")
    op.drop_index("ix_perf_rule_effective", table_name="performance_rule_versions")
    op.drop_table("performance_rule_versions")
