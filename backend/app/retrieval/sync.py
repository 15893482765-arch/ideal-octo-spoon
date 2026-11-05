"""语义索引同步流水线：Catalog → 文档 → embedding → 重建双 collection。

幂等设计：point id 为 doc_id 的 uuid5，且每次同步先 recreate collection，
语义层 YAML 是唯一事实源；同步脚本是唯一写入路径（见
``scripts/sync_semantic_index.py``），运行期检索只读。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.retrieval.documents import RetrievalDocument, build_documents
from app.retrieval.store import QdrantSemanticStore
from app.semantic.catalog import SemanticCatalog
from app.services.ai_embedding import EmbeddingClient

_EMBED_BATCH_SIZE = 32


@dataclass
class SyncReport:
    entity_documents: int = 0
    column_documents: int = 0
    written: int = 0
    vector_dim: int = 0
    dry_run: bool = False
    sample_texts: list[str] = field(default_factory=list)


def _embed_documents(
    documents: list[RetrievalDocument],
    embedding: EmbeddingClient,
) -> list[list[float]]:
    vectors: list[list[float]] = []
    for start in range(0, len(documents), _EMBED_BATCH_SIZE):
        batch = documents[start : start + _EMBED_BATCH_SIZE]
        vectors.extend(embedding.embed_documents([d.text for d in batch]))
    return vectors


def sync_semantic_index(
    catalog: SemanticCatalog,
    embedding: EmbeddingClient | None,
    store: QdrantSemanticStore | None,
    *,
    dry_run: bool = False,
) -> SyncReport:
    """全量重建语义索引。dry_run 只构建文档预览，不调模型不写库。"""
    documents = build_documents(catalog)
    entity_docs = [d for d in documents if d.is_entity_level]
    column_docs = [d for d in documents if not d.is_entity_level]
    report = SyncReport(
        entity_documents=len(entity_docs),
        column_documents=len(column_docs),
        dry_run=dry_run,
        sample_texts=[d.text for d in documents[:3]],
    )
    if dry_run:
        return report
    if embedding is None:
        raise RuntimeError("embedding 不可用：无法同步语义索引（请检查 embedding 配置）")
    if store is None:
        raise RuntimeError("Qdrant store 未配置：无法同步语义索引")

    vectors = _embed_documents(documents, embedding)
    report.vector_dim = embedding.dim
    store.recreate_collections()
    report.written = store.replace_documents(documents, vectors)
    return report
