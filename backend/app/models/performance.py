"""绩效与工资。"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import AuditMixin, Base, DatasetMixin, pk_uuid
from app.models.enums import (
    PayrollStatus,
    PerformanceAdjustmentKind,
    PerformanceRuleStatus,
    PerformanceSnapshotStatus,
)


class PerformanceRecord(Base, AuditMixin, DatasetMixin):
    __tablename__ = "performance_records"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "period_start", "period_end", name="uq_performance_user_period"
        ),
        CheckConstraint("period_end >= period_start", name="performance_period_range"),
        # 商家主动取消默认不扣款（v1.2 R40）：扣减必须引用审批依据
        # 扣减字段允许 0 或正，负值由业务层校验
        CheckConstraint("deduction_fen <= 0", name="performance_deduction_nonpositive"),
        Index("ix_performance_user_period", "user_id", "period_start"),
    )

    id: Mapped[UUID] = pk_uuid()
    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    new_contracts_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    confirmed_sales_amount_fen: Mapped[int] = mapped_column(
        nullable=False, default=0, server_default=text("0")
    )
    commission_fen: Mapped[int] = mapped_column(
        nullable=False, default=0, server_default=text("0")
    )
    # 责任扣款（<=0 分；商家主动取消默认 0，须由审批单据产生）
    deduction_fen: Mapped[int] = mapped_column(
        nullable=False, default=0, server_default=text("0")
    )
    # 扣款依据单据（审批单 ID）；扣款为 0 时可空，扣款 < 0 时必填
    deduction_approval_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("approval_requests.id", ondelete="RESTRICT"),
        nullable=True,
    )
    snapshot_json: Mapped[dict[str, Any]] = mapped_column(
        nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )


class PayrollRecord(Base, AuditMixin, DatasetMixin):
    __tablename__ = "payroll_records"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "period_start", "period_end", name="uq_payroll_user_period"
        ),
        CheckConstraint("period_end >= period_start", name="payroll_period_range"),
        CheckConstraint(
            "status IN ('DRAFT','REVIEWED','APPROVED','PAID')",
            name="payroll_status_check",
        ),
        CheckConstraint("base_salary_fen >= 0", name="payroll_base_nonneg"),
        Index("ix_payroll_user_period", "user_id", "period_start"),
    )

    id: Mapped[UUID] = pk_uuid()
    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    base_salary_fen: Mapped[int] = mapped_column(
        nullable=False, default=0, server_default=text("0")
    )
    performance_fen: Mapped[int] = mapped_column(
        nullable=False, default=0, server_default=text("0")
    )
    allowance_fen: Mapped[int] = mapped_column(
        nullable=False, default=0, server_default=text("0")
    )
    social_insurance_fen: Mapped[int] = mapped_column(
        nullable=False, default=0, server_default=text("0")
    )
    tax_fen: Mapped[int] = mapped_column(nullable=False, default=0, server_default=text("0"))
    # 个税/社保/补贴/底薪/绩效/其他调整均记录在 JSONB（避免字段膨胀）
    adjustments_json: Mapped[dict[str, Any]] = mapped_column(
        nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=PayrollStatus.DRAFT.value,
        server_default=text("'DRAFT'"),
    )


# ============================== T19 规则版本与月度快照 ==============================


class PerformanceRuleVersion(Base, AuditMixin, DatasetMixin):
    """绩效规则版本（T19 / PRD §4.10 R19）。

    - 保存生效日期区间、公式参数（JSONB）、配置人、审核人；
    - 已生成的月度快照以 rule_version_id 固化引用当期版本，后续改规则不回算历史；
    - 同一 (dataset) 内同一 code 只允许一个版本号；ACTIVE 版本可被新版本取代为
      SUPERSEDED（状态迁移，不物理删除，R24）。
    """

    __tablename__ = "performance_rule_versions"
    __table_args__ = (
        UniqueConstraint(
            "dataset_id", "code", "version",
            name="uq_perf_rule_version_no",
        ),
        CheckConstraint(
            "status IN ('DRAFT','ACTIVE','SUPERSEDED')",
            name="perf_rule_status_check",
        ),
        CheckConstraint("version >= 1", name="perf_rule_version_positive"),
        CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from",
            name="perf_rule_effective_range",
        ),
        CheckConstraint(
            "(status = 'ACTIVE') = (reviewed_by_id IS NOT NULL)",
            name="perf_rule_active_reviewed",
        ),
        Index("ix_perf_rule_effective", "dataset_id", "effective_from"),
    )

    id: Mapped[UUID] = pk_uuid()
    code: Mapped[str] = mapped_column(String(32), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=PerformanceRuleStatus.DRAFT.value,
        server_default=text("'DRAFT'"),
    )
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    # 公式参数：新签提成单价/巡访奖励单价/销量阶梯/活动奖励（金额分、比率整数基点）
    params_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    configured_by_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    reviewed_by_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
    )
    review_note: Mapped[str | None] = mapped_column(Text, nullable=True)


class PerformanceMonthlySnapshot(Base, AuditMixin, DatasetMixin):
    """月度绩效快照头（T19 / PRD §4.10、§6.5）。

    月末冻结已审核事实：头存当月规则版本、各分类金额与净额（整数分），
    状态 DRAFT → CONFIRMED（经理）→ REVIEWED（财务）；CONFIRMED/REVIEWED 后
    原行不可改，任何调整只写 performance_snapshot_adjustments 更正单并重算净额。
    """

    __tablename__ = "performance_monthly_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "period_month",
            name="uq_perf_snapshot_user_month",
        ),
        CheckConstraint(
            "status IN ('DRAFT','CONFIRMED','REVIEWED')",
            name="perf_snapshot_status_check",
        ),
        CheckConstraint("char_length(period_month) = 7",
                        name="perf_snapshot_period_format"),
        # 各分类金额（分）非负；扣减合计非正（存为 <=0 的分）
        CheckConstraint("new_contract_fen >= 0", name="perf_snapshot_newc_nonneg"),
        CheckConstraint("visit_bonus_fen >= 0", name="perf_snapshot_visit_nonneg"),
        CheckConstraint("sales_commission_fen >= 0",
                        name="perf_snapshot_sales_nonneg"),
        CheckConstraint("activity_bonus_fen >= 0",
                        name="perf_snapshot_activity_nonneg"),
        CheckConstraint("deduction_fen <= 0", name="perf_snapshot_deduction_nonpos"),
        CheckConstraint("adjustment_fen >= 0", name="perf_snapshot_adjust_nonneg"),
        Index("ix_perf_snapshot_month", "dataset_id", "period_month"),
    )

    id: Mapped[UUID] = pk_uuid()
    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    # 业务月，Asia/Shanghai，格式 YYYY-MM
    period_month: Mapped[str] = mapped_column(String(7), nullable=False)
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    rule_version_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("performance_rule_versions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    # 冻结时的规则版本号/参数冗余，便于直接反查（Q108）与审计
    rule_code: Mapped[str] = mapped_column(String(32), nullable=False)
    rule_version: Mapped[int] = mapped_column(Integer, nullable=False)
    new_contract_fen: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0"))
    visit_bonus_fen: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0"))
    sales_commission_fen: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0"))
    activity_bonus_fen: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0"))
    deduction_fen: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0"))
    adjustment_fen: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0"))
    # 净额 = 四项提成 + 调整 + 扣减(<=0)，服务端重算并落库
    net_performance_fen: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0"))
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=PerformanceSnapshotStatus.DRAFT.value,
        server_default=text("'DRAFT'"),
    )
    frozen_by_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
    )
    frozen_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    confirmed_by_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    reviewed_by_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)


class PerformanceSnapshotLine(Base, AuditMixin, DatasetMixin):
    """月度快照逐项依据行（T19 / PRD §4.10）。

    每条已审核事实一行（新签合同/有效巡访/确认销量/活动达成/扣减/调整），
    存事实单据类型+ID、归属用户、数量/金额（分）与命中规则参数；只增不改，
    供 Q108“快照可反查”逐项追溯。
    """

    __tablename__ = "performance_snapshot_lines"
    __table_args__ = (
        CheckConstraint(
            "line_type IN ('NEW_CONTRACT','VALID_VISIT','CONFIRMED_SALES',"
            "'ACTIVITY_ACHIEVED','DEDUCTION','ADJUSTMENT')",
            name="perf_snapshot_line_type_check",
        ),
        CheckConstraint("amount_fen IS NULL OR amount_fen >= 0 OR "
                        "line_type = 'DEDUCTION'",
                        name="perf_snapshot_line_amount_sign"),
        # 同一快照内同一事实单据只允许一行（幂等去重）
        UniqueConstraint(
            "snapshot_id", "line_type", "source_object_type", "source_object_id",
            name="uq_perf_snapshot_line_fact",
        ),
        Index("ix_perf_snapshot_line_snapshot", "snapshot_id"),
    )

    id: Mapped[UUID] = pk_uuid()
    snapshot_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("performance_monthly_snapshots.id", ondelete="RESTRICT"),
        nullable=False, index=True,
    )
    line_type: Mapped[str] = mapped_column(String(24), nullable=False)
    source_object_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_object_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True),
                                                   nullable=False)
    merchant_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("merchants.id", ondelete="RESTRICT"),
        nullable=True,
    )
    # 该事实经调拨日切分后归属的负责人（T14 owner_at）
    owner_user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    fact_date: Mapped[date] = mapped_column(Date, nullable=False)
    quantity: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    # 该行对绩效的金额贡献（分）；扣减行为负，其余非负
    amount_fen: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    # 命中的规则参数与计算依据（单价/比率/阶梯/公式），供逐项反查
    basis_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False,
                                                       default=dict)


class PerformanceSnapshotAdjustment(Base, AuditMixin, DatasetMixin):
    """快照更正单（T19 / PRD §4.10、R40、R24）。

    经理确认（CONFIRMED）/财务复核（REVIEWED）后不得改原行；任何扣减或调整
    必须引用“经审批的依据单据”（approval_requests，且状态 APPROVED），
    以本更正单追加，重算快照净额。商家主动取消不默认产生扣减（无依据即拒绝）。
    """

    __tablename__ = "performance_snapshot_adjustments"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('DEDUCTION','ADJUSTMENT')",
            name="perf_adjust_kind_check",
        ),
        # 扣减金额存负分（<0），调整金额非负
        CheckConstraint(
            "(kind = 'DEDUCTION' AND amount_fen < 0) OR "
            "(kind = 'ADJUSTMENT' AND amount_fen >= 0)",
            name="perf_adjust_amount_sign",
        ),
        # 扣减/调整都必须引用经审批依据（R20/R40）
        CheckConstraint("approval_id IS NOT NULL",
                        name="perf_adjust_approval_required"),
        Index("ix_perf_adjust_snapshot", "snapshot_id"),
    )

    id: Mapped[UUID] = pk_uuid()
    snapshot_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("performance_monthly_snapshots.id", ondelete="RESTRICT"),
        nullable=False, index=True,
    )
    kind: Mapped[str] = mapped_column(
        String(16), nullable=False,
        default=PerformanceAdjustmentKind.DEDUCTION.value,
    )
    amount_fen: Mapped[int] = mapped_column(BigInteger, nullable=False)
    approval_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("approval_requests.id", ondelete="RESTRICT"),
        nullable=False,
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    # 创建人复用 AuditMixin.created_by_id（NOT NULL 由业务写入保证）
