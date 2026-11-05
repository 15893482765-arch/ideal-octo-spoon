"""RAG 知识库 Embedding 客户端（任务卡 2；与 ai_llm 同构）。

设计：
- **懒加载单例工厂** `get_embedding_client()`：`embedding_enabled=False`、
  provider 配置不完整时返回 None，调用方（摄入脚本/检索工具）走降级路径；
  本地模型在**首次 embed 时**才加载（import torch 慢，避免拖慢服务启动）；
- **双 provider**：`local`（sentence-transformers 本地模型，默认
  bge-small-zh-v1.5）与 `openai_compatible`（智谱 embedding-3 等
  OpenAI 兼容端点，切换只改配置）；
- **失败语义**统一 `EmbeddingUnavailableError`：只携带异常类型 / HTTP 状态 /
  提供方错误码，绝不泄露密钥与请求正文；
- **维度校验**：实际输出与 `settings.embedding_dim` 不符立即报错，
  防止错误维度向量入库污染检索；
- **检索非对称编码**（bge 官方推荐）：查询侧加指令前缀、文档侧不加；
  输出统一 L2 归一化，点积即余弦，检索侧（任务卡 4）直接用点积排序。

调用约束：embed 为同步阻塞调用（本地模型吃 CPU），异步上下文请用
`asyncio.to_thread` 包裹（任务卡 4 的检索 handler 负责）。
"""

from __future__ import annotations

import logging
import math
import os
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Protocol

import httpx

from app.core.config import get_settings

logger = logging.getLogger(__name__)

# bge 中文 v1.5 系列官方推荐的查询侧检索指令（文档侧不加）
_BGE_QUERY_INSTRUCTION = "为这个句子生成表示以用于检索相关文章："


class EmbeddingUnavailableError(RuntimeError):
    """Embedding 服务不可用：降级信号，消息已脱敏（类型/状态码级）。"""


class EmbeddingClient(Protocol):
    """embedding 客户端协议：摄入与检索共用一个实例。"""

    @property
    def dim(self) -> int:
        """配置声明的向量维度（实际输出每次都校验）。"""
        ...

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        """文档切块编码（入库侧），返回与输入等长的归一化向量。"""
        ...

    def embed_query(self, text: str) -> list[float]:
        """查询编码（检索侧），bge 系模型自动加检索指令前缀。"""
        ...


def _validate_batch(vectors: list[list[float]], expected_dim: int, count: int) -> None:
    if len(vectors) != count:
        raise EmbeddingUnavailableError(
            f"embedding 返回条数不符：期望 {count} 实得 {len(vectors)}"
        )
    for vec in vectors:
        if len(vec) != expected_dim:
            raise EmbeddingUnavailableError(
                f"embedding 维度不符：期望 {expected_dim} 实得 {len(vec)}"
                "（config.embedding_dim 与模型实际输出不一致，禁止入库）"
            )


def _l2_normalize(vec: Sequence[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vec))
    if norm == 0:
        return [float(v) for v in vec]
    return [float(v) / norm for v in vec]


