"""Text2SQL 纯逻辑测试：QueryPlan 模型 / 校验器 / 确定性编译器。

不依赖数据库；小 catalog 直接内存构造（覆盖各类错误路径），
真实 catalog 做端到端编译 smoke。
"""

from __future__ import annotations

from datetime import date
from uuid import uuid4

import pytest
from pydantic import ValidationError as PydanticValidationError

from app.semantic.catalog import (
    SemanticCatalog,
    SemanticColumn,
    SemanticDimension,
    SemanticEntity,
    SemanticMetric,
    SemanticRelation,
    get_default_catalog,
)
from app.text2sql import (
    Filter,
    FilterOp,
    MetricRef,
    DimensionRef,
    OrderBy,
    PlanCompileError,
    PlanValidationError,
    QueryContext,
    QueryPlan,
    RelativeRange,
    RowScope,
    TimeRange,
    compile_query,
    validate_plan,
)


# ---------------------------------------------------------------------------
# 小 catalog（内存构造，加载校验会同步跑）
# ---------------------------------------------------------------------------


def _mini_catalog() -> SemanticCatalog:
    return SemanticCatalog(
        version="1.0",
        name="mini",
        defaults={"default_row_limit": 50},
        entities=[
            SemanticEntity(
                name="merchant",
                table="merchants",
                region_column="region_id",
                brand_column="brand_id",
                columns=[
                    SemanticColumn(name="id", data_type="uuid"),
                    SemanticColumn(name="name", data_type="text"),
                    SemanticColumn(name="grade", data_type="text"),
                    SemanticColumn(name="region_id", data_type="uuid"),
                    SemanticColumn(name="brand_id", data_type="uuid"),
                    SemanticColumn(
                        name="contact_phone", data_type="text", sensitive=True
                    ),
                ],
                relations=[
                    SemanticRelation(
                        name="region",
                        target_entity="region",
                        source_column="region_id",
                    ),
                    SemanticRelation(
                        name="brand",
                        target_entity="brand",
                        source_column="brand_id",
                    ),
                ],
            ),
            SemanticEntity(
                name="sales_confirmation",
                table="sales_confirmations",
                time_column="biz_date",
                scope_via="merchant",
                columns=[
                    SemanticColumn(name="id", data_type="uuid"),
                    SemanticColumn(name="merchant_id", data_type="uuid"),
                    SemanticColumn(name="sku_id", data_type="uuid"),
                    SemanticColumn(name="biz_date", data_type="date"),
                    SemanticColumn(name="confirmed_boxes", data_type="numeric"),
                    SemanticColumn(name="amount_fen", data_type="bigint"),
                ],
                relations=[
                    SemanticRelation(
                        name="merchant",
                        target_entity="merchant",
                        source_column="merchant_id",
                    ),
                    SemanticRelation(
                        name="sku", target_entity="sku", source_column="sku_id"
                    ),
                ],
            ),
            SemanticEntity(
                name="region",
                table="regions",
                columns=[
                    SemanticColumn(name="id", data_type="uuid"),
                    SemanticColumn(name="name", data_type="text"),
                ],
            ),
            SemanticEntity(
                name="brand",
                table="brands",
                columns=[
                    SemanticColumn(name="id", data_type="uuid"),
                    SemanticColumn(name="name", data_type="text"),
                ],
            ),
            SemanticEntity(
                name="sku",
                table="skus",
                brand_column="brand_id",
                columns=[
                    SemanticColumn(name="id", data_type="uuid"),
                    SemanticColumn(name="name", data_type="text"),
                    SemanticColumn(name="brand_id", data_type="uuid"),
                ],
                relations=[
                    SemanticRelation(
                        name="brand",
                        target_entity="brand",
                        source_column="brand_id",
                    ),
                ],
            ),
            SemanticEntity(
                name="payroll_statement",
                table="payroll_statements",
                time_column="period_month",
                time_granularity="month",
                user_column="user_id",
                columns=[
                    SemanticColumn(name="id", data_type="uuid"),
                    SemanticColumn(name="user_id", data_type="uuid"),
                    SemanticColumn(name="period_month", data_type="text"),
                    SemanticColumn(name="gross_pay_fen", data_type="bigint"),
                ],
            ),
            SemanticEntity(
                name="isolated_thing",
                table="isolated_things",
                columns=[SemanticColumn(name="id", data_type="uuid")],
            ),
        ],
        dimensions=[
            SemanticDimension(name="region_name", entity="region", column="name"),
            SemanticDimension(
                name="merchant_grade", entity="merchant", column="grade"
            ),
            SemanticDimension(name="sku_name", entity="sku", column="name"),
        ],
        metrics=[
            SemanticMetric(
                name="confirmed_boxes_total",
                entity="sales_confirmation",
                expression="SUM(confirmed_boxes)",
                unit="box",
            ),
            SemanticMetric(
                name="confirmed_amount_total",
                entity="sales_confirmation",
                expression="SUM(amount_fen)",
                unit="money",
            ),
            SemanticMetric(
                name="avg_box_price",
                entity="sales_confirmation",
                expression=(
                    "ROUND(SUM(amount_fen)::numeric / "
                    "NULLIF(SUM(confirmed_boxes), 0), 2)"
                ),
                unit="money_per_box",
            ),
            SemanticMetric(
                name="gross_pay_total",
                entity="payroll_statement",
                expression="SUM(gross_pay_fen)",
                unit="money",
            ),
            SemanticMetric(
                name="isolated_count",
                entity="isolated_thing",
                expression="COUNT(id)",
                unit="count",
            ),
        ],
    )


