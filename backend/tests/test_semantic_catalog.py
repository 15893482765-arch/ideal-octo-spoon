"""语义层 Catalog 测试：真实 YAML 可加载且口径完整，悬空引用能被校验拦截。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.semantic.catalog import (
    DEFAULT_CATALOG_PATH,
    SemanticCatalog,
    extract_identifiers,
    get_default_catalog,
    load_semantic_catalog,
)


@pytest.fixture(scope="module")
def catalog() -> SemanticCatalog:
    return load_semantic_catalog()


class TestDefaultCatalog:
    def test_yaml_file_exists(self) -> None:
        assert DEFAULT_CATALOG_PATH.exists()

    def test_load_and_summary(self, catalog: SemanticCatalog) -> None:
        summary = catalog.summary()
        assert summary["version"] == "1.0"
        assert summary["entities"] >= 15
        assert summary["metrics"] >= 30
        assert summary["dimensions"] >= 20
        assert summary["glossary"] >= 10

    def test_core_entities_present(self, catalog: SemanticCatalog) -> None:
        for name in (
            "sales_confirmation",
            "visit_task",
            "visit_record",
            "visit_sales_line",
            "freezer_asset",
            "freezer_movement",
            "merchant",
            "sku",
            "contract",
            "performance_snapshot",
            "payroll_statement",
            "expense",
            "user",
        ):
            assert catalog.get_entity(name).name == name

    def test_core_metrics_present(self, catalog: SemanticCatalog) -> None:
        for name in (
            "confirmed_sales_boxes",
            "confirmed_sales_amount",
            "valid_visit_count",
            "in_store_freezer_count",
            "gross_pay_amount",
            "net_performance_amount",
            "approved_expense_amount",
        ):
            assert catalog.get_metric(name).name == name

    def test_official_sales_caliber_entity(self, catalog: SemanticCatalog) -> None:
        """正式销量口径锚定 sales_confirmations，且经由商家做行级范围。"""
        entity = catalog.get_entity("sales_confirmation")
        assert entity.table == "sales_confirmations"
        assert entity.time_column == "biz_date"
        assert entity.dataset_scoped is True
        assert entity.scope_via == "merchant"
        assert entity.get_relation("merchant") is not None

    def test_freezer_movement_not_dataset_scoped(self, catalog: SemanticCatalog) -> None:
        """freezer_movements 无 DatasetMixin，语义层必须如实标注。"""
        assert catalog.get_entity("freezer_movement").dataset_scoped is False

    def test_money_metrics_use_fen_columns(self, catalog: SemanticCatalog) -> None:
        for metric in catalog.metrics:
            if metric.unit in ("money", "money_per_box"):
                assert "_fen" in metric.expression, (
                    f"金额类指标 {metric.name} 应基于 *_fen 列: {metric.expression}"
                )

    def test_month_granularity_entities(self, catalog: SemanticCatalog) -> None:
        for name in ("payroll_statement", "performance_snapshot"):
            entity = catalog.get_entity(name)
            assert entity.time_granularity == "month"
            assert entity.time_column == "period_month"

    def test_glossary_defines_official_calibers(self, catalog: SemanticCatalog) -> None:
        terms = {item.term for item in catalog.glossary}
        for term in ("销量", "金额单位", "有效巡访", "在店冰柜", "数据集隔离", "应发工资"):
            assert term in terms
        sales_term = next(item for item in catalog.glossary if item.term == "销量")
        assert "sales_confirmations" in sales_term.definition

    def test_get_missing_raises_key_error(self, catalog: SemanticCatalog) -> None:
        with pytest.raises(KeyError):
            catalog.get_entity("not_an_entity")
        with pytest.raises(KeyError):
            catalog.get_metric("not_a_metric")


class TestLookup:
    def test_find_metrics_by_synonym(self, catalog: SemanticCatalog) -> None:
        names = {m.name for m in catalog.find_metrics("销量")}
        assert "confirmed_sales_boxes" in names
        assert "confirmed_sales_amount" in names

    def test_find_entities_by_synonym(self, catalog: SemanticCatalog) -> None:
        names = {e.name for e in catalog.find_entities("门店")}
        assert "merchant" in names
        names = {e.name for e in catalog.find_entities("冰柜")}
        assert {"freezer_asset", "freezer_movement"} <= names

    def test_find_dimensions_by_synonym(self, catalog: SemanticCatalog) -> None:
        names = {d.name for d in catalog.find_dimensions("区域")}
        assert "region_name" in names

    def test_match_glossary_in_question(self, catalog: SemanticCatalog) -> None:
        hits = catalog.match_glossary("上个月各区域销量和毛利分别是多少")
        terms = {item.term for item in hits}
        assert {"销量", "毛利"} <= terms

    def test_empty_keyword_matches_nothing(self, catalog: SemanticCatalog) -> None:
        assert catalog.find_metrics("   ") == []


class TestValidation:
    """加载即校验：任何悬空引用都必须被拦截。"""

    @staticmethod
    def _base_payload() -> dict:
        return {
            "version": "1.0",
            "entities": [
                {
                    "name": "merchant",
                    "table": "merchants",
                    "primary_key": "id",
                    "columns": [
                        {"name": "id", "data_type": "uuid"},
                        {"name": "name", "data_type": "text"},
                    ],
                    "relations": [],
                }
            ],
            "metrics": [],
            "dimensions": [],
            "glossary": [],
        }

    def test_metric_with_unknown_entity_rejected(self) -> None:
        payload = self._base_payload()
        payload["metrics"] = [
            {
                "name": "bad_metric",
                "entity": "ghost",
                "expression": "COUNT(*)",
            }
        ]
        with pytest.raises(ValidationError, match="实体 'ghost' 不存在"):
            SemanticCatalog.model_validate(payload)

    def test_metric_expression_with_unknown_column_rejected(self) -> None:
        payload = self._base_payload()
        payload["metrics"] = [
            {
                "name": "bad_metric",
                "entity": "merchant",
                "expression": "SUM(revenue_fen)",
            }
        ]
        with pytest.raises(ValidationError, match="未定义标识符"):
            SemanticCatalog.model_validate(payload)

    def test_dimension_with_unknown_column_rejected(self) -> None:
        payload = self._base_payload()
        payload["dimensions"] = [
            {"name": "bad_dim", "entity": "merchant", "column": "ghost_col"}
        ]
        with pytest.raises(ValidationError, match="不在实体 'merchant' 的列定义中"):
            SemanticCatalog.model_validate(payload)

    def test_relation_with_unknown_target_rejected(self) -> None:
        payload = self._base_payload()
        payload["entities"][0]["relations"] = [
            {"name": "ghost_rel", "target_entity": "ghost", "source_column": "id"}
        ]
        with pytest.raises(ValidationError, match="目标实体 'ghost' 不存在"):
            SemanticCatalog.model_validate(payload)

    def test_duplicate_names_rejected(self) -> None:
        payload = self._base_payload()
        payload["entities"].append(dict(payload["entities"][0]))
        with pytest.raises(ValidationError, match="实体名称重复"):
            SemanticCatalog.model_validate(payload)

    def test_invalid_unit_rejected(self) -> None:
        payload = self._base_payload()
        payload["metrics"] = [
            {
                "name": "bad_unit",
                "entity": "merchant",
                "expression": "COUNT(*)",
                "unit": "yuan",
            }
        ]
        with pytest.raises(ValidationError, match="unit 'yuan' 非法"):
            SemanticCatalog.model_validate(payload)

    def test_time_column_must_exist(self) -> None:
        payload = self._base_payload()
        payload["entities"][0]["time_column"] = "biz_date"
        with pytest.raises(ValidationError, match="time_column 'biz_date' 不在列定义中"):
            SemanticCatalog.model_validate(payload)

    def test_scope_via_must_be_defined_relation(self) -> None:
        payload = self._base_payload()
        payload["entities"][0]["scope_via"] = "merchant"
        with pytest.raises(ValidationError, match="scope_via 指向未定义的关系"):
            SemanticCatalog.model_validate(payload)

    def test_glossary_related_metric_must_exist(self) -> None:
        payload = self._base_payload()
        payload["glossary"] = [
            {"term": "销量", "definition": "x", "related_metrics": ["ghost_metric"]}
        ]
        with pytest.raises(ValidationError, match="关联指标 'ghost_metric' 不存在"):
            SemanticCatalog.model_validate(payload)

    def test_minimal_valid_catalog(self) -> None:
        payload = self._base_payload()
        payload["metrics"] = [
            {
                "name": "merchant_count",
                "entity": "merchant",
                "expression": "COUNT(*) FILTER (WHERE name IS NOT NULL)",
                "unit": "count",
            }
        ]
        catalog = SemanticCatalog.model_validate(payload)
        assert catalog.get_metric("merchant_count").entity == "merchant"


class TestIdentifierExtraction:
    def test_string_literals_ignored(self) -> None:
        ids = extract_identifiers("COUNT(*) FILTER (WHERE status = 'ACTIVE')")
        assert ids == {"count", "filter", "where", "status"}

    def test_cast_type_extracted(self) -> None:
        ids = extract_identifiers("ROUND(SUM(amount_fen)::numeric / NULLIF(SUM(confirmed_boxes), 0), 2)")
        assert ids == {"round", "sum", "amount_fen", "numeric", "nullif", "confirmed_boxes"}

    def test_quoted_identifier_ignored(self) -> None:
        ids = extract_identifiers('SUM("weird col") + SUM(normal_col)')
        assert ids == {"sum", "normal_col"}


class TestDefaultCatalogCache:
    def test_get_default_catalog_cached(self) -> None:
        first = get_default_catalog(refresh=True)
        second = get_default_catalog()
        assert first is second