class LocalEmbeddingClient:
    """本地 sentence-transformers 模型（默认 bge-small-zh-v1.5）。

    `model_loader` 可注入假模型用于单测；为 None 时首次 embed 才 import
    sentence_transformers 并加载权重（cache_folder 取 embedding_model_dir）。
    """

    def __init__(
        self,
        *,
        model_name: str,
        device: str,
        cache_dir: str,
        batch_size: int,
        expected_dim: int,
        model_loader: Callable[[], Any] | None = None,
    ) -> None:
        self._model_name = model_name
        self._device = device
        self._cache_dir = cache_dir or None
        self._batch_size = batch_size
        self._dim = expected_dim
        self._loader = model_loader
        self._model: Any | None = None
        # bge 系模型查询侧需要检索指令前缀
        self._query_instruction = (
            _BGE_QUERY_INSTRUCTION if "bge" in model_name.lower() else ""
        )

    @property
    def dim(self) -> int:
        return self._dim

    def _prepare_hf_env(self) -> None:
        """按模型缓存状态设置 HF 环境（须在 import sentence_transformers 前调用）。

        transformers v4/v5 加载时会做 Hub 解析：默认端点不可达时
        AutoProcessor 误报 "Unrecognized processing class"（ValueError），
        错误信息完全不指向网络问题。策略：
        - 缓存已存在 → HF_HUB_OFFLINE=1：纯本地加载，启动更快且行为确定；
        - 缓存不存在（首次下载）→ HF_ENDPOINT 指向国内镜像。
        均用 setdefault：用户显式配置的环境变量优先。
        """
        if not self._cache_dir:
            return
        folder = "models--" + self._model_name.replace("/", "--")
        if (Path(self._cache_dir) / folder).exists():
            os.environ.setdefault("HF_HUB_OFFLINE", "1")
        else:
            os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

    def _load(self) -> Any:
        if self._model is None:
            if self._loader is not None:
                self._model = self._loader()
            else:
                self._prepare_hf_env()
                try:
                    from sentence_transformers import SentenceTransformer
                except ImportError as exc:
                    raise EmbeddingUnavailableError(
                        "缺少 sentence-transformers（pip install -e .[rag]）"
                    ) from exc
                self._model = SentenceTransformer(
                    self._model_name,
                    device=self._device,
                    cache_folder=self._cache_dir,
                )
                logger.info(
                    "local embedding model loaded: %s (device=%s)",
                    self._model_name,
                    self._device,
                )
        return self._model

    def _encode(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        try:
            raw = self._load().encode(
                list(texts),
                batch_size=self._batch_size,
                normalize_embeddings=True,
            )
        except EmbeddingUnavailableError:
            raise
        except Exception as exc:  # 模型推理失败：脱敏为类型名
            reason = f"本地 embedding 推理失败：{type(exc).__name__}"
            logger.warning("embedding unavailable: %s (model=%s)", reason, self._model_name)
            raise EmbeddingUnavailableError(reason) from exc
        vectors = [[float(v) for v in row] for row in raw.tolist()]
        _validate_batch(vectors, self._dim, len(texts))
        return vectors

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return self._encode(texts)

    def embed_query(self, text: str) -> list[float]:
        query = f"{self._query_instruction}{text}" if self._query_instruction else text
        return self._encode([query])[0]


class OpenAiCompatibleEmbeddingClient:
    """OpenAI 兼容端点（智谱 embedding-3 等）。

    `transport` 可注入 httpx.MockTransport 用于单测；超时/连接错误/5xx
    重试一次，429 等配额类错误立即失败（重试无意义）。
    """

    _MAX_ATTEMPTS = 2

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        batch_size: int,
        timeout_seconds: float,
        expected_dim: int,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._model = model
        self._batch_size = max(1, batch_size)
        self._dim = expected_dim
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout_seconds,
            transport=transport,
        )

    @property
    def dim(self) -> int:
        return self._dim

    def _post_batch(self, texts: Sequence[str]) -> list[list[float]]:
        payload = {"model": self._model, "input": list(texts)}
        response: httpx.Response | None = None
        last_error: Exception | None = None
        for attempt in range(1, self._MAX_ATTEMPTS + 1):
            try:
                response = self._client.post("/embeddings", json=payload)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = exc
                continue
            if response.status_code == 429 or response.status_code < 500:
                break  # 配额/参数类错误立即失败
            if attempt == self._MAX_ATTEMPTS:
                break
        if response is None:
            reason = f"embedding 请求失败：{type(last_error).__name__ if last_error else 'unknown'}"
            logger.warning("embedding unavailable: %s (model=%s)", reason, self._model)
            raise EmbeddingUnavailableError(reason)
        if response.status_code >= 400:
            code = ""
            try:
                code = str(response.json().get("error", {}).get("code", ""))
            except ValueError:
                pass
            reason = f"embedding 返回 HTTP {response.status_code}" + (f"（{code}）" if code else "")
            logger.warning("embedding unavailable: %s (model=%s)", reason, self._model)
            raise EmbeddingUnavailableError(reason)
        try:
            data = sorted(response.json()["data"], key=lambda item: item["index"])
            vectors = [[float(v) for v in item["embedding"]] for item in data]
        except (KeyError, TypeError, ValueError) as exc:
            raise EmbeddingUnavailableError(
                f"embedding 响应结构异常：{type(exc).__name__}"
            ) from exc
        _validate_batch(vectors, self._dim, len(texts))
        return vectors

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self._batch_size):
            batch = texts[start : start + self._batch_size]
            vectors.extend(self._post_batch(batch))
        return vectors

    def embed_query(self, text: str) -> list[float]:
        return self.embed_documents([text])[0]


_client: EmbeddingClient | None = None
_client_ready = False


def get_embedding_client(*, refresh: bool = False) -> EmbeddingClient | None:
    """懒加载单例；降级返回 None（开关关闭 / 配置不完整）。"""
    global _client, _client_ready
    if _client_ready and not refresh:
        return _client
    settings = get_settings()
    client: EmbeddingClient | None = None
    if settings.embedding_enabled:
        if settings.embedding_provider == "local":
            client = LocalEmbeddingClient(
                model_name=settings.embedding_model,
                device=settings.embedding_device,
                cache_dir=settings.embedding_model_dir,
                batch_size=settings.embedding_batch_size,
                expected_dim=settings.embedding_dim,
            )
        elif settings.embedding_provider == "openai_compatible":
            if settings.embedding_api_key and settings.embedding_base_url:
                client = OpenAiCompatibleEmbeddingClient(
                    base_url=settings.embedding_base_url,
                    api_key=settings.embedding_api_key.get_secret_value(),
                    model=settings.embedding_model,
                    batch_size=settings.embedding_batch_size,
                    timeout_seconds=settings.ai_chat_timeout_seconds,
                    expected_dim=settings.embedding_dim,
                )
            else:
                logger.warning(
                    "embedding provider=openai_compatible 但未配置 base_url/api_key，降级为不可用"
                )
        else:
            logger.warning("未知 embedding provider：%s", settings.embedding_provider)
    _client = client
    _client_ready = True
    return _client


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    """余弦相似度（向量已归一化时退化为点积；检索侧用，任务卡 4）。"""
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


__all__ = [
    "EmbeddingClient",
    "EmbeddingUnavailableError",
    "LocalEmbeddingClient",
    "OpenAiCompatibleEmbeddingClient",
    "cosine_similarity",
    "get_embedding_client",
]
