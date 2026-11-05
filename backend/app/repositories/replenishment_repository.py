"""补货确认与履约持久化（T17，PRD §4.9）。Repository 只做持久化。"""

from __future__ import annotations

from datetime import date
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    Merchant,
    ReplenishmentFulfillment,
    ReplenishmentOrder,
    SalesConfirmation,
    Sku,
    User,
)


class ReplenishmentRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    # ---------------- 订单 ----------------

    def lock_order(self, order_id: UUID) -> ReplenishmentOrder | None:
        """FOR UPDATE 行锁：履约累计与取消必须串行化，防超量并发。"""
        return self.session.scalar(
            select(ReplenishmentOrder)
            .where(ReplenishmentOrder.id == order_id)
            .with_for_update()
        )

    def get_order(self, order_id: UUID) -> ReplenishmentOrder | None:
        return self.session.get(ReplenishmentOrder, order_id)

    def add_order(self, order: ReplenishmentOrder) -> ReplenishmentOrder:
        self.session.add(order)
        self.session.flush()
        return order

    def list_orders_for_merchant(self, merchant_id: UUID) -> list[ReplenishmentOrder]:
        return list(
            self.session.scalars(
                select(ReplenishmentOrder)
                .where(ReplenishmentOrder.merchant_id == merchant_id)
                .order_by(ReplenishmentOrder.request_date.desc())
            ).all()
        )

    def list_orders(
        self,
        *,
        region_ids: set[UUID] | None = None,
        status: str | None = None,
        merchant_id: UUID | None = None,
        requested_from: date | None = None,
        requested_to: date | None = None,
        offset: int = 0,
        limit: int = 20,
    ) -> tuple[list[tuple[ReplenishmentOrder, Merchant, Sku]], int]:
        """补货单列表（补货中心用）：返回 (订单, 商家, SKU) 行与总数。

        - `region_ids=None` 表示不限制片区；空集合表示任何片区都不允许（返回空）；
        - 不限状态：DRAFT/SUBMITTED 也必须能列出（经理确认工作台依赖）；
        - 名称由 join 一次取出，避免逐行回查（前端不做 UUID 翻译）。
        """
        conditions = []
        if region_ids is not None:
            conditions.append(Merchant.region_id.in_(region_ids))
        if status is not None:
            conditions.append(ReplenishmentOrder.status == status)
        if merchant_id is not None:
            conditions.append(ReplenishmentOrder.merchant_id == merchant_id)
        if requested_from is not None:
            conditions.append(ReplenishmentOrder.request_date >= requested_from)
        if requested_to is not None:
            conditions.append(ReplenishmentOrder.request_date <= requested_to)

        stmt = (
            select(ReplenishmentOrder, Merchant, Sku)
            .join(Merchant, Merchant.id == ReplenishmentOrder.merchant_id)
            .join(Sku, Sku.id == ReplenishmentOrder.sku_id)
            .where(*conditions)
            .order_by(
                ReplenishmentOrder.request_date.desc(),
                ReplenishmentOrder.created_at.desc(),
            )
            .offset(max(offset, 0))
            .limit(limit)
        )
        rows = [tuple(r) for r in self.session.execute(stmt).all()]

        count_stmt = (
            select(func.count())
            .select_from(ReplenishmentOrder)
            .join(Merchant, Merchant.id == ReplenishmentOrder.merchant_id)
            .join(Sku, Sku.id == ReplenishmentOrder.sku_id)
            .where(*conditions)
        )
        total = self.session.scalar(count_stmt) or 0
        return rows, int(total)

    def merchant_region_dataset(
        self, merchant_id: UUID
    ) -> tuple[UUID, UUID, str] | None:
        row = self.session.execute(
            select(Merchant.region_id, Merchant.dataset_id, Merchant.dataset_type)
            .where(Merchant.id == merchant_id)
        ).first()
        if row is None:
            return None
        return row[0], row[1], row[2]

    def get_active_user(self, user_id: UUID, dataset_id: UUID) -> User | None:
        return self.session.scalar(
            select(User)
            .where(User.id == user_id)
            .where(User.status == "ACTIVE")
            .where(User.dataset_id == dataset_id)
        )

    # ---------------- 履约事件 ----------------

    def add_fulfillment(
        self, event: ReplenishmentFulfillment
    ) -> ReplenishmentFulfillment:
        self.session.add(event)
        self.session.flush()
        return event

    def get_fulfillment_by_idempotency(
        self, dataset_id: UUID, idempotency_key: str
    ) -> ReplenishmentFulfillment | None:
        return self.session.scalar(
            select(ReplenishmentFulfillment)
            .where(ReplenishmentFulfillment.dataset_id == dataset_id)
            .where(ReplenishmentFulfillment.idempotency_key == idempotency_key)
        )

    def list_fulfillments(self, order_id: UUID) -> list[ReplenishmentFulfillment]:
        return list(
            self.session.scalars(
                select(ReplenishmentFulfillment)
                .where(ReplenishmentFulfillment.order_id == order_id)
                .order_by(ReplenishmentFulfillment.delivered_on.asc())
            ).all()
        )

    def sales_confirmations_by_fulfillment(
        self, fulfillment_ids: list[UUID]
    ) -> dict[UUID, SalesConfirmation]:
        """履约事件 → 送达确认（含收入/成本/毛利），供详情与登记回显毛利。"""
        if not fulfillment_ids:
            return {}
        rows = self.session.scalars(
            select(SalesConfirmation).where(
                SalesConfirmation.source_fulfillment_id.in_(fulfillment_ids)
            )
        ).all()
        return {row.source_fulfillment_id: row for row in rows}

    # ---------------- 汇总/口径 ----------------

    def list_confirmed_orders(
        self,
        *,
        region_id: UUID | None = None,
        sku_id: UUID | None = None,
        merchant_ids: list[UUID] | None = None,
        requested_from: date | None = None,
        requested_to: date | None = None,
    ) -> list[ReplenishmentOrder]:
        """只返回已确认口径订单（CONFIRMED/PARTIAL/FULFILLED/CLOSED）。

        DRAFT/SUBMITTED/REQUESTED 一律不汇总（R8/Q089）。
        """
        confirmed_states = (
            "CONFIRMED", "PARTIALLY_FULFILLED", "FULFILLED", "CLOSED",
        )
        stmt = select(ReplenishmentOrder).where(
            ReplenishmentOrder.status.in_(confirmed_states)
        )
        if region_id is not None:
            m_ids = select(Merchant.id).where(Merchant.region_id == region_id)
            stmt = stmt.where(ReplenishmentOrder.merchant_id.in_(m_ids))
        if sku_id is not None:
            stmt = stmt.where(ReplenishmentOrder.sku_id == sku_id)
        if merchant_ids:
            stmt = stmt.where(ReplenishmentOrder.merchant_id.in_(merchant_ids))
        if requested_from is not None:
            stmt = stmt.where(ReplenishmentOrder.request_date >= requested_from)
        if requested_to is not None:
            stmt = stmt.where(ReplenishmentOrder.request_date <= requested_to)
        return list(self.session.scalars(stmt).all())
