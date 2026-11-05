"""ExpenseService（T20 / PRD §4.10：费用录入、凭证、审批、归属月份）。

红线：
- 录入即必须挂凭证（T04 FileObject，biz_type=PROOF_DOC），归属月份由发生日在
  Asia/Shanghai 的年月派生；
- 费用与凭证只接受同一 dataset_type+dataset_id，跨域整单拒绝；
- 未审批（PENDING_APPROVAL/REJECTED）不进损益，仅 APPROVED 计入
  （``approved_expenses_in_month``）；
- 引用审批单时必须 APPROVED、action='expense.approve'、同数据集、金额一致、
  目标费用一致，且审批人≠发起人；
- 不接发票（附录 A 首版不做）。
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import ConflictError, DomainError, PermissionDeniedError
from app.core.permissions import (
    MODULE_FINANCE,
    ActorContext,
    PermissionService,
)
from app.models import User
from app.models.enums import ExpenseStatus, ExpenseType
from app.models.payroll import Expense
from app.repositories.payroll_repository import PayrollRepository
from app.services.audit_service import EVENT_CREATE, AuditService
from app.services.business_transition_service import BusinessTransitionService
from app.services.demo_data_guard import DataScope, assert_production_scope
from app.services.state_machine import DOMAIN_EXPENSE

_EXPENSE_TYPES = frozenset(t.value for t in ExpenseType)
_APPROVE_ACTION = "expense.approve"
# 公司物品采购（财务总账页录入）：费用来源标记，决定支出流水来源为 COMPANY_PURCHASE
EXPENSE_CHANNEL_COMPANY_PURCHASE = "COMPANY_PURCHASE"
_COMPANY_PURCHASE_CATEGORIES = frozenset(
    {
        ExpenseType.PROCUREMENT.value,
        ExpenseType.FREEZER_PURCHASE.value,
        ExpenseType.OTHER.value,
    }
)


def _ledger_source_kind(expense: Expense) -> str:
    """费用 → 流水来源：公司物品采购优先，其次按费用类型映射。"""
    from app.models.finance import (
        SOURCE_EXPENSE_ASSET,
        SOURCE_EXPENSE_COMPANY,
        SOURCE_EXPENSE_OPEX,
        SOURCE_EXPENSE_PROCUREMENT,
    )

    if expense.type == ExpenseType.FREEZER_PURCHASE.value:
        return SOURCE_EXPENSE_ASSET
    if expense.source_object_type == EXPENSE_CHANNEL_COMPANY_PURCHASE:
        return SOURCE_EXPENSE_COMPANY
    if expense.type == ExpenseType.PROCUREMENT.value:
        return SOURCE_EXPENSE_PROCUREMENT
    return SOURCE_EXPENSE_OPEX


class ExpenseService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.repo = PayrollRepository(session)
        self.permissions = PermissionService()
        self.transition = BusinessTransitionService(session)
        self.audit = AuditService(session)

    # ============================ 录入 ============================

    def create_expense(
        self,
        actor: ActorContext,
        *,
        type: str,                          # noqa: A002 - 与领域术语一致
        title: str,
        amount_fen: int,
        incurred_on: date,
        voucher_file_id: UUID,
        source_object_type: str | None = None,
        source_object_id: UUID | None = None,
        remark: str | None = None,
    ) -> Expense:
        """录入一笔费用（PENDING_APPROVAL），必须带凭证。"""
        if type not in _EXPENSE_TYPES:
            raise DomainError(code="EXPENSE_TYPE_INVALID",
                              message="非法费用类型")
        if amount_fen is None or amount_fen <= 0:
            raise DomainError(code="EXPENSE_AMOUNT_INVALID",
                              message="费用金额必须为正整数分")
        if not title or not title.strip():
            raise DomainError(code="EXPENSE_TITLE_REQUIRED",
                              message="费用用途/标题必填")
        if (source_object_id is None) != (source_object_type is None):
            raise DomainError(
                code="EXPENSE_SOURCE_INCOMPLETE",
                message="来源对象类型与 id 必须同时提供或同时为空")

        ds_type, ds_id = self._dataset(actor)
        voucher = self.repo.get_proof_file(voucher_file_id)
        if voucher is None or voucher.biz_type != "PROOF_DOC":
            raise DomainError(code="EXPENSE_VOUCHER_INVALID",
                              message="费用必须挂凭证文件（PROOF_DOC）")
        if (voucher.dataset_type, voucher.dataset_id) != (ds_type, ds_id):
            raise DomainError(
                code="EXPENSE_DATASET_MISMATCH",
                message="费用与凭证不属于同一数据域；整单拒绝")

        period_month = incurred_on.strftime("%Y-%m")
        expense = Expense(
            expense_no=f"EXP-{uuid4().hex[:14].upper()}",
            type=type, title=title.strip(), amount_fen=amount_fen,
            incurred_on=incurred_on, period_month=period_month,
            status=ExpenseStatus.PENDING_APPROVAL.value,
            voucher_file_id=voucher_file_id,
            source_object_type=source_object_type,
            source_object_id=source_object_id,
            remark=remark,
            dataset_type=ds_type, dataset_id=ds_id,
        )
        self.session.add(expense)
        self.session.flush()
        self.audit.record(object_type="EXPENSE", object_id=expense.id,
                          event_type=EVENT_CREATE, actor_id=actor.user_id,
                          after={"expense_no": expense.expense_no,
                                 "type": type, "amount_fen": amount_fen,
                                 "period_month": period_month})
        return expense

    # ==================== 公司物品采购（T35 财务总账页）====================

    def create_company_purchase(
        self,
        actor: ActorContext,
        *,
        title: str,
        category: str,
        amount_fen: int,
        incurred_on: date,
        voucher_file_id: UUID,
        remark: str | None = None,
    ) -> Expense:
        """录入公司物品采购（待审批）；审批通过后自动进支出流水。

        `category` 复用费用类型：PROCUREMENT（物品采购）/ FREEZER_PURCHASE
        （冰柜购置）/ OTHER（其他支出）。来源标记为 COMPANY_PURCHASE 便于总账
        与流水按“公司物品采购”筛选。
        """
        self._require_finance(actor)
        if category not in _COMPANY_PURCHASE_CATEGORIES:
            raise DomainError(
                code="COMPANY_PURCHASE_CATEGORY_INVALID",
                message="公司物品采购类别只能为采购物品/冰柜购置/其他支出",
            )
        expense = self.create_expense(
            actor,
            type=category,
            title=title,
            amount_fen=amount_fen,
            incurred_on=incurred_on,
            voucher_file_id=voucher_file_id,
            remark=remark,
        )
        # 来源单据即本条采购记录自身（便于按来源筛选与回溯）
        expense.source_object_type = EXPENSE_CHANNEL_COMPANY_PURCHASE
        expense.source_object_id = expense.id
        self.session.flush()
        return expense

    def list_company_purchases(
        self, actor: ActorContext, *, status: str | None = None, limit: int = 100,
    ) -> list[Expense]:
        """公司物品采购列表（同数据集、时间倒序）。"""
        self._require_finance(actor)
        ds_type, ds_id = self._dataset(actor)
        stmt = select(Expense).where(
            Expense.dataset_type == ds_type,
            Expense.dataset_id == ds_id,
            Expense.source_object_type == EXPENSE_CHANNEL_COMPANY_PURCHASE,
        )
        if status:
            stmt = stmt.where(Expense.status == status)
        stmt = stmt.order_by(
            Expense.incurred_on.desc(), Expense.created_at.desc()
        ).limit(max(1, min(int(limit), 200)))
        return list(self.session.execute(stmt).scalars().all())

    # ============================ 审批 ============================

    def approve_expense(
        self, actor: ActorContext, expense_id: UUID,
        *, approval_id: UUID | None = None, reason: str | None = None,
    ) -> Expense:
        """财务审批通过；引用审批单时必须匹配 APPROVED 依据。"""
        self._require_finance(actor)
        ds_type, ds_id = self._dataset(actor)
        expense = self._get(expense_id)
        self._assert_same_dataset(expense, ds_type, ds_id)
        self._require_status(expense, (ExpenseStatus.PENDING_APPROVAL.value,),
                             "审批")
        if approval_id is not None:
            self._assert_valid_approval(approval_id, expense, actor)

        # 先落审批人/时间，再迁状态：transition 会 flush 出 status=APPROVED，
        # 若此时审批人/时间仍为空会触发 expense_approved_requires_approver。
        expense.approval_id = approval_id
        expense.approved_by_id = actor.user_id
        expense.approved_at = datetime.now(UTC)
        self.transition.transition(
            entity=expense, domain=DOMAIN_EXPENSE,
            to_state=ExpenseStatus.APPROVED.value, actor_id=actor.user_id,
            reason=reason or "财务审批通过费用",
        )
        self.session.flush()

        # T35：审批通过即进支出流水（按费用类型映射来源；幂等键=费用单）
        from app.services.finance_ledger_service import FinanceLedgerService

        FinanceLedgerService(self.session).record_expense(
            actor,
            biz_date=expense.incurred_on,
            amount_fen=int(expense.amount_fen),
            source_kind=_ledger_source_kind(expense),
            related_type="EXPENSE",
            related_id=expense.id,
            note=f"费用单 {expense.expense_no} 审批通过：{expense.title}",
            dataset_type=expense.dataset_type,
            dataset_id=expense.dataset_id,
        )
        return expense

    def reject_expense(
        self, actor: ActorContext, expense_id: UUID, *, reason: str,
    ) -> Expense:
        self._require_finance(actor)
        reason = (reason or "").strip()
        if not reason:
            raise DomainError(code="EXPENSE_REJECT_REASON_REQUIRED",
                              message="驳回费用必须填写原因")
        ds_type, ds_id = self._dataset(actor)
        expense = self._get(expense_id)
        self._assert_same_dataset(expense, ds_type, ds_id)
        self._require_status(expense, (ExpenseStatus.PENDING_APPROVAL.value,),
                             "驳回")
        self.transition.transition(
            entity=expense, domain=DOMAIN_EXPENSE,
            to_state=ExpenseStatus.REJECTED.value, actor_id=actor.user_id,
            reason=f"财务驳回费用：{reason}")
        expense.rejected_reason = reason
        self.session.flush()
        return expense

    # ============================ 查询 / 损益口径 ============================

    def get_expense(self, actor: ActorContext, expense_id: UUID) -> Expense:
        ds_type, ds_id = self._dataset(actor)
        expense = self._get(expense_id)
        self._assert_same_dataset(expense, ds_type, ds_id)
        return expense

    def approved_expenses_in_month(
        self, actor: ActorContext, period_month: str,
    ) -> list[Expense]:
        """某月进损益口径的费用：**仅 APPROVED**，未审批/驳回自动排除（Q114）。"""
        ds_type, ds_id = self._dataset(actor)
        # 费用/损益属正式口径：强制 PRODUCTION，DEMO 不进损益
        assert_production_scope(scope=DataScope(ds_type),
                                where="expense.pnl")
        return self.repo.approved_expenses_in_month(
            period_month, ds_type, ds_id)

    def approved_expense_total_fen(
        self, actor: ActorContext, period_month: str,
    ) -> int:
        return sum(e.amount_fen
                   for e in self.approved_expenses_in_month(actor, period_month))

    # ============================ 内部 ============================

    def _assert_valid_approval(
        self, approval_id: UUID, expense: Expense, actor: ActorContext,
    ) -> None:
        ap = self.repo.approved_approval(approval_id)
        if ap is None:
            raise DomainError(
                code="EXPENSE_APPROVAL_INVALID",
                message="费用审批依据不存在或未通过",
                details={"approval_id": str(approval_id)})
        if ap.action != _APPROVE_ACTION:
            raise DomainError(
                code="EXPENSE_APPROVAL_ACTION_MISMATCH",
                message="审批动作与费用审批不匹配")
        if ap.dataset_id != expense.dataset_id or \
                ap.dataset_type != expense.dataset_type:
            raise DomainError(
                code="EXPENSE_DATASET_MISMATCH",
                message="费用审批依据与费用不属于同一数据域；整单拒绝")
        payload = ap.request_payload or {}
        if payload.get("expense_id") is not None \
                and UUID(str(payload["expense_id"])) != expense.id:
            raise DomainError(
                code="EXPENSE_APPROVAL_TARGET_MISMATCH",
                message="审批单据不是针对该费用发起的")
        if payload.get("amount_fen") is not None \
                and int(payload["amount_fen"]) != expense.amount_fen:
            raise DomainError(
                code="EXPENSE_APPROVAL_AMOUNT_MISMATCH",
                message="费用金额与审批依据不一致")
        if ap.requested_by_id == actor.user_id:
            raise PermissionDeniedError(
                code="EXPENSE_SELF_APPROVAL_FORBIDDEN",
                message="不可审批本人发起的费用")

    @staticmethod
    def _require_status(expense: Expense, allowed: tuple[str, ...],
                        action: str) -> None:
        if expense.status not in allowed:
            raise ConflictError(
                code="EXPENSE_STATUS_CONFLICT",
                message=f"费用当前状态 {expense.status} 不允许{action}",
                details={"expense_id": str(expense.id),
                         "status": expense.status})

    def _get(self, expense_id: UUID) -> Expense:
        expense = self.repo.get_expense(expense_id)
        if expense is None:
            raise DomainError(code="EXPENSE_NOT_FOUND",
                              message="费用单不存在")
        return expense

    def _require_finance(self, actor: ActorContext) -> None:
        if not self.permissions.has_ability(actor, MODULE_FINANCE):
            raise PermissionDeniedError(
                code="EXPENSE_FINANCE_ONLY",
                message="仅财务可审批费用",
                details={"actor_id": str(actor.user_id)})

    @staticmethod
    def _assert_same_dataset(expense: Expense, ds_type: str,
                             ds_id: UUID) -> None:
        if expense.dataset_type != ds_type or expense.dataset_id != ds_id:
            raise PermissionDeniedError(
                code="EXPENSE_DATASET_FORBIDDEN",
                message="无权访问其他数据域的费用单")

    def _dataset(self, actor: ActorContext) -> tuple[str, UUID]:
        user = self.session.get(User, actor.user_id)
        if user is None:
            raise DomainError(code="USER_NOT_FOUND", message="登录用户不存在")
        return "PRODUCTION", user.dataset_id
