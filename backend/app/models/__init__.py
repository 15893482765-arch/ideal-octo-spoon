"""所有模型聚合导出，供 Alembic autogenerate 识别。"""

from app.db.base import Base
from app.models.approval import ApprovalRequest, TaskItem
from app.models.audit import AiLog, AuditLog, ImportBatch
from app.models.contract import Contract, ContractApproval, ContractChange
from app.models.core import Brand, Organization, Region
from app.models.enums import (
    ActivityStatus,
    ApprovalStatus,
    AuditEventType,
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
    ScheduleStatus,
    TaskStatus,
    VisitRecordStatus,
    VisitTaskStatus,
)
from app.models.finance import FinanceTransaction
from app.models.freezer import FreezerAsset, FreezerMovement
from app.models.merchant import Merchant, MerchantContact, MerchantGradeFrequency
from app.models.performance import PayrollRecord, PerformanceRecord
from app.models.procurement import ProcurementDelivery, ProcurementOrder
from app.models.product import ProductCategory, Sku
from app.models.replenishment import ReplenishmentConfirm, ReplenishmentOrder
from app.models.risk import IdempotencyKey, RiskItem
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
    # audit/import
    "AuditLog",
    "AiLog",
    "ImportBatch",
    # v1.2 risk/idempotency
    "RiskItem",
    "IdempotencyKey",
    # enums
    "ActivityStatus",
    "ApprovalStatus",
    "AuditEventType",
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
    "ScheduleStatus",
    "TaskStatus",
    "VisitRecordStatus",
    "VisitTaskStatus",
]
