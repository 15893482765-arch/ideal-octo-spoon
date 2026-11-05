"""T20 费用、工资与发放状态（PRD §4.10、§6.5）。

Q109 工资构成精确（底薪+T19绩效+审批调整）；Q110 系统管理员无默认发放权 403；
Q111 生产财务双人复核（发放人≠复核人）；Q112 已发放不可改只能更正单；
Q113 更正单只追加、原四项固化、历史单保留；Q114 未审批费用不进损益。
另含：生产工资引用 DEMO 绩效整单拒绝、跨数据域审批依据拒绝。
"""

from __future__ import annotations

from datetime import date
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session
from tests.test_t09_contract import _ctx, _user, _world

from app.core.errors import DomainError, PermissionDeniedError
from app.models import (
    ApprovalRequest,
    FileObject,
    PerformanceMonthlySnapshot,
    PerformanceRuleVersion,
    UserRegionBrandAuth,
)
from app.models.enums import RoleCode
from app.services.expense_service import ExpenseService
from app.services.payroll_service import PayrollService

MONTH = "2026-09"


# ----------------------------- fixtures / helpers -----------------------------


def _actor(user, role, w):
    return _ctx(user, role.value, frozenset({w["region"].id}))


def _auth(session, user, w):
    session.add(UserRegionBrandAuth(
        user_id=user.id, region_id=w["region"].id, brand_id=w["brand"].id))
    session.flush()


def _setup(session, *, dual_finance=False, extra_finance=False):
    w = _world(session)
    mgr = _user(session, w["ds"], RoleCode.REGIONAL_MANAGER,
                f"mgr20-{uuid4().hex[:4]}")
    fin = _user(session, w["ds"], RoleCode.FINANCE,
                f"fin20-{uuid4().hex[:4]}")
    fin2 = (_user(session, w["ds"], RoleCode.FINANCE,
                  f"fin20b-{uuid4().hex[:4]}") if extra_finance else None)
    rep = _user(session, w["ds"], RoleCode.SALES_REP,
                f"rep20-{uuid4().hex[:4]}")
    for u in (mgr, fin, fin2):
        if u is not None:
            _auth(session, u, w)
    session.flush()
    if dual_finance:
        from app.core.config import get_settings
        get_settings().payroll_dual_finance_review = True
    else:
        from app.core.config import get_settings
        get_settings().payroll_dual_finance_review = False
    return {
        "w": w, "mgr": mgr, "mgr_a": _actor(mgr, RoleCode.REGIONAL_MANAGER, w),
        "fin": fin, "fin_a": _actor(fin, RoleCode.FINANCE, w),
        "fin2": fin2,
        "fin2_a": _actor(fin2, RoleCode.FINANCE, w) if fin2 else None,
        "rep": rep, "rep_a": _actor(rep, RoleCode.SALES_REP, w),
    }


def _rule(session, w, configured_by, reviewed_by, code="RULE20"):
    rule = PerformanceRuleVersion(
        code=code, version=1, name=f"{code}-{uuid4().hex[:4]}",
        status="ACTIVE", effective_from=date(2026, 1, 1), effective_to=None,
        params_json={"new_contract_award_fen": 5000,
                     "valid_visit_award_fen": 1000,
                     "activity_award_fen": 20000,
                     "sales_tiers": [[None, 300]]},
        configured_by_id=configured_by.id, reviewed_by_id=reviewed_by.id,
        review_note="ok", dataset_type="PRODUCTION", dataset_id=w["ds"])
    session.add(rule)
    session.flush()
    return rule


def _reviewed_snapshot(session, w, rule, rep, fin, *, net_fen,
                       month=MONTH):
    snap = PerformanceMonthlySnapshot(
        user_id=rep.id, period_month=month,
        period_start=date(2026, 9, 1), period_end=date(2026, 9, 30),
        rule_version_id=rule.id, rule_code=rule.code, rule_version=rule.version,
        new_contract_fen=0, visit_bonus_fen=0, sales_commission_fen=net_fen,
        activity_bonus_fen=0, deduction_fen=0, adjustment_fen=0,
        net_performance_fen=net_fen, status="REVIEWED",
        reviewed_by_id=fin.id,
        dataset_type="PRODUCTION", dataset_id=w["ds"])
    session.add(snap)
    session.flush()
    return snap


