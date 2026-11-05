"""语义检索器：向量 + BM25 + 结构先验的混合召回。

降级语义：Qdrant / embedding 任一不可用 → 自动退回 catalog 关键词匹配
（低精度但不阻断助手），结果标记 ``degraded=True``，上层可据此提示用户
「语义索引不可用，回答精度可能受限」。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from app.retrieval.scoring import BM25Index, rank_documents
from app.retrieval.store import QdrantSemanticStore, RetrievalStoreError
from app.semantic.catalog import SemanticCatalog
from app.services.ai_embedding import EmbeddingClient, EmbeddingUnavailableError

logger = logging.getLogger(__name__)

_CORPUS_CACHE_TTL_SECONDS = 300.0


@dataclass
class ScoredDocument:
    doc_id: str
    kind: str
    entity_name: str
    name: str
    text: str
    score: float
    vector_score: float = 0.0
    bm25_score: float = 0.0
    prior: float = 0.0
    payload: dict = field(default_factory=dict)


@dataclass
class RetrievalResult:
    query: str
    entities: list[ScoredDocument] = field(default_factory=list)
    columns: list[ScoredDocument] = field(default_factory=list)
    degraded: bool = False
    degrade_reason: str = ""

    @property
    def entity_names(self) -> list[str]:
        return [doc.entity_name for doc in self.entities]

    @property
    def metric_names(self) -> list[str]:
        return [doc.name for doc in self.columns if doc.kind == "metric"]


class SemanticRetriever:
    """混合召回检索器；BM25 语料带 TTL 缓存（同步重建后自然过期）。"""

    def __init__(
        self,
        *,
        catalog: SemanticCatalog,
        embedding: EmbeddingClient | None,
        store: QdrantSemanticStore | None,
        top_k_entities: int = 6,
        top_k_columns: int = 12,
        candidate_limit: int = 24,
    ) -> None:
        self._catalog = catalog
        self._embedding = embedding
        self._store = store
        self._top_k_entities = top_k_entities
        self._top_k_columns = top_k_columns
        self._candidate_limit = candidate_limit
        self._corpus_cache: dict[str, tuple[float, dict[str, dict]]] = {}

    # ---- 主入口 ----

    def search(self, question: str) -> RetrievalResult:
        question = question.strip()
        if not question:
            return RetrievalResult(query=question)
        try:
            return self._hybrid_search(question)
        except (EmbeddingUnavailableError, RetrievalStoreError) as exc:
            logger.warning("语义检索降级（%s），退回关键词匹配", exc)
            return self._lexical_fallback(question, reason=str(exc))

    # ---- 混合召回 ----

    def _hybrid_search(self, question: str) -> RetrievalResult:
        if self._embedding is None:
            raise EmbeddingUnavailableError("embedding 未配置")
        if self._store is None:
            raise RetrievalStoreError("Qdrant store 未配置")

        query_vector = self._embedding.embed_query(question)
        entity_hits = self._store.vector_search(
            self._store.entity_collection, query_vector, limit=self._candidate_limit
        )
        column_hits = self._store.vector_search(
            self._store.column_collection, query_vector, limit=self._candidate_limit
        )
        entity_corpus = self._load_corpus(self._store.entity_collection)
        column_corpus = self._load_corpus(self._store.column_collection)

        entities = self._rank_collection(entity_hits, entity_corpus, question)
        columns = self._rank_collection(column_hits, column_corpus, question)

        top_entities = entities[: self._top_k_entities]
        top_columns = columns[: self._top_k_columns]

        # 细粒度命中反查实体：指标/列命中而其实体未入榜时补入（保 Schema 完整）
        entity_ids = {doc.doc_id for doc in top_entities}
        entity_corpus_payloads = entity_corpus
        for doc in top_columns:
            entity_doc_id = f"entity:{doc.entity_name}"
            if entity_doc_id in entity_ids:
                continue
            payload = entity_corpus_payloads.get(entity_doc_id)
            if payload is None:
                continue
            top_entities.append(
                ScoredDocument(
                    doc_id=entity_doc_id,
                    kind="entity",
                    entity_name=doc.entity_name,
                    name=doc.entity_name,
                    text=str(payload.get("text", "")),
                    score=doc.score,  # 继承最强细粒度命中的分数
                    prior=float(payload.get("prior", 0.0)),
                    payload=payload,
                )
            )
            entity_ids.add(entity_doc_id)

        return RetrievalResult(
            query=question, entities=top_entities, columns=top_columns
        )

    def _rank_collection(
        self,
        vector_hits: dict[str, tuple[float, dict]],
        corpus: dict[str, dict],
        question: str,
    ) -> list[ScoredDocument]:
        bm25 = BM25Index({doc_id: str(p.get("text", "")) for doc_id, p in corpus.items()})
        bm25_norm = bm25.normalized_score(question)
        vector_scores = {doc_id: score for doc_id, (score, _) in vector_hits.items()}
        priors = {
            doc_id: float(payload.get("prior", 0.0))
            for doc_id, payload in corpus.items()
        }
        # 只排序向量候选 ∪ BM25 有词的文档，避免全语料零分噪声入榜
        candidate_ids = {
            doc_id for doc_id, s in bm25_norm.items() if s > 0
        } | set(vector_scores)
        ranked = rank_documents(
            {doc_id: vector_scores.get(doc_id, 0.0) for doc_id in candidate_ids},
            {doc_id: bm25_norm.get(doc_id, 0.0) for doc_id in candidate_ids},
            {doc_id: priors.get(doc_id, 0.0) for doc_id in candidate_ids},
        )
        documents: list[ScoredDocument] = []
        for doc_id, score in ranked:
            payload = corpus.get(doc_id) or (vector_hits.get(doc_id) or (0.0, {}))[1]
            documents.append(
                ScoredDocument(
                    doc_id=doc_id,
                    kind=str(payload.get("kind", "")),
                    entity_name=str(payload.get("entity", "")),
                    name=str(payload.get("name", "")),
                    text=str(payload.get("text", "")),
                    score=score,
                    vector_score=vector_scores.get(doc_id, 0.0),
                    bm25_score=bm25_norm.get(doc_id, 0.0),
                    prior=priors.get(doc_id, 0.0),
                    payload=payload,
                )
            )
        return documents

    # ---- 语料缓存 ----

    def _load_corpus(self, collection: str) -> dict[str, dict]:
        cached = self._corpus_cache.get(collection)
        now = time.monotonic()
        if cached is not None and now - cached[0] < _CORPUS_CACHE_TTL_SECONDS:
            return cached[1]
        assert self._store is not None
        corpus = self._store.scroll_payloads(collection)
        self._corpus_cache[collection] = (now, corpus)
        return corpus

    def refresh_corpus(self) -> None:
        self._corpus_cache.clear()

    # ---- 降级路径 ----

    def _lexical_fallback(self, question: str, *, reason: str) -> RetrievalResult:
        """关键词兜底：实体/指标的名称或同义词出现在问题中即召回（反向包含，
        对自然语言问题有效；长度 <2 的词跳过以避免单字噪声）。"""
        result = RetrievalResult(
            query=question, degraded=True, degrade_reason=reason
        )
        lowered = question.lower()

        def hits(candidates: list[str]) -> bool:
            return any(
                len(word) >= 2 and word in lowered
                for word in (c.strip().lower() for c in candidates)
            )

        seen: set[str] = set()
        for entity in self._catalog.entities:
            if not hits([entity.name, entity.table, *entity.synonyms]):
                continue
            seen.add(entity.name)
            result.entities.append(
                ScoredDocument(
                    doc_id=f"entity:{entity.name}",
                    kind="entity",
                    entity_name=entity.name,
                    name=entity.name,
                    text=entity.description,
                    score=0.3,
                )
            )
        for metric in self._catalog.metrics:
            if not hits([metric.name, *metric.synonyms]):
                continue
            result.columns.append(
                ScoredDocument(
                    doc_id=f"metric:{metric.name}",
                    kind="metric",
                    entity_name=metric.entity,
                    name=metric.name,
                    text=metric.description,
                    score=0.3,
                )
            )
            if metric.entity not in seen:
                entity = self._catalog.entity_map.get(metric.entity)
                if entity is not None:
                    seen.add(entity.name)
                    result.entities.append(
                        ScoredDocument(
                            doc_id=f"entity:{entity.name}",
                            kind="entity",
                            entity_name=entity.name,
                            name=entity.name,
                            text=entity.description,
                            score=0.25,
                        )
                    )
        result.entities = result.entities[: self._top_k_entities]
        result.columns = result.columns[: self._top_k_columns]
        return result


def build_default_retriever(
    catalog: SemanticCatalog | None = None,
) -> SemanticRetriever:
    """按 settings 组装默认检索器（Qdrant 关闭/embedding 不可用时自动降级）。"""
    from app.core.config import get_settings
    from app.semantic import get_default_catalog
    from app.services.ai_embedding import get_embedding_client

    settings = get_settings()
    resolved_catalog = catalog or get_default_catalog()
    embedding = get_embedding_client()
    store: QdrantSemanticStore | None = None
    if settings.qdrant_enabled and embedding is not None:
        store = QdrantSemanticStore(
            url=settings.qdrant_url,
            api_key=(
                settings.qdrant_api_key.get_secret_value()
                if settings.qdrant_api_key
                else None
            ),
            timeout_seconds=settings.qdrant_timeout_seconds,
            entity_collection=settings.semantic_index_entity_collection,
            column_collection=settings.semantic_index_column_collection,
            vector_size=settings.embedding_dim,
        )
    return SemanticRetriever(
        catalog=resolved_catalog,
        embedding=embedding,
        store=store,
        top_k_entities=settings.retrieval_top_k_entities,
        top_k_columns=settings.retrieval_top_k_columns,
    )
