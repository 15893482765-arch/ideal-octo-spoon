"""业务状态机枚举：仅作 Python 端类型提示与文档。

数据库侧用 String + CheckConstraint 显式枚举值；不依赖 PG ENUM 类型，
便于后续跨库迁移与无破坏的状态值增删。
"""

from __future__ import annotations

from enum import StrEnum


class MerchantStatus(StrEnum):
    LEAD = "LEAD"
    NEGOTIATING = "NEGOTIATING"
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    TERMINATED = "TERMINATED"


class MerchantGrade(StrEnum):
    A = "A"
    B = "B"
    C = "C"


class ContractStatus(StrEnum):
    DRAFT = "DRAFT"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    ACTIVE = "ACTIVE"
    EXPIRED = "EXPIRED"
    TERMINATED = "TERMINATED"


class FreezerAssetStatus(StrEnum):
    IN_STOCK = "IN_STOCK"
    IN_TRANSIT = "IN_TRANSIT"
    DEPLOYED = "DEPLOYED"
    UNDER_MAINTENANCE = "UNDER_MAINTENANCE"
    SCRAPPED = "SCRAPPED"


class FreezerMovementType(StrEnum):
    PURCHASE = "PURCHASE"
    DEPLOY = "DEPLOY"
    RECALL = "RECALL"
    REPAIR_START = "REPAIR_START"
    REPAIR_END = "REPAIR_END"
    SCRAP = "SCRAP"
    DEPOSIT_COLLECTED = "DEPOSIT_COLLECTED"  # v1.2 押金收缴
    DEPOSIT_REFUNDED = "DEPOSIT_REFUNDED"  # v1.2 押金退还


class VisitTaskStatus(StrEnum):
    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    OVERDUE = "OVERDUE"
    ABNORMAL = "ABNORMAL"
    INVALIDATED = "INVALIDATED"


class VisitRecordStatus(StrEnum):
    DRAFT = "DRAFT"
    SUBMITTED = "SUBMITTED"
    RECOGNIZED = "RECOGNIZED"  # 经 VisitValidityService 判定为有效
    ABNORMAL = "ABNORMAL"
    REJECTED = "REJECTED"


class ReplenishmentStatus(StrEnum):
    REQUESTED = "REQUESTED"
    CONFIRMED = "CONFIRMED"
    IN_TRANSIT = "IN_TRANSIT"
    DELIVERED = "DELIVERED"
    OVERDUE = "OVERDUE"
    CANCELLED = "CANCELLED"


class ScheduleStatus(StrEnum):
    DRAFT = "DRAFT"
    PUBLISHED = "PUBLISHED"
    CANCELLED = "CANCELLED"
    COMPLETED = "COMPLETED"


class ActivityStatus(StrEnum):
    DRAFT = "DRAFT"
    PUBLISHED = "PUBLISHED"
    IN_PROGRESS = "IN_PROGRESS"
    REVIEWED = "REVIEWED"
    CLOSED = "CLOSED"
    CANCELLED = "CANCELLED"


class ProcurementStatus(StrEnum):
    REQUESTED = "REQUESTED"
    APPROVED = "APPROVED"
    ORDERED = "ORDERED"
    RECEIVED = "RECEIVED"
    CANCELLED = "CANCELLED"


class ApprovalStatus(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    WITHDRAWN = "WITHDRAWN"


class PayrollStatus(StrEnum):
    DRAFT = "DRAFT"
    REVIEWED = "REVIEWED"
    APPROVED = "APPROVED"
    PAID = "PAID"


class TaskStatus(StrEnum):
    OPEN = "OPEN"
    IN_PROGRESS = "IN_PROGRESS"
    CLOSED = "CLOSED"


class RiskStatus(StrEnum):
    OPEN = "OPEN"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    CLOSED = "CLOSED"


class RiskRuleCode(StrEnum):
    RK1_NO_VISIT = "RK-1"
    RK2_ABNORMAL_LOCATION = "RK-2"
    RK3_DISPLAY_BELOW = "RK-3"
    RK4_ZERO_SALES = "RK-4"
    RK5_REPLENISHMENT_OVERDUE = "RK-5"
    RK6_CONTRACT_EXPIRING = "RK-6"
    RK7_FREEZER_REPAIR_OVERDUE = "RK-7"


class RiskObjectType(StrEnum):
    MERCHANT = "MERCHANT"
    CONTRACT = "CONTRACT"
    FREEZER = "FREEZER"
    REPLENISHMENT = "REPLENISHMENT"


class AuditEventType(StrEnum):
    CREATE = "CREATE"
    UPDATE = "UPDATE"
    STATE_TRANSITION = "STATE_TRANSITION"
    DELETE_DENIED = "DELETE_DENIED"
