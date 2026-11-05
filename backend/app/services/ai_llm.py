"""OpenAI 兼容的对话模型客户端（Moonshot/Kimi、OpenAI 及中转端点，PRD §5.1、§9.1）。

边界：
- 只做「规划下一步调用哪个只读工具」与「把工具返回组织成中文回答」两件事；
- 不直接访问数据库，不持有业务规则；工具白名单与参数校验始终在服务端；
- 密钥只来自配置（`SecretStr`），日志与异常信息不得包含明文或响应正文；
- 失败一律抛 `LlmUnavailableError`，由上层回退到确定性规则答案，不返回 500。
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.core.config import get_settings

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 25.0
# 思考型模型（Kimi K2 系列）的 reasoning token 同样计入 max_tokens：
# 预留不足会导致 content 为空（finish_reason=length），因此默认放宽。
DEFAULT_MAX_TOKENS = 1200
# 5xx 属瞬时故障：重试一次，仍失败由上层降级
_RETRYABLE_STATUS = frozenset({500, 502, 503, 504})
# 429 细分（实测 Moonshot 瞬时限流 1~3s 级，单次快重试常仍撞同一窗口）：
# - 瞬时限流码（rate_limit/throttle 类）：递增退避最多 3 次；
# - 欠费/配额码（智谱 1113 等）：重试只会再吃一次计费拒绝，立即失败降级
_RATE_LIMIT_MAX_ATTEMPTS = 3
_QUOTA_429_MARKERS = ("1113", "balance", "billing", "insufficient", "quota")
# 走 OpenAI Chat Completions 协议的供应商（切换厂商只改 .env，不改代码）
_OPENAI_COMPAT_PROVIDERS = frozenset({"moonshot", "openai"})


class LlmUnavailableError(Exception):
    """模型不可用（网络/HTTP/响应结构异常）：上层必须降级，不得采纳半成品。"""


@dataclass(frozen=True)
class LlmToolCall:
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class LlmReply:
    content: str
    tool_calls: tuple[LlmToolCall, ...] = ()
    model: str = ""
    usage: dict[str, Any] = field(default_factory=dict)


class OpenAiCompatClient:
    """最小 OpenAI Chat Completions 客户端（支持 tools / tool_calls）。"""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self._api_key = api_key
        self._timeout = timeout
        self._transport = transport

    def complete(
        self,
        *,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]] | None = None,
        temperature: float | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> LlmReply:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": list(messages),
            "max_tokens": max_tokens,
        }
        # 推理型模型（kimi-k2 系列）只接受 temperature=1：不传由服务端决定，
        # 避免不同模型的采样参数约束把请求打成 400。
        if temperature is not None:
            payload["temperature"] = temperature
        if tools:
            # 不发 tool_choice：Moonshot 该字段仅支持具名函数，传 "auto" 会 400；
            # 不传时由模型自行决定是否调用工具。
            payload["tools"] = list(tools)

        response: httpx.Response | None = None
        last_error: Exception | None = None
        attempt = 0
        while True:
            attempt += 1
            try:
                with httpx.Client(
                    timeout=self._timeout, transport=self._transport
                ) as client:
                    response = client.post(
                        f"{self.base_url}/chat/completions",
                        headers={
                            "Authorization": f"Bearer {self._api_key}",
                            "Content-Type": "application/json",
                        },
                        json=payload,
                    )
            except httpx.HTTPError as exc:
                # 只暴露异常类型，不回传请求头/响应体（可能含凭据或业务数据）
                last_error = exc
                if attempt < 2:
                    time.sleep(0.5)
                    continue
                raise LlmUnavailableError(
                    f"模型请求失败：{type(exc).__name__}"
                ) from exc

            status = response.status_code
            if status == 429:
                code = _error_code(response).lower()
                if any(marker in code for marker in _QUOTA_429_MARKERS):
                    break  # 欠费/配额：立即交上层降级
                if attempt < _RATE_LIMIT_MAX_ATTEMPTS:
                    # 退避随次数递增（1.2s → 2.4s → 3.6s，上限 4s），
                    # 覆盖限流窗口又不过度拖长对话响应
                    time.sleep(min(_retry_after(response) * attempt, 4.0))
                    continue
                break
            if status in _RETRYABLE_STATUS and attempt < 2:
                time.sleep(_retry_after(response))
                continue
            break

        if response is None:
            reason = f"模型请求失败：{type(last_error).__name__ if last_error else 'unknown'}"
            # 只含异常类型名（无凭据/正文），便于回查降级根因
            logger.warning("chat llm unavailable: %s (model=%s)", reason, self.model)
            raise LlmUnavailableError(reason)
        if response.status_code >= 400:
            reason = f"模型返回 HTTP {response.status_code}" + (
                f"（{_error_code(response)}）" if _error_code(response) else ""
            )
            # 只含 HTTP 状态与提供方错误码（无凭据/提示词/正文）
            logger.warning("chat llm unavailable: %s (model=%s)", reason, self.model)
            raise LlmUnavailableError(reason)
        try:
            return _parse_reply(response, model=self.model)
        except LlmUnavailableError as exc:
            # 结构异常/输出截断（reasoning 占满 max_tokens）等，便于回查
            logger.warning("chat llm unavailable: %s (model=%s)", exc, self.model)
            raise


def _retry_after(response: httpx.Response) -> float:
    """读取 Retry-After；缺省 1.2s（Moonshot 限流提示通常为 1s 级），最多等 3s。"""
    raw = response.headers.get("retry-after")
    if raw:
        try:
            return min(max(float(raw), 0.2), 3.0)
        except ValueError:
            pass
    return 1.2


def _error_code(response: httpx.Response) -> str:
    """提取错误类型码用于排障（只取 code/type，不含请求头与提示词）。"""
    try:
        data = response.json()
    except ValueError:
        return ""
    error = data.get("error")
    if not isinstance(error, dict):
        return ""
    for key in ("code", "type"):
        value = error.get(key)
        if isinstance(value, str) and value:
            return value[:64]
    return ""


def _parse_reply(response: httpx.Response, *, model: str) -> LlmReply:
    try:
        data = response.json()
    except ValueError as exc:
        raise LlmUnavailableError("模型返回非 JSON") from exc
    choices = data.get("choices") or []
    if not choices:
        raise LlmUnavailableError("模型返回缺少 choices")
    message = choices[0].get("message") or {}
    content = message.get("content")
    text = content.strip() if isinstance(content, str) else ""

    calls: list[LlmToolCall] = []
    for raw in message.get("tool_calls") or []:
        function = raw.get("function") or {}
        name = function.get("name")
        if not isinstance(name, str) or not name:
            continue
        arguments = function.get("arguments") or "{}"
        try:
            parsed = json.loads(arguments) if isinstance(arguments, str) else arguments
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            calls.append(LlmToolCall(name=name, arguments=parsed))

    if not text and not calls:
        finish = str(choices[0].get("finish_reason") or "")
        if finish == "length":
            raise LlmUnavailableError("模型输出被截断（reasoning 占用 max_tokens）")
        raise LlmUnavailableError("模型返回既无文本也无工具调用")

    usage = data.get("usage")
    return LlmReply(
        content=text,
        tool_calls=tuple(calls),
        model=str(data.get("model") or model),
        usage=usage if isinstance(usage, dict) else {},
    )


def get_chat_llm() -> OpenAiCompatClient | None:
    """按配置返回对话模型；未启用或缺少密钥时返回 None（走确定性回退）。"""
    settings = get_settings()
    if settings.ai_chat_provider not in _OPENAI_COMPAT_PROVIDERS:
        return None
    if settings.ai_api_key is None:
        return None
    api_key = settings.ai_api_key.get_secret_value()
    if not api_key:
        return None
    return OpenAiCompatClient(
        base_url=settings.ai_base_url,
        api_key=api_key,
        model=settings.ai_model,
        timeout=settings.ai_chat_timeout_seconds,
    )
