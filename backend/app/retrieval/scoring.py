"""混合评分：0.55 × 向量语义 + 0.25 × BM25 + 0.20 × 结构先验。

- 向量分：Qdrant cosine 相似度（bge 归一化向量，天然 0-1）；
- BM25：中文友好的轻量实现（ASCII 词 + 中文字符 unigram/bigram 分词，
  不引入 jieba 依赖），语料仅百余条文档，内存计算并做 max 归一化；
- 结构先验：文档构建时写入 payload 的 prior（事实表/时间列/指标更高）。

三路信号任一缺失按 0 计（如 BM25 语料为空、向量库无命中），保证降级
场景仍可排序。
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Mapping

WEIGHT_VECTOR = 0.55
WEIGHT_BM25 = 0.25
WEIGHT_PRIOR = 0.20

_ASCII_WORD_RE = re.compile(r"[a-z0-9_]+")


def tokenize(text: str) -> list[str]:
    """中文友好分词：ASCII 词整词 + 其余字符（主要中文）unigram + bigram。"""
    lowered = text.lower()
    tokens = _ASCII_WORD_RE.findall(lowered)
    residual = _ASCII_WORD_RE.sub(" ", lowered)
    chars = [c for c in residual if not c.isspace()]
    tokens.extend(chars)
    tokens.extend(chars[i] + chars[i + 1] for i in range(len(chars) - 1))
    return tokens


class BM25Index:
    """内存版 BM25（k1=1.5, b=0.75），语料为 {doc_id: text}。"""

    def __init__(
        self,
        corpus: Mapping[str, str],
        *,
        k1: float = 1.5,
        b: float = 0.75,
    ) -> None:
        self._k1 = k1
        self._b = b
        self._doc_ids = list(corpus.keys())
        self._term_freqs: dict[str, Counter[str]] = {}
        self._doc_len: dict[str, int] = {}
        doc_freq: Counter[str] = Counter()
        for doc_id, text in corpus.items():
            tf: Counter[str] = Counter(tokenize(text))
            self._term_freqs[doc_id] = tf
            self._doc_len[doc_id] = sum(tf.values())
            for term in tf:
                doc_freq[term] += 1
        total = len(self._doc_ids)
        self._avgdl = (
            sum(self._doc_len.values()) / total if total else 0.0
        )
        # Robertson-Sparck Jones idf，+1 平滑避免负值
        self._idf = {
            term: math.log(1 + (total - df + 0.5) / (df + 0.5))
            for term, df in doc_freq.items()
        }

    @property
    def size(self) -> int:
        return len(self._doc_ids)

    def score(self, query: str) -> dict[str, float]:
        """全语料打分，返回 {doc_id: bm25}（未命中文档为 0）。"""
        query_terms = Counter(tokenize(query))
        scores = {doc_id: 0.0 for doc_id in self._doc_ids}
        if not query_terms or self._avgdl == 0:
            return scores
        for term in query_terms:
            idf = self._idf.get(term)
            if idf is None:
                continue
            for doc_id in self._doc_ids:
                tf = self._term_freqs[doc_id].get(term, 0)
                if tf == 0:
                    continue
                dl = self._doc_len[doc_id]
                denom = tf + self._k1 * (1 - self._b + self._b * dl / self._avgdl)
                scores[doc_id] += idf * (tf * (self._k1 + 1)) / denom
        return scores

    def normalized_score(self, query: str) -> dict[str, float]:
        """max 归一化到 0-1（全零语料/全零得分时安全返回全零）。"""
        scores = self.score(query)
        peak = max(scores.values(), default=0.0)
        if peak <= 0:
            return scores
        return {doc_id: value / peak for doc_id, value in scores.items()}


def hybrid_score(
    vector_score: float,
    bm25_score: float,
    prior: float,
    *,
    weight_vector: float = WEIGHT_VECTOR,
    weight_bm25: float = WEIGHT_BM25,
    weight_prior: float = WEIGHT_PRIOR,
) -> float:
    """三路信号加权合成（各路均应已归一化到 0-1）。"""
    return (
        weight_vector * vector_score
        + weight_bm25 * bm25_score
        + weight_prior * prior
    )


def rank_documents(
    vector_scores: Mapping[str, float],
    bm25_scores: Mapping[str, float],
    priors: Mapping[str, float],
    *,
    limit: int | None = None,
) -> list[tuple[str, float]]:
    """合并三路信号并降序返回 [(doc_id, hybrid)]；缺失信号按 0 计。"""
    doc_ids = set(vector_scores) | set(bm25_scores) | set(priors)
    ranked = [
        (
            doc_id,
            hybrid_score(
                vector_scores.get(doc_id, 0.0),
                bm25_scores.get(doc_id, 0.0),
                priors.get(doc_id, 0.0),
            ),
        )
        for doc_id in doc_ids
    ]
    ranked.sort(key=lambda item: (-item[1], item[0]))
    return ranked[:limit] if limit is not None else ranked
