"""所有模型聚合导出，供 Alembic autogenerate 识别。"""

from app.db.base import Base
from app.models.ai_interaction import AiInteractionLog
from app.models.approval import ApprovalRequest, TaskItem
from app.models.assistant_session import AssistantMessage, AssistantSession
from app.models.audit import AiLog, AuditLog, AuthEvent, ImportBatch
from app.models.contract import Contract, ContractVersion
from app.models.core import Brand, Organization, Region
from app.models.enums import (
    ActivityStatus,
    ApprovalStatus,
    AuditEventType,
    AuthEventType,
    ContractStatus,
    ExpenseStatus,
    ExpenseType,
    ExportFormat,
    ExportStatus,
    FreezerAssetStatus,
    FreezerMovementType,
    MerchantGrade,
    MerchantStatus,
    PayrollAdjustmentKind,
    PayrollStatus,
    PerformanceAdjustmentKind,
    PerformanceRuleStatus,
    PerformanceSnapshotStatus,
    ProcurementStatus,
    ReplenishmentStatus,
    ReportKey,
    RiskObjectType,
    RiskRuleCode,
    RiskStatus,
    RoleCode,
    ScheduleStatus,
    TaskStatus,
    TokenKind,
    UserStatus,
    VisitConclusion,
    VisitFreezerStatus,
    VisitRecordStatus,
    VisitSource,
    VisitTaskStatus,
)
from app.models.file_object import FileObject
from app.models.finance import FinanceTransaction
from app.models.freezer import FreezerAsset, FreezerMovement
from app.models.idempotency import IdempotencyKey
from app.models.inventory import BeverageStockEntry
from app.models.knowledge import KnowledgeChunk
from app.models.mcp_confirmation import McpConfirmationToken
from app.models.mcp_draft import McpDraft
from app.models.merchant import Merchant, MerchantContact, MerchantGradeFrequency
from app.models.merchant_assignment import (
    MerchantAssignment,
    MerchantGradeChange,
)
from app.models.merchant_transfer import MerchantTransfer
from app.models.notification import NotificationRead
from app.models.payroll import (
    Expense,
    PayrollAdjustment,
    PayrollLine,
    PayrollStatement,
    SalaryConfig,
)
from app.models.performance import (
    PayrollRecord,
    PerformanceMonthlySnapshot,
    PerformanceRecord,
    PerformanceRuleVersion,
    PerformanceSnapshotAdjustment,
    PerformanceSnapshotLine,
)
from app.models.procurement import ProcurementDelivery, ProcurementOrder
from app.models.product import ProductCategory, Sku
from app.models.purchase import (
    PurchaseOrder,
    PurchaseOrderLine,
    Supplier,
    SupplierAuthorization,
    SupplyConfirmation,
)
from app.models.replenishment import (
    ReplenishmentConfirm,
    ReplenishmentFulfillment,
    ReplenishmentOrder,
)
from app.models.reporting import ExportJob
from app.models.risk import RiskItem
from app.models.sales import SalesConfirmation
from app.models.schedule import (
    Activity,
    ActivityParticipant,
    ActivitySampleLedger,
    ResourceReservation,
    Schedule,
    SchedulePlanBatch,
    SchedulePlanEntry,
    ScheduleTemplate,
)
from app.models.seed import (
    DEMO_DATASET_ID_DEFAULT,
    SEED_STATUS_FAILED,
    SEED_STATUS_PENDING,
    SEED_STATUS_RUNNING,
    SEED_STATUS_SUCCEEDED,
    SEED_TYPE_BUSINESS,
    SEED_TYPE_EMPTY,
    SEED_TYPE_MASTER,
    SEED_TYPES,
    SeedBatch,
)
from app.models.supervision import (
    Rectification,
    SupervisionVisit,
    SupervisorIssue,
)
from app.models.user import Role, User, UserRegionBrandAuth
from app.models.visit import (
    VisitPhoto,
    VisitRecord,
    VisitRevision,
    VisitSalesLine,
    VisitTask,
)

