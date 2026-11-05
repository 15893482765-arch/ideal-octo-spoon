"""知识库语义检索（任务卡 4）：`knowledge.search` 工具的唯一实现。

- **向量模式**：query 编码后与 `knowledge_chunks` 全量算余弦。块规模为几十到
  几百条，全量内存计算足够（Python 侧余弦，毫秒级）；日后上量平移 pgvector
  时仅需改写本模块，工具契约不变；
- **降级**：embedding 客户端不可用或推理失败 → 字符二元组关键词匹配，
  检索不中断，信封上以 `retrieval_mode="keyword"` 明示精度下降；
- **缺向量行**（降级期摄入的 `embedding IS NULL`）：向量模式跳过，
  关键词模式仍参与——内容永远可被找到，只是排序精度不同；
- **注入防护**：知识文档属外部内容，命中块的正文在返回前统一过
  `inspect_external_text` 剥离越权指令（PRD §5.4 第 5/6 条），
  命中标记随 `KnowledgeHit.injection_blocked` 透出供审计。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from app.models import KnowledgeChunk
from app.services.ai_embedding import (
    EmbeddingUnavailableError,
    cosine_similarity,
    get_embedding_client,
)
from app.services.ai_prompt_guard import inspect_external_text

logger = logging.getLogger(__name__)

DEFAULT_TOP_K = 5
MAX_TOP_K = 10
# bge-small-zh 实测：相关块约 0.65+，擦边/无关块约 0.3~0.5。
# 阈值取 0.45：宁可答「知识库暂无」，也不把弱相关条目当答案（助手场景宁可缺不可错）
MIN_VECTOR_SCORE = 0.45

_AUTO = object()  # client 未显式传入：按配置取全局单例


@dataclass(frozen=True)
class KnowledgeHit:
    doc_id: str
    doc_title: str
    section_path: str
    content: str  # 已过注入防护清洗
    score: float
    injection_blocked: bool = False


def _hit(row: KnowledgeChunk, score: float) -> KnowledgeHit:
    guard = inspect_external_text(row.content)
    return KnowledgeHit(
        row.doc_id,
        row.doc_title,
        row.section_path,
        guard.cleaned_text,
        round(score, 6),
        guard.injection_detected,
    )


def _keyword_score(query: str, text: str) -> float:
    """字符二元组重合率（中文免分词）；整句命中加权。"""
    terms = {query[i : i + 2] for i in range(len(query) - 1)} if len(query) >= 2 else {query}
    if not terms:
        return 0.0
    hits = sum(1 for term in terms if term in text)
    score = hits / len(terms)
    if query in text:
        score += 0.5
    return score


def _vector_hits(
    rows: list[KnowledgeChunk], query: str, top_k: int, client: Any
) -> list[KnowledgeHit]:
    vector = client.embed_query(query)
    scored = [
        (cosine_similarity(vector, row.embedding), row)
        for row in rows
        if row.embedding is not None
    ]
    scored.sort(key=lambda item: item[0], reverse=True)
    return [_hit(row, score) for score, row in scored[:top_k] if score >= MIN_VECTOR_SCORE]


def _keyword_hits(rows: list[KnowledgeChunk], query: str, top_k: int) -> list[KnowledgeHit]:
    scored = [
        (_keyword_score(query, f"{row.section_path}\n{row.content}"), row) for row in rows
    ]
    scored.sort(key=lambda item: item[0], reverse=True)
    return [_hit(row, score) for score, row in scored[:top_k] if score > 0]


def search_knowledge(
    session: Any,
    query: str,
    top_k: int = DEFAULT_TOP_K,
    *,
    client: Any = _AUTO,
) -> tuple[list[KnowledgeHit], str]:
    """检索知识库，返回 (命中列表, 检索模式)；mode ∈ {"vector", "keyword"}。

    `client` 缺省取全局 embedding 单例；显式传 None 强制关键词模式（测试与降级）。
    """
    query = re.sub(r"\s+", " ", (query or "").strip())
    top_k = min(max(int(top_k), 1), MAX_TOP_K)
    if not query:
        return [], "vector" if client is not None else "keyword"

    rows = list(session.scalars(select(KnowledgeChunk)))
    if not rows:
        return [], "vector"

    if client is _AUTO:
        client = get_embedding_client()
    if client is not None:
        try:
            return _vector_hits(rows, query, top_k, client), "vector"
        except EmbeddingUnavailableError as exc:
            logger.warning("knowledge.search 向量检索降级为关键词：%s", exc)
    return _keyword_hits(rows, query, top_k), "keyword"


__all__ = [
    "DEFAULT_TOP_K",
    "MAX_TOP_K",
    "MIN_VECTOR_SCORE",
    "KnowledgeHit",
    "search_knowledge",
]
