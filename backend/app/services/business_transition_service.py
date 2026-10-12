"""业务域状态迁移服务（T05 / PRD §4.5/§4.6/§4.8/§4.9/§4.10；R24）。

统一入口 `BusinessTransitionService.transition(...)`：
- 接收任意带 `.id` 和 `.status` 的 ORM 实体（合同/冰柜/补货/整改/采购/绩效/工资
  共 7 域），统一校验 from→to 合法性后写入新状态并审计；
- 不在 Service 范围内做"业务规则校验"（如冰柜是否在该商家、绩效是否已
  经理确认），由各业务卡在自己的 Service 中处理；
- **同事务审计**（R23/R30）：由 `AuditService.record_state_transition` 在
  本 session 内落 `audit_logs`。

7 业务域的合法迁移图见 `app.services.state_machine`。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.services.audit_service import AuditService
from app.services.state_machine import assert_legal_transition


class BusinessTransitionService:
    """跨域统一状态迁移入口。"""

    DEFAULT_ATTR = "status"

    def __init__(self, session: Session) -> None:
        self.session = session

    def transition(
        self,
        *,
        entity: Any,
        domain: str,
        to_state: str,
        actor_id: UUID,
        reason: str,
        attr: str = DEFAULT_ATTR,
        extra_after: dict[str, Any] | None = None,
    ) -> Any:
        """执行状态迁移；非法跳转抛 DomainError(STATE_TRANSITION_INVALID)。

        - **Q025 核心**：调用 `assert_legal_transition`；非法直接抛错，实体不变；
        - **同事务审计**（R23/R30）：`record_state_transition` 写
          `audit_logs`（reason 必填，AuditService 强制）；
        - 不 commit：调用方事务边界。
        """
        if not reason or not reason.strip():
            raise ValueError("业务状态迁移必须填写原因（R24）")
        frm = getattr(entity, attr)
        assert_legal_transition(domain, frm, to_state)
        before = {attr: frm}
        setattr(entity, attr, to_state)
        self.session.flush()
        after: dict[str, Any] = {attr: to_state}
        if extra_after:
            after.update(extra_after)
        AuditService(self.session).record_state_transition(
            object_type=domain,
            object_id=entity.id,
            actor_id=actor_id,
            before=before,
            after=after,
            reason=reason,
        )
        return entity

    # 便捷方法：仅校验合法性（不写库）— 供业务 Service 在更新前做 dry-run
    @staticmethod
    def validate(*, domain: str, frm: str, to: str) -> None:
        assert_legal_transition(domain, frm, to)
