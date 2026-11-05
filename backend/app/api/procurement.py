"""/api/procurement 路由：采购建议、采购单与厂商供货（T18，PRD §4.9 / §5.1）。

Router 只鉴权/校验/装配；金额重算、品牌×片区隔离、门户开关、幂等与 PRODUCTION
口径全部在 ProcurementService。请求体刻意不含总额字段（Q096）。
"""

from __future__ import annotations

# ruff: noqa: B008  FastAPI Depends() 需出现在默认值
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.deps import current_actor, db_session
from app.core.permissions import ActorContext
from app.models import Sku
from app.schemas.procurement import (
    CreatePurchaseOrderRequest,
    CreateSupplierIn,
    InventorySummaryOut,
    PurchaseOrderLineView,
    PurchaseOrderView,
    SkuOptionListOut,
    SkuOptionView,
    SuggestionDiagnosticsView,
    SuggestionLineView,
    SupplierManagementOut,
    SupplierOptionListOut,
    SupplierOptionView,
    SupplierToggleOut,
    SupplierView,
    SupplyConfirmationView,
    SupplyConfirmRequest,
    SupplyRejectRequest,
)
from app.services.procurement_service import ProcurementService

router = APIRouter(prefix="/api/procurement", tags=["procurement"])


def _supply_view(record) -> SupplyConfirmationView:
    return SupplyConfirmationView(
        id=record.id,
        order_id=record.order_id,
        order_line_id=record.order_line_id,
        supplier_id=record.supplier_id,
        supplier_user_id=record.supplier_user_id,
        status=record.status,
        confirmed_quantity_cases=record.confirmed_quantity_cases,
        expected_delivery_date=record.expected_delivery_date,
        proof_file_id=record.proof_file_id,
        reject_reason=record.reject_reason,
        note=record.note,
    )


def _sku_labels(session: Session, sku_ids) -> dict[UUID, tuple[str, str]]:
    """一次批量取「编码 + 名称」，避免逐行查询。"""
    ids = {sid for sid in sku_ids if sid is not None}
    if not ids:
        return {}
    rows = session.execute(
        select(Sku.id, Sku.code, Sku.name).where(Sku.id.in_(ids))
    ).all()
    return {row[0]: (row[1], row[2]) for row in rows}


def _order_view(order, lines, session: Session) -> PurchaseOrderView:
    labels = _sku_labels(session, [ln.sku_id for ln in lines])
    return PurchaseOrderView(
        id=order.id,
        code=order.code,
        supplier_id=order.supplier_id,
        brand_id=order.brand_id,
        region_id=order.region_id,
        expected_date=order.expected_date,
        total_amount_fen=order.total_amount_fen,
        status=order.status,
        confirmed_at=order.confirmed_at,
        received_at=order.received_at,
        note=order.note,
        lines=[
            PurchaseOrderLineView(
                id=ln.id,
                sku_id=ln.sku_id,
                sku_code=labels.get(ln.sku_id, ("", ""))[0],
                sku_name=labels.get(ln.sku_id, ("", ""))[1] or "未知商品",
                quantity_cases=ln.quantity_cases,
                tax_inclusive_unit_price_fen=ln.tax_inclusive_unit_price_fen,
                line_amount_fen=ln.line_amount_fen,
                source_confirmed_boxes=ln.source_confirmed_boxes,
            )
            for ln in lines
        ],
    )


@router.get("/suggestions", response_model=list[SuggestionLineView])
def suggestions(
    brand_id: UUID = Query(...),
    region_id: UUID = Query(...),
    supplier_id: UUID | None = Query(default=None),
    include_covered: bool = Query(
        default=True,
        description=(
            "默认 true：已被在途采购覆盖的 SKU 也返回并标记 covered_by_open_purchase，"
            "页面据此禁止勾选并说明原因；false 时只返回仍需采购的行"
        ),
    ),
    session: Session = Depends(db_session),
    actor: ActorContext = Depends(current_actor),
):
    rows = ProcurementService(session).build_suggestions(
        actor,
        brand_id=brand_id,
        region_id=region_id,
        supplier_id=supplier_id,
        include_covered=include_covered,
    )
    return [
        SuggestionLineView(
            sku_id=r.sku_id,
            sku_code=r.sku_code,
            brand_id=r.brand_id,
            region_id=r.region_id,
            confirmed_unmet_boxes=r.confirmed_unmet_boxes,
            open_purchase_boxes=r.open_purchase_boxes,
            suggested_purchase_boxes=r.suggested_purchase_boxes,
            inventory_available=r.inventory_available,
            inventory_status=r.inventory_status,
            overdue_purchase_boxes=r.overdue_purchase_boxes,
            covered_by_open_purchase=r.covered_by_open_purchase,
            available_boxes=r.available_boxes,
        )
        for r in rows
    ]


