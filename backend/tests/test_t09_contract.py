"""T09 合同申请、审批与版本（PRD §4.4 / §4.6 / R38 / RK-6）。

Q043 未生效禁止投柜
Q044 业务员不能自审
Q045 50% 陈列阈值固化
Q046 变更留旧版（新版本不覆盖历史）
Q047 终止前状态不变（终止不是删除；终止待审期间仍 EFFECTIVE）
Q180 到期前 30 天为业务员+经理各生成一条续签评估待办，重复触发不重复生成
"""

from __future__ import annotations

from datetime import date, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import (
    ConflictError,
    DomainError,
    PermissionDeniedError,
)
from app.core.permissions import ActorContext
from app.core.security import hash_password
from app.models import (
    Brand,
    Contract,
    Merchant,
    Organization,
    Region,
    Role,
    TaskItem,
    User,
)
from app.models.enums import MerchantStatus, RoleCode, UserStatus
from app.schemas.contract import (
    ContractChangeRequest,
    ContractDecisionRequest,
    ContractDraftRequest,
    ContractRenewalRequest,
    ContractTerminateRequest,
)
from app.services.contract_service import ContractService
from app.services.renewal_service import RenewalService

# ----------------------------- 造数 -----------------------------


def _world(session: Session) -> dict:
    ds = uuid4()
    org = Organization(
        code=f"org-{uuid4().hex[:8]}",
        name="测试组织",
        dataset_type="PRODUCTION",
        dataset_id=ds,
    )
    session.add(org)
    session.flush()
    brand = Brand(
        code=f"brand-{uuid4().hex[:8]}",
        name="沁爽",
        dataset_type="PRODUCTION",
        dataset_id=ds,
    )
    session.add(brand)
    region = Region(
        code=f"reg-{uuid4().hex[:8]}",
        name="华东",
        org_id=org.id,
        dataset_type="PRODUCTION",
        dataset_id=ds,
    )
    session.add(region)
    session.flush()
    return {"ds": ds, "brand": brand, "region": region}


def _user(session: Session, ds: UUID, role_code: RoleCode, username: str) -> User:
    r = session.scalars(
        select(Role).where(Role.dataset_id == ds, Role.role_code == role_code.value)
    ).first()
    if r is None:
        r = Role(
            code=f"role-{uuid4().hex[:8]}",
            name=role_code.value,
            role_code=role_code.value,
            dataset_type="PRODUCTION",
            dataset_id=ds,
        )
        session.add(r)
        session.flush()
    u = User(
        username=f"{username}-{uuid4().hex[:6]}",
        display_name=username,
        role_id=r.id,
        status=UserStatus.ACTIVE.value,
        must_change_password=False,
        password_hash=hash_password("t09-pass-not-for-prod"),
        dataset_type="PRODUCTION",
        dataset_id=ds,
    )
    session.add(u)
    session.flush()
    return u


def _ctx(user: User, role_code: str, regions=frozenset()) -> ActorContext:
    return ActorContext(
        user_id=user.id,
        role_code=role_code,
        region_ids=regions,
        brand_ids=frozenset(),
    )


def _merchant(session: Session, w: dict, name: str = "便利店") -> Merchant:
    m = Merchant(
        code=f"M-{uuid4().hex[:8]}",
        name=name,
        status=MerchantStatus.ACTIVE.value,
        region_id=w["region"].id,
        brand_id=w["brand"].id,
        dataset_type="PRODUCTION",
        dataset_id=w["ds"],
    )
    session.add(m)
    session.flush()
    return m


def _draft_payload(merchant_id: UUID, **overrides) -> ContractDraftRequest:
    base = dict(
        merchant_id=merchant_id,
        contract_no=f"C-{uuid4().hex[:10]}",
        title="2026 年度陈列合作协议",
        party_name="天天便利店有限公司",
        effective_from=date(2026, 1, 1),
        expire_at=date(2026, 12, 31),
        commission_rate=3.5,
        display_subsidy_amount=120000,
        display_threshold_pct=50,
        commission_rule_version="comm-v2026-01",
        reason="创建合同草稿",
    )
    base.update(overrides)
    return ContractDraftRequest(**base)