def _approval(session, w, requester, *, action, user_id, amount_fen,
              status="APPROVED", payload_extra=None,
              dataset_type="PRODUCTION", dataset_id=None):
    payload = {"user_id": str(user_id), "amount_fen": amount_fen}
    if payload_extra:
        payload.update(payload_extra)
    ap = ApprovalRequest(
        target_object_type="PAYROLL_ADJUSTMENT", target_object_id=uuid4(),
        action=action, requester_id=requester.id, status=status,
        reason="依据", matrix_role_code=RoleCode.REGIONAL_MANAGER.value,
        request_payload=payload, dataset_type=dataset_type,
        dataset_id=dataset_id or w["ds"])
    session.add(ap)
    session.flush()
    return ap


def _voucher(session, w, owner, *, biz_type="PROOF_DOC"):
    f = FileObject(
        object_key=f"PROOF_DOC/{uuid4().hex}/invoice-{uuid4().hex[:6]}.jpg",
        original_filename="invoice.jpg", content_type="image/jpeg",
        size_bytes=1024, sha256=uuid4().hex * 2, biz_type=biz_type,
        owner_user_id=owner.id, scan_status="SKIPPED",
        dataset_type="PRODUCTION", dataset_id=w["ds"])
    session.add(f)
    session.flush()
    return f


# ========================= Q109 工资构成精确 =========================


def test_q109_payroll_composition_exact(session: Session) -> None:
    c = _setup(session)
    w, rep, mgr_a = c["w"], c["rep"], c["mgr_a"]
    rule = _rule(session, w, c["mgr"], c["fin"])
    # T19 绩效净额 = 360 元
    snap = _reviewed_snapshot(session, w, rule, rep, c["fin"], net_fen=36_000)
    # 奖金 200 元（已审批）
    bonus = _approval(session, w, c["mgr"], action="payroll.bonus",
                      user_id=rep.id, amount_fen=20_000)
    penalty = _approval(session, w, c["mgr"], action="payroll.penalty",
                        user_id=rep.id, amount_fen=-5_000)

    svc = PayrollService(session)
    st = svc.generate_statement(
        mgr_a, user_id=rep.id, period_month=MONTH, base_salary_fen=500_000,
        adjustments=[
            {"kind": "BONUS", "amount_fen": 20_000, "approval_id": bonus.id,
             "reason": "季度奖金"},
            {"kind": "PENALTY", "amount_fen": -5_000,
             "approval_id": penalty.id, "reason": "迟到罚扣"},
        ])
    detail = svc.get_detail(mgr_a, st.id)

    assert st.base_salary_fen == 500_000
    assert st.performance_fen == 36_000
    assert st.adjustment_fen == 15_000          # +200 - 50
    assert st.gross_pay_fen == 551_000          # 5000 + 360 + 150
    assert st.performance_snapshot_id == snap.id
    types = sorted(ln.line_type for ln in detail["lines"])
    assert types == ["ADJUSTMENT", "ADJUSTMENT", "BASE_SALARY", "PERFORMANCE"]
    # 整数分，无 float
    assert isinstance(st.gross_pay_fen, int)
    # 生成幂等：再生成返回同一单，不重算
    again = svc.generate_statement(
        mgr_a, user_id=rep.id, period_month=MONTH, base_salary_fen=999_999)
    assert again.id == st.id
    assert again.gross_pay_fen == 551_000


def test_t35_snapshot_auto_taken_even_not_reviewed(session: Session) -> None:
    """T35：工资草稿直接取当月最新快照（含未复核），无需先走财务复核。"""
    c = _setup(session)
    w, rep, mgr_a = c["w"], c["rep"], c["mgr_a"]
    rule = _rule(session, w, c["mgr"], c["fin"])
    snap = _reviewed_snapshot(session, w, rule, rep, c["fin"], net_fen=1000)
    snap.status = "CONFIRMED"  # 经理确认但财务未复核
    session.flush()

    st = PayrollService(session).generate_statement(
        mgr_a, user_id=rep.id, period_month=MONTH, base_salary_fen=100_000)
    assert st.performance_snapshot_id == snap.id
    assert st.performance_fen == 1000
    assert st.gross_pay_fen == 101_000
    perf_line = next(ln for ln in PayrollService(session).get_detail(
        mgr_a, st.id)["lines"] if ln.line_type == "PERFORMANCE")
    assert perf_line.basis_json["snapshot_status"] == "CONFIRMED"