@router.get("/suggestions/diagnostics", response_model=SuggestionDiagnosticsView)
def suggestion_diagnostics(
    brand_id: UUID = Query(...),
    region_id: UUID = Query(...),
    supplier_id: UUID | None = Query(default=None),
    session: Session = Depends(db_session),
    actor: ActorContext = Depends(current_actor),
):
    """采购建议口径诊断（只读）：确认需求 / 在途 / 超期 / 建议量 + 当前数据域。"""
    data = ProcurementService(session).suggestion_diagnostics(
        actor, brand_id=brand_id, region_id=region_id, supplier_id=supplier_id
    )
    return SuggestionDiagnosticsView(**data)


@router.post("/orders", response_model=PurchaseOrderView, status_code=201)
def create_order(
    body: CreatePurchaseOrderRequest,
    session: Session = Depends(db_session),
    actor: ActorContext = Depends(current_actor),
):
    svc = ProcurementService(session)
    order = svc.create_purchase_order(
        actor,
        supplier_id=body.supplier_id,
        brand_id=body.brand_id,
        region_id=body.region_id,
        expected_date=body.expected_date,
        lines=[
            (ln.sku_id, ln.quantity_cases, ln.tax_inclusive_unit_price_fen)
            for ln in body.lines
        ],
        note=body.note,
    )
    return _order_view(order, svc.repo.list_lines(order.id), session)


@router.post("/orders/{order_id}/receive", response_model=PurchaseOrderView)
def receive_order(
    order_id: UUID,
    session: Session = Depends(db_session),
    actor: ActorContext = Depends(current_actor),
):
    svc = ProcurementService(session)
    order = svc.receive_purchase_order(actor, order_id=order_id)
    return _order_view(order, svc.repo.list_lines(order.id), session)


@router.get("/inventory/summary", response_model=InventorySummaryOut)
def inventory_summary(
    session: Session = Depends(db_session),
    actor: ActorContext = Depends(current_actor),
):
    """饮料收发存总和清单（T32）：按 SKU 的累计入库/出库/现存，仅内部角色。"""
    from app.services.inventory_service import InventoryService

    return InventorySummaryOut(items=InventoryService(session).summary(actor))


@router.post(
    "/orders/{order_id}/lines/{line_id}/supply-confirm",
    response_model=SupplyConfirmationView,
    status_code=201,
)
def confirm_supply(
    order_id: UUID,
    line_id: UUID,
    body: SupplyConfirmRequest,
    session: Session = Depends(db_session),
    actor: ActorContext = Depends(current_actor),
):
    record = ProcurementService(session).confirm_supply(
        actor,
        order_id=order_id,
        line_id=line_id,
        confirmed_quantity_cases=body.confirmed_quantity_cases,
        expected_delivery_date=body.expected_delivery_date,
        proof_file_id=body.proof_file_id,
        note=body.note,
    )
    return _supply_view(record)


@router.post(
    "/orders/{order_id}/lines/{line_id}/supply-reject",
    response_model=SupplyConfirmationView,
    status_code=201,
)
def reject_supply(
    order_id: UUID,
    line_id: UUID,
    body: SupplyRejectRequest,
    session: Session = Depends(db_session),
    actor: ActorContext = Depends(current_actor),
):
    record = ProcurementService(session).reject_supply(
        actor, order_id=order_id, line_id=line_id, reason=body.reason)
    return _supply_view(record)


@router.get("/suppliers", response_model=SupplierOptionListOut)
def list_suppliers(
    brand_id: UUID | None = Query(default=None),
    region_id: UUID | None = Query(default=None),
    session: Session = Depends(db_session),
    actor: ActorContext = Depends(current_actor),
):
    """供应商下拉：内部角色取本数据集在职供应商；厂商只取自己授权的。"""
    rows = ProcurementService(session).list_supplier_options(
        actor, brand_id=brand_id, region_id=region_id
    )
    return SupplierOptionListOut(
        items=[
            SupplierOptionView(id=r["id"], code=r["code"], name=r["name"])
            for r in rows
        ]
    )


