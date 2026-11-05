"""/api/freezers 路由：冰柜投放、撤回、验收与维修（PRD §4.5 / R11 / R12 / R13 / R37）。

Router 只做鉴权、参数校验与调用 Service；状态机、行锁唯一绑定、库存口径、
押金收退/不入损益、撤柜联动等业务规则全部在 FreezerService。现场/凭证照片走
FileService（由前端先上传换 attachment_id，再随单据提交）。
"""

from __future__ import annotations

# FastAPI 的 Depends() 必须出现在参数默认值中，统一豁免 B008。
# ruff: noqa: B008
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.core.deps import current_actor, db_session, require_ability
from app.core.permissions import MODULE_FREEZER, ActorContext
from app.schemas.freezer import (
    FreezerAcceptanceRequest,
    FreezerDeployConfirmRequest,
    FreezerDeployRequest,
    FreezerDepositRefundRequest,
    FreezerMovementView,
    FreezerPickupConfirmRequest,
    FreezerPurchaseRequest,
    FreezerRecallDecisionRequest,
    FreezerRecallRequestCreate,
    FreezerRecallRescheduleRequest,
    FreezerRepairRequest,
    FreezerScrapRequest,
    FreezerView,
    StockCountView,
)
from app.services.freezer_service import FreezerService

router = APIRouter(
    prefix="/api/freezers",
    tags=["freezers"],
    dependencies=[Depends(require_ability(MODULE_FREEZER))],
)


@router.post("", response_model=FreezerView, status_code=status.HTTP_201_CREATED)
def purchase(
    body: FreezerPurchaseRequest,
    actor: ActorContext = Depends(current_actor),
    session: Session = Depends(db_session),
) -> FreezerView:
    """购置入库，建冰柜台账。"""
    service = FreezerService(session)
    asset = service.purchase(actor, body)
    session.commit()
    return service.to_view(asset)


@router.get("", response_model=list[FreezerView])
def list_assets(
    asset_code: str | None = Query(default=None, description="资产编码模糊匹配"),
    status: str | None = Query(default=None, description="受控状态码，如 IN_STOCK"),
    merchant_id: UUID | None = Query(default=None, description="只看某商家当前在店资产"),
    limit: int = Query(default=100, ge=1, le=200),
    actor: ActorContext = Depends(current_actor),
    session: Session = Depends(db_session),
) -> list[FreezerView]:
    """冰柜资产台账列表（PRD §4.5）。

    行级范围只由服务端按 actor 解析（业务员=本人负责商家；片区角色=授权片区；
    全局角色不额外裁剪），与 T23 `freezer.search` 共用同一 Service 方法。
    """
    return FreezerService(session).search_assets(
        actor,
        asset_code=asset_code,
        status=status,
        merchant_id=merchant_id,
        limit=limit,
    )


@router.get("/stock/count", response_model=StockCountView)
def stock_count(
    actor: ActorContext = Depends(current_actor),
    session: Session = Depends(db_session),
) -> StockCountView:
    """可用库存（仅在库；待验收/维修不计，R11）。"""
    return StockCountView(in_stock=FreezerService(session).available_stock_count(actor))


@router.get("/{asset_id}", response_model=dict)
def detail(
    asset_id: UUID,
    actor: ActorContext = Depends(current_actor),
    session: Session = Depends(db_session),
) -> dict:
    view, movements = FreezerService(session).get_detail(actor, asset_id)
    return {
        "asset": view.model_dump(mode="json"),
        "movements": [m.model_dump(mode="json") for m in movements],
    }


# ---------------- 投放 ----------------
@router.post(
    "/{asset_id}/deployments",
    response_model=FreezerMovementView,
    status_code=status.HTTP_201_CREATED,
)
def create_deployment(
    asset_id: UUID,
    body: FreezerDeployRequest,
    actor: ActorContext = Depends(current_actor),
    session: Session = Depends(db_session),
) -> FreezerMovementView:
    """创建投放单（在库→已预留）。合同必须生效（Q048）。"""
    service = FreezerService(session)
    movement = service.create_deployment(actor, asset_id, body)
    session.commit()
    return service.to_movement_view(movement)


@router.post("/{asset_id}/deployments/confirm", response_model=FreezerView)
def confirm_deployment(
    asset_id: UUID,
    body: FreezerDeployConfirmRequest,
    actor: ActorContext = Depends(current_actor),
    session: Session = Depends(db_session),
) -> FreezerView:
    """现场投放完成（已预留→已投放），此刻收缴押金（Q049/Q181）。"""
    service = FreezerService(session)
    asset, _ = service.confirm_deployment(actor, asset_id, body)
    session.commit()
    return service.to_view(asset)


