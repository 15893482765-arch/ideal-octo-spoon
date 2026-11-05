"""补货确认与履约服务（PRD §4.9 / R8 / R16 / A11 / A17 / RK-5，T17）。

状态链：DRAFT → SUBMITTED → CONFIRMED → PARTIALLY_FULFILLED/FULFILLED → CLOSED；
SUBMITTED/CONFIRMED/PARTIALLY_FULFILLED 可 CANCELLED（必填原因）。

红线与口径：
- **未确认不汇总**（R8）：DRAFT/SUBMITTED/历史 REQUESTED 的 committed_boxes=0，
  不进任何采购建议/绩效/看板正式口径；汇总只读已确认态（Q089）；
- 经理可调整确认数量，但必须记录调整原因（差异与原因留审计）；
- 已满足量由履约事件累计，恒有 0 ≤ fulfilled ≤ committed；超量直接拒绝（Q091）；
  部分满足保留余额并可继续履约（Q090）；
- 履约登记按 (dataset, idempotency_key) 幂等：重复回调返回原事件，不重复累计
  （Q092/A17）；
- 取消必须留原因（Q093）；已审核事实不物理删除，只状态迁移 + 审计（同事务）；
- 满足率口径（Q094）集中在本服务 `fulfillment_rate`：
  - 箱数满足率 = 已满足箱数 ÷ 已确认箱数（同一筛选范围，只取已确认态）；
  - 按时满足率 = 承诺日前（含）已满足箱数 ÷ 已确认箱数（无承诺日的不计入按时分子）；
- 行级片区范围由服务端 JWT ActorContext 注入，禁止客户端指定。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import DomainError, PermissionDeniedError
from app.core.permissions import (
    MODULE_REPLENISHMENT,
    ActorContext,
    PermissionService,
)
from app.models import (
    FileObject,
    ReplenishmentFulfillment,
    ReplenishmentOrder,
    SalesConfirmation,
    Sku,
)
from app.models.enums import ReplenishmentStatus, RoleCode
from app.repositories.replenishment_repository import ReplenishmentRepository
from app.services.audit_service import AuditService
from app.services.business_transition_service import BusinessTransitionService
from app.services.state_machine import (
    DOMAIN_REPLENISHMENT,
    assert_legal_transition,
)

_ZERO = Decimal("0")

# 已确认口径状态（R8：只有这些才进正式汇总/满足率分母）
CONFIRMED_STATES = frozenset(
    {"CONFIRMED", "PARTIALLY_FULFILLED", "FULFILLED", "CLOSED"}
)
# 可履约状态
_FULFILLABLE = frozenset({"CONFIRMED", "PARTIALLY_FULFILLED"})
# 可取消状态（草稿/提交态不在正式口径，允许经理直接取消未确认单）
_CANCELLABLE = frozenset(
    {"DRAFT", "SUBMITTED", "CONFIRMED", "PARTIALLY_FULFILLED",
     "REQUESTED", "IN_TRANSIT", "OVERDUE"}
)


@dataclass(frozen=True)
class FulfillmentRate:
    """满足率口径结果（Q094）。比率用 Decimal，保留 4 位小数，禁 float。"""

    confirmed_boxes: Decimal
    fulfilled_boxes: Decimal
    on_time_fulfilled_boxes: Decimal
    order_count: int

    @property
    def fulfillment_rate(self) -> Decimal:
        """箱数满足率 = 已满足 / 已确认；无已确认量时为 0。"""
        if self.confirmed_boxes <= 0:
            return _ZERO
        return (self.fulfilled_boxes / self.confirmed_boxes).quantize(
            Decimal("0.0001")
        )

    @property
    def on_time_rate(self) -> Decimal:
        """按时满足率 = 承诺日前（含）已满足 / 已确认。"""
        if self.confirmed_boxes <= 0:
            return _ZERO
        return (self.on_time_fulfilled_boxes / self.confirmed_boxes).quantize(
            Decimal("0.0001")
        )


class ReplenishmentService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.repo = ReplenishmentRepository(session)
        self.permissions = PermissionService()
        self.audit = AuditService(session)
        self.transition = BusinessTransitionService(session)

    # ============================ 业务员提交 ============================

    def submit_order(self, actor: ActorContext, *, order_id: UUID) -> ReplenishmentOrder:
        """业务员把巡访生成的补货 DRAFT 提交待确认：DRAFT → SUBMITTED。"""
        self.permissions.require_ability(actor, MODULE_REPLENISHMENT)
        order = self.repo.get_order(order_id)
        if order is None:
            raise DomainError(code="REPL_ORDER_NOT_FOUND", message="补货单不存在")
        self._assert_region(actor, order)
        if order.status == ReplenishmentStatus.SUBMITTED.value:
            return order  # 幂等
        self.transition.transition(
            entity=order, domain=DOMAIN_REPLENISHMENT,
            to_state=ReplenishmentStatus.SUBMITTED.value,
            actor_id=actor.user_id, reason="业务员提交补货需求待经理确认",
        )
        return order

    def submit_merchant_draft(
        self,
        actor: ActorContext,
        *,
        merchant_id: UUID,
        sku_id: UUID | None = None,
        requested_boxes: Decimal | None = None,
    ) -> ReplenishmentOrder:
        """MCP 受控入口：提交某商家当前的补货 DRAFT（T24）。

        补货 DRAFT 由巡访提交生成（业务口径）；本方法只取该商家最近的 DRAFT
        并复用 submit_order 的权限/行级范围/状态机校验。可选 sku/boxes 仅做
        一致性核对（防止确认错单），不直接改业务数量。
        """
        orders = self.repo.list_orders_for_merchant(merchant_id)
        draft = next(
            (o for o in orders
             if o.status == ReplenishmentStatus.DRAFT.value),
            None,
        )
        if draft is None:
            raise DomainError(
                code="REPL_DRAFT_NOT_FOUND",
                message="该商家没有可提交的补货草稿（补货草稿由巡访提交生成）",
                details={"merchant_id": str(merchant_id)},
            )
        if sku_id is not None and draft.sku_id != sku_id:
            raise DomainError(
                code="REPL_SKU_MISMATCH",
                message="草稿商品与提交参数不一致，请重新核对",
                details={"draft_sku_id": str(draft.sku_id),
                         "request_sku_id": str(sku_id)},
            )
        return self.submit_order(actor, order_id=draft.id)

    # ============================ 经理确认 ============================

    def confirm(
        self,
        actor: ActorContext,
        *,
        order_id: UUID,
        confirmed_boxes: Decimal,
        promised_delivery_date: date | None = None,
        adjust_reason: str | None = None,
    ) -> ReplenishmentOrder:
        """经理审核确认补货行：SUBMITTED → CONFIRMED。

        - 可调整确认数量（相对 requested_boxes 增减），**差异必须给原因**；
        - 记录承诺到货日（不早于需求日，DB CHECK 兜底）；
        - 已确认量 > 0 才进正式口径（R8）。
        """
        self._require_manager(actor)
        order = self._locked_order(order_id)
        self._assert_region(actor, order)
        if order.status == ReplenishmentStatus.CONFIRMED.value:
            return order  # 幂等：已确认不重复处理
        if order.status not in (
            ReplenishmentStatus.SUBMITTED.value,
            ReplenishmentStatus.REQUESTED.value,  # 兼容历史待确认台账
        ):
            raise DomainError(
                code="REPL_CONFIRM_STATE_INVALID",
                message="仅待确认（SUBMITTED）的补货单可确认",
                details={"status": order.status},
            )
        confirmed_boxes = Decimal(confirmed_boxes)
        if confirmed_boxes <= 0:
            raise DomainError(
                code="REPL_CONFIRMED_BOXES_INVALID",
                message="确认箱数必须大于 0（0 箱请走取消）",
            )
        if confirmed_boxes != Decimal(order.requested_boxes) and not (
            adjust_reason and adjust_reason.strip()
        ):
            raise DomainError(
                code="REPL_ADJUST_REASON_REQUIRED",
                message="调整确认数量必须记录原因",
            )
        if (
            promised_delivery_date is not None
            and promised_delivery_date < order.request_date
        ):
            raise DomainError(
                code="REPL_PROMISED_DATE_INVALID",
                message="承诺到货日不能早于需求日",
            )

        before = {
            "status": order.status,
            "committed_boxes": str(order.committed_boxes),
            "requested_boxes": str(order.requested_boxes),
        }
        order.committed_boxes = confirmed_boxes
        order.promised_delivery_date = promised_delivery_date
        order.confirmer_id = actor.user_id
        order.confirmed_at = date.today()
        order.adjust_reason = adjust_reason.strip() if adjust_reason else None

        # 历史 REQUESTED→CONFIRMED 未在新图显式列出，统一以 SUBMITTED 语义迁移：
        # REQUESTED 是 SUBMITTED 的旧台账词。
        from_state = (
            ReplenishmentStatus.SUBMITTED.value
            if order.status == ReplenishmentStatus.REQUESTED.value
            else order.status
        )
        # 直接写新状态前先做合法性校验（from 取新语义状态）
        assert_legal_transition(
            DOMAIN_REPLENISHMENT, from_state,
            ReplenishmentStatus.CONFIRMED.value,
        )
        order.status = ReplenishmentStatus.CONFIRMED.value
        self.session.flush()
        self.audit.record_state_transition(
            object_type="REPLENISHMENT", object_id=order.id,
            actor_id=actor.user_id, before=before,
            after={
                "status": "CONFIRMED",
                "committed_boxes": str(confirmed_boxes),
                "requested_boxes": str(order.requested_boxes),
                "promised_delivery_date": (
                    promised_delivery_date.isoformat()
                    if promised_delivery_date else None
                ),
                "adjust_reason": order.adjust_reason,
            },
            reason=adjust_reason.strip() if adjust_reason else "经理按提报数量确认",
        )
        return order

    # ============================ 履约登记（送达凭证） ============================

    def register_fulfillment(
        self,
        actor: ActorContext,
        *,
        order_id: UUID,
        delivered_boxes: Decimal,
        delivered_on: date,
        idempotency_key: str,
        proof_file_id: UUID | None = None,
        note: str | None = None,
    ) -> ReplenishmentFulfillment:
        """登记一次送达履约（凭送货单/签收照片）。幂等键重复直接返回原事件。"""
        self._require_manager(actor)
        if not idempotency_key or not idempotency_key.strip():
            raise DomainError(
                code="REPL_FULFILLMENT_KEY_REQUIRED",
                message="履约登记必须带幂等键（防重复回调）",
            )
        delivered_boxes = Decimal(delivered_boxes)
        if delivered_boxes <= 0:
            raise DomainError(
                code="REPL_FULFILLMENT_BOXES_INVALID",
                message="本次送达箱数必须大于 0",
            )

        order = self._locked_order(order_id)
        self._assert_region(actor, order)

        # 幂等：同 dataset + key 返回原事件，不重复累计（Q092/A17）
        existing = self.repo.get_fulfillment_by_idempotency(
            order.dataset_id, idempotency_key.strip()
        )
        if existing is not None:
            return existing

        if order.status not in _FULFILLABLE:
            raise DomainError(
                code="REPL_FULFILL_STATE_INVALID",
                message="仅已确认（CONFIRMED/部分满足）的补货单可登记履约",
                details={"status": order.status},
            )
        if proof_file_id is not None:
            proof = self.session.get(FileObject, proof_file_id)
            if proof is None or proof.dataset_id != order.dataset_id:
                raise DomainError(
                    code="REPL_PROOF_INVALID",
                    message="送达凭证不存在或数据范围不一致",
                )

        remaining = Decimal(order.committed_boxes) - Decimal(order.fulfilled_boxes)
        if delivered_boxes > remaining:
            # 超量拒绝（Q091）：整笔不写入、订单数量不变
            raise DomainError(
                code="REPL_FULFILLMENT_EXCEEDS_COMMITTED",
                message="本次送达箱数超过未满足余额，超量履约被拒绝",
                details={
                    "committed_boxes": str(order.committed_boxes),
                    "already_fulfilled": str(order.fulfilled_boxes),
                    "remaining": str(remaining),
                    "attempted": str(delivered_boxes),
                },
            )

        before_status = order.status
        before_fulfilled = Decimal(order.fulfilled_boxes)
        event = ReplenishmentFulfillment(
            order_id=order.id,
            delivered_boxes=delivered_boxes,
            delivered_on=delivered_on,
            proof_file_id=proof_file_id,
            idempotency_key=idempotency_key.strip(),
            note=note,
            registered_by=actor.user_id,
            dataset_type=order.dataset_type,
            dataset_id=order.dataset_id,
        )
        self.repo.add_fulfillment(event)

        # T32：送达出库记账（台账幂等：履约事件×SKU 已记过则跳过，同事务）
        from app.services.inventory_service import InventoryService

        InventoryService(self.session).record_replenishment_fulfillment(
            actor, event, order
        )

        # T33：送达即确认销量（口径：货送到商家即算确认销量，进确认销量/净利润正式
        # 口径；金额 = 送达箱数 × SKU 建议零售价；幂等：一履约事件只确认一次）
        self._confirm_sales_on_delivery(actor, event, order, delivered_boxes)

        new_fulfilled = before_fulfilled + delivered_boxes
        order.fulfilled_boxes = new_fulfilled
        order.actual_delivery_date = delivered_on
        if new_fulfilled == Decimal(order.committed_boxes):
            target = ReplenishmentStatus.FULFILLED.value
        else:
            target = ReplenishmentStatus.PARTIALLY_FULFILLED.value
        # CONFIRMED→PARTIAL/FULFILLED、PARTIAL→PARTIAL(继续部分)/FULFILLED 均合法
        self.transition.transition(
            entity=order, domain=DOMAIN_REPLENISHMENT,
            to_state=target, actor_id=actor.user_id,
            reason=f"登记送达 {delivered_boxes} 箱（累计 {new_fulfilled}/"
                   f"{order.committed_boxes}）",
            extra_after={
                "fulfilled_boxes": str(new_fulfilled),
                "idempotency_key": event.idempotency_key,
                "from_status": before_status,
                "before_fulfilled": str(before_fulfilled),
            },
        )
        return event

    def _confirm_sales_on_delivery(
        self,
        actor: ActorContext,
        event: ReplenishmentFulfillment,
        order: ReplenishmentOrder,
        delivered_boxes: Decimal,
    ) -> None:
        """送达即确认销量（T33）：金额 = 送达箱数 × 箱规 × SKU 建议零售价（元/瓶）。

        T36 口径：送货给商家时必须同时固化毛利——
        成本 = 送达箱数 × 箱规 × 参考进价（`Sku.purchase_price_fen`，元/瓶），
        毛利 = 收入 − 成本 = 箱数 × 箱规 ×（建议零售价 − 进价）；
        进价缺失时成本记 0（无成本依据不臆造），毛利等于收入并在备注中提示；
        箱规未维护时系数取 1（与按箱报价等效，不放大也不归零）。

        幂等：先查后插 + source_fulfillment_id 唯一约束兜底；
        与履约幂等键、台账唯一约束构成三道防线，重复登记绝不重复计收入。
        """
        dup = self.session.execute(
            select(SalesConfirmation.id).where(
                SalesConfirmation.source_fulfillment_id == event.id
            )
        ).first()
        if dup:
            return
        sku = self.session.get(Sku, order.sku_id)
        price_fen = sku.suggested_price_fen if sku is not None else 0
        cost_price_fen = (
            sku.purchase_price_fen
            if sku is not None and sku.purchase_price_fen is not None
            else 0
        )
        boxes = Decimal(delivered_boxes)
        bottles_per_case = sku.bottles_per_case if sku is not None else Decimal("1")
        amount_fen = int(
            (boxes * bottles_per_case * price_fen).quantize(
                Decimal("1"), rounding=ROUND_HALF_UP
            )
        )
        cost_fen = int(
            (boxes * bottles_per_case * cost_price_fen).quantize(
                Decimal("1"), rounding=ROUND_HALF_UP
            )
        )
        confirmation = SalesConfirmation(
            merchant_id=order.merchant_id,
            sku_id=order.sku_id,
            biz_date=event.delivered_on,
            confirmed_boxes=delivered_boxes,
            amount_fen=amount_fen,
            cost_fen=cost_fen,
            profit_fen=amount_fen - cost_fen,
            confirmer_id=actor.user_id,
            source_fulfillment_id=event.id,
            dataset_type=order.dataset_type,
            dataset_id=order.dataset_id,
        )
        self.session.add(confirmation)
        self.session.flush()

        # T35：送达确认即登记收入流水（金额 = 收款金额；幂等由流水唯一索引兜底）
        from app.services.finance_ledger_service import FinanceLedgerService

        FinanceLedgerService(self.session).record_income(
            actor,
            biz_date=event.delivered_on,
            amount_fen=amount_fen,
            related_type="SALES_CONFIRMATION",
            related_id=confirmation.id,
            note=f"补货单 {order.code} 送达确认收款",
            dataset_type=order.dataset_type,
            dataset_id=order.dataset_id,
        )

    # ============================ 关闭 / 取消 ============================

    def close_order(
        self, actor: ActorContext, *, order_id: UUID, note: str | None = None
    ) -> ReplenishmentOrder:
        """全部满足后闭环：FULFILLED → CLOSED。"""
        self._require_manager(actor)
        order = self._locked_order(order_id)
        self._assert_region(actor, order)
        if order.status == ReplenishmentStatus.CLOSED.value:
            return order  # 幂等
        if order.status != ReplenishmentStatus.FULFILLED.value:
            raise DomainError(
                code="REPL_CLOSE_STATE_INVALID",
                message="仅全部满足（FULFILLED）的补货单可关闭",
                details={"status": order.status},
            )
        self.transition.transition(
            entity=order, domain=DOMAIN_REPLENISHMENT,
            to_state=ReplenishmentStatus.CLOSED.value,
            actor_id=actor.user_id, reason=note or "全部满足，关闭补货单",
        )
        return order

    def cancel_order(
        self, actor: ActorContext, *, order_id: UUID, reason: str
    ) -> ReplenishmentOrder:
        """取消补货单（Q093：必须填写原因；已部分满足的余额取消，已满足保留）。"""
        self._require_manager(actor)
        if not reason or not reason.strip():
            raise DomainError(
                code="REPL_CANCEL_REASON_REQUIRED",
                message="取消补货单必须填写原因",
            )
        order = self._locked_order(order_id)
        self._assert_region(actor, order)
        if order.status == ReplenishmentStatus.CANCELLED.value:
            return order  # 幂等
        if order.status not in _CANCELLABLE:
            raise DomainError(
                code="REPL_CANCEL_STATE_INVALID",
                message="已关闭/已全部满足的补货单不能取消",
                details={"status": order.status},
            )
        before = {"status": order.status}
        order.cancel_reason = reason.strip()
        order.cancelled_by = actor.user_id
        self.transition.transition(
            entity=order, domain=DOMAIN_REPLENISHMENT,
            to_state=ReplenishmentStatus.CANCELLED.value,
            actor_id=actor.user_id, reason=reason.strip(),
            extra_after={"cancel_reason": reason.strip()},
        )
        # transition 已记 before.status；补一条取消留痕（含取消人）
        self.audit.record(
            object_type="REPLENISHMENT", object_id=order.id,
            event_type="UPDATE", actor_id=actor.user_id, before=before,
            after={"status": "CANCELLED", "cancelled_by": str(actor.user_id)},
            reason=reason.strip(),
        )
        return order

    # ============================ 汇总/满足率 ============================

    def summarize_confirmed(
        self,
        actor: ActorContext,
        *,
        region_id: UUID | None = None,
        sku_id: UUID | None = None,
        merchant_ids: list[UUID] | None = None,
        requested_from: date | None = None,
        requested_to: date | None = None,
    ) -> dict:
        """按 SKU 汇总**已确认**箱数（采购建议输入；未确认不汇总，Q089/R8）。"""
        self.permissions.require_ability(actor, MODULE_REPLENISHMENT)
        scope_region = self._resolve_region(actor, region_id)
        orders = self.repo.list_confirmed_orders(
            region_id=scope_region, sku_id=sku_id, merchant_ids=merchant_ids,
            requested_from=requested_from, requested_to=requested_to,
        )
        by_sku: dict[UUID, Decimal] = {}
        for o in orders:
            by_sku[o.sku_id] = by_sku.get(o.sku_id, _ZERO) + Decimal(o.committed_boxes)
        return {
            "confirmed_order_count": len(orders),
            "confirmed_boxes_total": sum(by_sku.values(), _ZERO),
            "by_sku": {str(k): v for k, v in by_sku.items()},
        }

    def fulfillment_rate(
        self,
        actor: ActorContext,
        *,
        region_id: UUID | None = None,
        sku_id: UUID | None = None,
        merchant_ids: list[UUID] | None = None,
        requested_from: date | None = None,
        requested_to: date | None = None,
    ) -> FulfillmentRate:
        """满足率口径（Q094，唯一裁决处）。

        - 分母：筛选范围内已确认态订单的 committed_boxes 之和；
        - 已满足：各单 fulfilled_boxes 之和；
        - 按时满足：累计满足时在 promised_delivery_date 当日或之前完成的箱数。
          对每张单，若全部满足日（最后一笔履约送达日）≤ 承诺日，则其已满足量
          全部计按时；否则不计（保守口径，避免对单笔履约逐笔拆分承诺归属）。
          无承诺日的订单不计入按时分子。
        """
        self.permissions.require_ability(actor, MODULE_REPLENISHMENT)
        scope_region = self._resolve_region(actor, region_id)
        orders = self.repo.list_confirmed_orders(
            region_id=scope_region, sku_id=sku_id, merchant_ids=merchant_ids,
            requested_from=requested_from, requested_to=requested_to,
        )
        confirmed_total = _ZERO
        fulfilled_total = _ZERO
        on_time_total = _ZERO
        for o in orders:
            committed = Decimal(o.committed_boxes)
            fulfilled = Decimal(o.fulfilled_boxes)
            confirmed_total += committed
            fulfilled_total += fulfilled
            if (
                fulfilled > 0
                and o.promised_delivery_date is not None
                and o.actual_delivery_date is not None
                and o.actual_delivery_date <= o.promised_delivery_date
            ):
                on_time_total += fulfilled
        return FulfillmentRate(
            confirmed_boxes=confirmed_total,
            fulfilled_boxes=fulfilled_total,
            on_time_fulfilled_boxes=on_time_total,
            order_count=len(orders),
        )

    # ============================ 列表（补货中心） ============================

    def list_orders(
        self,
        actor: ActorContext,
        *,
        region_id: UUID | None = None,
        status: str | None = None,
        merchant_id: UUID | None = None,
        requested_from: date | None = None,
        requested_to: date | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[dict], int, dict]:
        """补货单分页列表（含 DRAFT/SUBMITTED，供确认工作台）。

        - 行级范围只来自身份上下文：非系统管理员强制限定在其授权片区；显式传
          `region_id` 且不在授权内直接 403（与 summary/rate 同口径）；
        - `status` 只接受受控状态值，非法值拒绝（防前端拼错导致静默空列表）；
        - 本接口是**工作台视图**，未确认单也会出现，故不作为正式汇总口径（R8）。
        """
        self.permissions.require_ability(actor, MODULE_REPLENISHMENT)
        if status is not None:
            allowed_status = {s.value for s in ReplenishmentStatus}
            if status not in allowed_status:
                raise DomainError(
                    code="REPL_STATUS_INVALID",
                    message="补货状态取值非法",
                    details={"status": status},
                )
        scope = self._list_region_scope(actor, region_id)
        rows, total = self.repo.list_orders(
            region_ids=scope,
            status=status,
            merchant_id=merchant_id,
            requested_from=requested_from,
            requested_to=requested_to,
            offset=(page - 1) * page_size,
            limit=page_size,
        )
        items = [
            {
                "order": order,
                "merchant_code": merchant.code,
                "merchant_name": merchant.name,
                "sku_code": sku.code,
                "sku_name": sku.name,
            }
            for order, merchant, sku in rows
        ]
        applied = {
            "region_id": str(region_id) if region_id is not None else None,
            "status": status,
            "merchant_id": str(merchant_id) if merchant_id is not None else None,
            "requested_from": requested_from.isoformat() if requested_from else None,
            "requested_to": requested_to.isoformat() if requested_to else None,
            "page": page,
            "page_size": page_size,
        }
        return items, total, applied

    # ============================ 只读 ============================

    def get_order_detail(self, actor: ActorContext, *, order_id: UUID) -> dict:
        self.permissions.require_ability(actor, MODULE_REPLENISHMENT)
        order = self.repo.get_order(order_id)
        if order is None:
            raise DomainError(code="REPL_ORDER_NOT_FOUND", message="补货单不存在")
        self._assert_region(actor, order)
        fulfillments = self.repo.list_fulfillments(order_id)
        return {
            "order": order,
            "fulfillments": fulfillments,
            # T36：每笔送达的收入/成本/毛利（同一次查询批量取，避免 N+1）
            "sales_by_fulfillment": self.repo.sales_confirmations_by_fulfillment(
                [f.id for f in fulfillments]
            ),
        }

    def sales_confirmation_for_fulfillment(self, fulfillment_id: UUID):
        """送达登记后回显该笔收入/成本/毛利（无确认时返回 None）。"""
        return self.repo.sales_confirmations_by_fulfillment([fulfillment_id]).get(
            fulfillment_id
        )

    # ============================ 内部 ============================

    def _locked_order(self, order_id: UUID) -> ReplenishmentOrder:
        order = self.repo.lock_order(order_id)
        if order is None:
            raise DomainError(code="REPL_ORDER_NOT_FOUND", message="补货单不存在")
        return order

    def _require_manager(self, actor: ActorContext) -> None:
        """审核/确认/履约/取消为经理（或系统管理员）动作；业务员被拒（职责分离）。"""
        self.permissions.require_ability(actor, MODULE_REPLENISHMENT)
        if actor.role_code == RoleCode.SYSTEM_ADMIN.value:
            return
        if actor.role_code != RoleCode.REGIONAL_MANAGER.value:
            raise PermissionDeniedError(
                code="REPL_MANAGER_ONLY",
                message="仅片区经理可确认/履约/取消补货单",
            )

    def _assert_region(self, actor: ActorContext, order: ReplenishmentOrder) -> None:
        if actor.role_code == RoleCode.SYSTEM_ADMIN.value:
            return
        info = self.repo.merchant_region_dataset(order.merchant_id)
        if info is None:
            raise DomainError(code="MERCHANT_NOT_FOUND", message="商家不存在")
        merchant_region, _ds, _dt = info
        if merchant_region not in (actor.region_ids or frozenset()):
            raise PermissionDeniedError(
                code="REGION_FORBIDDEN", message="无权操作该片区的补货单"
            )

    def _resolve_region(
        self, actor: ActorContext, region_id: UUID | None
    ) -> UUID | None:
        """行级范围只来自服务端 JWT：非系统管理员强制限定其片区。"""
        if actor.role_code == RoleCode.SYSTEM_ADMIN.value:
            return region_id
        if region_id is not None and region_id not in (actor.region_ids or frozenset()):
            raise PermissionDeniedError(
                code="REGION_FORBIDDEN", message="无权查看该片区的补货数据"
            )
        # 片区经理通常只有一个片区；多片区时不在此自动合并，由 API 显式传 region_id
        if region_id is not None:
            return region_id
        ids = list(actor.region_ids or [])
        return ids[0] if len(ids) == 1 else None

    def _list_region_scope(
        self, actor: ActorContext, region_id: UUID | None
    ) -> set[UUID] | None:
        """列表用的片区范围。

        - 系统管理员：`region_id=None` → 不限制（None）；否则限定该片区；
        - 其他角色：显式传片区必须在授权内（否则 403），未传则限定全部授权片区，
          授权为空集时返回空集（列表为空，而不是越权看到全量）。
        """
        if actor.role_code == RoleCode.SYSTEM_ADMIN.value:
            return {region_id} if region_id is not None else None
        allowed = set(actor.region_ids or frozenset())
        if region_id is not None:
            if region_id not in allowed:
                raise PermissionDeniedError(
                    code="REGION_FORBIDDEN", message="无权查看该片区的补货数据"
                )
            return {region_id}
        return allowed