@router.get("/sku-options", response_model=SkuOptionListOut)
def list_sku_options(
    brand_id: UUID | None = Query(default=None),
    session: Session = Depends(db_session),
    actor: ActorContext = Depends(current_actor),
):
    """SKU 下拉：采购行展示名称与手工补行（不影响建议口径）。"""
    skus = ProcurementService(session).list_sku_options(actor, brand_id=brand_id)
    return SkuOptionListOut(
        items=[
            SkuOptionView(
                id=s.id,
                code=s.code,
                name=s.name,
                brand_id=s.brand_id,
                purchase_price_fen=s.purchase_price_fen,
                case_spec=s.case_spec,
            )
            for s in skus
        ]
    )


@router.get(
    "/orders/{order_id}/supply-confirmations",
    response_model=list[SupplyConfirmationView],
)
def list_supply_confirmations(
    order_id: UUID,
    session: Session = Depends(db_session),
    actor: ActorContext = Depends(current_actor),
):
    """采购单的逐行供货确认轨迹（厂商只见自己的记录）。"""
    records = ProcurementService(session).list_supply_confirmations(
        actor, order_id=order_id
    )
    return [_supply_view(r) for r in records]


@router.get("/orders", response_model=list[PurchaseOrderView])
def list_orders(
    brand_id: UUID | None = Query(default=None),
    region_id: UUID | None = Query(default=None),
    session: Session = Depends(db_session),
    actor: ActorContext = Depends(current_actor),
):
    svc = ProcurementService(session)
    orders = svc.list_orders_for_actor(
        actor, brand_id=brand_id, region_id=region_id)
    return [_order_view(o, svc.repo.list_lines(o.id), session) for o in orders]


@router.get("/suppliers/{supplier_id}", response_model=SupplierView)
def supplier_view(
    supplier_id: UUID,
    session: Session = Depends(db_session),
    actor: ActorContext = Depends(current_actor),
):
    return ProcurementService(session).get_supplier_public_view(
        actor, supplier_id=supplier_id)


@router.post(
    "/suppliers",
    response_model=SupplierManagementOut,
    status_code=status.HTTP_201_CREATED,
)
def create_supplier(
    body: CreateSupplierIn,
    session: Session = Depends(db_session),
    actor: ActorContext = Depends(current_actor),
):
    """创建供应商（仅 SETTINGS 角色，Q099：联系人字段对厂商始终脱敏）。"""
    svc = ProcurementService(session)
    supplier = svc.create_supplier(
        actor,
        code=body.code,
        name=body.name,
        contact_name=body.contact_name,
        contact_phone=body.contact_phone,
    )
    return SupplierManagementOut(
        id=str(supplier.id),
        code=supplier.code,
        name=supplier.name,
        contact_name=supplier.contact_name,
        contact_phone=supplier.contact_phone,
        active=supplier.active,
    )


@router.post(
    "/suppliers/{supplier_id}/toggle-active",
    response_model=SupplierToggleOut,
)
def toggle_supplier_active(
    supplier_id: UUID,
    session: Session = Depends(db_session),
    actor: ActorContext = Depends(current_actor),
):
    """启用/停用供应商（仅 SETTINGS 角色）。"""
    svc = ProcurementService(session)
    supplier = svc.toggle_supplier_active(actor, supplier_id=supplier_id)
    return SupplierToggleOut(
        id=str(supplier.id),
        code=supplier.code,
        name=supplier.name,
        active=supplier.active,
    )


@router.get(
    "/suppliers/management",
    response_model=list[SupplierManagementOut],
)
def list_suppliers_management(
    session: Session = Depends(db_session),
    actor: ActorContext = Depends(current_actor),
):
    """供应商管理台账（含停用，仅 SETTINGS 角色）。"""
    return [
        SupplierManagementOut(
            id=str(s["id"]),
            code=s["code"],
            name=s["name"],
            contact_name=s.get("contact_name"),
            contact_phone=s.get("contact_phone"),
            active=s["active"],
        )
        for s in ProcurementService(session).list_supplier_management(actor)
    ]
