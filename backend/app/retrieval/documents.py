"""检索文档构建：语义层 Catalog → 双粒度文档（实体表粒度 + 列/指标细粒度）。

- **实体文档**（entity collection）：实体名/表名/描述/同义词/指标清单，
  回答「这个问题涉及哪几张表」；
- **细粒度文档**（column collection）：列文档与指标文档，回答「表里的
  哪个字段/哪个指标匹配这个说法」；指标文档带口径表达式与同义词，
  是 Text2SQL 命中率的关键。

每个文档自带结构先验 prior（0-1）：事实表/时间列/指标文档天然更常被
业务问题命中，混合评分时作为第三路信号（权重见 scoring.py）。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.semantic.catalog import (
    SemanticCatalog,
    SemanticColumn,
    SemanticEntity,
    SemanticMetric,
)

KIND_ENTITY = "entity"
KIND_COLUMN = "column"
KIND_METRIC = "metric"


@dataclass(frozen=True)
class RetrievalDocument:
    """一条可入库的检索文档（text 同时用于 embedding 与 BM25）。"""

    doc_id: str
    kind: str
    entity_name: str
    name: str
    text: str
    prior: float
    extra: dict

    @property
    def is_entity_level(self) -> bool:
        return self.kind == KIND_ENTITY

    @property
    def payload(self) -> dict:
        """写入 Qdrant 的 payload（检索时原样取回，BM25/上下文直接用）。"""
        return {
            "doc_id": self.doc_id,
            "kind": self.kind,
            "entity": self.entity_name,
            "name": self.name,
            "text": self.text,
            "prior": self.prior,
            **self.extra,
        }


def _join(values: list[str]) -> str:
    return "、".join(v for v in values if v)


def _entity_prior(entity: SemanticEntity, metric_count: int) -> float:
    """实体结构先验：指标密集的事实表与时间序列表权重更高。"""
    prior = 0.4
    if metric_count >= 3:
        prior += 0.3
    elif metric_count >= 1:
        prior += 0.15
    if entity.time_column is not None:
        prior += 0.2
    return min(1.0, prior)


def _column_prior(entity: SemanticEntity, column: SemanticColumn) -> float:
    """列结构先验：枚举/时间/外键/金额列比纯文本列更常成为查询目标。"""
    if column.name == entity.time_column:
        return 0.65
    if column.data_type in {"enum", "date", "datetime", "month", "boolean"}:
        return 0.55
    if column.name.endswith("_id"):
        return 0.5
    if column.data_type in {"money", "numeric", "integer"}:
        return 0.45
    return 0.3


def _metric_prior() -> float:
    """指标先验：命中指标几乎总与问题相关。"""
    return 0.8


def _entity_text(
    entity: SemanticEntity,
    metrics: list[SemanticMetric],
) -> str:
    metric_brief = _join([f"{m.name}（{m.description}）" for m in metrics])
    parts = [
        f"实体 {entity.name}（表 {entity.table}）：{entity.description}",
    ]
    if entity.synonyms:
        parts.append(f"同义词：{_join(entity.synonyms)}")
    if metric_brief:
        parts.append(f"指标：{metric_brief}")
    if entity.time_column:
        parts.append(f"时间列：{entity.time_column}（{entity.time_granularity}）")
    return "。".join(parts)


def _column_text(entity: SemanticEntity, column: SemanticColumn) -> str:
    text = f"{entity.name}.{column.name}（{entity.description}）：{column.description}"
    return f"{text}（类型 {column.data_type}）"


def _metric_text(metric: SemanticMetric) -> str:
    parts = [f"指标 {metric.name}：{metric.description}"]
    if metric.synonyms:
        parts.append(f"同义词：{_join(metric.synonyms)}")
    parts.append(f"口径：{metric.expression}")
    parts.append(f"单位 {metric.unit}，所属实体 {metric.entity}")
    return "。".join(parts)


def build_documents(catalog: SemanticCatalog) -> list[RetrievalDocument]:
    """从语义层 Catalog 构建全量检索文档（确定性，幂等）。"""
    documents: list[RetrievalDocument] = []
    metrics_by_entity: dict[str, list[SemanticMetric]] = {}
    for metric in catalog.metrics:
        metrics_by_entity.setdefault(metric.entity, []).append(metric)

    for entity in catalog.entities:
        entity_metrics = metrics_by_entity.get(entity.name, [])
        documents.append(
            RetrievalDocument(
                doc_id=f"entity:{entity.name}",
                kind=KIND_ENTITY,
                entity_name=entity.name,
                name=entity.name,
                text=_entity_text(entity, entity_metrics),
                prior=_entity_prior(entity, len(entity_metrics)),
                extra={"table": entity.table},
            )
        )
        for column in entity.columns:
            documents.append(
                RetrievalDocument(
                    doc_id=f"column:{entity.name}.{column.name}",
                    kind=KIND_COLUMN,
                    entity_name=entity.name,
                    name=column.name,
                    text=_column_text(entity, column),
                    prior=_column_prior(entity, column),
                    extra={"data_type": column.data_type},
                )
            )

    for metric in catalog.metrics:
        documents.append(
            RetrievalDocument(
                doc_id=f"metric:{metric.name}",
                kind=KIND_METRIC,
                entity_name=metric.entity,
                name=metric.name,
                text=_metric_text(metric),
                prior=_metric_prior(),
                extra={"unit": metric.unit, "expression": metric.expression},
            )
        )

    for dimension in catalog.dimensions:
        documents.append(
            RetrievalDocument(
                doc_id=f"dimension:{dimension.name}",
                kind=KIND_COLUMN,  # 维度与列同为细粒度，归入 column collection
                entity_name=dimension.entity,
                name=dimension.name,
                text=(
                    f"维度 {dimension.name}（{dimension.entity}.{dimension.column}）："
                    f"{dimension.description}"
                    + (f"。同义词：{_join(dimension.synonyms)}" if dimension.synonyms else "")
                ),
                prior=0.6,
                extra={"column": dimension.column, "is_dimension": True},
            )
        )
    return documents


def split_by_collection(
    documents: list[RetrievalDocument],
) -> tuple[list[RetrievalDocument], list[RetrievalDocument]]:
    """拆分为（实体 collection 文档, 细粒度 collection 文档）。"""
    entity_docs = [d for d in documents if d.is_entity_level]
    fine_docs = [d for d in documents if not d.is_entity_level]
    return entity_docs, fine_docs
