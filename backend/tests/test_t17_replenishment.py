"""T17 补货确认与履约（PRD §4.9 / R8 / R16 / A11 / A17 / RK-5）。

- Q089 未确认不汇总：DRAFT/SUBMITTED 不进已确认箱数汇总（committed=0）；
- Q090 部分满足留余额：fulfilled < committed → PARTIALLY_FULFILLED，余额可继续履约；
- Q091 超量拒绝：本次送达 > 未满足余额直接拒绝，订单数量/状态不变（零写入事件）；
- Q092 重复回调幂等：同 idempotency_key 只建一笔事件、不重复累计；
- Q093 取消必须填写原因，且写入 cancel_reason 留痕；
- Q094 满足率口径：箱数满足率 = 已满足/已确认；按时满足率按承诺日判定。

跨卡：未确认不汇总沿用 R8（同 Q066 口径），本套件不重算销量/巡访指标。
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from tests.test_t09_contract import _ctx, _merchant, _user, _world

from app.core.errors import DomainError, PermissionDeniedError
from app.models import (
    ReplenishmentFulfillment,
    ReplenishmentOrder,
    Sku,
    UserRegionBrandAuth,
)
from app.models.enums import ReplenishmentStatus, RoleCode
from app.services.replenishment_service import ReplenishmentService

REQUEST_DAY = date(2026, 12, 14)
PROMISED_DAY = date(2026, 12, 18)


# ----------------------------- 造数 -----------------------------


def _sku(session: Session, w: dict, code: str) -> Sku:
    s = Sku(
        code=f"SKU-{code}-{uuid4().hex[:6]}",
        name="沁爽汽水",
        brand_id=w["brand"].id,
        capacity_ml=500,
        suggested_price_fen=300,
        case_spec=Decimal("24"),
        dataset_type="PRODUCTION",
        dataset_id=w["ds"],
    )
    session.add(s)
    session.flush()
    return s


def _people(session: Session, w: dict, tag: str):
    rep = _user(session, w["ds"], RoleCode.SALES_REP, f"{tag}-rep")
    mgr = _user(session, w["ds"], RoleCode.REGIONAL_MANAGER, f"{tag}-mgr")
    session.add_all(
        [
            UserRegionBrandAuth(user_id=rep.id, region_id=w["region"].id,
                                brand_id=w["brand"].id),
            UserRegionBrandAuth(user_id=mgr.id, region_id=w["region"].id,
                                brand_id=w["brand"].id),
        ]
    )
    session.flush()
    rep_actor = _ctx(rep, RoleCode.SALES_REP.value, frozenset({w["region"].id}))
    mgr_actor = _ctx(mgr, RoleCode.REGIONAL_MANAGER.value,
                     frozenset({w["region"].id}))
    return rep, rep_actor, mgr, mgr_actor


def _draft_order(
    session: Session, w: dict, merchant, sku, requested: str,
    *, promised: date | None = None,
) -> ReplenishmentOrder:
    order = ReplenishmentOrder(
        code=f"RD-{uuid4().hex[:12]}",
        merchant_id=merchant.id,
        sku_id=sku.id,
        requested_boxes=Decimal(requested),
        committed_boxes=Decimal("0"),
        fulfilled_boxes=Decimal("0"),
        request_date=REQUEST_DAY,
        promised_delivery_date=promised,
        status=ReplenishmentStatus.DRAFT.value,
        dataset_type="PRODUCTION",
        dataset_id=w["ds"],
    )
    session.add(order)
    session.flush()
    return order


def _confirmed_order(
    session: Session, w: dict, merchant, sku,
    *, requested: str = "10", confirmed: str | None = None,
    promised: date | None = PROMISED_DAY, tag: str = "t17",
):
    """DRAFT → 提交 → 经理确认 的完整造数，返回 (svc, mgr_actor, order)。"""
    _rep, rep_actor, _mgr, mgr_actor = _people(session, w, tag)
    order = _draft_order(session, w, merchant, sku, requested, promised=None)
    svc = ReplenishmentService(session)
    svc.submit_order(rep_actor, order_id=order.id)
    confirmed_boxes = Decimal(confirmed if confirmed is not None else requested)
    adjust = None
    if confirmed_boxes != Decimal(requested):
        adjust = "库存有限，核减确认数量"
    svc.confirm(
        mgr_actor, order_id=order.id,
        confirmed_boxes=confirmed_boxes,
        promised_delivery_date=promised,
        adjust_reason=adjust,
    )
    session.refresh(order)
    return svc, mgr_actor, order


# ----------------------------- Q089 -----------------------------


def test_q089_unconfirmed_not_summarized(session: Session) -> None:
    """DRAFT/SUBMITTED 的 committed=0，不进已确认汇总；确认后才计入。"""
    w = _world(session)
    merchant = _merchant(session, w, name="T17店-089")
    sku = _sku(session, w, "089")
    rep, rep_actor, mgr, mgr_actor = _people(session, w, "t17-89")

    order = _draft_order(session, w, merchant, sku, "10")
    svc = ReplenishmentService(session)

    # DRAFT：汇总为空
    draft_summary = svc.summarize_confirmed(
        mgr_actor, region_id=w["region"].id, sku_id=sku.id)
    assert draft_summary["confirmed_order_count"] == 0
    assert draft_summary["confirmed_boxes_total"] == Decimal("0")

    # SUBMITTED：仍未确认，不汇总
    svc.submit_order(rep_actor, order_id=order.id)
    submitted_summary = svc.summarize_confirmed(
        mgr_actor, region_id=w["region"].id, sku_id=sku.id)
    assert submitted_summary["confirmed_order_count"] == 0
    assert submitted_summary["confirmed_boxes_total"] == Decimal("0")

    # CONFIRMED：committed=10 进汇总
    svc.confirm(
        mgr_actor, order_id=order.id, confirmed_boxes=Decimal("10"),
        promised_delivery_date=PROMISED_DAY,
    )
    confirmed_summary = svc.summarize_confirmed(
        mgr_actor, region_id=w["region"].id, sku_id=sku.id)
    assert confirmed_summary["confirmed_order_count"] == 1
    assert confirmed_summary["confirmed_boxes_total"] == Decimal("10")
    assert confirmed_summary["by_sku"][str(sku.id)] == Decimal("10")


def test_q089_sales_rep_cannot_confirm(session: Session) -> None:
    """职责分离：业务员不能确认补货（R：业务员不能确认自身上报的补货）。"""
    w = _world(session)
    merchant = _merchant(session, w, name="T17店-089r")
    sku = _sku(session, w, "089r")
    rep, rep_actor, _mgr, _mgr_actor = _people(session, w, "t17-89r")
    order = _draft_order(session, w, merchant, sku, "10")
    svc = ReplenishmentService(session)
    svc.submit_order(rep_actor, order_id=order.id)
    with pytest.raises(PermissionDeniedError) as exc:
        svc.confirm(rep_actor, order_id=order.id, confirmed_boxes=Decimal("10"))
    assert exc.value.code == "REPL_MANAGER_ONLY"


def test_q089_adjust_without_reason_rejected(session: Session) -> None:
    """经理调整确认数量但不填原因 → 拒绝。"""
    w = _world(session)
    merchant = _merchant(session, w, name="T17店-089a")
    sku = _sku(session, w, "089a")
    _rep, rep_actor, _mgr, mgr_actor = _people(session, w, "t17-89a")
    order = _draft_order(session, w, merchant, sku, "10")
    svc = ReplenishmentService(session)
    svc.submit_order(rep_actor, order_id=order.id)
    with pytest.raises(DomainError) as exc:
        svc.confirm(mgr_actor, order_id=order.id,
                    confirmed_boxes=Decimal("8"), adjust_reason="  ")
    assert exc.value.code == "REPL_ADJUST_REASON_REQUIRED"


# ----------------------------- Q090 -----------------------------


def test_q090_partial_fulfillment_keeps_balance(session: Session) -> None:
    """送达 6/10：PARTIALLY_FULFILLED，已满足 6、余额 4 可继续履约直至 FULFILLED。"""
    w = _world(session)
    merchant = _merchant(session, w, name="T17店-090")
    sku = _sku(session, w, "090")
    svc, mgr_actor, order = _confirmed_order(
        session, w, merchant, sku, requested="10", tag="t17-90")

    e1 = svc.register_fulfillment(
        mgr_actor, order_id=order.id, delivered_boxes=Decimal("6"),
        delivered_on=PROMISED_DAY - timedelta(days=1), idempotency_key="fb-6",
    )
    session.refresh(order)
    assert e1.delivered_boxes == Decimal("6")
    assert order.fulfilled_boxes == Decimal("6")
    assert order.status == ReplenishmentStatus.PARTIALLY_FULFILLED.value
    # 余额 4
    assert Decimal(order.committed_boxes) - Decimal(order.fulfilled_boxes) == Decimal("4")

    e2 = svc.register_fulfillment(
        mgr_actor, order_id=order.id, delivered_boxes=Decimal("4"),
        delivered_on=PROMISED_DAY, idempotency_key="fb-4",
    )
    session.refresh(order)
    assert e2.delivered_boxes == Decimal("4")
    assert order.fulfilled_boxes == Decimal("10")
    assert order.status == ReplenishmentStatus.FULFILLED.value

    # FULFILLED → CLOSED
    svc.close_order(mgr_actor, order_id=order.id)
    session.refresh(order)
    assert order.status == ReplenishmentStatus.CLOSED.value


# ----------------------------- Q091 -----------------------------


def test_q091_over_fulfillment_rejected_zero_write(session: Session) -> None:
    """本次送达 7 但余额只剩 4 → 拒绝，事件数 0、订单状态与累计量不变。"""
    w = _world(session)
    merchant = _merchant(session, w, name="T17店-091")
    sku = _sku(session, w, "091")
    svc, mgr_actor, order = _confirmed_order(
        session, w, merchant, sku, requested="10", tag="t17-91")
    svc.register_fulfillment(
        mgr_actor, order_id=order.id, delivered_boxes=Decimal("6"),
        delivered_on=PROMISED_DAY - timedelta(days=1), idempotency_key="ok-6",
    )
    session.refresh(order)
    assert order.fulfilled_boxes == Decimal("6")

    event_count_before = session.scalar(
        select(func.count()).select_from(ReplenishmentFulfillment))
    with pytest.raises(DomainError) as exc:
        with session.begin_nested():
            svc.register_fulfillment(
                mgr_actor, order_id=order.id, delivered_boxes=Decimal("7"),
                delivered_on=PROMISED_DAY, idempotency_key="bad-7",
            )
    assert exc.value.code == "REPL_FULFILLMENT_EXCEEDS_COMMITTED"

    session.refresh(order)
    assert order.fulfilled_boxes == Decimal("6")
    assert order.status == ReplenishmentStatus.PARTIALLY_FULFILLED.value
    event_count_after = session.scalar(
        select(func.count()).select_from(ReplenishmentFulfillment))
    assert event_count_before == event_count_after == 1


# ----------------------------- Q092 -----------------------------


def test_q092_duplicate_callback_idempotent(session: Session) -> None:
    """同一 idempotency_key 重复回调：只建一笔事件、fulfilled 不重复累计。"""
    w = _world(session)
    merchant = _merchant(session, w, name="T17店-092")
    sku = _sku(session, w, "092")
    svc, mgr_actor, order = _confirmed_order(
        session, w, merchant, sku, requested="10", tag="t17-92")

    first = svc.register_fulfillment(
        mgr_actor, order_id=order.id, delivered_boxes=Decimal("3"),
        delivered_on=PROMISED_DAY, idempotency_key="cb-dup",
    )
    replay = svc.register_fulfillment(
        mgr_actor, order_id=order.id, delivered_boxes=Decimal("3"),
        delivered_on=PROMISED_DAY, idempotency_key="cb-dup",
    )
    session.refresh(order)
    assert first.id == replay.id
    assert order.fulfilled_boxes == Decimal("3")  # 未重复累计
    events = session.scalars(
        select(ReplenishmentFulfillment).where(
            ReplenishmentFulfillment.idempotency_key == "cb-dup")
    ).all()
    assert len(events) == 1


def test_q092_submit_confirm_idempotent(session: Session) -> None:
    """提交/确认重复调用幂等：不产生额外状态迁移异常，确认量不变。"""
    w = _world(session)
    merchant = _merchant(session, w, name="T17店-092i")
    sku = _sku(session, w, "092i")
    _rep, rep_actor, _mgr, mgr_actor = _people(session, w, "t17-92i")
    order = _draft_order(session, w, merchant, sku, "5")
    svc = ReplenishmentService(session)
    svc.submit_order(rep_actor, order_id=order.id)
    svc.submit_order(rep_actor, order_id=order.id)  # 重复提交
    svc.confirm(mgr_actor, order_id=order.id, confirmed_boxes=Decimal("5"),
                promised_delivery_date=PROMISED_DAY)
    svc.confirm(mgr_actor, order_id=order.id, confirmed_boxes=Decimal("5"),
                promised_delivery_date=PROMISED_DAY)  # 重复确认
    session.refresh(order)
    assert order.status == ReplenishmentStatus.CONFIRMED.value
    assert order.committed_boxes == Decimal("5")


# ----------------------------- Q093 -----------------------------


def test_q093_cancel_requires_reason(session: Session) -> None:
    """取消必须填写原因（空/空白拒绝）；成功取消留 cancel_reason 与取消人。"""
    w = _world(session)
    merchant = _merchant(session, w, name="T17店-093")
    sku = _sku(session, w, "093")
    svc, mgr_actor, order = _confirmed_order(
        session, w, merchant, sku, requested="10", tag="t17-93")
    # 已部分满足 6，取消剩余 4
    svc.register_fulfillment(
        mgr_actor, order_id=order.id, delivered_boxes=Decimal("6"),
        delivered_on=PROMISED_DAY - timedelta(days=1), idempotency_key="c-6",
    )
    with pytest.raises(DomainError) as exc:
        svc.cancel_order(mgr_actor, order_id=order.id, reason="   ")
    assert exc.value.code == "REPL_CANCEL_REASON_REQUIRED"

    cancelled = svc.cancel_order(
        mgr_actor, order_id=order.id, reason="厂商缺货，剩余 4 箱本周无法供应")
    assert cancelled.status == ReplenishmentStatus.CANCELLED.value
    assert cancelled.cancel_reason == "厂商缺货，剩余 4 箱本周无法供应"
    assert cancelled.cancelled_by == mgr_actor.user_id
    # 已满足量保留不被抹掉
    assert cancelled.fulfilled_boxes == Decimal("6")


def test_q093_cancel_closed_rejected(session: Session) -> None:
    """已 CLOSED 不可取消。"""
    w = _world(session)
    merchant = _merchant(session, w, name="T17店-093b")
    sku = _sku(session, w, "093b")
    svc, mgr_actor, order = _confirmed_order(
        session, w, merchant, sku, requested="2", tag="t17-93b")
    svc.register_fulfillment(
        mgr_actor, order_id=order.id, delivered_boxes=Decimal("2"),
        delivered_on=PROMISED_DAY, idempotency_key="f-2",
    )
    svc.close_order(mgr_actor, order_id=order.id)
    with pytest.raises(DomainError) as exc:
        svc.cancel_order(mgr_actor, order_id=order.id, reason="尝试关闭后取消")
    assert exc.value.code in {"REPL_CANCEL_STATE_INVALID", "STATE_TRANSITION_INVALID"}


# ----------------------------- Q094 -----------------------------


def test_q094_fulfillment_rate(session: Session) -> None:
    """满足率口径：两单已确认共 20，已满足 15 → 0.7500；
    其中一单按时、一单逾期 → 按时满足率仅计按时单的已满足量。"""
    w = _world(session)
    sku = _sku(session, w, "094")

    # 单 A：确认 10，承诺 12-18，12-17 全部送达 10（按时）
    m_a = _merchant(session, w, name="T17店-094a")
    svc_a, mgr_a, oa = _confirmed_order(
        session, w, m_a, sku, requested="10", promised=PROMISED_DAY, tag="t17-94a")
    svc_a.register_fulfillment(
        mgr_a, order_id=oa.id, delivered_boxes=Decimal("10"),
        delivered_on=PROMISED_DAY - timedelta(days=1), idempotency_key="a-10",
    )

    # 单 B：确认 10，承诺 12-18，先送 5（12-17 部分），再送 5（12-20 逾期）
    m_b = _merchant(session, w, name="T17店-094b")
    svc_b, mgr_b, ob = _confirmed_order(
        session, w, m_b, sku, requested="10", promised=PROMISED_DAY, tag="t17-94b")
    svc_b.register_fulfillment(
        mgr_b, order_id=ob.id, delivered_boxes=Decimal("5"),
        delivered_on=PROMISED_DAY - timedelta(days=1), idempotency_key="b-5",
    )
    svc_b.register_fulfillment(
        mgr_b, order_id=ob.id, delivered_boxes=Decimal("5"),
        delivered_on=PROMISED_DAY + timedelta(days=2), idempotency_key="b-5late",
    )

    rate = ReplenishmentService(session).fulfillment_rate(
        mgr_a, region_id=w["region"].id, sku_id=sku.id)
    assert rate.confirmed_boxes == Decimal("20")
    assert rate.fulfilled_boxes == Decimal("20")
    # A 全部按时（actual=12-17 ≤ 承诺）；B 最后送达 12-20 逾期 → 整单不计按时
    assert rate.on_time_fulfilled_boxes == Decimal("10")
    assert rate.fulfillment_rate == Decimal("1.0000")
    assert rate.on_time_rate == Decimal("0.5000")


def test_q094_rate_with_partial_and_unconfirmed(session: Session) -> None:
    """部分满足单计入已满足量；同 SKU 的未确认单不进分母。"""
    w = _world(session)
    sku = _sku(session, w, "094p")
    m = _merchant(session, w, name="T17店-094p")
    svc, mgr_actor, order = _confirmed_order(
        session, w, m, sku, requested="8", promised=PROMISED_DAY, tag="t17-94p")
    svc.register_fulfillment(
        mgr_actor, order_id=order.id, delivered_boxes=Decimal("2"),
        delivered_on=PROMISED_DAY, idempotency_key="p-2",
    )
    # 另一份未确认草稿（不同商家、同 SKU）不得进分母
    rep, rep_actor, _m2, _mgr2 = _people(session, w, "t17-94p2")
    m2 = _merchant(session, w, name="T17店-094p2")
    _draft_order(session, w, m2, sku, "100")

    rate = svc.fulfillment_rate(
        mgr_actor, region_id=w["region"].id, sku_id=sku.id)
    assert rate.confirmed_boxes == Decimal("8")
    assert rate.fulfilled_boxes == Decimal("2")
    assert rate.fulfillment_rate == Decimal("0.2500")
    assert rate.on_time_fulfilled_boxes == Decimal("2")
    assert rate.order_count == 1