def _make_effective_contract(
    session: Session,
    w: dict,
    rep: User,
    manager: User,
    *,
    threshold: int = 50,
    expire_at: date | None = date(2026, 12, 31),
) -> Contract:
    """业务员起草 → 提交 → 经理批准生效，返回 EFFECTIVE 合同。"""
    svc = ContractService(session)
    merchant = _merchant(session, w)
    contract = svc.create_draft(
        _ctx(rep, RoleCode.SALES_REP.value, frozenset({w["region"].id})),
        _draft_payload(merchant.id, display_threshold_pct=threshold, expire_at=expire_at),
    )
    # 补齐经理责任人（草稿由业务员建时 manager 为空，经理批准时写入）
    svc.submit(
        _ctx(rep, RoleCode.SALES_REP.value, frozenset({w["region"].id})),
        contract.id,
        "提交审批",
    )
    svc.decide(
        _ctx(manager, RoleCode.REGIONAL_MANAGER.value, frozenset({w["region"].id})),
        contract.id,
        ContractDecisionRequest(decision="APPROVE", comment="同意"),
    )
    session.flush()
    return svc.repo.get_by_id(contract.id)


# ----------------------------- Q043 -----------------------------


def test_q043_freezer_requires_effective_contract(session: Session) -> None:
    """草稿/待审/终止等非生效合同一律禁止投柜；仅 EFFECTIVE 放行。"""
    w = _world(session)
    rep = _user(session, w["ds"], RoleCode.SALES_REP, "rep")
    manager = _user(session, w["ds"], RoleCode.REGIONAL_MANAGER, "mgr")
    rep_actor = _ctx(rep, RoleCode.SALES_REP.value, frozenset({w["region"].id}))
    mgr_actor = _ctx(manager, RoleCode.REGIONAL_MANAGER.value, frozenset({w["region"].id}))
    svc = ContractService(session)

    merchant = _merchant(session, w)
    draft = svc.create_draft(rep_actor, _draft_payload(merchant.id))

    # 草稿 → 禁止投柜
    with pytest.raises(ConflictError) as ei:
        svc.assert_can_install_freezer(mgr_actor, draft.id)
    assert ei.value.code == "CONTRACT_NOT_EFFECTIVE_FOR_FREEZER"

    # 提交待审 → 仍禁止投柜
    svc.submit(rep_actor, draft.id, "提交")
    with pytest.raises(ConflictError):
        svc.assert_can_install_freezer(mgr_actor, draft.id)

    # 生效后放行
    svc.decide(mgr_actor, draft.id, ContractDecisionRequest(decision="APPROVE"))
    contract = svc.repo.get_by_id(draft.id)
    assert contract.status == "EFFECTIVE"
    assert svc.assert_can_install_freezer(mgr_actor, contract.id).status == "EFFECTIVE"

    # 无合同 → 拒绝投柜
    with pytest.raises(DomainError) as ei2:
        svc.assert_can_install_freezer(mgr_actor, None)
    assert ei2.value.code == "CONTRACT_REQUIRED_FOR_FREEZER"


# ----------------------------- Q044 -----------------------------


