"""饮料库存服务（T32：收发存台账）。

口径：
- 入库：采购单收货时按行记账，数量取该行厂商 CONFIRMED 的确认供货量
  （receive 前置已校验每行均有确认）；同一采购单重复调用幂等跳过；
- 出库：补货送达履约按事件记账，数量 = 本次送达箱数；履约幂等键防重之上，
  (source_type, source_id, sku_id) 唯一约束兜底；
- 汇总：现存量 = 入库合计 − 出库合计（流水代数和），按 SKU 输出总和清单；
  仅内部角色可查（厂商只见授权品牌×片区单据，库存属内部数据，R18 延伸）。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.permissions import (
    MODULE_PROCUREMENT,
    ActorContext,
    PermissionService,
    RoleCode,
)
from app.core.errors import PermissionDeniedError
from app.models import (
    BeverageStockEntry,
    PurchaseOrder,
    PurchaseOrderLine,
    ReplenishmentFulfillment,
    ReplenishmentOrder,
    Sku,
    SupplyConfirmation,
    User,
)
from app.models.enums import SupplyConfirmationStatus
from app.models.inventory import SOURCE_PURCHASE_RECEIVE, SOURCE_REPLENISH_FULFILL


class InventoryService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.permissions = PermissionService()

    # ============================ 记账 ============================

    def record_purchase_receipt(
        self, actor: ActorContext, order: PurchaseOrder, *, biz_date: date
    ) -> int:
        """采购收货入库：按行记正流水，返回记账条数（幂等，已记过返回 0）。"""
        count = 0
        lines = self.session.execute(
            select(PurchaseOrderLine).where(PurchaseOrderLine.order_id == order.id)
        ).scalars().all()
        for line in lines:
            if self._exists(SOURCE_PURCHASE_RECEIVE, order.id, line.sku_id):
                continue
            confirmed = self._confirmed_quantity(order.id, line.id)
            qty = confirmed if confirmed is not None else Decimal(line.quantity_cases)
            self.session.add(
                BeverageStockEntry(
                    sku_id=line.sku_id,
                    change_cases=qty,
                    source_type=SOURCE_PURCHASE_RECEIVE,
                    source_id=order.id,
                    biz_date=biz_date,
                    note=f"采购单 {order.code} 收货入库",
                    registered_by=actor.user_id,
                    dataset_type=order.dataset_type,
                    dataset_id=order.dataset_id,
                )
            )
            count += 1
        if count:
            self.session.flush()
        return count

    def record_replenishment_fulfillment(
        self,
        actor: ActorContext,
        event: ReplenishmentFulfillment,
        order: ReplenishmentOrder,
    ) -> bool:
        """补货送达出库：按履约事件记负流水（幂等，已记过返回 False）。"""
        if self._exists(SOURCE_REPLENISH_FULFILL, event.id, order.sku_id):
            return False
        self.session.add(
            BeverageStockEntry(
                sku_id=order.sku_id,
                change_cases=-Decimal(event.delivered_boxes),
                source_type=SOURCE_REPLENISH_FULFILL,
                source_id=event.id,
                biz_date=event.delivered_on,
                note=f"补货单 {order.code} 送达出库",
                registered_by=actor.user_id,
                dataset_type=order.dataset_type,
                dataset_id=order.dataset_id,
            )
        )
        self.session.flush()
        return True

    # ============================ 汇总 ============================

    def summary(self, actor: ActorContext) -> list[dict[str, Any]]:
        """按 SKU 的收发存总和清单（仅内部角色；厂商无权看库存）。"""
        self.permissions.require_ability(actor, MODULE_PROCUREMENT)
        if actor.role_code == RoleCode.SUPPLIER.value:
            raise PermissionDeniedError(
                code="INVENTORY_SUPPLIER_FORBIDDEN",
                message="库存汇总仅内部角色可查",
            )
        me = self.session.get(User, actor.user_id)
        if me is None:
            return []
        rows = self.session.execute(
            select(
                BeverageStockEntry.sku_id,
                func.coalesce(
                    func.sum(BeverageStockEntry.change_cases).filter(
                        BeverageStockEntry.change_cases > 0
                    ),
                    0,
                ).label("received"),
                func.coalesce(
                    -func.sum(BeverageStockEntry.change_cases).filter(
                        BeverageStockEntry.change_cases < 0
                    ),
                    0,
                ).label("delivered"),
                func.coalesce(func.sum(BeverageStockEntry.change_cases), 0).label(
                    "balance"
                ),
            )
            .where(
                BeverageStockEntry.dataset_type == me.dataset_type,
                BeverageStockEntry.dataset_id == me.dataset_id,
            )
            .group_by(BeverageStockEntry.sku_id)
        ).all()
        if not rows:
            return []
        skus = {
            sku.id: sku
            for sku in self.session.execute(
                select(Sku).where(Sku.id.in_([r.sku_id for r in rows]))
            )
            .scalars()
            .all()
        }
        items = [
            {
                "sku_id": str(r.sku_id),
                "sku_code": skus[r.sku_id].code if r.sku_id in skus else "",
                "sku_name": skus[r.sku_id].name if r.sku_id in skus else "",
                "received_cases": str(r.received),
                "delivered_cases": str(r.delivered),
                "balance_cases": str(r.balance),
            }
            for r in rows
        ]
        items.sort(key=lambda item: item["sku_code"])
        return items

    # ============================ 内部 ============================

    def _exists(self, source_type: str, source_id: UUID, sku_id: UUID) -> bool:
        return (
            self.session.execute(
                select(BeverageStockEntry.id).where(
                    BeverageStockEntry.source_type == source_type,
                    BeverageStockEntry.source_id == source_id,
                    BeverageStockEntry.sku_id == sku_id,
                )
            ).first()
            is not None
        )

    def _confirmed_quantity(
        self, order_id: UUID, line_id: UUID
    ) -> Decimal | None:
        """该行厂商 CONFIRMED 的确认供货量（首条；receive 前置已保证存在）。"""
        row = self.session.execute(
            select(SupplyConfirmation.confirmed_quantity_cases)
            .where(
                SupplyConfirmation.order_id == order_id,
                SupplyConfirmation.order_line_id == line_id,
                SupplyConfirmation.status == SupplyConfirmationStatus.CONFIRMED.value,
            )
            .order_by(SupplyConfirmation.created_at)
            .limit(1)
        ).first()
        return Decimal(row[0]) if row and row[0] is not None else None
