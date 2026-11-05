"""写工具审批矩阵常量（T22 / PRD §5.3.1，实现基线）。

PRD §5.3.1 的 11 行逐工具矩阵在此固化为代码常量：

| 工具 | initiator | confirmer | approver | executor | 可自确认/自审 | 功能开关 | 成功后状态 |

规则（PRD §5.3.1 末段）：
- 「可自确认」仅指用户确认本次工具参数，**不等于业务审批**；
- 只要存在 approver，该 approver 必须满足「非原业务申请人」规则；
- 实现不得用更宽松的默认值覆盖本表 —— 故模块导入时即校验完整性，
  字段缺失或「存在 approver 却允许自审」直接拒绝启动。
"""

from __future__ import annotations

from app.models.enums import RoleCode
from mcp.levels import ConfirmLevel, ToolKind
from mcp.spec import (
    CONFIRMER_CURRENT_ACTOR,
    CONFIRMER_CURRENT_APPROVER,
    CONFIRMER_CURRENT_SUPPLIER_ACCOUNT,
    SELF_APPROVAL_ALLOWED,
    SELF_APPROVAL_FORBIDDEN,
    WriteToolMatrix,
)

R_SALES = RoleCode.SALES_REP.value
R_MANAGER = RoleCode.REGIONAL_MANAGER.value
R_SUPERVISOR = RoleCode.SUPERVISOR.value
R_FINANCE = RoleCode.FINANCE.value
R_ADMIN = RoleCode.SYSTEM_ADMIN.value
R_SUPPLIER = RoleCode.SUPPLIER.value

# 审批节点角色（与 app.services.approval_matrix.APPROVER_ROLES 对齐）
APPROVER_NODE_ROLES: frozenset[str] = frozenset({R_MANAGER, R_SUPERVISOR, R_FINANCE, R_ADMIN})

_NO_APPROVER: frozenset[str] = frozenset()


def _matrix(
    *,
    initiator: frozenset[str],
    confirmer: str,
    approver: frozenset[str] | None = None,
    executor: str,
    self_approval_allowed: bool,
    feature_flag: str,
    final_state: tuple[str, ...],
) -> WriteToolMatrix:
    return WriteToolMatrix(
        initiator=initiator,
        confirmer=confirmer,
        approver=_NO_APPROVER if approver is None else approver,
        executor=executor,
        self_approval_allowed=self_approval_allowed,
        feature_flag=feature_flag,
        final_state=final_state,
    )


# ===== PRD §5.3.1：11 行逐工具审批矩阵 =====

