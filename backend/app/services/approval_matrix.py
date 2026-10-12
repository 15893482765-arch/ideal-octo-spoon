"""审批矩阵（T05 / PRD §5.3.1 写工具审批矩阵；R26）。

T05 范围：
- `approval.decide` 工具的不可自审/能力校验（Q021）；
- 退回/驳回原因校验（Q022 重叠防护）；
- 关键流程发起人 × 审批人能力 × 不可自审三段式校验。

未在 T05 范围（按 §5.3.1 全表扩展至 T23 MCP 卡）：
- 各业务工具的 initiator/confirmer/approver 全部条目；
- 完整的能力/角色矩阵在 T03 PermissionService 已实现，本模块复用其能力校验。
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from app.core.errors import DomainError, PermissionDeniedError
from app.core.permissions import ActorContext, permission_service
from app.models.enums import RoleCode


@dataclass(frozen=True)
class ApprovalRule:
    """单个审批动作的规则。

    - action：与 ApprovalRequest.action 一致（如 contract.create / freezer.recall）；
    - required_ability：审批人需具备的能力（PermissionService 已注册）；
    - self_approval_allowed：原业务发起人是否可作为审批人；按 §5.3.1 通常 False；
    - allow_returned：是否支持"退回补充材料"动作；T05 全部支持；
    - require_reason_for：哪些决策动作必须填原因（rejected/returned 强制）。
    """

    action: str
    required_ability: str
    self_approval_allowed: bool
    allow_returned: bool = True
    require_reason_for: frozenset[str] = frozenset({"REJECTED", "RETURNED"})


# T05 仅实现 approval.decide 工具的元数据；其他动作以代码注册，
# 不在表中穷举（避免本卡越界实现后续卡内容）
_DECISION_ACTIONS: dict[str, ApprovalRule] = {
    "approval.decide": ApprovalRule(
        action="approval.decide",
        required_ability="approval",
        self_approval_allowed=False,  # §5.3.1：不可自审
        allow_returned=True,
    ),
}


def get_rule(action: str) -> ApprovalRule | None:
    """按 action 查规则；找不到视为未在本卡登记 → 拒绝审批。"""
    return _DECISION_ACTIONS.get(action)


def assert_decision_authority(
    *,
    rule: ApprovalRule,
    actor: ActorContext,
    requester_id: UUID,
) -> None:
    """三段式校验：能力 + 不可自审 +（按需）原因。

    - 能力：审批人需具备 required_ability；
    - 不可自审：审批人 ≠ 原业务发起人；self_approval_allowed=True 时豁免；
    - 决策时若带 reason 且决策是 REJECTED/RETURNED，强制非空（与 §5.3.1 一致）。
    """
    if actor.user_id == requester_id and not rule.self_approval_allowed:
        raise PermissionDeniedError(
            code="SELF_APPROVAL_FORBIDDEN",
            message="不可审批本人发起的申请",
            details={"action": rule.action},
        )
    try:
        permission_service.require_ability(actor, rule.required_ability)
    except Exception as exc:  # PermissionError_
        raise PermissionDeniedError(
            code="PERMISSION_DENIED",
            message="当前角色无权处理该审批",
            details={"action": rule.action, "required": rule.required_ability},
        ) from exc


def assert_decision_reason(
    *, decision: str, reason: str | None, rule: ApprovalRule
) -> None:
    """驳回/退回必须填原因（其余决策原因可选）。"""
    if decision in rule.require_reason_for and not (reason and reason.strip()):
        raise DomainError(
            code="APPROVAL_REASON_REQUIRED",
            message="驳回或退回必须填写原因",
            details={"decision": decision, "action": rule.action},
        )


def role_competent_for(actor: ActorContext, action: str) -> bool:
    """便捷判定：actor 是否可处理指定审批动作。"""
    rule = get_rule(action)
    if rule is None:
        return False
    return rule.required_ability in actor.profile.abilities


# 角色白名单（仅供 UI/工具列参与人提示；不参与强制）
APPROVER_ROLES: frozenset[str] = frozenset(
    {
        RoleCode.REGIONAL_MANAGER.value,
        RoleCode.SUPERVISOR.value,
        RoleCode.FINANCE.value,
        RoleCode.SYSTEM_ADMIN.value,
    }
)
