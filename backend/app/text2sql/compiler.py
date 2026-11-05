"""确定性 SQL 编译器：ResolvedPlan -> 参数化 SQL（无 LLM 参与）。

安全红线：
- 标识符（表/列/别名）全部来自语义层白名单，经双引号包裹；
- 所有取值（过滤值/时间/范围 ID/dataset_id）一律 psycopg2
  pyformat 命名参数 ``%(name)s``，用户输入永远不进 SQL 文本；
- 唯一内嵌的非常量值是 LIMIT 整数（Pydantic 已校验 int 范围）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from app.semantic.catalog import SemanticEntity, extract_identifiers

from .errors import PlanCompileError
from .plan import FilterOp, RelativeRange
from .scope import QueryContext
from .validator import JoinStep, ResolvedPlan

# 时间分桶的输出格式（白名单常量，不来自用户输入）
_BUCKET_FORMATS = {
    "day": "YYYY-MM-DD",
    "week": 'IYYY-"W"IW',
    "month": "YYYY-MM",
}


@dataclass(frozen=True)
class OutputColumn:
    name: str
    kind: str  # "metric" | "dimension"
    unit: str | None
    description: str
    # 维度的时间粒度（day/week/month）；非空表示这一列是分桶后的时间序列
    bucket: str | None = None


@dataclass
class CompiledQuery:
    """编译产物：可直接喂给只读执行器，也可入库审计。"""

    sql: str
    params: dict[str, Any]
    columns: list[OutputColumn]
    limit: int
    primary_entity: str
    question: str = ""
    applied_scope: dict[str, Any] = field(default_factory=dict)


def compile_query(resolved: ResolvedPlan, context: QueryContext) -> CompiledQuery:
    compiler = _Compiler(resolved, context)
    return compiler.compile()


class _Compiler:
    def __init__(self, resolved: ResolvedPlan, context: QueryContext) -> None:
        self._r = resolved
        self._ctx = context
        self._params: dict[str, Any] = {}
        self._seq = 0
        # 主实体 t0，其余按 join 顺序 t1..tN
        self._alias: dict[str, str] = {
            name: f"t{i}" for i, name in enumerate(resolved.involved_entities)
        }
        self._entity: dict[str, SemanticEntity] = {
            e.name: e for e in resolved.catalog.entities
        }
        # 行级范围可能引入额外 join（如 scope_via），在此追加
        self._extra_joins: list = []

    # ---- 主流程 ----

    def compile(self) -> CompiledQuery:
        select_sql, columns = self._build_select()
        scope_conditions = self._build_scope_conditions()
        where_parts = self._build_dataset_conditions()
        where_parts.extend(scope_conditions)
        time_sql = self._build_time_condition()
        if time_sql:
            where_parts.append(time_sql)
        where_parts.extend(self._build_filter_conditions())

        distinct = "DISTINCT " if self._r.dimensions and not self._r.metrics else ""
        lines = [f"SELECT {distinct}{select_sql}", self._build_from()]
        where_parts = [p for p in where_parts if p]
        if where_parts:
            lines.append("WHERE " + "\n  AND ".join(where_parts))
        group_sql = self._build_group_by()
        if group_sql:
            lines.append(group_sql)
        order_sql = self._build_order_by(columns)
        if order_sql:
            lines.append(order_sql)
        limit = self._resolve_limit()
        lines.append(f"LIMIT {limit}")

        return CompiledQuery(
            sql="\n".join(lines),
            params=self._params,
            columns=columns,
            limit=limit,
            primary_entity=self._r.primary.name,
            question=self._r.plan.question,
            applied_scope=self._scope_audit(),
        )

    # ---- SELECT ----

    def _build_select(self) -> tuple[str, list[OutputColumn]]:
        parts: list[str] = []
        columns: list[OutputColumn] = []
        for ref, dimension in self._r.dimensions:
            alias = ref.alias or dimension.name
            parts.append(f'{self._dimension_sql(dimension)} AS "{alias}"')
            columns.append(
                OutputColumn(
                    name=alias,
                    kind="dimension",
                    unit=None,
                    description=dimension.description,
                    bucket=dimension.time_bucket,
                )
            )
        for ref, metric in self._r.metrics:
            alias = ref.alias or metric.name
            expression = self._qualify_metric_expression(
                metric.expression, metric.entity
            )
            parts.append(f'{expression} AS "{alias}"')
            columns.append(
                OutputColumn(
                    name=alias,
                    kind="metric",
                    unit=metric.unit,
                    description=metric.description,
                )
            )
        return ", ".join(parts), columns

    def _dimension_sql(self, dimension) -> str:
        """维度表达式：普通列直出；带 time_bucket 的转成可读文本（YYYY-MM 等）。

        统一用 to_char 输出文本，既有两个好处：分类轴可直接展示，
        且 'YYYY-MM' / 'YYYY-MM-DD' 的字典序与时间序一致（ORDER BY 别名即可）。
        """
        table_alias = self._alias[dimension.entity]
        column = f'{table_alias}."{dimension.column}"'
        bucket = dimension.time_bucket
        if not bucket:
            return column
        fmt = _BUCKET_FORMATS[bucket]
        return f"to_char(date_trunc('{bucket}', {column}), '{fmt}')"

    def _qualify_metric_expression(self, expression: str, entity_name: str) -> str:
        """把指标表达式中的裸列名替换为 别名."列名"（列名来自语义层白名单）。"""
        entity = self._entity[entity_name]
        table_alias = self._alias[entity_name]
        used = extract_identifiers(expression) & {
            c.lower() for c in entity.column_names
        }
        result = expression
        for column in sorted(used, key=len, reverse=True):
            result = re.sub(
                rf"\b{re.escape(column)}\b",
                f'{table_alias}."{column}"',
                result,
                flags=re.IGNORECASE,
            )
        return result

    # ---- FROM / JOIN ----

    def _build_from(self) -> str:
        primary = self._r.primary
        lines = [f'FROM "{primary.table}" {self._alias[primary.name]}']
        for step in [*self._r.joins, *self._extra_joins]:
            target = self._entity[step.to_entity]
            # ON：定义侧实体.source_column = 目标实体.target_column
            # （反向 join 时目标实体是已加入的一侧，不能用 to_entity）
            source_alias = self._alias[step.source_entity]
            target_alias = self._alias[step.target_entity]
            lines.append(
                f'LEFT JOIN "{target.table}" {self._alias[step.to_entity]} '
                f'ON {source_alias}."{step.source_column}" = '
                f'{target_alias}."{step.target_column}"'
            )
        return "\n".join(lines)

    def _ensure_joined(self, entity_name: str) -> str:
        """确保实体已 join（行级范围需要），返回其表别名。"""
        if entity_name in self._alias:
            return self._alias[entity_name]
        # 主实体 scope_via relation 直达目标
        primary = self._r.primary
        relation = primary.get_relation(primary.scope_via or "")
        if relation is None or relation.target_entity != entity_name:
            raise PlanCompileError(
                f"主实体 '{primary.name}' 无法经 scope_via 连接到 '{entity_name}'"
            )
        alias = f"t{len(self._alias)}"
        self._alias[entity_name] = alias
        self._extra_joins.append(
            JoinStep(
                from_entity=primary.name,
                to_entity=entity_name,
                relation_name=relation.name,
                source_entity=primary.name,
                source_column=relation.source_column,
                target_entity=entity_name,
                target_column=relation.target_column,
            )
        )
        return alias

    # ---- WHERE：数据集隔离 ----

    def _build_dataset_conditions(self) -> list[str]:
        """数据集隔离：只按 ``dataset_id``（租户主键）。

        不按 ``dataset_type`` 过滤——它只是数据来源标记，同一 dataset_id 下
        可能同时存在 PRODUCTION 与 DEMO 行（播种遗留），而总账、报表等模块
        一律只按 dataset_id 隔离。多一层 type 条件会让问数结果与财务总账
        对不上号（收入/支出各差那部分 PRODUCTION 行）。
        """
        parts: list[str] = []
        self._params["dataset_id"] = str(self._ctx.dataset_id)
        for name in self._alias:
            entity = self._entity[name]
            if not entity.dataset_scoped:
                continue
            alias = self._alias[name]
            parts.append(f"{alias}.dataset_id = %(dataset_id)s::uuid")
        return parts

    # ---- WHERE：行级范围 ----

    def _build_scope_conditions(self) -> list[str]:
        scope = self._ctx.scope
        if scope is None or scope.is_global:
            return []
        parts: list[str] = []
        if scope.merchant_ids is not None:
            alias = self._merchant_scope_alias()
            self._params["scope_merchant_ids"] = [
                str(v) for v in scope.merchant_ids
            ]
            parts.append(f'{alias}."id" = ANY(%(scope_merchant_ids)s::uuid[])')
        if scope.region_ids is not None:
            alias, column = self._scope_column("region_column")
            self._params["scope_region_ids"] = [str(v) for v in scope.region_ids]
            parts.append(f'{alias}."{column}" = ANY(%(scope_region_ids)s::uuid[])')
        if scope.brand_ids is not None:
            alias, column = self._scope_column("brand_column")
            self._params["scope_brand_ids"] = [str(v) for v in scope.brand_ids]
            parts.append(f'{alias}."{column}" = ANY(%(scope_brand_ids)s::uuid[])')
        return parts

    def _merchant_scope_alias(self) -> str:
        primary = self._r.primary
        if primary.name == "merchant":
            return self._alias[primary.name]
        if primary.scope_via:
            relation = primary.get_relation(primary.scope_via)
            if relation is not None and relation.target_entity == "merchant":
                return self._ensure_joined("merchant")
        raise PlanCompileError(
            f"行级范围 merchant_ids 无法应用到主实体 '{primary.name}'："
            f"语义层缺少指向 merchant 的 scope_via 关系"
        )

    def _scope_column(self, attr: str) -> tuple[str, str]:
        """优先主实体自带列；否则经 scope_via 传递到目标实体的对应列。"""
        primary = self._r.primary
        direct = getattr(primary, attr)
        if direct:
            return self._alias[primary.name], direct
        if primary.scope_via:
            relation = primary.get_relation(primary.scope_via)
            if relation is not None:
                target = self._entity[relation.target_entity]
                indirect = getattr(target, attr)
                if indirect:
                    return self._ensure_joined(target.name), indirect
        raise PlanCompileError(
            f"行级范围 {attr} 无法应用到主实体 '{primary.name}'："
            f"语义层未定义 {attr} 或可用的 scope_via 传递路径"
        )

    def _scope_audit(self) -> dict[str, Any]:
        scope = self._ctx.scope
        if scope is None or scope.is_global:
            return {}
        audit: dict[str, Any] = {}
        if scope.region_ids is not None:
            audit["region_ids"] = [str(v) for v in scope.region_ids]
        if scope.brand_ids is not None:
            audit["brand_ids"] = [str(v) for v in scope.brand_ids]
        if scope.merchant_ids is not None:
            audit["merchant_ids"] = [str(v) for v in scope.merchant_ids]
        return audit

    # ---- WHERE：时间范围 ----

    def _build_time_condition(self) -> str | None:
        tr = self._r.plan.time_range
        if tr is None:
            return None
        primary = self._r.primary
        assert primary.time_column is not None  # 校验器已保证
        alias = self._alias[primary.name]
        column = primary.time_column
        monthly = primary.time_granularity == "month"

        if tr.kind == "month":
            assert tr.month is not None
            if monthly:
                self._params["t_month"] = tr.month
                return f'{alias}."{column}" = %(t_month)s'
            start, end_exclusive = _month_bounds(tr.month)
            self._params["t_start"] = start
            self._params["t_end"] = end_exclusive
            return (
                f'({alias}."{column}" >= %(t_start)s '
                f'AND {alias}."{column}" < %(t_end)s)'
            )

        if tr.kind == "relative":
            assert tr.relative is not None
            start, end_exclusive = _relative_bounds(
                tr.relative, self._ctx.today or date.today()
            )
        else:  # absolute
            start = tr.start
            end_exclusive = (
                tr.end + timedelta(days=1) if tr.end is not None else None
            )

        if monthly:
            # period_month 为 'YYYY-MM' 字符串，字典序即时间序。
            # 上界取「最后包含日所在月的下一月」，保证 end 所在月整月计入。
            conditions: list[str] = []
            if start is not None:
                self._params["t_start"] = f"{start:%Y-%m}"
                conditions.append(f'{alias}."{column}" >= %(t_start)s')
            if end_exclusive is not None:
                last = end_exclusive - timedelta(days=1)
                year, month = last.year, last.month + 1
                if month == 13:
                    year, month = year + 1, 1
                self._params["t_end"] = f"{year:04d}-{month:02d}"
                conditions.append(f'{alias}."{column}" < %(t_end)s')
            return self._join_and(conditions)

        conditions = []
        if start is not None:
            self._params["t_start"] = start
            conditions.append(f'{alias}."{column}" >= %(t_start)s')
        if end_exclusive is not None:
            self._params["t_end"] = end_exclusive
            conditions.append(f'{alias}."{column}" < %(t_end)s')
        return self._join_and(conditions)

    @staticmethod
    def _join_and(conditions: list[str]) -> str | None:
        if not conditions:
            return None
        if len(conditions) == 1:
            return conditions[0]
        return "(" + " AND ".join(conditions) + ")"

    # ---- WHERE：结构化过滤 ----

    def _build_filter_conditions(self) -> list[str]:
        parts: list[str] = []
        for resolved_filter in self._r.filters:
            f = resolved_filter.filter
            alias = self._alias[resolved_filter.entity.name]
            column = f'{alias}."{f.column}"'
            op = f.op
            if op is FilterOp.IS_NULL:
                parts.append(f"{column} IS NULL")
                continue
            if op is FilterOp.NOT_NULL:
                parts.append(f"{column} IS NOT NULL")
                continue
            if op is FilterOp.IN:
                name = self._next_param()
                self._params[name] = list(f.values or [])
                parts.append(f"{column} = ANY(%({name})s)")
                continue
            if op is FilterOp.BETWEEN:
                lo, hi = self._next_param(), self._next_param()
                values = list(f.values or [None, None])
                self._params[lo], self._params[hi] = values[0], values[1]
                parts.append(f"{column} BETWEEN %({lo})s AND %({hi})s")
                continue
            if op is FilterOp.LIKE:
                name = self._next_param()
                self._params[name] = f"%{_escape_like(str(f.value))}%"
                parts.append(f"{column} LIKE %({name})s ESCAPE '\\'")
                continue
            sql_op = {
                FilterOp.EQ: "=",
                FilterOp.NE: "<>",
                FilterOp.GT: ">",
                FilterOp.GE: ">=",
                FilterOp.LT: "<",
                FilterOp.LE: "<=",
            }[op]
            name = self._next_param()
            self._params[name] = f.value
            parts.append(f"{column} {sql_op} %({name})s")
        return parts

    def _next_param(self) -> str:
        self._seq += 1
        return f"f{self._seq}"

    # ---- GROUP BY / ORDER BY / LIMIT ----

    def _build_group_by(self) -> str:
        if not self._r.dimensions or not self._r.metrics:
            return ""
        # 必须复用 SELECT 中的表达式：PostgreSQL 分组按表达式等价匹配，
        # 写成裸列名会在分桶场景下报“列必须出现在 GROUP BY 中”。
        keys = [self._dimension_sql(d) for _, d in self._r.dimensions]
        return "GROUP BY " + ", ".join(keys)

    def _build_order_by(self, columns: list[OutputColumn]) -> str:
        if not self._r.plan.order_by:
            return ""
        alias_set = {c.name for c in columns}
        parts: list[str] = []
        for order in self._r.plan.order_by:
            if order.key not in alias_set:
                # 校验器已保证 key 命中输出；防御性跳过
                continue
            direction = "ASC" if order.direction == "asc" else "DESC"
            parts.append(f'"{order.key}" {direction} NULLS LAST')
        return "ORDER BY " + ", ".join(parts) if parts else ""

    def _resolve_limit(self) -> int:
        if self._r.plan.limit is not None:
            return self._r.plan.limit
        default = self._r.catalog.defaults.get("default_row_limit", 200)
        try:
            return max(1, int(default))
        except (TypeError, ValueError):
            return 200


def _month_bounds(month: str) -> tuple[date, date]:
    year, mon = int(month[:4]), int(month[5:7])
    start = date(year, mon, 1)
    if mon == 12:
        return start, date(year + 1, 1, 1)
    return start, date(year, mon + 1, 1)


def _relative_bounds(relative: RelativeRange, today: date) -> tuple[date, date]:
    if relative is RelativeRange.TODAY:
        return today, today + timedelta(days=1)
    if relative is RelativeRange.YESTERDAY:
        return today - timedelta(days=1), today
    if relative is RelativeRange.LAST_7D:
        return today - timedelta(days=6), today + timedelta(days=1)
    if relative is RelativeRange.LAST_30D:
        return today - timedelta(days=29), today + timedelta(days=1)
    first = today.replace(day=1)
    if relative is RelativeRange.THIS_MONTH:
        if today.month == 12:
            return first, date(today.year + 1, 1, 1)
        return first, date(today.year, today.month + 1, 1)
    # LAST_MONTH
    if today.month == 1:
        return date(today.year - 1, 12, 1), first
    return date(today.year, today.month - 1, 1), first


def _escape_like(value: str) -> str:
    return (
        value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    )
