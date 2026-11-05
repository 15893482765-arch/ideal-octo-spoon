"""OpenAiCompatClient 重试政策单测（MockTransport，不触网）。

429 细分（实测 Moonshot 瞬时限流 1~3s 级）：
- 瞬时限流码（rate_limit_reached_error 等）→ 递增退避最多 3 次；
- 欠费/配额码（智谱 1113 等）→ 立即失败，不浪费计费拒绝；
- 5xx / 网络错误 → 保持原单次重试。
另覆盖 lifespan 的 embedding 预热：异常必须被吞掉（降级关键词兜底）。
"""

from __future__ import annotations

import httpx
import pytest

from app.services.ai_llm import (
    LlmUnavailableError,
    OpenAiCompatClient,
)

_OK_BODY = {
    "choices": [{"message": {"content": "好"}, "finish_reason": "stop"}],
    "model": "m",
}
_RATE_LIMIT_BODY = {
    "error": {"type": "rate_limit_reached_error", "message": "too many requests"}
}
_QUOTA_BODY = {"error": {"code": "1113", "message": "账户欠费"}}


def _client(handler, calls: list[int]) -> OpenAiCompatClient:
    def counting(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return handler(request)

    return OpenAiCompatClient(
        base_url="https://api.test",
        api_key="k",
        model="m",
        timeout=1,
        transport=httpx.MockTransport(counting),
    )


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.services.ai_llm.time.sleep", lambda *_: None)


def test_rate_limit_429_retried_until_success() -> None:
    calls: list[int] = []
    responses = iter(
        [
            httpx.Response(429, json=_RATE_LIMIT_BODY),
            httpx.Response(429, json=_RATE_LIMIT_BODY),
            httpx.Response(200, json=_OK_BODY),
        ]
    )
    client = _client(lambda _req: next(responses), calls)

    reply = client.complete(messages=[{"role": "user", "content": "hi"}])

    assert reply.content == "好"
    assert len(calls) == 3, "瞬时限流应递增退避重试，第三次成功"


def test_rate_limit_429_exhausts_three_attempts() -> None:
    calls: list[int] = []
    client = _client(lambda _req: httpx.Response(429, json=_RATE_LIMIT_BODY), calls)

    with pytest.raises(LlmUnavailableError, match="429"):
        client.complete(messages=[{"role": "user", "content": "hi"}])
    assert len(calls) == 3, "瞬时限流最多 3 次后交上层降级"


def test_quota_429_fails_fast_without_retry() -> None:
    calls: list[int] = []
    client = _client(lambda _req: httpx.Response(429, json=_QUOTA_BODY), calls)

    with pytest.raises(LlmUnavailableError, match="429"):
        client.complete(messages=[{"role": "user", "content": "hi"}])
    assert len(calls) == 1, "欠费/配额码重试无意义，必须立即失败"


def test_server_error_keeps_single_retry() -> None:
    calls: list[int] = []
    responses = iter(
        [httpx.Response(503, json={"error": {}}), httpx.Response(200, json=_OK_BODY)]
    )
    client = _client(lambda _req: next(responses), calls)

    reply = client.complete(messages=[{"role": "user", "content": "hi"}])

    assert reply.content == "好"
    assert len(calls) == 2


def test_success_on_first_try() -> None:
    calls: list[int] = []
    client = _client(lambda _req: httpx.Response(200, json=_OK_BODY), calls)

    assert client.complete(messages=[{"role": "user", "content": "hi"}]).content == "好"
    assert len(calls) == 1


def test_warmup_swallows_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """预热失败不致命：检索侧会按需加载或降级关键词。"""
    from app.main import _warm_embedding_model

    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("model load failed")

    monkeypatch.setattr(
        "app.services.ai_embedding.get_embedding_client", lambda: boom()
    )
    _warm_embedding_model()  # 不抛异常即通过
