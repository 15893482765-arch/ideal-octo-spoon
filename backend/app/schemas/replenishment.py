"""补货确认/履约 API 入参出参（Pydantic v2，T17）。ORM 不直接作为返回。"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, Field


class ConfirmRequest(BaseModel):
    confirmed_boxes: Decimal = Field(..., gt=0, max_digits=12, decimal_places=2)
    promised_delivery_date: date | None = None
    adjust_reason: str | None = Field(default=None, max_length=500)


class FulfillmentRequest(BaseModel):
    delivered_boxes: Decimal = Field(..., gt=0, max_digits=12, decimal_places=2)
    delivered_on: date
    idempotency_key: str = Field(min_length=1, max_length=64)
    proof_file_id: UUID | None = None
    note: str | None = Field(default=None, max_length=500)


class CancelRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


class CloseRequest(BaseModel):
    note: str | None = Field(default=None, max_length=500)


class FulfillmentView(BaseModel):
    id: UUID
    order_id: UUID
    delivered_boxes: Decimal
    delivered_on: date
    proof_file_id: UUID | None = None
    idempotency_key: str
    note: str | None = None
    registered_by: UUID
    # T36：送达即固化收入与毛利（建议零售价 − 参考进价，单位分）
    sales_amount_fen: int | None = None
    cost_fen: int | None = None
    profit_fen: int | None = None
    unit_retail_price_fen: int | None = None
    unit_purchase_price_fen: int | None = None


class ReplenishmentOrderView(BaseModel):
    id: UUID
    code: str
    merchant_id: UUID
    sku_id: UUID
    requested_boxes: Decimal
    committed_boxes: Decimal
    fulfilled_boxes: Decimal
    request_date: date
    promised_delivery_date: date | None = None
    actual_delivery_date: date | None = None
    status: str
    confirmer_id: UUID | None = None
    confirmed_at: date | None = None
    adjust_reason: str | None = None
    cancel_reason: str | None = None
    canceled_by: UUID | None = Field(default=None, validation_alias="cancelled_by")
    fulfillments: list[FulfillmentView] = Field(default_factory=list)

    model_config = {"populate_by_name": True}


class SummaryView(BaseModel):
    confirmed_order_count: int
    confirmed_boxes_total: Decimal
    by_sku: dict[str, Decimal]


class FulfillmentRateView(BaseModel):
    confirmed_boxes: Decimal
    fulfilled_boxes: Decimal
    on_time_fulfilled_boxes: Decimal
    order_count: int
    fulfillment_rate: Decimal
    on_time_rate: Decimal


# ----------------------------- 列表（T17 补货中心） -----------------------------


class ReplenishmentOrderListItem(BaseModel):
    """补货单列表行：带商家/SKU 展示名，避免前端逐行翻译 UUID。

    含 DRAFT/SUBMITTED（经理确认工作台需要看到待确认单），因此该视图**不是**
    正式汇总口径；已确认箱数汇总仍走 `/summary/confirmed`（R8：未确认不汇总）。
    """

    id: UUID
    code: str
    merchant_id: UUID
    merchant_code: str
    merchant_name: str
    sku_id: UUID
    sku_code: str
    sku_name: str
    requested_boxes: Decimal
    committed_boxes: Decimal
    fulfilled_boxes: Decimal
    # 未满足余额 = committed_boxes - fulfilled_boxes（恒 >= 0，服务层计算）
    remaining_boxes: Decimal
    request_date: date
    promised_delivery_date: date | None = None
    actual_delivery_date: date | None = None
    status: str
    confirmer_id: UUID | None = None
    confirmed_at: date | None = None
    adjust_reason: str | None = None
    cancel_reason: str | None = None


class Pagination(BaseModel):
    """分页元信息（与 schemas/merchant.Pagination 同形，各业务域自持一份）。"""

    page: int
    page_size: int
    total: int


class ReplenishmentOrderListResponse(BaseModel):
    items: list[ReplenishmentOrderListItem]
    pagination: Pagination
    # 回显本次实际生效的筛选条件（前端据此恢复 URL 状态）
    applied_filters: dict
