"""合同纸质正文（PRD §4.4 扩展）：模板渲染、草稿生成/修订、版本快照固化。

要点：
- 创建草稿即生成一页 A4 正文，商家店名/店长姓名/当前日期自动填入；
- 店长姓名未填时取商家主联系人；显式传入则覆盖；
- 更新草稿默认按最新条款重渲染，显式提交 body_text 则保留手工修订；
- 审批生效后正文与签署人随版本快照固化，历史不可覆盖。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.permissions import ActorContext
from app.core.security import hash_password
from app.models import Brand, ContractVersion, Merchant, Organization, Region, Role, User
from app.models.enums import MerchantStatus, RoleCode, UserStatus
from app.schemas.contract import ContractDecisionRequest, ContractDraftRequest
from app.services.contract_service import ContractService
from app.services.contract_template import render_contract_body

# ----------------------------- 造数（与 T09 同构） -----------------------------


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
        password_hash=hash_password("cb-pass-not-for-prod"),
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


def _merchant(session: Session, w: dict, name: str = "悦购便利店") -> Merchant:
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
        title="冰柜陈列合作协议",
        party_name="悦购便利店",
        effective_from=date(2026, 10, 16),
        expire_at=date(2027, 4, 16),
        commission_rate=3.5,
        display_threshold_pct=50,
        commission_rule_version="comm-v2026-01",
        reason="创建合同草稿",
    )
    base.update(overrides)
    return ContractDraftRequest(**base)


# ----------------------------- 模板渲染 -----------------------------


def test_render_body_fills_party_manager_and_today() -> None:
    body = render_contract_body(
        contract_no="HT-001",
        party_name="悦购便利店",
        store_manager_name="王店长",
        effective_from=date(2026, 10, 16),
        expire_at=date(2027, 4, 16),
        display_threshold_pct=50,
        commission_rate=Decimal("3.5000"),
        commission_rule_version="comm-v2026-01",
        today=date(2026, 10, 19),
    )

    assert "HT-001" in body
    assert "悦购便利店" in body
    assert "王店长" in body
    assert "2026-10-16" in body
    assert "2027-04-16" in body
    assert "2026-10-19" in body  # 签署日期 = 生成当日
    assert "不低于 50%" in body
    assert "3.5%" in body
    assert "comm-v2026-01" in body
    assert "{{" not in body, "所有占位符必须被替换"


def test_render_body_blanks_for_missing_fields() -> None:
    body = render_contract_body(
        contract_no="HT-002",
        party_name="",
        store_manager_name=None,
        effective_from=None,
        expire_at=None,
        display_threshold_pct=50,
        commission_rate=None,
        commission_rule_version=None,
        today=date(2026, 10, 19),
    )

    assert "＿＿＿＿＿＿" in body, "缺失字段应留下划线供打印后手填"
    assert "{{" not in body


# ----------------------------- 草稿生成 -----------------------------


def test_create_draft_generates_body_and_prefills_manager(session: Session) -> None:
    w = _world(session)
    rep = _user(session, w["ds"], RoleCode.SALES_REP, "rep")
    actor = _ctx(rep, RoleCode.SALES_REP.value, frozenset({w["region"].id}))
    merchant = _merchant(session, w)
    merchant.primary_contact_name = "王店长"
    session.flush()

    c = ContractService(session).create_draft(actor, _draft_payload(merchant.id))

    assert c.store_manager_name == "王店长", "未填店长时应取商家主联系人"
    assert c.body_text is not None
    assert "悦购便利店" in c.body_text
    assert "王店长" in c.body_text
    assert c.contract_no in c.body_text
    assert date.today().isoformat() in c.body_text


def test_create_draft_explicit_manager_overrides_contact(session: Session) -> None:
    w = _world(session)
    rep = _user(session, w["ds"], RoleCode.SALES_REP, "rep")
    actor = _ctx(rep, RoleCode.SALES_REP.value, frozenset({w["region"].id}))
    merchant = _merchant(session, w)
    merchant.primary_contact_name = "王店长"
    session.flush()

    c = ContractService(session).create_draft(
        actor, _draft_payload(merchant.id, store_manager_name="李店长")
    )

    assert c.store_manager_name == "李店长"
    assert "李店长" in (c.body_text or "")
    assert "王店长" not in (c.body_text or "")


def test_update_draft_rerenders_body_when_terms_change(session: Session) -> None:
    w = _world(session)
    rep = _user(session, w["ds"], RoleCode.SALES_REP, "rep")
    actor = _ctx(rep, RoleCode.SALES_REP.value, frozenset({w["region"].id}))
    merchant = _merchant(session, w)
    svc = ContractService(session)
    c = svc.create_draft(actor, _draft_payload(merchant.id))

    updated = svc.update_draft(
        actor,
        c.id,
        _draft_payload(
            merchant.id,
            contract_no=c.contract_no,
            party_name="城西便利店",
            store_manager_name="赵店长",
        ),
    )

    assert "城西便利店" in (updated.body_text or "")
    assert "赵店长" in (updated.body_text or "")
    assert "悦购便利店" not in (updated.body_text or "")


def test_update_draft_keeps_manual_body(session: Session) -> None:
    w = _world(session)
    rep = _user(session, w["ds"], RoleCode.SALES_REP, "rep")
    actor = _ctx(rep, RoleCode.SALES_REP.value, frozenset({w["region"].id}))
    merchant = _merchant(session, w)
    svc = ContractService(session)
    c = svc.create_draft(actor, _draft_payload(merchant.id))

    manual = "手工修订：冰柜用电由乙方承担，其他条款不变。"
    updated = svc.update_draft(
        actor,
        c.id,
        _draft_payload(merchant.id, contract_no=c.contract_no, body_text=manual),
    )

    assert updated.body_text == manual, "显式提交的正文必须原样保留，不得重渲染"


# ----------------------------- 快照固化 -----------------------------


def test_approval_snapshot_freezes_body_and_manager(session: Session) -> None:
    w = _world(session)
    rep = _user(session, w["ds"], RoleCode.SALES_REP, "rep")
    manager = _user(session, w["ds"], RoleCode.REGIONAL_MANAGER, "mgr")
    rep_actor = _ctx(rep, RoleCode.SALES_REP.value, frozenset({w["region"].id}))
    mgr_actor = _ctx(manager, RoleCode.REGIONAL_MANAGER.value, frozenset({w["region"].id}))
    merchant = _merchant(session, w)
    svc = ContractService(session)

    c = svc.create_draft(
        rep_actor, _draft_payload(merchant.id, store_manager_name="王店长")
    )
    svc.submit(rep_actor, c.id, "提交审批")
    svc.decide(mgr_actor, c.id, ContractDecisionRequest(decision="APPROVE", comment="同意"))

    v1 = session.scalars(
        select(ContractVersion).where(
            ContractVersion.contract_id == c.id, ContractVersion.version == 1
        )
    ).one()
    assert v1.store_manager_name == "王店长"
    assert v1.body_text is not None
    assert "悦购便利店" in v1.body_text
    assert v1.body_text == svc.repo.get_by_id(c.id).body_text
