"""Q008 PostGIS 200 米边界（geography 类型 + 距离查询）。"""

from __future__ import annotations

from geoalchemy2 import WKTElement
from sqlalchemy import text

# 上海人民广场附近两点（纬度 1°≈111 km，200 m 约 0.00180°）
SH_PEOPLE_SQUARE = (121.47370, 31.23040)
SH_NEAR_150M = (121.47370, 31.23215)
SH_NEAR_300M = (121.47370, 31.23310)


def _seed(session, code_prefix: str):
    from app.models.core import Brand, Organization, Region

    org = Organization(code=f"ORG-{code_prefix}", name="PostGIS 测试")
    brand = Brand(code=f"B-{code_prefix}", name="测试品牌")
    session.add_all([org, brand])
    session.flush()
    region = Region(code=f"R-{code_prefix}", name="上海", org_id=org.id)
    session.add(region)
    session.flush()
    return org, region, brand


def test_geography_distance_under_200m(session) -> None:
    """Q008：geography 类型 + 200 米以内 ST_DWithin 命中。"""
    from app.models import Merchant

    _, region, brand = _seed(session, "PG-A")
    m1 = Merchant(
        code="M-PG-A1",
        name="近点",
        region_id=region.id,
        brand_id=brand.id,
        location=WKTElement(f"POINT({SH_PEOPLE_SQUARE[0]} {SH_PEOPLE_SQUARE[1]})", srid=4326),
    )
    m2 = Merchant(
        code="M-PG-A2",
        name="中距点",
        region_id=region.id,
        brand_id=brand.id,
        location=WKTElement(f"POINT({SH_NEAR_150M[0]} {SH_NEAR_150M[1]})", srid=4326),
    )
    session.add_all([m1, m2])
    session.flush()
    sql = text(
        """
        SELECT ST_DWithin(a.location, b.location, :meters) AS within
        FROM merchants a, merchants b
        WHERE a.code = :a AND b.code = :b
        """
    )
    within = session.execute(sql, {"meters": 200, "a": "M-PG-A1", "b": "M-PG-A2"}).scalar()
    assert within is True


def test_geography_distance_over_200m(session) -> None:
    """Q008：超 200 m 不在边界内。"""
    from app.models import Merchant

    _, region, brand = _seed(session, "PG-B")
    m1 = Merchant(
        code="M-PG-B1",
        name="近点",
        region_id=region.id,
        brand_id=brand.id,
        location=WKTElement(f"POINT({SH_PEOPLE_SQUARE[0]} {SH_PEOPLE_SQUARE[1]})", srid=4326),
    )
    m2 = Merchant(
        code="M-PG-B2",
        name="远点",
        region_id=region.id,
        brand_id=brand.id,
        location=WKTElement(f"POINT({SH_NEAR_300M[0]} {SH_NEAR_300M[1]})", srid=4326),
    )
    session.add_all([m1, m2])
    session.flush()
    sql = text(
        """
        SELECT ST_DWithin(a.location, b.location, :meters) AS within
        FROM merchants a, merchants b
        WHERE a.code = :a AND b.code = :b
        """
    )
    within = session.execute(sql, {"meters": 200, "a": "M-PG-B1", "b": "M-PG-B2"}).scalar()
    assert within is False


def test_geography_point_round_trip(session) -> None:
    """Q008：geography 写入读出后坐标一致。"""
    from app.models import Merchant

    _, region, brand = _seed(session, "PG-C")
    m = Merchant(
        code="M-PG-C1",
        name="回环点",
        region_id=region.id,
        brand_id=brand.id,
        location=WKTElement(f"POINT({SH_PEOPLE_SQUARE[0]} {SH_PEOPLE_SQUARE[1]})", srid=4326),
    )
    session.add(m)
    session.flush()
    row = session.execute(
        text(
            "SELECT ST_X(location::geometry) AS lon, ST_Y(location::geometry) AS lat "
            "FROM merchants WHERE code='M-PG-C1'"
        )
    ).one()
    assert abs(row.lon - SH_PEOPLE_SQUARE[0]) < 1e-6
    assert abs(row.lat - SH_PEOPLE_SQUARE[1]) < 1e-6


def test_geography_column_type(session) -> None:
    """Q008：merchants.location / visit_records.location 均为 geography(Point,4326)。"""
    from sqlalchemy import inspect

    insp = inspect(session.get_bind())
    for tbl in ("merchants", "visit_records"):
        cols = {c["name"]: str(c["type"]).lower() for c in insp.get_columns(tbl)}
        assert "location" in cols
        assert "geography" in cols["location"]
        assert "point" in cols["location"] or "geometry" in cols["location"]
