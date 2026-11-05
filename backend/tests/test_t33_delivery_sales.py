"""T33 送达即确认销量 + 净利润口径修正 + 逐月收支汇总。

用户口径：货送到商家即算确认销量，计入确认销量指标与月度净利润收入项；
金额 = 送达箱数 × SKU 建议零售价（分，存储约定）；一履约事件只确认一次。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session
from tests.test_t09_contract import _merchant, _world
from tests.test_t18_procurement import (
    PROMISED_DAY,
    _confirmed_replenishment,
    _internal_actor,
    _sku,
)

from app.models import SalesConfirmation
from app.models.enums import RoleCode
from app.services.demo_data_guard import DataScope
from app.services.metrics_service import M_CONFIRMED_SALES_BOXES, MetricsService
from app.services.replenishment_service import ReplenishmentService

# _sku 建议零售价 300 分/瓶、箱规 24 瓶/箱（t18 helper 固定造数）
PRICE_FEN = 300
BOTTLES_PER_CASE = 24


def _mgr(session: Session, w: dict):
    return _internal_actor(
        session, w, RoleCode.REGIONAL_MANAGER, f"t33-mgr-{uuid4().hex[:5]}")


def _deliver(session: Session, w: dict, sku, committed: str, boxes: str,
             *, on: date | None = None, key: str | None = None):
    """补货确认 → 登记送达，返回 (order, event, mgr_actor)。"""
    repl = _confirmed_replenishment(session, w, sku, committed)
    _m, mgr = _mgr(session, w)
    event = ReplenishmentService(session).register_fulfillment(
        mgr, order_id=repl.id, delivered_boxes=Decimal(boxes),
        delivered_on=on or PROMISED_DAY,
        idempotency_key=key or f"t33-{uuid4().hex[:8]}",
    )
    return repl, event, mgr


# ----------------------------- 送达即确认销量 -----------------------------


def test_delivery_creates_sales_confirmation(session: Session) -> None:
    w = _world(session)
    sku = _sku(session, w, "T33A")
    repl, event, _mgr_actor = _deliver(session, w, sku, "6", "4")

    rows = session.scalars(
        select(SalesConfirmation).where(
            SalesConfirmation.source_fulfillment_id == event.id)
    ).all()
    assert len(rows) == 1
    row = rows[0]
    assert row.merchant_id == repl.merchant_id
    assert row.sku_id == sku.id
    assert row.biz_date == event.delivered_on
    assert row.confirmed_boxes == Decimal("4")
    # 金额 = 4 箱 × 24 瓶 × 300 分 = 28800 分
    assert row.amount_fen == 4 * BOTTLES_PER_CASE * PRICE_FEN


def test_delivery_confirmation_idempotent(session: Session) -> None:
    """同幂等键重复登记送达：只产生一条销量确认（不重复计收入）。"""
    w = _world(session)
    sku = _sku(session, w, "T33B")
    repl = _confirmed_replenishment(session, w, sku, "6")
    _m, mgr = _mgr(session, w)
    svc = ReplenishmentService(session)
    key = f"t33-{uuid4().hex[:8]}"
    first = svc.register_fulfillment(
        mgr, order_id=repl.id, delivered_boxes=Decimal("4"),
        delivered_on=PROMISED_DAY, idempotency_key=key)
    second = svc.register_fulfillment(
        mgr, order_id=repl.id, delivered_boxes=Decimal("4"),
        delivered_on=PROMISED_DAY, idempotency_key=key)
    assert second.id == first.id

    rows = session.scalars(
        select(SalesConfirmation).where(SalesConfirmation.sku_id == sku.id)
    ).all()
    assert len(rows) == 1


def test_confirmed_sales_metric_moves_after_delivery(session: Session) -> None:
    """修「工作台确认销量一直不变」：送达当月指标计入送达箱数。"""
    w = _world(session)
    sku = _sku(session, w, "T33C")
    today = date.today()
    _repl, _event, mgr = _deliver(session, w, sku, "6", "4", on=today)

    result = MetricsService(session).compute(
        mgr, M_CONFIRMED_SALES_BOXES, scope=DataScope.PRODUCTION,
        period=f"{today.year}-{today.month:02d}",
    )
    assert result.value == Decimal("4.00")


# ----------------------------- 逐月收支汇总 -----------------------------


def test_profit_trend_accumulates_month_over_month(session: Session) -> None:
    """逐月汇总：当月收入=送达确认额；累计净利润逐月代数累加。"""
    w = _world(session)
    sku = _sku(session, w, "T33D")
    today = date.today()
    _repl, _event, mgr = _deliver(session, w, sku, "6", "4", on=today)

    # 上月手工确认一笔 800 分收入（验证跨月累加）
    if today.month == 1:
        prev = date(today.year - 1, 12, 15)
    else:
        prev = date(today.year, today.month - 1, 15)
    m = _merchant(session, w, "T33 上月店")
    session.add(SalesConfirmation(
        merchant_id=m.id, sku_id=sku.id, biz_date=prev,
        confirmed_boxes=Decimal("2"), amount_fen=800,
        confirmer_id=mgr.user_id,
        dataset_type="PRODUCTION", dataset_id=w["ds"]))
    session.flush()

    items = MetricsService(session).profit_trend(
        mgr, scope="PRODUCTION", months=2)

    assert [i["period"] for i in items] == [
        f"{prev.year}-{prev.month:02d}", f"{today.year}-{today.month:02d}"]
    prev_item, cur_item = items
    assert prev_item["sales"] == 800
    assert prev_item["net"] == 800
    assert prev_item["cumulative"] == 800
    assert cur_item["sales"] == 4 * BOTTLES_PER_CASE * PRICE_FEN
    assert cur_item["net"] == 4 * BOTTLES_PER_CASE * PRICE_FEN
    assert cur_item["cumulative"] == 800 + 4 * BOTTLES_PER_CASE * PRICE_FEN
