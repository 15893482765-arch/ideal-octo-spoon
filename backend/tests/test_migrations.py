"""Q005 空库 migrate；Q006 downgrade/upgrade 演练。"""

from __future__ import annotations

import pytest
from sqlalchemy import inspect, text

EXPECTED_TABLES = {
    "organizations",
    "regions",
    "brands",
    "product_categories",
    "skus",
    "roles",
    "users",
    "user_region_brand_auths",
    "merchants",
    "merchant_contacts",
    "merchant_grade_frequencies",
    "merchant_assignments",
    "merchant_grade_changes",
    "contracts",
    "contract_versions",
    "freezer_assets",
    "freezer_movements",
    "visit_tasks",
    "visit_records",
    "replenishment_orders",
    "replenishment_confirms",
    "sales_confirmations",
    "schedules",
    "activities",
    "activity_sample_ledgers",
    "supervisor_issues",
    "procurement_orders",
    "procurement_deliveries",
    "finance_transactions",
    "salary_configs",
    "performance_records",
    "payroll_records",
    "approval_requests",
    "task_items",
    "audit_logs",
    "ai_logs",
    "import_batches",
    "risk_items",
    "idempotency_keys",
    "knowledge_chunks",
}


def test_all_tables_present(applied_engine) -> None:
    """Q005：空库 migrate 后全部业务表存在。"""
    insp = inspect(applied_engine)
    tables = set(insp.get_table_names())
    missing = EXPECTED_TABLES - tables
    assert not missing, f"缺少表: {missing}"


def test_alembic_version_at_head(applied_engine) -> None:
    with applied_engine.connect() as conn:
        version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    assert version, "alembic_version 应当记录当前 head"


def test_downgrade_then_upgrade(applied_engine) -> None:
    """Q006：先 downgrade base 再 upgrade head，验证迁移可重复执行。"""
    import os

    from alembic import command
    from alembic.config import Config

    # env.py 会用 settings.database_url_sync（读 DATABASE_URL/.env）覆盖
    # set_main_option 传入的 URL，必须同步把环境变量指到测试库，否则
    # downgrade/upgrade 会误作用于 dev 库。
    os.environ["DATABASE_URL"] = TEST_DATABASE_URL_STR().replace(
        "postgresql+psycopg2://", "postgresql+asyncpg://"
    )
    from app.core.config import get_settings

    get_settings.cache_clear()

    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", TEST_DATABASE_URL_STR())

    # 备份当前已存在的表清单（downgrade 后应被清空）
    insp = inspect(applied_engine)
    set(insp.get_table_names())

    command.downgrade(cfg, "base")
    insp = inspect(applied_engine)
    after_down = set(insp.get_table_names())
    # 业务表全部被删除（PostGIS 扩展表保留）
    business_tables_after_down = {t for t in after_down if t.startswith(tuple(EXPECTED_TABLES))}
    # 允许只剩下 alembic_version + 扩展表
    assert business_tables_after_down == set()

    command.upgrade(cfg, "head")
    insp = inspect(applied_engine)
    after_up = set(insp.get_table_names())
    assert EXPECTED_TABLES.issubset(after_up)


def TEST_DATABASE_URL_STR() -> str:
    import os

    return os.environ.get(
        "TEST_DATABASE_URL",
        "postgresql+psycopg2://qinshuang:change-me-strong-password@localhost:5432/qinshuang_test",
    )


@pytest.fixture(autouse=True)
def _set_db_url(monkeypatch: pytest.MonkeyPatch) -> None:
    # 让 conftest 的 applied_engine 共享同一 URL
    monkeypatch.setenv("TEST_DATABASE_URL", TEST_DATABASE_URL_STR())
