"""T36：采购建议收敛、含税单价自动生成、送达毛利（建议零售价 − 进价）。

覆盖：
- 已有采购单（在途）完全覆盖确认需求时，建议行消失，避免重复下单；
- 建单可省略含税单价，服务端按 SKU 参考进价自动生成；无参考进价则明确报错；
- 送货给商家即固化成本与毛利：主数据价格按元/瓶，金额 = 箱数 × 箱规 × 单价；
- 同一履约幂等键重复登记只产生一笔销量确认与一笔收入流水。
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from tests.test_t09_contract import _world
from tests.test_t18_procurement import (
    _confirmed_replenishment,
    _internal_actor,
    _sku,
    _supplier_world,
)

from app.core.errors import DomainError
from app.models import (
    FinanceTransaction,
    PurchaseOrder,
    PurchaseOrderLine,
    SalesConfirmation,
    Sku,
)
from app.models.enums import PurchaseOrderStatus, RoleCode
from app.services.procurement_service import ProcurementService
from app.services.replenishment_service import ReplenishmentService

DELIVERED_ON = date(2026, 12, 18)


def _add_open_po(
    session: Session,
    w,
    sku,
    supplier,
    boxes: str,
    *,
    expected_date: date = date(2099, 12, 25),
):
    po = PurchaseOrder(
        code=f"PO-{uuid4().hex[:12]}",
        supplier_id=supplier.id,
        brand_id=w["brand"].id,
        region_id=w["region"].id,
        status=PurchaseOrderStatus.CONFIRMED.value,
        total_amount_fen=0,
        expected_date=expected_date,
        dataset_type="PRODUCTION",
        dataset_id=w["ds"],
    )
    session.add(po)
    session.flush()
    session.add(
        PurchaseOrderLine(
            order_id=po.id,
            sku_id=sku.id,
            quantity_cases=Decimal(boxes),
            tax_inclusive_unit_price_fen=5850,
            line_amount_fen=int(Decimal(boxes) * 5850),
            dataset_type="PRODUCTION",
            dataset_id=w["ds"],
        )
    )
    session.flush()
    return po


# -------------------------- 建议收敛 --------------------------


def test_suggestion_hides_sku_already_covered_by_open_po(session: Session) -> None:
    w = _world(session)
    sku = _sku(session, w, "t36covered")
    _confirmed_replenishment(session, w, sku, "10")
    supplier, _su, _sa = _supplier_world(session, w, "t36sup1")
    _mgr, actor = _internal_actor(
        session, w, RoleCode.REGIONAL_MANAGER, "t36mgr1"
    )

    svc = ProcurementService(session)
    # 未下单：建议 10 箱
    before = svc.build_suggestions(
        actor, brand_id=w["brand"].id, region_id=w["region"].id
    )
    assert [line.suggested_purchase_boxes for line in before] == [Decimal("10")]

    # 在途采购完全覆盖 → 该 SKU 从建议里消失（不再显示“建议采购 0”）
    _add_open_po(session, w, sku, supplier, "10")
    after = svc.build_suggestions(
        actor, brand_id=w["brand"].id, region_id=w["region"].id
    )
    assert after == []

    # 但"为什么没有建议"必须可解释：显式请求覆盖行时返回并标记
    covered = svc.build_suggestions(
        actor,
        brand_id=w["brand"].id,
        region_id=w["region"].id,
        include_covered=True,
    )
    assert len(covered) == 1
    assert covered[0].suggested_purchase_boxes == Decimal("0")
    assert covered[0].covered_by_open_purchase is True
    assert covered[0].open_purchase_boxes == Decimal("10")


def test_overdue_open_purchase_no_longer_suppresses_suggestion(session: Session) -> None:
    """在途单超过预计到货日仍未收货 → 不再压制建议，避免商家一直断货。"""
    w = _world(session)
    sku = _sku(session, w, "t36overdue")
    _confirmed_replenishment(session, w, sku, "6")
    supplier, _su, _sa = _supplier_world(session, w, "t36sup4")
    # 预计到货日已是 30 天前，仍停在 CONFIRMED（没人跟进收货）
    _add_open_po(
        session, w, sku, supplier, "6",
        expected_date=date.today() - timedelta(days=30),
    )
    _mgr, actor = _internal_actor(
        session, w, RoleCode.REGIONAL_MANAGER, "t36mgr6"
    )

    rows = ProcurementService(session).build_suggestions(
        actor, brand_id=w["brand"].id, region_id=w["region"].id
    )
    assert len(rows) == 1
    line = rows[0]
    assert line.suggested_purchase_boxes == Decimal("6")  # 超期单不扣减
    assert line.overdue_purchase_boxes == Decimal("6")
    assert line.open_purchase_boxes == Decimal("0")


def test_suggestion_diagnostics_explains_empty_list(session: Session) -> None:
    """诊断口径：需求 / 在途 / 超期 / 建议量 一目了然，空态不再是"疑似故障"。"""
    w = _world(session)
    sku = _sku(session, w, "t36diag")
    _confirmed_replenishment(session, w, sku, "10")
    supplier, _su, _sa = _supplier_world(session, w, "t36sup5")
    _add_open_po(session, w, sku, supplier, "10")
    _mgr, actor = _internal_actor(
        session, w, RoleCode.REGIONAL_MANAGER, "t36mgr7"
    )

    diag = ProcurementService(session).suggestion_diagnostics(
        actor, brand_id=w["brand"].id, region_id=w["region"].id
    )
    assert diag["confirmed_sku_count"] == 1
    assert diag["confirmed_unmet_boxes"] == Decimal("10")
    assert diag["open_purchase_boxes"] == Decimal("10")
    assert diag["suggested_purchase_boxes"] == Decimal("0")
    assert diag["covered_sku_count"] == 1
    assert diag["overdue_purchase_boxes"] == Decimal("0")


# -------------------------- 含税单价自动生成 --------------------------


def test_purchase_order_auto_price_from_master_data(session: Session) -> None:
    w = _world(session)
    sku = _sku(session, w, "t36price")
    sku.purchase_price_fen = 5850
    session.flush()
    supplier, _su, _sa = _supplier_world(session, w, "t36sup2")
    _mgr, actor = _internal_actor(
        session, w, RoleCode.REGIONAL_MANAGER, "t36mgr2"
    )

    order = ProcurementService(session).create_purchase_order(
        actor,
        supplier_id=supplier.id,
        brand_id=w["brand"].id,
        region_id=w["region"].id,
        expected_date=None,
        lines=[(sku.id, Decimal("2"), None)],  # 单价留空 → 取参考进价
    )
    line = session.scalars(
        select(PurchaseOrderLine).where(PurchaseOrderLine.order_id == order.id)
    ).one()
    assert line.tax_inclusive_unit_price_fen == 5850
    # 单价是元/瓶（58.5 元），2 箱 × 24 瓶 = 48 瓶
    assert line.line_amount_fen == 280_800
    assert order.total_amount_fen == 280_800


def test_purchase_order_without_reference_price_requires_manual_price(
    session: Session,
) -> None:
    w = _world(session)
    sku = _sku(session, w, "t36noprice")
    assert sku.purchase_price_fen is None
    supplier, _su, _sa = _supplier_world(session, w, "t36sup3")
    _mgr, actor = _internal_actor(
        session, w, RoleCode.REGIONAL_MANAGER, "t36mgr3"
    )

    with pytest.raises(DomainError) as exc:
        ProcurementService(session).create_purchase_order(
            actor,
            supplier_id=supplier.id,
            brand_id=w["brand"].id,
            region_id=w["region"].id,
            expected_date=None,
            lines=[(sku.id, Decimal("2"), None)],
        )
    assert exc.value.code == "PURCHASE_PRICE_REQUIRED"
    # 手填单价仍可正常建单
    order = ProcurementService(session).create_purchase_order(
        actor,
        supplier_id=supplier.id,
        brand_id=w["brand"].id,
        region_id=w["region"].id,
        expected_date=None,
        lines=[(sku.id, Decimal("2"), 6200)],
    )
    assert order.total_amount_fen == 297_600  # 2 箱 × 24 瓶 × 62 元/瓶


# -------------------------- 送达毛利 --------------------------


def test_delivery_confirmation_freezes_margin(session: Session) -> None:
    w = _world(session)
    sku = _sku(session, w, "t36margin")
    sku.suggested_price_fen = 8000   # 建议零售价 80 元/瓶（箱规 24）
    sku.purchase_price_fen = 5850    # 参考进价 58.5 元/瓶
    session.flush()
    order = _confirmed_replenishment(session, w, sku, "10")
    mgr, actor = _internal_actor(session, w, RoleCode.REGIONAL_MANAGER, "t36mgr4")

    svc = ReplenishmentService(session)
    event = svc.register_fulfillment(
        actor,
        order_id=order.id,
        delivered_boxes=Decimal("4"),
        delivered_on=DELIVERED_ON,
        idempotency_key="t36-margin-key-1",
        proof_file_id=None,
        note="送达毛利用例",
    )

    confirmation = session.scalars(
        select(SalesConfirmation).where(
            SalesConfirmation.source_fulfillment_id == event.id
        )
    ).one()
    # 4 箱 × 24 瓶 = 96 瓶：收入 96×80 元，成本 96×58.5 元，毛利 96×(80−58.5)
    assert confirmation.amount_fen == 768_000
    assert confirmation.cost_fen == 561_600
    assert confirmation.profit_fen == 206_400

    income = session.scalars(
        select(FinanceTransaction).where(
            FinanceTransaction.related_object_id == confirmation.id
        )
    ).one()
    assert income.tx_type == "INCOME"
    assert income.amount_fen == 768_000

    # 明细回显（前端履约流水列）
    detail = svc.get_order_detail(actor, order_id=order.id)
    sales = detail["sales_by_fulfillment"][event.id]
    assert sales.profit_fen == 206_400

    # 幂等：同一幂等键重复登记不产生第二条确认/收入
    again = svc.register_fulfillment(
        actor,
        order_id=order.id,
        delivered_boxes=Decimal("4"),
        delivered_on=DELIVERED_ON,
        idempotency_key="t36-margin-key-1",
        proof_file_id=None,
        note="重复提交",
    )
    assert again.id == event.id
    assert (
        len(
            session.scalars(
                select(SalesConfirmation).where(
                    SalesConfirmation.merchant_id == order.merchant_id
                )
            ).all()
        )
        == 1
    )


def test_delivery_without_reference_price_has_zero_cost(session: Session) -> None:
    w = _world(session)
    sku = _sku(session, w, "t36nocost")
    sku.suggested_price_fen = 5000
    assert sku.purchase_price_fen is None
    session.flush()
    order = _confirmed_replenishment(session, w, sku, "3")
    _mgr, actor = _internal_actor(session, w, RoleCode.REGIONAL_MANAGER, "t36mgr5")

    event = ReplenishmentService(session).register_fulfillment(
        actor,
        order_id=order.id,
        delivered_boxes=Decimal("2"),
        delivered_on=DELIVERED_ON,
        idempotency_key="t36-nocost-key",
        proof_file_id=None,
        note=None,
    )
    confirmation = session.scalars(
        select(SalesConfirmation).where(
            SalesConfirmation.source_fulfillment_id == event.id
        )
    ).one()
    # 进价未维护：不臆造成本，毛利等于收入（2 箱 × 24 瓶 × 50 元/瓶）
    assert confirmation.cost_fen == 0
    assert confirmation.profit_fen == 240_000


def test_sku_without_purchase_price_is_listed_for_master_data(
    session: Session,
) -> None:
    """回归：SKU 参考进价缺失可被识别（前端据此提示先补主数据）。"""
    w = _world(session)
    sku = _sku(session, w, "t36missing")
    assert isinstance(sku, Sku)
    assert sku.purchase_price_fen is None