# ---------------- 撤回（撤柜）申请：业务员发起 → 片区经理审批 ----------------
@router.post("/{asset_id}/recall/request", response_model=FreezerView)
def request_recall(
    asset_id: UUID,
    body: FreezerRecallRequestCreate,
    actor: ActorContext = Depends(current_actor),
    session: Session = Depends(db_session),
) -> FreezerView:
    """提交撤回申请（业务员，需填计划回收日期与原因）。资产仍为已投放，待经理审批。"""
    service = FreezerService(session)
    asset = service.request_recall(actor, asset_id, body)
    session.commit()
    return service.to_view(asset)


@router.post("/{asset_id}/recall/decision", response_model=FreezerView)
def decide_recall(
    asset_id: UUID,
    body: FreezerRecallDecisionRequest,
    actor: ActorContext = Depends(current_actor),
    session: Session = Depends(db_session),
) -> FreezerView:
    """片区经理审批撤回申请；同意则转「待撤回」并生成回收待办（不可自审）。"""
    service = FreezerService(session)
    asset = service.decide_recall(actor, asset_id, body)
    session.commit()
    return service.to_view(asset)


@router.post("/{asset_id}/recall/reschedule", response_model=FreezerView)
def reschedule_recall(
    asset_id: UUID,
    body: FreezerRecallRescheduleRequest,
    actor: ActorContext = Depends(current_actor),
    session: Session = Depends(db_session),
) -> FreezerView:
    """修改计划回收日期（申请人本人或片区经理；现场回收确认后锁定）。"""
    service = FreezerService(session)
    asset = service.reschedule_recall(actor, asset_id, body)
    session.commit()
    return service.to_view(asset)


# ---------------- 撤柜 / 验收 ----------------
@router.post("/{asset_id}/pickup/confirm", response_model=FreezerView)
def confirm_pickup(
    asset_id: UUID,
    body: FreezerPickupConfirmRequest,
    actor: ActorContext = Depends(current_actor),
    session: Session = Depends(db_session),
) -> FreezerView:
    """现场回收（待撤回→待验收）。库存不加（Q050）。"""
    service = FreezerService(session)
    service.confirm_pickup(actor, asset_id, body)
    session.commit()
    asset = service.repo.get(asset_id)
    return service.to_view(asset)  # type: ignore[arg-type]


@router.post("/{asset_id}/acceptance", response_model=dict)
def accept(
    asset_id: UUID,
    body: FreezerAcceptanceRequest,
    actor: ActorContext = Depends(current_actor),
    session: Session = Depends(db_session),
) -> dict:
    """仓库验收：IN_STOCK 库存 +1（Q051）；REPAIR/SCRAP 不加。"""
    service = FreezerService(session)
    asset, movement = service.accept(actor, asset_id, body)
    in_stock = service.available_stock_count(actor)
    session.commit()
    return {
        "asset": service.to_view(asset).model_dump(mode="json"),
        "acceptance": service.to_movement_view(movement).model_dump(mode="json"),
        "in_stock": in_stock,
    }


# ---------------- 维修 / 报废 ----------------
@router.post("/{asset_id}/repair/start", response_model=FreezerView)
def start_repair(
    asset_id: UUID,
    body: FreezerRepairRequest,
    actor: ActorContext = Depends(current_actor),
    session: Session = Depends(db_session),
) -> FreezerView:
    """故障转维修（Q052）。"""
    service = FreezerService(session)
    service.start_repair(actor, asset_id, body)
    session.commit()
    asset = service.repo.get(asset_id)
    return service.to_view(asset)  # type: ignore[arg-type]


@router.post("/{asset_id}/repair/complete", response_model=FreezerView)
def complete_repair(
    asset_id: UUID,
    body: FreezerRepairRequest,
    actor: ActorContext = Depends(current_actor),
    session: Session = Depends(db_session),
) -> FreezerView:
    service = FreezerService(session)
    service.complete_repair(actor, asset_id, body)
    session.commit()
    asset = service.repo.get(asset_id)
    return service.to_view(asset)  # type: ignore[arg-type]


@router.post("/{asset_id}/scrap", response_model=FreezerView)
def scrap(
    asset_id: UUID,
    body: FreezerScrapRequest,
    actor: ActorContext = Depends(current_actor),
    session: Session = Depends(db_session),
) -> FreezerView:
    """维修中报废（不可逆，Q053）。"""
    service = FreezerService(session)
    service.scrap(actor, asset_id, body)
    session.commit()
    asset = service.repo.get(asset_id)
    return service.to_view(asset)  # type: ignore[arg-type]


# ---------------- 押金（R37，不入损益）----------------
@router.post("/{asset_id}/deposit/refund", response_model=FreezerMovementView)
def refund_deposit(
    asset_id: UUID,
    body: FreezerDepositRefundRequest,
    actor: ActorContext = Depends(current_actor),
    session: Session = Depends(db_session),
) -> FreezerMovementView:
    """财务确认实际退押金：精确到分、关联验收单、不进损益（Q181）。"""
    service = FreezerService(session)
    movement = service.confirm_deposit_refund(actor, asset_id, body)
    session.commit()
    return service.to_movement_view(movement)
