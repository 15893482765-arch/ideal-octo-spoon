"""Q179 v1.2 建模：押金字段、等级频次配置、RiskItem 唯一约束。"""

from __future__ import annotations

from datetime import date
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models import (
    Brand,
    FreezerAsset,
    FreezerMovement,
    MerchantGradeFrequency,
    Organization,
    Region,
    RiskItem,
    Role,
    User,
)


def _seed_org_brand_region(session):
    org = Organization(code="ORG-V12", name="v1.2 测试")
    brand = Brand(code="B-V12", name="v1.2 品牌")
    session.add_all([org, brand])
    session.flush()
    region = Region(code="R-V12", name="v1.2 片区", org_id=org.id)
    session.add(region)
    session.flush()
    return org, brand, region


def _seed_operator(session, code: str = "OP") -> User:
    role = Role(
        code=code,
        name="操作员",
        role_code="SUPERVISOR",
        dataset_type="PRODUCTION",
        dataset_id=uuid4(),
    )
    session.add(role)
    session.flush()
    u = User(
        username=f"op-{uuid4().hex[:6]}",
        password_hash="$argon2id$v=19$m=65536,t=3,p=4$xxx",
        display_name="操作员",
        role_id=role.id,
        dataset_type="PRODUCTION",
        dataset_id=uuid4(),
    )
    session.add(u)
    session.flush()
    return u


def test_freezer_deposit_fen_optional_and_nonneg(session) -> None:
    """Q179：FreezerAsset.deposit_fen 可空；不为空时必须 >= 0。"""
    org, brand, region = _seed_org_brand_region(session)
    a_no = FreezerAsset(
        asset_code="FZ-ND",
        brand_id=brand.id,
        model="M-1",
        purchase_date=date(2026, 1, 1),
        purchase_price_fen=120000,
        deposit_fen=None,
    )
    session.add(a_no)
    session.flush()
    assert a_no.deposit_fen is None

    a_neg = FreezerAsset(
        asset_code="FZ-NEG",
        brand_id=brand.id,
        model="M-2",
        purchase_date=date(2026, 1, 1),
        purchase_price_fen=100000,
        deposit_fen=-1,
    )
    session.add(a_neg)
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_deposit_movement_types_allowed(session) -> None:
    """Q179：DEPOSIT_COLLECTED / DEPOSIT_REFUNDED 是合法 movement_type。"""
    from app.models.enums import FreezerMovementType

    org, brand, region = _seed_org_brand_region(session)
    operator = _seed_operator(session, "OP1")
    asset = FreezerAsset(
        asset_code="FZ-DP",
        brand_id=brand.id,
        model="M-DP",
        purchase_date=date(2026, 1, 1),
        purchase_price_fen=100000,
        deposit_fen=50000,
    )
    session.add(asset)
    session.flush()
    # T10（R37）：押金收缴必须关联投放单；先造一条 DEPLOY 流水作为关联单据
    m_deploy = FreezerMovement(
        asset_id=asset.id,
        movement_type=FreezerMovementType.DEPLOY.value,
        operator_id=operator.id,
        to_status="DEPLOYED",
    )
    session.add(m_deploy)
    session.flush()
    m_collected = FreezerMovement(
        asset_id=asset.id,
        movement_type=FreezerMovementType.DEPOSIT_COLLECTED.value,
        operator_id=operator.id,
        amount_fen=50000,
        related_movement_id=m_deploy.id,  # R37：押金收缴关联投放单
    )
    session.add(m_collected)
    session.flush()
    m_refunded = FreezerMovement(
        asset_id=asset.id,
        movement_type=FreezerMovementType.DEPOSIT_REFUNDED.value,
        operator_id=operator.id,
        amount_fen=50000,
        related_movement_id=m_collected.id,
    )
    session.add(m_refunded)
    session.flush()
    assert m_collected.id is not None
    assert m_refunded.related_movement_id == m_collected.id


def test_deposit_movement_invalid_type_rejected(session) -> None:
    """Q179：非法 movement_type 被数据库 CHECK 拒绝。"""
    org, brand, region = _seed_org_brand_region(session)
    operator = _seed_operator(session, "OP2")
    asset = FreezerAsset(
        asset_code="FZ-DPX",
        brand_id=brand.id,
        model="M-DPX",
        purchase_date=date(2026, 1, 1),
        purchase_price_fen=100000,
        deposit_fen=0,
    )
    session.add(asset)
    session.flush()
    bad = FreezerMovement(
        asset_id=asset.id,
        movement_type="NOT_A_TYPE",
        operator_id=operator.id,
        amount_fen=0,
    )
    session.add(bad)
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_merchant_grade_frequency_default_seven_days(session) -> None:
    """Q179：等级频次配置：默认 7 天，CHECK > 0，end_date >= start_date。"""
    cfg_a = MerchantGradeFrequency(
        grade="A",
        frequency_days=7,
        start_date=date(2026, 1, 1),
        end_date=None,
    )
    cfg_b = MerchantGradeFrequency(
        grade="B",
        frequency_days=14,
        start_date=date(2026, 1, 1),
        end_date=date(2026, 6, 30),
    )
    session.add_all([cfg_a, cfg_b])
    session.flush()
    assert cfg_a.frequency_days == 7
    assert cfg_b.end_date == date(2026, 6, 30)


def test_merchant_grade_frequency_invalid(session) -> None:
    """Q179：frequency_days 必须 > 0；end_date >= start_date。"""
    bad_zero = MerchantGradeFrequency(
        grade="C",
        frequency_days=0,
        start_date=date(2026, 1, 1),
    )
    session.add(bad_zero)
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()

    bad_range = MerchantGradeFrequency(
        grade="A",
        frequency_days=7,
        start_date=date(2026, 6, 1),
        end_date=date(2026, 1, 1),
    )
    session.add(bad_range)
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_risk_item_unique_object_rule_period(session) -> None:
    """Q179：RiskItem 同对象同规则同周期不重复（幂等生成）。"""
    from app.models.enums import RiskObjectType, RiskRuleCode

    obj_id = uuid4()
    period = "2026-W42"
    r1 = RiskItem(
        object_type=RiskObjectType.MERCHANT.value,
        object_id=obj_id,
        rule_code=RiskRuleCode.RK1_NO_VISIT.value,
        period=period,
    )
    session.add(r1)
    session.flush()

    r2 = RiskItem(
        object_type=RiskObjectType.MERCHANT.value,
        object_id=obj_id,
        rule_code=RiskRuleCode.RK1_NO_VISIT.value,
        period=period,
    )
    session.add(r2)
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_risk_item_different_period_allowed(session) -> None:
    """Q179：不同周期可以各自生成一条（按周滚动）。"""
    from app.models.enums import RiskObjectType, RiskRuleCode

    obj_id = uuid4()
    for period in ("2026-W41", "2026-W42", "2026-W43"):
        r = RiskItem(
            object_type=RiskObjectType.MERCHANT.value,
            object_id=obj_id,
            rule_code=RiskRuleCode.RK1_NO_VISIT.value,
            period=period,
        )
        session.add(r)
    session.flush()
    count = session.execute(
        select(RiskItem).where(RiskItem.object_id == obj_id)
    ).scalars().all()
    assert len(count) == 3


def test_risk_item_rule_code_restricted(session) -> None:
    """Q179：rule_code 仅允许 RK-1..RK-7。"""
    r = RiskItem(
        object_type="MERCHANT",
        object_id=uuid4(),
        rule_code="RK-99",
        period="2026-W42",
    )
    session.add(r)
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()