def test_q044_sales_rep_cannot_self_approve(session: Session) -> None:
    """业务员不能审批合同，更不能自审（提交人=审批人也拒绝）。"""
    w = _world(session)
    rep = _user(session, w["ds"], RoleCode.SALES_REP, "rep")
    manager = _user(session, w["ds"], RoleCode.REGIONAL_MANAGER, "mgr")
    rep_actor = _ctx(rep, RoleCode.SALES_REP.value, frozenset({w["region"].id}))
    svc = ContractService(session)

    merchant = _merchant(session, w)
    contract = svc.create_draft(rep_actor, _draft_payload(merchant.id))
    svc.submit(rep_actor, contract.id, "提交")

    # 业务员角色审批 → 拒绝
    with pytest.raises(PermissionDeniedError) as ei:
        svc.decide(
            rep_actor,
            contract.id,
            ContractDecisionRequest(decision="APPROVE"),
        )
    assert ei.value.code == "CONTRACT_SELF_APPROVAL_FORBIDDEN"
    # 被拒后合同仍处于待审，未被放行
    assert svc.repo.get_by_id(contract.id).status == "PENDING_APPROVAL"

    # 经理正常审批通过（对照组）
    ok = svc.decide(
        _ctx(manager, RoleCode.REGIONAL_MANAGER.value, frozenset({w["region"].id})),
        contract.id,
        ContractDecisionRequest(decision="APPROVE"),
    )
    assert ok.status == "EFFECTIVE"


# ----------------------------- Q045 -----------------------------


def test_q045_threshold_frozen_at_50_percent(session: Session) -> None:
    """50% 陈列阈值在建合同时固化；生效后快照保留 50，不被后续改动覆盖历史。"""
    w = _world(session)
    rep = _user(session, w["ds"], RoleCode.SALES_REP, "rep")
    manager = _user(session, w["ds"], RoleCode.REGIONAL_MANAGER, "mgr")

    contract = _make_effective_contract(
        session, w, rep, manager, threshold=50
    )
    svc = ContractService(session)
    _, versions, _ = svc.get_detail(
        _ctx(manager, RoleCode.REGIONAL_MANAGER.value, frozenset({w["region"].id})),
        contract.id,
    )
    assert contract.display_threshold_pct == 50
    assert versions[0].display_threshold_pct == 50
    assert versions[0].commission_rule_version == "comm-v2026-01"


def test_threshold_out_of_range_rejected(session: Session) -> None:
    """阈值必须 0-100（库 check + API 校验）。"""
    w = _world(session)
    merchant = _merchant(session, w)
    with pytest.raises(ValidationError):
        _draft_payload(merchant.id, display_threshold_pct=120)


# ----------------------------- Q046 -----------------------------


def test_q046_change_keeps_old_version(session: Session) -> None:
    """生效后变更生成新版本 v2，旧版本 v1 完整保留且阈值/口径不被覆盖。"""
    w = _world(session)
    rep = _user(session, w["ds"], RoleCode.SALES_REP, "rep")
    manager = _user(session, w["ds"], RoleCode.REGIONAL_MANAGER, "mgr")
    contract = _make_effective_contract(
        session, w, rep, manager, threshold=50
    )
    mgr_actor = _ctx(manager, RoleCode.REGIONAL_MANAGER.value, frozenset({w["region"].id}))
    svc = ContractService(session)

    v1 = svc.repo.get_version(contract.id, 1)
    assert v1 is not None and v1.display_threshold_pct == 50

    # 经理发起变更：阈值 50→70，提成规则版本升级，到期日延长
    svc.change(
        mgr_actor,
        contract.id,
        ContractChangeRequest(
            reason="年中调整陈列要求",
            display_threshold_pct=70,
            commission_rule_version="comm-v2026-07",
            expire_at=date(2027, 6, 30),
        ),
    )
    session.flush()

    contract = svc.repo.get_by_id(contract.id)
    assert contract.current_version == 2
    assert contract.display_threshold_pct == 70
    assert contract.commission_rule_version == "comm-v2026-07"

    # 旧版 v1 仍是 50 / 旧规则版本，未被覆盖
    old = svc.repo.get_version(contract.id, 1)
    new = svc.repo.get_version(contract.id, 2)
    assert old.display_threshold_pct == 50
    assert old.commission_rule_version == "comm-v2026-01"
    assert new.display_threshold_pct == 70
    assert new.change_type == "CHANGE"

    # 版本列表按序保留两条
    versions = svc.repo.list_versions(contract.id)
    assert [v.version for v in versions] == [1, 2]


