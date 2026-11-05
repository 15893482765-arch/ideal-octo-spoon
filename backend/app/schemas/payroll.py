"""T20 费用 / 工资单 API 入参出参（Pydantic v2）。"""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, Field

_MONTH = r"^\d{4}-(0[1-9]|1[0-2])$"


# ============================== 费用 ==============================

class CreateExpenseRequest(BaseModel):
    type: str = Field(
        ...,
        pattern="^(PROCUREMENT|FREEZER_PURCHASE|FREEZER_REPAIR|ACTIVITY|OTHER)$")
    title: str = Field(..., min_length=1, max_length=128)
    amount_fen: int = Field(..., gt=0)
    incurred_on: date
    voucher_file_id: UUID
    source_object_type: str | None = Field(default=None, max_length=32)
    source_object_id: UUID | None = None
    remark: str | None = Field(default=None, max_length=500)


class ApproveExpenseRequest(BaseModel):
    approval_id: UUID | None = None
    reason: str | None = Field(default=None, max_length=500)


class RejectExpenseRequest(BaseModel):
    reason: str = Field(..., min_length=1, max_length=500)


class ExpenseView(BaseModel):
    id: UUID
    expense_no: str
    type: str
    title: str
    amount_fen: int
    incurred_on: date
    period_month: str
    status: str
    voucher_file_id: UUID
    source_object_type: str | None = None
    source_object_id: UUID | None = None
    approval_id: UUID | None = None
    approved_by_id: UUID | None = None
    approved_at: datetime | None = None
    rejected_reason: str | None = None


# ============================== 工资单 ==============================

class AdjustmentInput(BaseModel):
    kind: str = Field(..., pattern="^(BONUS|PENALTY)$")
    amount_fen: int
    approval_id: UUID
    reason: str = Field(..., min_length=1, max_length=500)


class GenerateStatementRequest(BaseModel):
    user_id: UUID
    period_month: str = Field(..., pattern=_MONTH)
    # T35：可省略，缺省按薪资配置里该月生效的月薪自动带出（前端仍可覆盖）
    base_salary_fen: int | None = Field(default=None, ge=0)
    performance_snapshot_id: UUID | None = None
    adjustments: list[AdjustmentInput] = Field(default_factory=list)


# ============================== 薪资配置 ==============================

class CreateSalaryConfigRequest(BaseModel):
    user_id: UUID
    base_salary_fen: int = Field(..., ge=0)
    effective_from: date
    effective_to: date | None = None
    note: str | None = Field(default=None, max_length=255)


class SalaryConfigView(BaseModel):
    id: UUID
    user_id: UUID
    user_name: str | None = None
    base_salary_fen: int
    effective_from: date
    effective_to: date | None = None
    is_active: bool
    note: str | None = None
    created_at: datetime


class EffectiveSalaryView(BaseModel):
    """某员工某月生效月薪（工资生成表单自动带出用；无配置时 base 为 None）。"""

    user_id: UUID
    period_month: str
    base_salary_fen: int | None = None
    config_id: UUID | None = None
    effective_from: date | None = None


class AddAdjustmentRequest(AdjustmentInput):
    pass


class ApplyCorrectionRequest(AdjustmentInput):
    pass


class MarkPaidRequest(BaseModel):
    paid_on: date | None = None


class PayrollLineView(BaseModel):
    id: UUID
    line_type: str
    source_object_type: str
    source_object_id: UUID
    amount_fen: int
    basis_json: dict


class PayrollAdjustmentView(BaseModel):
    id: UUID
    statement_id: UUID
    kind: str
    amount_fen: int
    approval_id: UUID
    reason: str
    is_correction: bool
    created_by_id: UUID


class StatementView(BaseModel):
    id: UUID
    statement_no: str
    user_id: UUID
    user_name: str | None = None
    period_month: str
    period_start: date
    period_end: date
    performance_snapshot_id: UUID | None = None
    base_salary_fen: int
    performance_fen: int
    adjustment_fen: int
    gross_pay_fen: int
    correction_fen: int
    corrected_pay_fen: int | None = None
    status: str
    confirmed_by_id: UUID | None = None
    confirmed_at: datetime | None = None
    reviewed_by_id: UUID | None = None
    reviewed_at: datetime | None = None
    paid_by_id: UUID | None = None
    paid_at: datetime | None = None
    paid_on: date | None = None
    corrected_by_id: UUID | None = None
    corrected_at: datetime | None = None


class StatementDetailView(StatementView):
    lines: list[PayrollLineView] = Field(default_factory=list)
    adjustments: list[PayrollAdjustmentView] = Field(default_factory=list)


class StatementListView(BaseModel):
    """工资单列表（财务工资发放页）。"""

    items: list[StatementView] = Field(default_factory=list)
    total: int = 0
    page: int = 1
    page_size: int = 50
