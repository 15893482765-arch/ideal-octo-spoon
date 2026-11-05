"""业务语义层 Catalog：加载 ``config/semantic_layer.yaml`` 并做引用完整性校验。

语义层是 Text2SQL 的唯一口径来源：Query Plan 只能引用这里定义过的
实体/指标/维度，编译器据此处确定性地生成参数化 SQL。加载即校验，
任何悬空引用（指标指向不存在的实体、表达式引用未定义列等）都会
在启动/同步阶段暴露，而不是等到查询时才失败。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, model_validator

BACKEND_DIR = Path(__file__).resolve().parents[2]
DEFAULT_CATALOG_PATH = BACKEND_DIR / "config" / "semantic_layer.yaml"

ALLOWED_UNITS = {"box", "money", "count", "ratio", "money_per_box"}
TIME_GRANULARITIES = {"date", "datetime", "month"}

# 时间分桶：维度可声明按日/周/月截断（date_trunc），用于「按月趋势」类问题。
TIME_BUCKETS = {"day", "week", "month"}
# 只有真正的日期/时间列才能做 date_trunc；已是 'YYYY-MM' 文本的 month 列不行。
_BUCKETABLE_COLUMN_TYPES = {"date", "datetime", "timestamp"}

# 指标表达式中允许出现的 SQL 函数/关键字/类型（小写），其余标识符必须
# 是该实体已定义的列名。
_SQL_KEYWORDS = {
    "sum", "count", "avg", "min", "max", "round", "coalesce", "nullif",
    "filter", "where", "case", "when", "then", "else", "end",
    "and", "or", "not", "in", "is", "null", "true", "false",
    "cast", "date_trunc", "extract", "distinct", "abs",
    "greatest", "least", "between", "like", "ilike", "asc", "desc",
    "numeric", "int", "integer", "bigint", "smallint", "text", "varchar",
    "date", "timestamp", "interval", "boolean",
}

_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_STRING_LITERAL_RE = re.compile(r"'(?:[^'\\]|\\.|'')*'")
_QUOTED_IDENT_RE = re.compile(r'"[^"]*"')


def extract_identifiers(expression: str) -> set[str]:
    """提取 SQL 表达式片段中的标识符（剔除字符串字面量与双引号引用）。"""
    cleaned = _STRING_LITERAL_RE.sub(" ", expression)
    cleaned = _QUOTED_IDENT_RE.sub(" ", cleaned)
    return {m.group(0).lower() for m in _IDENTIFIER_RE.finditer(cleaned)}


class SemanticColumn(BaseModel):
    name: str
    data_type: str = "text"
    description: str = ""
    sensitive: bool = False


class SemanticRelation(BaseModel):
    """实体间关系（join 路径）：source_column -> target_entity.target_column。"""

    name: str
    target_entity: str
    source_column: str
    target_column: str = "id"
    description: str = ""


class SemanticEntity(BaseModel):
    name: str
    table: str
    description: str = ""
    synonyms: list[str] = Field(default_factory=list)
    primary_key: str = "id"
    time_column: str | None = None
    time_granularity: str = "date"
    dataset_scoped: bool = True
    region_column: str | None = None
    brand_column: str | None = None
    user_column: str | None = None
    scope_via: str | None = None
    columns: list[SemanticColumn] = Field(default_factory=list)
    relations: list[SemanticRelation] = Field(default_factory=list)

    @property
    def column_names(self) -> set[str]:
        return {c.name for c in self.columns}

    @property
    def relation_names(self) -> set[str]:
        return {r.name for r in self.relations}

    def get_column(self, name: str) -> SemanticColumn | None:
        for column in self.columns:
            if column.name == name:
                return column
        return None

    def get_relation(self, name: str) -> SemanticRelation | None:
        for relation in self.relations:
            if relation.name == name:
                return relation
        return None


class SemanticMetric(BaseModel):
    """指标：expression 为基于所属实体列的 SQL 聚合片段（不带表前缀）。"""

    name: str
    entity: str
    expression: str
    description: str = ""
    unit: str = "count"
    synonyms: list[str] = Field(default_factory=list)


class SemanticDimension(BaseModel):
    name: str
    entity: str
    column: str
    description: str = ""
    synonyms: list[str] = Field(default_factory=list)
    # day/week/month：编译为 date_trunc(...) 后再 to_char，输出可直接展示的文本
    time_bucket: str | None = None


class GlossaryItem(BaseModel):
    term: str
    definition: str
    related_metrics: list[str] = Field(default_factory=list)


class SemanticCatalog(BaseModel):
    """语义层完整目录，加载时校验全部交叉引用。"""

    version: str = "1.0"
    name: str = ""
    defaults: dict[str, Any] = Field(default_factory=dict)
    entities: list[SemanticEntity] = Field(default_factory=list)
    dimensions: list[SemanticDimension] = Field(default_factory=list)
    metrics: list[SemanticMetric] = Field(default_factory=list)
    glossary: list[GlossaryItem] = Field(default_factory=list)

    # ---- 校验 ----

    @model_validator(mode="after")
    def _validate_references(self) -> "SemanticCatalog":
        errors: list[str] = []
        entity_map = _check_unique(self.entities, "实体", errors)
        metric_map = _check_unique(self.metrics, "指标", errors)
        _check_unique(self.dimensions, "维度", errors)
        _check_unique(self.glossary, "术语", errors, key=lambda g: g.term)

        for entity in self.entities:
            self._validate_entity(entity, entity_map, errors)
        for metric in self.metrics:
            self._validate_metric(metric, entity_map, errors)
        for dimension in self.dimensions:
            target = entity_map.get(dimension.entity)
            if target is None:
                errors.append(f"维度 {dimension.name}: 实体 '{dimension.entity}' 不存在")
            elif dimension.column not in target.column_names:
                errors.append(
                    f"维度 {dimension.name}: 列 '{dimension.column}' 不在实体 "
                    f"'{dimension.entity}' 的列定义中"
                )
            elif dimension.time_bucket is not None:
                self._validate_time_bucket(dimension, target, errors)
        for item in self.glossary:
            for related in item.related_metrics:
                if related not in metric_map:
                    errors.append(f"术语 {item.term}: 关联指标 '{related}' 不存在")

        if errors:
            raise ValueError("语义层校验失败:\n" + "\n".join(f"- {e}" for e in errors))
        return self

    @staticmethod
    def _validate_time_bucket(
        dimension: SemanticDimension, entity: SemanticEntity, errors: list[str]
    ) -> None:
        """time_bucket 只能取 day/week/month，且只能作用于真实日期列。"""
        if dimension.time_bucket not in TIME_BUCKETS:
            errors.append(
                f"维度 {dimension.name}: time_bucket '{dimension.time_bucket}' 非法，"
                f"允许 {sorted(TIME_BUCKETS)}"
            )
            return
        column = entity.get_column(dimension.column)
        data_type = (column.data_type if column is not None else "").lower()
        if data_type not in _BUCKETABLE_COLUMN_TYPES:
            errors.append(
                f"维度 {dimension.name}: 列 '{dimension.column}' 的类型是 "
                f"'{data_type or 'unknown'}'，不能做 {dimension.time_bucket} 分桶"
                f"（仅支持 {sorted(_BUCKETABLE_COLUMN_TYPES)}）"
            )

    @staticmethod
    def _validate_entity(
        entity: SemanticEntity,
        entity_map: dict[str, SemanticEntity],
        errors: list[str],
    ) -> None:
        cols = entity.column_names
        if entity.time_granularity not in TIME_GRANULARITIES:
            errors.append(
                f"实体 {entity.name}: time_granularity '{entity.time_granularity}' 非法，"
                f"允许 {sorted(TIME_GRANULARITIES)}"
            )
        for attr in ("time_column", "region_column", "brand_column", "user_column"):
            column = getattr(entity, attr)
            if column is not None and column not in cols:
                errors.append(f"实体 {entity.name}: {attr} '{column}' 不在列定义中")
        if entity.scope_via is not None and entity.scope_via not in entity.relation_names:
            errors.append(
                f"实体 {entity.name}: scope_via 指向未定义的关系 '{entity.scope_via}'"
            )
        for relation in entity.relations:
            if relation.source_column not in cols:
                errors.append(
                    f"实体 {entity.name} 关系 {relation.name}: "
                    f"源列 '{relation.source_column}' 不在列定义中"
                )
            target = entity_map.get(relation.target_entity)
            if target is None:
                errors.append(
                    f"实体 {entity.name} 关系 {relation.name}: "
                    f"目标实体 '{relation.target_entity}' 不存在"
                )
            elif relation.target_column not in target.column_names and (
                relation.target_column != target.primary_key
            ):
                errors.append(
                    f"实体 {entity.name} 关系 {relation.name}: 目标列 "
                    f"'{relation.target_column}' 不在实体 '{relation.target_entity}' 中"
                )

    @staticmethod
    def _validate_metric(
        metric: SemanticMetric,
        entity_map: dict[str, SemanticEntity],
        errors: list[str],
    ) -> None:
        target = entity_map.get(metric.entity)
        if target is None:
            errors.append(f"指标 {metric.name}: 实体 '{metric.entity}' 不存在")
            return
        if metric.unit not in ALLOWED_UNITS:
            errors.append(
                f"指标 {metric.name}: unit '{metric.unit}' 非法，允许 {sorted(ALLOWED_UNITS)}"
            )
        unknown = (
            extract_identifiers(metric.expression)
            - {c.lower() for c in target.column_names}
            - _SQL_KEYWORDS
        )
        if unknown:
            errors.append(
                f"指标 {metric.name}: 表达式引用未定义标识符 {sorted(unknown)}"
                f"（实体 '{metric.entity}' 的列）"
            )

    # ---- 索引视图 ----

    @property
    def entity_map(self) -> dict[str, SemanticEntity]:
        return {e.name: e for e in self.entities}

    @property
    def metric_map(self) -> dict[str, SemanticMetric]:
        return {m.name: m for m in self.metrics}

    @property
    def dimension_map(self) -> dict[str, SemanticDimension]:
        return {d.name: d for d in self.dimensions}

    # ---- 查询助手 ----

    def get_entity(self, name: str) -> SemanticEntity:
        try:
            return self.entity_map[name]
        except KeyError:
            raise KeyError(f"语义层不存在实体 '{name}'") from None

    def get_metric(self, name: str) -> SemanticMetric:
        try:
            return self.metric_map[name]
        except KeyError:
            raise KeyError(f"语义层不存在指标 '{name}'") from None

    def get_dimension(self, name: str) -> SemanticDimension:
        try:
            return self.dimension_map[name]
        except KeyError:
            raise KeyError(f"语义层不存在维度 '{name}'") from None

    def find_entities(self, keyword: str) -> list[SemanticEntity]:
        """按名称/表名/同义词/描述做最小可用匹配（Qdrant 检索前的兜底）。"""
        return [
            e
            for e in self.entities
            if _matches(keyword, e.name, e.table, e.description, *e.synonyms)
        ]

    def find_metrics(self, keyword: str) -> list[SemanticMetric]:
        return [
            m
            for m in self.metrics
            if _matches(keyword, m.name, m.description, *m.synonyms)
        ]

    def find_dimensions(self, keyword: str) -> list[SemanticDimension]:
        return [
            d
            for d in self.dimensions
            if _matches(keyword, d.name, d.description, *d.synonyms)
        ]

    def match_glossary(self, text: str) -> list[GlossaryItem]:
        """返回术语出现在给定文本中的全部词条（用于上下文注入）。"""
        return [g for g in self.glossary if g.term in text]

    def summary(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "entities": len(self.entities),
            "dimensions": len(self.dimensions),
            "metrics": len(self.metrics),
            "glossary": len(self.glossary),
            "tables": sorted({e.table for e in self.entities}),
        }


def _matches(keyword: str, *candidates: str) -> bool:
    keyword = keyword.strip().lower()
    if not keyword:
        return False
    return any(keyword in c.lower() for c in candidates if c)


def _check_unique(
    items: list[Any],
    label: str,
    errors: list[str],
    key=lambda item: item.name,
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for item in items:
        item_key = key(item)
        if item_key in result:
            errors.append(f"{label}名称重复: '{item_key}'")
        result[item_key] = item
    return result


def load_semantic_catalog(path: str | Path | None = None) -> SemanticCatalog:
    """加载语义层 YAML 并返回校验通过的 Catalog；校验失败抛 ValueError。"""
    catalog_path = Path(path) if path is not None else DEFAULT_CATALOG_PATH
    if not catalog_path.exists():
        raise FileNotFoundError(f"语义层文件不存在: {catalog_path}")
    data = yaml.safe_load(catalog_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"语义层文件格式错误（应为 YAML 对象）: {catalog_path}")
    return SemanticCatalog.model_validate(data)


_CACHED_CATALOG: SemanticCatalog | None = None


def get_default_catalog(*, refresh: bool = False) -> SemanticCatalog:
    """进程内缓存的默认 Catalog（生产语义层只读，允许缓存）。"""
    global _CACHED_CATALOG
    if _CACHED_CATALOG is None or refresh:
        _CACHED_CATALOG = load_semantic_catalog()
    return _CACHED_CATALOG
