"""T19 绩效规则版本与月度快照（PRD §4.10、§6.5、R19/R20/R24/R40）。

Q102 仅生效合同；Q103 仅有效巡访；Q104 仅经理确认销量；Q105 规则切换边界；
Q106 历史不回算；Q107 Decimal/分精度；Q108 快照可反查；Q183 商家主动取消
零默认扣款、无审批依据的扣减被拒绝。调拨日切分另见 test_t14（Q074-Q078）。
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from tests.test_t09_contract import _ctx, _merchant, _user, _world

from app.core.errors import DomainError
from app.models import (
    Activity,
    ActivityParticipant,
    ApprovalRequest,
    Contract,
    MerchantAssignment,
    PerformanceSnapshotAdjustment,
    SalesConfirmation,
    Sku,
    UserRegionBrandAuth,
    VisitRecord,
    VisitTask,
)
from app.models.enums import (
    ActivityStatus,
    ContractStatus,
    PerformanceAdjustmentKind,
    RoleCode,
)
from app.services.performance_snapshot_service import PerformanceSnapshotService
from app.services.transfer_service import shanghai_day_start_utc

MONTH = "2026-06"
PARAMS_V1 = {
    "new_contract_award_fen": 5000,
    "valid_visit_award_fen": 1000,
    "activity_award_fen": 20000,
    "sales_tiers": [(1_000_000, 200), (None, 300)],
}
PARAMS_V2 = {
    "new_contract_award_fen": 8000,
    "valid_visit_award_fen": 1500,
    "activity_award_fen": 30000,
    "sales_tiers": [(2_000_000, 250), (None, 400)],
}
_FIN = {}


def _actor(user, role, w):
    return _ctx(user, role.value, frozenset({w["region"].id}))


def _auth(session, user, w):
    session.add(UserRegionBrandAuth(
        user_id=user.id, region_id=w["region"].id, brand_id=w["brand"].id))
    session.flush()


def _setup(session, two_reps=False):
    w = _world(session)
    mgr = _user(session, w["ds"], RoleCode.REGIONAL_MANAGER, f"mgr19-{uuid4().hex[:4]}")
    fin = _user(session, w["ds"], RoleCode.FINANCE, f"fin19-{uuid4().hex[:4]}")
    _auth(session, mgr, w)
    _auth(session, fin, w)
    rep = _user(session, w["ds"], RoleCode.SALES_REP, f"rep19-{uuid4().hex[:4]}")
    rep2 = (_user(session, w["ds"], RoleCode.SALES_REP, f"rep19b-{uuid4().hex[:4]}")
            if two_reps else None)
    merchant = _merchant(session, w, name=f"T19店-{uuid4().hex[:5]}")
    session.flush()
    session.add(MerchantAssignment(
        merchant_id=merchant.id, owner_user_id=rep.id,
        assignment_type="PRIMARY",
        valid_range=func.tstzrange(
            func.to_timestamp(datetime(2026, 1, 1, tzinfo=UTC).timestamp()),
            None, "[)"),
        dataset_type="PRODUCTION", dataset_id=w["ds"]))
    session.flush()
    _FIN["actor"] = _actor(fin, RoleCode.FINANCE, w)
    return (w, mgr, _actor(mgr, RoleCode.REGIONAL_MANAGER, w), fin,
            rep, rep2, merchant)


def _rule(svc, mgr_actor, params, code="RULE19", eff=date(2026, 1, 1)):
    created = svc.create_rule_version(
        mgr_actor, params=params, effective_from=eff,
        name=f"{code}-{uuid4().hex[:4]}", code=code)
    return svc.review_rule_version(_FIN["actor"], created.id, note="财务审核")


def _contract(session, w, merchant, day, *, status=ContractStatus.EFFECTIVE,
              signed=True):
    c = Contract(
        contract_no=f"C-{uuid4().hex[:10]}", title="经销协议",
        party_name=merchant.name, merchant_id=merchant.id, status=status.value,
        # 已生效合同必须已选模板（T09 口径）
        template_version="v1" if status == ContractStatus.EFFECTIVE else None,
        signed_at=day if signed else None, effective_from=day,
        expire_at=date(2027, 12, 31),
        dataset_type="PRODUCTION", dataset_id=w["ds"])
    session.add(c)
    session.flush()
    return c


def _visit(session, w, merchant, user, day, *, validity="VALID",
           source="WEEKLY_SCHEDULE"):
    # 任务必须 COMPLETED 才能挂正式记录
    t = VisitTask(
        merchant_id=merchant.id, assigned_user_id=user.id, planned_date=day,
        status="COMPLETED", source=source, priority=2,
        dataset_type="PRODUCTION", dataset_id=w["ds"])
    session.add(t)
    session.flush()
    # validity=VALID 时记录可为经理认可 RECOGNIZED；异常待判定 ABNORMAL_PENDING
    rec_status = "RECOGNIZED" if validity == "VALID" else "ABNORMAL_PENDING"
    v = VisitRecord(
        visit_task_id=t.id, merchant_id=merchant.id, user_id=user.id,
        source=source, is_official=True, status=rec_status, validity=validity,
        conclusion="NORMAL", freezer_status="NORMAL", photo_count=1,
        submitted_at=shanghai_day_start_utc(day).replace(tzinfo=None),
        dataset_type="PRODUCTION", dataset_id=w["ds"])
    session.add(v)
    session.flush()
    return v


def _sku(session, w):
    s = Sku(code=f"SKU-{uuid4().hex[:8]}", name="沁爽汽水", brand_id=w["brand"].id,
            suggested_price_fen=300, case_spec=Decimal("24"),
            dataset_type="PRODUCTION", dataset_id=w["ds"])
    session.add(s)
    session.flush()
    return s


def _sales(session, w, merchant, sku, confirmer, day, *, boxes, amount_fen,
           in_month=True):
    s = SalesConfirmation(
        merchant_id=merchant.id, sku_id=sku.id,
        biz_date=day if in_month else date(2025, 1, 1),
        confirmed_boxes=Decimal(boxes), amount_fen=amount_fen,
        confirmer_id=confirmer.id,
        dataset_type="PRODUCTION", dataset_id=w["ds"])
    session.add(s)
    session.flush()
    return s


def _activity(session, w, owner, participant, day, *,
              status=ActivityStatus.REVIEWED):
    start = shanghai_day_start_utc(day)
    a = Activity(
        name=f"地推-{uuid4().hex[:4]}", start_at=start.replace(tzinfo=None),
        end_at=start.replace(hour=2, tzinfo=None), location="门店广场",
        owner_user_id=owner.id, status=status.value, region_id=w["region"].id,
        dataset_type="PRODUCTION", dataset_id=w["ds"])
    session.add(a)
    session.flush()
    session.add(ActivityParticipant(
        activity_id=a.id, user_id=participant.id,
        dataset_type="PRODUCTION", dataset_id=w["ds"]))
    session.flush()
    return a


def _approval(session, w, requester, *, action, user_id, amount_fen,
              status="APPROVED", day=date(2026, 6, 28)):
    ap = ApprovalRequest(
        target_object_type="PERFORMANCE_ADJUSTMENT", target_object_id=uuid4(),
        action=action, requester_id=requester.id, status=status,
        reason="依据", decided_at=(shanghai_day_start_utc(day)
                                   if status == "APPROVED" else None),
        matrix_role_code=RoleCode.REGIONAL_MANAGER.value,
        request_payload={"user_id": str(user_id), "amount_fen": amount_fen},
        dataset_type="PRODUCTION", dataset_id=w["ds"])
    session.add(ap)
    session.flush()
    return ap


# ----------------------------- Q102 -----------------------------


def test_q102_only_effective_contracts(session: Session) -> None:
    w, _m, mgr_actor, _f, rep, _r2, merchant = _setup(session)
    svc = PerformanceSnapshotService(session)
    _rule(svc, mgr_actor, PARAMS_V1)
    _contract(session, w, merchant, date(2026, 6, 10))
    _contract(session, w, merchant, date(2026, 6, 11),
              status=ContractStatus.DRAFT, signed=False)
    _contract(session, w, merchant, date(2026, 6, 12),
              status=ContractStatus.PENDING_APPROVAL, signed=False)
    snap = svc.freeze_month(mgr_actor, user_id=rep.id, period_month=MONTH)
    assert snap.new_contract_fen == 5000
    assert len([ln for ln in svc.repo.list_lines(snap.id)
                if ln.line_type == "NEW_CONTRACT"]) == 1


# ----------------------------- Q103 -----------------------------


def test_t35_visits_no_longer_counted(session: Session) -> None:
    """T35：有效巡访奖励下线——巡访只做过程管理，不再产生金额与依据行。"""
    w, _m, mgr_actor, _f, rep, _r2, merchant = _setup(session)
    svc = PerformanceSnapshotService(session)
    _rule(svc, mgr_actor, PARAMS_V1)
    _visit(session, w, merchant, rep, date(2026, 6, 3), validity="VALID")
    _visit(session, w, merchant, rep, date(2026, 6, 4), validity="VALID")
    _visit(session, w, merchant, rep, date(2026, 6, 5),
           validity="ABNORMAL_PENDING")
    # 督导跟访：记录 VALID 但来源 SUPERVISION，同样不计绩效
    _visit(session, w, merchant, rep, date(2026, 6, 6),
           validity="VALID", source="SUPERVISION")
    snap = svc.freeze_month(mgr_actor, user_id=rep.id, period_month=MONTH)
    assert snap.visit_bonus_fen == 0
    assert [ln for ln in svc.repo.list_lines(snap.id)
            if ln.line_type == "VALID_VISIT"] == []


# ----------------------------- Q104 -----------------------------


def test_t35_confirmed_sales_no_commission(session: Session) -> None:
    """T35：确认销量提成下线——销量只进损益，不计入个人绩效。"""
    w, mgr, mgr_actor, _f, rep, _r2, merchant = _setup(session)
    svc = PerformanceSnapshotService(session)
    _rule(svc, mgr_actor, PARAMS_V1)
    sku = _sku(session, w)
    _sales(session, w, merchant, sku, mgr, date(2026, 6, 15),
           boxes="100", amount_fen=600_000)
    _sales(session, w, merchant, sku, mgr, date(2026, 6, 16),
           boxes="999", amount_fen=9_000_000, in_month=False)
    snap = svc.freeze_month(mgr_actor, user_id=rep.id, period_month=MONTH)
    assert snap.sales_commission_fen == 0
    assert [ln for ln in svc.repo.list_lines(snap.id)
            if ln.line_type == "CONFIRMED_SALES"] == []


# ----------------------------- Q105 -----------------------------


def test_q105_rule_switch_boundary(session: Session) -> None:
    w, _m, mgr_actor, _f, rep, _r2, merchant = _setup(session)
    svc = PerformanceSnapshotService(session)
    v1 = svc.create_rule_version(
        mgr_actor, params=PARAMS_V1, effective_from=date(2026, 1, 1),
        name="v1", code="SWITCH")
    svc.review_rule_version(_FIN["actor"], v1.id)
    v2 = svc.create_rule_version(
        mgr_actor, params=PARAMS_V2, effective_from=date(2026, 6, 1),
        name="v2", code="SWITCH")
    svc.review_rule_version(_FIN["actor"], v2.id)
    session.refresh(v1)
    assert v1.effective_to == date(2026, 6, 1)

    _contract(session, w, merchant, date(2026, 5, 20))
    may = svc.freeze_month(mgr_actor, user_id=rep.id, period_month="2026-05")
    assert may.rule_version == 1 and may.new_contract_fen == 5000

    _contract(session, w, merchant, date(2026, 6, 20))
    jun = svc.freeze_month(mgr_actor, user_id=rep.id, period_month="2026-06")
    assert jun.rule_version == 2 and jun.new_contract_fen == 8000


# ----------------------------- Q106 -----------------------------


def test_q106_history_not_recalculated(session: Session) -> None:
    w, _m, mgr_actor, _f, rep, _r2, merchant = _setup(session)
    svc = PerformanceSnapshotService(session)
    v1 = svc.create_rule_version(
        mgr_actor, params=PARAMS_V1, effective_from=date(2026, 1, 1),
        name="v1", code="HIST")
    svc.review_rule_version(_FIN["actor"], v1.id)
    _contract(session, w, merchant, date(2026, 6, 5))
    snap = svc.freeze_month(mgr_actor, user_id=rep.id, period_month=MONTH)
    svc.confirm_snapshot(mgr_actor, snap.id)
    old_net, old_rule = snap.net_performance_fen, snap.rule_version_id

    v2 = svc.create_rule_version(
        mgr_actor, params=PARAMS_V2, effective_from=date(2026, 7, 1),
        name="v2", code="HIST")
    svc.review_rule_version(_FIN["actor"], v2.id)
    _contract(session, w, merchant, date(2026, 6, 25))

    again = svc.freeze_month(mgr_actor, user_id=rep.id, period_month=MONTH)
    assert again.id == snap.id
    assert again.net_performance_fen == old_net
    assert again.rule_version_id == old_rule
    assert again.new_contract_fen == 5000


# ----------------------------- Q107 -----------------------------


def test_t35_snapshot_amounts_are_integer_fen(session: Session) -> None:
    """T35：快照金额全为整数分，净额 = 新签 + 活动（销量单据不再产生提成行）。"""
    w, mgr, mgr_actor, _f, rep, _r2, merchant = _setup(session)
    svc = PerformanceSnapshotService(session)
    _rule(svc, mgr_actor, PARAMS_V1)
    sku = _sku(session, w)
    _contract(session, w, merchant, date(2026, 6, 3))
    _activity(session, w, mgr, rep, date(2026, 6, 18))
    _sales(session, w, merchant, sku, mgr, date(2026, 6, 18),
           boxes="12.5", amount_fen=1_500_000)
    snap = svc.freeze_month(mgr_actor, user_id=rep.id, period_month=MONTH)
    assert snap.sales_commission_fen == 0
    assert snap.new_contract_fen == 5000
    assert snap.activity_bonus_fen == 20_000
    assert snap.net_performance_fen == 25_000
    for value in (snap.new_contract_fen, snap.activity_bonus_fen,
                  snap.net_performance_fen):
        assert isinstance(value, int)
    for ln in svc.repo.list_lines(snap.id):
        assert isinstance(ln.amount_fen, int)


def test_t35_engine_ignores_legacy_inputs() -> None:
    """T35：引擎对巡访/销量/扣减/调整入参一律不计算（仅保留新签 + 活动）。"""
    from app.services.performance_engine import (
        ApprovedItem,
        EngineFact,
        PerformanceEngine,
        RuleParams,
    )
    params = RuleParams(
        new_contract_award_fen=5000, valid_visit_award_fen=1000,
        activity_award_fen=8000, sales_tiers=((10000, 200), (None, 300)))
    r = PerformanceEngine().compute(
        params=params,
        new_contracts=[EngineFact(object_type="C", object_id="1",
                                  fact_date=date(2026, 6, 1))],
        activities=[EngineFact(object_type="A", object_id="2",
                               fact_date=date(2026, 6, 2))],
        valid_visits=[EngineFact(object_type="V", object_id="3",
                                 fact_date=date(2026, 6, 3))],
        confirmed_sales=[EngineFact(object_type="S", object_id="4",
                                    fact_date=date(2026, 6, 4),
                                    amount_fen=1_000_000,
                                    quantity=Decimal("10"))],
        deductions=[ApprovedItem(object_type="D", object_id="5",
                                 amount_fen=-2000, fact_date=date(2026, 6, 5),
                                 reason="历史扣减")],
        adjustments=[ApprovedItem(object_type="J", object_id="6",
                                  amount_fen=3000, fact_date=date(2026, 6, 6),
                                  reason="历史调整")],
    )
    assert r.new_contract_fen == 5000
    assert r.activity_bonus_fen == 8000
    assert r.visit_bonus_fen == 0
    assert r.sales_commission_fen == 0
    assert r.deduction_fen == 0
    assert r.adjustment_fen == 0
    assert r.net_fen == 13_000
    assert {ln.line_type for ln in r.lines} == {"NEW_CONTRACT", "ACTIVITY_ACHIEVED"}
    assert isinstance(r.net_fen, int)


# ----------------------------- Q108 -----------------------------


def test_q108_snapshot_lines_traceable(session: Session) -> None:
    w, mgr, mgr_actor, _f, rep, _r2, merchant = _setup(session)
    svc = PerformanceSnapshotService(session)
    _rule(svc, mgr_actor, PARAMS_V1)
    c = _contract(session, w, merchant, date(2026, 6, 8))
    v = _visit(session, w, merchant, rep, date(2026, 6, 9))
    sku = _sku(session, w)
    s = _sales(session, w, merchant, sku, mgr, date(2026, 6, 10),
               boxes="10", amount_fen=500_000)
    a = _activity(session, w, mgr, rep, date(2026, 6, 11))
    snap = svc.freeze_month(mgr_actor, user_id=rep.id, period_month=MONTH)
    lines = svc.get_snapshot_detail(mgr_actor, snap.id)["lines"]
    ids = {(ln.line_type, ln.source_object_id) for ln in lines}
    assert ("NEW_CONTRACT", c.id) in ids
    assert ("ACTIVITY_ACHIEVED", a.id) in ids
    # T35：巡访与销量不再是绩效依据（无对应行）
    assert ("VALID_VISIT", v.id) not in ids
    assert ("CONFIRMED_SALES", s.id) not in ids
    for ln in lines:
        assert ln.owner_user_id == rep.id
        assert ln.fact_date is not None
        assert isinstance(ln.amount_fen, int)
        assert ln.basis_json
    # 50（新签）+ 200（活动）= 250 元
    assert snap.net_performance_fen == 25_000
    assert snap.rule_version_id is not None and snap.rule_version == 1

    # 页面只读列表（T19 前端联调）：规则版本 / 快照（带业务员）/ 人员
    rules = svc.list_rule_versions(mgr_actor)
    assert [r.id for r in rules] == [snap.rule_version_id]
    assert [r.id for r in svc.list_rule_versions(mgr_actor, code="OTHER")] == []
    rows = svc.list_snapshots(mgr_actor, period_month=MONTH, status="DRAFT")
    assert [(s.id, u.id) for s, u in rows] == [(snap.id, rep.id)]
    assert svc.list_snapshots(mgr_actor, status="REVIEWED") == []
    assert rep.id in [u.id for u in svc.list_user_options(mgr_actor,
                                                          role_code="SALES_REP")]
    with pytest.raises(DomainError):
        svc.list_snapshots(mgr_actor, status="BOGUS")
    with pytest.raises(DomainError):
        svc.list_user_options(mgr_actor, role_code="NOT_A_ROLE")


# ----------------------------- Q183 -----------------------------


def test_q183_merchant_cancel_no_default_deduction(session: Session) -> None:
    w, mgr, mgr_actor, _f, rep, _r2, merchant = _setup(session)
    svc = PerformanceSnapshotService(session)
    _rule(svc, mgr_actor, PARAMS_V1)
    _contract(session, w, merchant, date(2026, 6, 8))
    # 合同终止/商家取消类审批（非 performance.deduction）→ 不产生扣减
    _approval(session, w, mgr, action="contract.terminate",
              user_id=rep.id, amount_fen=-5000)
    snap = svc.freeze_month(mgr_actor, user_id=rep.id, period_month=MONTH)
    assert snap.new_contract_fen == 5000
    assert snap.deduction_fen == 0
    assert snap.net_performance_fen == 5000


def test_t35_deduction_entry_disabled(session: Session) -> None:
    """T35：更正单入口下线——哪怕有已审批依据也不再接受扣减/调整。"""
    w, mgr, mgr_actor, _f, rep, _r2, merchant = _setup(session)
    svc = PerformanceSnapshotService(session)
    _rule(svc, mgr_actor, PARAMS_V1)
    _contract(session, w, merchant, date(2026, 6, 8))
    snap = svc.freeze_month(mgr_actor, user_id=rep.id, period_month=MONTH)
    svc.confirm_snapshot(mgr_actor, snap.id)

    approved = _approval(session, w, mgr, action="performance.deduction",
                         user_id=rep.id, amount_fen=-2000)
    with pytest.raises(DomainError) as exc:
        svc.apply_adjustment(
            mgr_actor, snapshot_id=snap.id,
            kind=PerformanceAdjustmentKind.DEDUCTION.value,
            amount_fen=-2000, approval_id=approved.id, reason="责任扣减")
    assert exc.value.code == "PERF_ADJUSTMENT_DISABLED"

    with pytest.raises(DomainError) as exc2:
        svc.apply_adjustment(
            mgr_actor, snapshot_id=snap.id,
            kind=PerformanceAdjustmentKind.ADJUSTMENT.value,
            amount_fen=3000, approval_id=approved.id, reason="奖励调整")
    assert exc2.value.code == "PERF_ADJUSTMENT_DISABLED"

    assert session.scalar(select(func.count())
                          .select_from(PerformanceSnapshotAdjustment)) == 0
    session.refresh(snap)
    assert snap.deduction_fen == 0
    assert snap.adjustment_fen == 0
    assert snap.net_performance_fen == 5000


# --------------------- 调拨日切分（与 T14 Q074-Q078 同口径） ---------------------


def test_t19_transfer_effective_day_split(session: Session) -> None:
    """生效日前新签归 A；生效日（含）当天及以后归 B（T14 owner_at 口径）。"""
    w, mgr, mgr_actor, _f, rep_a, rep_b, merchant = _setup(session, two_reps=True)
    svc = PerformanceSnapshotService(session)
    _rule(svc, mgr_actor, PARAMS_V1)
    effective = date(2026, 6, 15)
    # 生效日前的合同事实归 A
    _contract(session, w, merchant, date(2026, 6, 10))
    # 调拨走 T14 TransferService；绩效服务复用其 owner_at 归属口径
    from app.services.transfer_service import TransferService
    t = TransferService(session).create_transfer(
        mgr_actor, merchant_id=merchant.id, from_user_id=rep_a.id,
        to_user_id=rep_b.id, effective_date=effective, reason="片区调整")
    TransferService(session).apply(mgr_actor, transfer_id=t.id)
    _contract(session, w, merchant, date(2026, 6, 15))
    _contract(session, w, merchant, date(2026, 6, 20))

    snap_a = svc.freeze_month(mgr_actor, user_id=rep_a.id, period_month=MONTH)
    snap_b = svc.freeze_month(mgr_actor, user_id=rep_b.id, period_month=MONTH)
    # A：1 单 ×50 元；B：2 单 ×50 元
    assert snap_a.new_contract_fen == 5000
    assert snap_b.new_contract_fen == 10_000


def test_t19_self_review_forbidden(session: Session) -> None:
    """规则配置人不能审核自己的规则（R19 职责分离）。"""
    from app.core.errors import PermissionDeniedError as PD
    w, _m, mgr_actor, _f, rep, _r2, merchant = _setup(session)
    svc = PerformanceSnapshotService(session)
    created = svc.create_rule_version(
        mgr_actor, params=PARAMS_V1, effective_from=date(2026, 1, 1),
        name="self", code="SELF")
    with pytest.raises(PD) as exc:
        svc.review_rule_version(mgr_actor, created.id)
    assert exc.value.code == "PERF_RULE_SELF_REVIEW_FORBIDDEN"
