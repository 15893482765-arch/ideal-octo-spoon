"""AI 模型断路器与失败指标（T26 / PRD §9.3、§9.4）。

- 连续失败达到阈值 → 打开（OPEN）：AiGateway 不再调用模型，直接逐能力降级，
  避免拖垮业务页面（业务不因 AI 故障 500）；
- 打开后经过冷却时间 → 半开（HALF_OPEN）：放一次试探调用；
  成功 → 关闭（CLOSED）并清零失败计数；失败 → 重新打开；
- 失败指标（PRD §9.4 AI 工具失败率）：计数调用/成功/失败/超时/降级/拦截注入，
  供监控读取（首版进程内内存计数；多实例/Prometheus 属后续可观测卡）。

线程安全：用可重入锁保护状态；时间以单调钟计时（不依赖系统墙钟跳变）。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from enum import StrEnum

from app.core.config import get_settings


class CircuitState(StrEnum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


class FailureKind(StrEnum):
    TIMEOUT = "TIMEOUT"
    INVALID_OUTPUT = "INVALID_OUTPUT"
    PROVIDER_ERROR = "PROVIDER_ERROR"


@dataclass
class AIFailureMetrics:
    total: int = 0
    success: int = 0
    failed: int = 0
    timeouts: int = 0
    invalid_outputs: int = 0
    degraded: int = 0
    injection_blocked: int = 0
    by_capability: dict[str, dict[str, int]] = field(default_factory=dict)

    def as_dict(self) -> dict:
        rate = (self.failed / self.total) if self.total else 0.0
        return {
            "total": self.total,
            "success": self.success,
            "failed": self.failed,
            "timeouts": self.timeouts,
            "invalid_outputs": self.invalid_outputs,
            "degraded": self.degraded,
            "injection_blocked": self.injection_blocked,
            "failure_rate": round(rate, 4),
            "by_capability": self._copy_by_cap(),
        }

    def _copy_by_cap(self) -> dict:
        return {cap: dict(v) for cap, v in self.by_capability.items()}


class CircuitBreaker:
    """进程内单例断路器。"""

    def __init__(
        self,
        *,
        failure_threshold: int | None = None,
        open_cooldown_seconds: float | None = None,
        clock=time.monotonic,
    ) -> None:
        settings = get_settings()
        self._failure_threshold = (
            failure_threshold if failure_threshold is not None
            else settings.ai_circuit_failure_threshold
        )
        self._open_cooldown = (
            open_cooldown_seconds if open_cooldown_seconds is not None
            else settings.ai_circuit_open_cooldown_seconds
        )
        self._clock = clock
        self._lock = threading.RLock()
        self._state = CircuitState.CLOSED
        self._consecutive_failures = 0
        self._opened_at: float | None = None

    # ----- 状态判定 -----
    def state(self) -> CircuitState:
        with self._lock:
            if self._state == CircuitState.OPEN:
                # 冷却到期 → 半开（允许一次试探）
                if self._opened_at is not None and (
                    self._clock() - self._opened_at >= self._open_cooldown
                ):
                    self._state = CircuitState.HALF_OPEN
            return self._state

    def allow_request(self) -> bool:
        """CLOSED / HALF_OPEN 放行；OPEN 直接拒绝（走降级）。"""
        return self.state() in (CircuitState.CLOSED, CircuitState.HALF_OPEN)

    # ----- 结果回填 -----
    def record_success(self) -> None:
        with self._lock:
            self._state = CircuitState.CLOSED
            self._consecutive_failures = 0
            self._opened_at = None

    def record_failure(self) -> CircuitState:
        with self._lock:
            self._consecutive_failures += 1
            if (
                self._state == CircuitState.HALF_OPEN
                or self._consecutive_failures >= self._failure_threshold
            ):
                self._state = CircuitState.OPEN
                self._opened_at = self._clock()
            return self._state

    # ----- 测试运维辅助 -----
    def reset(self) -> None:
        with self._lock:
            self._state = CircuitState.CLOSED
            self._consecutive_failures = 0
            self._opened_at = None


# 全局单例（进程内）
_breaker: CircuitBreaker | None = None
_metrics = AIFailureMetrics()
_metrics_lock = threading.Lock()
_singleton_lock = threading.Lock()


def get_circuit_breaker() -> CircuitBreaker:
    global _breaker
    if _breaker is None:
        with _singleton_lock:
            if _breaker is None:
                _breaker = CircuitBreaker()
    return _breaker


def get_failure_metrics() -> AIFailureMetrics:
    return _metrics


def record_call_metric(
    capability: str,
    *,
    success: bool,
    failure_kind: FailureKind | None = None,
    degraded: bool = False,
    injection_blocked: bool = False,
) -> None:
    """累计一次 AI 调用的结果指标（线程安全）。"""
    with _metrics_lock:
        _metrics.total += 1
        cap = _metrics.by_capability.setdefault(
            capability,
            {"total": 0, "success": 0, "failed": 0, "degraded": 0},
        )
        cap["total"] += 1
        if success:
            _metrics.success += 1
            cap["success"] += 1
        else:
            _metrics.failed += 1
            cap["failed"] += 1
        if degraded:
            _metrics.degraded += 1
            cap["degraded"] += 1
        if failure_kind == FailureKind.TIMEOUT:
            _metrics.timeouts += 1
        if failure_kind == FailureKind.INVALID_OUTPUT:
            _metrics.invalid_outputs += 1
        if injection_blocked:
            _metrics.injection_blocked += 1


def reset_metrics() -> None:
    global _metrics
    with _metrics_lock:
        _metrics = AIFailureMetrics()
