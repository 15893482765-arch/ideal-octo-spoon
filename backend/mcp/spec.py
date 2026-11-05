"""工具声明与写工具审批矩阵数据结构（T22）。

`ToolSpec` 是**纯声明**：输入/输出 Schema、允许角色、数据范围、字段权限、
风险等级与确认策略；执行器由适配器通过 `ToolRegistry.bind_handler` 绑定
（T23/T24），本骨架不实现业务逻辑。

`WriteToolMatrix` 对应 PRD §5.3.1 的 7 列：
`initiator / confirmer / approver / executor / self_approval_allowed /
feature_flag / final_state`；字段缺失即拒绝注册（§5.4.8）。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from mcp.errors import McpMatrixIncompleteError, McpSelfApprovalForbiddenError
from mcp.feature_flags import is_known_flag
from mcp.levels import ConfirmLevel, ToolKind

# 确认人取值（PRD §5.3.1「结构化确认人」列）
CONFIRMER_CURRENT_ACTOR = "CURRENT_ACTOR"
CONFIRMER_CURRENT_SUPPLIER_ACCOUNT = "CURRENT_SUPPLIER_ACCOUNT"
CONFIRMER_CURRENT_APPROVER = "CURRENT_APPROVER"
ALLOWED_CONFIRMERS: frozenset[str] = frozenset(
    {CONFIRMER_CURRENT_ACTOR, CONFIRMER_CURRENT_SUPPLIER_ACCOUNT, CONFIRMER_CURRENT_APPROVER}
)

# 自审标志取值
# - True  ：允许自确认/自审（无独立审批人，自确认不构成业务审批）
# - False ：禁止自审（存在 approver 时必须换人）
SELF_APPROVAL_ALLOWED = True
SELF_APPROVAL_FORBIDDEN = False


@dataclass(frozen=True)
class FieldPolicy:
    """字段级权限声明（空 `visible_fields` 表示不限白名单，再按 `hidden_fields` 剔除）。"""

    visible_fields: frozenset[str] = field(default_factory=frozenset)
    hidden_fields: frozenset[str] = field(default_factory=frozenset)

    @property
    def is_open(self) -> bool:
        return not self.visible_fields and not self.hidden_fields


@dataclass(frozen=True)
class ConfirmationPolicy:
    """确认策略（由风险等级与审批矩阵推导）。"""

    structured_confirmation: bool  # A2+ 必须结构化确认 UI；自由文本“确认”无效
    requires_approver: bool  # 存在独立审批人（AI 不可跳过）
    dual_review: bool  # A4 双人复核/指定审批


@dataclass(frozen=True)
class WriteToolMatrix:
    """写工具审批矩阵（PRD §5.3.1，7 列）。

    - `initiator`：允许发起本工具的角色集合（非空）；
    - `confirmer`：结构化确认人（`ALLOWED_CONFIRMERS` 之一）；
    - `approver`：后续审批人角色集合；**必须是显式集合**（空集表示「无，不审批」），
      `None` 表示未声明 → 拒绝注册；
    - `executor`：执行业务的 Service 名称（PRD 明文）；
    - `self_approval_allowed`：是否允许自审（存在 approver 时禁止为 True）；
    - `feature_flag`：功能开关名（须在 `feature_flags` 注册）；
    - `final_state`：成功后状态（非空）。
    """

    initiator: frozenset[str]
    confirmer: str
    approver: frozenset[str] | None
    executor: str
    self_approval_allowed: bool
    feature_flag: str
    final_state: tuple[str, ...]

    def validate(self, tool_name: str) -> None:
        """完整性校验：任一字段缺失/非法即拒绝注册（§5.4.8）。"""
        missing: list[str] = []
        if not self.initiator:
            missing.append("initiator")
        if not self.confirmer or self.confirmer not in ALLOWED_CONFIRMERS:
            missing.append("confirmer")
        if self.approver is None:
            missing.append("approver")
        if not self.executor or not self.executor.strip():
            missing.append("executor")
        if not isinstance(self.self_approval_allowed, bool):
            missing.append("self_approval_allowed")
        if not self.feature_flag or not is_known_flag(self.feature_flag):
            missing.append("feature_flag")
        if not self.final_state or any(not str(s).strip() for s in self.final_state):
            missing.append("final_state")
        if missing:
            raise McpMatrixIncompleteError(tool_name, missing)
        # 关键流程默认禁止自审：存在 approver 却声明允许自审 → 拒绝
        if self.approver and self.self_approval_allowed:
            raise McpSelfApprovalForbiddenError(tool_name)

    @property
    def approver_roles(self) -> frozenset[str]:
        return self.approver or frozenset()


@dataclass(frozen=True)
class ToolSpec:
    """单个 MCP 工具的声明式元数据。"""

    name: str
    title: str
    description: str
    kind: ToolKind
    risk_level: ConfirmLevel
    allowed_roles: frozenset[str]
    input_schema: Mapping[str, Any]
    output_schema: Mapping[str, Any]
    field_policy: FieldPolicy = field(default_factory=FieldPolicy)
    write_matrix: WriteToolMatrix | None = None
    requires_data_scope: bool = True
    capabilities: frozenset[str] = field(default_factory=frozenset)

    @property
    def is_write(self) -> bool:
        return self.kind is ToolKind.WRITE

    @property
    def requires_idempotency_key(self) -> bool:
        """写工具（PRD §5.3.1 矩阵内的工具）必须携带幂等键（§5.4.2）。"""
        return self.write_matrix is not None

    @property
    def confirmation_policy(self) -> ConfirmationPolicy:
        return ConfirmationPolicy(
            structured_confirmation=self.risk_level.is_write,
            requires_approver=bool(self.write_matrix and self.write_matrix.approver),
            dual_review=self.risk_level.dual_review,
        )

    @property
    def feature_flag(self) -> str | None:
        return self.write_matrix.feature_flag if self.write_matrix else None

    def to_public_dict(self) -> dict[str, Any]:
        """对外（模型/客户端）可见的工具描述：不含敏感实现细节。"""
        payload: dict[str, Any] = {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "kind": self.kind.value,
            "risk_level": self.risk_level.value,
            "allowed_roles": sorted(self.allowed_roles),
            "requires_idempotency_key": self.requires_idempotency_key,
            "requires_data_scope": self.requires_data_scope,
            "input_schema": dict(self.input_schema),
            "output_schema": dict(self.output_schema),
            "confirmation_policy": {
                "structured_confirmation": self.confirmation_policy.structured_confirmation,
                "requires_approver": self.confirmation_policy.requires_approver,
                "dual_review": self.confirmation_policy.dual_review,
            },
        }
        if self.write_matrix is not None:
            payload["write_matrix"] = {
                "initiator": sorted(self.write_matrix.initiator),
                "confirmer": self.write_matrix.confirmer,
                "approver": sorted(self.write_matrix.approver_roles),
                "executor": self.write_matrix.executor,
                "self_approval_allowed": self.write_matrix.self_approval_allowed,
                "feature_flag": self.write_matrix.feature_flag,
                "final_state": list(self.write_matrix.final_state),
            }
        if self.field_policy.visible_fields or self.field_policy.hidden_fields:
            payload["field_policy"] = {
                "visible_fields": sorted(self.field_policy.visible_fields),
                "hidden_fields": sorted(self.field_policy.hidden_fields),
            }
        return payload