@pytest.fixture(scope="module")
def catalog() -> SemanticCatalog:
    return _mini_catalog()


def _ctx(**kwargs) -> QueryContext:
    return QueryContext(dataset_id=uuid4(), **kwargs)


def _sales_plan(**overrides) -> QueryPlan:
    base = dict(
        entity="sales_confirmation",
        metrics=[MetricRef(name="confirmed_boxes_total")],
    )
    base.update(overrides)
    return QueryPlan(**base)


# ---------------------------------------------------------------------------
# QueryPlan 模型
# ---------------------------------------------------------------------------


class TestPlanModel:
    def test_limit_bounds(self) -> None:
        with pytest.raises(PydanticValidationError):
            QueryPlan(entity="x", metrics=[MetricRef(name="m")], limit=0)
        with pytest.raises(PydanticValidationError):
            QueryPlan(entity="x", metrics=[MetricRef(name="m")], limit=1001)

    def test_month_pattern(self) -> None:
        with pytest.raises(PydanticValidationError):
            TimeRange(kind="month", month="2026-13")
        TimeRange(kind="month", month="2026-09")  # 合法

    def test_extra_fields_rejected(self) -> None:
        with pytest.raises(PydanticValidationError):
            QueryPlan.model_validate(
                {"entity": "x", "raw_sql": "SELECT 1"}  # 模型试图塞 SQL
            )


# ---------------------------------------------------------------------------
# 校验器
# ---------------------------------------------------------------------------