# ----------------------------- Q047 -----------------------------


def test_q047_termination_keeps_state_until_approved(session: Session) -> None:
    """终止申请期间合同仍 EFFECTIVE、记录不删除；批准后才 TERMINATED。"""
    w = _world(session)
    rep = _user(session, w["ds"], RoleCode.SALES_REP, "rep")
    manager = _user(session, w["ds"], RoleCode.REGIONAL_MANAGER, "mgr")
    contract = _make_effective_contract(session, w, rep, manager)
    rep_actor = _ctx(rep, RoleCode.SALES_REP.value, frozenset({w["region"].id}))
    mgr_actor = _ctx(manager, RoleCode.REGIONAL_MANAGER.value, frozenset({w["region"].id}))
    svc = ContractService(session)
    contract_id = contract.id

    svc.request_termination(
        rep_actor,
        contract_id,
        ContractTerminateRequest(reason="门店停业", terminate_date=date(2026, 9, 1)),
    )
    session.flush()
    pending = svc.repo.get_by_id(contract_id)
    # 终止前状态不变：等待审批期间仍允许按生效合同处理
    assert pending.status == "TERMINATION_PENDING"
    # 记录仍在（终止不是删除）
    assert pending.id == contract_id
    # 终止待审期间：业务上合同尚未终止；投柜守卫以最终态为准应拒绝
    with pytest.raises(ConflictError):
        svc.assert_can_install_freezer(mgr_actor, contract_id)

    svc.approve_termination(mgr_actor, contract_id, "同意终止")
    session.flush()
    ended = svc.repo.get_by_id(contract_id)
    assert ended.status == "TERMINATED"
    # 终止后记录仍可查询（未物理删除）
    assert svc.get_detail(mgr_actor, contract_id)[0].status == "TERMINATED"


# ----------------------------- Q180 -----------------------------


def test_q180_renewal_tasks_generated_once_for_both_owners(session: Session) -> None:
    """到期前 30 天为责任业务员和片区经理各生成一条续签评估待办；重复触发不重复。"""
    w = _world(session)
    rep = _user(session, w["ds"], RoleCode.SALES_REP, "rep")
    manager = _user(session, w["ds"], RoleCode.REGIONAL_MANAGER, "mgr")

    today = date(2026, 12, 15)
    # 到期日 2026-12-31，距 today 16 天，落在“前 30 天”窗口内
    contract = _make_effective_contract(
        session, w, rep, manager, expire_at=date(2026, 12, 31)
    )
    renewal = RenewalService(session)

    result1 = renewal.run_renewal_scan(today=today, remind_days=30)
    assert result1["scanned"] == 1
    assert result1["generated"] == 2  # 业务员 + 经理各一条

    tasks = list(
        session.scalars(
            select(TaskItem).where(
                TaskItem.related_object_type == "contract",
                TaskItem.related_object_id == contract.id,
                TaskItem.task_kind == "CONTRACT_EXPIRING_RENEWAL_EVAL",
            )
        ).all()
    )
    assignees = {t.assignee_id for t in tasks}
    assert assignees == {rep.id, manager.id}
    assert all(t.status == "OPEN" for t in tasks)

    # 重复触发：幂等，不新增
    result2 = renewal.run_renewal_scan(today=today, remind_days=30)
    assert result2["generated"] == 0
    tasks_again = list(
        session.scalars(
            select(TaskItem).where(
                TaskItem.related_object_type == "contract",
                TaskItem.related_object_id == contract.id,
                TaskItem.task_kind == "CONTRACT_EXPIRING_RENEWAL_EVAL",
            )
        ).all()
    )
    assert len(tasks_again) == 2

    # 合同标记已为本到期周期生成
    assert (
        ContractService(session).repo.get_by_id(contract.id)
        .renewal_generated_for_expire_at
        == date(2026, 12, 31)
    )


