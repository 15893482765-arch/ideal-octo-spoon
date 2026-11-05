"""冰柜中心 API 请求/响应模型（PRD §4.5 / R11 / R12 / R37）。ORM 不直接出 API。"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

# 验收结论
AcceptResult = Literal["IN_STOCK", "REPAIR", "SCRAP"]

# 撤回申请审批结论
RecallDecision = Literal["APPROVE", "REJECT"]


# ----------------------------- 请求 -----------------------------
class FreezerPurchaseRequest(BaseModel):
    """购置入库（建台账）。金额单位分，>=0。"""

    asset_code: str = Field(min_length=1, max_length=32)
    brand_id: UUID
    model: str = Field(min_length=1, max_length=64)
    purchase_date: date
    purchase_price_fen: int = Field(default=0, ge=0)
    deposit_fen: int | None = Field(default=None, ge=0)
    note: str | None = Field(default=None, max_length=255)


class FreezerDeployRequest(BaseModel):
    """创建投放单（在库 → 已预留）。必须关联生效合同。"""

    contract_id: UUID
    deposit_fen: int | None = Field(
        default=None,
        ge=0,
        description="本次收缴押金（分）；缺省取资产台账 deposit_fen；0/None 表示不收",
    )
    note: str | None = Field(default=None, max_length=255)


class FreezerDeployConfirmRequest(BaseModel):
    """现场投放确认（已预留 → 已投放），收缴押金流水在此刻生效。"""

    attachment_id: UUID | None = None
    note: str | None = Field(default=None, max_length=255)


class FreezerRecallRequestCreate(BaseModel):
    """提交撤回（撤柜）申请：业务员发起，需填写计划回收日期与原因。"""

    planned_pickup_date: date = Field(description="计划现场回收日期")
    reason: str = Field(min_length=1, max_length=255, description="撤回原因")


class FreezerRecallDecisionRequest(BaseModel):
    """片区经理审批撤回申请；同意时可将资产转「待撤回」，也可顺带改计划日期。"""

    decision: RecallDecision
    planned_pickup_date: date | None = Field(
        default=None, description="审批时可调整计划回收日期；缺省沿用申请日期"
    )
    note: str | None = Field(default=None, max_length=255)


class FreezerRecallRescheduleRequest(BaseModel):
    """修改计划回收日期（申请通过前后均可改，回收确认后不可改）。"""

    planned_pickup_date: date
    note: str | None = Field(default=None, max_length=255)


class FreezerPickupConfirmRequest(BaseModel):
    """现场回收（待撤回 → 待验收）。回收完成 ≠ 在库（R11）。"""

    attachment_id: UUID | None = None
    note: str | None = Field(default=None, max_length=255)


class FreezerAcceptanceRequest(BaseModel):
    """仓库验收（待验收 → 在库/维修/报废）。仅 IN_STOCK 时可用库存 +1。"""

    result: AcceptResult
    attachment_id: UUID | None = None
    note: str | None = Field(default=None, max_length=255)


class FreezerRepairRequest(BaseModel):
    """故障转维修（在库 → 维修中），或维修结束（维修中 → 在库）。"""

    attachment_id: UUID | None = None
    note: str | None = Field(default=None, max_length=255)


class FreezerScrapRequest(BaseModel):
    """维修中报废（不可逆，R13）。"""

    reason: str = Field(min_length=1, max_length=255)
    attachment_id: UUID | None = None


class FreezerDepositRefundRequest(BaseModel):
    """财务确认实际退押金（金额精确到分，关联验收单，不入损益，R37）。"""

    amount_fen: int | None = Field(
        default=None, ge=0, description="实际退款金额；缺省按收缴金额全额退"
    )
    attachment_id: UUID | None = Field(default=None, description="退款凭证")
    note: str | None = Field(default=None, max_length=255)


# ----------------------------- 响应 -----------------------------
class FreezerView(BaseModel):
    id: UUID
    asset_code: str
    brand_id: UUID
    model: str
    purchase_date: date
    purchase_price_fen: int
    deposit_fen: int | None = None
    status: str
    current_merchant_id: UUID | None = None
    responsible_user_id: UUID | None = None
    last_deployed_at: datetime | None = None
    last_recalled_at: datetime | None = None
    repair_count: int = 0
    is_active: bool = True
    # 撤回申请（PENDING 待审批 / APPROVED 已同意 / REJECTED 已驳回）
    recall_status: str | None = None
    recall_planned_date: date | None = None
    recall_reason: str | None = None
    recall_requested_by: UUID | None = None
    recall_requested_at: datetime | None = None
    recall_decided_by: UUID | None = None
    recall_decided_at: datetime | None = None
    recall_decision_note: str | None = None


class FreezerMovementView(BaseModel):
    id: UUID
    asset_id: UUID
    movement_type: str
    doc_no: str | None = None
    merchant_id: UUID | None = None
    contract_id: UUID | None = None
    related_movement_id: UUID | None = None
    amount_fen: int | None = None
    from_status: str | None = None
    to_status: str | None = None
    occurred_at: datetime
    attachment_id: UUID | None = None
    operator_id: UUID
    note: str | None = None


class StockCountView(BaseModel):
    """可用库存口径（仅 IN_STOCK；待验收不计，R11）。"""

    in_stock: int