class TestValidator:
    def _codes(self, exc: PlanValidationError) -> set[str]:
        return {issue.code for issue in exc.issues}

    def test_unknown_entity(self, catalog: SemanticCatalog) -> None:
        with pytest.raises(PlanValidationError) as err:
            validate_plan(QueryPlan(entity="nope", metrics=[]), catalog)
        assert "UNKNOWN_ENTITY" in self._codes(err.value)

    def test_unknown_metric_dimension_column(
        self, catalog: SemanticCatalog
    ) -> None:
        plan = _sales_plan(
            metrics=[MetricRef(name="nope_metric")],
            dimensions=[DimensionRef(name="nope_dim")],
            filters=[Filter(column="nope_col", op=FilterOp.EQ, value=1)],
        )
        with pytest.raises(PlanValidationError) as err:
            validate_plan(plan, catalog)
        assert {
            "UNKNOWN_METRIC",
            "UNKNOWN_DIMENSION",
            "UNKNOWN_COLUMN",
        } <= self._codes(err.value)

    def test_empty_select(self, catalog: SemanticCatalog) -> None:
        with pytest.raises(PlanValidationError) as err:
            validate_plan(QueryPlan(entity="sales_confirmation"), catalog)
        assert "EMPTY_SELECT" in self._codes(err.value)

    def test_filter_value_rules(self, catalog: SemanticCatalog) -> None:
        plan = _sales_plan(
            filters=[
                Filter(column="biz_date", op=FilterOp.IN, values=[]),
                Filter(column="biz_date", op=FilterOp.BETWEEN, values=[1, 2, 3]),
                Filter(column="biz_date", op=FilterOp.EQ, value=None),
            ]
        )
        with pytest.raises(PlanValidationError) as err:
            validate_plan(plan, catalog)
        assert err.value.issues[0].code == "INVALID_FILTER_VALUE"
        assert len(err.value.issues) == 3

    def test_sensitive_column_rejected(self, catalog: SemanticCatalog) -> None:
        plan = _sales_plan(
            filters=[
                Filter(
                    entity="merchant",
                    column="contact_phone",
                    op=FilterOp.EQ,
                    value="138",
                )
            ]
        )
        with pytest.raises(PlanValidationError) as err:
            validate_plan(plan, catalog)
        assert "SENSITIVE_COLUMN" in self._codes(err.value)

    def test_time_range_requires_time_column(
        self, catalog: SemanticCatalog
    ) -> None:
        plan = QueryPlan(
            entity="merchant",
            dimensions=[DimensionRef(name="merchant_grade")],
            time_range=TimeRange(
                kind="absolute", start=date(2026, 10, 1), end=date(2026, 10, 31)
            ),
        )
        with pytest.raises(PlanValidationError) as err:
            validate_plan(plan, catalog)
        assert "NO_TIME_COLUMN" in self._codes(err.value)

    def test_absolute_range_order(self, catalog: SemanticCatalog) -> None:
        plan = _sales_plan(
            time_range=TimeRange(
                kind="absolute", start=date(2026, 11, 1), end=date(2026, 10, 1)
            )
        )
        with pytest.raises(PlanValidationError) as err:
            validate_plan(plan, catalog)
        assert "INVALID_TIME_RANGE" in self._codes(err.value)

    def test_unknown_order_key(self, catalog: SemanticCatalog) -> None:
        plan = _sales_plan(order_by=[OrderBy(key="not_a_column")])
        with pytest.raises(PlanValidationError) as err:
            validate_plan(plan, catalog)
        assert "UNKNOWN_ORDER_KEY" in self._codes(err.value)

    def test_order_key_accepts_alias(self, catalog: SemanticCatalog) -> None:
        plan = _sales_plan(
            metrics=[MetricRef(name="confirmed_boxes_total", alias="销量")],
            order_by=[OrderBy(key="销量")],
        )
        validate_plan(plan, catalog)  # 不抛

    def test_unreachable_entity(self, catalog: SemanticCatalog) -> None:
        plan = _sales_plan(metrics=[MetricRef(name="isolated_count")])
        with pytest.raises(PlanValidationError) as err:
            validate_plan(plan, catalog)
        assert "ENTITY_NOT_REACHABLE" in self._codes(err.value)

    def test_join_derivation_two_hops(self, catalog: SemanticCatalog) -> None:
        """sc -> merchant -> region 两跳；join 步骤父先于子。"""
        plan = _sales_plan(dimensions=[DimensionRef(name="region_name")])
        resolved = validate_plan(plan, catalog)
        assert resolved.involved_entities == (
            "sales_confirmation",
            "merchant",
            "region",
        )
        joins = resolved.joins
        assert joins[0].to_entity == "merchant"
        assert joins[0].source_entity == "sales_confirmation"
        assert joins[0].target_entity == "merchant"
        assert joins[1].to_entity == "region"
        assert joins[1].source_entity == "merchant"
        assert joins[1].target_column == "id"

    def test_reverse_join_derivation(self, catalog: SemanticCatalog) -> None:
        """主实体 merchant、指标在 sales_confirmation：反向 join。"""
        plan = QueryPlan(
            entity="merchant",
            dimensions=[DimensionRef(name="merchant_grade")],
            metrics=[MetricRef(name="confirmed_boxes_total")],
        )
        resolved = validate_plan(plan, catalog)
        assert resolved.involved_entities == ("merchant", "sales_confirmation")
        step = resolved.joins[0]
        assert step.source_entity == "sales_confirmation"
        assert step.source_column == "merchant_id"
        assert step.target_entity == "merchant"

    def test_issues_are_llm_friendly(self, catalog: SemanticCatalog) -> None:
        plan = _sales_plan(metrics=[MetricRef(name="nope")])
        with pytest.raises(PlanValidationError) as err:
            validate_plan(plan, catalog)
        issue = err.value.issues[0]
        assert issue.path == "metrics[0].name"
        assert "nope" in issue.message


