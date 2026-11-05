"""检索层测试：文档构建 / BM25 / 混合评分 / Qdrant 存储 / 检索器 / ContextBuilder / 同步。

用内存版 FakeQdrantClient（实现真实 cosine 检索与分页 scroll）+ 哈希假
embedding（token 重叠即相似），无需真实 Qdrant 与模型即可端到端验证
「同步 → 混合召回 → 上下文装配」全链路。
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from typing import Any

import pytest

from app.retrieval.context import ContextBuilder, estimate_tokens
from app.retrieval.documents import build_documents, split_by_collection
from app.retrieval.retriever import RetrievalResult, SemanticRetriever
from app.retrieval.scoring import (
    WEIGHT_BM25,
    WEIGHT_PRIOR,
    WEIGHT_VECTOR,
    BM25Index,
    rank_documents,
    tokenize,
)
from app.retrieval.store import (
    QdrantSemanticStore,
    RetrievalStoreError,
    point_id_for,
)
from app.retrieval.sync import sync_semantic_index
from app.semantic import SemanticCatalog, get_default_catalog


@pytest.fixture(scope="module")
def catalog() -> SemanticCatalog:
    return get_default_catalog(refresh=True)


# ---------------------------------------------------------------------------
# 测试替身
# ---------------------------------------------------------------------------


class HashEmbedding:
    """哈希假 embedding：token 重叠越多向量越相似（dim=64，归一化）。"""

    def __init__(self, dim: int = 64) -> None:
        self._dim = dim

    @property
    def dim(self) -> int:
        return self._dim

    def _embed(self, text: str) -> list[float]:
        vec = [0.0] * self._dim
        for token in tokenize(text):
            index = int(hashlib.md5(token.encode()).hexdigest(), 16) % self._dim
            vec[index] += 1.0
        norm = math.sqrt(sum(v * v for v in vec))
        return [v / norm for v in vec] if norm else vec

    def embed_documents(self, texts):
        return [self._embed(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._embed(text)


@dataclass
class _FakePoint:
    id: str
    vector: list[float]
    payload: dict


@dataclass
class _FakeScoredPoint:
    id: str
    score: float
    payload: dict


class _FakeQueryResponse:
    def __init__(self, points: list[_FakeScoredPoint]) -> None:
        self.points = points


class FakeQdrantClient:
    """内存版 Qdrant：真实 cosine 相似度 + 分页 scroll。"""

    def __init__(self) -> None:
        self.collections: dict[str, dict[str, Any]] = {}
        self.upsert_calls: list[tuple[str, int]] = []

    # 管理
    def get_collections(self):
        return list(self.collections)

    def collection_exists(self, name: str) -> bool:
        return name in self.collections

    def create_collection(self, collection_name: str, vectors_config: Any) -> None:
        self.collections[collection_name] = {"config": vectors_config, "points": {}}

    def delete_collection(self, collection_name: str) -> None:
        self.collections.pop(collection_name, None)

    # 写入
    def upsert(self, collection_name: str, points: list[Any]) -> None:
        bucket = self.collections[collection_name]["points"]
        for point in points:
            bucket[point.id] = _FakePoint(point.id, list(point.vector), dict(point.payload))
        self.upsert_calls.append((collection_name, len(points)))

    # 检索
    def query_points(
        self, collection_name: str, query: list[float], limit: int, with_payload: bool
    ) -> _FakeQueryResponse:
        bucket = self.collections.get(collection_name, {}).get("points", {})
        scored = [
            _FakeScoredPoint(
                id=pid,
                score=sum(a * b for a, b in zip(query, point.vector, strict=True)),
                payload=point.payload,
            )
            for pid, point in bucket.items()
        ]
        scored.sort(key=lambda p: p.score, reverse=True)
        return _FakeQueryResponse(scored[:limit])

    def scroll(
        self,
        collection_name: str,
        limit: int,
        offset: Any,
        with_payload: bool,
        with_vectors: bool,
    ):
        points = list(self.collections.get(collection_name, {}).get("points", {}).values())
        start = offset or 0
        page = points[start : start + limit]
        next_offset = start + limit if start + limit < len(points) else None
        return page, next_offset


class ExplodingStore(QdrantSemanticStore):
    """所有操作都失败的 store，验证降级路径。"""

    def __init__(self) -> None:
        pass

    @property
    def entity_collection(self) -> str:
        return "x"

    @property
    def column_collection(self) -> str:
        return "y"

    def vector_search(self, *args, **kwargs):
        raise RetrievalStoreError("向量检索失败：ConnectionError")

    def scroll_payloads(self, *args, **kwargs):
        raise RetrievalStoreError("抽取 payload 失败：ConnectionError")


def _make_store(client: FakeQdrantClient, dim: int = 64) -> QdrantSemanticStore:
    return QdrantSemanticStore(
        url="memory://fake",
        entity_collection="test_entities",
        column_collection="test_columns",
        vector_size=dim,
        client=client,
    )


# ---------------------------------------------------------------------------
# 文档构建
# ---------------------------------------------------------------------------


class TestDocuments:
    def test_build_documents_counts(self, catalog: SemanticCatalog) -> None:
        docs = build_documents(catalog)
        entity_docs, fine_docs = split_by_collection(docs)
        assert len(entity_docs) == len(catalog.entities)
        expected_fine = (
            sum(len(e.columns) for e in catalog.entities)
            + len(catalog.metrics)
            + len(catalog.dimensions)
        )
        assert len(fine_docs) == expected_fine
        assert len({d.doc_id for d in docs}) == len(docs)  # doc_id 唯一

    def test_entity_doc_text_contains_semantics(self, catalog: SemanticCatalog) -> None:
        docs = {d.doc_id: d for d in build_documents(catalog)}
        entity_doc = docs["entity:sales_confirmation"]
        assert "sales_confirmations" in entity_doc.text
        assert "销量确认" in entity_doc.text
        assert "confirmed_sales_boxes" in entity_doc.text  # 指标清单
        assert entity_doc.extra["table"] == "sales_confirmations"

    def test_metric_doc_text_contains_caliber(self, catalog: SemanticCatalog) -> None:
        docs = {d.doc_id: d for d in build_documents(catalog)}
        metric_doc = docs["metric:confirmed_sales_amount"]
        assert "SUM(amount_fen)" in metric_doc.text
        assert "销售额" in metric_doc.text
        assert metric_doc.extra["unit"] == "money"

    def test_column_doc_text(self, catalog: SemanticCatalog) -> None:
        docs = {d.doc_id: d for d in build_documents(catalog)}
        column_doc = docs["column:sales_confirmation.amount_fen"]
        assert "确认金额" in column_doc.text
        assert column_doc.entity_name == "sales_confirmation"

    def test_dimension_doc_in_column_collection(self, catalog: SemanticCatalog) -> None:
        docs = {d.doc_id: d for d in build_documents(catalog)}
        dim_doc = docs["dimension:region_name"]
        assert dim_doc.is_entity_level is False
        assert "区域" in dim_doc.text
        assert dim_doc.extra["is_dimension"] is True

    def test_prior_ranges_and_ordering(self, catalog: SemanticCatalog) -> None:
        docs = {d.doc_id: d for d in build_documents(catalog)}
        assert all(0.0 <= d.prior <= 1.0 for d in docs.values())
        # 事实表（多指标+时间列）先验高于纯维度表
        assert (
            docs["entity:sales_confirmation"].prior
            > docs["entity:product_category"].prior
        )
        assert docs["metric:confirmed_sales_boxes"].prior == 0.8
        # 时间列先验高于普通文本列
        sales_docs = docs["column:sales_confirmation.biz_date"]
        assert sales_docs.prior == 0.65

    def test_payload_roundtrip_shape(self, catalog: SemanticCatalog) -> None:
        for doc in build_documents(catalog)[:5]:
            payload = doc.payload
            for key in ("doc_id", "kind", "entity", "name", "text", "prior"):
                assert key in payload


# ---------------------------------------------------------------------------
# 分词与 BM25
# ---------------------------------------------------------------------------


class TestTokenize:
    def test_chinese_bigrams(self) -> None:
        tokens = tokenize("销量确认")
        assert "销量" in tokens
        assert "量确" in tokens
        assert "售" not in tokens

    def test_ascii_words_kept_whole(self) -> None:
        tokens = tokenize("sales_confirmations 金额 amount_fen")
        assert "sales_confirmations" in tokens
        assert "amount_fen" in tokens

    def test_mixed(self) -> None:
        tokens = tokenize("上个月的gross_pay")
        assert "gross_pay" in tokens
        assert "个月" in tokens


class TestBM25:
    CORPUS = {
        "a": "确认销量（箱），正式销量口径",
        "b": "冰柜资产台账，状态快照",
        "c": "应发工资合计 = 底薪 + 绩效 + 调整",
    }

    def test_relevant_doc_ranks_first(self) -> None:
        scores = BM25Index(self.CORPUS).score("销量")
        assert scores["a"] > scores["b"]
        assert scores["a"] > scores["c"]
        assert scores["b"] == 0.0

    def test_normalized_bounds(self) -> None:
        norm = BM25Index(self.CORPUS).normalized_score("工资 底薪")
        assert max(norm.values()) == pytest.approx(1.0)
        assert all(0.0 <= v <= 1.0 for v in norm.values())

    def test_empty_corpus_safe(self) -> None:
        assert BM25Index({}).score("销量") == {}

    def test_no_match_query_returns_zeros(self) -> None:
        scores = BM25Index(self.CORPUS).score("不存在的词xyz")
        assert all(v == 0.0 for v in scores.values())


class TestRankDocuments:
    def test_weighted_merge(self) -> None:
        ranked = rank_documents(
            {"a": 1.0},
            {"b": 1.0},
            {"c": 1.0},
        )
        scores = dict(ranked)
        assert scores["a"] == pytest.approx(WEIGHT_VECTOR)
        assert scores["b"] == pytest.approx(WEIGHT_BM25)
        assert scores["c"] == pytest.approx(WEIGHT_PRIOR)
        assert [doc_id for doc_id, _ in ranked] == ["a", "b", "c"]

    def test_limit_and_desc_order(self) -> None:
        ranked = rank_documents({"a": 0.9, "b": 0.1}, {}, {}, limit=1)
        assert ranked == [("a", pytest.approx(WEIGHT_VECTOR * 0.9))]

    def test_weights_sum_to_one(self) -> None:
        assert WEIGHT_VECTOR + WEIGHT_BM25 + WEIGHT_PRIOR == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Qdrant 存储
# ---------------------------------------------------------------------------


class TestStore:
    def test_recreate_collections(self) -> None:
        client = FakeQdrantClient()
        client.create_collection("test_entities", None)
        store = _make_store(client)
        store.recreate_collections()
        assert set(client.collections) == {"test_entities", "test_columns"}

    def test_replace_documents_splits_collections(self, catalog: SemanticCatalog) -> None:
        client = FakeQdrantClient()
        store = _make_store(client)
        store.recreate_collections()
        docs = build_documents(catalog)
        vectors = HashEmbedding().embed_documents([d.text for d in docs])
        written = store.replace_documents(docs, vectors)
        assert written == len(docs)
        entity_points = client.collections["test_entities"]["points"]
        column_points = client.collections["test_columns"]["points"]
        assert len(entity_points) == len(catalog.entities)
        assert len(column_points) == len(docs) - len(catalog.entities)

    def test_replace_documents_length_mismatch(self, catalog: SemanticCatalog) -> None:
        client = FakeQdrantClient()
        store = _make_store(client)
        docs = build_documents(catalog)[:2]
        with pytest.raises(ValueError, match="不一致"):
            store.replace_documents(docs, [[0.1] * 64])

    def test_vector_search_and_scroll(self, catalog: SemanticCatalog) -> None:
        client = FakeQdrantClient()
        store = _make_store(client)
        store.recreate_collections()
        docs = build_documents(catalog)
        embedding = HashEmbedding()
        store.replace_documents(docs, embedding.embed_documents([d.text for d in docs]))

        hits = store.vector_search(
            "test_entities", embedding.embed_query("销量确认"), limit=3
        )
        assert hits
        top_doc_id = max(hits, key=lambda k: hits[k][0])
        assert top_doc_id == "entity:sales_confirmation"

        corpus = store.scroll_payloads("test_columns")
        assert "metric:confirmed_sales_amount" in corpus
        assert corpus["metric:confirmed_sales_amount"]["text"]

    def test_error_wrapping(self) -> None:
        class BrokenClient(FakeQdrantClient):
            def query_points(self, **kwargs):
                raise ConnectionError("boom")

        client = BrokenClient()
        client.create_collection("test_entities", None)
        store = _make_store(client)
        with pytest.raises(RetrievalStoreError, match="ConnectionError"):
            store.vector_search("test_entities", [0.1] * 64, limit=1)

    def test_point_id_stable(self) -> None:
        assert point_id_for("entity:merchant") == point_id_for("entity:merchant")
        assert point_id_for("entity:merchant") != point_id_for("entity:sku")


# ---------------------------------------------------------------------------
# 检索器（端到端：同步 → 混合召回）
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def synced_retriever(catalog: SemanticCatalog) -> SemanticRetriever:
    client = FakeQdrantClient()
    embedding = HashEmbedding()
    store = _make_store(client, embedding.dim)
    sync_semantic_index(catalog, embedding=embedding, store=store)
    return SemanticRetriever(
        catalog=catalog, embedding=embedding, store=store, candidate_limit=24
    )


class TestRetriever:
    def test_sales_question_recalls_sales_entities_and_metrics(
        self, synced_retriever: SemanticRetriever
    ) -> None:
        result = synced_retriever.search("上个月各区域的确认销量和销售额")
        assert not result.degraded
        assert "sales_confirmation" in result.entity_names
        metric_names = set(result.metric_names)
        assert {"confirmed_sales_boxes", "confirmed_sales_amount"} & metric_names

    def test_freezer_question(self, synced_retriever: SemanticRetriever) -> None:
        result = synced_retriever.search("在店冰柜数量和押金")
        assert not result.degraded
        assert {"freezer_asset", "freezer_movement"} & set(result.entity_names)

    def test_column_hit_backfills_entity(
        self, synced_retriever: SemanticRetriever
    ) -> None:
        # 命中工资单指标时，即使实体向量分不高也要把 payroll_statement 补入
        result = synced_retriever.search("应发工资 gross_pay 怎么算")
        assert "payroll_statement" in result.entity_names

    def test_top_k_limits(self, catalog: SemanticCatalog) -> None:
        client = FakeQdrantClient()
        embedding = HashEmbedding()
        store = _make_store(client, embedding.dim)
        sync_semantic_index(catalog, embedding=embedding, store=store)
        retriever = SemanticRetriever(
            catalog=catalog,
            embedding=embedding,
            store=store,
            top_k_entities=2,
            top_k_columns=3,
        )
        result = retriever.search("销量 巡访 冰柜 工资 绩效 费用 合同 商家")
        assert len(result.columns) <= 3
        # 实体允许因细粒度反查补入略超 top_k，但直接命中不超过
        assert len(result.entities) <= 2 + 3

    def test_degraded_when_store_unavailable(self, catalog: SemanticCatalog) -> None:
        retriever = SemanticRetriever(
            catalog=catalog, embedding=HashEmbedding(), store=ExplodingStore()
        )
        result = retriever.search("各区域销量")
        assert result.degraded
        assert result.degrade_reason
        # 降级路径仍应通过关键词匹配召回销量实体
        assert "sales_confirmation" in result.entity_names

    def test_degraded_when_embedding_missing(self, catalog: SemanticCatalog) -> None:
        retriever = SemanticRetriever(catalog=catalog, embedding=None, store=None)
        result = retriever.search("工资")
        assert result.degraded
        assert "payroll_statement" in result.entity_names

    def test_empty_question(self, synced_retriever: SemanticRetriever) -> None:
        result = synced_retriever.search("   ")
        assert result.entities == [] and result.columns == []

    def test_corpus_cache(self, synced_retriever: SemanticRetriever) -> None:
        synced_retriever.refresh_corpus()
        first = synced_retriever._load_corpus("test_entities")
        second = synced_retriever._load_corpus("test_entities")
        assert first is second


# ---------------------------------------------------------------------------
# ContextBuilder
# ---------------------------------------------------------------------------


class TestContextBuilder:
    def test_build_contains_caliber_and_glossary(
        self, catalog: SemanticCatalog, synced_retriever: SemanticRetriever
    ) -> None:
        question = "上个月各区域的确认销量和销售额分别是多少"
        result = synced_retriever.search(question)
        context = ContextBuilder(catalog).build(question, result)
        assert "SUM(amount_fen)" in context.text  # 命中指标口径
        assert "sales_confirmations" in context.text  # 实体 Schema
        assert "销量" in context.glossary_terms  # 术语命中
        assert "金额单位：分" in context.text  # 全局约定
        assert not context.truncated
        assert context.estimated_tokens > 0

    def test_budget_truncation(self, catalog: SemanticCatalog) -> None:
        question = "销量"
        result = RetrievalResult(query=question)
        for metric in catalog.metrics[:8]:
            result.columns.append(
                _metric_scored(catalog, metric.name, score=0.9)
            )
        for entity in catalog.entities[:6]:
            result.entities.append(
                _entity_scored(entity.name, score=0.8)
            )
        context = ContextBuilder(catalog, token_budget=120).build(question, result)
        assert context.truncated
        # 保底段（全局约定 + 实体 Schema）即便超预算也保留
        assert "全局约定" in context.text
        assert "相关实体" in context.text

    def test_entity_section_has_scope_hints(
        self, catalog: SemanticCatalog, synced_retriever: SemanticRetriever
    ) -> None:
        result = synced_retriever.search("销量")
        context = ContextBuilder(catalog).build("销量", result)
        assert "行级范围经 merchant" in context.text

    def test_estimate_tokens_monotonic(self) -> None:
        assert estimate_tokens("") == 0
        assert estimate_tokens("ab") < estimate_tokens("abcdefghij")
        assert estimate_tokens("销量") >= 1


def _entity_scored(name: str, score: float):
    from app.retrieval.retriever import ScoredDocument

    return ScoredDocument(
        doc_id=f"entity:{name}",
        kind="entity",
        entity_name=name,
        name=name,
        text="",
        score=score,
    )


def _metric_scored(catalog: SemanticCatalog, name: str, score: float):
    from app.retrieval.retriever import ScoredDocument

    metric = catalog.get_metric(name)
    return ScoredDocument(
        doc_id=f"metric:{name}",
        kind="metric",
        entity_name=metric.entity,
        name=name,
        text=metric.description,
        score=score,
    )


# ---------------------------------------------------------------------------
# 同步流水线
# ---------------------------------------------------------------------------


class TestSync:
    def test_dry_run_builds_only(self, catalog: SemanticCatalog) -> None:
        report = sync_semantic_index(catalog, embedding=None, store=None, dry_run=True)
        assert report.dry_run
        assert report.entity_documents == len(catalog.entities)
        assert report.column_documents > 0
        assert report.written == 0
        assert report.sample_texts

    def test_full_sync(self, catalog: SemanticCatalog) -> None:
        client = FakeQdrantClient()
        embedding = HashEmbedding()
        store = _make_store(client, embedding.dim)
        report = sync_semantic_index(catalog, embedding=embedding, store=store)
        assert report.vector_dim == embedding.dim
        assert report.written == report.entity_documents + report.column_documents
        assert set(client.collections) == {"test_entities", "test_columns"}

    def test_sync_requires_embedding(self, catalog: SemanticCatalog) -> None:
        client = FakeQdrantClient()
        store = _make_store(client)
        with pytest.raises(RuntimeError, match="embedding 不可用"):
            sync_semantic_index(catalog, embedding=None, store=store)
