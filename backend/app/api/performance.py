"""/api/performance 路由：绩效规则版本与月度快照（T19 / PRD §4.10）。

Router 只鉴权/校验/装配；规则校验、状态机、事务、调拨日归属、审批依据校验与
金额重算全部在 PerformanceSnapshotService。
"""

from __future__ import annotations

# ruff: noqa: B008  FastAPI Depends() 需出现在默认值
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.deps import current_actor, db_session
from app.core.permissions import ActorContext
from app.schemas.performance import (
    AdjustmentView,
    CreateRuleVersionRequest,
    FreezeSnapshotRequest,
    OptionListView,
    OptionView,
    ReturnSnapshotRequest,
    ReviewRuleVersionRequest,
    RuleVersionView,
    SnapshotDetailView,
    SnapshotLineView,
    SnapshotListItemView,
    SnapshotView,
)
from app.services.performance_snapshot_service import PerformanceSnapshotService

router = APIRouter(prefix="/api/performance", tags=["performance"])


# ----------------------------- 规则版本 -----------------------------


def _rule_view(rule) -> RuleVersionView:
    return RuleVersionView(
        id=rule.id, code=rule.code, version=rule.version, name=rule.name,
        status=rule.status, effective_from=rule.effective_from,
        effective_to=rule.effective_to, params_json=rule.params_json,
        configured_by_id=rule.configured_by_id,
        reviewed_by_id=rule.reviewed_by_id, review_note=rule.review_note,
    )


@router.get("/rule-versions", response_model=list[RuleVersionView])
def list_rule_versions(
    code: str | None = Query(default=None, max_length=32),
    session: Session = Depends(db_session),
    actor: ActorContext = Depends(current_actor),
):
    """规则版本列表：含 DRAFT/ACTIVE/SUPERSEDED，历史版本仍可反查（Q105/Q106）。"""
    rules = PerformanceSnapshotService(session).list_rule_versions(
        actor, code=code)
    return [_rule_view(r) for r in rules]


@router.post("/rule-versions", response_model=RuleVersionView, status_code=201)
def create_rule_version(
    body: CreateRuleVersionRequest,
    session: Session = Depends(db_session),
    actor: ActorContext = Depends(current_actor),
):
    params = body.params.model_dump()
    # 阶梯以 (cap, bp) 元组落库
    params["sales_tiers"] = [
        (t["cap_fen"], t["rate_bp"]) for t in params["sales_tiers"]
    ]
    rule = PerformanceSnapshotService(session).create_rule_version(
        actor,
        params=params,
        effective_from=body.effective_from,
        effective_to=body.effective_to,
        name=body.name,
        code=body.code,
    )
    return _rule_view(rule)


@router.post("/rule-versions/{version_id}/review",
             response_model=RuleVersionView)
def review_rule_version(
    version_id: UUID,
    body: ReviewRuleVersionRequest,
    session: Session = Depends(db_session),
    actor: ActorContext = Depends(current_actor),
):
    rule = PerformanceSnapshotService(session).review_rule_version(
        actor, version_id, note=body.note)
    return _rule_view(rule)


# ----------------------------- 月度快照 -----------------------------


def _snapshot_view(s) -> SnapshotView:
    return SnapshotView(
        id=s.id, user_id=s.user_id, period_month=s.period_month,
        period_start=s.period_start, period_end=s.period_end,
        rule_version_id=s.rule_version_id, rule_code=s.rule_code,
        rule_version=s.rule_version, new_contract_fen=s.new_contract_fen,
        visit_bonus_fen=s.visit_bonus_fen,
        sales_commission_fen=s.sales_commission_fen,
        activity_bonus_fen=s.activity_bonus_fen,
        deduction_fen=s.deduction_fen, adjustment_fen=s.adjustment_fen,
        net_performance_fen=s.net_performance_fen, status=s.status,
        frozen_by_id=s.frozen_by_id, frozen_at=s.frozen_at,
        confirmed_by_id=s.confirmed_by_id, confirmed_at=s.confirmed_at,
        reviewed_by_id=s.reviewed_by_id, reviewed_at=s.reviewed_at,
    )