# ---------------------------------------------------------------------------
# 编译器
# ---------------------------------------------------------------------------


class TestCompiler:
    def test_single_entity_aggregate(self, catalog: SemanticCatalog) -> None:
        compiled = compile_query(validate_plan(_sales_plan(), catalog), _ctx())
        sql = compiled.sql
        assert 'SUM(t0."confirmed_boxes") AS "confirmed_boxes_total"' in sql
        assert 'FROM "sales_confirmations" t0' in sql
        # 数据集隔离只按 dataset_id：与财务总账/报表口径保持一致
        assert "t0.dataset_id = %(dataset_id)s" in sql
        assert "dataset_type" not in sql
        assert sql.rstrip().endswith("LIMIT 50")  # catalog default
        assert compiled.params["dataset_id"]
        assert compiled.columns[0].unit == "box"

    def test_dimension_group_by_two_hops(self, catalog: SemanticCatalog) -> None:
        plan = _sales_plan(dimensions=[DimensionRef(name="region_name")])
        compiled = compile_query(validate_plan(plan, catalog), _ctx())
        sql = compiled.sql
        assert 't2."name" AS "region_name"' in sql
        assert 'LEFT JOIN "merchants" t1 ON t0."merchant_id" = t1."id"' in sql
        assert 'LEFT JOIN "regions" t2 ON t1."region_id" = t2."id"' in sql
        assert 'GROUP BY t2."name"' in sql

    def test_reverse_join_on_clause(self, catalog: SemanticCatalog) -> None:
        plan = QueryPlan(
            entity="merchant",
            dimensions=[DimensionRef(name="merchant_grade")],
            metrics=[MetricRef(name="confirmed_boxes_total")],
        )
        compiled = compile_query(validate_plan(plan, catalog), _ctx())
        assert (
            'LEFT JOIN "sales_confirmations" t1 '
            'ON t1."merchant_id" = t0."id"' in compiled.sql
        )

    def test_filter_values_parameterized(self, catalog: SemanticCatalog) -> None:
        plan = _sales_plan(
            filters=[
                Filter(
                    entity="merchant",
                    column="name",
                    op=FilterOp.EQ,
                    value="全家便利店",
                )
            ]
        )
        compiled = compile_query(validate_plan(plan, catalog), _ctx())
        assert "全家便利店" not in compiled.sql
        assert "全家便利店" in compiled.params.values()
        assert any(
            't1."name" = %(f' in line for line in compiled.sql.splitlines()
        )

    def test_injection_string_stays_in_params(
        self, catalog: SemanticCatalog
    ) -> None:
        evil = "x'; DROP TABLE merchants; --"
        plan = _sales_plan(
            filters=[
                Filter(
                    entity="merchant", column="name", op=FilterOp.EQ, value=evil
                )
            ]
        )
        compiled = compile_query(validate_plan(plan, catalog), _ctx())
        assert "DROP TABLE" not in compiled.sql
        assert evil in compiled.params.values()

    def test_like_escapes_wildcards(self, catalog: SemanticCatalog) -> None:
        plan = _sales_plan(
            filters=[
                Filter(
                    entity="merchant",
                    column="name",
                    op=FilterOp.LIKE,
                    value="100%真_货",
                )
            ]
        )
        compiled = compile_query(validate_plan(plan, catalog), _ctx())
        assert r"%100\%真\_货%" in compiled.params.values()
        assert "ESCAPE '\\'" in compiled.sql

    def test_in_between_null_ops(self, catalog: SemanticCatalog) -> None:
        plan = _sales_plan(
            filters=[
                Filter(
                    entity="merchant",
                    column="grade",
                    op=FilterOp.IN,
                    values=["A", "B"],
                ),
                Filter(
                    column="confirmed_boxes",
                    op=FilterOp.BETWEEN,
                    values=[10, 100],
                ),
                Filter(column="sku_id", op=FilterOp.NOT_NULL),
            ]
        )
        compiled = compile_query(validate_plan(plan, catalog), _ctx())
        sql = compiled.sql
        assert '= ANY(%(f1)s)' in sql
        assert "BETWEEN %(f2)s AND %(f3)s" in sql
        assert 't0."sku_id" IS NOT NULL' in sql
        assert compiled.params["f1"] == ["A", "B"]

    def test_absolute_time_range_half_open(
        self, catalog: SemanticCatalog
    ) -> None:
        plan = _sales_plan(
            time_range=TimeRange(
                kind="absolute", start=date(2026, 10, 1), end=date(2026, 10, 22)
            )
        )
        compiled = compile_query(validate_plan(plan, catalog), _ctx())
        assert compiled.params["t_start"] == date(2026, 10, 1)
        assert compiled.params["t_end"] == date(2026, 10, 23)  # 左闭右开
        assert 't0."biz_date" >= %(t_start)s' in compiled.sql
        assert 't0."biz_date" < %(t_end)s' in compiled.sql

    def test_relative_last_month_injected_today(
        self, catalog: SemanticCatalog
    ) -> None:
        plan = _sales_plan(
            time_range=TimeRange(kind="relative", relative=RelativeRange.LAST_MONTH)
        )
        compiled = compile_query(
            validate_plan(plan, catalog), _ctx(today=date(2026, 10, 22))
        )
        assert compiled.params["t_start"] == date(2026, 9, 1)
        assert compiled.params["t_end"] == date(2026, 10, 1)

    def test_month_kind_on_date_entity(self, catalog: SemanticCatalog) -> None:
        plan = _sales_plan(time_range=TimeRange(kind="month", month="2026-12"))
        compiled = compile_query(validate_plan(plan, catalog), _ctx())
        assert compiled.params["t_start"] == date(2026, 12, 1)
        assert compiled.params["t_end"] == date(2027, 1, 1)  # 跨年

    def test_month_kind_on_month_entity(self, catalog: SemanticCatalog) -> None:
        plan = QueryPlan(
            entity="payroll_statement",
            metrics=[MetricRef(name="gross_pay_total")],
            time_range=TimeRange(kind="month", month="2026-09"),
        )
        compiled = compile_query(validate_plan(plan, catalog), _ctx())
        assert 't0."period_month" = %(t_month)s' in compiled.sql
        assert compiled.params["t_month"] == "2026-09"

    def test_absolute_range_on_month_entity_uses_yyyy_mm(
        self, catalog: SemanticCatalog
    ) -> None:
        plan = QueryPlan(
            entity="payroll_statement",
            metrics=[MetricRef(name="gross_pay_total")],
            time_range=TimeRange(
                kind="absolute", start=date(2026, 4, 15), end=date(2026, 9, 20)
            ),
        )
        compiled = compile_query(validate_plan(plan, catalog), _ctx())
        assert compiled.params["t_start"] == "2026-04"
        assert compiled.params["t_end"] == "2026-10"  # end+1day → '2026-10'
        assert 't0."period_month" >= %(t_start)s' in compiled.sql

    def test_scope_via_region_filter(self, catalog: SemanticCatalog) -> None:
        region_id = uuid4()
        ctx = _ctx(scope=RowScope(region_ids=(region_id,)))
        compiled = compile_query(validate_plan(_sales_plan(), catalog), ctx)
        sql = compiled.sql
        # 主实体无 region_column → scope_via 传递：join merchants 并过滤
        assert 'LEFT JOIN "merchants" t1 ON t0."merchant_id" = t1."id"' in sql
        assert 't1."region_id" = ANY(%(scope_region_ids)s::uuid[])' in sql
        assert compiled.params["scope_region_ids"] == [str(region_id)]
        assert compiled.applied_scope["region_ids"] == [str(region_id)]

    def test_scope_reuses_existing_join(self, catalog: SemanticCatalog) -> None:
        """维度已经 join merchant 时，scope 不重复 join。"""
        plan = _sales_plan(dimensions=[DimensionRef(name="merchant_grade")])
        ctx = _ctx(scope=RowScope(region_ids=(uuid4(),)))
        compiled = compile_query(validate_plan(plan, catalog), ctx)
        assert compiled.sql.count('LEFT JOIN "merchants"') == 1

    def test_scope_merchant_ids_on_merchant_entity(
        self, catalog: SemanticCatalog
    ) -> None:
        merchant_id = uuid4()
        plan = QueryPlan(
            entity="merchant", dimensions=[DimensionRef(name="merchant_grade")]
        )
        ctx = _ctx(scope=RowScope(merchant_ids=(merchant_id,)))
        compiled = compile_query(validate_plan(plan, catalog), ctx)
        assert 't0."id" = ANY(%(scope_merchant_ids)s::uuid[])' in compiled.sql

    def test_scope_brand_direct_column(self, catalog: SemanticCatalog) -> None:
        plan = QueryPlan(
            entity="merchant",
            metrics=[MetricRef(name="confirmed_boxes_total")],
        )
        ctx = _ctx(scope=RowScope(brand_ids=(uuid4(),)))
        compiled = compile_query(validate_plan(plan, catalog), ctx)
        assert 't0."brand_id" = ANY(%(scope_brand_ids)s::uuid[])' in compiled.sql

    def test_scope_empty_set_is_never_true(self, catalog: SemanticCatalog) -> None:
        """「有范围维度但集合为空」必须生成永假条件，不能退化为不限。"""
        ctx = _ctx(scope=RowScope(region_ids=()))
        compiled = compile_query(validate_plan(_sales_plan(), catalog), ctx)
        assert 'ANY(%(scope_region_ids)s::uuid[])' in compiled.sql
        assert compiled.params["scope_region_ids"] == []

    def test_from_merchant_scope_preserves_empty(self) -> None:
        class _FakeMerchantScope:
            is_global = False
            region_ids: tuple = ()
            merchant_ids = None

        scope = RowScope.from_merchant_scope(_FakeMerchantScope())
        assert scope.region_ids == ()
        assert scope.merchant_ids is None

    def test_scope_unsupported_raises(self, catalog: SemanticCatalog) -> None:
        """payroll 无 region/brand 列也无 scope_via：编译期明确拒绝。"""
        plan = QueryPlan(
            entity="payroll_statement",
            metrics=[MetricRef(name="gross_pay_total")],
        )
        ctx = _ctx(scope=RowScope(region_ids=(uuid4(),)))
        with pytest.raises(PlanCompileError, match="region_column"):
            compile_query(validate_plan(plan, catalog), ctx)

    def test_complex_metric_expression_qualified(
        self, catalog: SemanticCatalog
    ) -> None:
        plan = _sales_plan(metrics=[MetricRef(name="avg_box_price")])
        compiled = compile_query(validate_plan(plan, catalog), _ctx())
        assert (
            'ROUND(SUM(t0."amount_fen")::numeric / '
            'NULLIF(SUM(t0."confirmed_boxes"), 0), 2)' in compiled.sql
        )

    def test_dimension_only_uses_distinct(self, catalog: SemanticCatalog) -> None:
        plan = QueryPlan(
            entity="merchant", dimensions=[DimensionRef(name="merchant_grade")]
        )
        compiled = compile_query(validate_plan(plan, catalog), _ctx())
        assert compiled.sql.startswith("SELECT DISTINCT")
        assert "GROUP BY" not in compiled.sql

    def test_order_by_and_explicit_limit(self, catalog: SemanticCatalog) -> None:
        plan = _sales_plan(
            dimensions=[DimensionRef(name="region_name")],
            order_by=[OrderBy(key="confirmed_boxes_total")],
            limit=10,
        )
        compiled = compile_query(validate_plan(plan, catalog), _ctx())
        assert 'ORDER BY "confirmed_boxes_total" DESC NULLS LAST' in compiled.sql
        assert compiled.sql.rstrip().endswith("LIMIT 10")

    def test_no_scope_no_dataset_leak(self, catalog: SemanticCatalog) -> None:
        """非 dataset_scoped 实体不加数据集过滤（mini catalog 全 scoped，
        用真实 catalog 的 freezer_movement 验证见 smoke）。"""
        compiled = compile_query(validate_plan(_sales_plan(), catalog), _ctx())
        assert compiled.applied_scope == {}


