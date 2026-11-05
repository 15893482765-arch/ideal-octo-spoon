"""T14 商家调拨路由：只做鉴权、参数校验与调用 Service（PRD §4.7 / R9 / A8）。

Router 不写业务规则、不手动 commit（事务边界由 deps.db_session 的 session_scope 管理）。
行级范围由 current_actor 注入，Service 再校验片区。
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.deps import db_session, require_ability
from app.core.permissions import MODULE_MERCHANT, ActorContext
from app.schemas.transfer import (
    TransferCreate,
    TransferOut,
    TransferReverse,
    TransferWithdraw,
)
from app.services.transfer_service import TransferService

router = APIRouter(prefix="/api/v1/merchants/transfers", tags=["merchant-transfers"])

ActorDep = Annotated[ActorContext, Depends(require_ability(MODULE_MERCHANT))]
DbDep = Annotated[Session, Depends(db_session)]


@router.get("/options", response_model=dict[str, Any])
def transfer_options(
    merchant_id: UUID,
    session: DbDep,
    actor: ActorDep,
) -> dict[str, Any]:
    """调拨表单选项：当前主负责人 + 本片区可承接的在职业务员。纯读。"""
    return TransferService(session).options(actor, merchant_id=merchant_id)


@router.post("/preview", response_model=dict[str, Any])
def preview_transfer(
    payload: TransferCreate,
    session: DbDep,
    actor: ActorDep,
) -> dict[str, Any]:
    """调拨预览：原/新负责人、生效日、未完成任务、待办、临时授权、合同与绩效分界。纯读。"""
    return TransferService(session).preview(
        actor,
        merchant_id=payload.merchant_id,
        from_user_id=payload.from_user_id,
        to_user_id=payload.to_user_id,
        effective_date=payload.effective_date,
    )


@router.post("", response_model=TransferOut, status_code=201)
def create_transfer(payload: TransferCreate, session: DbDep, actor: ActorDep):
    return TransferService(session).create_transfer(
        actor,
        merchant_id=payload.merchant_id,
        from_user_id=payload.from_user_id,
        to_user_id=payload.to_user_id,
        effective_date=payload.effective_date,
        reason=payload.reason,
    )


@router.post("/{transfer_id}/apply", response_model=TransferOut)
def apply_transfer(transfer_id: UUID, session: DbDep, actor: ActorDep):
    return TransferService(session).apply(actor, transfer_id=transfer_id)


@router.post("/{transfer_id}/withdraw", response_model=TransferOut)
def withdraw_transfer(
    transfer_id: UUID, payload: TransferWithdraw, session: DbDep, actor: ActorDep
):
    return TransferService(session).withdraw(
        actor, transfer_id=transfer_id, reason=payload.reason)


@router.post("/reverse", response_model=TransferOut, status_code=201)
def create_reverse(payload: TransferReverse, session: DbDep, actor: ActorDep):
    return TransferService(session).create_reverse(
        actor,
        original_id=payload.original_id,
        effective_date=payload.effective_date,
        reason=payload.reason,
    )