# ========================= Q110 系统管理员无发放权 =========================


def test_q110_admin_disburse_forbidden(session: Session) -> None:
    c = _setup(session)
    w, rep, mgr_a, fin_a = c["w"], c["rep"], c["mgr_a"], c["fin_a"]
    rule = _rule(session, w, c["mgr"], c["fin"])
    _reviewed_snapshot(session, w, rule, rep, c["fin"], net_fen=10_000)
    svc = PayrollService(session)
    st = svc.generate_statement(
        mgr_a, user_id=rep.id, period_month=MONTH, base_salary_fen=500_000)
    st = svc.confirm_statement(mgr_a, st.id)
    st = svc.review_statement(fin_a, st.id)

    admin = _user(session, w["ds"], RoleCode.SYSTEM_ADMIN,
                  f"admin20-{uuid4().hex[:4]}")
    _auth(session, admin, w)
    admin_a = _actor(admin, RoleCode.SYSTEM_ADMIN, w)
    with pytest.raises(PermissionDeniedError) as exc:
        svc.mark_paid(admin_a, st.id)
    assert exc.value.code == "PAYROLL_DISBURSE_FORBIDDEN"
    # 业务员同样无权
    with pytest.raises(PermissionDeniedError):
        svc.mark_paid(c["rep_a"], st.id)


# ========================= Q111 财务双人复核 =========================


def test_q111_dual_finance_reviewer_cannot_pay(session: Session) -> None:
    c = _setup(session, dual_finance=True, extra_finance=True)
    w, rep, mgr_a, fin_a, fin2_a = (c["w"], c["rep"], c["mgr_a"],
                                    c["fin_a"], c["fin2_a"])
    rule = _rule(session, w, c["mgr"], c["fin"])
    _reviewed_snapshot(session, w, rule, rep, c["fin"], net_fen=10_000)
    svc = PayrollService(session)
    st = svc.generate_statement(
        mgr_a, user_id=rep.id, period_month=MONTH, base_salary_fen=500_000)
    st = svc.confirm_statement(mgr_a, st.id)
    st = svc.review_statement(fin_a, st.id)   # 财务 A 复核
    # 同一财务 A 发放 → 拒绝
    with pytest.raises(PermissionDeniedError) as exc:
        svc.mark_paid(fin_a, st.id)
    assert exc.value.code == "PAYROLL_DUAL_REVIEW_REQUIRED"
    # 另一名财务 B 发放 → 成功
    st = svc.mark_paid(fin2_a, st.id, paid_on=date(2026, 10, 14))
    assert st.status == "PAID"
    assert st.paid_by_id == c["fin2"].id
    assert st.paid_on == date(2026, 10, 14)


def test_q111_dual_disabled_same_finance_ok(session: Session) -> None:
    # 开关关闭（dev/test 默认）：同一财务可复核并发放
    c = _setup(session, dual_finance=False)
    w, rep, mgr_a, fin_a = c["w"], c["rep"], c["mgr_a"], c["fin_a"]
    rule = _rule(session, w, c["mgr"], c["fin"])
    _reviewed_snapshot(session, w, rule, rep, c["fin"], net_fen=10_000)
    svc = PayrollService(session)
    st = svc.generate_statement(
        mgr_a, user_id=rep.id, period_month=MONTH, base_salary_fen=500_000)
    st = svc.confirm_statement(mgr_a, st.id)
    st = svc.review_statement(fin_a, st.id)
    st = svc.mark_paid(fin_a, st.id)
    assert st.status == "PAID"
    assert st.gross_pay_fen == 510_000


# ========================= Q112 已发放不可改 =========================