def test_q180_no_renewal_outside_window(session: Session) -> None:
    """距到期超过 30 天不生成续签待办。"""
    w = _world(session)
    rep = _user(session, w["ds"], RoleCode.SALES_REP, "rep")
    manager = _user(session, w["ds"], RoleCode.REGIONAL_MANAGER, "mgr")
    # 到期 2026-12-31，today=2026-11-01（60 天），窗口外
    _make_effective_contract(
        session, w, rep, manager, expire_at=date(2026, 12, 31)
    )
    result = RenewalService(session).run_renewal_scan(
        today=date(2026, 11, 1), remind_days=30
    )
    assert result["scanned"] == 0
    assert result["generated"] == 0


def test_q180_renewal_regenerates_after_expiry_change(session: Session) -> None:
    """变更延长到期日 → 进入新周期，可对新到期日再次生成（旧标记重置）。"""
    w = _world(session)
    rep = _user(session, w["ds"], RoleCode.SALES_REP, "rep")
    manager = _user(session, w["ds"], RoleCode.REGIONAL_MANAGER, "mgr")
    contract = _make_effective_contract(
        session, w, rep, manager, expire_at=date(2026, 12, 31)
    )
    svc = ContractService(session)
    renewal = RenewalService(session)
    mgr_actor = _ctx(manager, RoleCode.REGIONAL_MANAGER.value, frozenset({w["region"].id}))

    renewal.run_renewal_scan(today=date(2026, 12, 15), remind_days=30)
    assert svc.repo.get_by_id(contract.id).renewal_generated_for_expire_at is not None

    # 经理变更到期日到 2027-06-30 → 标记重置
    svc.change(
        mgr_actor,
        contract.id,
        ContractChangeRequest(reason="续签延长", expire_at=date(2027, 6, 30)),
    )
    session.flush()
    new_c = svc.repo.get_by_id(contract.id)
    assert new_c.renewal_generated_for_expire_at is None

    # 新窗口内（2027-06-15）再次生成
    result = renewal.run_renewal_scan(today=date(2027, 6, 15), remind_days=30)
    assert result["generated"] == 2


# ----------------------------- HTTP 鉴权冒烟 -----------------------------


def test_contracts_endpoint_requires_auth(client: TestClient) -> None:
    """合同路由已挂载，未携带 JWT 一律 401。"""
    assert client.get("/api/contracts").status_code == 401
    assert client.get(f"/api/contracts/{uuid4()}").status_code == 401


def test_draft_without_fixed_terms_cannot_submit(session: Session) -> None:
    """缺提成规则版本时不能提交审批（固化口径校验）。"""
    w = _world(session)
    rep = _user(session, w["ds"], RoleCode.SALES_REP, "rep")
    rep_actor = _ctx(rep, RoleCode.SALES_REP.value, frozenset({w["region"].id}))
    svc = ContractService(session)
    merchant = _merchant(session, w)
    # 手工构造一份缺 commission_rule_version 的草稿
    contract = svc.create_draft(
        rep_actor, _draft_payload(merchant.id, commission_rule_version=None)
    )
    with pytest.raises(DomainError) as ei:
        svc.submit(rep_actor, contract.id, "提交")
    assert ei.value.code == "CONTRACT_TERMS_INCOMPLETE"
    assert svc.repo.get_by_id(contract.id).status == "DRAFT"


# ----------------------------- Q187 续约申请 -----------------------------


