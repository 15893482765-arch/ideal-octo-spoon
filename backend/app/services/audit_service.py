"""AuditService（T04 / PRD §5、§8；R23、R30）。

红线：
- 与业务写操作**同一 session、同一事务**：本服务绝不自行 commit/rollback，
  事务归属调用方；业务回滚时审计一并消失（失败事务不留成功审计）；
- 前后值快照写库前先过 redact()，凭据正文永不进审计；
- 审计日志只增不改：本服务不提供任何 UPDATE/DELETE 方法。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.core.context import get_request_id
from app.core.logging import redact
from app.models import AuditLog

# 允许的事件类型与 audit_logs_event_type_check 对齐
EVENT_CREATE = "CREATE"
EVENT_UPDATE = "UPDATE"
EVENT_STATE_TRANSITION = "STATE_TRANSITION"
EVENT_DELETE_DENIED = "DELETE_DENIED"

_ALLOWED_EVENTS = frozenset(
    {EVENT_CREATE, EVENT_UPDATE, EVENT_STATE_TRANSITION, EVENT_DELETE_DENIED}
)


class AuditService:
    """写操作审计的唯一入口。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    def record(
        self,
        *,
        object_type: str,
        object_id: UUID,
        event_type: str,
        actor_id: UUID | None = None,
        before: dict[str, Any] | None = None,
        after: dict[str, Any] | None = None,
        reason: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> AuditLog:
        """在当前事务内写一条审计记录。

        - before/after/extra 全部先脱敏再入库；
        - reason：状态迁移/驳回/终止类操作必须由调用方填写（R24），本方法不强制，
          由各业务 Service 的校验兜底；
        - 不 commit：调用方事务提交即审计提交，回滚即审计回滚。
        """
        if event_type not in _ALLOWED_EVENTS:
            raise ValueError(f"非法审计事件类型: {event_type}")
        if len(object_type) > 48:
            raise ValueError("object_type 超长")
        if reason is not None and len(reason) > 2000:
            raise ValueError("reason 超长")

        log = AuditLog(
            object_type=object_type,
            object_id=object_id,
            event_type=event_type,
            actor_id=actor_id,
            request_id=get_request_id(),
            reason=reason,
            before_json=redact(before) if before else None,
            after_json=redact(after) if after else None,
            payload=redact(extra) if extra else {},
        )
        self.session.add(log)
        # flush 让调用方在提交前就能拿到 log.id（用于断言/联动），但不提交
        self.session.flush()
        return log

    def record_state_transition(
        self,
        *,
        object_type: str,
        object_id: UUID,
        actor_id: UUID | None,
        before: dict[str, Any],
        after: dict[str, Any],
        reason: str,
    ) -> AuditLog:
        """状态迁移审计：R24 要求必须填原因，此处强制。"""
        if not reason or not reason.strip():
            raise ValueError("状态迁移审计必须填写原因")
        return self.record(
            object_type=object_type,
            object_id=object_id,
            event_type=EVENT_STATE_TRANSITION,
            actor_id=actor_id,
            before=before,
            after=after,
            reason=reason,
        )
