"""T08 商家中心与责任归属（PRD §4.3 / §4.6 / R35）。

Q038 重复商家提示（同片区店名/地址近似 → 非阻断 warning）
Q039 跨片区隔离（片区经理只能读本片区；业务员只能读本人/临时授权商家）
Q040 归属时间区间无重叠（主责任区间相交 → 409；临时授权可并行）
Q041 敏感字段裁剪（厂商/财务/经理对手机号、精确坐标的不同可见性）
Q042 URL 筛选恢复（查询参数 → 规范化 applied_filters 可往返）
商家状态维护（PRD §4.3，本卡新增）：负责业务员改本人商家；片区经理本片区兜底；
     督导/财务/系统管理员只读；状态机推进/相邻回退；终止走取消合作审批；
     置 ACTIVE（合作中）必须有已生效且未到期的合同（R2/§4.3:224 巡访口径）。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import ConflictError, DomainError, PermissionDeniedError
from app.core.permissions import ActorContext
from app.core.security import hash_password
from app.models import (
    AuditLog,
    Brand,
    Contract,
    Merchant,
    MerchantAssignment,
    Organization,
    Region,
    Role,
    User,
)
from app.models.enums import MerchantStatus, RoleCode, UserStatus
from app.schemas.merchant import (
    AssignmentCreateRequest,
    GradeChangeRequest,
    MerchantCreateRequest,
    MerchantListQuery,
    MerchantStatusChangeRequest,
)
from app.services.merchant_service import MerchantService

UTC = UTC


# ----------------------------- 夹具 -----------------------------


def _make_world(session: Session) -> dict:
    """建组织 + 两个片区 + 品牌，供跨片区隔离使用。"""
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
    r1 = Region(
        code=f"reg-{uuid4().hex[:8]}",
        name="华东",
        org_id=org.id,
        dataset_type="PRODUCTION",
        dataset_id=ds,
    )
    r2 = Region(
        code=f"reg-{uuid4().hex[:8]}",
        name="华南",
        org_id=org.id,
        dataset_type="PRODUCTION",
        dataset_id=ds,
    )
    session.add_all([r1, r2])
    session.flush()
    return {"ds": ds, "org": org, "brand": brand, "r1": r1, "r2": r2}


def _make_user(
    session: Session, ds: UUID, role_code: RoleCode, username: str
) -> User:
    # (dataset, role_code) 唯一：同 dataset 同角色复用一条 Role
    r = session.scalars(
        select(Role).where(
            Role.dataset_id == ds,
            Role.role_code == role_code.value,
        )
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
    role_id = r.id
    u = User(
        username=f"{username}-{uuid4().hex[:6]}",
        display_name=username,
        role_id=role_id,
        status=UserStatus.ACTIVE.value,
        must_change_password=False,
        password_hash=hash_password("t08-pass-not-for-prod"),
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


def _add_merchant(
    session: Session,
    ds: UUID,
    region: Region,
    brand: Brand,
    *,
    code: str,
    name: str,
    address: str | None = None,
    phone: str | None = "13800000000",
    grade: str | None = None,
) -> Merchant:
    m = Merchant(
        code=f"{code}-{uuid4().hex[:4]}",
        name=name,
        status=MerchantStatus.ACTIVE.value,
        grade=grade,
        region_id=region.id,
        brand_id=brand.id,
        address=address,
        contact_phone=phone,
        dataset_type="PRODUCTION",
        dataset_id=ds,
    )
    session.add(m)
    session.flush()
    return m


# ----------------------------- Q038 -----------------------------


def test_q038_duplicate_merchant_warning(session: Session) -> None:
    """同片区店名/地址高度相似时建档返回非阻断重复提示，且建档仍成功。"""
    w = _make_world(session)
    mgr = _make_user(session, w["ds"], RoleCode.REGIONAL_MANAGER, "mgr")
    actor = _ctx(mgr, RoleCode.REGIONAL_MANAGER.value, frozenset({w["r1"].id}))

    _add_merchant(
        session, w["ds"], w["r1"], w["brand"],
        code="M-EXIST", name="天天便利店（中山公园店）",
        address="上海市长宁区长宁路1000号",
    )
    session.flush()
    svc = MerchantService(session)

    payload = MerchantCreateRequest(
        code=f"M-NEW-{uuid4().hex[:6]}",
        name="天天便利店中山公园店",
        region_id=w["r1"].id,
        brand_id=w["brand"].id,
        address="上海市长宁区长宁路1000号1层",
    )
    merchant, warnings = svc.create_merchant(actor, payload)
    session.flush()

    assert merchant.id is not None  # 建档成功（非阻断）
    assert len(warnings) >= 1
    assert warnings[0]["name"] == "天天便利店（中山公园店）"
    assert warnings[0]["name_similarity"] > 0.6


def test_q038_no_warning_across_regions(session: Session) -> None:
    """跨片区即使店名相同也不给重复提示（重复判定限同片区）。"""
    w = _make_world(session)
    mgr = _make_user(session, w["ds"], RoleCode.REGIONAL_MANAGER, "mgr")
    actor = _ctx(mgr, RoleCode.REGIONAL_MANAGER.value, frozenset({w["r2"].id}))

    _add_merchant(
        session, w["ds"], w["r1"], w["brand"],
        code="M-R1", name="同名便利店", address="甲地1号",
    )
    session.flush()
    svc = MerchantService(session)
    payload = MerchantCreateRequest(
        code=f"M-R2-{uuid4().hex[:6]}",
        name="同名便利店",
        region_id=w["r2"].id,
        brand_id=w["brand"].id,
        address="乙地2号",
    )
    _, warnings = svc.create_merchant(actor, payload)
    assert warnings == []


# ----------------------------- Q039 -----------------------------


def test_q039_region_manager_isolation(session: Session) -> None:
    """片区经理只能看到/读取本片区商家，越区详情 403。"""
    w = _make_world(session)
    m1 = _add_merchant(session, w["ds"], w["r1"], w["brand"], code="A", name="华东店")
    _add_merchant(session, w["ds"], w["r2"], w["brand"], code="B", name="华南店")
    mgr = _make_user(session, w["ds"], RoleCode.REGIONAL_MANAGER, "mgr")
    actor = _ctx(mgr, RoleCode.REGIONAL_MANAGER.value, frozenset({w["r1"].id}))

    svc = MerchantService(session)
    items, total, _ = svc.list_merchants(actor, MerchantListQuery())
    assert total == 1
    assert items[0].id == m1.id

    # 越区读取详情 → 403
    other = _add_merchant(session, w["ds"], w["r2"], w["brand"], code="C", name="华南店2")
    with pytest.raises(PermissionDeniedError) as ei:
        svc.get_merchant_detail(actor, other.id)
    assert ei.value.code == "MERCHANT_REGION_FORBIDDEN"


def test_q039_sales_rep_only_assigned(session: Session) -> None:
    """业务员列表只含本人主责任/临时授权商家。"""
    w = _make_world(session)
    mine = _add_merchant(session, w["ds"], w["r1"], w["brand"], code="MINE", name="我的店")
    _add_merchant(session, w["ds"], w["r1"], w["brand"], code="OTHER", name="别人的店")

    rep = _make_user(session, w["ds"], RoleCode.SALES_REP, "rep")
    other_rep = _make_user(session, w["ds"], RoleCode.SALES_REP, "rep2")
    now = datetime.now(UTC)
    session.add(
        MerchantAssignment(
            merchant_id=mine.id,
            owner_user_id=rep.id,
            assignment_type="PRIMARY",
            valid_range=MerchantService._range(now - timedelta(days=1), None),
            reason="指派",
            dataset_type="PRODUCTION",
            dataset_id=w["ds"],
        )
    )
    session.flush()
    _ = other_rep

    actor = _ctx(rep, RoleCode.SALES_REP.value, frozenset({w["r1"].id}))
    svc = MerchantService(session)
    items, total, _ = svc.list_merchants(actor, MerchantListQuery())
    assert total == 1
    assert items[0].id == mine.id

    # 写操作越权：操作非本人商家 → 403
    not_mine = _add_merchant(
        session, w["ds"], w["r1"], w["brand"], code="NM", name="非我店"
    )
    with pytest.raises(PermissionDeniedError) as ei:
        svc.assert_can_operate_merchant(actor, not_mine)
    assert ei.value.code == "MERCHANT_NOT_ASSIGNED"


# ----------------------------- Q040 -----------------------------


def test_q040_primary_assignment_no_overlap(session: Session) -> None:
    """同一商家主责任区间相交必须被拒（409）。"""
    w = _make_world(session)
    m = _add_merchant(session, w["ds"], w["r1"], w["brand"], code="X", name="店")
    mgr = _make_user(session, w["ds"], RoleCode.REGIONAL_MANAGER, "mgr")
    u1 = _make_user(session, w["ds"], RoleCode.SALES_REP, "rep1")
    u2 = _make_user(session, w["ds"], RoleCode.SALES_REP, "rep2")
    actor = _ctx(mgr, RoleCode.REGIONAL_MANAGER.value, frozenset({w["r1"].id}))
    svc = MerchantService(session)

    base = datetime(2026, 1, 1, tzinfo=UTC)
    # u1 主责任 [1/1, 6/1)
    svc.assign(
        actor, m.id,
        AssignmentCreateRequest(
            owner_user_id=u1.id, assignment_type="PRIMARY",
            start_at=base, end_at=base + timedelta(days=150), reason="首派",
        ),
    )
    session.flush()
    # u2 主责任与 u1 区间相交 → 拒绝
    with pytest.raises(ConflictError) as ei:
        svc.assign(
            actor, m.id,
            AssignmentCreateRequest(
                owner_user_id=u2.id, assignment_type="PRIMARY",
                start_at=base + timedelta(days=10),
                end_at=base + timedelta(days=200),
                reason="重叠指派",
            ),
        )
    assert ei.value.code == "ASSIGNMENT_OVERLAP"


def test_q040_temporary_assignment_can_overlap(session: Session) -> None:
    """临时授权（TEMPORARY）允许与主责任并行，不受主责任排他限制。"""
    w = _make_world(session)
    m = _add_merchant(session, w["ds"], w["r1"], w["brand"], code="Y", name="店")
    mgr = _make_user(session, w["ds"], RoleCode.REGIONAL_MANAGER, "mgr")
    u1 = _make_user(session, w["ds"], RoleCode.SALES_REP, "rep1")
    temp = _make_user(session, w["ds"], RoleCode.SALES_REP, "temprep")
    actor = _ctx(mgr, RoleCode.REGIONAL_MANAGER.value, frozenset({w["r1"].id}))
    svc = MerchantService(session)

    base = datetime(2026, 1, 1, tzinfo=UTC)
    svc.assign(
        actor, m.id,
        AssignmentCreateRequest(
            owner_user_id=u1.id, assignment_type="PRIMARY",
            start_at=base, end_at=None, reason="主责任",
        ),
    )
    session.flush()
    ta = svc.assign(
        actor, m.id,
        AssignmentCreateRequest(
            owner_user_id=temp.id, assignment_type="TEMPORARY",
            start_at=base + timedelta(days=5),
            end_at=base + timedelta(days=12),
            reason="临时授权",
        ),
    )
    session.flush()
    assert ta.assignment_type == "TEMPORARY"


# ----------------------------- Q041 -----------------------------


def test_q041_sensitive_fields_redaction(session: Session) -> None:
    """联系人/精确坐标按角色裁剪：厂商看不到，经理可见，财务看电话不看坐标。"""
    w = _make_world(session)
    m = _add_merchant(
        session, w["ds"], w["r1"], w["brand"], code="S", name="敏感店",
        phone="13912345678",
    )
    # 给坐标（geography Point, SRID=4326）
    m.location = MerchantService._make_point(121.4737, 31.2304)
    session.flush()

    LAT, LNG = 31.2304, 121.4737
    PHONE = "13912345678"

    # 厂商：电话与精确坐标均裁剪（权限层 SENSITIVE_FIELDS_SUPPLIER 语义）
    phone, lat, lng, c_mask, loc_mask = MerchantService.redact_view_for_role(
        RoleCode.SUPPLIER.value, PHONE, LAT, LNG
    )
    assert c_mask is True and loc_mask is True
    assert PHONE not in (phone or "")
    assert "*" in (phone or "")
    assert lat != pytest.approx(LAT, abs=1e-3)

    # 财务：电话可见（对账需要）、精确坐标裁剪
    phone, lat, lng, c_mask, loc_mask = MerchantService.redact_view_for_role(
        RoleCode.FINANCE.value, PHONE, LAT, LNG
    )
    assert phone == PHONE and c_mask is False
    assert loc_mask is True
    assert lat != pytest.approx(LAT, abs=1e-3)

    # 片区经理：电话与精确坐标均可见（详情接口端到端）
    mgr = _make_user(session, w["ds"], RoleCode.REGIONAL_MANAGER, "mgr")
    m_actor = _ctx(mgr, RoleCode.REGIONAL_MANAGER.value, frozenset({w["r1"].id}))
    detail_m = MerchantService(session).get_merchant_detail(m_actor, m.id)
    assert detail_m["contact_masked"] is False
    assert detail_m["contact_phone"] == PHONE
    assert detail_m["location_masked"] is False
    assert detail_m["latitude"] == pytest.approx(LAT, abs=1e-6)
    assert detail_m["longitude"] == pytest.approx(LNG, abs=1e-6)

    # 厂商经详情接口读商家明细 → 行级 403（厂商不读商家明细，仅脱敏聚合）
    supplier = _make_user(session, w["ds"], RoleCode.SUPPLIER, "supplier")
    with pytest.raises(PermissionDeniedError):
        MerchantService(session).get_merchant_detail(
            _ctx(supplier, RoleCode.SUPPLIER.value), m.id
        )


# ----------------------------- 等级审计（PRD §4.6，附测） -----------------------------


def test_grade_change_only_manager_and_no_backfill(session: Session) -> None:
    w = _make_world(session)
    m = _add_merchant(
        session, w["ds"], w["r1"], w["brand"], code="G", name="等级店", grade="C"
    )
    mgr = _make_user(session, w["ds"], RoleCode.REGIONAL_MANAGER, "mgr")
    rep = _make_user(session, w["ds"], RoleCode.SALES_REP, "rep")
    actor = _ctx(mgr, RoleCode.REGIONAL_MANAGER.value, frozenset({w["r1"].id}))
    svc = MerchantService(session)

    # 业务员无权调级
    with pytest.raises(PermissionDeniedError):
        svc.change_grade(
            _ctx(rep, RoleCode.SALES_REP.value, frozenset({w["r1"].id})),
            m.id,
            GradeChangeRequest(
                new_grade="A", effective_from=date.today(), reason="业务员越权"
            ),
        )

    # 未来生效：写历史但当前快照不变（不回溯）
    future = date.today() + timedelta(days=30)
    change = svc.change_grade(
        actor,
        m.id,
        GradeChangeRequest(new_grade="A", effective_from=future, reason="下月升级"),
    )
    session.flush()
    assert change.new_grade == "A"
    assert svc.repo.get_by_id(m.id).grade == "C"

    # 过去生效日被拒
    with pytest.raises(DomainError):
        svc.change_grade(
            actor,
            m.id,
            GradeChangeRequest(
                new_grade="B",
                effective_from=date.today() - timedelta(days=1),
                reason="回溯",
            ),
        )


# ----------------------------- Q042 -----------------------------


def test_q042_url_filter_roundtrip(session: Session) -> None:
    """URL 查询参数经 MerchantListQuery 规范化后，applied_filters 可还原同条件。"""
    w = _make_world(session)
    for i in range(3):
        _add_merchant(
            session, w["ds"], w["r1"], w["brand"],
            code=f"F{i}", name=f"华东{i}号店", grade="A",
        )
    _add_merchant(
        session, w["ds"], w["r2"], w["brand"], code="F9", name="华南店", grade="B"
    )
    mgr = _make_user(session, w["ds"], RoleCode.REGIONAL_MANAGER, "mgr")
    actor = _ctx(mgr, RoleCode.REGIONAL_MANAGER.value, frozenset({w["r1"].id}))
    svc = MerchantService(session)

    q = MerchantListQuery(
        status="active", grade="a", region_id=w["r1"].id,
        keyword="华东", order="name", page=1, page_size=10,
    )
    items1, total, applied = svc.list_merchants(actor, q)

    # 回显规范化（大写枚举），可据此重建 URL
    assert applied["status"] == "ACTIVE"
    assert applied["grade"] == "A"
    assert applied["region_id"] == str(w["r1"].id)
    assert applied["keyword"] == "华东"
    assert applied["order"] == "name"
    assert total == 3

    # 用回显条件重建查询，结果一致（往返恢复）
    q2 = MerchantListQuery(
        status=applied["status"],
        grade=applied["grade"],
        region_id=UUID(applied["region_id"]),
        keyword=applied["keyword"],
        order=applied["order"],
        page=applied["page"],
        page_size=applied["page_size"],
    )
    items2, total2, _ = svc.list_merchants(actor, q2)
    assert total2 == total == 3
    # 往返恢复：用回显条件重建的查询返回同一批商家
    assert {it.id for it in items2} == {it.id for it in items1}


# --------------------- HTTP 挂载/鉴权冒烟（Q042 路由层） ---------------------


# --------------- 商家状态维护（PRD §4.3 状态机，仅负责业务员） ---------------


def _owned_merchant(session: Session, w: dict, mgr: User, rep: User) -> Merchant:
    """建档并把主责任指派给业务员（服务层路径，区间数据域与商家一致）。"""
    mgr_actor = _ctx(mgr, RoleCode.REGIONAL_MANAGER.value, frozenset({w["r1"].id}))
    merchant, _ = MerchantService(session).create_merchant(
        mgr_actor,
        MerchantCreateRequest(
            code=f"M-ST-{uuid4().hex[:6]}",
            name="状态机店",
            region_id=w["r1"].id,
            brand_id=w["brand"].id,
            owner_user_id=rep.id,
        ),
    )
    session.flush()
    return merchant


def _add_contract(
    session: Session,
    merchant: Merchant,
    *,
    effective_from: date = date(2026, 1, 1),
    expire_at: date | None = None,
) -> Contract:
    """给商家挂一份生效合同（状态直接置 EFFECTIVE，绕开审批链路）。"""
    contract = Contract(
        merchant_id=merchant.id,
        contract_no=f"C-{uuid4().hex[:8]}",
        title="年度供货合同",
        party_name=merchant.name,
        status="EFFECTIVE",
        effective_from=effective_from,
        expire_at=expire_at,
    )
    session.add(contract)
    session.flush()
    return contract


def test_merchant_status_owner_sales_rep_follows_state_machine(session: Session) -> None:
    """负责业务员可沿 PRD §4.3 状态机推进/相邻回退，并逐次留审计。"""
    w = _make_world(session)
    mgr = _make_user(session, w["ds"], RoleCode.REGIONAL_MANAGER, "mgr")
    rep = _make_user(session, w["ds"], RoleCode.SALES_REP, "rep")
    rep_actor = _ctx(rep, RoleCode.SALES_REP.value, frozenset({w["r1"].id}))
    svc = MerchantService(session)

    merchant = _owned_merchant(session, w, mgr, rep)
    assert merchant.status == MerchantStatus.LEAD.value  # 建档默认线索
    _add_contract(session, merchant)  # 置 ACTIVE 的前置：生效合同

    for target in ("NEGOTIATING", "ACTIVE", "PAUSED", "ACTIVE"):
        updated, old = svc.change_status(
            rep_actor,
            merchant.id,
            MerchantStatusChangeRequest(new_status=target, reason=f"改为 {target}"),
        )
        assert updated.status == target
        assert old != target
    session.flush()
    assert merchant.status == MerchantStatus.ACTIVE.value

    audits = session.scalars(
        select(AuditLog)
        .where(AuditLog.object_type == "MERCHANT")
        .where(AuditLog.object_id == merchant.id)
    ).all()
    status_audits = [a for a in audits if (a.after_json or {}).get("status")]
    assert [a.after_json["status"] for a in status_audits] == [
        "NEGOTIATING",
        "ACTIVE",
        "PAUSED",
        "ACTIVE",
    ]
    assert all(a.reason for a in status_audits)


def test_merchant_status_rejects_skip_change_and_termination(session: Session) -> None:
    """跳级变更、原地变更、直接终止都要被拒（终止走取消合作审批）。"""
    w = _make_world(session)
    mgr = _make_user(session, w["ds"], RoleCode.REGIONAL_MANAGER, "mgr")
    rep = _make_user(session, w["ds"], RoleCode.SALES_REP, "rep")
    rep_actor = _ctx(rep, RoleCode.SALES_REP.value, frozenset({w["r1"].id}))
    svc = MerchantService(session)
    merchant = _owned_merchant(session, w, mgr, rep)

    with pytest.raises(DomainError) as skip:
        svc.change_status(
            rep_actor,
            merchant.id,
            MerchantStatusChangeRequest(new_status="ACTIVE", reason="跳级"),
        )
    assert skip.value.code == "MERCHANT_STATUS_TRANSITION_INVALID"
    assert skip.value.details["allowed"] == ["NEGOTIATING"]

    with pytest.raises(ConflictError) as same:
        svc.change_status(
            rep_actor,
            merchant.id,
            MerchantStatusChangeRequest(new_status="LEAD", reason="原地"),
        )
    assert same.value.code == "MERCHANT_STATUS_UNCHANGED"

    with pytest.raises(DomainError) as term:
        svc.change_status(
            rep_actor,
            merchant.id,
            MerchantStatusChangeRequest(new_status="TERMINATED", reason="不想做了"),
        )
    assert term.value.code == "MERCHANT_TERMINATION_REQUIRES_APPROVAL"
    session.flush()
    assert merchant.status == MerchantStatus.LEAD.value  # 全部被拒，状态未动


def test_merchant_status_active_requires_effective_contract(session: Session) -> None:
    """无生效合同（或已到期）不能置为「合作中」；生效且未到期才放行。"""
    w = _make_world(session)
    mgr = _make_user(session, w["ds"], RoleCode.REGIONAL_MANAGER, "mgr")
    rep = _make_user(session, w["ds"], RoleCode.SALES_REP, "rep")
    rep_actor = _ctx(rep, RoleCode.SALES_REP.value, frozenset({w["r1"].id}))
    svc = MerchantService(session)
    merchant = _owned_merchant(session, w, mgr, rep)
    svc.change_status(
        rep_actor,
        merchant.id,
        MerchantStatusChangeRequest(new_status="NEGOTIATING", reason="洽谈中"),
    )

    # 完全没有合同
    with pytest.raises(DomainError) as no_contract:
        svc.change_status(
            rep_actor,
            merchant.id,
            MerchantStatusChangeRequest(new_status="ACTIVE", reason="先置合作中"),
        )
    assert no_contract.value.code == "MERCHANT_ACTIVE_REQUIRES_EFFECTIVE_CONTRACT"
    session.flush()
    assert merchant.status == MerchantStatus.NEGOTIATING.value  # 未落库

    # 合同已到期：不算生效
    expired = _add_contract(
        session,
        merchant,
        effective_from=date(2025, 1, 1),
        expire_at=date(2025, 12, 31),
    )
    with pytest.raises(DomainError) as expired_exc:
        svc.change_status(
            rep_actor,
            merchant.id,
            MerchantStatusChangeRequest(new_status="ACTIVE", reason="到期合同"),
        )
    assert expired_exc.value.code == "MERCHANT_ACTIVE_REQUIRES_EFFECTIVE_CONTRACT"

    # 生效且未到期：放行
    expired.status = "TERMINATED"
    session.flush()
    _add_contract(session, merchant, expire_at=date(2027, 12, 31))
    updated, old = svc.change_status(
        rep_actor,
        merchant.id,
        MerchantStatusChangeRequest(new_status="ACTIVE", reason="合同已生效"),
    )
    assert updated.status == MerchantStatus.ACTIVE.value
    assert old == MerchantStatus.NEGOTIATING.value


def test_merchant_detail_aggregation_returns_full_contract_fields(session: Session) -> None:
    """详情聚合必须返回完整合同编号/状态。

    回归：多列 select 误用 scalars() 只回第一列，c[0]/c[1] 退化成字符串
    首字符（"HT-DEMO-003"→"H"/"T"），前端据此误判无生效合同、藏起「合作中」。
    """
    w = _make_world(session)
    mgr = _make_user(session, w["ds"], RoleCode.REGIONAL_MANAGER, "mgr")
    rep = _make_user(session, w["ds"], RoleCode.SALES_REP, "rep")
    mgr_actor = _ctx(mgr, RoleCode.REGIONAL_MANAGER.value, frozenset({w["r1"].id}))
    merchant = _owned_merchant(session, w, mgr, rep)
    contract = _add_contract(session, merchant)

    detail = MerchantService(session).get_merchant_detail(mgr_actor, merchant.id)

    assert detail["contracts"] == [
        {"contract_no": contract.contract_no, "status": "EFFECTIVE"}
    ]
    assert len(detail["contracts"][0]["contract_no"]) > 1


def test_merchant_status_manager_fallback_and_other_roles_readonly(session: Session) -> None:
    """片区经理可在本片区兜底改状态（业务员离职出口）；越区与其他角色一律拒绝。"""
    w = _make_world(session)
    mgr = _make_user(session, w["ds"], RoleCode.REGIONAL_MANAGER, "mgr")
    rep = _make_user(session, w["ds"], RoleCode.SALES_REP, "rep")
    other = _make_user(session, w["ds"], RoleCode.SALES_REP, "rep2")
    admin = _make_user(session, w["ds"], RoleCode.SYSTEM_ADMIN, "admin")
    mgr_actor = _ctx(mgr, RoleCode.REGIONAL_MANAGER.value, frozenset({w["r1"].id}))
    svc = MerchantService(session)
    merchant = _owned_merchant(session, w, mgr, rep)  # 商家落在 r1
    body = MerchantStatusChangeRequest(new_status="NEGOTIATING", reason="兜底推进")

    # 片区经理本片区兜底：放行（不限当前负责人）
    moved, old = svc.change_status(mgr_actor, merchant.id, body)
    assert moved.status == MerchantStatus.NEGOTIATING.value
    assert old == MerchantStatus.LEAD.value

    # 片区经理越区：行级范围拒绝
    outside = _add_merchant(
        session, w["ds"], w["r2"], w["brand"], code="M-R2", name="华南店"
    )
    with pytest.raises(PermissionDeniedError) as cross:
        svc.change_status(
            mgr_actor,
            outside.id,
            MerchantStatusChangeRequest(new_status="NEGOTIATING", reason="越区"),
        )
    assert cross.value.code == "MERCHANT_REGION_FORBIDDEN"

    # 系统管理员：只读
    with pytest.raises(PermissionDeniedError) as e2:
        svc.change_status(_ctx(admin, RoleCode.SYSTEM_ADMIN.value), merchant.id, body)
    assert e2.value.code == "MERCHANT_STATUS_FORBIDDEN"

    # 同片区非负责人业务员
    with pytest.raises(PermissionDeniedError) as e3:
        svc.change_status(
            _ctx(other, RoleCode.SALES_REP.value, frozenset({w["r1"].id})),
            merchant.id,
            body,
        )
    assert e3.value.code == "MERCHANT_NOT_ASSIGNED"


# --------------- 责任归属数据域一致（绩效归属前置条件，附测） ---------------


def test_assignment_shares_merchant_dataset_domain(session: Session) -> None:
    """建档指派/归属调整产生的责任区间必须与商家同 dataset_type + dataset_id。

    绩效归属（PerformanceRepository.primary_owner_at）按 dataset_id 命中区间；
    遗漏数据域会落成 PRODUCTION + 随机 UUID，使该商家的新签/巡访/销量绩效全部
    归不到人，而前端「当前负责人」仍显示正常，属于难察觉的静默错误。
    """
    w = _make_world(session)
    mgr = _make_user(session, w["ds"], RoleCode.REGIONAL_MANAGER, "mgr")
    rep = _make_user(session, w["ds"], RoleCode.SALES_REP, "rep")
    actor = _ctx(mgr, RoleCode.REGIONAL_MANAGER.value, frozenset({w["r1"].id}))
    svc = MerchantService(session)

    # 建档即指派主负责人
    merchant, _ = svc.create_merchant(
        actor,
        MerchantCreateRequest(
            code=f"M-DS-{uuid4().hex[:6]}",
            name="数据域校验店",
            region_id=w["r1"].id,
            brand_id=w["brand"].id,
            owner_user_id=rep.id,
        ),
    )
    session.flush()
    created = session.scalars(
        select(MerchantAssignment).where(
            MerchantAssignment.merchant_id == merchant.id
        )
    ).all()
    assert len(created) == 1
    assert created[0].dataset_id == merchant.dataset_id
    assert created[0].dataset_type == merchant.dataset_type

    # 归属调整同样保持一致（区间取在建档区间之前，避免主责任重叠被拒）
    base = datetime(2026, 1, 1, tzinfo=UTC)
    changed = svc.assign(
        actor,
        merchant.id,
        AssignmentCreateRequest(
            owner_user_id=rep.id, assignment_type="PRIMARY",
            start_at=base, end_at=base + timedelta(days=30), reason="历史归属",
        ),
    )
    session.flush()
    assert changed.dataset_id == merchant.dataset_id
    assert changed.dataset_type == merchant.dataset_type


def test_merchants_endpoint_requires_auth(client: TestClient) -> None:
    """商家中心路由已挂载；未携带 JWT 一律 401（行级范围只由服务端注入）。"""
    assert client.get("/api/merchants").status_code == 401
    assert client.get("/api/merchants?status=ACTIVE&grade=A&page=1").status_code == 401


def test_merchants_url_filters_roundtrip_http(
    client: TestClient,
    bootstrap_admin: dict[str, str],
    session: Session,
) -> None:
    """管理员经真实 HTTP 携带 URL 筛选访问，回显 applied_filters 可恢复同条件。"""
    # 管理员登录拿 token
    login = client.post(
        "/api/auth/login",
        json={
            "username": bootstrap_admin["username"],
            "password": bootstrap_admin["password"],
        },
    )
    assert login.status_code == 200, login.text
    token = login.json()["access_token"]

    resp = client.get(
        "/api/merchants",
        params={"status": "active", "grade": "a", "keyword": "店", "page": 2, "page_size": 5},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # URL 参数被规范化回显（枚举大写、分页原值），前端据此恢复 URL
    assert body["applied_filters"]["status"] == "ACTIVE"
    assert body["applied_filters"]["grade"] == "A"
    assert body["applied_filters"]["keyword"] == "店"
    assert body["pagination"]["page"] == 2
    assert body["pagination"]["page_size"] == 5