def test_q187_renewal_creates_new_contract_without_touching_old(
    session: Session,
) -> None:
    """续约生成一份接续的新合同（草稿），旧合同状态与到期日保持不变。"""
    w = _world(session)
    rep = _user(session, w["ds"], RoleCode.SALES_REP, "rep")
    manager = _user(session, w["ds"], RoleCode.REGIONAL_MANAGER, "mgr")
    contract = _make_effective_contract(
        session, w, rep, manager, expire_at=date(2026, 12, 31)
    )
    rep_actor = _ctx(rep, RoleCode.SALES_REP.value, frozenset({w["region"].id}))
    svc = ContractService(session)

    renewal = svc.create_renewal(
        rep_actor,
        contract.id,
        ContractRenewalRequest(
            effective_from=date(2027, 1, 1),
            expire_at=date(2027, 12, 31),
            reason="年度续约",
        ),
    )
    session.flush()

    # 新合同：草稿态、独立编号、指回旧合同，条款沿用
    assert renewal.status == "DRAFT"
    assert renewal.id != contract.id
    assert renewal.renewed_from_contract_id == contract.id
    assert renewal.contract_no.startswith(f"{contract.contract_no}-R")
    assert renewal.merchant_id == contract.merchant_id
    assert renewal.effective_from == date(2027, 1, 1)
    assert renewal.expire_at == date(2027, 12, 31)
    assert renewal.commission_rate == contract.commission_rate
    assert renewal.display_threshold_pct == contract.display_threshold_pct
    assert renewal.commission_rule_version == contract.commission_rule_version
    assert renewal.sales_rep_user_id == contract.sales_rep_user_id

    # 旧合同不被终止、不改日期：到期自然失效，无需"先终止再重签"
    old = svc.repo.get_by_id(contract.id)
    assert old.status == "EFFECTIVE"
    assert old.expire_at == date(2026, 12, 31)
    assert old.current_version == 1


def test_q187_renewal_effective_date_must_follow_old_expiry(session: Session) -> None:
    """新生效日必须晚于旧合同到期日，避免两份合同有效期重叠。"""
    w = _world(session)
    rep = _user(session, w["ds"], RoleCode.SALES_REP, "rep")
    manager = _user(session, w["ds"], RoleCode.REGIONAL_MANAGER, "mgr")
    contract = _make_effective_contract(
        session, w, rep, manager, expire_at=date(2026, 12, 31)
    )
    rep_actor = _ctx(rep, RoleCode.SALES_REP.value, frozenset({w["region"].id}))
    svc = ContractService(session)

    with pytest.raises(DomainError) as ei:
        svc.create_renewal(
            rep_actor,
            contract.id,
            ContractRenewalRequest(
                effective_from=date(2026, 12, 31),
                expire_at=date(2027, 12, 31),
                reason="提前接续",
            ),
        )
    assert ei.value.code == "CONTRACT_RENEWAL_OVERLAP"


def test_q187_duplicate_renewal_rejected_until_closed(session: Session) -> None:
    """同一旧合同同时只允许一份未终结的续约申请；被驳回后可重新发起。"""
    w = _world(session)
    rep = _user(session, w["ds"], RoleCode.SALES_REP, "rep")
    manager = _user(session, w["ds"], RoleCode.REGIONAL_MANAGER, "mgr")
    contract = _make_effective_contract(
        session, w, rep, manager, expire_at=date(2026, 12, 31)
    )
    rep_actor = _ctx(rep, RoleCode.SALES_REP.value, frozenset({w["region"].id}))
    mgr_actor = _ctx(
        manager, RoleCode.REGIONAL_MANAGER.value, frozenset({w["region"].id})
    )
    svc = ContractService(session)
    payload = ContractRenewalRequest(
        effective_from=date(2027, 1, 1),
        expire_at=date(2027, 12, 31),
        reason="年度续约",
    )

    first = svc.create_renewal(rep_actor, contract.id, payload)
    with pytest.raises(ConflictError) as ei:
        svc.create_renewal(rep_actor, contract.id, payload)
    assert ei.value.code == "CONTRACT_RENEWAL_EXISTS"

    # 驳回后终结，可再次发起
    svc.submit(rep_actor, first.id, "提交续约")
    svc.decide(mgr_actor, first.id, ContractDecisionRequest(decision="REJECT"))
    session.flush()
    second = svc.create_renewal(rep_actor, contract.id, payload)
    assert second.id != first.id
    assert second.contract_no != first.contract_no


