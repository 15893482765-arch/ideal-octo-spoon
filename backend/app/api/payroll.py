"""/api/payroll 路由：费用与工资发放（T20 / PRD §4.10、§6.5）。

Router 只鉴权/校验/装配；同数据集隔离、审批依据校验、金额合计、双人复核、
已发放更正等业务规则全部在 ExpenseService / PayrollService。
"""

from __future__ import annotations

# ruff: noqa: B008  FastAPI Depends() 需出现在默认值
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.deps import current_actor, db_session
from app.core.permissions import ActorContext
from app.models import User
from app.schemas.payroll import (
    AddAdjustmentRequest,
    ApplyCorrectionRequest,
    ApproveExpenseRequest,
    CreateExpenseRequest,
    CreateSalaryConfigRequest,
    EffectiveSalaryView,
    ExpenseView,
    GenerateStatementRequest,
    MarkPaidRequest,
    PayrollAdjustmentView,
    PayrollLineView,
    RejectExpenseRequest,
    SalaryConfigView,
    StatementDetailView,
    StatementListView,
    StatementView,
)
from app.services.expense_service import ExpenseService
from app.services.payroll_service import PayrollService
from app.services.salary_config_service import SalaryConfigService

router = APIRouter(prefix="/api/payroll", tags=["payroll"])


# ============================== 费用 ==============================

def _expense_view(e) -> ExpenseView:
    return ExpenseView(
        id=e.id, expense_no=e.expense_no, type=e.type, title=e.title,
        amount_fen=e.amount_fen, incurred_on=e.incurred_on,
        period_month=e.period_month, status=e.status,
        voucher_file_id=e.voucher_file_id,
        source_object_type=e.source_object_type,
        source_object_id=e.source_object_id, approval_id=e.approval_id,
        approved_by_id=e.approved_by_id, approved_at=e.approved_at,
        rejected_reason=e.rejected_reason)


@router.post("/expenses", response_model=ExpenseView, status_code=201)
def create_expense(
    body: CreateExpenseRequest,
    session: Session = Depends(db_session),
    actor: ActorContext = Depends(current_actor),
):
    expense = ExpenseService(session).create_expense(
        actor, type=body.type, title=body.title,
        amount_fen=body.amount_fen, incurred_on=body.incurred_on,
        voucher_file_id=body.voucher_file_id,
        source_object_type=body.source_object_type,
        source_object_id=body.source_object_id, remark=body.remark)
    return _expense_view(expense)


@router.post("/expenses/{expense_id}/approve", response_model=ExpenseView)
def approve_expense(
    expense_id: UUID,
    body: ApproveExpenseRequest,
    session: Session = Depends(db_session),
    actor: ActorContext = Depends(current_actor),
):
    expense = ExpenseService(session).approve_expense(
        actor, expense_id, approval_id=body.approval_id, reason=body.reason)
    return _expense_view(expense)


@router.post("/expenses/{expense_id}/reject", response_model=ExpenseView)
def reject_expense(
    expense_id: UUID,
    body: RejectExpenseRequest,
    session: Session = Depends(db_session),
    actor: ActorContext = Depends(current_actor),
):
    expense = ExpenseService(session).reject_expense(
        actor, expense_id, reason=body.reason)
    return _expense_view(expense)


@router.get("/expenses/approved/{period_month}",
            response_model=list[ExpenseView])
def approved_expenses(
    period_month: str,
    session: Session = Depends(db_session),
    actor: ActorContext = Depends(current_actor),
):
    """某月进损益口径费用（仅 APPROVED；未审批/驳回被排除）。

    注意：静态段必须在 ``/expenses/{expense_id}`` 之前注册，否则会被动态段拦截。
    """
    rows = ExpenseService(session).approved_expenses_in_month(
        actor, period_month)
    return [_expense_view(e) for e in rows]


@router.get("/expenses/{expense_id}", response_model=ExpenseView)
def get_expense(
    expense_id: UUID,
    session: Session = Depends(db_session),
    actor: ActorContext = Depends(current_actor),
):
    return _expense_view(ExpenseService(session).get_expense(actor, expense_id))


# ============================== 工资单 ==============================

