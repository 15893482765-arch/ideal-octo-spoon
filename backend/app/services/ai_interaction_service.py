"""AI 交互审计服务（T26 / PRD §5.4-6、§9.3 AiGateway、R30）。

与 AuditService 同一原则：
- **同一 session、同一事务**：不自行 commit/rollback；业务回滚则审计一并回滚；
- 写入前统一脱敏：只存脱敏问题与参数摘要，绝不落密码/完整电话/精确坐标；
- 只增不改：不提供 UPDATE/DELETE。

审计列（PRD §5.4-6）：脱敏问题、意图、允许工具、数据范围、参数摘要、结果引用、
确认引用、模型、耗时、错误。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.core.ai_redaction import redact_prompt, summarize_params
from app.models.ai_interaction import (
    ALLOWED_INTENTS,
    ALLOWED_STATUS,
    INTENT_OTHER,
    STATUS_OK,
    AiInteractionLog,
)


@dataclass
class AiInteractionRecord:
    """一次 AI 交互的审计输入（全部为服务端可信数据 + 已脱敏文本）。"""

    intent: str
    model: str
    allowed_tools: tuple[str, ...] = ()
    prompt_redacted: str = ""
    params: dict[str, Any] = field(default_factory=dict)
    scope: dict[str, Any] = field(default_factory=dict)
    result_ref: str | None = None
    confirm_ref: str | None = None
    latency_ms: int = 0
    attempts: int = 1
    degraded: bool = False
    degraded_reason: str | None = None
    injection_blocked: bool = False
    blocked_directives: tuple[str, ...] = ()
    status: str = STATUS_OK
    error_code: str | None = None
    error_message: str | None = None


class AiInteractionService:
    """AI 交互审计唯一入口。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    def record(
        self,
        *,
        actor: Any | None,
        request_id: str | None,
        session_id: str | None,
        data: AiInteractionRecord,
    ) -> AiInteractionLog:
        if data.intent not in ALLOWED_INTENTS:
            raise ValueError(f"非法 AI 意图: {data.intent}")
        if data.status not in ALLOWED_STATUS:
            raise ValueError(f"非法 AI 交互状态: {data.status}")
        if data.latency_ms < 0:
            raise ValueError("latency_ms 不能为负")

        user_id: UUID | None = getattr(actor, "user_id", None)
        role_code: str | None = getattr(actor, "role_code", None)

        log = AiInteractionLog(
            user_id=user_id,
            role_code=role_code,
            session_id=(session_id or "")[:64] or None,
            request_id=(request_id or "")[:64] or None,
            intent=data.intent,
            allowed_tools=",".join(data.allowed_tools)[:4000],
            scope_json=summarize_params(data.scope),
            prompt_redacted=redact_prompt(data.prompt_redacted),
            params_summary_json=summarize_params(data.params),
            result_ref=(data.result_ref or "")[:128] or None,
            confirm_ref=(data.confirm_ref or "")[:128] or None,
            model=data.model[:64],
            latency_ms=int(data.latency_ms),
            attempts=int(data.attempts),
            degraded=bool(data.degraded),
            degraded_reason=(data.degraded_reason or "")[:64] or None,
            injection_blocked=bool(data.injection_blocked),
            blocked_directives_json=list(data.blocked_directives)[:50],
            status=data.status,
            error_code=(data.error_code or "")[:64] or None,
            error_message=redact_prompt(data.error_message) if data.error_message else None,
        )
        self.session.add(log)
        self.session.flush()
        return log

    def list_for_user(self, user_id: UUID, *, limit: int = 100) -> list[AiInteractionLog]:
        from sqlalchemy import select

        rows = self.session.scalars(
            select(AiInteractionLog)
            .where(AiInteractionLog.user_id == user_id)
            .order_by(AiInteractionLog.occurred_at.desc())
            .limit(limit)
        ).all()
        return list(rows)


def intent_other() -> str:
    return INTENT_OTHER
