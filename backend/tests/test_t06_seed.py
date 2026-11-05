"""T06 SeedCheck 与跨域负例自检测试（PRD §10.2、§10.4 / R18 / R24）。

- Q029 空批次可运行：PENDING/0 rows → run → SUCCEEDED，无负例
- Q030 三类跨域负例：
  1) 演示商家 + 生产合同
  2) 生产工资引用演示绩效
  3) 异步导出混入演示数据
- Q031 生产配置拒绝 DEMO 批次：environment=production + DEMO 批次 → 拒
- 守卫：assert_same_dataset 在两端口径不一致时抛 DATASET_MIXED
- DataScopeValidator：未注册入口默认放行；已注册入口要求 PRODUCTION
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import DomainError
from app.core.security import hash_password
from app.models import (
    Brand,
    Contract,
    Merchant,
    Organization,
    PayrollRecord,
    PerformanceRecord,
    ProcurementOrder,
    Region,
    Role,
    Sku,
    User,
)
from app.models.enums import (
    RoleCode,
    UserStatus,
)
from app.models.seed import (
    SEED_STATUS_FAILED,
    SEED_STATUS_SUCCEEDED,
)
from app.services.data_scope_validator import (
    DataScopeValidator,
    ScopeRule,
    data_scope_validator,
    find_demo_merchant_with_production_contract,
    find_payroll_referencing_demo_performance,
    required_scope_for_role,
)
from app.services.demo_data_guard import (
    DataScope,
    assert_demo_disabled_in_production,
    assert_production_scope,
    assert_same_dataset,
    same_dataset,
)
from app.services.seed_check_service import SeedCheckService

# ===== 夹具 =====


@pytest.fixture
def system_user(session: Session) -> User:
    role = Role(
        code=f"t06-admin-{uuid4().hex[:6]}",
        name="T06 System Admin",
        role_code=RoleCode.SYSTEM_ADMIN.value,
        dataset_type="PRODUCTION",
        dataset_id=uuid4(),
    )
    session.add(role)
    session.flush()
    user = User(
        username=f"t06_admin_{uuid4().hex[:8]}",
        display_name="T06 System Admin",
        role_id=role.id,
        status=UserStatus.ACTIVE.value,
        must_change_password=False,
        password_hash=hash_password("t06-not-for-prod"),
        dataset_type="PRODUCTION",
        dataset_id=role.dataset_id,
    )
    session.add(user)
    session.flush()
    return user


def _make_brand(session: Session, *, name: str = "T06 品牌") -> Brand:
    brand = Brand(
        code=f"T06-B-{uuid4().hex[:6]}",
        name=name,
        dataset_type="PRODUCTION",
        dataset_id=uuid4(),
    )
    session.add(brand)
    session.flush()
    return brand


def _make_region(session: Session, *, name: str = "T06 片区") -> Region:
    org = Organization(
        code=f"T06-O-{uuid4().hex[:6]}",
        name="T06 Org",
        dataset_type="PRODUCTION",
        dataset_id=uuid4(),
    )
    session.add(org)
    session.flush()
    region = Region(
        code=f"T06-R-{uuid4().hex[:6]}",
        name=name,
        org_id=org.id,
        dataset_type="PRODUCTION",
        dataset_id=org.dataset_id,
    )
    session.add(region)
    session.flush()
    return region


def _make_merchant(
    session: Session,
    *,
    region: Region,
    brand: Brand,
    dataset_type: str = "PRODUCTION",
) -> Merchant:
    m = Merchant(
        code=f"T06-M-{uuid4().hex[:6]}",
        name=f"T06 商家-{dataset_type}",
        region_id=region.id,
        brand_id=brand.id,
        status="ACTIVE",
        dataset_type=dataset_type,
        dataset_id=uuid4(),
    )
    session.add(m)
    session.flush()
    return m


def _make_contract(
    session: Session,
    *,
    merchant: Merchant,
    dataset_type: str = "PRODUCTION",
) -> Contract:
    from datetime import date

    c = Contract(
        contract_no=f"T06-C-{uuid4().hex[:6]}",
        merchant_id=merchant.id,
        title="T06 种子合同",
        party_name="T06 甲方",
        effective_from=date(2026, 1, 1),
        expire_at=date(2026, 12, 31),
        status="DRAFT",
        display_threshold_pct=50,
        commission_rule_version="v1",
        dataset_type=dataset_type,
        dataset_id=uuid4(),
    )
    session.add(c)
    session.flush()
    return c


def _make_perf_record(
    session: Session, *, dataset_type: str, user_id: UUID
) -> PerformanceRecord:
    from datetime import date

    rec = PerformanceRecord(
        user_id=user_id,
        period_start=date(2026, 9, 1),
        period_end=date(2026, 9, 30),
        new_contracts_count=0,
        confirmed_sales_amount_fen=0,
        commission_fen=1000,
        deduction_fen=0,
        snapshot_json={},
        dataset_type=dataset_type,
        dataset_id=uuid4(),
    )
    session.add(rec)
    session.flush()
    return rec


def _make_payroll_record(
    session: Session, *, dataset_type: str, user_id: UUID
) -> PayrollRecord:
    from datetime import date

    rec = PayrollRecord(
        user_id=user_id,
        period_start=date(2026, 9, 1),
        period_end=date(2026, 9, 30),
        base_salary_fen=5000,
        performance_fen=0,
        allowance_fen=0,
        social_insurance_fen=0,
        tax_fen=0,
        adjustments_json={},
        status="DRAFT",
        dataset_type=dataset_type,
        dataset_id=uuid4(),
    )
    session.add(rec)
    session.flush()
    return rec


def _make_procurement(
    session: Session, *, dataset_type: str, sku_id: UUID
) -> ProcurementOrder:
    from datetime import date
    from decimal import Decimal

    rec = ProcurementOrder(
        code=f"T06-PO-{uuid4().hex[:6]}",
        sku_id=sku_id,
        supplier_name="T06 Supplier",
        quantity_cases=Decimal("10.00"),
        unit_price_fen=100,
        expected_date=date(2026, 12, 31),
        status="REQUESTED",
        dataset_type=dataset_type,
        dataset_id=uuid4(),
    )
    session.add(rec)
    session.flush()
    return rec


def _make_sku(session: Session, *, brand: Brand) -> Sku:
    rec = Sku(
        code=f"T06-SKU-{uuid4().hex[:6]}",
        name="T06 测试 SKU",
        brand_id=brand.id,
        capacity_ml=500,
        suggested_price_fen=300,
        case_spec=0,
        is_active=True,
        dataset_type="PRODUCTION",
        dataset_id=uuid4(),
    )
    session.add(rec)
    session.flush()
    return rec


# ===== Q029 空批次可运行 =====


def test_q029_empty_batch_runs_to_succeeded(session: Session, system_user: User) -> None:
    """Q029：空批次（PENDING/0 rows）执行后 SUCCEEDED，无负例。"""
    svc = SeedCheckService(session)
    batch = svc.create_empty_batch(submitted_by_id=system_user.id)
    session.flush()
    assert batch.status == "PENDING"
    assert batch.rows_total == 0

    result = svc.run_check(batch)
    session.refresh(result)
    assert result.status == SEED_STATUS_SUCCEEDED
    assert result.rows_rejected == 0
    findings = (result.negative_findings or {}).get("findings", [])
    assert findings == []


def test_q029_empty_batch_via_factory_dev(session: Session, system_user: User) -> None:
    """Q029 dev/test：build_empty_check_for_environment 在 dev → DEMO 批次。"""
    from app.services.seed_check_service import build_empty_check_for_environment

    batch = build_empty_check_for_environment(
        session, environment="dev", submitted_by_id=system_user.id
    )
    session.flush()
    assert batch.dataset_type == "DEMO"


# ===== Q030 三类跨域负例 =====


def test_q030_demo_merchant_with_production_contract(
    session: Session, system_user: User
) -> None:
    """Q030-1：演示商家 + 生产合同 → 负例命中。"""
    region = _make_region(session)
    brand = _make_brand(session)
    demo_merchant = _make_merchant(
        session, region=region, brand=brand, dataset_type="DEMO"
    )
    # 生产合同（与演示商家关联在此测试中仅做存在性断言：FK 暂允许不同 id）
    prod_merchant = _make_merchant(
        session, region=region, brand=brand, dataset_type="PRODUCTION"
    )
    _make_contract(session, merchant=prod_merchant, dataset_type="PRODUCTION")

    findings = find_demo_merchant_with_production_contract(session)
    assert findings, "应至少返回一条演示商家+生产合同证据"
    assert any(
        f["demo_merchant_id"] == str(demo_merchant.id) for f in findings
    )


def test_q030_production_payroll_referencing_demo_performance(
    session: Session, system_user: User
) -> None:
    """Q030-2：演示绩效 + 生产工资 → 负例命中。"""
    _make_perf_record(session, dataset_type="DEMO", user_id=system_user.id)
    _make_payroll_record(session, dataset_type="PRODUCTION", user_id=system_user.id)

    flagged = find_payroll_referencing_demo_performance(session)
    assert len(flagged) >= 1


def test_q030_async_export_demo_mixed(session: Session, system_user: User) -> None:
    """Q030-3：异步导出混入演示数据 → 负例命中。

    模拟演示商家 + 演示采购单混入正式表，触发 export.async 注册项。
    """
    from app.services.data_scope_validator import count_demo_records_in_production_scope

    # 演示商家（dataset_type=DEMO）
    region = _make_region(session)
    brand = _make_brand(session)
    _make_merchant(session, region=region, brand=brand, dataset_type="DEMO")
    # 演示采购单
    sku = _make_sku(session, brand=brand)
    _make_procurement(session, dataset_type="DEMO", sku_id=sku.id)

    counts = count_demo_records_in_production_scope(session)
    assert counts.get("merchants", 0) >= 1
    assert counts.get("procurement_orders", 0) >= 1

    # DataScopeValidator 强制 PRODUCTION
    with pytest.raises(DomainError) as excinfo:
        data_scope_validator.assert_production(
            key="export.async", scope=DataScope.ALL
        )
    assert excinfo.value.code == "EXPORT_ASYNC_SCOPE_DEMO_MIXED"


def test_q030_run_check_collects_all_three(session: Session, system_user: User) -> None:
    """Q030 端到端：run_check 一次同时收集三类负例（按 PENDING→FAILED）。"""
    region = _make_region(session)
    brand = _make_brand(session)
    _make_merchant(session, region=region, brand=brand, dataset_type="DEMO")
    prod_merchant = _make_merchant(
        session, region=region, brand=brand, dataset_type="PRODUCTION"
    )
    _make_contract(session, merchant=prod_merchant, dataset_type="PRODUCTION")
    _make_perf_record(session, dataset_type="DEMO", user_id=system_user.id)
    _make_payroll_record(session, dataset_type="PRODUCTION", user_id=system_user.id)
    sku = _make_sku(session, brand=brand)
    _make_procurement(session, dataset_type="DEMO", sku_id=sku.id)

    svc = SeedCheckService(session)
    batch = svc.create_empty_batch(submitted_by_id=system_user.id, dataset_type="DEMO")
    session.flush()
    result = svc.run_check(batch)
    session.refresh(result)
    assert result.status == SEED_STATUS_FAILED
    findings = (result.negative_findings or {}).get("findings", [])
    kinds = {f["kind"] for f in findings}
    assert (
        SeedCheckService.NEGATIVE_KIND_DEMO_MERCHANT_PROD_CONTRACT in kinds
    )
    assert (
        SeedCheckService.NEGATIVE_KIND_PROD_PAYROLL_DEMO_PERFORMANCE in kinds
    )
    assert SeedCheckService.NEGATIVE_KIND_EXPORT_DEMO_MIXED in kinds


def test_q030_run_check_succeeds_without_demo_data(
    session: Session, system_user: User
) -> None:
    """Q030 反向：仅生产数据 → SUCCEEDED。"""
    region = _make_region(session)
    brand = _make_brand(session)
    prod_merchant = _make_merchant(
        session, region=region, brand=brand, dataset_type="PRODUCTION"
    )
    _make_contract(session, merchant=prod_merchant, dataset_type="PRODUCTION")
    _make_perf_record(session, dataset_type="PRODUCTION", user_id=system_user.id)
    _make_payroll_record(session, dataset_type="PRODUCTION", user_id=system_user.id)

    svc = SeedCheckService(session)
    batch = svc.create_empty_batch(submitted_by_id=system_user.id, dataset_type="DEMO")
    session.flush()
    result = svc.run_check(batch)
    session.refresh(result)
    assert result.status == SEED_STATUS_SUCCEEDED
    assert result.rows_rejected == 0


# ===== Q031 生产配置拒绝 DEMO 批次 =====


def test_q031_production_blocks_demo_dataset(
    session: Session, system_user: User, monkeypatch
) -> None:
    """Q031：environment=production + dataset_type=DEMO → 拒。"""
    settings = get_settings()
    # 临时把环境改 production（不写文件）
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "production_dataset_lock", True)
    with pytest.raises(DomainError) as excinfo:
        assert_demo_disabled_in_production(dataset_type="DEMO")
    assert excinfo.value.code == "DEMO_BLOCKED_IN_PRODUCTION"

    # PRODUCTION 批次仍可创建
    svc = SeedCheckService(session)
    batch = svc.create_empty_batch(
        submitted_by_id=system_user.id, dataset_type="PRODUCTION"
    )
    session.flush()
    assert batch.dataset_type == "PRODUCTION"

    # DEMO 仍被拒
    with pytest.raises(DomainError) as excinfo:
        svc.create_empty_batch(submitted_by_id=system_user.id, dataset_type="DEMO")
    assert excinfo.value.code == "DEMO_BLOCKED_IN_PRODUCTION"


def test_q031_factory_in_production_returns_production(
    session: Session, system_user: User, monkeypatch
) -> None:
    """Q031：build_empty_check_for_environment 在 production 仅返回 PRODUCTION 批次。"""
    from app.services.seed_check_service import build_empty_check_for_environment

    settings = get_settings()
    monkeypatch.setattr(settings, "environment", "production")
    batch = build_empty_check_for_environment(
        session, environment="production", submitted_by_id=system_user.id
    )
    session.flush()
    assert batch.dataset_type == "PRODUCTION"


# ===== 守卫单元 =====


def test_demo_data_guard_same_dataset() -> None:
    a, b = uuid4(), uuid4()
    assert same_dataset(a_type="DEMO", a_id=a, b_type="DEMO", b_id=a) is True
    assert same_dataset(a_type="DEMO", a_id=a, b_type="PRODUCTION", b_id=a) is False
    assert same_dataset(a_type="DEMO", a_id=a, b_type="DEMO", b_id=b) is False


def test_demo_data_guard_assert_same_dataset_rejects_mix() -> None:
    a = uuid4()
    with pytest.raises(DomainError) as excinfo:
        assert_same_dataset(
            parent_type="DEMO",
            parent_id=a,
            child_type="PRODUCTION",
            child_id=a,
            relation="merchant.contract",
        )
    assert excinfo.value.code == "DATASET_MIXED"


def test_demo_data_guard_assert_production_scope() -> None:
    # ALL/DEMO 拒
    for bad in (DataScope.ALL, DataScope.DEMO):
        with pytest.raises(DomainError) as excinfo:
            assert_production_scope(scope=bad, where="export.async")
        assert excinfo.value.code == "DATA_SCOPE_NOT_PRODUCTION"
    # PRODUCTION 通过
    assert (
        assert_production_scope(scope=DataScope.PRODUCTION, where="export.async")
        is None
    )


def test_data_scope_validator_register_and_assert() -> None:
    """DataScopeValidator 自身：注册项要求 PRODUCTION；未注册项放行。"""
    v = DataScopeValidator()
    v.register(
        ScopeRule(key="custom.entry", code="CUSTOM_SCOPE", message="仅 PRODUCTION")
    )
    with pytest.raises(DomainError) as excinfo:
        v.assert_production(key="custom.entry", scope=DataScope.DEMO)
    assert excinfo.value.code == "CUSTOM_SCOPE"
    # 未注册入口放行
    v.assert_production(key="not.registered", scope=DataScope.ALL)
    # 重复注册报错
    with pytest.raises(ValueError):
        v.register(
            ScopeRule(key="custom.entry", code="CUSTOM_SCOPE", message="dup")
        )
    assert "custom.entry" in v.registered_keys()


def test_required_scope_for_role() -> None:
    assert required_scope_for_role(RoleCode.SALES_REP.value) == DataScope.PRODUCTION
    assert required_scope_for_role(RoleCode.FINANCE.value) == DataScope.PRODUCTION
    assert required_scope_for_role("UNKNOWN_ROLE") == DataScope.PRODUCTION


# ===== 路由/CLI 健壮性（不依赖 Excel 解析）=====


def test_seed_api_module_imports() -> None:
    from app.api.v1 import seed as api_seed

    assert hasattr(api_seed, "router")
    assert api_seed.router.prefix == "/api/v1/seed"


def test_cli_help() -> None:
    from app.cli import db as cli_db

    # help 退出码 0
    with pytest.raises(SystemExit) as excinfo:
        cli_db.main(["--help"])
    assert excinfo.value.code == 0


def test_cli_unknown_subcommand() -> None:
    from app.cli import db as cli_db

    with pytest.raises(SystemExit):
        cli_db.main(["not-a-cmd"])
