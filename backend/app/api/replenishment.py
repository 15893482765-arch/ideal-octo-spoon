"""/api/replenishment 路由：补货确认与履约（PRD §4.9，T17）。

Router 只鉴权/校验/装配；状态机、数量口径、满足率全在 ReplenishmentService。
"""

from __future__ import annotations

# ruff: noqa: B008  FastAPI Depends() 需出现在默认值
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.deps import current_actor, db_session
from app.core.permissions import ActorContext
from app.schemas.replenishment import (
    CancelRequest,
    CloseRequest,
    ConfirmRequest,
    FulfillmentRateView,
    FulfillmentRequest,
    FulfillmentView,
    Pagination,
    ReplenishmentOrderListItem,
    ReplenishmentOrderListResponse,
    ReplenishmentOrderView,
    SummaryView,
)
from app.services.replenishment_service import ReplenishmentService

router = APIRouter(prefix="/api/replenishment", tags=["replenishment"])


def _fulfillment_view(f, confirmation=None) -> FulfillmentView:
    """履约事件视图（T36：带上该笔送达的收入/成本/毛利与单价口径）。"""
    unit_retail = None
    unit_purchase = None
    if confirmation is not None and Decimal(f.delivered_boxes) > 0:
        boxes = Decimal(f.delivered_boxes)
        unit_retail = int(
            (Decimal(confirmation.amount_fen) / boxes).quantize(
                Decimal("1"), rounding=ROUND_HALF_UP
            )
        )
        unit_purchase = int(
            (Decimal(confirmation.cost_fen) / boxes).quantize(
                Decimal("1"), rounding=ROUND_HALF_UP
            )
        )
    return FulfillmentView(
        id=f.id,
        order_id=f.order_id,
        delivered_boxes=f.delivered_boxes,
        delivered_on=f.delivered_on,
        proof_file_id=f.proof_file_id,
        idempotency_key=f.idempotency_key,
        note=f.note,
        registered_by=f.registered_by,
        sales_amount_fen=confirmation.amount_fen if confirmation else None,
        cost_fen=confirmation.cost_fen if confirmation else None,
        profit_fen=confirmation.profit_fen if confirmation else None,
        unit_retail_price_fen=unit_retail,
        unit_purchase_price_fen=unit_purchase,
    )


def _view(detail: dict) -> ReplenishmentOrderView:
    o = detail["order"]
    return ReplenishmentOrderView(
        id=o.id,
        code=o.code,
        merchant_id=o.merchant_id,
        sku_id=o.sku_id,
        requested_boxes=o.requested_boxes,
        committed_boxes=o.committed_boxes,
        fulfilled_boxes=o.fulfilled_boxes,
        request_date=o.request_date,
        promised_delivery_date=o.promised_delivery_date,
        actual_delivery_date=o.actual_delivery_date,
        status=o.status,
        confirmer_id=o.confirmer_id,
        confirmed_at=o.confirmed_at,
        adjust_reason=o.adjust_reason,
        cancel_reason=o.cancel_reason,
        cancelled_by=o.cancelled_by,
        fulfillments=[
            _fulfillment_view(f, detail.get("sales_by_fulfillment", {}).get(f.id))
            for f in detail["fulfillments"]
        ],
    )


def _list_item(row: dict) -> ReplenishmentOrderListItem:
    o = row["order"]
    committed = o.committed_boxes
    fulfilled = o.fulfilled_boxes
    return ReplenishmentOrderListItem(
        id=o.id,
        code=o.code,
        merchant_id=o.merchant_id,
        merchant_code=row["merchant_code"],
        merchant_name=row["merchant_name"],
        sku_id=o.sku_id,
        sku_code=row["sku_code"],
        sku_name=row["sku_name"],
        requested_boxes=o.requested_boxes,
        committed_boxes=committed,
        fulfilled_boxes=fulfilled,
        remaining_boxes=committed - fulfilled,
        request_date=o.request_date,
        promised_delivery_date=o.promised_delivery_date,
        actual_delivery_date=o.actual_delivery_date,
        status=o.status,
        confirmer_id=o.confirmer_id,
        confirmed_at=o.confirmed_at,
        adjust_reason=o.adjust_reason,
        cancel_reason=o.cancel_reason,
    )


@router.get("/orders", response_model=ReplenishmentOrderListResponse)
def list_orders(
    region_id: UUID | None = Query(default=None),
    status: str | None = Query(default=None, description="受控状态值，单值筛选"),
    merchant_id: UUID | None = Query(default=None),
    requested_from: date | None = Query(default=None),
    requested_to: date | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    session: Session = Depends(db_session),
    actor: ActorContext = Depends(current_actor),
):
    """补货单列表（补货中心）：含 DRAFT/SUBMITTED，行级范围由服务端注入。"""
    items, total, applied = ReplenishmentService(session).list_orders(
        actor,
        region_id=region_id,
        status=status,
        merchant_id=merchant_id,
        requested_from=requested_from,
        requested_to=requested_to,
        page=page,
        page_size=page_size,
    )
    return ReplenishmentOrderListResponse(
        items=[_list_item(i) for i in items],
        pagination=Pagination(page=page, page_size=page_size, total=total),
        applied_filters=applied,
    )


