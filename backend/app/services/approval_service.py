"""ApprovalService（T05 / PRD §5.3.1、§6.6；R24、R30）。

设计要点：
- **不可自审**（Q021）：调用方传入 `requester_id` 与 `actor.user_id` 比对；
- **节点能力校验**：动作级 required_ability 必须在审批人画像内；
- **退回/驳回原因**：`REJECTED`/`RETURNED` 必须填原因（`require_reason_for`）；
- **重复决定幂等**（Q022）：PENDING 唯一约束 + 状态守卫，二次 decide 直接拒；
- **同事务审计**（R23）：决定落地时由 `AuditService.record_state_transition`
  写入 `audit_logs`，与业务更新同 session 提交/回滚。
- **ApprovalRequest.action**：发起端写入；本服务只校验 action 是否登记
  （未登记即拒，体现"未配置完整时拒绝注册"的 §5.4 精神）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.core.errors import ConflictError, DomainError
from app.core.permissions import ActorContext
from app.models import ApprovalRequest, User
from app.models.enums import ApprovalStatus
from app.services.approval_matrix import (
    ApprovalRule,
    assert_decision_authority,
    assert_decision_reason,
    get_rule,
)
from app.services.audit_service import AuditService

DECISION_APPROVED = ApprovalStatus.APPROVED.value
DECISION_REJECTED = ApprovalStatus.REJECTED.value
DECISION_RETURNED = ApprovalStatus.RETURNED.value


class ApprovalService:
    def __init__(self, session: Session) -> None:
        self.session = session

    # -------- 发起审批（业务侧调用）--------

    def submit(
        self,
        *,
        target_object_type: str,
        target_object_id: UUID,
        action: str,
        requester_id: UUID,
        matrix_role_code: str,
        request_payload: dict[str, object] | None = None,
    ) -> ApprovalRequest:
        """提交一条审批申请。

        - action 必须在审批矩阵登记（未登记即拒，避免"未配置完整时拒绝注册"）；
        - 同 (target_object_type, target_object_id, action) PENDING 唯一：
          同一业务对象同一动作在 PENDING 期间不可再次发起（由部分唯一索引保障）；
        - 不 commit：业务调用方决定事务边界。
        """
        rule = get_rule(action)
        if rule is None:
            raise DomainError(
                code="APPROVAL_ACTION_NOT_REGISTERED",
                message="审批动作未在矩阵登记，禁止发起",
                details={"action": action},
            )
        req = ApprovalRequest(
            target_object_type=target_object_type,
            target_object_id=target_object_id,
            action=action,
            requester_id=requester_id,
            status=ApprovalStatus.PENDING.value,
            matrix_role_code=matrix_role_code,
            request_payload=request_payload or {},
        )
        self.session.add(req)
        try:
            self.session.flush()
        except Exception as exc:  # IntegrityError due to pending unique
            raise ConflictError(
                code="APPROVAL_ALREADY_PENDING",
                message="该业务对象同一动作已存在待审申请",
                details={
                    "target_object_type": target_object_type,
                    "target_object_id": str(target_object_id),
                    "action": action,
                },
            ) from exc
        return req

    # -------- 决定（审批人调用）--------

    def decide(
        self,
        *,
        approval_id: UUID,
        actor: ActorContext,
        decision: str,
        reason: str | None = None,
    ) -> ApprovalRequest:
        """审批人做决定（APPROVED / REJECTED / RETURNED）。

        - **Q021 不可自审**：`actor.user_id == requester_id` → `SELF_APPROVAL_FORBIDDEN`；
        - **Q022 重复决定**：PENDING 唯一 + 状态守卫；非 PENDING 直接拒；
        - **R24 原因**：REJECTED/RETURNED 强制 reason 非空（由 ApprovalRule 决定）；
        - **R23 审计**：落审计（state transition）。
        """
        if decision not in {DECISION_APPROVED, DECISION_REJECTED, DECISION_RETURNED}:
            raise DomainError(
                code="APPROVAL_DECISION_INVALID",
                message="非法审批决定",
                details={"decision": decision},
            )

        req = self.session.get(ApprovalRequest, approval_id)
        if req is None:
            raise DomainError(
                code="APPROVAL_NOT_FOUND",
                message="审批申请不存在",
                details={"approval_id": str(approval_id)},
            )
        if req.status != ApprovalStatus.PENDING.value:
            raise ConflictError(
                code="APPROVAL_NOT_PENDING",
                message="该审批已做出决定，不得重复处理",
                details={"approval_id": str(approval_id), "status": req.status},
            )

        rule: ApprovalRule | None = get_rule(req.action)
        if rule is None:
            raise DomainError(
                code="APPROVAL_ACTION_NOT_REGISTERED",
                message="审批动作未在矩阵登记，禁止决定",
                details={"action": req.action},
            )
        assert_decision_authority(rule=rule, actor=actor, requester_id=req.requester_id)
        assert_decision_reason(decision=decision, reason=reason, rule=rule)

        before = {
            "status": req.status,
            "approver_id": str(req.approver_id) if req.approver_id else None,
        }
        req.status = decision
        req.approver_id = actor.user_id
        req.reason = reason
        req.decided_at = datetime.now(tz=UTC)
        self.session.flush()

        # 同事务审计
        AuditService(self.session).record_state_transition(
            object_type="APPROVAL_REQUEST",
            object_id=req.id,
            actor_id=actor.user_id,
            before=before,
            after={
                "status": req.status,
                "approver_id": str(actor.user_id),
                "target_object_type": req.target_object_type,
                "target_object_id": str(req.target_object_id),
                "action": req.action,
            },
            reason=reason or f"审批决定 {decision}",
        )
        return req

    # -------- 申请人撤回（仅 PENDING 状态）--------

    def withdraw(self, *, approval_id: UUID, actor: ActorContext) -> ApprovalRequest:
        req = self.session.get(ApprovalRequest, approval_id)
        if req is None:
            raise DomainError(code="APPROVAL_NOT_FOUND", message="审批申请不存在")
        if req.status != ApprovalStatus.PENDING.value:
            raise ConflictError(
                code="APPROVAL_NOT_PENDING",
                message="仅 PENDING 状态可撤回",
                details={"status": req.status},
            )
        if req.requester_id != actor.user_id:
            raise DomainError(
                code="APPROVAL_WITHDRAW_FORBIDDEN",
                message="仅发起人可撤回",
            )
        before = {"status": req.status}
        req.status = ApprovalStatus.WITHDRAWN.value
        req.decided_at = datetime.now(tz=UTC)
        self.session.flush()
        AuditService(self.session).record_state_transition(
            object_type="APPROVAL_REQUEST",
            object_id=req.id,
            actor_id=actor.user_id,
            before=before,
            after={"status": req.status},
            reason="申请人撤回",
        )
        return req

    # -------- 查询 --------

    def get(self, approval_id: UUID) -> ApprovalRequest | None:
        return self.session.get(ApprovalRequest, approval_id)

    def list_pending(
        self, *, target_object_type: str | None = None, limit: int = 100
    ) -> list[ApprovalRequest]:
        stmt = select(ApprovalRequest).where(
            ApprovalRequest.status == ApprovalStatus.PENDING.value
        )
        if target_object_type is not None:
            stmt = stmt.where(ApprovalRequest.target_object_type == target_object_type)
        stmt = stmt.order_by(ApprovalRequest.created_at.asc()).limit(limit)
        return list(self.session.execute(stmt).scalars().all())

    def list_for_actor(
        self,
        actor: ActorContext,
        *,
        status: str | None = None,
        target_object_type: str | None = None,
        limit: int = 100,
    ) -> list[ApprovalRequest]:
        """本角色待办/已办审批（T23 `approval.list`）。

        行级裁剪只由服务端 actor 注入：
        - 待办（PENDING）：按**审批矩阵角色**路由到当前角色（`matrix_role_code`）；
        - 已办（APPROVED/REJECTED/RETURNED/WITHDRAWN）：按**实际审批人**归属；
        - 一律限定在当前用户数据集内（不跨数据域读取）。
        """
        user = self.session.get(User, actor.user_id)
        if user is None:
            raise DomainError(code="USER_NOT_FOUND", message="登录用户不存在")
        pending_route = and_(
            ApprovalRequest.status == ApprovalStatus.PENDING.value,
            ApprovalRequest.matrix_role_code == actor.role_code,
        )
        decided_route = ApprovalRequest.approver_id == actor.user_id
        stmt = select(ApprovalRequest).where(
            ApprovalRequest.dataset_id == user.dataset_id
        )
        if status:
            wanted = status.strip().upper()
            stmt = stmt.where(ApprovalRequest.status == wanted)
            if wanted == ApprovalStatus.PENDING.value:
                stmt = stmt.where(
                    ApprovalRequest.matrix_role_code == actor.role_code
                )
            else:
                stmt = stmt.where(decided_route)
        else:
            stmt = stmt.where(or_(pending_route, decided_route))
        if target_object_type is not None:
            stmt = stmt.where(ApprovalRequest.target_object_type == target_object_type)
        stmt = stmt.order_by(ApprovalRequest.created_at.asc()).limit(limit)
        return list(self.session.execute(stmt).scalars().all())
