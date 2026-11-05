"""T35 财务总账 API 入参出参（金额整数分，Pydantic v2）。

只做参数形态校验；数据域隔离、幂等、审批规则全部在 Service 层。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field


# ============================== 总账 ==============================

class LedgerBucketView(BaseModel):
    """分桶收支（按月为 `YYYY-MM`，按年为 `YYYY`）。"""

    period: str
    income_fen: int
    expense_fen: int
    net_fen: int


class LedgerSummaryView(BaseModel):
    granularity: Literal["month", "year"]
    year: int
    month: int | None = None
    income_fen: int
    expense_fen: int
    net_fen: int
    buckets: list[LedgerBucketView]


class FinanceTransactionView(BaseModel):
    id: UUID
    biz_date: date
    tx_type: str
    source_kind: str
    source_label: str
    amount_fen: int
    related_object_type: str
    related_object_id: UUID
    note: str | None = None
    bookkeeper_id: UUID
    bookkeeper_name: str | None = None
    created_at: datetime


# ======================== 公司物品采购 ========================

class CreateCompanyPurchaseRequest(BaseModel):
    """公司物品采购：提交后为待审批，审批通过自动计入支出流水。"""

    title: str = Field(..., min_length=1, max_length=128)
    category: str = Field(..., pattern="^(PROCUREMENT|FREEZER_PURCHASE|OTHER)$")
    amount_fen: int = Field(..., gt=0)
    incurred_on: date
    voucher_file_id: UUID
    remark: str | None = Field(default=None, max_length=500)


class CompanyPurchaseDecisionRequest(BaseModel):
    approval_id: UUID | None = None
    reason: str | None = Field(default=None, max_length=500)


class CompanyPurchaseView(BaseModel):
    id: UUID
    expense_no: str
    title: str
    category: str
    amount_fen: int
    incurred_on: date
    status: str
    voucher_file_id: UUID
    remark: str | None = None
    approved_at: datetime | None = None
    rejected_reason: str | None = None
    created_at: datetime
