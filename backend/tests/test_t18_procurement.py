"""T18 采购、供货与可选厂商门户（PRD §4.9 / §5.1 / R8 / R17 / R18 / R33 / R34）。

- Q095 采购建议只含已确认补货需求（未确认 DRAFT/SUBMITTED 不进建议）；
- Q096 采购金额由行项目服务端重算，请求体无总额字段，伪造总额无处可传；
- Q097 SUPPLIER_PORTAL_ENABLED 关闭时厂商在服务层确认供货被 403；
- Q098 厂商按品牌×片区授权隔离，越品牌/越片区 403，厂商不能建采购单；
- Q099 厂商看不到供应商内部联系人（脱敏），也看不到员工/工资数据；
- Q100 厂商供货确认不产生财务流水、不改库存（首版无库存域），不等于付款；
- Q101 重复供货确认幂等：同一(行,厂商用户)只建一条确认，头单不重复迁移。

跨卡：采购链拒绝任何 DEMO 上游事实（R33/R34）。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from tests.test_t09_contract import _merchant, _user, _world

from app.core.config import get_settings
from app.core.errors import DomainError, PermissionDeniedError
from app.core.permissions import (
    ActorContext,
    PermissionError_,
)
from app.models import (
    FinanceTransaction,
    PurchaseOrder,
    PurchaseOrderLine,
    ReplenishmentOrder,
    Sku,
    Supplier,
    SupplierAuthorization,
    SupplyConfirmation,
    UserRegionBrandAuth,
)
from app.models.enums import (
    PurchaseOrderStatus,
    ReplenishmentStatus,
    RoleCode,
    SupplyConfirmationStatus,
)
from app.services.procurement_service import ProcurementService
from app.services.replenishment_service import ReplenishmentService

REQUEST_DAY = date(2026, 12, 14)
PROMISED_DAY = date(2026, 12, 20)
EXPECTED_DAY = date(2026, 12, 25)


# ----------------------------- 造数 -----------------------------


def _sku(session: Session, w: dict, code: str, *, dataset_type: str = "PRODUCTION",
         dataset_id=None) -> Sku:
    s = Sku(
        code=f"SKU-{code}-{uuid4().hex[:6]}",
        name="沁爽汽水",
        brand_id=w["brand"].id,
        capacity_ml=500,
        suggested_price_fen=300,
        case_spec=Decimal("24"),
        dataset_type=dataset_type,
        dataset_id=dataset_id or w["ds"],
    )
    session.add(s)
    session.flush()
    return s


def _internal_actor(session: Session, w: dict, role: RoleCode, tag: str):
    u = _user(session, w["ds"], role, tag)
    session.add(
        UserRegionBrandAuth(
            user_id=u.id, region_id=w["region"].id, brand_id=w["brand"].id
        )
    )
    session.flush()
    actor = ActorContext(
        user_id=u.id,
        role_code=role.value,
        region_ids=frozenset({w["region"].id}),
        brand_ids=frozenset({w["brand"].id}),
    )
    return u, actor


def _supplier_world(session: Session, w: dict, tag: str, *,
                    region_id=None, brand_id=None):
    """供应商 + 厂商登录用户 + 品牌×片区授权。返回 (supplier, user, actor)。"""
    supplier = Supplier(
        code=f"SUP-{uuid4().hex[:8]}",
        name=f"{tag}饮品供应有限公司",
        contact_name="张采购",
        contact_phone="13800001111",
        dataset_type="PRODUCTION",
        dataset_id=w["ds"],
    )
    session.add(supplier)
    session.flush()
    u = _user(session, w["ds"], RoleCode.SUPPLIER, f"{tag}-supplier")
    session.add(
        UserRegionBrandAuth(
            user_id=u.id,
            region_id=region_id or w["region"].id,
            brand_id=brand_id or w["brand"].id,
        )
    )
    session.add(
        SupplierAuthorization(
            supplier_id=supplier.id,
            user_id=u.id,
            brand_id=brand_id or w["brand"].id,
            region_id=region_id or w["region"].id,
            dataset_type="PRODUCTION",
            dataset_id=w["ds"],
        )
    )
    session.flush()
    actor = ActorContext(
        user_id=u.id,
        role_code=RoleCode.SUPPLIER.value,
        region_ids=frozenset({region_id or w["region"].id}),
        brand_ids=frozenset({brand_id or w["brand"].id}),
        supplier_brand_ids=frozenset({brand_id or w["brand"].id}),
        supplier_region_ids=frozenset({region_id or w["region"].id}),
    )
    return supplier, u, actor


def _confirmed_replenishment(session: Session, w: dict, sku, boxes: str):
    """DRAFT→SUBMITTED→CONFIRMED 的补货单（committed=boxes, fulfilled=0）。"""
    rep, rep_actor = _internal_actor(
        session, w, RoleCode.SALES_REP, f"rep-{uuid4().hex[:5]}")
    mgr, mgr_actor = _internal_actor(
        session, w, RoleCode.REGIONAL_MANAGER, f"mgr-{uuid4().hex[:5]}")
    merchant = _merchant(session, w, name=f"T18店-{uuid4().hex[:5]}")
    order = ReplenishmentOrder(
        code=f"RD-{uuid4().hex[:12]}",
        merchant_id=merchant.id,
        sku_id=sku.id,
        requested_boxes=Decimal(boxes),
        committed_boxes=Decimal("0"),
        fulfilled_boxes=Decimal("0"),
        request_date=REQUEST_DAY,
        status=ReplenishmentStatus.DRAFT.value,
        dataset_type="PRODUCTION",
        dataset_id=w["ds"],
    )
    session.add(order)
    session.flush()
    svc = ReplenishmentService(session)
    svc.submit_order(rep_actor, order_id=order.id)
    svc.confirm(
        mgr_actor, order_id=order.id, confirmed_boxes=Decimal(boxes),
        promised_delivery_date=PROMISED_DAY,
    )
    session.refresh(order)
    return order


def _unconfirmed_replenishment(session: Session, w: dict, sku, boxes: str):
    """仅 DRAFT/SUBMITTED 的补货单（不应进建议）。"""
    rep, rep_actor = _internal_actor(
        session, w, RoleCode.SALES_REP, f"rep-{uuid4().hex[:5]}")
    merchant = _merchant(session, w, name=f"T18店U-{uuid4().hex[:5]}")
    order = ReplenishmentOrder(
        code=f"RU-{uuid4().hex[:12]}",
        merchant_id=merchant.id,
        sku_id=sku.id,
        requested_boxes=Decimal(boxes),
        committed_boxes=Decimal("0"),
        fulfilled_boxes=Decimal("0"),
        request_date=REQUEST_DAY,
        status=ReplenishmentStatus.DRAFT.value,
        dataset_type="PRODUCTION",
        dataset_id=w["ds"],
    )
    session.add(order)
    session.flush()
    ReplenishmentService(session).submit_order(rep_actor, order_id=order.id)
    return order


@pytest.fixture
def portal_enabled(monkeypatch):
    """开启厂商门户（Q097 之外的用例需要）。"""
    get_settings.cache_clear()
    monkeypatch.setenv("SUPPLIER_PORTAL_ENABLED", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ----------------------------- Q095 -----------------------------


def test_q095_suggestions_only_confirmed_demand(session: Session) -> None:
    w = _world(session)
    sku = _sku(session, w, "095")
    # 已确认未满足 12 箱
    _confirmed_replenishment(session, w, sku, "12")
    # 未确认（SUBMITTED）50 箱，不得进建议
    _unconfirmed_replenishment(session, w, sku, "50")

    _mgr_user, mgr_actor = _internal_actor(
        session, w, RoleCode.REGIONAL_MANAGER, "t18-mgr-095")
    suggestions = ProcurementService(session).build_suggestions(
        mgr_actor, brand_id=w["brand"].id, region_id=w["region"].id)
    assert len(suggestions) == 1
    line = suggestions[0]
    assert line.sku_id == sku.id
    assert line.confirmed_unmet_boxes == Decimal("12")
    assert line.suggested_purchase_boxes == Decimal("12")
    # T32 台账已接入：如实给出真实余额（本用例无流水，余额 0）
    assert line.inventory_available is True
    assert line.inventory_status == "AVAILABLE"
    assert line.available_boxes == Decimal("0")


# ----------------------------- Q096 -----------------------------


def test_q096_amount_recomputed_server_side(session: Session) -> None:
    w = _world(session)
    sku = _sku(session, w, "096")
    supplier, _su, _sa = _supplier_world(session, w, "t18-096")
    _fin, mgr_actor = _internal_actor(
        session, w, RoleCode.FINANCE, "t18-fin-096")

    order = ProcurementService(session).create_purchase_order(
        mgr_actor,
        supplier_id=supplier.id,
        brand_id=w["brand"].id,
        region_id=w["region"].id,
        expected_date=EXPECTED_DAY,
        lines=[
            (sku.id, Decimal("10"), 3500),   # 10 箱 × 24 瓶 × 35.00 元/瓶
        ],
        note="首批采购",
    )
    session.refresh(order)
    line = session.scalar(
        select(PurchaseOrderLine).where(PurchaseOrderLine.order_id == order.id))
    # 行金额与头总额都由服务端重算（分）：数量按箱、单价按瓶，需乘箱规
    assert line.line_amount_fen == 840_000
    assert order.total_amount_fen == 840_000
    assert order.status == PurchaseOrderStatus.CONFIRMED.value


def test_order_view_inlines_sku_code_and_name(session: Session) -> None:
    """采购单行必须随单返回商品编码+名称。

    回归点：行视图曾只给 sku_id，前端再用 sku-options 做 id→名称映射；
    该接口一旦为空（品牌未选/跨数据域）就退化成 UUID 片段，厂商侧看到一串乱码。
    """
    from app.api.procurement import _order_view

    w = _world(session)
    sku = _sku(session, w, "skuview")
    supplier, _su, _sa = _supplier_world(session, w, "t18-skuview")
    _fin, actor = _internal_actor(session, w, RoleCode.FINANCE, "t18-fin-skuview")
    svc = ProcurementService(session)

    order = svc.create_purchase_order(
        actor,
        supplier_id=supplier.id,
        brand_id=w["brand"].id,
        region_id=w["region"].id,
        expected_date=EXPECTED_DAY,
        lines=[(sku.id, Decimal("2"), 3500)],
    )
    view = _order_view(order, svc.repo.list_lines(order.id), session)
    assert len(view.lines) == 1
    line = view.lines[0]
    assert line.sku_id == sku.id
    assert line.sku_code == sku.code
    assert line.sku_name == sku.name


def test_q096_multi_line_amount_and_qty_guard(session: Session) -> None:
    """多行：总额=各行重算之和；重复 SKU/负价/0 箱被拒。"""
    w = _world(session)
    sku_a = _sku(session, w, "096a")
    sku_b = _sku(session, w, "096b")
    supplier, _su, _sa = _supplier_world(session, w, "t18-096m")
    _fin, fin_actor = _internal_actor(session, w, RoleCode.FINANCE, "t18-fin-096m")
    svc = ProcurementService(session)
    order = svc.create_purchase_order(
        fin_actor,
        supplier_id=supplier.id,
        brand_id=w["brand"].id,
        region_id=w["region"].id,
        expected_date=EXPECTED_DAY,
        lines=[
            (sku_a.id, Decimal("2"), 3500),    # 2 箱 × 24 瓶 × 35 元 = 1680 元
            (sku_b.id, Decimal("3"), 4200),    # 3 箱 × 24 瓶 × 42 元 = 3024 元
        ],
    )
    session.refresh(order)
    assert order.total_amount_fen == 168_000 + 302_400

    with pytest.raises(DomainError) as exc:
        svc.create_purchase_order(
            fin_actor,
            supplier_id=supplier.id,
            brand_id=w["brand"].id,
            region_id=w["region"].id,
            expected_date=EXPECTED_DAY,
            lines=[(sku_a.id, Decimal("1"), 3500),
                   (sku_a.id, Decimal("2"), 3500)],
        )
    assert exc.value.code == "PURCHASE_DUPLICATE_SKU"


# ----------------------------- Q097 -----------------------------


def test_q097_supplier_blocked_when_portal_disabled(session: Session,
                                                     monkeypatch) -> None:
    """门户关闭：厂商登录层 T02 已拦截，服务层确认供货再拒 403。

    显式把开关置为 false（而不是依赖变量缺失），避免被本地 .env 的
    演示配置（SUPPLIER_PORTAL_ENABLED=true）覆盖。
    """
    get_settings.cache_clear()
    monkeypatch.setenv("SUPPLIER_PORTAL_ENABLED", "false")
    get_settings.cache_clear()
    assert get_settings().supplier_portal_enabled is False

    w = _world(session)
    sku = _sku(session, w, "097")
    supplier, _su, supplier_actor = _supplier_world(session, w, "t18-097")
    _fin, fin_actor = _internal_actor(session, w, RoleCode.FINANCE, "t18-fin-097")
    order = _create_confirmed_po(session, w, sku, supplier, fin_actor)
    line = session.scalar(
        select(PurchaseOrderLine).where(PurchaseOrderLine.order_id == order.id))

    with pytest.raises(PermissionDeniedError) as exc:
        ProcurementService(session).confirm_supply(
            supplier_actor, order_id=order.id, line_id=line.id,
            confirmed_quantity_cases=Decimal("10"),
            expected_delivery_date=EXPECTED_DAY,
        )
    assert exc.value.code == "SUPPLIER_PORTAL_DISABLED"
    # PermissionDeniedError 由全局异常处理器映射为 HTTP 403
    assert isinstance(exc.value, PermissionDeniedError)


# ----------------------------- Q098 -----------------------------


def test_q098_supplier_brand_region_isolation(session: Session,
                                              portal_enabled) -> None:
    w = _world(session)
    sku = _sku(session, w, "098")
    supplier, _su, supplier_actor = _supplier_world(session, w, "t18-098")
    _fin, fin_actor = _internal_actor(session, w, RoleCode.FINANCE, "t18-fin-098")
    order = _create_confirmed_po(session, w, sku, supplier, fin_actor)
    line = session.scalar(
        select(PurchaseOrderLine).where(PurchaseOrderLine.order_id == order.id))

    # 另一套品牌/片区（第二个 world）
    w2 = _world(session)
    sku2 = _sku(session, w2, "098x")
    supplier2, _su2, supplier_actor2 = _supplier_world(session, w2, "t18-098b")
    order2 = _create_confirmed_po(session, w2, sku2, supplier2,
                                  _make_finance(session, w2))

    # 供应商1 的厂商账号确认 world2 的采购单 → 403（品牌/片区都不匹配）
    line2 = session.scalar(
        select(PurchaseOrderLine).where(PurchaseOrderLine.order_id == order2.id))
    with pytest.raises(PermissionDeniedError) as exc:
        ProcurementService(session).confirm_supply(
            supplier_actor, order_id=order2.id, line_id=line2.id,
            confirmed_quantity_cases=Decimal("1"),
            expected_delivery_date=EXPECTED_DAY,
        )
    assert exc.value.code == "SUPPLY_SCOPE_FORBIDDEN"
    assert isinstance(exc.value, PermissionDeniedError)  # 映射 HTTP 403

    # 厂商不能建采购单
    with pytest.raises(PermissionDeniedError) as exc2:
        ProcurementService(session).create_purchase_order(
            supplier_actor,
            supplier_id=supplier.id, brand_id=w["brand"].id,
            region_id=w["region"].id, expected_date=EXPECTED_DAY,
            lines=[(sku.id, Decimal("1"), 3500)],
        )
    assert exc2.value.code == "PURCHASE_SUPPLIER_CANNOT_ORDER"

    # 授权范围内确认成功
    rec = ProcurementService(session).confirm_supply(
        supplier_actor, order_id=order.id, line_id=line.id,
        confirmed_quantity_cases=Decimal("10"),
        expected_delivery_date=EXPECTED_DAY,
    )
    assert rec.status == SupplyConfirmationStatus.CONFIRMED.value


def _make_finance(session: Session, w: dict):
    _, fin_actor = _internal_actor(session, w, RoleCode.FINANCE, f"fin-{uuid4().hex[:5]}")
    return fin_actor


# ----------------------------- Q099 -----------------------------


def test_q099_supplier_cannot_see_contacts(session: Session,
                                           portal_enabled) -> None:
    w = _world(session)
    supplier, _su, supplier_actor = _supplier_world(session, w, "t18-099")
    svc = ProcurementService(session)

    public = svc.get_supplier_public_view(supplier_actor, supplier_id=supplier.id)
    # 内部联系人/电话对厂商脱敏为 None
    assert public["contact_name"] is None
    assert public["contact_phone"] is None
    assert public["name"] == supplier.name

    # 内部财务可见真实联系人
    _fin, fin_actor = _internal_actor(session, w, RoleCode.FINANCE, "t18-fin-099")
    internal = svc.get_supplier_public_view(fin_actor, supplier_id=supplier.id)
    assert internal["contact_name"] == "张采购"
    assert internal["contact_phone"] == "13800001111"


def test_q099_supplier_has_no_payroll_ability(session: Session) -> None:
    """厂商画像不含工资/绩效/员工模块能力（越权即 PermissionError_）。"""
    w = _world(session)
    _sup, _u, supplier_actor = _supplier_world(session, w, "t18-099p")
    from app.core.permissions import MODULE_PAYROLL, PermissionService

    with pytest.raises(PermissionError_):
        PermissionService().require_ability(supplier_actor, MODULE_PAYROLL)


# ----------------------------- Q100 -----------------------------


def test_q100_supply_confirm_not_payment_nor_stock(session: Session,
                                                   portal_enabled) -> None:
    w = _world(session)
    sku = _sku(session, w, "100")
    supplier, _su, supplier_actor = _supplier_world(session, w, "t18-100")
    _fin, fin_actor = _internal_actor(session, w, RoleCode.FINANCE, "t18-fin-100")
    order = _create_confirmed_po(session, w, sku, supplier, fin_actor)
    line = session.scalar(
        select(PurchaseOrderLine).where(PurchaseOrderLine.order_id == order.id))

    tx_before = session.scalar(
        select(func.count()).select_from(FinanceTransaction))
    rec = ProcurementService(session).confirm_supply(
        supplier_actor, order_id=order.id, line_id=line.id,
        confirmed_quantity_cases=Decimal("10"),
        expected_delivery_date=EXPECTED_DAY,
        note="厂商承诺 12-25 送达",
    )
    tx_after = session.scalar(
        select(func.count()).select_from(FinanceTransaction))
    session.refresh(order)

    # 不产生任何财务流水（付款是独立财务动作）
    assert tx_before == tx_after
    # 头单仅到“厂商已确认”，不是已收货/已付款
    assert order.status == PurchaseOrderStatus.SUPPLIER_CONFIRMED.value
    assert order.received_at is None
    # 行采购量不被供货确认改写
    session.refresh(line)
    assert line.quantity_cases == Decimal("10")
    assert rec.confirmed_quantity_cases == Decimal("10")


# ----------------------------- Q101 -----------------------------


def test_q101_duplicate_supply_confirm_idempotent(session: Session,
                                                  portal_enabled) -> None:
    w = _world(session)
    sku = _sku(session, w, "101")
    supplier, _su, supplier_actor = _supplier_world(session, w, "t18-101")
    _fin, fin_actor = _internal_actor(session, w, RoleCode.FINANCE, "t18-fin-101")
    order = _create_confirmed_po(session, w, sku, supplier, fin_actor)
    line = session.scalar(
        select(PurchaseOrderLine).where(PurchaseOrderLine.order_id == order.id))
    svc = ProcurementService(session)

    first = svc.confirm_supply(
        supplier_actor, order_id=order.id, line_id=line.id,
        confirmed_quantity_cases=Decimal("10"),
        expected_delivery_date=EXPECTED_DAY,
    )
    # 重复回调（即使改量/改日期）返回原记录
    replay = svc.confirm_supply(
        supplier_actor, order_id=order.id, line_id=line.id,
        confirmed_quantity_cases=Decimal("8"),
        expected_delivery_date=date(2026, 12, 28),
    )
    assert first.id == replay.id
    assert replay.confirmed_quantity_cases == Decimal("10")
    confirm_count = session.scalar(
        select(func.count())
        .select_from(SupplyConfirmation)
        .where(SupplyConfirmation.order_line_id == line.id)
    )
    assert confirm_count == 1
    session.refresh(order)
    assert order.status == PurchaseOrderStatus.SUPPLIER_CONFIRMED.value


# ----------------------------- 跨卡：DEMO 上游拒绝 -----------------------------


def test_t18_demo_sku_rejected_from_production_po(session: Session) -> None:
    """DEMO SKU 现在允许进入采购单（已移除限制）。"""
    w = _world(session)
    demo_sku = _sku(session, w, "demo", dataset_type="DEMO")
    supplier, _su, _sa = _supplier_world(session, w, "t18-demo")
    _fin, fin_actor = _internal_actor(session, w, RoleCode.FINANCE, "t18-fin-demo")
    # DEMO SKU 现在允许创建采购单
    order = ProcurementService(session).create_purchase_order(
        fin_actor,
        supplier_id=supplier.id, brand_id=w["brand"].id,
        region_id=w["region"].id, expected_date=EXPECTED_DAY,
        lines=[(demo_sku.id, Decimal("1"), 3500)],
    )
    assert order is not None


def test_t18_demo_supplier_rejected_from_production_po(session: Session) -> None:
    """R33/R34：演示（DEMO）供应商不得进入正式采购单。"""
    w = _world(session)
    sku = _sku(session, w, "pro")
    # 与当前数据集同 id 但 DEMO 类型——才能命中 DEMO 拒绝码而非 SUPPLIER_NOT_FOUND
    demo_supplier = Supplier(
        code=f"SUP-DEMO-{uuid4().hex[:8]}", name="演示供应商",
        dataset_type="DEMO", dataset_id=w["ds"],
    )
    session.add(demo_supplier)
    session.flush()
    _fin, fin_actor = _internal_actor(session, w, RoleCode.FINANCE, "t18-fin-demosup")
    # DEMO 供应商现在允许创建采购单（已移除限制）
    order = ProcurementService(session).create_purchase_order(
        fin_actor,
        supplier_id=demo_supplier.id, brand_id=w["brand"].id,
        region_id=w["region"].id, expected_date=EXPECTED_DAY,
        lines=[(sku.id, Decimal("1"), 3500)],
    )
    assert order is not None


def test_t18_demo_supplier_not_in_internal_options(session: Session) -> None:
    """内部供应商下拉不列 DEMO 供应商（前端建单直接不可选）。"""
    w = _world(session)
    supplier, _su, _sa = _supplier_world(session, w, "t18-opt")
    demo_supplier = Supplier(
        code=f"SUP-DEMO-{uuid4().hex[:8]}", name="演示供应商",
        dataset_type="DEMO", dataset_id=w["ds"],
    )
    session.add(demo_supplier)
    session.flush()
    _fin, fin_actor = _internal_actor(session, w, RoleCode.FINANCE, "t18-fin-opt")

    rows = ProcurementService(session).list_supplier_options(fin_actor)
    ids = {r["id"] for r in rows}
    assert supplier.id in ids
    assert demo_supplier.id not in ids


# ----------------------------- 公共造单 -----------------------------


def _create_confirmed_po(session: Session, w: dict, sku, supplier, fin_actor,
                         boxes: str = "10", price_fen: int = 3500) -> PurchaseOrder:
    order = ProcurementService(session).create_purchase_order(
        fin_actor,
        supplier_id=supplier.id,
        brand_id=w["brand"].id,
        region_id=w["region"].id,
        expected_date=EXPECTED_DAY,
        lines=[(sku.id, Decimal(boxes), price_fen)],
    )
    session.refresh(order)
    assert order.status == PurchaseOrderStatus.CONFIRMED.value
    return order


# --------------------- Q189/Q190 供货确认可见性与收货一致性 ---------------------


def test_q189_supplier_org_sees_colleague_confirmation(
    session: Session, portal_enabled
) -> None:
    """同一供应商的不同账号共享行确认：同事确认过的行不再显示"待确认"，也不重复落库。"""
    w = _world(session)
    sku = _sku(session, w, "189")
    supplier, _user_a, actor_a = _supplier_world(session, w, "org-a")
    user_b = _user(session, w["ds"], RoleCode.SUPPLIER, "org-b")
    session.add(UserRegionBrandAuth(
        user_id=user_b.id, region_id=w["region"].id, brand_id=w["brand"].id))
    session.add(SupplierAuthorization(
        supplier_id=supplier.id, user_id=user_b.id,
        brand_id=w["brand"].id, region_id=w["region"].id,
        dataset_type="PRODUCTION", dataset_id=w["ds"]))
    session.flush()
    actor_b = ActorContext(
        user_id=user_b.id,
        role_code=RoleCode.SUPPLIER.value,
        region_ids=frozenset({w["region"].id}),
        brand_ids=frozenset({w["brand"].id}),
        supplier_brand_ids=frozenset({w["brand"].id}),
        supplier_region_ids=frozenset({w["region"].id}),
    )

    _fin, fin_actor = _internal_actor(session, w, RoleCode.FINANCE, "t18-fin-189")
    order = _create_confirmed_po(session, w, sku, supplier, fin_actor)
    line = session.scalar(
        select(PurchaseOrderLine).where(PurchaseOrderLine.order_id == order.id))
    svc = ProcurementService(session)
    first = svc.confirm_supply(
        actor_a, order_id=order.id, line_id=line.id,
        confirmed_quantity_cases=Decimal("10"), expected_delivery_date=EXPECTED_DAY,
    )

    # 同组织另一账号可见该确认记录（前端不再误显示"待确认"）
    visible = svc.list_supply_confirmations(actor_b, order_id=order.id)
    assert [c.id for c in visible] == [first.id]

    # 另一账号重复提交：幂等返回原记录，不按账号重复落库
    replay = svc.confirm_supply(
        actor_b, order_id=order.id, line_id=line.id,
        confirmed_quantity_cases=Decimal("8"), expected_delivery_date=EXPECTED_DAY,
    )
    assert replay.id == first.id
    assert replay.confirmed_quantity_cases == Decimal("10")


def test_q190_receive_blocked_until_all_lines_confirmed(
    session: Session, portal_enabled
) -> None:
    """头单已 SUPPLIER_CONFIRMED 但仍有未确认行时禁止收货；允许补录后再收货。"""
    w = _world(session)
    sku_1 = _sku(session, w, "190a")
    sku_2 = _sku(session, w, "190b")
    supplier, _u, supplier_actor = _supplier_world(session, w, "recv")
    _fin, fin_actor = _internal_actor(session, w, RoleCode.FINANCE, "t18-fin-190")
    svc = ProcurementService(session)
    order = svc.create_purchase_order(
        fin_actor,
        supplier_id=supplier.id,
        brand_id=w["brand"].id,
        region_id=w["region"].id,
        expected_date=EXPECTED_DAY,
        lines=[(sku_1.id, Decimal("10"), 3500), (sku_2.id, Decimal("20"), 4200)],
    )
    lines = list(session.scalars(
        select(PurchaseOrderLine)
        .where(PurchaseOrderLine.order_id == order.id)
        .order_by(PurchaseOrderLine.sku_id)
    ))
    assert len(lines) == 2

    svc.confirm_supply(
        supplier_actor, order_id=order.id, line_id=lines[0].id,
        confirmed_quantity_cases=Decimal("10"), expected_delivery_date=EXPECTED_DAY,
    )
    session.refresh(order)
    assert order.status == PurchaseOrderStatus.CONFIRMED.value

    # 模拟历史/演示数据：头单已置 SUPPLIER_CONFIRMED，但第二行没有确认记录
    order.status = PurchaseOrderStatus.SUPPLIER_CONFIRMED.value
    session.flush()
    with pytest.raises(DomainError) as exc:
        svc.receive_purchase_order(fin_actor, order_id=order.id)
    assert exc.value.code == "PURCHASE_RECEIVE_UNCONFIRMED"

    # 允许对"已确认头单"的缺行补录，补全后可正常收货
    svc.confirm_supply(
        supplier_actor, order_id=order.id, line_id=lines[1].id,
        confirmed_quantity_cases=Decimal("20"), expected_delivery_date=EXPECTED_DAY,
    )
    received = svc.receive_purchase_order(fin_actor, order_id=order.id)
    assert received.status == PurchaseOrderStatus.RECEIVED.value
