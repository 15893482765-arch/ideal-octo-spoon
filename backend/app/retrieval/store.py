"""Qdrant 语义索引存储：双 collection 的建表、重建、检索与 payload 抽取。

错误语义统一 `RetrievalStoreError`（脱敏：只带异常类型名），检索器捕获后
走关键词兜底，绝不让向量库故障穿透到助手对话。
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from typing import Any

from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

from app.retrieval.documents import RetrievalDocument

logger = logging.getLogger(__name__)

_POINT_ID_NAMESPACE = uuid.NAMESPACE_URL
_SCROLL_PAGE_SIZE = 256
_UPSERT_BATCH_SIZE = 128


class RetrievalStoreError(RuntimeError):
    """Qdrant 不可用/响应异常：检索降级信号，消息已脱敏。"""


def point_id_for(doc_id: str) -> str:
    """Qdrant point id（uuid5，幂等重建时覆盖同一逻辑文档）。"""
    return str(uuid.uuid5(_POINT_ID_NAMESPACE, f"qinshuang-semantic:{doc_id}"))


class QdrantSemanticStore:
    """双 collection 存储封装；`client` 可注入假客户端用于单测。"""

    def __init__(
        self,
        *,
        url: str,
        entity_collection: str,
        column_collection: str,
        vector_size: int,
        api_key: str | None = None,
        timeout_seconds: float = 10.0,
        client: Any | None = None,
    ) -> None:
        self._entity_collection = entity_collection
        self._column_collection = column_collection
        self._vector_size = vector_size
        self._client = client or QdrantClient(
            url=url,
            api_key=api_key,
            timeout=timeout_seconds,
        )

    @property
    def entity_collection(self) -> str:
        return self._entity_collection

    @property
    def column_collection(self) -> str:
        return self._column_collection

    # ---- 管理 ----

    def ping(self) -> bool:
        try:
            self._client.get_collections()
            return True
        except Exception:
            return False

    def recreate_collections(self) -> None:
        """删除并重建双 collection（同步幂等：语义层是单一事实源）。"""
        for collection in (self._entity_collection, self._column_collection):
            self._recreate_collection(collection)

    def _recreate_collection(self, collection: str) -> None:
        try:
            if self._client.collection_exists(collection):
                self._client.delete_collection(collection)
            self._client.create_collection(
                collection_name=collection,
                vectors_config=VectorParams(
                    size=self._vector_size, distance=Distance.COSINE
                ),
            )
        except Exception as exc:
            raise RetrievalStoreError(
                f"重建 collection 失败：{type(exc).__name__}"
            ) from exc

    def replace_documents(
        self,
        documents: Sequence[RetrievalDocument],
        vectors: Sequence[Sequence[float]],
    ) -> int:
        """全量写入一组文档到其归属 collection（须先 recreate）。"""
        if len(documents) != len(vectors):
            raise ValueError(
                f"文档数 {len(documents)} 与向量数 {len(vectors)} 不一致"
            )
        points = [
            PointStruct(
                id=point_id_for(doc.doc_id),
                vector=[float(v) for v in vector],
                payload=doc.payload,
            )
            for doc, vector in zip(documents, vectors, strict=True)
        ]
        written = 0
        for collection, batch in self._iter_collection_batches(points):
            try:
                self._client.upsert(collection_name=collection, points=batch)
            except Exception as exc:
                raise RetrievalStoreError(
                    f"写入 collection 失败：{type(exc).__name__}"
                ) from exc
            written += len(batch)
        return written

    def _iter_collection_batches(self, points: list[PointStruct]):
        entity_batch: list[PointStruct] = []
        column_batch: list[PointStruct] = []
        for point in points:
            if (point.payload or {}).get("kind") == "entity":
                entity_batch.append(point)
            else:
                column_batch.append(point)
        for collection, batch in (
            (self._entity_collection, entity_batch),
            (self._column_collection, column_batch),
        ):
            for start in range(0, len(batch), _UPSERT_BATCH_SIZE):
                yield collection, batch[start : start + _UPSERT_BATCH_SIZE]

    # ---- 检索 ----

    def vector_search(
        self,
        collection: str,
        vector: Sequence[float],
        *,
        limit: int,
    ) -> dict[str, tuple[float, dict]]:
        """向量检索，返回 {doc_id: (score, payload)}。"""
        try:
            response = self._client.query_points(
                collection_name=collection,
                query=[float(v) for v in vector],
                limit=limit,
                with_payload=True,
            )
        except Exception as exc:
            raise RetrievalStoreError(
                f"向量检索失败：{type(exc).__name__}"
            ) from exc
        result: dict[str, tuple[float, dict]] = {}
        for point in response.points:
            payload = dict(point.payload or {})
            doc_id = str(payload.get("doc_id") or point.id)
            result[doc_id] = (float(point.score), payload)
        return result

    def scroll_payloads(self, collection: str) -> dict[str, dict]:
        """抽出 collection 全部 payload（BM25 语料；规模小，全量内存）。"""
        corpus: dict[str, dict] = {}
        offset: Any = None
        try:
            while True:
                points, offset = self._client.scroll(
                    collection_name=collection,
                    limit=_SCROLL_PAGE_SIZE,
                    offset=offset,
                    with_payload=True,
                    with_vectors=False,
                )
                for point in points:
                    payload = dict(point.payload or {})
                    doc_id = str(payload.get("doc_id") or point.id)
                    corpus[doc_id] = payload
                if offset is None:
                    break
        except Exception as exc:
            raise RetrievalStoreError(
                f"抽取 payload 失败：{type(exc).__name__}"
            ) from exc
        return corpus
