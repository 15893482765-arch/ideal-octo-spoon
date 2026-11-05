"""/api/contracts 路由：合同申请、审批与版本（PRD §4.4 / R38）。

Router 只做鉴权、参数校验与调用 Service；状态机、固化口径、自审拦截、版本快照、
续签待办幂等等业务规则全部在 ContractService / RenewalService。
附件上传走 FileService（白名单 + 哈希 + 不可覆盖）。
"""

from __future__ import annotations

# FastAPI 的 Query()/Depends()/File() 必须出现在参数默认值中，统一豁免 B008。
# ruff: noqa: B008
from uuid import UUID

from fastapi import APIRouter, Depends, File, Query, UploadFile
from sqlalchemy.orm import Session

from app.core.deps import current_actor, db_session, require_ability
from app.core.permissions import MODULE_CONTRACT, ActorContext
from app.schemas.contract import (
    ContractChangeRequest,
    ContractDecisionRequest,
    ContractDraftRequest,
    ContractRenewalRequest,
    ContractSubmitRequest,
    ContractTerminateRequest,
    ContractVersionView,
    ContractView,
    RenewalRunView,
)
from app.services.contract_service import ContractService
from app.services.file_service import BIZ_CONTRACT_ATTACHMENT, FileService
from app.services.renewal_service import RenewalService
from app.services.storage import get_storage_backend

router = APIRouter(
    prefix="/api/contracts",
    tags=["contracts"],
    dependencies=[Depends(require_ability(MODULE_CONTRACT))],
)


@router.get("", response_model=list[ContractView])
def list_contracts(
    status: str | None = Query(default=None),
    keyword: str | None = Query(default=None),
    actor: ActorContext = Depends(current_actor),
    session: Session = Depends(db_session),
) -> list[ContractView]:
    return ContractService(session).list_contracts(
        actor, status=status, keyword=keyword
    )


@router.post("", response_model=ContractView, status_code=201)
def create_draft(
    body: ContractDraftRequest,
    actor: ActorContext = Depends(current_actor),
    session: Session = Depends(db_session),
) -> ContractView:
    service = ContractService(session)
    contract = service.create_draft(actor, body)
    session.commit()
    return service._to_view(contract)


@router.get("/{contract_id}", response_model=dict)
def get_contract(
    contract_id: UUID,
    actor: ActorContext = Depends(current_actor),
    session: Session = Depends(db_session),
) -> dict:
    service = ContractService(session)
    detail, versions, renewals = service.get_detail(actor, contract_id)
    return {"contract": detail, "versions": versions, "renewals": renewals}


@router.put("/{contract_id}", response_model=ContractView)
def update_draft(
    contract_id: UUID,
    body: ContractDraftRequest,
    actor: ActorContext = Depends(current_actor),
    session: Session = Depends(db_session),
) -> ContractView:
    service = ContractService(session)
    contract = service.update_draft(actor, contract_id, body)
    session.commit()
    return service._to_view(contract)


@router.post("/{contract_id}/submit", response_model=ContractView)
def submit_contract(
    contract_id: UUID,
    body: ContractSubmitRequest,
    actor: ActorContext = Depends(current_actor),
    session: Session = Depends(db_session),
) -> ContractView:
    service = ContractService(session)
    contract = service.submit(actor, contract_id, body.reason)
    session.commit()
    return service._to_view(contract)


@router.post("/{contract_id}/decision", response_model=ContractView)
def decide_contract(
    contract_id: UUID,
    body: ContractDecisionRequest,
    actor: ActorContext = Depends(current_actor),
    session: Session = Depends(db_session),
) -> ContractView:
    service = ContractService(session)
    contract = service.decide(actor, contract_id, body)
    session.commit()
    return service._to_view(contract)


@router.post("/{contract_id}/change", response_model=ContractView)
def change_contract(
    contract_id: UUID,
    body: ContractChangeRequest,
    actor: ActorContext = Depends(current_actor),
    session: Session = Depends(db_session),
) -> ContractView:
    service = ContractService(session)
    contract = service.change(actor, contract_id, body)
    session.commit()
    return service._to_view(contract)


@router.post("/{contract_id}/renewals", response_model=ContractView, status_code=201)
def create_renewal(
    contract_id: UUID,
    body: ContractRenewalRequest,
    actor: ActorContext = Depends(current_actor),
    session: Session = Depends(db_session),
) -> ContractView:
    """续约申请：基于生效合同追加一份接续合同（草稿），审批通过后按新生效日生效。

    旧合同不终止、不改状态（到期自然失效），因此不需要"先终止再重签"。
    """
    service = ContractService(session)
    contract = service.create_renewal(actor, contract_id, body)
    session.commit()
    return service._to_view(contract)


@router.post("/{contract_id}/termination-request", response_model=ContractView)
def request_termination(
    contract_id: UUID,
    body: ContractTerminateRequest,
    actor: ActorContext = Depends(current_actor),
    session: Session = Depends(db_session),
) -> ContractView:
    service = ContractService(session)
    contract = service.request_termination(actor, contract_id, body)
    session.commit()
    return service._to_view(contract)


@router.post("/{contract_id}/termination-approve", response_model=ContractView)
def approve_termination(
    contract_id: UUID,
    body: ContractSubmitRequest,
    actor: ActorContext = Depends(current_actor),
    session: Session = Depends(db_session),
) -> ContractView:
    service = ContractService(session)
    contract = service.approve_termination(actor, contract_id, body.reason)
    session.commit()
    return service._to_view(contract)


@router.get("/{contract_id}/versions", response_model=list[ContractVersionView])
def list_versions(
    contract_id: UUID,
    actor: ActorContext = Depends(current_actor),
    session: Session = Depends(db_session),
) -> list[ContractVersionView]:
    _, versions, _ = ContractService(session).get_detail(actor, contract_id)
    return versions


@router.post("/attachments", status_code=201)
async def upload_attachment(
    file: UploadFile = File(...),
    actor: ActorContext = Depends(current_actor),
    session: Session = Depends(db_session),
) -> dict:
    """上传合同附件：白名单/大小/SHA-256/不可覆盖，全部由 FileService 保证。"""
    data = await file.read()
    service = FileService(session, get_storage_backend())
    obj = service.save_upload(
        owner_user_id=actor.user_id,
        filename=file.filename or "attachment",
        content_type=file.content_type or "application/octet-stream",
        data=data,
        biz_type=BIZ_CONTRACT_ATTACHMENT,
    )
    session.commit()
    return {
        "attachment_id": obj.id,
        "filename": obj.original_filename,
        "content_type": obj.content_type,
        "size_bytes": obj.size_bytes,
        "scan_status": obj.scan_status,
    }


@router.post("/renewal-scan", response_model=RenewalRunView)
def run_renewal_scan(
    actor: ActorContext = Depends(current_actor),
    session: Session = Depends(db_session),
) -> RenewalRunView:
    """手动触发到期续签扫描（定时任务同入口）；结果幂等，重复触发不重复生成。"""
    result = RenewalService(session).run_renewal_scan()
    session.commit()
    return RenewalRunView(**result)