WRITE_APPROVAL_MATRIX: dict[str, WriteToolMatrix] = {
    # 行 1：草稿不审批（final_state=DRAFT）
    "merchant.create_draft": _matrix(
        initiator=frozenset({R_SALES, R_MANAGER}),
        confirmer=CONFIRMER_CURRENT_ACTOR,
        # A1 草稿只落独立暂存表（McpDraftService），不写 merchants 正式表、不进指标
        executor="McpDraftService",
        self_approval_allowed=SELF_APPROVAL_ALLOWED,
        feature_flag="MERCHANT_ENABLED",
        final_state=("DRAFT",),
    ),
    "merchant.update_draft": _matrix(
        initiator=frozenset({R_SALES, R_MANAGER}),
        confirmer=CONFIRMER_CURRENT_ACTOR,
        executor="McpDraftService",
        self_approval_allowed=SELF_APPROVAL_ALLOWED,
        feature_flag="MERCHANT_ENABLED",
        final_state=("DRAFT",),
    ),
    # 行 2：可自确认提交；不可自判异常（异常判定走经理，非本工具）
    "visit.submit": _matrix(
        initiator=frozenset({R_SALES, R_SUPERVISOR}),
        confirmer=CONFIRMER_CURRENT_ACTOR,
        executor="VisitService",
        self_approval_allowed=SELF_APPROVAL_ALLOWED,
        feature_flag="VISIT_ENABLED",
        final_state=("SUBMITTED", "ABNORMAL_PENDING"),
    ),
    # 行 3：可自确认提交；不可自批（审批人为非发起人的片区经理）
    "replenishment.submit": _matrix(
        initiator=frozenset({R_SALES, R_MANAGER}),
        confirmer=CONFIRMER_CURRENT_ACTOR,
        approver=frozenset({R_MANAGER}),
        executor="ReplenishmentService",
        self_approval_allowed=SELF_APPROVAL_FORBIDDEN,
        feature_flag="REPLENISHMENT_ENABLED",
        final_state=("SUBMITTED",),
    ),
    # 行 4：不可自审（审批人为非申请人的片区经理）
    "contract.create_application": _matrix(
        initiator=frozenset({R_SALES, R_MANAGER}),
        confirmer=CONFIRMER_CURRENT_ACTOR,
        approver=frozenset({R_MANAGER}),
        executor="ContractService",
        self_approval_allowed=SELF_APPROVAL_FORBIDDEN,
        feature_flag="CONTRACT_ENABLED",
        final_state=("PENDING_APPROVAL",),
    ),
    # 行 5：无独立审批；冲突服务是硬门禁
    "schedule.publish": _matrix(
        initiator=frozenset({R_MANAGER}),
        confirmer=CONFIRMER_CURRENT_ACTOR,
        executor="ScheduleService",
        self_approval_allowed=SELF_APPROVAL_ALLOWED,
        feature_flag="SCHEDULE_ENABLED",
        final_state=("PUBLISHED",),
    ),
    # 行 6：不可自审（非发起人的经理或督导/运营管理员）
    "merchant.transfer.create_request": _matrix(
        initiator=frozenset({R_MANAGER}),
        confirmer=CONFIRMER_CURRENT_ACTOR,
        approver=frozenset({R_MANAGER, R_SUPERVISOR}),
        executor="TransferService",
        self_approval_allowed=SELF_APPROVAL_FORBIDDEN,
        feature_flag="MERCHANT_TRANSFER_ENABLED",
        final_state=("PENDING_APPROVAL",),
    ),
    # 行 7：不可自审（非发起人的督导/运营管理员；超预算另需财务）
    "campaign.publish": _matrix(
        initiator=frozenset({R_MANAGER}),
        confirmer=CONFIRMER_CURRENT_ACTOR,
        approver=frozenset({R_SUPERVISOR, R_FINANCE}),
        executor="ActivityService",
        self_approval_allowed=SELF_APPROVAL_FORBIDDEN,
        feature_flag="CAMPAIGN_ENABLED",
        final_state=("PENDING_APPROVAL", "PUBLISHED"),
    ),
    # 行 8：可自确认现场提交；不可自验整改
    "supervision.submit": _matrix(
        initiator=frozenset({R_SUPERVISOR, R_MANAGER}),
        confirmer=CONFIRMER_CURRENT_ACTOR,
        executor="SupervisionService",
        self_approval_allowed=SELF_APPROVAL_ALLOWED,
        feature_flag="SUPERVISION_ENABLED",
        final_state=("SUBMITTED",),
    ),
    # 行 9：不可自审内部接收（内部片区经理或财务确认接收）
    "supplier.confirm_supply": _matrix(
        initiator=frozenset({R_SUPPLIER}),
        confirmer=CONFIRMER_CURRENT_SUPPLIER_ACCOUNT,
        approver=frozenset({R_MANAGER, R_FINANCE}),
        executor="ProcurementService",
        self_approval_allowed=SELF_APPROVAL_FORBIDDEN,
        feature_flag="SUPPLIER_PORTAL_ENABLED",
        final_state=("SUPPLIER_CONFIRMED",),
    ),
    # 行 10：普通可自确认；敏感导出不可自审（敏感分支由 ExportService 运行时判定）
    "report.export_request": _matrix(
        initiator=frozenset({R_MANAGER, R_FINANCE, R_ADMIN}),
        confirmer=CONFIRMER_CURRENT_ACTOR,
        executor="ExportService",
        self_approval_allowed=SELF_APPROVAL_ALLOWED,
        feature_flag="EXPORT_ENABLED",
        final_state=("QUEUED",),
    ),
    # 行 11：不可审批本人申请
    "approval.decide": _matrix(
        initiator=APPROVER_NODE_ROLES,
        confirmer=CONFIRMER_CURRENT_APPROVER,
        approver=APPROVER_NODE_ROLES,
        executor="ApprovalService",
        self_approval_allowed=SELF_APPROVAL_FORBIDDEN,
        feature_flag="APPROVAL_ENABLED",
        final_state=("APPROVED", "REJECTED", "RETURNED"),
    ),
}

# 矩阵中工具的风险等级（A2 普通单据 / A3 关键流程 / A4 高风险；A1 草稿）
MATRIX_TOOL_RISK_LEVELS: dict[str, ConfirmLevel] = {
    "merchant.create_draft": ConfirmLevel.A1,
    "merchant.update_draft": ConfirmLevel.A1,
    "visit.submit": ConfirmLevel.A2,
    "replenishment.submit": ConfirmLevel.A2,
    "contract.create_application": ConfirmLevel.A3,
    "schedule.publish": ConfirmLevel.A3,
    "merchant.transfer.create_request": ConfirmLevel.A3,
    "campaign.publish": ConfirmLevel.A3,
    "supervision.submit": ConfirmLevel.A2,
    "supplier.confirm_supply": ConfirmLevel.A3,
    "report.export_request": ConfirmLevel.A3,
    "approval.decide": ConfirmLevel.A3,
}


def matrix_tool_kind(tool_name: str) -> ToolKind:
    """草稿类矩阵工具为 DRAFT，其余为 WRITE。"""
    return (
        ToolKind.DRAFT
        if MATRIX_TOOL_RISK_LEVELS[tool_name] is ConfirmLevel.A1
        else ToolKind.WRITE
    )


def _validate_all() -> None:
    """导入期即校验：任一矩阵行字段缺失/非法 → 拒绝启动。"""
    for tool_name, matrix in WRITE_APPROVAL_MATRIX.items():
        matrix.validate(tool_name)
        if tool_name not in MATRIX_TOOL_RISK_LEVELS:
            raise KeyError(f"矩阵工具缺少风险等级声明：{tool_name}")
    for tool_name in MATRIX_TOOL_RISK_LEVELS:
        if tool_name not in WRITE_APPROVAL_MATRIX:
            raise KeyError(f"风险等级声明了未在矩阵登记的工具：{tool_name}")


_validate_all()

# 矩阵覆盖的工具名（12 个工具名 / 11 行）
MATRIX_TOOL_NAMES: frozenset[str] = frozenset(WRITE_APPROVAL_MATRIX)
