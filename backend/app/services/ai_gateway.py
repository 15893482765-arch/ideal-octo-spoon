"""AiGateway：受控 AI 能力的统一韧性与审计入口（T26 / PRD §9.3、§5.4、§5.5）。

职责（横切，不承载业务裁决）：
1. **防注入**：外部数据（备注/合同/附件/OCR）先过 `inspect_external_text`，命中只
   剥离 + 审计标记，绝不改变白名单/角色/确认/审批（这些仍由服务端矩阵决定）；
2. **超时**：模型调用限时 15 秒；
3. **重试**：超时/提供方错误最多重试 1 次（共 ≤ ai_max_attempts 次）；
   结构化校验失败属确定性错误，不浪费重试（模型把事实答错无法靠重试纠正）；
4. **断路器**：打开期内不调用模型，直接走逐能力降级；
5. **审计**：每次调用（含降级/拒绝/错误）在同事务写 AiInteractionLog；
6. **失败指标**：累计调用/成功/失败/超时/降级/注入拦截；
7. **降级**：任何模型侧故障都回落到 `ai_degradation` 的确定性产出，业务不 500。

关键红线：本类**不做权限判断、不改确认级别、不自行扩数据范围**；`allowed_tools`
由调用方从服务端目录/矩阵计算后传入。
"""

from __future__ import annotations

import concurrent.futures
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.permissions import ActorContext
from app.services.ai_circuit_breaker import (
    FailureKind,
    get_circuit_breaker,
    record_call_metric,
)
from app.services.ai_interaction_service import (
    AiInteractionRecord,
    AiInteractionService,
)
from app.services.ai_prompt_guard import inspect_external_text
from app.services.ai_provider import AIOutputInvalid

# 模型调用的统一结果封装
OK = "OK"
DEGRADED = "DEGRADED"


class AiTimeoutError(Exception):
    """模型调用超过时限。"""


@dataclass
class AiCallResult:
    status: str
    output: Any = None
    degraded: bool = False
    degraded_reason: str | None = None
    latency_ms: int = 0
    attempts: int = 1
    injection_blocked: bool = False
    blocked_directives: tuple[str, ...] = ()
    error_code: str | None = None
    audit_id: Any | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == OK


