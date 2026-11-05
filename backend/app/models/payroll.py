"""T20 费用、工资与发放状态（PRD §4.10 / §6.5）。

红线（§4.10 / 附录 A）：
- 费用与工资 Service 只接受同一 dataset_type+dataset_id 的上游事实；生产工资单
  引用 DEMO 绩效时整单拒绝（DataScope.PRODUCTION）；
- 未审批费用不进损益（仅 ``status=APPROVED`` 计入费用口径）；
- 工资 = 底薪 + T19 已复核绩效（``PerformanceMonthlySnapshot``）+ 审批调整；
- 工资单 PAID 后金额列固化，原三项构成行不改，差异只走**更正单**
  （``PayrollAdjustment.is_correction=True``）追加，历史工资单一律保留；
- 系统管理员无默认工资发放权；生产可配置财务双人复核（发放人≠复核人）；
- 首版不接银行代发 / 支付通道 / 税务 / 发票。

旧 v12 台账 ``payroll_records``（finance.py，从未落业务）保持不动。
"""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import AuditMixin, Base, DatasetMixin, pk_uuid


class Expense(Base, AuditMixin, DatasetMixin):
    """费用单（采购/冰柜购置/冰柜维修/活动/其他）。

    必须带凭证（file_objects PROOF_DOC）、归属月份；PENDING_APPROVAL/REJECTED
    不进损益，仅 APPROVED 计入。审批通过后回填 approval_id / approved_*。
    """

    __tablename__ = "expenses"
    __table_args__ = (
        CheckConstraint(
            "type IN ('PROCUREMENT','FREEZER_PURCHASE','FREEZER_REPAIR',"
            "'ACTIVITY','OTHER')",
            name="expense_type_check",
        ),
        CheckConstraint(
            "status IN ('PENDING_APPROVAL','APPROVED','REJECTED')",
            name="expense_status_check",
        ),
        CheckConstraint("amount_fen > 0", name="expense_amount_positive"),
        CheckConstraint("char_length(period_month) = 7",
                        name="expense_period_format"),
        # 审批通过必须有审批人与审批时间（财务审批动作即凭据；approval_id 可空）
        CheckConstraint(
            "status <> 'APPROVED' OR "
            "(approved_by_id IS NOT NULL AND approved_at IS NOT NULL)",
            name="expense_approved_requires_approver"),
        Index("ix_expense_period", "dataset_id", "period_month"),
    )

    id: Mapped[UUID] = pk_uuid()
    expense_no: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    type: Mapped[str] = mapped_column(String(24), nullable=False)
    title: Mapped[str] = mapped_column(String(128), nullable=False)
    amount_fen: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # 费用发生日（归属月份由其在 Asia/Shanghai 的年月派生）
    incurred_on: Mapped[date] = mapped_column(Date, nullable=False)
    period_month: Mapped[str] = mapped_column(String(7), nullable=False)
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, server_default=text("'PENDING_APPROVAL'"))
    # 凭证（T04 FileObject，biz_type=PROOF_DOC），录入即必填
    voucher_file_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("file_objects.id", ondelete="RESTRICT"),
        nullable=False)
    # 可选来源关联（采购单/活动/冰柜/巡访维修等）；OTHER 可空
    source_object_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    source_object_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True),
                                                          nullable=True)
    remark: Mapped[str | None] = mapped_column(Text, nullable=True)
    approval_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("approval_requests.id",
                                          ondelete="RESTRICT"), nullable=True)
    approved_by_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    rejected_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class PayrollStatement(Base, AuditMixin, DatasetMixin):
    """月度工资单头：底薪 + T19 已复核绩效 + 审批调整。

    DRAFT→REVIEWED（经理确认）→APPROVED（财务复核）→PAID（财务发放）。
    PAID 后 base/performance/adjustment/gross 四项固化不改；差异只追加更正单，
    累加到 correction_fen 并反映到 corrected_pay_fen，原单保留。
    """

    __tablename__ = "payroll_statements"
    __table_args__ = (
        UniqueConstraint("user_id", "period_month",
                         name="uq_payroll_user_month"),
        CheckConstraint("status IN ('DRAFT','REVIEWED','APPROVED','PAID')",
                        name="payroll_status_check"),
        CheckConstraint("char_length(period_month) = 7",
                        name="payroll_period_format"),
        CheckConstraint("base_salary_fen >= 0", name="payroll_base_nonneg"),
        CheckConstraint("gross_pay_fen >= 0", name="payroll_gross_nonneg"),
        CheckConstraint("corrected_pay_fen IS NULL OR corrected_pay_fen >= 0",
                        name="payroll_corrected_nonneg"),
        Index("ix_payroll_period", "dataset_id", "period_month"),
    )

    id: Mapped[UUID] = pk_uuid()
    statement_no: Mapped[str] = mapped_column(String(32), nullable=False,
                                              unique=True)
    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False)
    period_month: Mapped[str] = mapped_column(String(7), nullable=False)
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    # T19 已复核绩效快照（同 dataset）；当月无绩效可空
    performance_snapshot_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("performance_monthly_snapshots.id", ondelete="RESTRICT"),
        nullable=True)
    base_salary_fen: Mapped[int] = mapped_column(BigInteger, nullable=False,
                                                 server_default=text("0"))
    performance_fen: Mapped[int] = mapped_column(BigInteger, nullable=False,
                                                 server_default=text("0"))
    # 发放前审批调整合计（奖金正/罚扣负）
    adjustment_fen: Mapped[int] = mapped_column(BigInteger, nullable=False,
                                                server_default=text("0"))
    # 发放时固化应发 = base + performance + adjustment
    gross_pay_fen: Mapped[int] = mapped_column(BigInteger, nullable=False,
                                               server_default=text("0"))
    # PAID 后更正单合计（可正可负）及更正后应发（未发生更正为 NULL）
    correction_fen: Mapped[int] = mapped_column(BigInteger, nullable=False,
                                                server_default=text("0"))
    corrected_pay_fen: Mapped[int | None] = mapped_column(BigInteger,
                                                          nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False,
                                        server_default=text("'DRAFT'"))
    confirmed_by_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    reviewed_by_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    paid_by_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True)
    paid_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    paid_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    corrected_by_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True)
    corrected_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)