def _statement_view(s, user_name: str | None = None) -> StatementView:
    return StatementView(
        id=s.id, statement_no=s.statement_no, user_id=s.user_id,
        user_name=user_name,
        period_month=s.period_month, period_start=s.period_start,
        period_end=s.period_end, performance_snapshot_id=s.performance_snapshot_id,
        base_salary_fen=s.base_salary_fen, performance_fen=s.performance_fen,
        adjustment_fen=s.adjustment_fen, gross_pay_fen=s.gross_pay_fen,
        correction_fen=s.correction_fen, corrected_pay_fen=s.corrected_pay_fen,
        status=s.status, confirmed_by_id=s.confirmed_by_id,
        confirmed_at=s.confirmed_at, reviewed_by_id=s.reviewed_by_id,
        reviewed_at=s.reviewed_at, paid_by_id=s.paid_by_id,
        paid_at=s.paid_at, paid_on=s.paid_on,
        corrected_by_id=s.corrected_by_id, corrected_at=s.corrected_at)


def _detail_view(detail: dict) -> StatementDetailView:
    s = detail["statement"]
    base = _statement_view(s, detail.get("user_name")).model_dump()
    base["lines"] = [
        PayrollLineView(
            id=ln.id, line_type=ln.line_type,
            source_object_type=ln.source_object_type,
            source_object_id=ln.source_object_id, amount_fen=ln.amount_fen,
            basis_json=ln.basis_json)
        for ln in detail["lines"]
    ]
    base["adjustments"] = [
        PayrollAdjustmentView(
            id=a.id, statement_id=a.statement_id, kind=a.kind,
            amount_fen=a.amount_fen, approval_id=a.approval_id,
            reason=a.reason, is_correction=a.is_correction,
            created_by_id=a.created_by_id)
        for a in detail["adjustments"]
    ]
    return StatementDetailView(**base)


@router.post("/statements/generate", response_model=StatementDetailView,
             status_code=201)
def generate_statement(
    body: GenerateStatementRequest,
    session: Session = Depends(db_session),
    actor: ActorContext = Depends(current_actor),
):
    svc = PayrollService(session)
    st = svc.generate_statement(
        actor, user_id=body.user_id, period_month=body.period_month,
        base_salary_fen=body.base_salary_fen,
        performance_snapshot_id=body.performance_snapshot_id,
        adjustments=[a.model_dump() for a in body.adjustments])
    return _detail_view(svc.get_detail(actor, st.id))


@router.post("/statements/{statement_id}/adjustments",
             response_model=PayrollAdjustmentView, status_code=201)
def add_adjustment(
    statement_id: UUID,
    body: AddAdjustmentRequest,
    session: Session = Depends(db_session),
    actor: ActorContext = Depends(current_actor),
):
    adj = PayrollService(session).add_adjustment(
        actor, statement_id, kind=body.kind, amount_fen=body.amount_fen,
        approval_id=body.approval_id, reason=body.reason)
    return _adjustment_view(adj)


@router.post("/statements/{statement_id}/confirm",
             response_model=StatementView)
def confirm_statement(
    statement_id: UUID,
    session: Session = Depends(db_session),
    actor: ActorContext = Depends(current_actor),
):
    return _statement_view(
        PayrollService(session).confirm_statement(actor, statement_id))


@router.post("/statements/{statement_id}/review",
             response_model=StatementView)
def review_statement(
    statement_id: UUID,
    session: Session = Depends(db_session),
    actor: ActorContext = Depends(current_actor),
):
    return _statement_view(
        PayrollService(session).review_statement(actor, statement_id))


@router.post("/statements/{statement_id}/pay", response_model=StatementView)
def mark_paid(
    statement_id: UUID,
    body: MarkPaidRequest,
    session: Session = Depends(db_session),
    actor: ActorContext = Depends(current_actor),
):
    return _statement_view(
        PayrollService(session).mark_paid(actor, statement_id,
                                          paid_on=body.paid_on))


@router.post("/statements/{statement_id}/corrections",
             response_model=PayrollAdjustmentView, status_code=201)
def apply_correction(
    statement_id: UUID,
    body: ApplyCorrectionRequest,
    session: Session = Depends(db_session),
    actor: ActorContext = Depends(current_actor),
):
    adj = PayrollService(session).apply_correction(
        actor, statement_id, kind=body.kind, amount_fen=body.amount_fen,
        approval_id=body.approval_id, reason=body.reason)
    return _adjustment_view(adj)


