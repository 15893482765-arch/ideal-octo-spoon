"""/api/finance 路由：财务总账与公司物品采购（T35）。

Router 只鉴权/校验/装配：
- 汇总、流水明细、公司物品采购的录入与审批规则全部在
  FinanceLedgerService / ExpenseService；
- 金额一律整数分；业务日按 Asia/Shanghai 解释；
- 只记账，不提供任何支付/退款接口（线下交易，系统留痕）。
"""

from __future__ import annotations

# FastAPI 的 Depends()/File()/Query() 必须出现在参数默认值中，统一豁免 B008。
# ruff: noqa: B008
from datetime import UTC, date, datetime
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, File, Query, UploadFile, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.deps import current_actor, db_session, require_ability
from app.core.errors import DomainError
from app.core.permissions import MODULE_FINANCE, ActorContext
from app.models import User
from app.models.payroll import Expense
from app.schemas.finance import (
    CompanyPurchaseDecisionRequest,
    CompanyPurchaseView,
    CreateCompanyPurchaseRequest,
    FinanceTransactionView,
    LedgerBucketView,
    LedgerSummaryView,
)
from app.services.expense_service import ExpenseService
from app.services.file_service import BIZ_PROOF_DOC, FileService
from app.services.finance_ledger_service import (
    SOURCE_KIND_LABELS,
    FinanceLedgerService,
)
from app.services.storage import get_storage_backend

router = APIRouter(
    prefix="/api/finance",
    tags=["finance"],
    dependencies=[Depends(require_ability(MODULE_FINANCE))],
)

_TZ = ZoneInfo("Asia/Shanghai")


def _today() -> date:
    return datetime.now(UTC).astimezone(_TZ).date()


def _purchase_view(e: Expense) -> CompanyPurchaseView:
    return CompanyPurchaseView(
        id=e.id,
        expense_no=e.expense_no,
        title=e.title,
        category=e.type,
        amount_fen=e.amount_fen,
        incurred_on=e.incurred_on,
        status=e.status,
        voucher_file_id=e.voucher_file_id,
        remark=e.remark,
        approved_at=e.approved_at,
        rejected_reason=e.rejected_reason,
        created_at=e.created_at,
    )


# ============================== 总账 ==============================

@router.get("/ledger/summary", response_model=LedgerSummaryView)
def get_ledger_summary(
    granularity: Literal["month", "year"] = Query(default="month"),
    year: int | None = Query(default=None, ge=2000, le=2100),
    month: int | None = Query(default=None, ge=1, le=12),
    actor: ActorContext = Depends(current_actor),
    session: Session = Depends(db_session),
) -> LedgerSummaryView:
    """收入/支出/净利润汇总 + 分桶明细（按月 12 桶，按年近 5 年）。"""
    today = _today()
    summary = FinanceLedgerService(session).summary(
        actor,
        granularity=granularity,
        year=year or today.year,
        month=month if month is not None else today.month,
    )
    return LedgerSummaryView(
        granularity=summary.granularity,
        year=summary.year,
        month=summary.month,
        income_fen=summary.income_fen,
        expense_fen=summary.expense_fen,
        net_fen=summary.net_fen,
        buckets=[
            LedgerBucketView(
                period=b.period,
                income_fen=b.income_fen,
                expense_fen=b.expense_fen,
                net_fen=b.net_fen,
            )
            for b in summary.buckets
        ],
    )


@router.get("/ledger/transactions", response_model=list[FinanceTransactionView])
def list_ledger_transactions(
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    tx_type: Literal["INCOME", "EXPENSE", "ADJUSTMENT"] | None = Query(default=None),
    source_kind: str | None = Query(default=None, max_length=32),
    limit: int = Query(default=200, ge=1, le=500),
    actor: ActorContext = Depends(current_actor),
    session: Session = Depends(db_session),
) -> list[FinanceTransactionView]:
    """流水明细：按日期倒序，支持类型/来源/区间筛选。"""
    rows = FinanceLedgerService(session).list_transactions(
        actor,
        date_from=date_from,
        date_to=date_to,
        tx_type=tx_type,
        source_kind=source_kind,
        limit=limit,
    )
    names: dict[UUID, str] = {}
    if rows:
        names = dict(
            session.execute(
                select(User.id, User.display_name).where(
                    User.id.in_({r.bookkeeper_id for r in rows})
                )
            ).all()
        )
    return [
        FinanceTransactionView(
            id=r.id,
            biz_date=r.biz_date,
            tx_type=r.tx_type,
            source_kind=r.source_kind,
            source_label=SOURCE_KIND_LABELS.get(r.source_kind, r.source_kind),
            amount_fen=r.amount_fen,
            related_object_type=r.related_object_type,
            related_object_id=r.related_object_id,
            note=r.note,
            bookkeeper_id=r.bookkeeper_id,
            bookkeeper_name=names.get(r.bookkeeper_id),
            created_at=r.created_at,
        )
        for r in rows
    ]


