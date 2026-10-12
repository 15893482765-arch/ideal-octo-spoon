"""所有模型聚合导出，供 Alembic autogenerate 识别。"""

from app.db.base import Base
from app.models.approval import ApprovalRequest, TaskItem
from app.models.audit import AiLog, AuditLog, AuthEvent, ImportBatch
from app.models.contract import Contract, ContractApproval, ContractChange
from app.models.core import Brand, Organization, Region
from app.models.enums import (
    ActivityStatus,
    ApprovalStatus,
    AuditEventType,
    AuthEventType,
    ContractStatus,
    FreezerAssetStatus,
    FreezerMovementType,
    MerchantGrade,
    MerchantStatus,
    PayrollStatus,
    ProcurementStatus,
    ReplenishmentStatus,
    RiskObjectType,
    RiskRuleCode,
    RiskStatus,
    RoleCode,
    ScheduleStatus,
    TaskStatus,
    TokenKind,
    UserStatus,
    VisitRecordStatus,
    VisitTaskStatus,
)
from app.models.file_object import FileObject
from app.models.finance import FinanceTransaction
from app.models.freezer import FreezerAsset, FreezerMovement
from app.models.idempotency import IdempotencyKey
from app.models.merchant import Merchant, MerchantContact, MerchantGradeFrequency
from app.models.performance import PayrollRecord, PerformanceRecord
from app.models.procurement import ProcurementDelivery, ProcurementOrder
from app.models.product import ProductCategory, Sku
from app.models.replenishment import ReplenishmentConfirm, ReplenishmentOrder
from app.models.risk import RiskItem
from app.models.sales import SalesConfirmation
from app.models.schedule import Activity, ActivitySampleLedger, Schedule
from app.models.supervision import SupervisorIssue
from app.models.user import Role, User, UserRegionBrandAuth
from app.models.visit import VisitRecord, VisitTask

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
    # contract
    "Contract",
    "ContractChange",
    "ContractApproval",
    # freezer
    "FreezerAsset",
    "FreezerMovement",
    # visit
    "VisitTask",
    "VisitRecord",
    # replenishment
    "ReplenishmentOrder",
    "ReplenishmentConfirm",
    # sales
    "SalesConfirmation",
    # schedule/activity
    "Schedule",
    "Activity",
    "ActivitySampleLedger",
    # supervision
    "SupervisorIssue",
    # procurement
    "ProcurementOrder",
    "ProcurementDelivery",
    # finance
    "FinanceTransaction",
    # performance/payroll
    "PerformanceRecord",
    "PayrollRecord",
    # approval/task
    "ApprovalRequest",
    "TaskItem",
    # v1.2 risk
    "RiskItem",
    # audit/import
    "AuditLog",
    "AiLog",
    "AuthEvent",
    "ImportBatch",
    # T04 idempotency/files
    "IdempotencyKey",
    "FileObject",
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
    "VisitRecordStatus",
    "VisitTaskStatus",
]
