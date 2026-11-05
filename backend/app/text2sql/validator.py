"""Query Plan 校验器：语义层驱动的硬校验 + join 路径推导。

校验不通过的 plan 一律抛出 ``PlanValidationError``（携带全部 issues，
可回喂 LLM 一次性修正）。通过校验的 plan 被解析为 ``ResolvedPlan``——
编译器只消费 ResolvedPlan，不再做任何名称查找。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from app.semantic.catalog import (
    SemanticCatalog,
    SemanticColumn,
    SemanticDimension,
    SemanticEntity,
    SemanticMetric,
    SemanticRelation,
)

from .errors import PlanIssue, PlanValidationError
from .plan import DimensionRef, Filter, FilterOp, MetricRef, QueryPlan


@dataclass(frozen=True)
class JoinStep:
    """一步 join：relation 恒为「定义侧实体.source_column -> 目标实体.target_column」。"""

    from_entity: str  # 已加入查询的一侧
    to_entity: str  # 本步新加入的实体
    relation_name: str
    source_entity: str  # relation 定义所在实体
    source_column: str
    target_entity: str
    target_column: str


@dataclass(frozen=True)
class ResolvedFilter:
    filter: Filter
    entity: SemanticEntity
    column: SemanticColumn


@dataclass(frozen=True)
class ResolvedPlan:
    """校验通过、引用全部解析完毕的 plan（编译器输入）。"""

    plan: QueryPlan
    catalog: SemanticCatalog
    primary: SemanticEntity
    metrics: tuple[tuple[MetricRef, SemanticMetric], ...]
    dimensions: tuple[tuple[DimensionRef, SemanticDimension], ...]
    filters: tuple[ResolvedFilter, ...]
    joins: tuple[JoinStep, ...]
    involved_entities: tuple[str, ...]  # 主实体在前，其余按 join 顺序


def validate_plan(plan: QueryPlan, catalog: SemanticCatalog) -> ResolvedPlan:
    issues: list[PlanIssue] = []
    entity_map = catalog.entity_map

    # ---- 主实体 ----
    primary = entity_map.get(plan.entity)
    if primary is None:
        issues.append(
            PlanIssue(
                code="UNKNOWN_ENTITY",
                message=f"主实体 '{plan.entity}' 不在语义层中（可选: "
                f"{', '.join(sorted(entity_map)[:10])} 等）",
                path="entity",
            )
        )
        raise PlanValidationError(issues)

    # ---- 指标 ----
    resolved_metrics: list[tuple[MetricRef, SemanticMetric]] = []
    for i, ref in enumerate(plan.metrics):
        metric = catalog.metric_map.get(ref.name)
        if metric is None:
            issues.append(
                PlanIssue(
                    code="UNKNOWN_METRIC",
                    message=f"指标 '{ref.name}' 不在语义层中",
                    path=f"metrics[{i}].name",
                )
            )
        else:
            resolved_metrics.append((ref, metric))

    # ---- 维度 ----
    resolved_dimensions: list[tuple[DimensionRef, SemanticDimension]] = []
    for i, ref in enumerate(plan.dimensions):
        dimension = catalog.dimension_map.get(ref.name)
        if dimension is None:
            issues.append(
                PlanIssue(
                    code="UNKNOWN_DIMENSION",
                    message=f"维度 '{ref.name}' 不在语义层中",
                    path=f"dimensions[{i}].name",
                )
            )
        else:
            resolved_dimensions.append((ref, dimension))

    if not resolved_metrics and not resolved_dimensions:
        issues.append(
            PlanIssue(
                code="EMPTY_SELECT",
                message="至少需要一个指标或一个维度",
                path="metrics",
            )
        )

    # ---- 过滤条件 ----
    resolved_filters: list[ResolvedFilter] = []
    for i, f in enumerate(plan.filters):
        path = f"filters[{i}]"
        target = primary if f.entity is None else entity_map.get(f.entity)
        if target is None:
            issues.append(
                PlanIssue(
                    code="UNKNOWN_ENTITY",
                    message=f"过滤条件引用的实体 '{f.entity}' 不在语义层中",
                    path=f"{path}.entity",
                )
            )
            continue
        column = target.get_column(f.column)
        if column is None:
            issues.append(
                PlanIssue(
                    code="UNKNOWN_COLUMN",
                    message=f"列 '{f.column}' 不在实体 '{target.name}' 的语义层列定义中"
                    f"（可选: {', '.join(sorted(target.column_names))}）",
                    path=f"{path}.column",
                )
            )
            continue
        if column.sensitive:
            issues.append(
                PlanIssue(
                    code="SENSITIVE_COLUMN",
                    message=f"列 '{target.name}.{f.column}' 标记为敏感，禁止作为过滤条件",
                    path=f"{path}.column",
                )
            )
            continue
        if not _check_filter_value(f, path, issues):
            continue
        resolved_filters.append(ResolvedFilter(filter=f, entity=target, column=column))

    # ---- 时间范围 ----
    if plan.time_range is not None:
        _check_time_range(plan, primary, issues)

    # ---- 排序 ----
    selectable = {
        ref.alias or metric.name for ref, metric in resolved_metrics
    } | {metric.name for _, metric in resolved_metrics} | {
        ref.alias or dim.name for ref, dim in resolved_dimensions
    } | {dim.name for _, dim in resolved_dimensions}
    for i, order in enumerate(plan.order_by):
        if order.key not in selectable:
            issues.append(
                PlanIssue(
                    code="UNKNOWN_ORDER_KEY",
                    message=f"排序键 '{order.key}' 不是已引用的指标/维度（名称或别名）",
                    path=f"order_by[{i}].key",
                )
            )

    # ---- join 可达性（BFS 推导） ----
    needed = {
        metric.entity for _, metric in resolved_metrics
    } | {dim.entity for _, dim in resolved_dimensions} | {
        rf.entity.name for rf in resolved_filters
    }
    needed.discard(primary.name)
    joins, unreachable = _derive_joins(catalog, primary.name, needed)
    for name in sorted(unreachable):
        issues.append(
            PlanIssue(
                code="ENTITY_NOT_REACHABLE",
                message=f"实体 '{name}' 无法从主实体 '{primary.name}' 经语义层 "
                f"relations 到达（请检查关系定义或更换主实体）",
                path="entity",
            )
        )

    if issues:
        raise PlanValidationError(issues)

    involved = [primary.name] + [step.to_entity for step in joins]
    return ResolvedPlan(
        plan=plan,
        catalog=catalog,
        primary=primary,
        metrics=tuple(resolved_metrics),
        dimensions=tuple(resolved_dimensions),
        filters=tuple(resolved_filters),
        joins=tuple(joins),
        involved_entities=tuple(involved),
    )


def _check_filter_value(
    f: Filter, path: str, issues: list[PlanIssue]
) -> bool:
    if f.op in (FilterOp.IS_NULL, FilterOp.NOT_NULL):
        return True
    if f.op is FilterOp.IN:
        if not f.values:
            issues.append(
                PlanIssue(
                    code="INVALID_FILTER_VALUE",
                    message="op=in 需要非空 values",
                    path=f"{path}.values",
                )
            )
            return False
        return True
    if f.op is FilterOp.BETWEEN:
        if not f.values or len(f.values) != 2:
            issues.append(
                PlanIssue(
                    code="INVALID_FILTER_VALUE",
                    message="op=between 需要恰好 2 个 values",
                    path=f"{path}.values",
                )
            )
            return False
        return True
    if f.value is None:
        issues.append(
            PlanIssue(
                code="INVALID_FILTER_VALUE",
                message=f"op={f.op.value} 需要 value",
                path=f"{path}.value",
            )
        )
        return False
    return True


def _check_time_range(
    plan: QueryPlan, primary: SemanticEntity, issues: list[PlanIssue]
) -> None:
    assert plan.time_range is not None
    tr = plan.time_range
    if primary.time_column is None:
        issues.append(
            PlanIssue(
                code="NO_TIME_COLUMN",
                message=f"主实体 '{primary.name}' 在语义层中没有 time_column，"
                f"不能按时间过滤",
                path="time_range",
            )
        )
        return
    if tr.kind == "absolute":
        if tr.start is None and tr.end is None:
            issues.append(
                PlanIssue(
                    code="INVALID_TIME_RANGE",
                    message="absolute 时间范围需要 start 或 end 至少一个",
                    path="time_range",
                )
            )
        elif tr.start is not None and tr.end is not None and tr.start > tr.end:
            issues.append(
                PlanIssue(
                    code="INVALID_TIME_RANGE",
                    message="absolute 时间范围 start 不能晚于 end",
                    path="time_range",
                )
            )
    elif tr.kind == "month":
        if tr.month is None:
            issues.append(
                PlanIssue(
                    code="INVALID_TIME_RANGE",
                    message="month 时间范围需要 month 字段（YYYY-MM）",
                    path="time_range.month",
                )
            )
    else:  # relative
        if tr.relative is None:
            issues.append(
                PlanIssue(
                    code="INVALID_TIME_RANGE",
                    message="relative 时间范围需要 relative 字段",
                    path="time_range.relative",
                )
            )


def _build_adjacency(
    catalog: SemanticCatalog,
) -> dict[str, list[tuple[SemanticRelation, str, str]]]:
    """实体邻接表：entity -> [(relation, neighbor, owner)]。

    owner 为 relation 的定义侧实体（source_column 所在表）；
    join 条件对称，双向各记一条。
    """

    adjacency: dict[str, list[tuple[SemanticRelation, str, str]]] = {
        e.name: [] for e in catalog.entities
    }
    for entity in catalog.entities:
        for rel in entity.relations:
            if rel.target_entity not in adjacency:
                continue
            adjacency[entity.name].append((rel, rel.target_entity, entity.name))
            adjacency[rel.target_entity].append((rel, entity.name, entity.name))
    return adjacency


def _derive_joins(
    catalog: SemanticCatalog, primary_name: str, needed: set[str]
) -> tuple[list[JoinStep], set[str]]:
    """BFS 推导 join 路径；返回（有序去重的步骤，不可达实体集合）。"""
    adjacency = _build_adjacency(catalog)
    # BFS 记录每个实体的父边：entity -> (from_entity, relation, owner)
    parent: dict[str, tuple[str, SemanticRelation, str]] = {}
    visited = {primary_name}
    queue: deque[str] = deque([primary_name])
    while queue:
        current = queue.popleft()
        for relation, neighbor, owner in adjacency.get(current, []):
            if neighbor in visited:
                continue
            visited.add(neighbor)
            parent[neighbor] = (current, relation, owner)
            queue.append(neighbor)

    unreachable = needed - visited
    # 回溯路径，父先于子加入（chain 反转后从主实体侧开始）
    steps: dict[str, JoinStep] = {}
    order: list[str] = []
    for target in sorted(needed & visited):
        chain: list[str] = []
        node = target
        while node != primary_name:
            chain.append(node)
            node = parent[node][0]
        for node in reversed(chain):
            if node in steps:
                continue
            from_entity, relation, owner = parent[node]
            steps[node] = JoinStep(
                from_entity=from_entity,
                to_entity=node,
                relation_name=relation.name,
                source_entity=owner,
                source_column=relation.source_column,
                target_entity=relation.target_entity,
                target_column=relation.target_column,
            )
            order.append(node)
    return [steps[name] for name in order], unreachable