def test_q112_paid_statement_immutable(session: Session) -> None:
    c = _setup(session)
    w, rep, mgr_a, fin_a = c["w"], c["rep"], c["mgr_a"], c["fin_a"]
    rule = _rule(session, w, c["mgr"], c["fin"])
    _reviewed_snapshot(session, w, rule, rep, c["fin"], net_fen=10_000)
    svc = PayrollService(session)
    st = svc.generate_statement(
        mgr_a, user_id=rep.id, period_month=MONTH, base_salary_fen=500_000)
    st = svc.confirm_statement(mgr_a, st.id)
    st = svc.review_statement(fin_a, st.id)
    st = svc.mark_paid(fin_a, st.id)
    frozen_gross = st.gross_pay_fen

    bonus = _approval(session, w, c["mgr"], action="payroll.bonus",
                      user_id=rep.id, amount_fen=99_000)
    # 发放后不能再走普通调整
    with pytest.raises(DomainError) as exc:
        svc.add_adjustment(
            mgr_a, st.id, kind="BONUS", amount_fen=99_000,
            approval_id=bonus.id, reason="试图发放后追加")
    assert exc.value.code == "PAYROLL_ADJUST_STATE_INVALID"

    # 发放后不能再次发放 / 复核
    with pytest.raises(DomainError):
        svc.mark_paid(fin_a, st.id)

    # 原四项金额仍固化
    session.refresh(st)
    assert st.gross_pay_fen == frozen_gross == 510_000
    assert st.adjustment_fen == 0


# ========================= Q113 更正单 =========================


def test_q113_correction_after_paid(session: Session) -> None:
    c = _setup(session)
    w, rep, mgr_a, fin_a = c["w"], c["rep"], c["mgr_a"], c["fin_a"]
    rule = _rule(session, w, c["mgr"], c["fin"])
    _reviewed_snapshot(session, w, rule, rep, c["fin"], net_fen=10_000)
    svc = PayrollService(session)
    st = svc.generate_statement(
        mgr_a, user_id=rep.id, period_month=MONTH, base_salary_fen=500_000)
    st = svc.confirm_statement(mgr_a, st.id)
    st = svc.review_statement(fin_a, st.id)
    st = svc.mark_paid(fin_a, st.id)

    cor_bonus = _approval(session, w, c["mgr"], action="payroll.bonus",
                          user_id=rep.id, amount_fen=30_000)
    cor_pen = _approval(session, w, c["mgr"], action="payroll.penalty",
                        user_id=rep.id, amount_fen=-8_000)

    adj1 = svc.apply_correction(
        fin_a, st.id, kind="BONUS", amount_fen=30_000,
        approval_id=cor_bonus.id, reason="补发活动奖")
    assert adj1.is_correction is True
    st = svc.repo.get_statement(st.id)
    assert st.correction_fen == 30_000
    assert st.corrected_pay_fen == 540_000
    # 原应发仍固化
    assert st.gross_pay_fen == 510_000

    adj2 = svc.apply_correction(
        fin_a, st.id, kind="PENALTY", amount_fen=-8_000,
        approval_id=cor_pen.id, reason="多计扣回")
    st = svc.repo.get_statement(st.id)
    assert st.correction_fen == 22_000
    assert st.corrected_pay_fen == 532_000
    assert st.status == "PAID"  # 状态不变，历史单保留

    detail = svc.get_detail(fin_a, st.id)
    corrections = [a for a in detail["adjustments"] if a.is_correction]
    assert {a.id for a in corrections} == {adj1.id, adj2.id}
    # 原构成行（底薪/绩效）数量与金额不变
    base_line = next(ln for ln in detail["lines"]
                     if ln.line_type == "BASE_SALARY")
    assert base_line.amount_fen == 500_000
    # 同一审批依据幂等
    again = svc.apply_correction(
        fin_a, st.id, kind="BONUS", amount_fen=30_000,
        approval_id=cor_bonus.id, reason="补发活动奖")
    assert again.id == adj1.id
    st = svc.repo.get_statement(st.id)
    assert st.correction_fen == 22_000


