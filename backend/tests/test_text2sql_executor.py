"""Text2SQL 只读执行器端到端测试（真实 PostgreSQL）。

执行器走独立 raw connection（读不到外层事务未提交数据），因此造数
直接 commit 落测试库（helpers_text2sql.seed_sales_world），独立
dataset_id 天然隔离。
"""

from __future__ import annotations

from datetime import date
from uuid import uuid4

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.semantic.catalog import get_default_catalog
from app.text2sql import (
    CompiledQuery,
    ExecutionRejectedError,
    MetricRef,
    DimensionRef,
    QueryContext,
    QueryPlan,
    QueryTimeoutError,
    ReadOnlyExecutor,
    RelativeRange,
    RowScope,
    TimeRange,
    compile_query,
    validate_plan,
)
from tests.helpers_text2sql import purge_sales_world, seed_sales_world

# ---------------------------------------------------------------------------
# 造数（commit，dataset 隔离）
# ---------------------------------------------------------------------------


@pytest.fixture
def world(engine: Engine):
    """造数 commit（raw connection 需要可见），收尾按 dataset 清理——
    commit 数据不能残留（test_constraints 等用例假设全表单行）。"""
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    session: Session = factory()
    try:
        w = seed_sales_world(session)
        session.commit()
        yield w
    finally:
        session.close()
        purge_sales_world(engine, w)


# ---------------------------------------------------------------------------
# 端到端
# ---------------------------------------------------------------------------


def _executor(engine: Engine, **kwargs) -> ReadOnlyExecutor:
    kwargs.setdefault("statement_timeout_ms", 5000)
    return ReadOnlyExecutor(engine, **kwargs)


def _sales_by_region_plan() -> QueryPlan:
    return QueryPlan(
        question="上个月各区域确认销量和销售额",
        entity="sales_confirmation",
        metrics=[
            MetricRef(name="confirmed_sales_boxes"),
            MetricRef(name="confirmed_sales_amount"),
        ],
        dimensions=[DimensionRef(name="region_name")],
        time_range=TimeRange(kind="relative", relative=RelativeRange.LAST_MONTH),
        order_by=[],
    )


class TestEndToEnd:
    def test_sales_by_region_last_month(
        self, engine: Engine, world: dict
    ) -> None:
        catalog = get_default_catalog()
        ctx = QueryContext(dataset_id=world["ds"], today=date(2026, 10, 22))
        compiled = compile_query(
            validate_plan(_sales_by_region_plan(), catalog), ctx
        )
        result = _executor(engine).execute(compiled)

        assert result.columns == [
            "region_name",
            "confirmed_sales_boxes",
            "confirmed_sales_amount",
        ]
        by_region = {row["region_name"]: row for row in result.rows}
        assert set(by_region) == {"华东", "华南"}
        # 华东：9 月两条（10+5 箱，150000 分）；10 月与异数据集不计入
        assert by_region["华东"]["confirmed_sales_boxes"] == 15.0
        assert by_region["华东"]["confirmed_sales_amount"] == 150000
        assert by_region["华南"]["confirmed_sales_boxes"] == 7.0
        assert not result.truncated
        assert result.elapsed_ms >= 0

    def test_dataset_isolation(self, engine: Engine, world: dict) -> None:
        """换用异 dataset_id 执行：一行都查不到。"""
        catalog = get_default_catalog()
        ctx = QueryContext(dataset_id=uuid4(), today=date(2026, 10, 22))
        compiled = compile_query(
            validate_plan(_sales_by_region_plan(), catalog), ctx
        )
        result = _executor(engine).execute(compiled)
        assert result.rows == []

    def test_row_scope_region(self, engine: Engine, world: dict) -> None:
        """区域经理范围：只见本片区聚合。"""
        catalog = get_default_catalog()
        ctx = QueryContext(
            dataset_id=world["ds"],
            scope=RowScope(region_ids=(world["region"].id,)),
            today=date(2026, 10, 22),
        )
        compiled = compile_query(
            validate_plan(_sales_by_region_plan(), catalog), ctx
        )
        result = _executor(engine).execute(compiled)
        assert [row["region_name"] for row in result.rows] == ["华东"]


# ---------------------------------------------------------------------------
# 执行器防线
# ---------------------------------------------------------------------------


def _raw_query(sql: str, params: dict | None = None) -> CompiledQuery:
    return CompiledQuery(
        sql=sql, params=params or {}, columns=[], limit=100, primary_entity="raw"
    )


class TestExecutorGuards:
    def test_read_only_rejects_write(self, engine: Engine) -> None:
        evil = _raw_query("UPDATE regions SET name = 'x' WHERE false")
        with pytest.raises(ExecutionRejectedError):
            _executor(engine).execute(evil)

    def test_statement_timeout(self, engine: Engine) -> None:
        slow = _raw_query("SELECT pg_sleep(1)")
        with pytest.raises(QueryTimeoutError):
            _executor(engine, statement_timeout_ms=100).execute(slow)

    def test_max_rows_truncation(self, engine: Engine) -> None:
        wide = _raw_query("SELECT generate_series(1, 5) AS n")
        result = _executor(engine, max_rows=2).execute(wide)
        assert result.truncated
        assert result.row_count == 2
        assert result.rows == [{"n": 1}, {"n": 2}]

    def test_jsonable_values(self, engine: Engine) -> None:
        query = _raw_query(
            "SELECT 1::numeric(12,2) AS dec, "
            "DATE '2026-10-22' AS d, 150000::bigint AS fen"
        )
        result = _executor(engine).execute(query)
        row = result.rows[0]
        assert row["dec"] == 1.0 and isinstance(row["dec"], float)
        assert row["d"] == "2026-10-22"
        assert row["fen"] == 150000 and isinstance(row["fen"], int)

    def test_explain_estimate(self, engine: Engine) -> None:
        estimate = _executor(engine).explain_estimate(
            _raw_query("SELECT 1 AS one")
        )
        assert estimate is None or isinstance(estimate, int)

    def test_result_carries_audit_fields(self, engine: Engine) -> None:
        result = _executor(engine).execute(_raw_query("SELECT 1 AS one"))
        assert result.sql == "SELECT 1 AS one"
        assert result.primary_entity == "raw"