@router.post("/orders/{order_id}/submit", response_model=ReplenishmentOrderView)
def submit_order(
    order_id: UUID,
    session: Session = Depends(db_session),
    actor: ActorContext = Depends(current_actor),
):
    service = ReplenishmentService(session)
    service.submit_order(actor, order_id=order_id)
    return _view(service.get_order_detail(actor, order_id=order_id))


@router.post("/orders/{order_id}/confirm", response_model=ReplenishmentOrderView)
def confirm_order(
    order_id: UUID,
    body: ConfirmRequest,
    session: Session = Depends(db_session),
    actor: ActorContext = Depends(current_actor),
):
    service = ReplenishmentService(session)
    service.confirm(
        actor,
        order_id=order_id,
        confirmed_boxes=body.confirmed_boxes,
        promised_delivery_date=body.promised_delivery_date,
        adjust_reason=body.adjust_reason,
    )
    return _view(service.get_order_detail(actor, order_id=order_id))


@router.post("/orders/{order_id}/fulfillments", response_model=FulfillmentView,
             status_code=201)
def register_fulfillment(
    order_id: UUID,
    body: FulfillmentRequest,
    session: Session = Depends(db_session),
    actor: ActorContext = Depends(current_actor),
):
    service = ReplenishmentService(session)
    event = service.register_fulfillment(
        actor,
        order_id=order_id,
        delivered_boxes=body.delivered_boxes,
        delivered_on=body.delivered_on,
        idempotency_key=body.idempotency_key,
        proof_file_id=body.proof_file_id,
        note=body.note,
    )
    return _fulfillment_view(
        event, service.sales_confirmation_for_fulfillment(event.id)
    )


@router.post("/orders/{order_id}/close", response_model=ReplenishmentOrderView)
def close_order(
    order_id: UUID,
    body: CloseRequest,
    session: Session = Depends(db_session),
    actor: ActorContext = Depends(current_actor),
):
    service = ReplenishmentService(session)
    service.close_order(actor, order_id=order_id, note=body.note)
    return _view(service.get_order_detail(actor, order_id=order_id))


@router.post("/orders/{order_id}/cancel", response_model=ReplenishmentOrderView)
def cancel_order(
    order_id: UUID,
    body: CancelRequest,
    session: Session = Depends(db_session),
    actor: ActorContext = Depends(current_actor),
):
    service = ReplenishmentService(session)
    service.cancel_order(actor, order_id=order_id, reason=body.reason)
    return _view(service.get_order_detail(actor, order_id=order_id))


@router.get("/orders/{order_id}", response_model=ReplenishmentOrderView)
def order_detail(
    order_id: UUID,
    session: Session = Depends(db_session),
    actor: ActorContext = Depends(current_actor),
):
    return _view(
        ReplenishmentService(session).get_order_detail(actor, order_id=order_id)
    )


@router.get("/summary/confirmed", response_model=SummaryView)
def confirmed_summary(
    region_id: UUID | None = Query(default=None),
    sku_id: UUID | None = Query(default=None),
    requested_from: date | None = Query(default=None),
    requested_to: date | None = Query(default=None),
    session: Session = Depends(db_session),
    actor: ActorContext = Depends(current_actor),
):
    return ReplenishmentService(session).summarize_confirmed(
        actor, region_id=region_id, sku_id=sku_id,
        requested_from=requested_from, requested_to=requested_to,
    )


@router.get("/fulfillment-rate", response_model=FulfillmentRateView)
def fulfillment_rate(
    region_id: UUID | None = Query(default=None),
    sku_id: UUID | None = Query(default=None),
    requested_from: date | None = Query(default=None),
    requested_to: date | None = Query(default=None),
    session: Session = Depends(db_session),
    actor: ActorContext = Depends(current_actor),
):
    rate = ReplenishmentService(session).fulfillment_rate(
        actor, region_id=region_id, sku_id=sku_id,
        requested_from=requested_from, requested_to=requested_to,
    )
    return FulfillmentRateView(
        confirmed_boxes=rate.confirmed_boxes,
        fulfilled_boxes=rate.fulfilled_boxes,
        on_time_fulfilled_boxes=rate.on_time_fulfilled_boxes,
        order_count=rate.order_count,
        fulfillment_rate=rate.fulfillment_rate,
        on_time_rate=rate.on_time_rate,
    )
