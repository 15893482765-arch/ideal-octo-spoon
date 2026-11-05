"""通用速率限制器（T29 / PRD §9.4：接口限流）。

- 固定窗口 + 原子 INCR/EXPIRE，语义足够且对 Redis/内存都易实现；
- 后端可注入：`InMemoryRateBackend`（单进程/测试默认）与 `RedisRateBackend`
  （生产多实例共享，接口不变）；
- 只做"判定 + 计数"，不直接耦合 HTTP；中间件负责构造 key 与返回 429；
- Redis 不可用时**故障开放（fail-open）**并计一次降级：限流是防护而非核心业务，
  不应因 Redis 抖动让登录/巡访等核心动作整体不可用（Q172）。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Protocol


class RateBackend(Protocol):
    def hit(self, key: str, limit: int, window_seconds: int, now: float) -> RateDecision: ...


@dataclass(frozen=True)
class RateDecision:
    allowed: bool
    remaining: int
    limit: int
    retry_after_seconds: int


class InMemoryRateBackend:
    """进程内固定窗口计数（测试/单实例）。线程安全。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._hits: dict[str, tuple[int, int]] = {}  # key -> (window_start_epoch, count)

    def hit(self, key: str, limit: int, window_seconds: int, now: float) -> RateDecision:
        window = int(now // window_seconds)
        with self._lock:
            # 惰性清理过期窗口，避免长运行进程内存无限增长
            stale = [k for k, (w, _) in self._hits.items() if w != window and w < window]
            for k in stale:
                self._hits.pop(k, None)
            start, count = self._hits.get(key, (window, 0))
            if start != window:
                start, count = window, 0
            count += 1
            self._hits[key] = (start, count)
            allowed = count <= limit
            remaining = max(0, limit - count)
            retry_after = 0 if allowed else (window + 1) * window_seconds - int(now)
            return RateDecision(allowed, remaining, limit, max(0, retry_after))

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


class RedisRateBackend:
    """Redis 固定窗口（生产多实例共享）。

    用 Lua/管道保证 INCR 与首包 EXPIRE 的原子性；连接异常时 fail-open。
    """

    def __init__(self, client: object, *, on_backend_error: object | None = None) -> None:
        self._client = client
        self._on_backend_error = on_backend_error

    def hit(self, key: str, limit: int, window_seconds: int, now: float) -> RateDecision:
        window = int(now // window_seconds)
        redis_key = f"ratelimit:{key}:{window}"
        try:
            pipe = self._client.pipeline()  # type: ignore[attr-defined]
            pipe.incr(redis_key)
            pipe.expire(redis_key, window_seconds + 1)
            count, _ = pipe.execute()
            count = int(count)
        except Exception as exc:  # noqa: BLE001 - 限流后端故障：fail-open
            if callable(self._on_backend_error):
                self._on_backend_error(exc)
            return RateDecision(True, limit, limit, 0)
        allowed = count <= limit
        remaining = max(0, limit - count)
        retry_after = 0 if allowed else (window + 1) * window_seconds - int(now)
        return RateDecision(allowed, remaining, limit, max(0, retry_after))


@dataclass(frozen=True)
class RateRule:
    """限流规则：窗口内最多 max_requests 次。"""

    name: str
    max_requests: int
    window_seconds: int


class RateLimiter:
    """按规则名 + 身份键做限流判定。"""

    def __init__(self, backend: RateBackend | None = None, *, clock: object = time.time) -> None:
        self._backend: RateBackend = backend or InMemoryRateBackend()
        self._clock = clock

    def check(self, rule: RateRule, identity: str) -> RateDecision:
        now = float(self._clock())  # type: ignore[operator]
        key = f"{rule.name}:{identity}"
        return self._backend.hit(key, rule.max_requests, rule.window_seconds, now)

    def reset(self) -> None:
        """清空内存后端计数（测试隔离用；Redis 后端为 no-op）。"""
        reset = getattr(self._backend, "reset", None)
        if callable(reset):
            reset()


# 进程级默认限流器（内存后端；生产由 main 装配时替换为 Redis 后端）
rate_limiter = RateLimiter()
