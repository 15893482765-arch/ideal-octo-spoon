"""供应商管理端点测试（T34：供应商创建/启停/管理列表）。"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from tests.test_t09_contract import _user, _world
from app.core.errors import DomainError, PermissionDeniedError
from app.core.permissions import ActorContext, PermissionError_
from app.models import (
    PurchaseOrder,
    PurchaseOrderLine,
    Sku,
    Supplier,
    SupplierAuthorization,
    UserRegionBrandAuth,
)
from app.models.enums import RoleCode
from app.services.procurement_service import ProcurementService


# ----------------------------- 造数 -----------------------------


def _sku(session: Session, w: dict, code: str, *, dataset_type: str = "PRODUCTION",
         dataset_id=None) -> Sku:
    s = Sku(
        code=f"SKU-{code}-{uuid4().hex[:6]}",
        name="沁爽汽水",
        brand_id=w["brand"].id,
        capacity_ml=500, case_spec=Decimal("24"),
        suggested_price_fen=6000,
        dataset_type=dataset_type,
        dataset_id=dataset_id or w["ds"],
    )
    session.add(s)
    session.flush()
    return s


def _supplier_world(session: Session, w: dict, tag: str, *,
                    region_id=None, brand_id=None):
    """供应商 + 厂商登录用户 + 品牌×片区授权。返回 (supplier, user, actor)。"""
    supplier = Supplier(
        code=f"SUP-{uuid4().hex[:8]}",
        name=f"{tag}饮品供应有限公司",
        contact_name="张采购",
        contact_phone="13800001111",
        dataset_type="PRODUCTION",
        dataset_id=w["ds"],
    )
    session.add(supplier)
    session.flush()
    u = _user(session, w["ds"], RoleCode.SUPPLIER, f"{tag}-supplier")
    sa = SupplierAuthorization(
        supplier_id=supplier.id, user_id=u.id,
        brand_id=brand_id or w["brand"].id,
        region_id=region_id or w["region"].id,
        dataset_type="PRODUCTION", dataset_id=w["ds"],
    )
    session.add(sa)
    session.flush()
    actor = ActorContext(
        user_id=u.id, role_code=RoleCode.SUPPLIER.value,
        region_ids=frozenset({region_id or w["region"].id}),
        brand_ids=frozenset({brand_id or w["brand"].id}),
    )
    return supplier, u, actor


def _internal_actor(session: Session, w: dict, role: RoleCode, tag: str):
    u = _user(session, w["ds"], role, tag)
    session.add(
        UserRegionBrandAuth(
            user_id=u.id, region_id=w["region"].id, brand_id=w["brand"].id
        )
    )
    session.flush()
    actor = ActorContext(
        user_id=u.id,
        role_code=role.value,
        region_ids=frozenset({w["region"].id}),
        brand_ids=frozenset({w["brand"].id}),
    )
    return u, actor


# ----------------------------- 测试 -----------------------------


def test_t34_create_supplier_basic(session: Session) -> None:
    """创建供应商：基本信息正确写入。"""
    w = _world(session)
    _admin, admin_actor = _internal_actor(session, w, RoleCode.SYSTEM_ADMIN, "t34-admin")
    svc = ProcurementService(session)

    supplier = svc.create_supplier(
        admin_actor,
        code="SUP-T34",
        name="测试供应商公司",
        contact_name="张三",
        contact_phone="13800138000",
    )

    assert supplier.code == "SUP-T34"
    assert supplier.name == "测试供应商公司"
    assert supplier.contact_name == "张三"
    assert supplier.contact_phone == "13800138000"
    assert supplier.active is True
    assert supplier.dataset_type == "PRODUCTION"
    assert supplier.dataset_id == w["ds"]

    # 验证落库
    db_supplier = session.scalar(select(Supplier).where(Supplier.code == "SUP-T34"))
    assert db_supplier is not None
    assert db_supplier.id == supplier.id


def test_t34_create_supplier_blank_code(session: Session) -> None:
    """创建供应商：空编码应拒绝。"""
    w = _world(session)
    _admin, admin_actor = _internal_actor(session, w, RoleCode.SYSTEM_ADMIN, "t34-admin-blank")

    with pytest.raises(DomainError) as exc:
        ProcurementService(session).create_supplier(admin_actor, code="", name="测试")
    assert exc.value.code == "SUPPLIER_CODE_REQUIRED"


def test_t34_create_supplier_duplicate_code(session: Session) -> None:
    """创建供应商：重复编码应拒绝。"""
    w = _world(session)
    _admin, admin_actor = _internal_actor(session, w, RoleCode.SYSTEM_ADMIN, "t34-admin-dup")

    svc = ProcurementService(session)
    svc.create_supplier(admin_actor, code="SUP-T34DUP", name="第一家")
    with pytest.raises(DomainError) as exc:
        svc.create_supplier(admin_actor, code="SUP-T34DUP", name="重复的")
    assert exc.value.code == "SUPPLIER_CODE_DUPLICATE"


def test_t34_toggle_supplier_active(session: Session) -> None:
    """启停供应商：toggle后active状态翻转。"""
    w = _world(session)
    _admin, admin_actor = _internal_actor(session, w, RoleCode.SYSTEM_ADMIN, "t34-admin-toggle")

    svc = ProcurementService(session)
    supplier = svc.create_supplier(admin_actor, code="SUP-T34TOG", name="测试供应商")
    assert supplier.active is True

    toggled = svc.toggle_supplier_active(admin_actor, supplier_id=supplier.id)
    assert toggled.active is False

    toggled2 = svc.toggle_supplier_active(admin_actor, supplier_id=supplier.id)
    assert toggled2.active is True


def test_t34_deactivated_supplier_cannot_be_used_in_purchase(session: Session) -> None:
    """已停用供应商不得用于建单。"""
    from app.models import ProductCategory
    w = _world(session)
    cat = ProductCategory(
        code=f"CAT-{uuid4().hex[:8]}", name="测试类别",
        dataset_type="PRODUCTION", dataset_id=w["ds"],
    )
    session.add(cat)
    session.flush()
    sku1 = _sku(session, w, "deact-sku")
    _admin, admin_actor = _internal_actor(session, w, RoleCode.SYSTEM_ADMIN, "t34-admin-deact")
    sku2 = _sku(session, w, "line-sku")

    svc = ProcurementService(session)
    supplier = svc.create_supplier(admin_actor, code="SUP-T34DEACT", name="测试供应商")

    # 停用供应商
    svc.toggle_supplier_active(admin_actor, supplier_id=supplier.id)

    # 建单应失败
    with pytest.raises(DomainError) as exc:
        svc.create_purchase_order(
            admin_actor,
            supplier_id=supplier.id, brand_id=w["brand"].id, region_id=w["region"].id,
            expected_date=date(2026, 10, 25),
            lines=[(sku2.id, Decimal("1"), 3500)],
        )
    assert exc.value.code == "SUPPLIER_NOT_FOUND"
    # 无任何采购单落库
    assert session.scalar(select(func.count()).select_from(PurchaseOrder)) == 0


def test_t34_supplier_not_in_options_after_deactivation(session: Session) -> None:
    """停用的供应商不出现在下拉选项中。"""
    w = _world(session)
    _admin, admin_actor = _internal_actor(session, w, RoleCode.SYSTEM_ADMIN, "t34-admin-opt")

    svc = ProcurementService(session)
    supplier = svc.create_supplier(admin_actor, code="SUP-T34OPT", name="测试供应商")

    # 停用
    svc.toggle_supplier_active(admin_actor, supplier_id=supplier.id)

    # 下拉不应包含已停用供应商
    options = svc.list_supplier_options(admin_actor)
    ids = {r["id"] for r in options}
    assert supplier.id not in ids


def test_t34_supplier_management_list_includes_inactive(session: Session) -> None:
    """管理列表包含已停用供应商。"""
    w = _world(session)
    _admin, admin_actor = _internal_actor(session, w, RoleCode.SYSTEM_ADMIN, "t34-admin-mgmt")

    svc = ProcurementService(session)
    supplier = svc.create_supplier(admin_actor, code="SUP-T34MGT", name="测试供应商")

    # 停用
    svc.toggle_supplier_active(admin_actor, supplier_id=supplier.id)

    # 管理列表应包含已停用供应商
    management = svc.list_supplier_management(admin_actor)
    codes = {r["code"] for r in management}
    assert "SUP-T34MGT" in codes
    # 同时确认该条目active=False
    row = next(r for r in management if r["code"] == "SUP-T34MGT")
    assert row["active"] is False


def test_t34_supplier_not_found_after_invalid_id(session: Session) -> None:
    """供应商不存在时toggle应返回NOT_FOUND错误。"""
    w = _world(session)
    _admin, admin_actor = _internal_actor(session, w, RoleCode.SYSTEM_ADMIN, "t34-admin-nf")

    from uuid import UUID
    fake_id = UUID(int=999999)
    with pytest.raises(DomainError) as exc:
        ProcurementService(session).toggle_supplier_active(admin_actor, supplier_id=fake_id)
    assert exc.value.code == "SUPPLIER_NOT_FOUND"


def test_t34_supplier_management_protects_supplier_role(session: Session) -> None:
    """厂商角色不得访问供应商管理接口。"""
    w = _world(session)
    supplier, _su, supplier_actor = _supplier_world(session, w, "t34-sup-prot")

    # 厂商尝试创建供应商 → 应被拒绝
    with pytest.raises(PermissionError_):
        ProcurementService(session).create_supplier(
            supplier_actor, code="SUP-T34X", name="试图创建的供应商"
        )

    # 厂商尝试toggle → 应被拒绝
    with pytest.raises(PermissionError_):
        ProcurementService(session).toggle_supplier_active(
            supplier_actor, supplier_id=supplier.id
        )

    # 厂商尝试查看管理列表 → 应被拒绝
    with pytest.raises(PermissionError_):
        ProcurementService(session).list_supplier_management(supplier_actor)