@router.get("/snapshots", response_model=list[SnapshotListItemView])
def list_snapshots(
    period_month: str | None = Query(
        default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    status: str | None = Query(default=None, max_length=16),
    user_id: UUID | None = Query(default=None),
    session: Session = Depends(db_session),
    actor: ActorContext = Depends(current_actor),
):
    """月度快照列表（行级范围由服务端按角色收窄，附带业务员展示名）。"""
    rows = PerformanceSnapshotService(session).list_snapshots(
        actor, period_month=period_month, status=status, user_id=user_id)
    return [
        SnapshotListItemView(
            **_snapshot_view(snap).model_dump(),
            user_username=user.username, user_name=user.display_name,
        )
        for snap, user in rows
    ]


@router.get("/options/users", response_model=OptionListView)
def user_options(
    role_code: str | None = Query(default=None, max_length=32),
    session: Session = Depends(db_session),
    actor: ActorContext = Depends(current_actor),
):
    """绩效人员下拉：数据集内在职用户（冻结快照 / 筛选业务员）。"""
    users = PerformanceSnapshotService(session).list_user_options(
        actor, role_code=role_code)
    return OptionListView(
        items=[OptionView(id=u.id, code=u.username, name=u.display_name)
               for u in users]
    )


@router.post("/snapshots/freeze", response_model=SnapshotDetailView,
             status_code=201)
def freeze_snapshot(
    body: FreezeSnapshotRequest,
    session: Session = Depends(db_session),
    actor: ActorContext = Depends(current_actor),
):
    svc = PerformanceSnapshotService(session)
    snap = svc.freeze_month(
        actor, user_id=body.user_id, period_month=body.period_month,
        rule_version_id=body.rule_version_id)
    detail = svc.get_snapshot_detail(actor, snap.id)
    return _detail_view(detail)


@router.post("/snapshots/{snapshot_id}/confirm",
             response_model=SnapshotView)
def confirm_snapshot(
    snapshot_id: UUID,
    session: Session = Depends(db_session),
    actor: ActorContext = Depends(current_actor),
):
    return _snapshot_view(
        PerformanceSnapshotService(session).confirm_snapshot(actor, snapshot_id))


@router.post("/snapshots/{snapshot_id}/review",
             response_model=SnapshotView)
def review_snapshot(
    snapshot_id: UUID,
    session: Session = Depends(db_session),
    actor: ActorContext = Depends(current_actor),
):
    return _snapshot_view(
        PerformanceSnapshotService(session).review_snapshot(actor, snapshot_id))


@router.post("/snapshots/{snapshot_id}/return",
             response_model=SnapshotView)
def return_snapshot(
    snapshot_id: UUID,
    body: ReturnSnapshotRequest,
    session: Session = Depends(db_session),
    actor: ActorContext = Depends(current_actor),
):
    return _snapshot_view(
        PerformanceSnapshotService(session).return_snapshot(
            actor, snapshot_id, reason=body.reason))


@router.get("/snapshots/{snapshot_id}", response_model=SnapshotDetailView)
def get_snapshot(
    snapshot_id: UUID,
    session: Session = Depends(db_session),
    actor: ActorContext = Depends(current_actor),
):
    return _detail_view(
        PerformanceSnapshotService(session).get_snapshot_detail(
            actor, snapshot_id))


def _detail_view(detail: dict) -> SnapshotDetailView:
    snap = detail["snapshot"]
    base = _snapshot_view(snap).model_dump()
    base["lines"] = [
        SnapshotLineView(
            id=ln.id, line_type=ln.line_type,
            source_object_type=ln.source_object_type,
            source_object_id=ln.source_object_id, merchant_id=ln.merchant_id,
            owner_user_id=ln.owner_user_id, fact_date=ln.fact_date,
            quantity=ln.quantity, amount_fen=ln.amount_fen,
            basis_json=ln.basis_json,
        )
        for ln in detail["lines"]
    ]
    base["adjustments"] = [
        AdjustmentView(
            id=a.id, snapshot_id=a.snapshot_id, kind=a.kind,
            amount_fen=a.amount_fen, approval_id=a.approval_id,
            reason=a.reason, created_by_id=a.created_by_id,
        )
        for a in detail["adjustments"]
    ]
    return SnapshotDetailView(**base)