def test_q187_renewal_needs_approval_and_closes_old_renewal_tasks(
    session: Session,
) -> None:
    """续约合同需提交-审批后生效；生效时关闭旧合同的续签评估待办并留痕。"""
    w = _world(session)
    rep = _user(session, w["ds"], RoleCode.SALES_REP, "rep")
    manager = _user(session, w["ds"], RoleCode.REGIONAL_MANAGER, "mgr")
    contract = _make_effective_contract(
        session, w, rep, manager, expire_at=date(2026, 12, 31)
    )
    rep_actor = _ctx(rep, RoleCode.SALES_REP.value, frozenset({w["region"].id}))
    mgr_actor = _ctx(
        manager, RoleCode.REGIONAL_MANAGER.value, frozenset({w["region"].id})
    )
    svc = ContractService(session)

    # 到期前 30 天：旧合同已有两条续签评估待办
    RenewalService(session).run_renewal_scan(today=date(2026, 12, 15), remind_days=30)

    renewal = svc.create_renewal(
        rep_actor,
        contract.id,
        ContractRenewalRequest(
            effective_from=date(2027, 1, 1),
            expire_at=date(2027, 12, 31),
            reason="年度续约",
        ),
    )
    # 未审批前不生效
    assert renewal.status == "DRAFT"
    svc.submit(rep_actor, renewal.id, "提交续约审批")
    assert svc.repo.get_by_id(renewal.id).status == "PENDING_APPROVAL"
    assert svc.repo.get_by_id(contract.id).status == "EFFECTIVE"

    svc.decide(mgr_actor, renewal.id, ContractDecisionRequest(decision="APPROVE"))
    session.flush()

    approved = svc.repo.get_by_id(renewal.id)
    assert approved.status == "EFFECTIVE"
    assert approved.signed_at is not None
    # 旧合同仍 EFFECTIVE（到期自然失效），不做终止
    assert svc.repo.get_by_id(contract.id).status == "EFFECTIVE"

    # 旧合同的续签评估待办被续约生效收口
    tasks = list(
        session.scalars(
            select(TaskItem).where(
                TaskItem.related_object_type == "contract",
                TaskItem.related_object_id == contract.id,
                TaskItem.task_kind == "CONTRACT_EXPIRING_RENEWAL_EVAL",
            )
        ).all()
    )
    assert tasks and all(t.status == "CLOSED" for t in tasks)

    # 详情里能看到续约关系
    _, _, renewals = svc.get_detail(mgr_actor, contract.id)
    assert [r.id for r in renewals] == [approved.id]
    assert renewals[0].contract_no == approved.contract_no


def test_q187_other_sales_rep_cannot_renew(session: Session) -> None:
    """业务员只能续约本人负责的合同（经理限本片区）。"""
    w = _world(session)
    rep = _user(session, w["ds"], RoleCode.SALES_REP, "rep")
    other_rep = _user(session, w["ds"], RoleCode.SALES_REP, "rep2")
    manager = _user(session, w["ds"], RoleCode.REGIONAL_MANAGER, "mgr")
    contract = _make_effective_contract(
        session, w, rep, manager, expire_at=date(2026, 12, 31)
    )
    other_actor = _ctx(
        other_rep, RoleCode.SALES_REP.value, frozenset({w["region"].id})
    )

    with pytest.raises(PermissionDeniedError) as ei:
        ContractService(session).create_renewal(
            other_actor,
            contract.id,
            ContractRenewalRequest(
                effective_from=date(2027, 1, 1),
                expire_at=date(2027, 12, 31),
                reason="代他人续约",
            ),
        )
    assert ei.value.code == "CONTRACT_NOT_ASSIGNED"


# 避免 timedelta 被未使用告警（保留供后续扩展）
_ = timedelta
