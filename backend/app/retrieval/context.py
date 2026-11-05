"""ContextBuilder：把检索结果装配为受 token 预算约束的语义上下文。

预算分配按优先级：全局约定 > 命中指标口径 > 实体 Schema > 维度 > 术语。
超出预算时从低优先级段开始整段舍弃（不半截截断，避免模型读到残缺
口径），并标记 truncated=True；至少保留全局约定与排名第一的实体。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from app.retrieval.retriever import RetrievalResult, ScoredDocument
from app.semantic.catalog import SemanticCatalog, SemanticEntity


_MAX_DIMENSIONS = 10


def estimate_tokens(text: str) -> int:
    """token 估算（中英混合按 1.5 字符 ≈ 1 token 的保守近似）。"""
    return max(1, math.ceil(len(text) / 1.5)) if text else 0


@dataclass
class SemanticContext:
    text: str
    entities: list[str] = field(default_factory=list)
    metrics: list[str] = field(default_factory=list)
    dimensions: list[str] = field(default_factory=list)
    glossary_terms: list[str] = field(default_factory=list)
    truncated: bool = False
    estimated_tokens: int = 0


class ContextBuilder:
    """默认预算与 settings.retrieval_context_token_budget（3000）保持一致。"""

    def __init__(self, catalog: SemanticCatalog, *, token_budget: int = 3000) -> None:
        self._catalog = catalog
        self._budget = token_budget

    def build(self, question: str, retrieval: RetrievalResult) -> SemanticContext:
        context = SemanticContext(text="")
        sections: list[tuple[int, str, str]] = []  # (优先级, 段落名, 内容)

        sections.append((0, "全局约定", self._render_defaults()))
        metric_section, metrics = self._render_metrics(retrieval.columns)
        if metric_section:
            sections.append((1, "命中指标", metric_section))
        entity_section, entities = self._render_entities(retrieval.entities)
        if entity_section:
            sections.append((2, "相关实体", entity_section))
        dimension_section, dimensions = self._render_dimensions(retrieval)
        if dimension_section:
            sections.append((3, "相关维度", dimension_section))
        glossary_section, terms = self._render_glossary(question)
        if glossary_section:
            sections.append((4, "业务术语", glossary_section))

        sections.sort(key=lambda item: item[0])
        used = 0
        rendered: list[str] = []
        truncated = False
        for priority, _, content in sections:
            cost = estimate_tokens(content)
            if used + cost <= self._budget:
                rendered.append(content)
                used += cost
            elif priority in (0, 2):
                # 全局约定与实体 Schema 保底：即便超预算也保留（模型没有 Schema 无法工作）
                rendered.append(content)
                used += cost
            else:
                truncated = True
                if priority == 1:
                    metrics = []
                elif priority == 3:
                    dimensions = []
                elif priority == 4:
                    terms = []

        text = "\n\n".join(rendered)
        context.text = text
        context.entities = entities
        context.metrics = metrics
        context.dimensions = dimensions
        context.glossary_terms = terms
        context.truncated = truncated
        context.estimated_tokens = estimate_tokens(text)
        return context

    # ---- 各段渲染 ----

    def _render_defaults(self) -> str:
        defaults = self._catalog.defaults
        divisor = defaults.get("money_display_divisor", 100)
        timezone = defaults.get("timezone", "Asia/Shanghai")
        month_format = defaults.get("period_month_format", "YYYY-MM")
        return (
            "## 全局约定\n"
            f"- 金额单位：分（*_fen 列），展示时除以 {divisor} 转元；禁止 float 处理金额。\n"
            f"- 业务时区 {timezone}；业务月 period_month 为字符串 {month_format}。\n"
            "- 除 freezer_movements 外业务表按 dataset_type + dataset_id 隔离，查询必须过滤当前数据集。"
        )

    def _render_metrics(
        self, columns: list[ScoredDocument]
    ) -> tuple[str, list[str]]:
        metric_docs = [doc for doc in columns if doc.kind == "metric"]
        if not metric_docs:
            return "", []
        lines = ["## 命中指标（口径以此为准）"]
        names: list[str] = []
        for doc in metric_docs:
            metric = self._catalog.metric_map.get(doc.name)
            if metric is None:
                continue
            names.append(metric.name)
            synonyms = f"；同义词：{'、'.join(metric.synonyms)}" if metric.synonyms else ""
            lines.append(
                f"- {metric.name}（{metric.description}）= `{metric.expression}`，"
                f"单位 {metric.unit}，所属实体 {metric.entity}{synonyms}"
            )
        if not names:
            return "", []
        return "\n".join(lines), names

    def _render_entities(
        self, entities: list[ScoredDocument]
    ) -> tuple[str, list[str]]:
        if not entities:
            return "", []
        blocks = ["## 相关实体（表结构）"]
        names: list[str] = []
        for doc in entities:
            entity = self._catalog.entity_map.get(doc.entity_name)
            if entity is None:
                continue
            names.append(entity.name)
            blocks.append(self._render_entity(entity))
        if not names:
            return "", []
        return "\n\n".join(blocks), names

    @staticmethod
    def _render_entity(entity: SemanticEntity) -> str:
        lines = [f"### {entity.name} → {entity.table}（{entity.description}）"]
        column_parts = []
        for column in entity.columns:
            sensitive = "（敏感）" if column.sensitive else ""
            column_parts.append(
                f"{column.name}({column.data_type}) {column.description}{sensitive}"
            )
        lines.append("列：" + "；".join(column_parts))
        if entity.relations:
            rel_parts = [
                f"{rel.name} → {rel.target_entity} via {rel.source_column}"
                for rel in entity.relations
            ]
            lines.append("关系：" + "；".join(rel_parts))
        scope_hints: list[str] = []
        if entity.dataset_scoped:
            scope_hints.append("数据集隔离")
        if entity.scope_via:
            scope_hints.append(f"行级范围经 {entity.scope_via} 过滤区域/品牌")
        if entity.region_column:
            scope_hints.append(f"区域列 {entity.region_column}")
        if entity.brand_column:
            scope_hints.append(f"品牌列 {entity.brand_column}")
        if entity.user_column:
            scope_hints.append(f"仅本人列 {entity.user_column}")
        if scope_hints:
            lines.append("范围：" + "；".join(scope_hints))
        return "\n".join(lines)

    def _render_dimensions(
        self, retrieval: RetrievalResult
    ) -> tuple[str, list[str]]:
        # 直接命中的维度优先；命中实体的其余维度按序补足，总量封顶
        # （维度是辅助分组信息，主动节选不算 truncated）
        entity_names = set(retrieval.entity_names)
        ordered: list[str] = []
        for doc in retrieval.columns:
            if doc.payload.get("is_dimension") and doc.name not in ordered:
                ordered.append(doc.name)
        for dimension in self._catalog.dimensions:
            if dimension.entity in entity_names and dimension.name not in ordered:
                ordered.append(dimension.name)
        names = ordered[:_MAX_DIMENSIONS]
        if not names:
            return "", []
        lines = ["## 相关维度（分组/筛选视角）"]
        for name in names:
            dimension = self._catalog.dimension_map[name]
            synonyms = (
                f"；同义词：{'、'.join(dimension.synonyms)}" if dimension.synonyms else ""
            )
            lines.append(
                f"- {dimension.name} = {dimension.entity}.{dimension.column}"
                f"（{dimension.description}）{synonyms}"
            )
        return "\n".join(lines), names

    def _render_glossary(self, question: str) -> tuple[str, list[str]]:
        items = self._catalog.match_glossary(question)
        if not items:
            return "", []
        lines = ["## 业务术语（必须遵守的口径）"]
        for item in items:
            lines.append(f"- {item.term}：{item.definition}")
        return "\n".join(lines), [item.term for item in items]
