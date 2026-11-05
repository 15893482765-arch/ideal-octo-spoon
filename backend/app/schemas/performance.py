"""T19 绩效规则版本 / 月度快照 API 入参出参（Pydantic v2）。"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, Field

# ----------------------------- 规则版本 -----------------------------


class SalesTierInput(BaseModel):
    # 该档上限（分）；最后一档必须为 null（无上限）
    cap_fen: int | None = Field(default=None, ge=0)
    # 该档边际提成比率，基点（1bp=0.01%），如 250=2.50%
    rate_bp: int = Field(..., ge=0, le=10000)


class RuleParamsInput(BaseModel):
    new_contract_award_fen: int = Field(..., ge=0)
    valid_visit_award_fen: int = Field(..., ge=0)
    activity_award_fen: int = Field(..., ge=0)
    sales_tiers: list[SalesTierInput] = Field(..., min_length=1)


class CreateRuleVersionRequest(BaseModel):
    code: str = Field(default="SALES_PERSON_COMMISSION", max_length=32)
    name: str = Field(..., min_length=1, max_length=128)
    effective_from: date
    effective_to: date | None = None
    params: RuleParamsInput


class ReviewRuleVersionRequest(BaseModel):
    note: str | None = Field(default=None, max_length=500)


class RuleVersionView(BaseModel):
    id: UUID
    code: str
    version: int
    name: str
    status: str
    effective_from: date
    effective_to: date | None = None
    params_json: dict
    configured_by_id: UUID
    reviewed_by_id: UUID | None = None
    review_note: str | None = None


# ----------------------------- 月度快照 -----------------------------


class FreezeSnapshotRequest(BaseModel):
    user_id: UUID
    period_month: str = Field(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    rule_version_id: UUID | None = None


class ConfirmSnapshotRequest(BaseModel):
    pass


class ReturnSnapshotRequest(BaseModel):
    reason: str = Field(..., min_length=1, max_length=500)


class ApplyAdjustmentRequest(BaseModel):
    # DEDUCTION 扣减（amount_fen 必须为负）/ ADJUSTMENT 调整（非负）
    kind: str = Field(..., pattern="^(DEDUCTION|ADJUSTMENT)$")
    amount_fen: int
    approval_id: UUID
    reason: str = Field(..., min_length=1, max_length=500)


class SnapshotLineView(BaseModel):
    id: UUID
    line_type: str
    source_object_type: str
    source_object_id: UUID
    merchant_id: UUID | None = None
    owner_user_id: UUID
    fact_date: date
    quantity: Decimal | None = None
    amount_fen: int
    basis_json: dict


class AdjustmentView(BaseModel):
    id: UUID
    snapshot_id: UUID
    kind: str
    amount_fen: int
    approval_id: UUID
    reason: str
    created_by_id: UUID


class SnapshotView(BaseModel):
    id: UUID
    user_id: UUID
    period_month: str
    period_start: date
    period_end: date
    rule_version_id: UUID
    rule_code: str
    rule_version: int
    new_contract_fen: int
    visit_bonus_fen: int
    sales_commission_fen: int
    activity_bonus_fen: int
    deduction_fen: int
    adjustment_fen: int
    net_performance_fen: int
    status: str
    frozen_by_id: UUID | None = None
    frozen_at: datetime | None = None
    confirmed_by_id: UUID | None = None
    confirmed_at: datetime | None = None
    reviewed_by_id: UUID | None = None
    reviewed_at: datetime | None = None


class SnapshotDetailView(SnapshotView):
    lines: list[SnapshotLineView] = Field(default_factory=list)
    adjustments: list[AdjustmentView] = Field(default_factory=list)


class SnapshotListItemView(SnapshotView):
    """快照列表行：附带业务员展示名，免去前端逐行回查用户。"""

    user_username: str
    user_name: str


# ----------------------------- 下拉 / 选项 -----------------------------


class OptionView(BaseModel):
    id: UUID
    code: str
    name: str


class OptionListView(BaseModel):
    items: list[OptionView] = Field(default_factory=list)


class ApprovalOptionView(BaseModel):
    """更正单可引用的已审批依据（R40：无依据不得扣减/调整）。"""

    id: UUID
    action: str
    amount_fen: int
    reason: str
    decided_at: datetime | None = None


class ApprovalOptionListView(BaseModel):
    items: list[ApprovalOptionView] = Field(default_factory=list)
