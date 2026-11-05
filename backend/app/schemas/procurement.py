"""采购与厂商供货 API 入参出参（Pydantic v2，T18）。

安全口径：
- 建单请求体**不含** total_amount 字段（Q096：总额一律服务端重算，无法被伪造）；
- 供货确认不含任何付款/库存字段（Q100：厂商确认≠付款/入库）。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, Field


class SuggestionQuery(BaseModel):
    brand_id: UUID
    region_id: UUID
    supplier_id: UUID | None = None
    requested_from: date | None = None
    requested_to: date | None = None


class SuggestionLineView(BaseModel):
    sku_id: UUID
    sku_code: str
    brand_id: UUID
    region_id: UUID
    confirmed_unmet_boxes: Decimal
    open_purchase_boxes: Decimal
    suggested_purchase_boxes: Decimal
    inventory_available: bool
    inventory_status: str
    # T36：超期未收货的在途量（不参与扣减，需跟进厂商或关闭采购单）
    overdue_purchase_boxes: Decimal = Decimal("0")
    # 已被有效在途完全覆盖（仅 include_covered=true 时返回，页面据此解释空态）
    covered_by_open_purchase: bool = False
    # T32：系统现存箱数（入库 − 出库），负值提示账实待核对
    available_boxes: Decimal = Decimal("0")


class SuggestionDiagnosticsView(BaseModel):
    """采购建议口径诊断：解释"为什么这里没有建议"。"""

    dataset_type: str | None = None
    dataset_id: str | None = None
    confirmed_sku_count: int = 0
    confirmed_unmet_boxes: Decimal = Decimal("0")
    open_purchase_boxes: Decimal = Decimal("0")
    overdue_purchase_boxes: Decimal = Decimal("0")
    suggested_purchase_boxes: Decimal = Decimal("0")
    available_inventory_boxes: Decimal = Decimal("0")
    covered_sku_count: int = 0
    overdue_grace_days: int = 3


class PurchaseOrderLineInput(BaseModel):
    sku_id: UUID
    quantity_cases: Decimal = Field(..., gt=0, max_digits=12, decimal_places=2)
    # T36：含税单价可缺省——缺省时服务端按 SKU 参考进价（purchase_price_fen）自动带出，
    # 避免人工算价/空价导致建单失败；行金额与总额仍只由服务端重算（Q096）。
    # 单价单位是**元/瓶**，数量是箱：行金额 = 箱数 × 箱规 × 单价。
    tax_inclusive_unit_price_fen: int | None = Field(default=None, ge=0)
    # 刻意无 line_amount / total：金额只由服务端重算（Q096）


class CreatePurchaseOrderRequest(BaseModel):
    supplier_id: UUID
    brand_id: UUID
    region_id: UUID
    expected_date: date | None = None
    lines: list[PurchaseOrderLineInput] = Field(..., min_length=1)
    note: str | None = Field(default=None, max_length=500)
    # 刻意无 total_amount_fen（Q096）


class PurchaseOrderLineView(BaseModel):
    id: UUID
    sku_id: UUID
    # 商品编码/名称随单返回：厂商供货确认与采购列表直接展示"编码 · 名称"，
    # 不再依赖前端二次调用 sku-options 做 id→名称映射（映射失败会退化成 UUID 片段）。
    sku_code: str
    sku_name: str
    quantity_cases: Decimal
    tax_inclusive_unit_price_fen: int
    line_amount_fen: int
    source_confirmed_boxes: Decimal


class PurchaseOrderView(BaseModel):
    id: UUID
    code: str
    supplier_id: UUID
    brand_id: UUID
    region_id: UUID
    expected_date: date | None = None
    total_amount_fen: int
    status: str
    confirmed_at: date | None = None
    received_at: date | None = None
    note: str | None = None
    lines: list[PurchaseOrderLineView] = Field(default_factory=list)


class SupplyConfirmRequest(BaseModel):
    confirmed_quantity_cases: Decimal = Field(
        ..., gt=0, max_digits=12, decimal_places=2)
    expected_delivery_date: date
    proof_file_id: UUID | None = None
    note: str | None = Field(default=None, max_length=500)


class SupplyRejectRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


class SupplyConfirmationView(BaseModel):
    id: UUID
    order_id: UUID
    order_line_id: UUID
    supplier_id: UUID
    supplier_user_id: UUID
    status: str
    confirmed_quantity_cases: Decimal | None = None
    expected_delivery_date: date | None = None
    proof_file_id: UUID | None = None
    reject_reason: str | None = None
    note: str | None = None


class SupplierView(BaseModel):
    id: str
    code: str
    name: str
    # 厂商身份下恒为 None（Q099：内部联系人脱敏）
    contact_name: str | None = None
    contact_phone: str | None = None


class CreateSupplierIn(BaseModel):
    """创建供应商请求（SETTINGS 角色专用）。"""
    code: str = Field(..., min_length=1, max_length=50)
    name: str = Field(..., min_length=1, max_length=200)
    contact_name: str | None = Field(default=None, max_length=100)
    contact_phone: str | None = Field(default=None, max_length=30)


class SupplierManagementOut(BaseModel):
    """供应商管理台账行（含停用）。"""
    id: str
    code: str
    name: str
    contact_name: str | None = None
    contact_phone: str | None = None
    active: bool


class SupplierToggleOut(BaseModel):
    id: str
    code: str
    name: str
    active: bool


# ----------------------------- 下拉选项（T18 采购与供货页面） -----------------------------


class SupplierOptionView(BaseModel):
    """供应商下拉项：只给编码/名称，不携带联系人（下拉不需要，也免脱敏歧义）。"""

    id: UUID
    code: str
    name: str


class SupplierOptionListOut(BaseModel):
    items: list[SupplierOptionView] = Field(default_factory=list)


class SkuOptionView(BaseModel):
    """SKU 下拉项：用于建议行/采购行展示名称与手工补行。"""

    id: UUID
    code: str
    name: str
    brand_id: UUID
    # 参考进价（分/瓶，可空）：手工加行时预填含税单价，减少手输错价
    purchase_price_fen: int | None = None
    # 箱规（瓶/箱）：前端据此把瓶价换算成箱价展示并算预估总额
    case_spec: Decimal | None = None


class SkuOptionListOut(BaseModel):
    items: list[SkuOptionView] = Field(default_factory=list)


class InventorySummaryItem(BaseModel):
    """库存汇总行（T32）：箱数用字符串（Decimal 序列化口径，与采购行一致）。"""

    sku_id: str
    sku_code: str
    sku_name: str
    received_cases: str
    delivered_cases: str
    balance_cases: str


class InventorySummaryOut(BaseModel):
    items: list[InventorySummaryItem] = Field(default_factory=list)
