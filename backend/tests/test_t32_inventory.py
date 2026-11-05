"""T32 饮料收发存台账（库存流水，补 Q100 首版无库存域缺口）。

- 入库：采购单收货按行记账，数量取厂商 CONFIRMED 确认供货量（非订购量）；
- 出库：补货「登记送达」按履约事件记账；
- 幂等：重复收货不重复入库；同幂等键重复履约不重复出库；
- 汇总：按 SKU 输出累计入库/出库/现存总和清单，仅内部角色，厂商 403。
"""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from tests.test_t09_contract import _world
from tests.test_t18_procurement import (
    EXPECTED_DAY,
    PROMISED_DAY,
    _confirmed_replenishment,
    _internal_actor,
    _sku,
    _supplier_world,
)

from app.core.config import get_settings
from app.core.errors import PermissionDeniedError
from app.models import BeverageStockEntry, PurchaseOrderLine
from app.models.enums import RoleCode
from app.services.inventory_service import InventoryService
from app.services.procurement_service import ProcurementService
from app.services.replenishment_service import ReplenishmentService


@pytest.fixture(autouse=True)
def portal_enabled(monkeypatch):
    """开启厂商门户（confirm_supply 的前置，同 t18）。"""
    get_settings.cache_clear()
    monkeypatch.setenv("SUPPLIER_PORTAL_ENABLED", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _received_order(session: Session, w: dict, sku, order_qty: str, confirm_qty: str):
    """建单 → 厂商确认 → 收货闭环，返回 (order, fin_actor)。"""
    supplier, _su, supplier_actor = _supplier_world(
        session, w, f"t32-{uuid4().hex[:5]}")
    _fin, fin_actor = _internal_actor(
        session, w, RoleCode.FINANCE, f"t32-fin-{uuid4().hex[:5]}")
    svc = ProcurementService(session)
    order = svc.create_purchase_order(
        fin_actor,
        supplier_id=supplier.id,
        brand_id=w["brand"].id,
        region_id=w["region"].id,
        expected_date=EXPECTED_DAY,
        lines=[(sku.id, Decimal(order_qty), 3500)],
    )
    line = session.scalars(
        select(PurchaseOrderLine).where(PurchaseOrderLine.order_id == order.id)
    ).first()
    svc.confirm_supply(
        supplier_actor, order_id=order.id, line_id=line.id,
        confirmed_quantity_cases=Decimal(confirm_qty),
        expected_delivery_date=EXPECTED_DAY,
    )
    svc.receive_purchase_order(fin_actor, order_id=order.id)
    return order, fin_actor


def _summary_row(session: Session, actor, sku_id) -> dict:
    items = InventoryService(session).summary(actor)
    return next(i for i in items if i["sku_id"] == str(sku_id))


# ----------------------------- 入库 -----------------------------


def test_receive_records_inbound_by_confirmed_quantity(session: Session) -> None:
    """收货入库：数量取厂商确认供货量（8），不是订购量（10）。"""
    w = _world(session)
    sku = _sku(session, w, "T32A")
    _order, fin_actor = _received_order(session, w, sku, "10", "8")

    row = _summary_row(session, fin_actor, sku.id)
    assert Decimal(row["received_cases"]) == Decimal("8")
    assert Decimal(row["delivered_cases"]) == Decimal("0")
    assert Decimal(row["balance_cases"]) == Decimal("8")
    assert row["sku_code"] == sku.code
    assert row["sku_name"] == sku.name


def test_receive_is_idempotent_in_ledger(session: Session) -> None:
    """重复收货（状态机早退）不重复记账。"""
    w = _world(session)
    sku = _sku(session, w, "T32B")
    order, fin_actor = _received_order(session, w, sku, "10", "10")

    before = len(session.scalars(select(BeverageStockEntry)).all())
    ProcurementService(session).receive_purchase_order(fin_actor, order_id=order.id)
    after = len(session.scalars(select(BeverageStockEntry)).all())
    assert before == after == 1


# ----------------------------- 出库与现存 -----------------------------


def test_fulfillment_records_outbound_and_balance(session: Session) -> None:
    """登记送达后库存减少：现存 = 入库 10 − 送达 4 = 6。"""
    w = _world(session)
    sku = _sku(session, w, "T32C")
    _order, fin_actor = _received_order(session, w, sku, "10", "10")

    repl = _confirmed_replenishment(session, w, sku, "6")
    _mgr, mgr_actor = _internal_actor(
        session, w, RoleCode.REGIONAL_MANAGER, f"t32-mgr-{uuid4().hex[:5]}")
    ReplenishmentService(session).register_fulfillment(
        mgr_actor, order_id=repl.id, delivered_boxes=Decimal("4"),
        delivered_on=PROMISED_DAY, idempotency_key=f"t32-{uuid4().hex[:8]}",
    )

    row = _summary_row(session, fin_actor, sku.id)
    assert Decimal(row["received_cases"]) == Decimal("10")
    assert Decimal(row["delivered_cases"]) == Decimal("4")
    assert Decimal(row["balance_cases"]) == Decimal("6")


def test_fulfillment_idempotency_not_double_counted(session: Session) -> None:
    """同幂等键重复登记送达：返回原事件，台账只扣一次。"""
    w = _world(session)
    sku = _sku(session, w, "T32D")
    repl = _confirmed_replenishment(session, w, sku, "6")
    _mgr, mgr_actor = _internal_actor(
        session, w, RoleCode.REGIONAL_MANAGER, f"t32-mgr-{uuid4().hex[:5]}")
    svc = ReplenishmentService(session)
    key = f"t32-{uuid4().hex[:8]}"
    first = svc.register_fulfillment(
        mgr_actor, order_id=repl.id, delivered_boxes=Decimal("4"),
        delivered_on=PROMISED_DAY, idempotency_key=key)
    second = svc.register_fulfillment(
        mgr_actor, order_id=repl.id, delivered_boxes=Decimal("4"),
        delivered_on=PROMISED_DAY, idempotency_key=key)
    assert second.id == first.id

    entries = session.scalars(
        select(BeverageStockEntry).where(BeverageStockEntry.sku_id == sku.id)
    ).all()
    assert len(entries) == 1
    assert entries[0].change_cases == Decimal("-4")


# ----------------------------- 权限与空态 -----------------------------


def test_summary_forbidden_for_supplier(session: Session) -> None:
    """厂商不可查库存汇总（R18 延伸：库存属内部数据）。"""
    w = _world(session)
    _supplier, _su, supplier_actor = _supplier_world(
        session, w, f"t32-{uuid4().hex[:5]}")
    with pytest.raises(PermissionDeniedError):
        InventoryService(session).summary(supplier_actor)


def test_summary_empty_when_no_entries(session: Session) -> None:
    w = _world(session)
    _mgr, mgr_actor = _internal_actor(
        session, w, RoleCode.REGIONAL_MANAGER, f"t32-mgr-{uuid4().hex[:5]}")
    assert InventoryService(session).summary(mgr_actor) == []