class PayrollLine(Base, AuditMixin, DatasetMixin):
    """工资单构成行（底薪/绩效/发放前调整），只增不改，供反查。"""

    __tablename__ = "payroll_statement_lines"
    __table_args__ = (
        CheckConstraint(
            "line_type IN ('BASE_SALARY','PERFORMANCE','ADJUSTMENT')",
            name="payroll_line_type_check"),
        UniqueConstraint("statement_id", "line_type", "source_object_type",
                         "source_object_id", name="uq_payroll_line_fact"),
    )

    id: Mapped[UUID] = pk_uuid()
    statement_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("payroll_statements.id", ondelete="RESTRICT"), nullable=False)
    line_type: Mapped[str] = mapped_column(String(16), nullable=False)
    source_object_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_object_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True),
                                                   nullable=False)
    # 调整（罚扣）可为负；底薪/绩效非负由 Service 保证
    amount_fen: Mapped[int] = mapped_column(BigInteger, nullable=False)
    basis_json: Mapped[dict] = mapped_column(JSONB, nullable=False,
                                             server_default=text("'{}'::jsonb"))


class PayrollAdjustment(Base, AuditMixin, DatasetMixin):
    """工资审批调整；PAID 后录入即“更正单”（is_correction=True）。

    奖金 BONUS 非负、罚扣 PENALTY 为负；必须引用 APPROVED 且动作匹配、同数据集、
    目标人员一致的审批单据。创建人即 AuditMixin.created_by_id（NOT NULL）。
    """

    __tablename__ = "payroll_statement_adjustments"
    __table_args__ = (
        CheckConstraint("kind IN ('BONUS','PENALTY')",
                        name="payroll_adjust_kind_check"),
        CheckConstraint(
            "(kind = 'BONUS' AND amount_fen >= 0) OR "
            "(kind = 'PENALTY' AND amount_fen < 0)",
            name="payroll_adjust_amount_sign"),
        Index("ix_payroll_adjust_statement", "statement_id"),
    )

    id: Mapped[UUID] = pk_uuid()
    statement_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("payroll_statements.id", ondelete="RESTRICT"), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    amount_fen: Mapped[int] = mapped_column(BigInteger, nullable=False)
    approval_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("approval_requests.id",
                                          ondelete="RESTRICT"), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    is_correction: Mapped[bool] = mapped_column(Boolean, nullable=False,
                                                server_default=text("false"))
    # 显式声明 NOT NULL 创建人（AuditMixin 仅提供可空 created_by_id）；
    # 迁移中带 users.id 外键。调整/更正的录入人即责任人。
    created_by_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)


class SalaryConfig(Base, AuditMixin, DatasetMixin):
    """薪资配置（T35）：按员工维护月薪与生效日期，支持历史版本。

    工资草稿生成时按 ``period_month`` 首日解析生效底薪：
    取 ``effective_from <= 该日`` 且 (``effective_to`` 为空或 ``>= 该日``)
    的最新一条 ``is_active`` 配置；缺失则报 ``SALARY_CONFIG_MISSING``。

    版本化方式：同一员工允许存在多条历史配置，(user_id, effective_from)
    唯一；新版本生效即自然覆盖旧版本，无需回写历史工资单。
    """

    __tablename__ = "salary_configs"
    __table_args__ = (
        UniqueConstraint("user_id", "effective_from",
                         name="uq_salary_config_user_from"),
        CheckConstraint("base_salary_fen >= 0",
                        name="salary_config_base_nonnegative"),
        CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from",
            name="salary_config_effective_range"),
        Index("ix_salary_config_user", "user_id"),
    )

    id: Mapped[UUID] = pk_uuid()
    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    base_salary_fen: Mapped[int] = mapped_column(BigInteger, nullable=False)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False,
                                            server_default=text("true"))
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