# ---------------------------------------------------------------------------
# 真实 catalog 编译 smoke
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def real_catalog() -> SemanticCatalog:
    return get_default_catalog(refresh=True)


class TestRealCatalogSmoke:

    def test_sales_by_region_last_month(
        self, real_catalog: SemanticCatalog
    ) -> None:
        """「上个月各区域确认销量和销售额」全链路编译。"""
        plan = QueryPlan(
            question="上个月各区域的确认销量和销售额分别是多少",
            entity="sales_confirmation",
            metrics=[
                MetricRef(name="confirmed_sales_boxes"),
                MetricRef(name="confirmed_sales_amount"),
            ],
            dimensions=[DimensionRef(name="region_name")],
            time_range=TimeRange(
                kind="relative", relative=RelativeRange.LAST_MONTH
            ),
            order_by=[OrderBy(key="confirmed_sales_amount")],
        )
        resolved = validate_plan(plan, real_catalog)
        compiled = compile_query(resolved, _ctx(today=date(2026, 10, 22)))
        sql = compiled.sql
        assert 'FROM "sales_confirmations"' in sql
        assert '"merchants"' in sql and '"regions"' in sql
        assert 'SUM(t0."confirmed_boxes")' in sql
        assert 'SUM(t0."amount_fen")' in sql
        assert compiled.params["t_start"] == date(2026, 9, 1)
        assert {c.unit for c in compiled.columns} >= {"box", "money", None}

    def test_freezer_movement_no_dataset_filter(
        self, real_catalog: SemanticCatalog
    ) -> None:
        """freezer_movements 无数据集隔离列：编译产物不含其 dataset 条件。"""
        metric = next(
            m for m in real_catalog.metrics if m.entity == "freezer_movement"
        )
        plan = QueryPlan(
            entity="freezer_movement", metrics=[MetricRef(name=metric.name)]
        )
        compiled = compile_query(validate_plan(plan, real_catalog), _ctx())
        assert "dataset_id" not in compiled.sql