def test_q113_correction_only_when_paid(session: Session) -> None:
    c = _setup(session)
    w, rep, mgr_a = c["w"], c["rep"], c["mgr_a"]
    rule = _rule(session, w, c["mgr"], c["fin"])
    _reviewed_snapshot(session, w, rule, rep, c["fin"], net_fen=10_000)
    svc = PayrollService(session)
    st = svc.generate_statement(
        mgr_a, user_id=rep.id, period_month=MONTH, base_salary_fen=500_000)
    bonus = _approval(session, w, c["mgr"], action="payroll.bonus",
                      user_id=rep.id, amount_fen=1000)
    with pytest.raises(DomainError) as exc:
        svc.apply_correction(
            mgr_a, st.id, kind="BONUS", amount_fen=1000,
            approval_id=bonus.id, reason="未发放不能更正")
    assert exc.value.code == "PAYROLL_CORRECTION_STATE_INVALID"


# ========================= Q114 未审批费用不进损益 =========================


def test_q114_only_approved_expenses_in_pnl(session: Session) -> None:
    c = _setup(session)
    w, mgr, mgr_a, fin_a = c["w"], c["mgr"], c["mgr_a"], c["fin_a"]
    svc = ExpenseService(session)
    v1 = _voucher(session, w, mgr)
    v2 = _voucher(session, w, mgr)
    v3 = _voucher(session, w, mgr)

    e_approved = svc.create_expense(
        mgr_a, type="FREEZER_PURCHASE", title="购置冰柜",
        amount_fen=200_000, incurred_on=date(2026, 9, 5),
        voucher_file_id=v1.id)
    e_pending = svc.create_expense(
        mgr_a, type="ACTIVITY", title="待审批活动费",
        amount_fen=80_000, incurred_on=date(2026, 9, 6),
        voucher_file_id=v2.id)
    e_rejected = svc.create_expense(
        mgr_a, type="OTHER", title="被驳回费用",
        amount_fen=30_000, incurred_on=date(2026, 9, 7),
        voucher_file_id=v3.id)

    svc.approve_expense(fin_a, e_approved.id)
    svc.reject_expense(fin_a, e_rejected.id, reason="凭证不符")

    rows = svc.approved_expenses_in_month(fin_a, MONTH)
    assert [r.id for r in rows] == [e_approved.id]
    assert svc.approved_expense_total_fen(fin_a, MONTH) == 200_000
    # 状态落库正确
    assert e_approved.status == "APPROVED"
    assert e_pending.status == "PENDING_APPROVAL"
    assert e_rejected.status == "REJECTED"
    # 归属月份由发生日派生
    assert e_approved.period_month == MONTH


def test_q114_expense_requires_voucher(session: Session) -> None:
    c = _setup(session)
    w, rep, mgr_a = c["w"], c["rep"], c["mgr_a"]
    # 非凭证类型文件（照片）不能作为费用凭证
    photo = _voucher(session, w, rep, biz_type="PHOTO")
    with pytest.raises(DomainError) as exc:
        ExpenseService(session).create_expense(
            mgr_a, type="OTHER", title="无有效凭证", amount_fen=1000,
            incurred_on=date(2026, 9, 8), voucher_file_id=photo.id)
    assert exc.value.code == "EXPENSE_VOUCHER_INVALID"


# ========================= 跨数据域隔离 =========================


def test_production_statement_reject_demo_snapshot(session: Session) -> None:
    c = _setup(session)
    rep, mgr_a = c["rep"], c["mgr_a"]
    # 构造一个 DEMO 绩效快照（dataset_type=DEMO，独立 dataset_id）
    demo_ds = uuid4()
    demo_rule = PerformanceRuleVersion(
        code="RULE20D", version=1, name="demo-rule", status="ACTIVE",
        effective_from=date(2026, 1, 1), effective_to=None,
        params_json={"sales_tiers": [[None, 300]]},
        configured_by_id=c["mgr"].id, reviewed_by_id=c["fin"].id,
        dataset_type="DEMO", dataset_id=demo_ds)
    session.add(demo_rule)
    session.flush()
    demo_snap = PerformanceMonthlySnapshot(
        user_id=rep.id, period_month=MONTH,
        period_start=date(2026, 9, 1), period_end=date(2026, 9, 30),
        rule_version_id=demo_rule.id, rule_code=demo_rule.code,
        rule_version=1, net_performance_fen=999_000, status="REVIEWED",
        reviewed_by_id=c["fin"].id, dataset_type="DEMO", dataset_id=demo_ds)
    session.add(demo_snap)
    session.flush()
    with pytest.raises(DomainError) as exc:
        PayrollService(session).generate_statement(
            mgr_a, user_id=rep.id, period_month=MONTH,
            base_salary_fen=100_000,
            performance_snapshot_id=demo_snap.id)
    assert exc.value.code == "PAYROLL_SCOPE_DEMO_MIXED"


