"""采购建议、采购单与厂商供货服务（T18，PRD §4.9 / §5.1）。

红线（违反返工）：
- 采购建议只读**已确认**补货需求（R8/Q095），建议不等于采购单（R17），不落库；
- 采购金额一律由行项目（箱数 × 箱规 × 含税单价/分，单价按瓶）在服务端重算，
  **拒绝请求体总额**
  （Q096）；金额用整数分，禁止 float；
- SUPPLIER_PORTAL_ENABLED 关闭时厂商在服务层也被拒绝（Q097，登录层 T02 已拦截，
  此处纵深防御）；
- 厂商行级范围 = 授权的品牌 × 片区（R18/Q098），越权一律 403，无 read_deleted
  后门；供应商联系人等内部信息对厂商脱敏（Q099），厂商完全看不到员工/薪资/商家
  联系人数据（本域根本不返回）；
- 厂商确认仅写供货量/日期/凭证，**不创建 finance_transactions、不改库存状态**
  （Q100）；
- 重复供货确认按 (行, 厂商用户) 幂等返回原记录（Q101/A17）；
- 采购链所有写操作只接受 DataScope.PRODUCTION，DEMO 上游事实一律拒绝（R33/R34）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import (
    ConflictError,
    DomainError,
    PermissionDeniedError,
)
from app.core.permissions import (
    MODULE_PROCUREMENT,
    MODULE_SETTINGS,
    MODULE_SUPPLIER_CONFIRM,
    ActorContext,
    PermissionService,
)
from app.models import (
    PurchaseOrder,
    PurchaseOrderLine,
    Sku,
    Supplier,
    SupplyConfirmation,
    User,
)
from app.models.enums import (
    PurchaseOrderStatus,
    RoleCode,
    SupplyConfirmationStatus,
)
from app.repositories.purchase_repository import PurchaseRepository
from app.services.audit_service import AuditService
from app.services.business_transition_service import BusinessTransitionService
from app.services.demo_data_guard import DataScope, assert_production_scope
from app.services.state_machine import DOMAIN_PURCHASE

_ZERO = Decimal("0")
SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class SuggestionLine:
    """采购建议单行（R17：建议≠采购单）。库存取 T32 台账真实余额，不编造数字。"""

    sku_id: UUID
    sku_code: str
    brand_id: UUID
    region_id: UUID
    confirmed_unmet_boxes: Decimal   # 已确认但尚未满足的补货箱数
    open_purchase_boxes: Decimal     # 有效在途（未超期）采购箱数
    suggested_purchase_boxes: Decimal
    inventory_available: bool        # 库存台账可读（T32 收发存流水已接入）
    inventory_status: str            # "AVAILABLE"：系统库存（收发存台账）
    # T36：超期未收货的在途量（不参与扣减，需跟进厂商或关闭采购单）
    overdue_purchase_boxes: Decimal = _ZERO
    # 已被有效在途完全覆盖（include_covered=true 时才返回，供页面解释"为什么没有建议"）
    covered_by_open_purchase: bool = False
    # 系统现存箱数（入库 − 出库）；负值说明历史出库多于入库，需账实核对
    available_boxes: Decimal = _ZERO


class ProcurementService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.repo = PurchaseRepository(session)
        self.permissions = PermissionService()
        self.audit = AuditService(session)
        self.transition = BusinessTransitionService(session)

    # ====================== 采购建议（不落库） ======================

    def build_suggestions(
        self,
        actor: ActorContext,
        *,
        brand_id: UUID,
        region_id: UUID,
        supplier_id: UUID | None = None,
        requested_from: date | None = None,
        requested_to: date | None = None,
        dataset_id: UUID | None = None,
        include_covered: bool = False,
        overdue_grace_days: int = 3,
    ) -> list[SuggestionLine]:
        """按品牌/SKU/片区汇总**已确认且尚未满足**的补货需求生成建议。

        未确认（DRAFT/SUBMITTED/REQUESTED）补货行不取（Q095/R8）。
        建议只是计算结果，不写任何表（R17）。

        库存口径（T32）：现存量取 `beverage_stock_entries` 流水的代数和
        （采购收货为正、补货送达为负），按 SKU 汇总后扣减建议量；负库存
        不抵扣需求（只如实展示，提示账实待核对）。

        在途口径（T36）：
        - 有效在途（未超期）扣减建议量；被完全覆盖的 SKU 默认不返回，
          避免重复下单，也避免页面出现"建议采购 0"的噪音；
        - 超期未收货（`expected_date` 早于今天 − grace）不再扣减，
          否则一张无人跟进的采购单会让商家一直断货；
        - `include_covered=True` 时把"已被在途覆盖"的行一并返回并标记
          `covered_by_open_purchase`，供页面解释"为什么这一行没有建议"。

        建议采购 = max(确认缺口 − 有效在途 − 现存库存, 0)。
        """
        self.permissions.require_ability(actor, MODULE_PROCUREMENT)
        self._assert_brand_region(actor, brand_id, region_id)
        ds = self._resolve_dataset(actor, dataset_id)
        user = self.session.get(User, actor.user_id)
        dataset_type = user.dataset_type if user is not None else "PRODUCTION"

        demand = self.repo.confirmed_demand_by_sku(
            dataset_id=ds, brand_id=brand_id, region_id=region_id,
            requested_from=requested_from, requested_to=requested_to,
        )
        if supplier_id is not None:
            supplier = self.repo.get_supplier(supplier_id)
            if supplier is None or supplier.dataset_id != ds:
                raise DomainError(
                    code="SUPPLIER_NOT_FOUND", message="供应商不存在或数据范围不一致")
        # 在途量：指定供应商取该供应商；未指定则汇总该品牌/片区全部在途（T25
        # procurement.suggest 只用「确认需求 + 在途量」，库存域未接入不臆造库存）。
        overdue_before = (
            datetime.now(SHANGHAI_TZ).date() - timedelta(days=max(0, overdue_grace_days))
        )
        open_po, overdue_po = self.repo.open_purchase_detail_scoped(
            dataset_id=ds, brand_id=brand_id, region_id=region_id,
            supplier_id=supplier_id, overdue_before=overdue_before,
        )

        suggestions: list[SuggestionLine] = []
        # 库存属内部数据（R18 延伸）：厂商角色只给需求量，不下发内部库存余额
        inventory_readable = actor.role_code != RoleCode.SUPPLIER.value
        balances = (
            self.repo.stock_balance_by_sku(
                dataset_type=dataset_type, dataset_id=ds, sku_ids=list(demand.keys())
            )
            if inventory_readable
            else {}
        )
        for sku_id, unmet in demand.items():
            unmet = Decimal(unmet)
            if unmet <= 0:
                continue  # 已满足完的已确认单不形成采购需求
            on_the_way = Decimal(open_po.get(sku_id, 0))
            overdue = Decimal(overdue_po.get(sku_id, 0))
            available = Decimal(balances.get(sku_id, _ZERO)) if inventory_readable else _ZERO
            # 负库存（出库多于入库）说明台账缺历史入库，不能拿来抵需求
            deductible = max(available, _ZERO)
            suggested = max(unmet - on_the_way - deductible, _ZERO)
            covered = suggested <= 0
            if covered and not include_covered:
                continue
            sku = self.session.get(Sku, sku_id)
            suggestions.append(
                SuggestionLine(
                    sku_id=sku_id,
                    sku_code=sku.code if sku else str(sku_id),
                    brand_id=brand_id,
                    region_id=region_id,
                    confirmed_unmet_boxes=unmet,
                    open_purchase_boxes=on_the_way,
                    suggested_purchase_boxes=suggested,
                    inventory_available=inventory_readable,
                    inventory_status="AVAILABLE" if inventory_readable else "UNAVAILABLE",
                    overdue_purchase_boxes=overdue,
                    covered_by_open_purchase=covered,
                    available_boxes=available,
                )
            )
        return sorted(suggestions, key=lambda s: s.sku_code)

    def suggestion_diagnostics(
        self,
        actor: ActorContext,
        *,
        brand_id: UUID,
        region_id: UUID,
        supplier_id: UUID | None = None,
        dataset_id: UUID | None = None,
    ) -> dict:
        """采购建议口径诊断：把"为什么没有建议"变成页面上一句话。

        返回的是同一口径的汇总（已确认未满足 / 有效在途 / 超期在途 / 建议量），
        只读不落库；页面据此解释空态，而不是让用户猜是不是接口坏了。
        """
        self.permissions.require_ability(actor, MODULE_PROCUREMENT)
        self._assert_brand_region(actor, brand_id, region_id)
        ds = self._resolve_dataset(actor, dataset_id)
        user = self.session.get(User, actor.user_id)
        dataset_type = user.dataset_type if user is not None else "PRODUCTION"

        demand = self.repo.confirmed_demand_by_sku(
            dataset_id=ds, brand_id=brand_id, region_id=region_id
        )
        overdue_before = datetime.now(SHANGHAI_TZ).date() - timedelta(days=3)
        open_po, overdue_po = self.repo.open_purchase_detail_scoped(
            dataset_id=ds, brand_id=brand_id, region_id=region_id,
            supplier_id=supplier_id, overdue_before=overdue_before,
        )

        inventory_readable = actor.role_code != RoleCode.SUPPLIER.value
        balances = (
            self.repo.stock_balance_by_sku(
                dataset_type=dataset_type, dataset_id=ds, sku_ids=list(demand.keys())
            )
            if inventory_readable
            else {}
        )
        unmet_total = _ZERO
        suggested_total = _ZERO
        available_total = _ZERO
        covered = 0
        for sku_id, unmet in demand.items():
            unmet = Decimal(unmet)
            if unmet <= 0:
                continue
            unmet_total += unmet
            available = Decimal(balances.get(sku_id, _ZERO)) if inventory_readable else _ZERO
            available_total += available
            rest = max(
                unmet - Decimal(open_po.get(sku_id, 0)) - max(available, _ZERO), _ZERO
            )
            if rest <= 0:
                covered += 1
            suggested_total += rest

        return {
            "dataset_type": user.dataset_type if user is not None else None,
            "dataset_id": str(ds) if ds is not None else None,
            "confirmed_sku_count": len([v for v in demand.values() if Decimal(v) > 0]),
            "confirmed_unmet_boxes": unmet_total,
            "open_purchase_boxes": sum(
                (Decimal(v) for v in open_po.values()), _ZERO
            ),
            "overdue_purchase_boxes": sum(
                (Decimal(v) for v in overdue_po.values()), _ZERO
            ),
            "suggested_purchase_boxes": suggested_total,
            "available_inventory_boxes": available_total,
            "covered_sku_count": covered,
            "overdue_grace_days": 3,
        }

    # ====================== 采购单（金额服务端重算） ======================

    def create_purchase_order(
        self,
        actor: ActorContext,
        *,
        supplier_id: UUID,
        brand_id: UUID,
        region_id: UUID,
        expected_date: date | None,
        # 行：(sku_id, 箱数, 含税单价分|None)；单价是**每瓶**含税价，
        # 缺省时取 SKU 参考进价（元/瓶）；箱价 = 单价 × 箱规，由服务端换算。
        # **不含**行金额/总额——一律服务端重算
        lines: list[tuple[UUID, Decimal, int | None]],
        note: str | None = None,
        dataset_id: UUID | None = None,
    ) -> PurchaseOrder:
        """内部（经理/财务/管理员）创建采购单：DRAFT 落库后直接确认到 CONFIRMED。

        厂商角色被拒绝（Q098：厂商只能确认供货，不能下单）。
        任何请求体携带的 total_amount 均不在签名中——从源头拒绝前端总额（Q096）。
        """
        self.permissions.require_ability(actor, MODULE_PROCUREMENT)
        if actor.role_code == RoleCode.SUPPLIER.value:
            raise PermissionDeniedError(
                code="PURCHASE_SUPPLIER_CANNOT_ORDER",
                message="厂商不能创建采购单，只能确认供货",
            )
        self._assert_brand_region(actor, brand_id, region_id)
        ds = self._resolve_dataset(actor, dataset_id)
        if not lines:
            raise DomainError(
                code="PURCHASE_LINES_REQUIRED", message="采购单至少需要一行")

        supplier = self.repo.get_supplier(supplier_id)
        if supplier is None or not supplier.active or supplier.dataset_id != ds:
            raise DomainError(
                code="SUPPLIER_NOT_FOUND",
                message="供应商不存在、已停用或数据范围不一致",
            )
        # R33/R34：采购链只接受显式 PRODUCTION，DEMO/ALL 一律拒绝
        assert_production_scope(
            scope=DataScope.PRODUCTION, where="purchase_order.create")

        # 先把所有行校验 + 服务端重算做完，全部通过后才建头/行——任何 DEMO 上游
        # 或非法输入都不得留下半截采购单（R33/R34，Q096）
        seen_skus: set[UUID] = set()
        normalized: list[tuple[UUID, Decimal, int, int]] = []
        total_fen = 0
        for sku_id, qty, unit_price_fen in lines:
            qty = Decimal(qty)
            if qty <= 0:
                raise DomainError(
                    code="PURCHASE_QTY_INVALID", message="采购箱数必须大于 0")
            if unit_price_fen is not None and unit_price_fen < 0:
                raise DomainError(
                    code="PURCHASE_PRICE_INVALID",
                    message="含税单价不能为负（单位：分）")
            if sku_id in seen_skus:
                raise DomainError(
                    code="PURCHASE_DUPLICATE_SKU", message="同一采购单内 SKU 不得重复")
            seen_skus.add(sku_id)
            sku = self.session.get(Sku, sku_id)
            if sku is None or sku.dataset_id != ds:
                raise DomainError(
                    code="SKU_NOT_FOUND", message="SKU 不存在或数据范围不一致")
            if unit_price_fen is None:
                # T36：单价不手填 → 用主数据参考进价自动生成；缺失则明确报错
                if sku.purchase_price_fen is None:
                    raise DomainError(
                        code="PURCHASE_PRICE_REQUIRED",
                        message=(
                            f"SKU {sku.code} 未维护参考进价，请先在主数据补录或手填含税单价"
                        ),
                        details={"sku_code": sku.code},
                    )
                unit_price_fen = int(sku.purchase_price_fen)
            # 服务端重算行金额：箱数 × 箱规 × 含税单价（元/瓶，分）
            # 单价按瓶维护、数量按箱下单，缺箱规时系数取 1（不放大也不归零）
            line_amount = int(
                (qty * sku.bottles_per_case * Decimal(unit_price_fen)).quantize(
                    Decimal("1"), rounding=ROUND_HALF_UP
                )
            )
            total_fen += line_amount
            normalized.append((sku_id, qty, unit_price_fen, line_amount))

        order = PurchaseOrder(
            code=f"PO-{uuid4().hex[:14].upper()}",
            supplier_id=supplier_id,
            brand_id=brand_id,
            region_id=region_id,
            expected_date=expected_date,
            status=PurchaseOrderStatus.DRAFT.value,
            created_by_id_internal=actor.user_id,
            note=note,
            dataset_type=DataScope.PRODUCTION.value,
            dataset_id=ds,
        )
        self.repo.add_order(order)
        for sku_id, qty, unit_price_fen, line_amount in normalized:
            self.repo.add_line(
                PurchaseOrderLine(
                    order_id=order.id,
                    sku_id=sku_id,
                    quantity_cases=qty,
                    tax_inclusive_unit_price_fen=unit_price_fen,
                    line_amount_fen=line_amount,
                    dataset_type=DataScope.PRODUCTION.value,
                    dataset_id=ds,
                )
            )
        # 头表总额只采信服务端重算值（Q096）
        order.total_amount_fen = total_fen

        # DRAFT → CONFIRMED（内部审核合一；后续如需多级审批在后续卡扩展）
        self.transition.transition(
            entity=order, domain=DOMAIN_PURCHASE,
            to_state=PurchaseOrderStatus.CONFIRMED.value,
            actor_id=actor.user_id,
            reason="内部创建采购单并确认，金额由行项目服务端重算",
            extra_after={"total_amount_fen": total_fen, "line_count": len(lines)},
        )
        order.confirmed_by_id = actor.user_id
        order.confirmed_at = date.today()
        self.session.flush()
        return order

    def receive_purchase_order(
        self, actor: ActorContext, *, order_id: UUID
    ) -> PurchaseOrder:
        """内部收货：SUPPLIER_CONFIRMED → RECEIVED。收货即按行入库存台账
        （T32，数量取厂商确认供货量，幂等）；不触发付款（财务付款是独立动作）。"""
        self.permissions.require_ability(actor, MODULE_PROCUREMENT)
        order = self.repo.lock_order(order_id)
        if order is None:
            raise DomainError(code="PURCHASE_ORDER_NOT_FOUND", message="采购单不存在")
        self._assert_brand_region(actor, order.brand_id, order.region_id)
        if order.status == PurchaseOrderStatus.RECEIVED.value:
            return order
        # 行级兜底：头单虽已 SUPPLIER_CONFIRMED，仍可能有未确认行（历史/演示数据），
        # 此时收货会把缺行永久冻结（之后厂商无法再确认），先拦截并提示（Q190）。
        confirmed_line_ids = {
            c.order_line_id
            for c in self.repo.list_confirmations(order.id)
            if c.status == SupplyConfirmationStatus.CONFIRMED.value
        }
        missing = [
            ln for ln in self.repo.list_lines(order.id)
            if ln.id not in confirmed_line_ids
        ]
        if missing:
            raise DomainError(
                code="PURCHASE_RECEIVE_UNCONFIRMED",
                message=f"仍有 {len(missing)} 行未确认供货，需厂商确认后再收货",
                details={
                    "missing_line_ids": [str(ln.id) for ln in missing],
                    "missing_sku_ids": [str(ln.sku_id) for ln in missing],
                },
            )
        self.transition.transition(
            entity=order, domain=DOMAIN_PURCHASE,
            to_state=PurchaseOrderStatus.RECEIVED.value,
            actor_id=actor.user_id, reason="内部确认收货",
        )
        order.received_at = date.today()
        # T32：收货入库记账（台账幂等：单×SKU 已记过则跳过，同事务）
        from app.services.inventory_service import InventoryService

        InventoryService(self.session).record_purchase_receipt(
            actor, order, biz_date=order.received_at
        )

        # T35：收货即登记进货支出流水（金额 = 行含税金额合计；只记账不支付）
        from app.models.finance import SOURCE_EXPENSE_PROCUREMENT
        from app.services.finance_ledger_service import FinanceLedgerService

        total_fen = sum(int(ln.line_amount_fen) for ln in self.repo.list_lines(order.id))
        FinanceLedgerService(self.session).record_expense(
            actor,
            biz_date=order.received_at,
            amount_fen=total_fen,
            source_kind=SOURCE_EXPENSE_PROCUREMENT,
            related_type="PURCHASE_ORDER",
            related_id=order.id,
            note=f"采购单 {order.code} 收货进货支出",
            dataset_type=order.dataset_type,
            dataset_id=order.dataset_id,
        )
        self.session.flush()
        return order

    # ====================== 厂商供货确认 ======================

    def confirm_supply(
        self,
        actor: ActorContext,
        *,
        order_id: UUID,
        line_id: UUID,
        confirmed_quantity_cases: Decimal,
        expected_delivery_date: date,
        proof_file_id: UUID | None = None,
        note: str | None = None,
    ) -> SupplyConfirmation:
        """厂商按行确认供货量与预计到货日（PRD §5.1）。

        - 门户开关关闭 → 403（Q097，登录层之外的服务层纵深防御）；
        - 仅 SUPPLIER 角色、且经品牌×片区授权（Q098）；
        - 不创建财务流水、不改库存（Q100）；
        - (line, supplier_user) 重复确认返回原记录（Q101）；
        - 头单全部行已确认 → CONFIRMED → SUPPLIER_CONFIRMED（状态机同事务）。
        """
        self._require_portal_enabled()
        self.permissions.require_ability(actor, MODULE_SUPPLIER_CONFIRM)
        if actor.role_code != RoleCode.SUPPLIER.value:
            raise PermissionDeniedError(
                code="SUPPLY_CONFIRM_SUPPLIER_ONLY",
                message="只有厂商账号可以确认供货",
            )
        confirmed_quantity_cases = Decimal(confirmed_quantity_cases)
        if confirmed_quantity_cases <= 0:
            raise DomainError(
                code="SUPPLY_QTY_INVALID", message="确认供货箱数必须大于 0")

        order = self.repo.lock_order(order_id)
        if order is None:
            raise DomainError(code="PURCHASE_ORDER_NOT_FOUND", message="采购单不存在")
        line = self.repo.get_line(line_id)
        if line is None or line.order_id != order_id:
            raise DomainError(
                code="PURCHASE_LINE_NOT_FOUND",
                message="采购行不存在或不属于该采购单",
            )

        # 品牌×片区授权（Q098）：无授权 → 403
        auth = self.repo.authorization(
            user_id=actor.user_id,
            brand_id=order.brand_id, region_id=order.region_id)
        if auth is None:
            raise PermissionDeniedError(
                code="SUPPLY_SCOPE_FORBIDDEN",
                message="未授权该品牌/片区的供货确认",
            )

        # 幂等优先于状态校验：同一供应商组织对该行已有确认（含同事账号提交的）时
        # 直接返回原记录，不报错也不重复落库（Q101/A17/Q189）
        existing = self.repo.find_confirmation_by_line_supplier(
            line_id=line_id, supplier_id=auth.supplier_id)
        if existing is not None:
            return existing

        # 允许的状态：待供货、驳回后重新协商；已标记 SUPPLIER_CONFIRMED 但缺行时
        # 允许补录（历史/演示数据可能只确认了部分行，否则该行永远无法再确认）
        if order.status not in (
            PurchaseOrderStatus.CONFIRMED.value,
            PurchaseOrderStatus.SUPPLY_REJECTED.value,
            PurchaseOrderStatus.SUPPLIER_CONFIRMED.value,
        ):
            raise DomainError(
                code="SUPPLY_CONFIRM_STATE_INVALID",
                message="采购单已收货/已取消，供货确认已截止（仅待供货或被驳回后重新协商的单可确认）",
                details={"status": order.status},
            )

        if proof_file_id is not None:
            from app.models import FileObject

            proof = self.session.get(FileObject, proof_file_id)
            if proof is None or proof.dataset_id != order.dataset_id:
                raise DomainError(
                    code="SUPPLY_PROOF_INVALID",
                    message="供货凭证不存在或数据范围不一致",
                )

        record = self.repo.add_confirmation(
            SupplyConfirmation(
                order_id=order.id,
                order_line_id=line.id,
                supplier_id=auth.supplier_id,
                supplier_user_id=actor.user_id,
                status=SupplyConfirmationStatus.CONFIRMED.value,
                confirmed_quantity_cases=confirmed_quantity_cases,
                expected_delivery_date=expected_delivery_date,
                proof_file_id=proof_file_id,
                note=note,
                dataset_type=order.dataset_type,
                dataset_id=order.dataset_id,
            )
        )

        # 所有行都已有确认记录 → 头单迁移为 SUPPLIER_CONFIRMED
        all_lines = self.repo.list_lines(order.id)
        all_confirmed = all(
            self.repo.find_confirmation_by_line_supplier(
                line_id=ln.id, supplier_id=auth.supplier_id) is not None
            for ln in all_lines
        )
        if all_confirmed and order.status != PurchaseOrderStatus.SUPPLIER_CONFIRMED.value:
            self.transition.transition(
                entity=order, domain=DOMAIN_PURCHASE,
                to_state=PurchaseOrderStatus.SUPPLIER_CONFIRMED.value,
                actor_id=actor.user_id,
                reason="厂商完成全部行供货确认",
                extra_after={"confirmed_lines": len(all_lines)},
            )
        self.session.flush()
        return record

    def confirm_supply_by_code(
        self,
        actor: ActorContext,
        *,
        procurement_code: str,
        confirmed_quantity_cases: Decimal,
        expected_delivery_date: date,
        proof_file_id: UUID | None = None,
        note: str = "",
    ) -> SupplyConfirmation:
        """MCP 受控入口：按采购单号定位待确认行后执行供货确认（T24）。

        适配层只能提供采购单号（PRD 工具参数 procurement_code），由本 Service
        解析订单与其待确认行；供应商授权、状态机、并发仍由 confirm_supply 裁决。
        """
        order = self.repo.get_order_by_code(procurement_code)
        if order is None:
            raise DomainError(
                code="PURCHASE_ORDER_NOT_FOUND",
                message="采购单不存在",
                details={"procurement_code": procurement_code},
            )
        line = next(
            (
                ln
                for ln in self.repo.list_lines(order.id)
                if self.repo.find_confirmation(
                    line_id=ln.id, supplier_user_id=actor.user_id
                )
                is None
            ),
            None,
        )
        if line is None:
            raise ConflictError(
                code="SUPPLY_ALREADY_CONFIRMED",
                message="该采购单不存在待你确认的供货行",
                details={"procurement_code": procurement_code},
            )
        return self.confirm_supply(
            actor,
            order_id=order.id,
            line_id=line.id,
            confirmed_quantity_cases=confirmed_quantity_cases,
            expected_delivery_date=expected_delivery_date,
            proof_file_id=proof_file_id,
            note=note,
        )

    def reject_supply(
        self,
        actor: ActorContext,
        *,
        order_id: UUID,
        line_id: UUID,
        reason: str,
    ) -> SupplyConfirmation:
        """厂商驳回某行供货（必须留原因）：CONFIRMED → SUPPLY_REJECTED。"""
        self._require_portal_enabled()
        self.permissions.require_ability(actor, MODULE_SUPPLIER_CONFIRM)
        if actor.role_code != RoleCode.SUPPLIER.value:
            raise PermissionDeniedError(
                code="SUPPLY_CONFIRM_SUPPLIER_ONLY",
                message="只有厂商账号可以驳回供货",
            )
        if not reason or not reason.strip():
            raise DomainError(
                code="SUPPLY_REJECT_REASON_REQUIRED", message="驳回供货必须填写原因")
        order = self.repo.lock_order(order_id)
        if order is None:
            raise DomainError(code="PURCHASE_ORDER_NOT_FOUND", message="采购单不存在")
        line = self.repo.get_line(line_id)
        if line is None or line.order_id != order_id:
            raise DomainError(
                code="PURCHASE_LINE_NOT_FOUND",
                message="采购行不存在或不属于该采购单",
            )
        auth = self.repo.authorization(
            user_id=actor.user_id,
            brand_id=order.brand_id, region_id=order.region_id)
        if auth is None:
            raise PermissionDeniedError(
                code="SUPPLY_SCOPE_FORBIDDEN", message="未授权该品牌/片区")
        if order.status != PurchaseOrderStatus.CONFIRMED.value:
            raise DomainError(
                code="SUPPLY_REJECT_STATE_INVALID",
                message="仅待供货（CONFIRMED）的采购单可驳回",
                details={"status": order.status},
            )
        existing = self.repo.find_confirmation(
            line_id=line_id, supplier_user_id=actor.user_id)
        if existing is not None and existing.status == "REJECTED":
            return existing  # 幂等
        if existing is None:
            record = self.repo.add_confirmation(
                SupplyConfirmation(
                    order_id=order.id,
                    order_line_id=line.id,
                    supplier_id=auth.supplier_id,
                    supplier_user_id=actor.user_id,
                    status=SupplyConfirmationStatus.REJECTED.value,
                    reject_reason=reason.strip(),
                    dataset_type=order.dataset_type,
                    dataset_id=order.dataset_id,
                )
            )
        else:
            record = existing
        self.transition.transition(
            entity=order, domain=DOMAIN_PURCHASE,
            to_state=PurchaseOrderStatus.SUPPLY_REJECTED.value,
            actor_id=actor.user_id, reason=reason.strip(),
        )
        self.session.flush()
        return record

    # ====================== 只读（脱敏视图） ======================

    def list_orders_for_actor(
        self, actor: ActorContext, *, brand_id: UUID | None = None,
        region_id: UUID | None = None,
    ) -> list[PurchaseOrder]:
        """列采购单。厂商只返回其授权品牌×片区的单（Q098）。"""
        from sqlalchemy import select

        self.permissions.require_ability(actor, MODULE_PROCUREMENT)
        stmt = select(PurchaseOrder)
        if actor.role_code == RoleCode.SUPPLIER.value:
            scopes = self.repo.supplier_user_scopes(actor.user_id)
            if not scopes:
                return []
            allowed = {(s.brand_id, s.region_id) for s in scopes}
            orders = list(self.session.scalars(
                stmt.order_by(PurchaseOrder.created_at.desc())).all())
            return [
                o for o in orders
                if (o.brand_id, o.region_id) in allowed
                and (brand_id is None or o.brand_id == brand_id)
                and (region_id is None or o.region_id == region_id)
            ]
        if brand_id is not None and region_id is not None:
            self._assert_brand_region(actor, brand_id, region_id)
            stmt = stmt.where(
                PurchaseOrder.brand_id == brand_id,
                PurchaseOrder.region_id == region_id,
            )
        return list(
            self.session.scalars(
                stmt.order_by(PurchaseOrder.created_at.desc())).all()
        )

    # ====================== 下拉选项（页面装配用，只读） ======================

    def list_supplier_options(
        self, actor: ActorContext, *, brand_id: UUID | None = None,
        region_id: UUID | None = None,
    ) -> list[dict]:
        """供应商下拉（建单/供货页面用）。

        - 内部角色：本数据集在职供应商（可传品牌×片区做展示过滤，并按授权校验）；
        - 厂商：只返回其 SupplierAuthorization 命中的供应商，避免越权看到同业厂商；
        - 只返回 id/编码/名称，**不含**联系人——下拉不需要，也就没有脱敏歧义（Q099）。
        """
        if not (
            self.permissions.has_ability(actor, MODULE_PROCUREMENT)
            or self.permissions.has_ability(actor, MODULE_SUPPLIER_CONFIRM)
        ):
            raise PermissionDeniedError(
                code="SUPPLIER_VIEW_FORBIDDEN", message="无权查看供应商信息"
            )
        if actor.role_code == RoleCode.SUPPLIER.value:
            scopes = self.repo.supplier_user_scopes(actor.user_id)
            allowed = {
                s.supplier_id
                for s in scopes
                if (brand_id is None or s.brand_id == brand_id)
                and (region_id is None or s.region_id == region_id)
            }
            suppliers = self.repo.list_suppliers_by_ids(allowed)
        else:
            if brand_id is not None and region_id is not None:
                self._assert_brand_region(actor, brand_id, region_id)
            suppliers = self.repo.list_suppliers(self._resolve_dataset(actor, None))
        return [
            {"id": s.id, "code": s.code, "name": s.name} for s in suppliers
        ]

    def list_sku_options(
        self, actor: ActorContext, *, brand_id: UUID | None = None
    ) -> list:
        """SKU 下拉：用于采购行展示名称与手工补行（不改变建议口径）。"""
        self.permissions.require_ability(actor, MODULE_PROCUREMENT)
        return self.repo.list_skus(
            self._resolve_dataset(actor, None), brand_id=brand_id
        )

    def list_supply_confirmations(
        self, actor: ActorContext, *, order_id: UUID
    ) -> list[SupplyConfirmation]:
        """采购单的供货确认记录（逐行确认/拒绝轨迹）。

        - 内部角色：需品牌×片区授权，可见该单全部记录（用于跟踪缺行与拒绝原因）；
        - 厂商：需持有该单品牌×片区的授权，返回**同一供应商组织**的记录
          （多账号协作时，同事已确认的行不应再显示"待确认"，Q189）。
        """
        if not (
            self.permissions.has_ability(actor, MODULE_PROCUREMENT)
            or self.permissions.has_ability(actor, MODULE_SUPPLIER_CONFIRM)
        ):
            raise PermissionDeniedError(
                code="SUPPLIER_VIEW_FORBIDDEN", message="无权查看供货确认记录"
            )
        order = self.repo.get_order(order_id)
        if order is None:
            raise DomainError(
                code="PURCHASE_ORDER_NOT_FOUND", message="采购单不存在"
            )
        if actor.role_code == RoleCode.SUPPLIER.value:
            auth = self.repo.authorization(
                user_id=actor.user_id,
                brand_id=order.brand_id,
                region_id=order.region_id,
            )
            if auth is None:
                raise PermissionDeniedError(
                    code="PURCHASE_ORDER_FORBIDDEN",
                    message="无权查看该采购单（未授权该品牌×片区）",
                )
            return [
                c
                for c in self.repo.list_confirmations(order_id)
                if c.supplier_id == auth.supplier_id
            ]
        self._assert_brand_region(actor, order.brand_id, order.region_id)
        return self.repo.list_confirmations(order_id)

    def get_supplier_public_view(
        self, actor: ActorContext, *, supplier_id: UUID
    ) -> dict:
        """        供应商视图：厂商身份下联系人/电话一律脱敏（Q099/R18）。"""
        # 内部采购视角或厂商供货视角任一即可
        if not (
            self.permissions.has_ability(actor, MODULE_PROCUREMENT)
            or self.permissions.has_ability(actor, MODULE_SUPPLIER_CONFIRM)
        ):
            raise PermissionDeniedError(
                code="SUPPLIER_VIEW_FORBIDDEN",
                message="无权查看供应商信息",
            )
        supplier = self.repo.get_supplier(supplier_id)
        if supplier is None:
            raise DomainError(code="SUPPLIER_NOT_FOUND", message="供应商不存在")
        is_internal = actor.role_code in (
            RoleCode.SYSTEM_ADMIN.value,
            RoleCode.FINANCE.value,
            RoleCode.REGIONAL_MANAGER.value,
            RoleCode.SUPERVISOR.value,
        )
        if is_internal:
            return {
                "id": str(supplier.id),
                "code": supplier.code,
                "name": supplier.name,
                "contact_name": supplier.contact_name,
                "contact_phone": supplier.contact_phone,
            }
        # 厂商：只给名称/编码，联系人字段直接不返回（而不是打码字符串冒充真实值）
        return {
            "id": str(supplier.id),
            "code": supplier.code,
            "name": supplier.name,
            "contact_name": None,
            "contact_phone": None,
        }

    # ====================== 供应商台账管理（SETTINGS 角色） ======================

    def create_supplier(
        self,
        actor: ActorContext,
        *,
        code: str,
        name: str,
        contact_name: str | None = None,
        contact_phone: str | None = None,
    ) -> Supplier:
        """创建供应商（仅 SETTINGS 角色）。

        唯一约束：同一数据集内 code 不可重复。
        写入强制 PRODUCTION，DEMO 不可进（Q096）。
        """
        self.permissions.require_ability(actor, MODULE_SETTINGS)
        self._assert_internal_only(actor)
        ds = self._resolve_dataset(actor, None)
        if not code or not code.strip():
            raise DomainError(
                code="SUPPLIER_CODE_REQUIRED", message="供应商编码不能为空")
        if not name or not name.strip():
            raise DomainError(
                code="SUPPLIER_NAME_REQUIRED", message="供应商名称不能为空")
        existing = self.repo.get_supplier_by_code(code.strip(), ds)
        if existing is not None:
            raise DomainError(
                code="SUPPLIER_CODE_DUPLICATE",
                message=f"供应商编码 '{code}' 已存在",
            )
        if len(name) > 200:
            raise DomainError(
                code="SUPPLIER_NAME_TOO_LONG",
                message="供应商名称不得超过 200 字符",
            )
        supplier = Supplier(
            code=code.strip(),
            name=name.strip(),
            contact_name=contact_name.strip() if contact_name else None,
            contact_phone=contact_phone.strip() if contact_phone else None,
            active=True,
            dataset_type=DataScope.PRODUCTION.value,
            dataset_id=ds,
        )
        return self.repo.add_supplier(supplier)

    def toggle_supplier_active(
        self,
        actor: ActorContext,
        *,
        supplier_id: UUID,
    ) -> Supplier:
        """启用/停用供应商（仅 SETTINGS 角色）。

        停用不影响历史采购单；新建采购单时已停用供应商会被拒绝
        （`SUPPLIER_NOT_FOUND` 分支的 `supplier.active` 检查）。
        """
        self.permissions.require_ability(actor, MODULE_SETTINGS)
        self._assert_internal_only(actor)
        ds = self._resolve_dataset(actor, None)
        supplier = self.repo.get_supplier(supplier_id)
        if supplier is None or supplier.dataset_id != ds:
            raise DomainError(
                code="SUPPLIER_NOT_FOUND", message="供应商不存在或不在本数据集")
        if str(supplier.dataset_type) == DataScope.DEMO.value:
            raise DomainError(
                code="SUPPLIER_DEMO_READ_ONLY",
                message="演示供应商不可手动启停",
            )
        supplier.active = not supplier.active
        self.session.flush()
        return supplier

    def list_supplier_management(
        self, actor: ActorContext,
    ) -> list[dict]:
        """供应商管理台账（含停用），仅供 SETTINGS 角色查看。"""
        self.permissions.require_ability(actor, MODULE_SETTINGS)
        self._assert_internal_only(actor)
        ds = self._resolve_dataset(actor, None)
        suppliers = self.repo.list_all_suppliers(ds)
        return [
            {
                "id": str(s.id),
                "code": s.code,
                "name": s.name,
                "contact_name": s.contact_name,
                "contact_phone": s.contact_phone,
                "active": s.active,
            }
            for s in suppliers
        ]

    def _assert_internal_only(self, actor: ActorContext) -> None:
        """非内部角色（厂商）不得访问供应商管理入口。"""
        if actor.role_code == RoleCode.SUPPLIER.value:
            raise PermissionDeniedError(
                code="SUPPLIER_MANAGEMENT_FORBIDDEN",
                message="厂商不能访问供应商管理",
            )

    # ====================== 内部辅助 ======================

    def _require_portal_enabled(self) -> None:
        """Q097：服务层纵深防御——开关关闭时厂商供货能力直接 403。"""
        if not get_settings().supplier_portal_enabled:
            raise PermissionDeniedError(
                code="SUPPLIER_PORTAL_DISABLED",
                message="厂商门户未开启（SUPPLIER_PORTAL_ENABLED=false）",
            )

    def _assert_brand_region(
        self, actor: ActorContext, brand_id: UUID, region_id: UUID
    ) -> None:
        if actor.role_code == RoleCode.SYSTEM_ADMIN.value:
            return
        if brand_id not in (actor.brand_ids or frozenset()):
            raise PermissionDeniedError(
                code="BRAND_FORBIDDEN", message="无权操作该品牌的数据")
        if region_id not in (actor.region_ids or frozenset()):
            raise PermissionDeniedError(
                code="REGION_FORBIDDEN", message="无权操作该片区的数据")

    def _resolve_dataset(
        self, actor: ActorContext, dataset_id: UUID | None
    ) -> UUID:
        """数据集只从服务端登录用户解析；禁止客户端指定（行级范围红线）。

        若调用方显式传入 dataset_id，仅允许与登录用户归属一致（用于内部跨表
        查询的显式对齐）；否则以 users.dataset_id 为准。
        """
        user = self.session.get(User, actor.user_id)
        if user is None:
            raise DomainError(code="USER_NOT_FOUND", message="登录用户不存在")
        if dataset_id is not None and dataset_id != user.dataset_id:
            raise PermissionDeniedError(
                code="DATASET_SCOPE_FORBIDDEN",
                message="数据范围必须与登录身份一致，禁止客户端指定",
            )
        return user.dataset_id