@router.get("/statements", response_model=StatementListView)
def list_statements(
    period_month: str | None = Query(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    status: str | None = Query(default=None, max_length=16),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    session: Session = Depends(db_session),
    actor: ActorContext = Depends(current_actor),
):
    """工资发放列表（财务）：按月份/状态筛选，默认最近 50 条，含员工姓名。"""
    rows, total = PayrollService(session).list_statements(
        actor,
        period_month=period_month,
        status=status,
        page=page,
        page_size=page_size,
    )
    return StatementListView(
        items=[_statement_view(r["statement"], r["user_name"]) for r in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/statements/{statement_id}", response_model=StatementDetailView)
def get_statement(
    statement_id: UUID,
    session: Session = Depends(db_session),
    actor: ActorContext = Depends(current_actor),
):
    return _detail_view(
        PayrollService(session).get_detail(actor, statement_id))


def _adjustment_view(a) -> PayrollAdjustmentView:
    return PayrollAdjustmentView(
        id=a.id, statement_id=a.statement_id, kind=a.kind,
        amount_fen=a.amount_fen, approval_id=a.approval_id, reason=a.reason,
        is_correction=a.is_correction, created_by_id=a.created_by_id)


# ============================== 薪资配置（T35） ==============================

def _salary_config_view(config, user_name: str | None = None) -> SalaryConfigView:
    return SalaryConfigView(
        id=config.id, user_id=config.user_id, user_name=user_name,
        base_salary_fen=config.base_salary_fen,
        effective_from=config.effective_from,
        effective_to=config.effective_to, is_active=config.is_active,
        note=config.note, created_at=config.created_at)


@router.get("/salary-configs", response_model=list[SalaryConfigView])
def list_salary_configs(
    user_id: UUID | None = Query(default=None),
    only_active: bool = Query(default=False),
    limit: int = Query(default=200, ge=1, le=500),
    session: Session = Depends(db_session),
    actor: ActorContext = Depends(current_actor),
):
    """薪资配置历史（按生效日倒序）：新增版本不覆盖历史，便于追溯。"""
    rows = SalaryConfigService(session).list_configs(
        actor, user_id=user_id, only_active=only_active, limit=limit)
    return [_salary_config_view(c, name) for c, name in rows]


@router.get("/salary-configs/effective", response_model=EffectiveSalaryView)
def effective_salary(
    user_id: UUID = Query(...),
    period_month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    session: Session = Depends(db_session),
    actor: ActorContext = Depends(current_actor),
):
    """该员工该月生效月薪（工资草稿自动带出；无配置返回 null 由前端提示）。"""
    config = SalaryConfigService(session).effective_for_month(
        actor, user_id=user_id, period_month=period_month)
    return EffectiveSalaryView(
        user_id=user_id, period_month=period_month,
        base_salary_fen=(config.base_salary_fen if config else None),
        config_id=(config.id if config else None),
        effective_from=(config.effective_from if config else None))


@router.post("/salary-configs", response_model=SalaryConfigView, status_code=201)
def create_salary_config(
    body: CreateSalaryConfigRequest,
    session: Session = Depends(db_session),
    actor: ActorContext = Depends(current_actor),
):
    config = SalaryConfigService(session).create_config(
        actor, user_id=body.user_id, base_salary_fen=body.base_salary_fen,
        effective_from=body.effective_from, effective_to=body.effective_to,
        note=body.note)
    user = session.get(User, config.user_id)
    return _salary_config_view(
        config, user.display_name if user is not None else None)


@router.post("/salary-configs/{config_id}/toggle-active",
             response_model=SalaryConfigView)
def toggle_salary_config(
    config_id: UUID,
    session: Session = Depends(db_session),
    actor: ActorContext = Depends(current_actor),
):
    """停用/启用配置：停用后不再参与解析，历史工资单已固化的底薪不变。"""
    config = SalaryConfigService(session).toggle_active(actor, config_id)
    user = session.get(User, config.user_id)
    return _salary_config_view(
        config, user.display_name if user is not None else None)