def test_adjustment_cross_dataset_approval_rejected(session: Session) -> None:
    c = _setup(session)
    w, rep, mgr_a = c["w"], c["rep"], c["mgr_a"]
    rule = _rule(session, w, c["mgr"], c["fin"])
    _reviewed_snapshot(session, w, rule, rep, c["fin"], net_fen=10_000)
    # 审批单属于其它数据集
    foreign = _approval(session, w, c["mgr"], action="payroll.bonus",
                        user_id=rep.id, amount_fen=5_000,
                        dataset_id=uuid4())
    svc = PayrollService(session)
    st = svc.generate_statement(
        mgr_a, user_id=rep.id, period_month=MONTH, base_salary_fen=500_000)
    with pytest.raises(DomainError) as exc:
        svc.add_adjustment(mgr_a, st.id, kind="BONUS", amount_fen=5_000,
                           approval_id=foreign.id, reason="跨域奖金")
    assert exc.value.code == "PAYROLL_SCOPE_DEMO_MIXED"


def test_adjustment_sign_and_basis_enforced(session: Session) -> None:
    c = _setup(session)
    w, rep, mgr_a = c["w"], c["rep"], c["mgr_a"]
    rule = _rule(session, w, c["mgr"], c["fin"])
    _reviewed_snapshot(session, w, rule, rep, c["fin"], net_fen=10_000)
    svc = PayrollService(session)
    st = svc.generate_statement(
        mgr_a, user_id=rep.id, period_month=MONTH, base_salary_fen=500_000)
    # BONUS 不能为负
    bad = _approval(session, w, c["mgr"], action="payroll.bonus",
                    user_id=rep.id, amount_fen=5_000)
    with pytest.raises(DomainError) as exc:
        svc.add_adjustment(mgr_a, st.id, kind="BONUS", amount_fen=-5_000,
                           approval_id=bad.id, reason="负奖金")
    assert exc.value.code == "PAYROLL_ADJUST_SIGN_INVALID"
    # 未通过审批（PENDING）不可作为依据
    pending = _approval(session, w, c["mgr"], action="payroll.bonus",
                        user_id=rep.id, amount_fen=5_000, status="PENDING")
    with pytest.raises(DomainError) as exc:
        svc.add_adjustment(mgr_a, st.id, kind="BONUS", amount_fen=5_000,
                           approval_id=pending.id, reason="待审批")
    assert exc.value.code == "PAYROLL_ADJUST_APPROVAL_INVALID"


# ========================= Q191 财务工资发放列表 =========================


def test_q191_list_statements_finance_only_with_filters(session: Session) -> None:
    """财务可按月份/状态查工资单列表（含员工姓名）；经理无工资能力不可查。"""
    c = _setup(session)
    w, rep, mgr_a, fin_a = c["w"], c["rep"], c["mgr_a"], c["fin_a"]
    rule = _rule(session, w, c["mgr"], c["fin"])
    _reviewed_snapshot(session, w, rule, rep, c["fin"], net_fen=10_000)
    svc = PayrollService(session)
    st = svc.generate_statement(
        mgr_a, user_id=rep.id, period_month=MONTH, base_salary_fen=500_000)
    svc.confirm_statement(mgr_a, st.id)

    rows, total = svc.list_statements(fin_a, period_month=MONTH)
    assert total == 1
    assert rows[0]["statement"].id == st.id
    assert rows[0]["user_name"] == rep.display_name

    # 状态筛选
    rows, total = svc.list_statements(fin_a, period_month=MONTH, status="PAID")
    assert total == 0

    # 经理无 payroll 能力：不能看工资发放列表
    with pytest.raises(PermissionDeniedError) as exc:
        svc.list_statements(mgr_a, period_month=MONTH)
    assert exc.value.code == "PAYROLL_FINANCE_ONLY"