__all__ = [
    "Base",
    # core
    "Organization",
    "Region",
    "Brand",
    "ProductCategory",
    "Sku",
    # user/auth
    "Role",
    "User",
    "UserRegionBrandAuth",
    # merchant
    "Merchant",
    "MerchantContact",
    "MerchantGradeFrequency",
    "MerchantAssignment",
    "MerchantGradeChange",
    "MerchantTransfer",
    # contract
    "Contract",
    "ContractVersion",
    # freezer
    "FreezerAsset",
    "FreezerMovement",
    # visit
    "VisitTask",
    "VisitRecord",
    "VisitPhoto",
    "VisitRevision",
    "VisitSalesLine",
    # replenishment
    "ReplenishmentOrder",
    "ReplenishmentConfirm",
    "ReplenishmentFulfillment",
    # sales
    "SalesConfirmation",
    # schedule/activity
    "Schedule",
    "SchedulePlanBatch",
    "SchedulePlanEntry",
    "ScheduleTemplate",
    "ResourceReservation",
    "Activity",
    "ActivityParticipant",
    "ActivitySampleLedger",
    # supervision
    "SupervisorIssue",
    "SupervisionVisit",
    "Rectification",
    # procurement
    "ProcurementOrder",
    "ProcurementDelivery",
    "Supplier",
    "SupplierAuthorization",
    "PurchaseOrder",
    "PurchaseOrderLine",
    "SupplyConfirmation",
    # finance
    "FinanceTransaction",
    # performance/payroll
    "PerformanceRecord",
    "PayrollRecord",
    "PerformanceRuleVersion",
    "PerformanceMonthlySnapshot",
    "PerformanceSnapshotLine",
    "PerformanceSnapshotAdjustment",
    # T20 expense/payroll
    "Expense",
    "PayrollStatement",
    "PayrollLine",
    "PayrollAdjustment",
    "SalaryConfig",
    # approval/task
    "ApprovalRequest",
    "TaskItem",
    # v1.2 risk
    "RiskItem",
    # T21 reporting/export
    "ExportJob",
    # audit/import
    "AuditLog",
    "AiLog",
    "AuthEvent",
    "ImportBatch",
    # T04 idempotency/files
    "IdempotencyKey",
    "FileObject",
    # T32 beverage stock ledger
    "BeverageStockEntry",
    # RAG knowledge chunks
    "KnowledgeChunk",
    # T24 mcp confirmation / drafts
    "McpConfirmationToken",
    "McpDraft",
    # T26 ai interaction audit
    "AiInteractionLog",
    # assistant sessions（AI 助手会话持久化）
    "AssistantSession",
    "AssistantMessage",
    # T27 notification read state
    "NotificationRead",
    # T06 demo seed
    "SeedBatch",
    "DEMO_DATASET_ID_DEFAULT",
    "SEED_STATUS_PENDING",
    "SEED_STATUS_RUNNING",
    "SEED_STATUS_SUCCEEDED",
    "SEED_STATUS_FAILED",
    "SEED_TYPE_MASTER",
    "SEED_TYPE_BUSINESS",
    "SEED_TYPE_EMPTY",
    "SEED_TYPES",
    # enums
    "ActivityStatus",
    "ApprovalStatus",
    "AuditEventType",
    "AuthEventType",
    "ContractStatus",
    "FreezerAssetStatus",
    "FreezerMovementType",
    "MerchantGrade",
    "MerchantStatus",
    "PayrollStatus",
    "PerformanceRuleStatus",
    "PerformanceSnapshotStatus",
    "PerformanceAdjustmentKind",
    "ExpenseType",
    "ExpenseStatus",
    "PayrollAdjustmentKind",
    "ExportFormat",
    "ExportStatus",
    "ReportKey",
    "ProcurementStatus",
    "ReplenishmentStatus",
    "RiskObjectType",
    "RiskRuleCode",
    "RiskStatus",
    "RoleCode",
    "ScheduleStatus",
    "TaskStatus",
    "TokenKind",
    "UserStatus",
    "VisitConclusion",
    "VisitFreezerStatus",
    "VisitRecordStatus",
    "VisitSource",
    "VisitTaskStatus",
]
