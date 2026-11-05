"""/api/workbench 路由：角色首页聚合、通知中心、全局搜索（T27 / PRD §4.2）。

Router 只鉴权/校验/装配；角色差异、行级裁剪、指标口径与事务边界全部在 Service。
- GET  /home         一次聚合：指标卡 + 待办 + 风险 + 快捷动作（Q154/Q156/Q185）
- GET  /notifications 通知列表（任务/审批/风险，已读独立，Q158）
- GET  /notifications/unread-count
- POST /notifications/read        标记单条已读（不改来源对象状态，Q158）
- POST /notifications/read-all
- GET  /search/merchants 全局搜索（只返回有权对象，越权为空，Q155；深链 Q157）

风险关闭复用既有 POST /api/reporting/risks/{id}/close（RiskService，同事务留痕）。
"""

from __future__ import annotations

# ruff: noqa: B008  FastAPI Depends()/Query() 需出现在默认值
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.deps import current_actor, db_session
from app.core.permissions import ActorContext
from app.schemas.workbench import (
    MarkAllReadView,
    MarkReadRequest,
    MetricCard,
    NotificationView,
    QuickAction,
    RiskCard,
    SearchHitView,
    TodoItem,
    UnreadCountView,
    WorkbenchHome,
)
from app.services.notification_service import NotificationService
from app.services.search_service import GlobalSearchService
from app.services.workbench_service import WorkbenchService

router = APIRouter(prefix="/api/workbench", tags=["workbench"])


# ============================== 角色首页聚合 ==============================

@router.get("/home", response_model=WorkbenchHome)
def workbench_home(
    period: str = Query(..., description="统计周期 YYYY-MM 或 YYYY-Www"),
    risk_limit: int = Query(10, ge=1, le=100),
    todo_limit: int = Query(20, ge=1, le=200),
    session: Session = Depends(db_session),
    actor: ActorContext = Depends(current_actor),
):
    data = WorkbenchService(session).home(
        actor, period=period, risk_limit=risk_limit, todo_limit=todo_limit
    )
    # 强类型化（Service 返回 dict，边界处转 Pydantic，避免 ORM 直出）
    return WorkbenchHome(
        role=data["role"],
        period=data["period"],
        as_of=data["as_of"],
        metric_cards=[MetricCard(**c) for c in data["metric_cards"]],
        todos=[TodoItem(**t) for t in data["todos"]],
        unread_count=data["unread_count"],
        risk_list=[RiskCard(**r) for r in data["risk_list"]],
        quick_actions=[QuickAction(**a) for a in data["quick_actions"]],
        deep_links=data["deep_links"],
    )


# ============================== 通知中心 ==============================

@router.get("/notifications", response_model=list[NotificationView])
def list_notifications(
    limit: int = Query(50, ge=1, le=200),
    session: Session = Depends(db_session),
    actor: ActorContext = Depends(current_actor),
):
    items = NotificationService(session).list_notifications(actor, limit=limit)
    return [NotificationView(**n.to_dict()) for n in items]


@router.get("/notifications/unread-count", response_model=UnreadCountView)
def unread_count(
    session: Session = Depends(db_session),
    actor: ActorContext = Depends(current_actor),
):
    return UnreadCountView(
        unread_count=NotificationService(session).unread_count(actor)
    )


@router.post("/notifications/read")
def mark_notification_read(
    body: MarkReadRequest,
    session: Session = Depends(db_session),
    actor: ActorContext = Depends(current_actor),
):
    svc = NotificationService(session)
    # 不可见键在 Service 直接拒绝；来源已关闭的通知其标记仍幂等成立
    svc.mark_read(actor, body.key)
    hit = next((n for n in svc.list_notifications(actor) if n.key == body.key), None)
    if hit is None:
        return {"key": body.key, "read": True, "active": False}
    return {**hit.to_dict(), "active": True}


@router.post("/notifications/read-all", response_model=MarkAllReadView)
def mark_all_read(
    session: Session = Depends(db_session),
    actor: ActorContext = Depends(current_actor),
):
    return MarkAllReadView(marked=NotificationService(session).mark_all_read(actor))


# ============================== 全局搜索 ==============================

@router.get("/search/merchants", response_model=list[SearchHitView])
def search_merchants(
    q: str = Query(..., min_length=2, max_length=64, description="名称/编号/主联系人"),
    limit: int = Query(20, ge=1, le=20),
    session: Session = Depends(db_session),
    actor: ActorContext = Depends(current_actor),
):
    hits = GlobalSearchService(session).search_merchants(actor, q, limit=limit)
    return [SearchHitView(**h.to_dict()) for h in hits]
