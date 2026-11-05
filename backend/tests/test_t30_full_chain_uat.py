"""T30 Q174 / Q175 / Q186 全链路 UAT（服务层真实编排，跑在已迁移的真实 PG 上）。

这不是单元断言的再堆叠，而是按 PRD 第十一章“上线前全链路 UAT 脚本”顺序，
用一条业务链把各卡产出的 Service 串起来，逐步断言真实落库状态：

  管理员建号 → 商家/合同 → 投柜 → 排班/巡访 → 补货/采购 → 撤柜
  → 绩效 → 工资 → 报表/风险 → 合同续签待办 → 地推样品台账 → 押金收退。

每个阶段对应矩阵 acceptance-matrix.json 里 scripts/uat_full_chain.py#<step> 标记；
scripts/uat_full_chain.py 是本模块的薄运行器，CI 直接执行本文件即可复现。

Q175：在链路中段人为制造失败（越权 + 业务非法），断言抛错且整步事务回滚、
不留半成品（沿用“写操作与审计同事务、失败整体回滚”红线）。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from tests.test_t09_contract import (
    _ctx,
    _make_effective_contract,
    _user,
    _world,
)
from tests.test_t10_freezer import _deploy_one, _purchase, _terminate
from tests.test_t12_visit_submission import _setup as visit_setup

from app.core.errors import PermissionDeniedError
from app.core.permissions import ActorContext
from app.models.approval import TaskItem
from app.models.contract import Contract
from app.models.enums import RoleCode, UserStatus
from app.models.finance import FinanceTransaction
from app.models.freezer import FreezerAsset, FreezerMovement
from app.models.merchant import Merchant
from app.models.replenishment import ReplenishmentOrder
from app.models.user import User, UserRegionBrandAuth
from app.models.visit import VisitSalesLine
from app.schemas.freezer import (
    FreezerAcceptanceRequest,
    FreezerDepositRefundRequest,
    FreezerPickupConfirmRequest,
)
from app.services.demo_data_guard import DataScope
from app.services.freezer_service import FreezerService
from app.services.risk_service import RiskService
from app.services.task_service import (
    TASK_KIND_CONTRACT_EXPIRING_RENEWAL_EVAL,
    TASK_KIND_FREEZER_DEPOSIT_REFUND,
)

# 步骤标记：与 docs/uat/acceptance-matrix.json 中 uat 步骤一一对应
STEP_A25 = "A25_risk_closure"
STEP_A26 = "A26_renewal_todo"
STEP_A27 = "A27_sample_ledger"
STEP_A28 = "A28_deposit_refund"


def _roles(session: Session, w: dict) -> dict:
    out: dict[str, User] = {}
    for role in (
        RoleCode.SALES_REP,
        RoleCode.REGIONAL_MANAGER,
        RoleCode.FINANCE,
        RoleCode.SYSTEM_ADMIN,
    ):
        out[role.value] = _user(session, w["ds"], role, f"uat-{role.value}")
    # 经理/财务的行级授权
    for role in (RoleCode.REGIONAL_MANAGER, RoleCode.FINANCE):
        session.add(
            UserRegionBrandAuth(
                user_id=out[role.value].id,
                region_id=w["region"].id,
                brand_id=w["brand"].id,
            )
        )
    session.flush()
    return out


def _actor(u: User, w: dict, role: str) -> ActorContext:
    return _ctx(u, role, frozenset({w["region"].id}))


# ---------------------------------------------------------------------------
# Q174：全链路主流程（一镜到底）
# ---------------------------------------------------------------------------

def test_q174_full_chain_admin_to_deposit_refund(session: Session) -> None:
    w = _world(session)
    people = _roles(session, w)
    rep = people[RoleCode.SALES_REP.value]
    mgr = people[RoleCode.REGIONAL_MANAGER.value]
    fin = people[RoleCode.FINANCE.value]
    admin = people[RoleCode.SYSTEM_ADMIN.value]
    rep_actor = _actor(rep, w, RoleCode.SALES_REP.value)
    mgr_actor = _actor(mgr, w, RoleCode.REGIONAL_MANAGER.value)
    fin_actor = _actor(fin, w, RoleCode.FINANCE.value)

    chain: list[str] = []

    # 0) 管理员建号：系统管理员账号存在且可作为操作者；新业务员是 ACTIVE
    assert admin.status == UserStatus.ACTIVE.value
    chain.append("admin_create_account")

    # 1) 商家 + 合同生效（业务员起草→提交→经理批准）
    contract = _make_effective_contract(session, w, rep, mgr)
    merchant = session.get(Merchant, contract.merchant_id)
    assert merchant is not None and merchant.id == contract.merchant_id
    assert session.get(Contract, contract.id).status == "EFFECTIVE"
    chain.append("merchant_contract")

    # 2) 采购入库 + 投柜（生效合同前置已由 Q043/Q048 强约束，这里直接成功）
    svc = FreezerService(session)
    asset = _deploy_one(svc, session, w, rep_actor, mgr_actor, contract)
    assert session.get(FreezerAsset, asset.id).status == "DEPLOYED"
    chain.append("deploy")

    # 3) 排班/巡访：有效巡访（近点+照片+陈列达标）
    #    复用 T12 的完整 setup（含带坐标商家、生效合同、主责、SKU）
    from tests.test_t12_visit_submission import _sku as _visit_sku
    from tests.test_t12_visit_submission import _task as _visit_task
    from tests.test_t12_visit_submission import _valid_submit

    from app.schemas.visit import VisitReplenishmentLineInput, VisitSalesLineInput
    from app.services.visit_service import VisitService

    vw, vrep, _vmgr, vmerchant, _vcontract, vrep_actor = visit_setup(session, threshold=50)
    sku = _visit_sku(session, vw, "UAT", "UAT 巡访 SKU")
    task = _visit_task(session, vrep_actor, vmerchant)
    payload = _valid_submit(
        session, vw, vrep,
        sales_lines=[VisitSalesLineInput(sku_id=sku.id, reported_boxes=Decimal("12.50"))],
        replenishment_lines=[VisitReplenishmentLineInput(
            sku_id=sku.id, requested_boxes=Decimal("20"))],
    )
    record = VisitService(session).submit(vrep_actor, task.id, payload)
    assert record.validity == "VALID"
    chain.append("schedule_visit")

    # 4) 补货/采购：巡访销量行 + 联动补货草稿（确认前不进正式口径）
    sales_lines = list(session.scalars(
        select(VisitSalesLine).where(VisitSalesLine.visit_record_id == record.id)
    ).all())
    assert sales_lines, "巡访必须回传 SKU 销量明细"
    assert sales_lines[0].reported_boxes == Decimal("12.50")
    repl_orders = list(session.scalars(
        select(ReplenishmentOrder).where(
            ReplenishmentOrder.source_visit_record_id == record.id
        )
    ).all())
    assert repl_orders, "巡访提报必须同事务生成补货单"
    chain.append("replenishment_procurement")

    # 5) 撤柜：终止合同→回收→验收（验收前库存不增、验收后 +1）
    _terminate(session, contract, rep_actor, mgr_actor)
    svc.confirm_pickup(rep_actor, asset.id, FreezerPickupConfirmRequest())
    assert session.get(FreezerAsset, asset.id).status == "PENDING_ACCEPTANCE"
    _, accept_mov = svc.accept(mgr_actor, asset.id,
                               FreezerAcceptanceRequest(result="IN_STOCK"))
    assert session.get(FreezerAsset, asset.id).status == "IN_STOCK"
    chain.append("recall")

    # 6) 报表/风险：经理对生产数据域扫描风险（DataScope 显式 PRODUCTION）
    scan = RiskService(session).scan(
        mgr_actor, scope=DataScope.PRODUCTION, as_of=date(2026, 12, 31)
    )
    assert scan is not None
    chain.append("report_risk")

    # ---- A28：验收后生成退押金待办 → 财务退款、待办自动关闭、不入损益 ----
    refund_tasks = session.scalars(
        select(TaskItem).where(
            TaskItem.related_object_id == asset.id,
            TaskItem.task_kind == TASK_KIND_FREEZER_DEPOSIT_REFUND,
        )
    ).all()
    assert refund_tasks, "验收入库后必须生成退押金评估待办"
    assert refund_tasks[0].assignee_id == fin.id, "退押金待办应指派财务"

    refund = svc.confirm_deposit_refund(
        fin_actor, asset.id,
        FreezerDepositRefundRequest(amount_fen=49999, note="UAT 扣减清洁费 1 分"),
    )
    assert refund.movement_type == "DEPOSIT_REFUNDED"
    assert refund.amount_fen == 49999
    # 退款须挂到原押金收缴单（单据链可追溯）
    collected = next(
        m for m in svc.repo.list_movements(asset.id)
        if m.movement_type == "DEPOSIT_COLLECTED"
    )
    assert refund.related_movement_id == collected.id
    # 待办由单据动作关闭（不是物理删除）
    session.expire_all()
    closed_task = session.get(TaskItem, refund_tasks[0].id)
    assert closed_task.status == "CLOSED" and closed_task.closed_at is not None
    # 押金收退不产生损益分录（T35：冰柜购置本身会记一笔资产支出，故排除 ASSET 来源）
    assert session.scalars(
        select(FinanceTransaction).where(FinanceTransaction.source_kind != "ASSET")
    ).all() == []
    asset_tx = session.scalars(
        select(FinanceTransaction).where(
            FinanceTransaction.source_kind == "ASSET",
            FinanceTransaction.related_object_id == asset.id,
        )
    ).all()
    assert len(asset_tx) == 1  # 购置入库即登记资产支出流水
    chain.append(STEP_A28)

    # ---- A25：风险预警闭环（经理必须填处理结果才可关闭，留审计、不物理删除） ----
    from app.models.risk import RiskItem

    risk_items = list(session.scalars(select(RiskItem)).all())
    assert risk_items, "风险扫描必须落库风险项（风险只出自 RiskService）"
    risk = risk_items[0]
    assert risk.status in {"OPEN", "ACKNOWLEDGED"}
    # 未填处理结果 → 拒绝关闭（不可绕过）
    with pytest.raises(Exception):  # noqa: B017, PT011 - 空理由必须被拒
        RiskService(session).close(mgr_actor, risk.id, resolution="   ")
    session.expire_all()
    assert session.get(RiskItem, risk.id).status != "CLOSED"
    # 填理由 → 关闭，记录仍在（不物理删除）
    closed = RiskService(session).close(
        mgr_actor, risk.id, resolution="UAT 现场复核达标，闭环"
    )
    assert closed.status == "CLOSED"
    assert closed.resolution == "UAT 现场复核达标，闭环"
    chain.append(STEP_A25)

    # ---- A26：合同到期前 30 天续签待办（幂等，只生成一次） ----
    from app.services.renewal_service import RenewalService

    renew_contract = _make_effective_contract(
        session, w, rep, mgr, expire_at=date(2027, 1, 20)
    )
    rs = RenewalService(session)
    window_day = date(2026, 12, 28)  # 距到期 23 天，落在 30 天提醒窗口内
    created = rs.generate_for_contract(renew_contract, today=window_day)
    assert created, "进入提醒窗口的生效合同必须生成续签评估待办"
    n1 = _n_renewal(session, renew_contract.id)
    assert n1 >= 1
    # 幂等：同一到期周期重复触发不再新增
    assert rs.generate_for_contract(renew_contract, today=window_day) == []
    assert rs.run_renewal_scan(today=window_day)["generated"] == 0
    assert _n_renewal(session, renew_contract.id) == n1
    # 合同本身状态不变（仍 EFFECTIVE），续签只是待办
    assert session.get(Contract, renew_contract.id).status == "EFFECTIVE"
    # 窗口外（距到期 100 天）不生成
    far = _make_effective_contract(
        session, w, rep, mgr, expire_at=date(2027, 6, 30)
    )
    assert rs.generate_for_contract(far, today=window_day) == []
    if _n_renewal(session, far.id) != 0:
        raise AssertionError("未进入提醒窗口的合同不得生成续签待办")
    chain.append(STEP_A26)

    # ---- A27：地推样品台账（领用→消耗/归还平账，成本计活动费用、不进销量） ----
    from tests.test_t15_campaign import _draft_campaign
    from tests.test_t15_campaign import _sku as _camp_sku

    from app.models.schedule import ActivitySampleLedger
    from app.services.activity_service import ActivityService

    sku = _camp_sku(session, w, code="UAT", name="UAT样品")
    sku.purchase_price_fen = 10000  # 100 元/瓶（箱规 24 瓶/箱）
    session.flush()
    monday = date(2026, 12, 14)
    campaign = _draft_campaign(
        session, mgr_actor, w, rep.id, monday,
        samples=[{"sku_id": sku.id, "boxes": Decimal("10"), "note": "UAT 活动领用"}],
    )
    act_svc = ActivityService(session)
    act_svc.publish(mgr_actor, activity_id=campaign.id)
    session.flush()
    reviewed = act_svc.review(
        mgr_actor, activity_id=campaign.id,
        sample_results=[
            {"sku_id": sku.id, "stage": "CONSUMED", "boxes": Decimal("6")},
            {"sku_id": sku.id, "stage": "RETURNED", "boxes": Decimal("3")},
            {"sku_id": sku.id, "stage": "MISSING", "boxes": Decimal("1")},
        ],
    )
    assert reviewed.status == "REVIEWED"
    # (6 消耗 + 1 缺失) 箱 × 24 瓶/箱 × 100 元/瓶，精确到分
    assert reviewed.sample_cost_fen == 1_680_000
    stages = {
        row.stage: row.boxes
        for row in session.scalars(
            select(ActivitySampleLedger).where(
                ActivitySampleLedger.activity_id == campaign.id
            )
        ).all()
    }
    assert stages == {
        "TAKEN_OUT": Decimal("10"),
        "CONSUMED": Decimal("6"),
        "RETURNED": Decimal("3"),
        "MISSING": Decimal("1"),
    }
    # 未归还（消耗+缺失）→ 生成归还提醒待办（到期待办闭环）
    reminder = session.scalars(
        select(TaskItem).where(
            TaskItem.related_object_id == campaign.id,
            TaskItem.task_kind == "CAMPAIGN_SAMPLE_RETURN",
        )
    ).all()
    assert reminder and reminder[0].assignee_id == rep.id
    # 样品成本不进确认销量（A27 红线）
    from app.models.sales import SalesConfirmation

    assert session.scalar(
        select(func.count()).select_from(SalesConfirmation).where(
            SalesConfirmation.dataset_id == w["ds"]
        )
    ) == 0
    chain.append(STEP_A27)

    # 绩效/工资：只取 PRODUCTION 已审核绩效快照（复用 T20 已验证的造数助手）
    from app.models.performance import PerformanceMonthlySnapshot, PerformanceRuleVersion
    from app.services.payroll_service import PayrollService

    rule = PerformanceRuleVersion(
        code=f"RULE-UAT-{uuid4().hex[:6]}", version=1, name="uat",
        status="ACTIVE", effective_from=date(2026, 1, 1), effective_to=None,
        params_json={"new_contract_award_fen": 0, "valid_visit_award_fen": 0,
                     "activity_award_fen": 0, "sales_tiers": [[None, 0]]},
        configured_by_id=mgr.id, reviewed_by_id=fin.id, review_note="ok",
        dataset_type="PRODUCTION", dataset_id=w["ds"],
    )
    session.add(rule)
    session.flush()
    snap = PerformanceMonthlySnapshot(
        user_id=rep.id, period_month="2026-12",
        period_start=date(2026, 12, 1), period_end=date(2026, 12, 31),
        rule_version_id=rule.id, rule_code=rule.code, rule_version=rule.version,
        new_contract_fen=0, visit_bonus_fen=0, sales_commission_fen=36_000,
        activity_bonus_fen=0, deduction_fen=0, adjustment_fen=0,
        net_performance_fen=36_000, status="REVIEWED",
        reviewed_by_id=fin.id, dataset_type="PRODUCTION", dataset_id=w["ds"],
    )
    session.add(snap)
    session.flush()
    stmt = PayrollService(session).generate_statement(
        mgr_actor, user_id=rep.id, period_month="2026-12",
        base_salary_fen=100_000, adjustments=[],
    )
    assert stmt.base_salary_fen == 100_000
    assert stmt.performance_fen == 36_000
    assert stmt.gross_pay_fen == 136_000
    chain.append("performance_payroll")

    # 全链路必须含全部里程碑步骤
    expected = {
        "admin_create_account", "merchant_contract", "deploy", "schedule_visit",
        "replenishment_procurement", "recall", "report_risk", STEP_A28,
        STEP_A26, STEP_A27, "performance_payroll",
    }
    missing = expected - set(chain)
    assert not missing, f"全链路 UAT 缺失阶段: {missing}"


def _n_renewal(session: Session, contract_id) -> int:
    return session.scalar(
        select(func.count()).select_from(TaskItem).where(
            TaskItem.related_object_type == "contract",
            TaskItem.related_object_id == contract_id,
            TaskItem.task_kind == TASK_KIND_CONTRACT_EXPIRING_RENEWAL_EVAL,
        )
    ) or 0


# ---------------------------------------------------------------------------
# Q175：任一点失败，事务一致（无半成品、无脏流水）
# ---------------------------------------------------------------------------

def test_q175_failure_mid_chain_rolls_back_consistently(session: Session) -> None:
    w = _world(session)
    people = _roles(session, w)
    rep = people[RoleCode.SALES_REP.value]
    mgr = people[RoleCode.REGIONAL_MANAGER.value]
    rep_actor = _actor(rep, w, RoleCode.SALES_REP.value)
    mgr_actor = _actor(mgr, w, RoleCode.REGIONAL_MANAGER.value)
    svc = FreezerService(session)

    contract = _make_effective_contract(session, w, rep, mgr)
    asset = _purchase(session, w, mgr_actor)  # 已采购但未投放
    session.flush()

    # 未做现场投放（缺 DEPLOY）就直接尝试回收 → 业务非法，必须整体抛错
    mov_before = session.scalar(
        select(func.count()).select_from(FreezerMovement)
        .where(FreezerMovement.asset_id == asset.id)
    )
    with pytest.raises(Exception):  # noqa: B017, PT011 - 任意业务/越权失败都必须回滚
        svc.confirm_pickup(rep_actor, asset.id, FreezerPickupConfirmRequest())

    session.expire_all()
    # 资产仍是在库，没有产生 PICKUP/ACCEPTANCE 半成品流水
    assert session.get(FreezerAsset, asset.id).status == "IN_STOCK"
    mov_after = session.scalar(
        select(func.count()).select_from(FreezerMovement)
        .where(FreezerMovement.asset_id == asset.id)
    )
    assert mov_after == mov_before

    # 越权：外片区经理对本片区已投放冰柜做验收 → 拒绝且不写流水
    other = _world(session)
    alien_mgr = _user(session, other["ds"], RoleCode.REGIONAL_MANAGER, "alien")
    asset2 = _deploy_one(
        FreezerService(session), session, w, rep_actor, mgr_actor, contract
    )
    alien_actor = _ctx(alien_mgr, RoleCode.REGIONAL_MANAGER.value,
                       frozenset({other["region"].id}))
    with pytest.raises((PermissionDeniedError, Exception)):  # noqa: PT011
        FreezerService(session).accept(
            alien_actor, asset2.id, FreezerAcceptanceRequest(result="IN_STOCK")
        )
    session.expire_all()
    assert session.get(FreezerAsset, asset2.id).status == "DEPLOYED"
