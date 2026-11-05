"""采购与供货持久化（T18，PRD §4.9）。Repository 只做持久化。"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    BeverageStockEntry,
    PurchaseOrder,
    PurchaseOrderLine,
    ReplenishmentOrder,
    Supplier,
    SupplierAuthorization,
    SupplyConfirmation,
)

# 补货“已确认”口径（R8：只有这些状态进采购建议分母）
_CONFIRMED_REPL_STATES = (
    "CONFIRMED", "PARTIALLY_FULFILLED", "FULFILLED", "CLOSED",
)
# 采购在途口径：已下给厂商但尚未收货，扣减建议量（避免重复采购）
_OPEN_PO_STATES = ("CONFIRMED", "SUPPLIER_CONFIRMED")


class PurchaseRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    # ---------------- 供应商/授权 ----------------

    def add_supplier(self, supplier: Supplier) -> Supplier:
        self.session.add(supplier)
        self.session.flush()
        return supplier

    def get_supplier(self, supplier_id: UUID) -> Supplier | None:
        return self.session.get(Supplier, supplier_id)

    def list_suppliers(self, dataset_id: UUID) -> list[Supplier]:
        """本数据范围内的在职供应商（下拉用，按编码稳定排序）。

        显式排除 DEMO：演示供应商不得进入正式采购单——建单接口
        （PURCHASE_DEMO_UPSTREAM_REJECTED）兜底之外，下拉直接不可选。
        """
        return list(
            self.session.scalars(
                select(Supplier)
                .where(Supplier.dataset_id == dataset_id)
                .where(Supplier.dataset_type == "PRODUCTION")
                .where(Supplier.active.is_(True))
                .order_by(Supplier.code.asc())
            ).all()
        )

    def list_suppliers_by_ids(self, supplier_ids) -> list[Supplier]:
        ids = set(supplier_ids)
        if not ids:
            return []
        return list(
            self.session.scalars(
                select(Supplier)
                .where(Supplier.id.in_(ids))
                .where(Supplier.active.is_(True))
                .order_by(Supplier.code.asc())
            ).all()
        )

    def list_all_suppliers(self, dataset_id: UUID) -> list[Supplier]:
        """管理台账：含停用，按编码排序。仅供 SETTINGS 角色使用。"""
        return list(
            self.session.scalars(
                select(Supplier)
                .where(Supplier.dataset_id == dataset_id)
                .where(Supplier.dataset_type == "PRODUCTION")
                .order_by(Supplier.code.asc())
            ).all()
        )

    def get_supplier_by_code(self, code: str, dataset_id: UUID) -> Supplier | None:
        return self.session.scalar(
            select(Supplier)
            .where(Supplier.code == code)
            .where(Supplier.dataset_id == dataset_id)
            .where(Supplier.dataset_type == "PRODUCTION")
        )

    def list_skus(self, dataset_id: UUID, *, brand_id: UUID | None = None) -> list:
        """本数据范围（可限定品牌）的 SKU 下拉项。"""
        from app.models import Sku

        stmt = select(Sku).where(Sku.dataset_id == dataset_id)
        if brand_id is not None:
            stmt = stmt.where(Sku.brand_id == brand_id)
        return list(self.session.scalars(stmt.order_by(Sku.code.asc())).all())

    def add_authorization(self, auth: SupplierAuthorization) -> None:
        self.session.add(auth)
        self.session.flush()

    def authorization(
        self, *, user_id: UUID, brand_id: UUID, region_id: UUID
    ) -> SupplierAuthorization | None:
        return self.session.scalar(
            select(SupplierAuthorization)
            .where(SupplierAuthorization.user_id == user_id)
            .where(SupplierAuthorization.brand_id == brand_id)
            .where(SupplierAuthorization.region_id == region_id)
        )

    def supplier_user_scopes(self, user_id: UUID) -> list[SupplierAuthorization]:
        return list(
            self.session.scalars(
                select(SupplierAuthorization).where(
                    SupplierAuthorization.user_id == user_id
                )
            ).all()
        )

    # ---------------- 采购单/行 ----------------

    def add_order(self, order: PurchaseOrder) -> PurchaseOrder:
        self.session.add(order)
        self.session.flush()
        return order

    def lock_order(self, order_id: UUID) -> PurchaseOrder | None:
        """FOR UPDATE：供货确认/取消串行化。"""
        return self.session.scalar(
            select(PurchaseOrder)
            .where(PurchaseOrder.id == order_id)
            .with_for_update()
        )

    def get_order_by_code(self, code: str) -> PurchaseOrder | None:
        """按采购单号取订单（T24 厂商按 procurement_code 确认）。"""
        return self.session.scalar(
            select(PurchaseOrder).where(PurchaseOrder.code == code)
        )

    def list_lines(self, order_id: UUID) -> list[PurchaseOrderLine]:
        return list(
            self.session.scalars(
                select(PurchaseOrderLine)
                .where(PurchaseOrderLine.order_id == order_id)
                .order_by(PurchaseOrderLine.created_at.asc())
            ).all()
        )

    def get_line(self, line_id: UUID) -> PurchaseOrderLine | None:
        return self.session.get(PurchaseOrderLine, line_id)

    def add_line(self, line: PurchaseOrderLine) -> PurchaseOrderLine:
        self.session.add(line)
        self.session.flush()
        return line

    # ---------------- 供货确认（幂等） ----------------

    def find_confirmation(
        self, *, line_id: UUID, supplier_user_id: UUID
    ) -> SupplyConfirmation | None:
        return self.session.scalar(
            select(SupplyConfirmation)
            .where(SupplyConfirmation.order_line_id == line_id)
            .where(SupplyConfirmation.supplier_user_id == supplier_user_id)
        )

    def find_confirmation_by_line_supplier(
        self, *, line_id: UUID, supplier_id: UUID
    ) -> SupplyConfirmation | None:
        """同一供应商组织对该行的确认（多账号共享，避免同事确认后仍显示待确认）。"""
        return self.session.scalar(
            select(SupplyConfirmation)
            .where(SupplyConfirmation.order_line_id == line_id)
            .where(SupplyConfirmation.supplier_id == supplier_id)
            .order_by(SupplyConfirmation.created_at.asc())
        )

    def add_confirmation(self, c: SupplyConfirmation) -> SupplyConfirmation:
        self.session.add(c)
        self.session.flush()
        return c

    def list_confirmations(self, order_id: UUID) -> list[SupplyConfirmation]:
        """某采购单的全部供货确认记录（含拒绝），按生成顺序。"""
        return list(
            self.session.scalars(
                select(SupplyConfirmation)
                .where(SupplyConfirmation.order_id == order_id)
                .order_by(SupplyConfirmation.created_at.asc())
            ).all()
        )

    def get_order(self, order_id: UUID) -> PurchaseOrder | None:
        """只读取单（不加行锁；需要并发裁决的场景仍走 lock_order）。"""
        return self.session.get(PurchaseOrder, order_id)

    # ---------------- 采购建议口径 ----------------

    def confirmed_demand_by_sku(
        self,
        *,
        dataset_id: UUID,
        brand_id: UUID,
        region_id: UUID,
        requested_from: date | None = None,
        requested_to: date | None = None,
    ) -> dict[UUID, Decimal]:
        """按 SKU 汇总已确认补货需求（committed - fulfilled = 尚未满足余额）。

        未确认的 DRAFT/SUBMITTED/REQUESTED 一律不取（R8/Q095）。
        返回 {sku_id: 未满足确认箱数}。
        """
        stmt = select(
            ReplenishmentOrder.sku_id,
            func.coalesce(
                func.sum(
                    ReplenishmentOrder.committed_boxes
                    - ReplenishmentOrder.fulfilled_boxes
                ),
                0,
            ),
        ).where(
            ReplenishmentOrder.dataset_id == dataset_id,
            ReplenishmentOrder.status.in_(_CONFIRMED_REPL_STATES),
        )
        # 商家归属的品牌/片区由补货行无直接品牌列：品牌经 SKU 关联，片区经商家关联。
        from app.models import Merchant, Sku

        stmt = (
            stmt.join(Sku, Sku.id == ReplenishmentOrder.sku_id)
            .join(Merchant, Merchant.id == ReplenishmentOrder.merchant_id)
            .where(Sku.brand_id == brand_id)
            .where(Merchant.region_id == region_id)
        )
        if requested_from is not None:
            stmt = stmt.where(ReplenishmentOrder.request_date >= requested_from)
        if requested_to is not None:
            stmt = stmt.where(ReplenishmentOrder.request_date <= requested_to)
        stmt = stmt.group_by(ReplenishmentOrder.sku_id)
        return {row[0]: row[1] for row in self.session.execute(stmt).all()}

    def open_purchase_by_sku(
        self, *, dataset_id: UUID, supplier_id: UUID
    ) -> dict:
        """该供应商在途采购（CONFIRMED/SUPPLIER_CONFIRMED）按 SKU 的箱数。"""
        stmt = (
            select(
                PurchaseOrderLine.sku_id,
                func.coalesce(func.sum(PurchaseOrderLine.quantity_cases), 0),
            )
            .join(PurchaseOrder, PurchaseOrder.id == PurchaseOrderLine.order_id)
            .where(
                PurchaseOrder.dataset_id == dataset_id,
                PurchaseOrder.supplier_id == supplier_id,
                PurchaseOrder.status.in_(_OPEN_PO_STATES),
            )
            .group_by(PurchaseOrderLine.sku_id)
        )
        return {row[0]: row[1] for row in self.session.execute(stmt).all()}

    def open_purchase_by_sku_scoped(
        self,
        *,
        dataset_id: UUID,
        brand_id: UUID,
        region_id: UUID,
        supplier_id: UUID | None = None,
    ) -> dict:
        """品牌/片区（可选供应商）维度的在途采购按 SKU 箱数（T25 采购建议）。

        不区分供应商时汇总该品牌/片区全部 CONFIRMED/SUPPLIER_CONFIRMED 在途量。
        只做持久化聚合，不做业务裁决。
        """
        stmt = (
            select(
                PurchaseOrderLine.sku_id,
                func.coalesce(func.sum(PurchaseOrderLine.quantity_cases), 0),
            )
            .join(PurchaseOrder, PurchaseOrder.id == PurchaseOrderLine.order_id)
            .where(
                PurchaseOrder.dataset_id == dataset_id,
                PurchaseOrder.brand_id == brand_id,
                PurchaseOrder.region_id == region_id,
                PurchaseOrder.status.in_(_OPEN_PO_STATES),
            )
            .group_by(PurchaseOrderLine.sku_id)
        )
        if supplier_id is not None:
            stmt = stmt.where(PurchaseOrder.supplier_id == supplier_id)
        return {row[0]: row[1] for row in self.session.execute(stmt).all()}

    def open_purchase_detail_scoped(
        self,
        *,
        dataset_id: UUID,
        brand_id: UUID,
        region_id: UUID,
        supplier_id: UUID | None = None,
        overdue_before: date | None = None,
    ) -> tuple[dict, dict]:
        """在途采购按 SKU 拆成（有效在途, 已超期在途）两份箱数。

        超期口径：采购单 `expected_date` 早于 `overdue_before`（通常 = 今天 − 宽限天数）
        且仍未收货。超期单不再压制采购建议，否则一张无人跟进的单会让商家一直断货。
        """
        stmt = (
            select(
                PurchaseOrderLine.sku_id,
                PurchaseOrder.expected_date,
                func.coalesce(func.sum(PurchaseOrderLine.quantity_cases), 0),
            )
            .join(PurchaseOrder, PurchaseOrder.id == PurchaseOrderLine.order_id)
            .where(
                PurchaseOrder.dataset_id == dataset_id,
                PurchaseOrder.brand_id == brand_id,
                PurchaseOrder.region_id == region_id,
                PurchaseOrder.status.in_(_OPEN_PO_STATES),
            )
            .group_by(PurchaseOrderLine.sku_id, PurchaseOrder.expected_date)
        )
        if supplier_id is not None:
            stmt = stmt.where(PurchaseOrder.supplier_id == supplier_id)

        active: dict = {}
        overdue: dict = {}
        for sku_id, expected_date, qty in self.session.execute(stmt).all():
            is_overdue = (
                overdue_before is not None
                and expected_date is not None
                and expected_date < overdue_before
            )
            bucket = overdue if is_overdue else active
            bucket[sku_id] = bucket.get(sku_id, 0) + qty
        return active, overdue

    # ---------------- 库存口径（T32 收发存台账） ----------------

    def stock_balance_by_sku(
        self,
        *,
        dataset_type: str,
        dataset_id: UUID,
        sku_ids: list[UUID] | None = None,
    ) -> dict[UUID, Decimal]:
        """按 SKU 汇总现存量（入库 − 出库 = 流水代数和）；无流水即 0。

        台账只记采购收货（正）与补货送达（负），可能因历史数据不全为负值，
        调用方需自行决定是否可抵扣需求。必须同时按 dataset_type 过滤：
        DEMO 与 PRODUCTION 可能共用同一个 dataset_id。
        """
        if sku_ids is not None and not sku_ids:
            return {}
        stmt = (
            select(
                BeverageStockEntry.sku_id,
                func.coalesce(func.sum(BeverageStockEntry.change_cases), 0),
            )
            .where(
                BeverageStockEntry.dataset_type == dataset_type,
                BeverageStockEntry.dataset_id == dataset_id,
            )
            .group_by(BeverageStockEntry.sku_id)
        )
        if sku_ids is not None:
            stmt = stmt.where(BeverageStockEntry.sku_id.in_(sku_ids))
        return {
            sku_id: Decimal(total or 0)
            for sku_id, total in self.session.execute(stmt).all()
        }
