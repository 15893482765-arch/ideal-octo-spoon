"""采购、供货与厂商门户数据模型（T18，PRD §4.9 / §5.1 / R17 / R18 / R33）。

设计口径：
- 采购建议（ProcurementService.build_suggestions）**不落表**，只是基于已确认
  补货需求（ReplenishmentOrder 已确认态，R8）+ 在途采购的计算结果；R17：
  “建议不等于采购单”；
- 采购单 PurchaseOrder + 行 PurchaseOrderLine：头表记供应商/品牌/片区/总金额，
  行记 SKU、箱数、**含税单价（分）**；总额永远由行重算，拒绝前端总额（Q096）；
- 供货确认 SupplyConfirmation：厂商仅对行确认供货量与预计到货日、上传凭证；
  确认不改 finance_transactions、不改库存（首版无库存域，Q100）；
- Supplier 为厂商主体（品牌+片区授权在 SupplierAuthorization），登录仍走
  users 表 + UserRegionBrandAuth（T02/T03 已实现），本模型只做业务档案；
- 全部业务表带 DatasetMixin：采购链任何上游事实必须 PRODUCTION（R33/R34），
  DEMO 上游入 PRODUCTION 采购由 Service 用 assert_same_dataset 拒绝。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Numeric,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import AuditMixin, Base, DatasetMixin, pk_uuid
from app.models.enums import PurchaseOrderStatus, SupplyConfirmationStatus

_PO_STATES = (
    "DRAFT", "CONFIRMED", "SUPPLIER_CONFIRMED",
    "SUPPLY_REJECTED", "RECEIVED", "CANCELLED",
)
_PO_STATUS_IN = ", ".join(f"'{s}'" for s in _PO_STATES)


class Supplier(Base, AuditMixin, DatasetMixin):
    """供应商/厂商业务档案（登录账号仍在 users 表，经 SupplierAuthorization 关联）。"""

    __tablename__ = "suppliers"
    __table_args__ = (
        UniqueConstraint(
            "dataset_type", "dataset_id", "code", name="uq_suppliers_dataset_code"
        ),
        CheckConstraint("char_length(name) > 0", name="suppliers_name_nonempty"),
        Index("ix_suppliers_dataset", "dataset_type", "dataset_id"),
    )

    id: Mapped[UUID] = pk_uuid()
    code: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    # 联系人/电话属内部信息：厂商账号之间互不可见，仅内部经理/财务可读（R18）
    contact_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    contact_phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    active: Mapped[bool] = mapped_column(
        nullable=False, default=True, server_default=text("true")
    )


class SupplierAuthorization(Base, AuditMixin, DatasetMixin):
    """厂商账号 × 品牌 × 片区行级授权（PRD §2.4 / R18：brand_id + region_id）。

    与 UserRegionBrandAuth 的区别：后者是登录侧通用授权，本张表明确“厂商主体—
    内部用户”的供货授权关系，供 ProcurementService 判厂商可见采购单。
    """

    __tablename__ = "supplier_authorizations"
    __table_args__ = (
        UniqueConstraint(
            "supplier_id", "user_id", "brand_id", "region_id",
            name="uq_supplier_auth_scope",
        ),
        Index("ix_supplier_auth_user", "user_id"),
    )

    id: Mapped[UUID] = pk_uuid()
    supplier_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("suppliers.id", ondelete="RESTRICT"),
        nullable=False,
    )
    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    brand_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("brands.id", ondelete="RESTRICT"),
        nullable=False,
    )
    region_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("regions.id", ondelete="RESTRICT"),
        nullable=False,
    )


class PurchaseOrder(Base, AuditMixin, DatasetMixin):
    """采购单（头）。total_amount_fen 永远由行项目服务端重算（Q096）。"""

    __tablename__ = "purchase_orders"
    __table_args__ = (
        UniqueConstraint(
            "dataset_type", "dataset_id", "code", name="uq_purchase_orders_dataset_code"
        ),
        CheckConstraint(f"status IN ({_PO_STATUS_IN})",
                        name="purchase_orders_status_check"),
        CheckConstraint("total_amount_fen >= 0",
                        name="purchase_orders_total_nonneg"),
        CheckConstraint(
            "cancelled_at IS NULL OR cancel_reason IS NOT NULL",
            name="purchase_orders_cancel_reason_required",
        ),
        Index("ix_purchase_orders_supplier", "supplier_id"),
        Index("ix_purchase_orders_brand_region", "brand_id", "region_id"),
        Index("ix_purchase_orders_status", "status"),
    )

    id: Mapped[UUID] = pk_uuid()
    code: Mapped[str] = mapped_column(String(32), nullable=False)
    supplier_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("suppliers.id", ondelete="RESTRICT"),
        nullable=False,
    )
    brand_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("brands.id", ondelete="RESTRICT"),
        nullable=False,
    )
    region_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("regions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    expected_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    # 头表金额为“行重算结果”的落账副本；任何建单/改单请求体中的总额一律忽略
    total_amount_fen: Mapped[int] = mapped_column(
        nullable=False, default=0, server_default=text("0")
    )
    status: Mapped[str] = mapped_column(
        String(24),
        nullable=False,
        default=PurchaseOrderStatus.DRAFT.value,
        server_default=text("'DRAFT'"),
    )
    created_by_id_internal: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
    )
    confirmed_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    confirmed_by_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
    )
    received_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    cancel_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    cancelled_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    note: Mapped[str | None] = mapped_column(String(500), nullable=True)


class PurchaseOrderLine(Base, AuditMixin, DatasetMixin):
    """采购行：SKU × 箱数 × 含税单价（分）。金额 = 箱数×单价，由服务端重算。"""

    __tablename__ = "purchase_order_lines"
    __table_args__ = (
        UniqueConstraint("order_id", "sku_id", name="uq_purchase_lines_order_sku"),
        CheckConstraint("quantity_cases > 0",
                        name="purchase_lines_qty_positive"),
        CheckConstraint("tax_inclusive_unit_price_fen >= 0",
                        name="purchase_lines_price_nonneg"),
        CheckConstraint("line_amount_fen >= 0",
                        name="purchase_lines_amount_nonneg"),
        Index("ix_purchase_lines_sku", "sku_id"),
    )

    id: Mapped[UUID] = pk_uuid()
    order_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("purchase_orders.id", ondelete="RESTRICT"),
        nullable=False,
    )
    sku_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("skus.id", ondelete="RESTRICT"),
        nullable=False,
    )
    quantity_cases: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    tax_inclusive_unit_price_fen: Mapped[int] = mapped_column(nullable=False)
    line_amount_fen: Mapped[int] = mapped_column(
        nullable=False, default=0, server_default=text("0")
    )
    # 建单依据：聚合自已确认补货需求的箱数（可审计“建议→采购单”的量从哪来）
    source_confirmed_boxes: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, default=Decimal("0"),
        server_default=text("0"),
    )


class SupplyConfirmation(Base, AuditMixin, DatasetMixin):
    """厂商供货确认（按行）。只记录供货量/预计到货/凭证；不产生付款、不入库（Q100）。

    幂等：(order_line_id, supplier_user_id) 唯一；重复确认返回原记录（Q101）。
    """

    __tablename__ = "supply_confirmations"
    __table_args__ = (
        UniqueConstraint(
            "order_line_id", "supplier_user_id",
            name="uq_supply_confirm_line_supplier",
        ),
        CheckConstraint(
            "status IN ('CONFIRMED','REJECTED')",
            name="supply_confirmations_status_check",
        ),
        CheckConstraint(
            "status <> 'CONFIRMED' OR (confirmed_quantity_cases IS NOT NULL "
            "AND confirmed_quantity_cases > 0 "
            "AND expected_delivery_date IS NOT NULL)",
            name="supply_confirmations_confirmed_fields",
        ),
        CheckConstraint(
            "status <> 'REJECTED' OR reject_reason IS NOT NULL",
            name="supply_confirmations_reject_reason_required",
        ),
        Index("ix_supply_confirmations_order", "order_id"),
    )

    id: Mapped[UUID] = pk_uuid()
    order_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("purchase_orders.id", ondelete="RESTRICT"),
        nullable=False,
    )
    order_line_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("purchase_order_lines.id", ondelete="RESTRICT"),
        nullable=False,
    )
    supplier_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("suppliers.id", ondelete="RESTRICT"),
        nullable=False,
    )
    supplier_user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=SupplyConfirmationStatus.CONFIRMED.value,
        server_default=text("'CONFIRMED'"),
    )
    confirmed_quantity_cases: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 2), nullable=True
    )
    expected_delivery_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    proof_file_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("file_objects.id", ondelete="RESTRICT"),
        nullable=True,
    )
    reject_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    note: Mapped[str | None] = mapped_column(String(500), nullable=True)
