"""T21 指标/风险/报表持久化查询（PRD §1.5 指标、§4.11 风险、§4.10 报表）。

只做查询；口径（口径文案、分母边界、数据不足降级）全部在 Service。
所有查询一律接收显式 `dataset_id`（正式口径 PRODUCTION 由 Service 用
`assert_production_scope` 强制，本层不再读 DEMO）与 `MerchantScope` 行级范围。
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models.contract import Contract, ContractVersion
from app.models.finance import FinanceTransaction
from app.models.freezer import FreezerAsset, FreezerMovement
from app.models.merchant import Merchant, MerchantGradeFrequency
from app.models.payroll import Expense, PayrollStatement
from app.models.procurement import ProcurementOrder
from app.models.purchase import PurchaseOrder, PurchaseOrderLine
from app.models.replenishment import ReplenishmentOrder
from app.models.sales import SalesConfirmation
from app.models.visit import VisitRecord, VisitTask
from app.services.report_scope import MerchantScope

_CONTRACT_EFFECTIVE = "EFFECTIVE"
# “已生效过”的状态集合（用于新签统计：含当期终止者）
_EFFECTIVE_EVER = ("EFFECTIVE", "TERMINATION_PENDING", "TERMINATED")
_EMPTY_UUID = UUID(int=0)
_DEAD = (_EMPTY_UUID,)


class ReportingRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    # ============================ 行级范围子查询 ============================

    def _merchant_subquery(self, dataset_id: UUID, scope: MerchantScope | None):
        """按行级范围构造 merchant id 子查询。"""
        stmt = select(Merchant.id).where(Merchant.dataset_id == dataset_id)
        if scope is not None and scope.region_ids is not None:
            stmt = stmt.where(Merchant.region_id.in_(scope.region_list or list(_DEAD)))
        if scope is not None and scope.merchant_ids is not None:
            stmt = stmt.where(Merchant.id.in_(scope.merchant_list or list(_DEAD)))
        return stmt

    def merchant_region_map(self, dataset_id: UUID) -> dict[UUID, UUID]:
        rows = self.session.execute(
            select(Merchant.id, Merchant.region_id).where(Merchant.dataset_id == dataset_id)
        ).all()
        return {r[0]: r[1] for r in rows}

    def scope_merchant_ids(
        self, dataset_id: UUID, scope: MerchantScope | None,
    ) -> set[UUID]:
        stmt = select(Merchant.id).where(Merchant.dataset_id == dataset_id)
        if scope is not None and scope.region_ids is not None:
            stmt = stmt.where(Merchant.region_id.in_(scope.region_list or list(_DEAD)))
        if scope is not None and scope.merchant_ids is not None:
            stmt = stmt.where(Merchant.id.in_(scope.merchant_list or list(_DEAD)))
        return set(self.session.execute(stmt).scalars().all())

    def merchant_rows(
        self, dataset_id: UUID, scope: MerchantScope | None,
    ) -> dict[UUID, tuple[str | None, UUID]]:
        """{merchant_id: (grade, region_id)}，仅 ACTIVE 合作商家且在行级范围内。"""
        stmt = select(Merchant.id, Merchant.grade, Merchant.region_id).where(
            Merchant.dataset_id == dataset_id, Merchant.status == "ACTIVE"
        )
        if scope is not None and scope.region_ids is not None:
            stmt = stmt.where(Merchant.region_id.in_(scope.region_list or list(_DEAD)))
        if scope is not None and scope.merchant_ids is not None:
            stmt = stmt.where(Merchant.id.in_(scope.merchant_list or list(_DEAD)))
        return {r[0]: (r[1], r[2]) for r in self.session.execute(stmt).all()}

    # ============================ 指标：合同 ============================

    def effective_merchant_ids_at(
        self, as_of: date, dataset_id: UUID, scope: MerchantScope | None,
    ) -> set[UUID]:
        stmt = select(func.distinct(Contract.merchant_id)).where(
            Contract.dataset_id == dataset_id,
            Contract.status == _CONTRACT_EFFECTIVE,
            Contract.effective_from.is_not(None),
            Contract.effective_from <= as_of,
            or_(Contract.expire_at.is_(None), Contract.expire_at >= as_of),
        ).where(Contract.merchant_id.in_(self._merchant_subquery(dataset_id, scope)))
        stmt = stmt.where(Contract.merchant_id.in_(
            select(Merchant.id).where(
                Merchant.dataset_id == dataset_id, Merchant.status != "TERMINATED"
            )
        ))
        return set(self.session.execute(stmt).scalars().all())

    def new_contract_count(
        self, start: date, end: date, dataset_id: UUID, scope: MerchantScope | None,
    ) -> int:
        """统计期内签署且**已生效过**的合同数（不含纯草稿/待审创建）。

        含当期已终止的合同：取消率分母的“当期新生效合同数”须计入当期生而又终止者
        （PRD §1.5 取消率口径）。
        """
        stmt = select(func.count(func.distinct(Contract.id))).where(
            Contract.dataset_id == dataset_id,
            Contract.status.in_(list(_EFFECTIVE_EVER)),
            Contract.signed_at.is_not(None),
            Contract.signed_at >= start,
            Contract.signed_at <= end,
        ).where(Contract.merchant_id.in_(self._merchant_subquery(dataset_id, scope)))
        return int(self.session.execute(stmt).scalar() or 0)

    def terminated_contract_ids_in(
        self, start_dt: datetime, end_dt: datetime, dataset_id: UUID,
        scope: MerchantScope | None,
    ) -> set[UUID]:
        stmt = select(func.distinct(ContractVersion.contract_id)).where(
            ContractVersion.dataset_id == dataset_id,
            ContractVersion.change_type == "TERMINATE",
            ContractVersion.created_at >= start_dt,
            ContractVersion.created_at < end_dt,
        ).where(ContractVersion.contract_id.in_(
            select(Contract.id).where(
                Contract.dataset_id == dataset_id,
                Contract.merchant_id.in_(self._merchant_subquery(dataset_id, scope)),
            )
        ))
        return set(self.session.execute(stmt).scalars().all())

    # ============================ 指标：巡访 ============================

    def active_merchant_ids(
        self, dataset_id: UUID, scope: MerchantScope | None,
    ) -> set[UUID]:
        stmt = select(Merchant.id).where(
            Merchant.dataset_id == dataset_id, Merchant.status == "ACTIVE"
        )
        if scope is not None and scope.region_ids is not None:
            stmt = stmt.where(Merchant.region_id.in_(scope.region_list or list(_DEAD)))
        if scope is not None and scope.merchant_ids is not None:
            stmt = stmt.where(Merchant.id.in_(scope.merchant_list or list(_DEAD)))
        return set(self.session.execute(stmt).scalars().all())

    def valid_visit_merchant_ids_in(
        self, start_dt: datetime, end_dt: datetime, dataset_id: UUID,
        scope: MerchantScope | None,
    ) -> set[UUID]:
        stmt = select(func.distinct(VisitRecord.merchant_id)).where(
            VisitRecord.dataset_id == dataset_id,
            VisitRecord.status == "RECOGNIZED",
            VisitRecord.source != "SUPERVISION",
            VisitRecord.is_official.is_(True),
            VisitRecord.submitted_at >= start_dt,
            VisitRecord.submitted_at < end_dt,
        ).where(VisitRecord.merchant_id.in_(self._merchant_subquery(dataset_id, scope)))
        return set(self.session.execute(stmt).scalars().all())

    def on_time_visit_counts(
        self, window_start: datetime, window_end: datetime, dataset_id: UUID,
        scope: MerchantScope | None,
    ) -> tuple[int, int]:
        stmt = select(VisitTask).where(
            VisitTask.dataset_id == dataset_id,
            VisitTask.window_start_at.is_not(None),
            VisitTask.window_end_at.is_not(None),
            VisitTask.window_start_at >= window_start,
            VisitTask.window_start_at < window_end,
            VisitTask.merchant_id.in_(self._merchant_subquery(dataset_id, scope)),
        )
        tasks = list(self.session.execute(stmt).scalars().all())
        due = len(tasks)
        if due == 0:
            return 0, 0
        visits = self.session.execute(
            select(VisitRecord.visit_task_id, VisitRecord.checkin_at).where(
                VisitRecord.dataset_id == dataset_id,
                VisitRecord.visit_task_id.in_([t.id for t in tasks]),
                VisitRecord.is_official.is_(True),
                VisitRecord.status == "RECOGNIZED",
            )
        ).all()
        checkin = {r[0]: r[1] for r in visits}
        on_time = 0
        for t in tasks:
            at = checkin.get(t.id)
            if at is not None and t.window_start_at <= at <= t.window_end_at:
                on_time += 1
        return on_time, due

    def display_check_counts(
        self, start_dt: datetime, end_dt: datetime, dataset_id: UUID,
        scope: MerchantScope | None,
    ) -> tuple[int, int]:
        stmt = select(VisitRecord.display_met).where(
            VisitRecord.dataset_id == dataset_id,
            VisitRecord.status == "RECOGNIZED",
            VisitRecord.source != "SUPERVISION",
            VisitRecord.is_official.is_(True),
            VisitRecord.display_pct.is_not(None),
            VisitRecord.submitted_at >= start_dt,
            VisitRecord.submitted_at < end_dt,
        ).where(VisitRecord.merchant_id.in_(self._merchant_subquery(dataset_id, scope)))
        rows = list(self.session.execute(stmt).scalars().all())
        return sum(1 for r in rows if r is True), len(rows)

    # ============================ 指标：销量/补货/损益 ============================

    def confirmed_sales_boxes(
        self, start: date, end: date, dataset_id: UUID, scope: MerchantScope | None,
        *, sku_id: UUID | None = None,
    ) -> Decimal:
        stmt = select(func.coalesce(func.sum(SalesConfirmation.confirmed_boxes), 0)).where(
            SalesConfirmation.dataset_id == dataset_id,
            SalesConfirmation.biz_date >= start,
            SalesConfirmation.biz_date <= end,
            SalesConfirmation.merchant_id.in_(self._merchant_subquery(dataset_id, scope)),
        )
        if sku_id is not None:
            stmt = stmt.where(SalesConfirmation.sku_id == sku_id)
        return Decimal(self.session.execute(stmt).scalar() or 0)

    def confirmed_sales_amount_fen(
        self, start: date, end: date, dataset_id: UUID, scope: MerchantScope | None,
    ) -> int:
        stmt = select(func.coalesce(func.sum(SalesConfirmation.amount_fen), 0)).where(
            SalesConfirmation.dataset_id == dataset_id,
            SalesConfirmation.biz_date >= start,
            SalesConfirmation.biz_date <= end,
            SalesConfirmation.merchant_id.in_(self._merchant_subquery(dataset_id, scope)),
        )
        return int(self.session.execute(stmt).scalar() or 0)

    def replenishment_fulfillment_counts(
        self, start: date, end: date, dataset_id: UUID, scope: MerchantScope | None,
    ) -> tuple[Decimal, Decimal]:
        stmt = select(ReplenishmentOrder).where(
            ReplenishmentOrder.dataset_id == dataset_id,
            ReplenishmentOrder.status.in_(
                ["CONFIRMED", "PARTIALLY_FULFILLED", "FULFILLED", "CLOSED"]
            ),
            ReplenishmentOrder.request_date >= start,
            ReplenishmentOrder.request_date <= end,
            ReplenishmentOrder.merchant_id.in_(self._merchant_subquery(dataset_id, scope)),
        )
        orders = list(self.session.execute(stmt).scalars().all())
        committed = sum((o.committed_boxes for o in orders), Decimal("0"))
        on_time = Decimal("0")
        for o in orders:
            if (
                o.promised_delivery_date is not None
                and o.actual_delivery_date is not None
                and o.actual_delivery_date <= o.promised_delivery_date
                and o.fulfilled_boxes == o.committed_boxes
            ):
                on_time += o.fulfilled_boxes
        return on_time, committed

    def procurement_cost_fen(self, start: date, end: date, dataset_id: UUID) -> int:
        stmt = select(
            func.coalesce(
                func.sum(ProcurementOrder.quantity_cases * ProcurementOrder.unit_price_fen), 0
            )
        ).where(
            ProcurementOrder.dataset_id == dataset_id,
            ProcurementOrder.status.in_(["APPROVED", "ORDERED", "RECEIVED"]),
            ProcurementOrder.expected_date >= start,
            ProcurementOrder.expected_date <= end,
        )
        return int(self.session.execute(stmt).scalar() or 0)

    def purchase_received_cost_fen(
        self, start: date, end: date, dataset_id: UUID
    ) -> int:
        """T18 采购单已收货成本（T33 净利润口径）：按行金额合计，
        状态 RECEIVED 且收货日在统计期内（货到才计成本，权责发生）。"""
        stmt = (
            select(func.coalesce(func.sum(PurchaseOrderLine.line_amount_fen), 0))
            .select_from(PurchaseOrderLine)
            .join(PurchaseOrder, PurchaseOrder.id == PurchaseOrderLine.order_id)
            .where(
                PurchaseOrder.dataset_id == dataset_id,
                PurchaseOrder.status == "RECEIVED",
                PurchaseOrder.received_at >= start,
                PurchaseOrder.received_at <= end,
            )
        )
        return int(self.session.execute(stmt).scalar() or 0)

    def freezer_cost_fen(self, start_dt: datetime, end_dt: datetime,
                         dataset_id: UUID) -> int:
        """冰柜购置/维修成本（FreezerMovement 无 dataset 列，join 资产过滤）。"""
        def _sum(movement_type: str):
            return (
                select(func.coalesce(func.sum(FreezerMovement.amount_fen), 0))
                .select_from(FreezerMovement)
                .join(FreezerAsset, FreezerAsset.id == FreezerMovement.asset_id)
                .where(
                    FreezerAsset.dataset_id == dataset_id,
                    FreezerMovement.movement_type == movement_type,
                    FreezerMovement.occurred_at >= start_dt,
                    FreezerMovement.occurred_at < end_dt,
                )
            )

        return int(
            (self.session.execute(_sum("PURCHASE")).scalar() or 0)
            + (self.session.execute(_sum("REPAIR_END")).scalar() or 0)
        )

    def approved_expense_fen_by_type(
        self, period_month: str, dataset_id: UUID, types: tuple[str, ...],
    ) -> int:
        stmt = select(func.coalesce(func.sum(Expense.amount_fen), 0)).where(
            Expense.dataset_id == dataset_id,
            Expense.period_month == period_month,
            Expense.status == "APPROVED",
            Expense.type.in_(types or _DEAD),
        )
        return int(self.session.execute(stmt).scalar() or 0)

    def paid_performance_fen(self, period_month: str, dataset_id: UUID) -> int:
        stmt = select(func.coalesce(func.sum(PayrollStatement.performance_fen), 0)).where(
            PayrollStatement.dataset_id == dataset_id,
            PayrollStatement.period_month == period_month,
            PayrollStatement.status == "PAID",
        )
        return int(self.session.execute(stmt).scalar() or 0)

    def finance_tx_net_fen(self, start: date, end: date, dataset_id: UUID) -> int:
        stmt = select(func.coalesce(func.sum(FinanceTransaction.amount_fen), 0)).where(
            FinanceTransaction.dataset_id == dataset_id,
            FinanceTransaction.biz_date >= start,
            FinanceTransaction.biz_date <= end,
        )
        return int(self.session.execute(stmt).scalar() or 0)

    # ============================ 风险：事实采集 ============================

    def grade_frequency_map(self, as_of: date) -> dict[str, int]:
        stmt = select(MerchantGradeFrequency).where(
            MerchantGradeFrequency.start_date <= as_of,
            or_(MerchantGradeFrequency.end_date.is_(None),
                MerchantGradeFrequency.end_date >= as_of),
        )
        out: dict[str, int] = {}
        for r in self.session.execute(stmt).scalars().all():
            out.setdefault(r.grade, r.frequency_days)
        return out

    def last_valid_visit_at(
        self, merchant_ids: list[UUID], dataset_id: UUID,
    ) -> dict[UUID, datetime]:
        if not merchant_ids:
            return {}
        stmt = select(
            VisitRecord.merchant_id, func.max(VisitRecord.submitted_at)
        ).where(
            VisitRecord.dataset_id == dataset_id,
            VisitRecord.merchant_id.in_(merchant_ids),
            VisitRecord.status == "RECOGNIZED",
            VisitRecord.source != "SUPERVISION",
        ).group_by(VisitRecord.merchant_id)
        return {r[0]: r[1] for r in self.session.execute(stmt).all()}

    def recent_valid_visits(
        self, merchant_ids: list[UUID], dataset_id: UUID, limit_per_merchant: int,
    ) -> dict[UUID, list[VisitRecord]]:
        if not merchant_ids:
            return {}
        stmt = select(VisitRecord).where(
            VisitRecord.dataset_id == dataset_id,
            VisitRecord.merchant_id.in_(merchant_ids),
            VisitRecord.is_official.is_(True),
        ).order_by(VisitRecord.merchant_id, VisitRecord.submitted_at.desc())
        out: dict[UUID, list[VisitRecord]] = {}
        for v in self.session.execute(stmt).scalars().all():
            bucket = out.setdefault(v.merchant_id, [])
            if len(bucket) < limit_per_merchant:
                bucket.append(v)
        return out

    def confirmed_sales_merchant_ids_in(
        self, start: date, end: date, dataset_id: UUID,
    ) -> set[UUID]:
        stmt = select(func.distinct(SalesConfirmation.merchant_id)).where(
            SalesConfirmation.dataset_id == dataset_id,
            SalesConfirmation.biz_date >= start,
            SalesConfirmation.biz_date <= end,
            SalesConfirmation.confirmed_boxes > 0,
        )
        return set(self.session.execute(stmt).scalars().all())

    def merchants_with_any_sales_confirmation(self, dataset_id: UUID) -> set[UUID]:
        stmt = select(func.distinct(SalesConfirmation.merchant_id)).where(
            SalesConfirmation.dataset_id == dataset_id
        )
        return set(self.session.execute(stmt).scalars().all())

    def overdue_replenishments(self, as_of: date, dataset_id: UUID) -> list[ReplenishmentOrder]:
        stmt = select(ReplenishmentOrder).where(
            ReplenishmentOrder.dataset_id == dataset_id,
            ReplenishmentOrder.status.in_(["CONFIRMED", "PARTIALLY_FULFILLED"]),
            ReplenishmentOrder.promised_delivery_date.is_not(None),
            ReplenishmentOrder.promised_delivery_date < as_of,
            ReplenishmentOrder.fulfilled_boxes < ReplenishmentOrder.committed_boxes,
        )
        return list(self.session.execute(stmt).scalars().all())

    def contracts_expiring(
        self, as_of: date, within_days: int, dataset_id: UUID,
    ) -> list[Contract]:
        limit = date.fromordinal(as_of.toordinal() + within_days)
        stmt = select(Contract).where(
            Contract.dataset_id == dataset_id,
            Contract.status == _CONTRACT_EFFECTIVE,
            Contract.expire_at.is_not(None),
            Contract.expire_at >= as_of,
            Contract.expire_at <= limit,
        )
        return list(self.session.execute(stmt).scalars().all())

    def open_renewal_task_contract_ids(self, contract_ids: list[UUID]) -> set[UUID]:
        if not contract_ids:
            return set()
        from app.models.approval import TaskItem
        from app.models.enums import TaskStatus

        stmt = select(TaskItem.related_object_id).where(
            TaskItem.task_kind == "CONTRACT_EXPIRING_RENEWAL_EVAL",
            TaskItem.related_object_type == "CONTRACT",
            TaskItem.related_object_id.in_(contract_ids),
            TaskItem.status.in_([TaskStatus.OPEN.value, TaskStatus.IN_PROGRESS.value]),
        )
        return set(self.session.execute(stmt).scalars().all())

    def repairing_freezers(self, dataset_id: UUID) -> list[FreezerAsset]:
        stmt = select(FreezerAsset).where(
            FreezerAsset.dataset_id == dataset_id,
            FreezerAsset.status == "REPAIRING",
        )
        return list(self.session.execute(stmt).scalars().all())

    def repair_start_at(self, asset_ids: list[UUID], dataset_id: UUID) -> dict[UUID, datetime]:
        if not asset_ids:
            return {}
        stmt = (
            select(FreezerMovement.asset_id, func.max(FreezerMovement.occurred_at))
            .select_from(FreezerMovement)
            .join(FreezerAsset, FreezerAsset.id == FreezerMovement.asset_id)
            .where(
                FreezerAsset.dataset_id == dataset_id,
                FreezerMovement.asset_id.in_(asset_ids),
                FreezerMovement.movement_type == "REPAIR_START",
            )
            .group_by(FreezerMovement.asset_id)
        )
        return {r[0]: r[1] for r in self.session.execute(stmt).all()}

    # ============================ 报表行（导出） ============================

    def report_rows(
        self, report_key: str, dataset_id: UUID, scope: MerchantScope | None,
        start: date | None, end: date | None,
    ) -> list[dict]:
        mq = self._merchant_subquery(dataset_id, scope)
        if report_key == "MERCHANTS":
            stmt = select(Merchant).where(Merchant.dataset_id == dataset_id, Merchant.id.in_(mq))
            return [
                {"id": str(m.id), "code": m.code, "name": m.name, "status": m.status,
                 "grade": m.grade, "region_id": str(m.region_id), "brand_id": str(m.brand_id)}
                for m in self.session.execute(stmt).scalars().all()
            ]
        if report_key == "CONTRACTS":
            stmt = select(Contract).where(
                Contract.dataset_id == dataset_id, Contract.merchant_id.in_(mq)
            )
            return [
                {"id": str(c.id), "contract_no": c.contract_no, "title": c.title,
                 "status": c.status, "merchant_id": str(c.merchant_id),
                 "effective_from": str(c.effective_from), "expire_at": str(c.expire_at)}
                for c in self.session.execute(stmt).scalars().all()
            ]
        if report_key == "SALES":
            stmt = select(SalesConfirmation).where(
                SalesConfirmation.dataset_id == dataset_id,
                SalesConfirmation.merchant_id.in_(mq),
            )
            if start and end:
                stmt = stmt.where(
                    SalesConfirmation.biz_date >= start, SalesConfirmation.biz_date <= end
                )
            return [
                {"id": str(s.id), "merchant_id": str(s.merchant_id),
                 "sku_id": str(s.sku_id), "biz_date": str(s.biz_date),
                 "confirmed_boxes": str(s.confirmed_boxes), "amount_fen": s.amount_fen}
                for s in self.session.execute(stmt).scalars().all()
            ]
        if report_key == "FREEZERS":
            stmt = select(FreezerAsset).where(FreezerAsset.dataset_id == dataset_id)
            return [
                {"id": str(a.id), "asset_code": a.asset_code, "model": a.model,
                 "status": a.status, "purchase_price_fen": a.purchase_price_fen,
                 "current_merchant_id": str(a.current_merchant_id)}
                for a in self.session.execute(stmt).scalars().all()
            ]
        if report_key == "REPLENISHMENT":
            stmt = select(ReplenishmentOrder).where(
                ReplenishmentOrder.dataset_id == dataset_id,
                ReplenishmentOrder.merchant_id.in_(mq),
            )
            if start and end:
                stmt = stmt.where(
                    ReplenishmentOrder.request_date >= start,
                    ReplenishmentOrder.request_date <= end,
                )
            return [
                {"id": str(r.id), "code": r.code, "merchant_id": str(r.merchant_id),
                 "status": r.status, "committed_boxes": str(r.committed_boxes),
                 "fulfilled_boxes": str(r.fulfilled_boxes),
                 "promised_delivery_date": str(r.promised_delivery_date)}
                for r in self.session.execute(stmt).scalars().all()
            ]
        if report_key == "PROCUREMENT":
            stmt = select(ProcurementOrder).where(ProcurementOrder.dataset_id == dataset_id)
            if start and end:
                stmt = stmt.where(
                    ProcurementOrder.expected_date >= start,
                    ProcurementOrder.expected_date <= end,
                )
            return [
                {"id": str(p.id), "code": p.code, "sku_id": str(p.sku_id),
                 "status": p.status, "quantity_cases": str(p.quantity_cases),
                 "unit_price_fen": p.unit_price_fen, "expected_date": str(p.expected_date)}
                for p in self.session.execute(stmt).scalars().all()
            ]
        if report_key == "EXPENSES":
            stmt = select(Expense).where(Expense.dataset_id == dataset_id)
            if start and end:
                stmt = stmt.where(Expense.incurred_on >= start, Expense.incurred_on <= end)
            return [
                {"id": str(e.id), "expense_no": e.expense_no, "type": e.type,
                 "title": e.title, "amount_fen": e.amount_fen, "status": e.status,
                 "period_month": e.period_month}
                for e in self.session.execute(stmt).scalars().all()
            ]
        if report_key == "PERFORMANCE":
            stmt = select(PayrollStatement).where(
                PayrollStatement.dataset_id == dataset_id
            )
            if start and end:
                stmt = stmt.where(
                    PayrollStatement.period_start >= start,
                    PayrollStatement.period_end <= end,
                )
            return [
                {"id": str(p.id), "statement_no": p.statement_no, "user_id": str(p.user_id),
                 "status": p.status, "base_salary_fen": p.base_salary_fen,
                 "performance_fen": p.performance_fen, "gross_pay_fen": p.gross_pay_fen,
                 "period_month": p.period_month}
                for p in self.session.execute(stmt).scalars().all()
            ]
        if report_key == "VISITS":
            stmt = select(VisitRecord).where(
                VisitRecord.dataset_id == dataset_id, VisitRecord.merchant_id.in_(mq)
            )
            if start and end:
                stmt = stmt.where(
                    VisitRecord.submitted_at >= datetime.combine(start, datetime.min.time())
                )
            return [
                {"id": str(v.id), "merchant_id": str(v.merchant_id),
                 "user_id": str(v.user_id), "status": v.status, "validity": v.validity,
                 "source": v.source, "display_pct": v.display_pct,
                 "submitted_at": v.submitted_at.isoformat() if v.submitted_at else None}
                for v in self.session.execute(stmt).scalars().all()
            ]
        return []
