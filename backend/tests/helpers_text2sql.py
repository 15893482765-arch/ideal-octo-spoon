"""Text2SQL 测试共享造数：commit 落测试库，独立 dataset_id 天然隔离。

执行器走独立 raw connection（读不到外层事务未提交数据），所以这里
的数据必须 commit——与 conftest 中 bootstrap_admin/all_roles 的惯例一致。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import uuid4

from sqlalchemy.orm import Session
from sqlalchemy import text

from app.models import Merchant, Region, SalesConfirmation, Sku
from app.models.enums import RoleCode
from tests.test_t09_contract import _user, _world


def seed_sales_world(session: Session) -> dict:
    """造：华东/华南两区域、两商家、一个 SKU、若干 9 月/10 月销售确认单。

    返回 dict 含 ds（数据集 id）、region/region2/brand、confirmer、
    m_east/m_south、sku。数据含一条异 dataset 销售单（隔离验证用）。
    调用方负责 commit。
    """
    w = _world(session)
    ds = uuid4()
    w["ds"] = ds
    for obj in (w["brand"], w["region"]):
        obj.dataset_id = ds
    session.execute(
        text("UPDATE organizations SET dataset_id = :ds WHERE id = :oid"),
        {"ds": ds, "oid": w["region"].org_id},
    )
    region2 = Region(
        code=f"reg2-{uuid4().hex[:8]}",
        name="华南",
        org_id=w["region"].org_id,
        dataset_type="PRODUCTION",
        dataset_id=ds,
    )
    session.add(region2)
    w["region2"] = region2

    confirmer = _user(session, ds, RoleCode.FINANCE, "t2sql-fin")
    m_east = Merchant(
        code=f"M-{uuid4().hex[:8]}",
        name="华东一店",
        status="ACTIVE",
        grade="A",
        region_id=w["region"].id,
        brand_id=w["brand"].id,
        dataset_type="PRODUCTION",
        dataset_id=ds,
    )
    m_south = Merchant(
        code=f"M-{uuid4().hex[:8]}",
        name="华南一店",
        status="ACTIVE",
        grade="B",
        region_id=region2.id,
        brand_id=w["brand"].id,
        dataset_type="PRODUCTION",
        dataset_id=ds,
    )
    sku = Sku(
        code=f"SKU-{uuid4().hex[:8]}",
        name="500ml 冰红茶",
        brand_id=w["brand"].id,
        dataset_type="PRODUCTION",
        dataset_id=ds,
    )
    session.add_all([m_east, m_south, sku])
    session.flush()

    def sale(merchant: Merchant, day: date, boxes: str, fen: int, ds_id) -> None:
        session.add(
            SalesConfirmation(
                merchant_id=merchant.id,
                sku_id=sku.id,
                biz_date=day,
                confirmed_boxes=Decimal(boxes),
                amount_fen=fen,
                confirmer_id=confirmer.id,
                dataset_type="PRODUCTION",
                dataset_id=ds_id,
            )
        )

    sale(m_east, date(2026, 9, 5), "10", 100000, ds)
    sale(m_east, date(2026, 9, 20), "5", 50000, ds)
    sale(m_south, date(2026, 9, 8), "7", 77777, ds)
    sale(m_east, date(2026, 10, 2), "99", 990000, ds)  # 本月，不计入「上个月」
    alien_ds = uuid4()
    sale(m_east, date(2026, 9, 6), "1000", 1, alien_ds)  # 异数据集，不可见

    w.update(
        confirmer=confirmer,
        m_east=m_east,
        m_south=m_south,
        sku=sku,
        alien_ds=alien_ds,
    )
    return w


# ---------------------------------------------------------------------------
# 清理：commit 数据必须用完即删（test_constraints 等用例假设全表单行）
# ---------------------------------------------------------------------------

_USERNAME_PREFIX = "t2sql-%"


def purge_sales_world(engine, w: dict) -> None:
    """按 dataset_id + 用户名前缀删除 seed 产生的一切行（含审计/会话）。"""
    ds_ids = [str(w["ds"]), str(w["alien_ds"])]
    user_subquery = "SELECT id FROM users WHERE username LIKE 't2sql-%'"
    statements = [
        f"DELETE FROM ai_interaction_logs WHERE user_id IN ({user_subquery})",
        "DELETE FROM assistant_messages WHERE session_id IN "
        f"(SELECT id FROM assistant_sessions WHERE user_id IN ({user_subquery}))",
        f"DELETE FROM assistant_sessions WHERE user_id IN ({user_subquery})",
        f"DELETE FROM user_region_brand_auths WHERE user_id IN ({user_subquery})",
        "DELETE FROM sales_confirmations WHERE dataset_id = ANY(CAST(:ds AS uuid[]))",
        "DELETE FROM skus WHERE dataset_id = ANY(CAST(:ds AS uuid[]))",
        "DELETE FROM merchants WHERE dataset_id = ANY(CAST(:ds AS uuid[]))",
        "DELETE FROM users WHERE username LIKE 't2sql-%'",
        "DELETE FROM roles WHERE dataset_id = ANY(CAST(:ds AS uuid[]))",
        "DELETE FROM regions WHERE dataset_id = ANY(CAST(:ds AS uuid[]))",
        "DELETE FROM brands WHERE dataset_id = ANY(CAST(:ds AS uuid[]))",
        "DELETE FROM organizations WHERE dataset_id = ANY(CAST(:ds AS uuid[]))",
    ]
    with engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement), {"ds": ds_ids})
