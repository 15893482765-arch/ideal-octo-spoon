"""Q007 关键唯一约束；Q009 金额/箱数精度。"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models import (
    Brand,
    Contract,
    FreezerAsset,
    Merchant,
    Organization,
    ProcurementOrder,
    Region,
    ReplenishmentOrder,
    Role,
    Sku,
    User,
    UserRegionBrandAuth,
)


def _seed_basics(session):
    org = Organization(code="ORG-1", name="总部")
    brand = Brand(code="B-1", name="沁爽主品牌")
    session.add_all([org, brand])
    session.flush()
    region = Region(code="R-1", name="华东一区", org_id=org.id)
    session.add(region)
    session.flush()
    return org, region, brand


def _make_sku(session, code: str, brand_id) -> Sku:
    sku = Sku(
        code=code,
        name=f"测试 {code}",
        brand_id=brand_id,
        suggested_price_fen=500,
        case_spec=Decimal("12.00"),
    )
    session.add(sku)
    session.flush()
    return sku


# ============== Q007 唯一约束 ==============


def test_user_username_unique_within_dataset(session) -> None:
    org, region, brand = _seed_basics(session)
    role = Role(
        code="MGR",
        name="经理",
        role_code="REGIONAL_MANAGER",
        dataset_type="PRODUCTION",
        dataset_id=uuid4(),
    )
    session.add(role)
    session.flush()
    dsid = uuid4()
    u1 = User(
        username="alice",
        password_hash="$argon2id$v=19$m=65536,t=3,p=4$xxx",
        display_name="Alice",
        role_id=role.id,
        dataset_type="PRODUCTION",
        dataset_id=dsid,
    )
    session.add(u1)
    session.flush()
    u2 = User(
        username="alice",
        password_hash="$argon2id$v=19$m=65536,t=3,p=4$xxx",
        display_name="Alice2",
        role_id=role.id,
        dataset_type="PRODUCTION",
        dataset_id=dsid,
    )
    session.add(u2)
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_sku_code_unique(session) -> None:
    _seed_basics(session)
    # 删除本测试插入的 skus
    session.execute(select(Sku))  # warm up
    # 直接使用唯一 code 即可；第二次插入相同 code 必冲突
    s1 = Sku(
        code="SKU-1",
        name="沁爽橙汁 500ml",
        brand_id=session.execute(select(Brand).where(Brand.code == "B-1")).scalar_one().id,
        suggested_price_fen=500,
        case_spec=Decimal("12"),
    )
    s2 = Sku(
        code="SKU-1",
        name="重复",
        brand_id=s1.brand_id,
        suggested_price_fen=500,
        case_spec=Decimal("12"),
    )
    session.add_all([s1, s2])
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_merchant_code_unique_within_dataset(session) -> None:
    org, region, brand = _seed_basics(session)
    dsid = uuid4()
    m1 = Merchant(
        code="M-1",
        name="一号店",
        region_id=region.id,
        brand_id=brand.id,
        dataset_type="PRODUCTION",
        dataset_id=dsid,
    )
    m2 = Merchant(
        code="M-1",
        name="二号店",
        region_id=region.id,
        brand_id=brand.id,
        dataset_type="PRODUCTION",
        dataset_id=dsid,
    )
    session.add_all([m1, m2])
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_contract_code_unique_within_dataset(session) -> None:
    org, region, brand = _seed_basics(session)
    m = Merchant(code="M-9", name="甲", region_id=region.id, brand_id=brand.id)
    session.add(m)
    session.flush()
    dsid = uuid4()
    c1 = Contract(
        code="C-1",
        merchant_id=m.id,
        start_date=date(2026, 1, 1),
        end_date=date(2026, 12, 31),
        commission_rate=Decimal("0.0500"),
        dataset_type="PRODUCTION",
        dataset_id=dsid,
    )
    c2 = Contract(
        code="C-1",
        merchant_id=m.id,
        start_date=date(2026, 1, 1),
        end_date=date(2026, 12, 31),
        commission_rate=Decimal("0.0500"),
        dataset_type="PRODUCTION",
        dataset_id=dsid,
    )
    session.add_all([c1, c2])
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_freezer_asset_code_unique(session) -> None:
    _seed_basics(session)
    brand_id = session.execute(select(Brand)).scalar_one().id
    dsid = uuid4()
    a1 = FreezerAsset(
        asset_code="FZ-1",
        brand_id=brand_id,
        model="MODEL-A",
        purchase_date=date(2026, 1, 1),
        dataset_type="PRODUCTION",
        dataset_id=dsid,
    )
    a2 = FreezerAsset(
        asset_code="FZ-1",
        brand_id=brand_id,
        model="MODEL-B",
        purchase_date=date(2026, 1, 1),
        dataset_type="PRODUCTION",
        dataset_id=dsid,
    )
    session.add_all([a1, a2])
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_procurement_order_code_unique(session) -> None:
    _seed_basics(session)
    brand_id = session.execute(select(Brand)).scalar_one().id
    sku = _make_sku(session, "SKU-PO", brand_id)
    dsid = uuid4()
    o1 = ProcurementOrder(
        code="PO-1",
        sku_id=sku.id,
        supplier_name="供应商X",
        quantity_cases=Decimal("10.00"),
        unit_price_fen=12000,
        expected_date=date(2026, 6, 1),
        dataset_type="PRODUCTION",
        dataset_id=dsid,
    )
    o2 = ProcurementOrder(
        code="PO-1",
        sku_id=sku.id,
        supplier_name="供应商Y",
        quantity_cases=Decimal("5.00"),
        unit_price_fen=12000,
        expected_date=date(2026, 6, 1),
        dataset_type="PRODUCTION",
        dataset_id=dsid,
    )
    session.add_all([o1, o2])
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_replenishment_order_code_unique(session) -> None:
    org, region, brand = _seed_basics(session)
    m = Merchant(code="M-RP-2", name="RP店", region_id=region.id, brand_id=brand.id)
    session.add(m)
    session.flush()
    sku = _make_sku(session, "SKU-RP-2", brand.id)
    dsid = uuid4()
    r1 = ReplenishmentOrder(
        code="RP-1",
        merchant_id=m.id,
        sku_id=sku.id,
        requested_boxes=Decimal("5.00"),
        committed_boxes=Decimal("5.00"),
        request_date=date(2026, 6, 1),
        dataset_type="PRODUCTION",
        dataset_id=dsid,
    )
    r2 = ReplenishmentOrder(
        code="RP-1",
        merchant_id=m.id,
        sku_id=sku.id,
        requested_boxes=Decimal("3.00"),
        committed_boxes=Decimal("3.00"),
        request_date=date(2026, 6, 1),
        dataset_type="PRODUCTION",
        dataset_id=dsid,
    )
    session.add_all([r1, r2])
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_user_region_brand_unique(session) -> None:
    org, region, brand = _seed_basics(session)
    role = Role(
        code="REP",
        name="业务员",
        role_code="SALES_REP",
        dataset_type="PRODUCTION",
        dataset_id=uuid4(),
    )
    session.add(role)
    session.flush()
    u = User(
        username="bob",
        password_hash="$argon2id$v=19$m=65536,t=3,p=4$xxx",
        display_name="Bob",
        role_id=role.id,
        dataset_type="PRODUCTION",
        dataset_id=uuid4(),
    )
    session.add(u)
    session.flush()
    a1 = UserRegionBrandAuth(user_id=u.id, region_id=region.id, brand_id=brand.id)
    a2 = UserRegionBrandAuth(user_id=u.id, region_id=region.id, brand_id=brand.id)
    session.add_all([a1, a2])
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


# ============== Q009 金额/箱数精度 ==============


def test_money_amount_rejects_negative(session) -> None:
    """Q009：金额 BIGINT 存分，CHECK >= 0。"""
    org, region, brand = _seed_basics(session)
    m = Merchant(code="M-AMT", name="金额测试店", region_id=region.id, brand_id=brand.id)
    session.add(m)
    session.flush()
    c = Contract(
        code="C-AMT",
        merchant_id=m.id,
        start_date=date(2026, 1, 1),
        end_date=date(2026, 12, 31),
        monthly_rent_fen=-1,  # 触发 CHECK 约束
        commission_rate=Decimal("0.05"),
    )
    session.add(c)
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_box_count_scale_and_precision(session) -> None:
    """Q009：箱数 NUMERIC(12,2)，可存 0.01 精度。"""
    _seed_basics(session)
    brand_id = session.execute(select(Brand)).scalar_one().id
    sku = _make_sku(session, "SKU-SCALE", brand_id)
    o = ProcurementOrder(
        code="PO-SCALE",
        sku_id=sku.id,
        supplier_name="供应商",
        quantity_cases=Decimal("0.01"),
        unit_price_fen=1,
        expected_date=date(2026, 6, 1),
    )
    session.add(o)
    session.flush()
    fetched = session.execute(
        select(ProcurementOrder).where(ProcurementOrder.code == "PO-SCALE")
    ).scalar_one()
    assert fetched.quantity_cases == Decimal("0.01")


def test_money_int_only_at_db(session) -> None:
    """Q009：金额列类型为 BIGINT。"""
    from sqlalchemy import inspect

    insp = inspect(session.get_bind())
    cols = {c["name"]: c for c in insp.get_columns("contracts")}
    assert "monthly_rent_fen" in cols
    rent_col_type = str(cols["monthly_rent_fen"]["type"]).lower()
    assert "int" in rent_col_type or "bigint" in rent_col_type
