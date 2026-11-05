"""Embedding 客户端单测（任务卡 2）：全程 mock，不加载真实模型、不触网。

覆盖：工厂降级（开关关闭/配置缺失/未知 provider）、本地客户端懒加载与
bge 查询指令、维度校验、OpenAI 兼容端点分批/重试/429 快速失败/脱敏。
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

import httpx
import numpy as np
import pytest

from app.core.config import get_settings
from app.services.ai_embedding import (
    EmbeddingUnavailableError,
    LocalEmbeddingClient,
    OpenAiCompatibleEmbeddingClient,
    cosine_similarity,
    get_embedding_client,
)

# ---------------------------------------------------------------------------
# 工厂
# ---------------------------------------------------------------------------


@pytest.fixture
def _fresh_factory(monkeypatch: pytest.MonkeyPatch):
    """每个用例后复位单例与 settings 缓存，避免污染其他测试。"""
    yield monkeypatch
    get_settings.cache_clear()
    get_embedding_client(refresh=True)


def _reload_client() -> Any:
    get_settings.cache_clear()
    return get_embedding_client(refresh=True)


def test_factory_disabled_returns_none(_fresh_factory) -> None:
    _fresh_factory.setenv("EMBEDDING_ENABLED", "false")
    assert _reload_client() is None


def test_factory_local_returns_client_without_loading_model(_fresh_factory) -> None:
    _fresh_factory.setenv("EMBEDDING_ENABLED", "true")
    _fresh_factory.setenv("EMBEDDING_PROVIDER", "local")
    client = _reload_client()
    assert isinstance(client, LocalEmbeddingClient)
    assert client._model is None  # 懒加载：拿到客户端不等于加载模型


def test_factory_openai_missing_key_returns_none(_fresh_factory) -> None:
    _fresh_factory.setenv("EMBEDDING_ENABLED", "true")
    _fresh_factory.setenv("EMBEDDING_PROVIDER", "openai_compatible")
    _fresh_factory.setenv("EMBEDDING_BASE_URL", "https://example.com/v1")
    _fresh_factory.setenv("EMBEDDING_API_KEY", "")
    assert _reload_client() is None


def test_factory_unknown_provider_returns_none(_fresh_factory) -> None:
    from types import SimpleNamespace

    import app.services.ai_embedding as mod

    # Literal 校验在 settings 加载层已拦截非法值，这里直测工厂的防御分支
    _fresh_factory.setattr(
        mod, "get_settings",
        lambda: SimpleNamespace(embedding_enabled=True, embedding_provider="mystery"),
    )
    assert mod.get_embedding_client(refresh=True) is None


# ---------------------------------------------------------------------------
# 本地客户端（假模型）
# ---------------------------------------------------------------------------


class FakeST:
    """假 sentence-transformers 模型：记录入参，返回全 1 向量。"""

    def __init__(self, dim: int = 4) -> None:
        self.dim = dim
        self.calls: list[dict[str, Any]] = []

    def encode(self, texts: list[str], **kwargs: Any) -> np.ndarray:
        self.calls.append({"texts": list(texts), **kwargs})
        return np.ones((len(texts), self.dim), dtype="float32")


def _local(fake: FakeST, model_name: str = "BAAI/bge-small-zh-v1.5") -> LocalEmbeddingClient:
    return LocalEmbeddingClient(
        model_name=model_name,
        device="cpu",
        cache_dir="",
        batch_size=8,
        expected_dim=fake.dim,
        model_loader=lambda: fake,
    )


def test_local_lazy_loads_once() -> None:
    loaded = []
    fake = FakeST()
    client = LocalEmbeddingClient(
        model_name="m",
        device="cpu",
        cache_dir="",
        batch_size=8,
        expected_dim=fake.dim,
        model_loader=lambda: loaded.append(1) or fake,
    )
    assert loaded == []
    client.embed_documents(["a", "b"])
    client.embed_query("c")
    assert loaded == [1]  # 只加载一次


def test_local_documents_encode_and_normalize_flag() -> None:
    fake = FakeST()
    vectors = _local(fake).embed_documents(["合同续签", "冰柜维修"])
    assert len(vectors) == 2 and len(vectors[0]) == fake.dim
    assert fake.calls[0]["texts"] == ["合同续签", "冰柜维修"]  # 文档侧不加前缀
    assert fake.calls[0]["normalize_embeddings"] is True


def test_local_query_gets_bge_instruction() -> None:
    fake = FakeST()
    _local(fake).embed_query("续签评估什么时候触发")
    assert fake.calls[0]["texts"] == ["为这个句子生成表示以用于检索相关文章：续签评估什么时候触发"]


def test_local_non_bge_query_no_instruction() -> None:
    fake = FakeST()
    _local(fake, model_name="text-embedding-3-small").embed_query("hi")
    assert fake.calls[0]["texts"] == ["hi"]


def test_local_empty_input_skips_model() -> None:
    client = _local(FakeST())
    assert client.embed_documents([]) == []
    assert client._model is None


def test_local_hf_env_offline_when_cached(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """缓存已存在 → 离线加载；不存在 → 指向镜像（用户显式 env 优先）。"""
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    monkeypatch.delenv("HF_ENDPOINT", raising=False)
    cached = tmp_path / "models--BAAI--bge-small-zh-v1.5"
    cached.mkdir()
    client = LocalEmbeddingClient(
        model_name="BAAI/bge-small-zh-v1.5", device="cpu",
        cache_dir=str(tmp_path), batch_size=8, expected_dim=4,
    )
    client._prepare_hf_env()
    assert os.environ.get("HF_HUB_OFFLINE") == "1"
    assert "HF_ENDPOINT" not in os.environ


def test_local_hf_env_mirror_when_not_cached(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    monkeypatch.delenv("HF_ENDPOINT", raising=False)
    client = LocalEmbeddingClient(
        model_name="BAAI/bge-small-zh-v1.5", device="cpu",
        cache_dir=str(tmp_path), batch_size=8, expected_dim=4,
    )
    client._prepare_hf_env()
    assert os.environ.get("HF_ENDPOINT") == "https://hf-mirror.com"
    assert "HF_HUB_OFFLINE" not in os.environ


def test_local_missing_dependency_raises_masked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "sentence_transformers", None)
    client = LocalEmbeddingClient(
        model_name="m", device="cpu", cache_dir="", batch_size=8, expected_dim=4
    )
    with pytest.raises(EmbeddingUnavailableError, match="sentence-transformers"):
        client.embed_documents(["x"])


def test_local_inference_failure_masked() -> None:
    def boom() -> Any:
        raise RuntimeError("should not surface")

    client = LocalEmbeddingClient(
        model_name="m", device="cpu", cache_dir="", batch_size=8, expected_dim=4,
        model_loader=boom,
    )
    with pytest.raises(EmbeddingUnavailableError, match="RuntimeError"):
        client.embed_documents(["x"])


def test_local_dimension_mismatch_raises() -> None:
    fake = FakeST(dim=8)
    client = _local(fake)  # expected_dim 用 fake.dim=4 构造，这里改预期制造不符
    client._dim = 4
    with pytest.raises(EmbeddingUnavailableError, match="维度不符"):
        client.embed_documents(["x"])


# ---------------------------------------------------------------------------
# OpenAI 兼容端点（MockTransport）
# ---------------------------------------------------------------------------


def _openai(handler: Any, batch_size: int = 2) -> tuple[OpenAiCompatibleEmbeddingClient, list]:
    requests: list[dict[str, Any]] = []

    def recording(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return handler(request)

    client = OpenAiCompatibleEmbeddingClient(
        base_url="https://example.com/v1",
        api_key="secret-key-should-not-leak",
        model="embedding-3",
        batch_size=batch_size,
        timeout_seconds=5.0,
        expected_dim=4,
        transport=httpx.MockTransport(recording),
    )
    return client, requests


def _ok_response(payload: dict[str, Any], dim: int = 4) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "data": [
                {"index": i, "embedding": [0.1] * dim} for i, _ in enumerate(payload["input"])
            ]
        },
    )


def test_openai_batches_and_preserves_order() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        # 故意乱序返回，客户端必须按 index 还原
        data = [{"index": i, "embedding": [float(i)] * 4} for i in range(len(payload["input"]))]
        data.reverse()
        return httpx.Response(200, json={"data": data})

    client, requests = _openai(handler, batch_size=2)
    vectors = client.embed_documents(["a", "b", "c", "d", "e"])
    assert len(requests) == 3  # 2+2+1 三批
    assert [len(r["input"]) for r in requests] == [2, 2, 1]
    # 每批内按 index 还原顺序
    assert vectors[0] == [0.0] * 4 and vectors[1] == [1.0] * 4


def test_openai_retry_on_500_then_success() -> None:
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx.Response(500, json={"error": {"code": "internal"}})
        return _ok_response(json.loads(request.content))

    client, _ = _openai(handler)
    assert client.embed_query("你好") == [0.1] * 4
    assert attempts["n"] == 2


def test_openai_429_fails_fast_and_masked() -> None:
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(429, json={"error": {"code": "1113"}})

    client, _ = _openai(handler)
    with pytest.raises(EmbeddingUnavailableError) as excinfo:
        client.embed_query("你好")
    assert attempts["n"] == 1  # 配额类错误不重试
    assert "429" in str(excinfo.value) and "1113" in str(excinfo.value)
    assert "secret-key" not in str(excinfo.value)


def test_openai_timeout_retries_then_raises() -> None:
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        raise httpx.ConnectTimeout("timeout", request=request)

    client, _ = _openai(handler)
    with pytest.raises(EmbeddingUnavailableError, match="ConnectTimeout"):
        client.embed_query("你好")
    assert attempts["n"] == 2


def test_openai_dimension_mismatch_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _ok_response(json.loads(request.content), dim=8)

    client, _ = _openai(handler)  # expected_dim=4
    with pytest.raises(EmbeddingUnavailableError, match="维度不符"):
        client.embed_query("你好")


# ---------------------------------------------------------------------------
# 相似度
# ---------------------------------------------------------------------------


def test_cosine_similarity_sanity() -> None:
    assert cosine_similarity([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
    assert cosine_similarity([0.0, 0.0], [1.0, 0.0]) == 0.0