class AiGateway:
    """编排一次受控 AI 能力调用：防护 → 韧性 → 审计 → 降级。"""

    def __init__(
        self,
        session: Session,
        *,
        breaker=None,
        executor: concurrent.futures.Executor | None = None,
        clock=time.monotonic,
    ) -> None:
        self.session = session
        self.settings = get_settings()
        self.breaker = breaker or get_circuit_breaker()
        self._executor = executor
        self._owns_executor = executor is None
        self._clock = clock
        self.audit = AiInteractionService(session)

    def close(self) -> None:
        if self._owns_executor and self._executor is not None:
            self._executor.shutdown(wait=False)
            self._executor = None

    # ============================== 主入口 ==============================

    def invoke(
        self,
        *,
        actor: ActorContext,
        intent: str,
        model: str,
        call: Callable[[], Any],
        degrade: Callable[[], Any],
        prompt: str = "",
        external_text: str = "",
        params: dict[str, Any] | None = None,
        scope: dict[str, Any] | None = None,
        allowed_tools: tuple[str, ...] = (),
        result_ref: str | None = None,
        confirm_ref: str | None = None,
        request_id: str | None = None,
        session_id: str | None = None,
    ) -> AiCallResult:
        """执行一次受控调用。

        :param call: 实际模型/Provider 调用（无参可调用），必须返回结构化对象；
        :param degrade: 模型不可用时的**确定性降级**可调用（返回降级 payload）。
        """
        params = params or {}
        scope = scope or {}

        # 1) 防注入：外部内容只作数据，剥离越权指令并标记（不影响权限）。
        guard = inspect_external_text(external_text or prompt)
        injection_blocked, blocked = guard.to_audit()

        started = self._clock()
        attempts = 0
        output: Any = None
        degraded = False
        degraded_reason: str | None = None
        error_code: str | None = None
        failure_kind: FailureKind | None = None

        # 2) 断路器打开：不调用模型，直接降级。
        if not self.breaker.allow_request():
            degraded = True
            degraded_reason = "AI_DEGRADED:CIRCUIT_OPEN"
            output = degrade()
            record_call_metric(
                intent, success=False, degraded=True,
                injection_blocked=injection_blocked,
            )
            attempts = 0
        else:
            # 3) 超时 + 最多 1 次重试；结构化校验失败不重试。
            max_attempts = max(1, int(self.settings.ai_max_attempts))
            last_exc: Exception | None = None
            while attempts < max_attempts:
                attempts += 1
                try:
                    output = self._run_with_timeout(call)
                    last_exc = None
                    break
                except AiTimeoutError as exc:
                    last_exc = exc
                    failure_kind = FailureKind.TIMEOUT
                    error_code = "AI_TIMEOUT"
                    continue  # 超时可重试一次
                except AIOutputInvalid as exc:
                    last_exc = exc
                    failure_kind = FailureKind.INVALID_OUTPUT
                    error_code = "AI_INVALID_OUTPUT"
                    break  # 确定性错误不重试
                except Exception as exc:  # noqa: BLE001 - 提供方错误需兜底降级
                    last_exc = exc
                    failure_kind = FailureKind.PROVIDER_ERROR
                    error_code = "AI_PROVIDER_ERROR"
                    continue  # 提供方故障可重试一次

            if last_exc is None:
                # 成功
                self.breaker.record_success()
                record_call_metric(
                    intent, success=True, injection_blocked=injection_blocked
                )
            else:
                # 全部尝试失败 → 断路器记账 + 降级
                from app.services.ai_circuit_breaker import CircuitState

                state = self.breaker.record_failure()
                record_call_metric(
                    intent, success=False, failure_kind=failure_kind, degraded=True,
                    injection_blocked=injection_blocked,
                )
                degraded = True
                degraded_reason = (
                    "AI_DEGRADED:CIRCUIT_OPEN"
                    if state == CircuitState.OPEN
                    else f"AI_DEGRADED:{error_code or 'PROVIDER_ERROR'}"
                )
                output = degrade()

        latency_ms = int((self._clock() - started) * 1000)
        status = DEGRADED if degraded else OK

        # 4) 同事务审计（脱敏在 Service 内完成）。
        record = AiInteractionRecord(
            intent=intent,
            model=model,
            allowed_tools=allowed_tools,
            prompt_redacted=guard.cleaned_text if (external_text or prompt) else "",
            params=params,
            scope=scope,
            result_ref=result_ref,
            confirm_ref=confirm_ref,
            latency_ms=latency_ms,
            attempts=attempts,
            degraded=degraded,
            degraded_reason=degraded_reason,
            injection_blocked=injection_blocked,
            blocked_directives=blocked,
            status=status,
            error_code=error_code if degraded else None,
            error_message=None if not degraded else (
                "model unavailable; deterministic fallback returned"
            ),
        )
        log = self.audit.record(
            actor=actor, request_id=request_id, session_id=session_id, data=record
        )

        return AiCallResult(
            status=status,
            output=output,
            degraded=degraded,
            degraded_reason=degraded_reason,
            latency_ms=latency_ms,
            attempts=attempts,
            injection_blocked=injection_blocked,
            blocked_directives=blocked,
            error_code=error_code if degraded else None,
            audit_id=getattr(log, "id", None),
        )

    # ============================== 超时实现 ==============================

    def _run_with_timeout(self, call: Callable[[], Any]) -> Any:
        timeout = float(self.settings.ai_timeout_seconds)
        executor = self._get_executor()
        future = executor.submit(call)
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError as exc:
            future.cancel()
            raise AiTimeoutError(f"模型调用超过 {timeout:g}s 时限") from exc

    def _get_executor(self) -> concurrent.futures.Executor:
        if self._executor is None:
            self._executor = concurrent.futures.ThreadPoolExecutor(
                max_workers=4, thread_name_prefix="ai-gateway"
            )
            self._owns_executor = True
        return self._executor