# ======================== 公司物品采购 ========================

@router.post(
    "/company-purchases",
    response_model=CompanyPurchaseView,
    status_code=status.HTTP_201_CREATED,
)
def create_company_purchase(
    body: CreateCompanyPurchaseRequest,
    actor: ActorContext = Depends(current_actor),
    session: Session = Depends(db_session),
) -> CompanyPurchaseView:
    """录入公司物品采购（待审批），审批通过后自动计入支出流水。"""
    expense = ExpenseService(session).create_company_purchase(
        actor,
        title=body.title,
        category=body.category,
        amount_fen=body.amount_fen,
        incurred_on=body.incurred_on,
        voucher_file_id=body.voucher_file_id,
        remark=body.remark,
    )
    session.commit()
    return _purchase_view(expense)


@router.get("/company-purchases", response_model=list[CompanyPurchaseView])
def list_company_purchases(
    status_filter: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=100, ge=1, le=200),
    actor: ActorContext = Depends(current_actor),
    session: Session = Depends(db_session),
) -> list[CompanyPurchaseView]:
    rows = ExpenseService(session).list_company_purchases(
        actor, status=status_filter, limit=limit
    )
    return [_purchase_view(e) for e in rows]


@router.post(
    "/company-purchases/{expense_id}/approve", response_model=CompanyPurchaseView
)
def approve_company_purchase(
    expense_id: UUID,
    body: CompanyPurchaseDecisionRequest,
    actor: ActorContext = Depends(current_actor),
    session: Session = Depends(db_session),
) -> CompanyPurchaseView:
    """审批通过：状态迁移 + 支出流水（同事务，失败整体回滚）。"""
    expense = ExpenseService(session).approve_expense(
        actor, expense_id, approval_id=body.approval_id, reason=body.reason
    )
    session.commit()
    return _purchase_view(expense)


@router.post(
    "/company-purchases/{expense_id}/reject", response_model=CompanyPurchaseView
)
def reject_company_purchase(
    expense_id: UUID,
    body: CompanyPurchaseDecisionRequest,
    actor: ActorContext = Depends(current_actor),
    session: Session = Depends(db_session),
) -> CompanyPurchaseView:
    reason = (body.reason or "").strip()
    if not reason:
        raise DomainError(
            code="COMPANY_PURCHASE_REJECT_REASON_REQUIRED",
            message="驳回公司物品采购必须填写原因",
        )
    expense = ExpenseService(session).reject_expense(
        actor, expense_id, reason=reason
    )
    session.commit()
    return _purchase_view(expense)


@router.post("/vouchers", status_code=status.HTTP_201_CREATED)
async def upload_voucher(
    file: UploadFile = File(...),
    actor: ActorContext = Depends(current_actor),
    session: Session = Depends(db_session),
) -> dict:
    """上传采购凭证：白名单/大小/SHA-256 由 FileService 保证，返回 file_id。"""
    owner = session.get(User, actor.user_id)
    if owner is None:
        raise DomainError(code="FINANCE_VOUCHER_OWNER_INVALID", message="上传人不存在")
    data = await file.read()
    service = FileService(session, get_storage_backend())
    obj = service.save_upload(
        owner_user_id=actor.user_id,
        filename=file.filename or "voucher.pdf",
        content_type=file.content_type or "application/octet-stream",
        data=data,
        biz_type=BIZ_PROOF_DOC,
        dataset_type=owner.dataset_type,
        dataset_id=owner.dataset_id,
    )
    if not get_settings().file_scan_enabled:
        service.mark_scan_status(file_id=obj.id, status="SKIPPED")
    session.commit()
    return FileService.describe(obj)
