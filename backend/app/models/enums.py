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
    # T09 申请-审批-版本生命周期（PRD §4.4 / §4.6）
    DRAFT = "DRAFT"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    RETURNED = "RETURNED"
    REJECTED = "REJECTED"
    APPROVED = "APPROVED"
    EFFECTIVE = "EFFECTIVE"
    TERMINATION_PENDING = "TERMINATION_PENDING"
    TERMINATED = "TERMINATED"
    # 兼容旧台账口径（T03 遗留；首版合同模块未使用）
    ACTIVE = "ACTIVE"
    EXPIRED = "EXPIRED"


class FreezerAssetStatus(StrEnum):
    # T10（PRD §4.5 v1.2）冰柜状态机
    IN_STOCK = "IN_STOCK"                      # 在库
    RESERVED = "RESERVED"                      # 已预留（投放单已建、出库前）
    DEPLOYED = "DEPLOYED"                      # 已投放
    PENDING_PICKUP = "PENDING_PICKUP"          # 待撤回（撤柜任务已下发现场未回收）
    PENDING_ACCEPTANCE = "PENDING_ACCEPTANCE"  # 现场已回收，待仓库验收
    REPAIRING = "REPAIRING"                    # 维修中
    SCRAPPED = "SCRAPPED"                      # 报废（不可逆）
    # 兼容 T02 旧台账口径（首版新流程不再写入；夹具导入经映射转换）
    IN_TRANSIT = "IN_TRANSIT"
    UNDER_MAINTENANCE = "UNDER_MAINTENANCE"


class FreezerMovementType(StrEnum):
    PURCHASE = "PURCHASE"                      # 购置入库
    DEPLOYMENT_ORDER = "DEPLOYMENT_ORDER"      # 创建投放单（预留）
    DEPLOY = "DEPLOY"                          # 现场投放完成（投放单）
    RECALL = "RECALL"                          # 兼容旧词：现场回收
    PICKUP = "PICKUP"                          # 现场回收（→ 待验收）
    ACCEPT = "ACCEPT"                          # 仓库验收（在库/维修/报废，验收单）
    REPAIR_START = "REPAIR_START"
    REPAIR_END = "REPAIR_END"
    SCRAP = "SCRAP"
    DEPOSIT_COLLECTED = "DEPOSIT_COLLECTED"    # v1.2 押金收缴（关联投放单）
    DEPOSIT_REFUNDED = "DEPOSIT_REFUNDED"      # v1.2 押金退还（关联验收单）


class VisitTaskStatus(StrEnum):
    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    OVERDUE = "OVERDUE"
    ABNORMAL = "ABNORMAL"
    INVALIDATED = "INVALIDATED"


class VisitSource(StrEnum):
    """巡访任务来源（PRD §4.6：周排班/临时/整改/督导）。"""

    WEEKLY_SCHEDULE = "WEEKLY_SCHEDULE"  # 周排班
    AD_HOC = "AD_HOC"                    # 临时
    RECTIFICATION = "RECTIFICATION"      # 整改
    SUPERVISION = "SUPERVISION"          # 督导跟访（不计业务员覆盖率）


class VisitRecordStatus(StrEnum):
    DRAFT = "DRAFT"
    SUBMITTED = "SUBMITTED"
    RECOGNIZED = "RECOGNIZED"            # 经 VisitValidityService 判定为有效
    ABNORMAL_PENDING = "ABNORMAL_PENDING"  # 定位超阈值/缺照片，待经理判定（§4.6）
    ABNORMAL = "ABNORMAL"                # 经理判定：不计有效
    REJECTED = "REJECTED"


class VisitFreezerStatus(StrEnum):
    """巡访现场观察到的冰柜状态（PRD §4.6 提交表单"冰柜状态/设备异常"）。"""

    NORMAL = "NORMAL"              # 正常运转
    ABSENT = "ABSENT"              # 现场未见冰柜
    POWERED_OFF = "POWERED_OFF"    # 断电/未插电
    DIRTY = "DIRTY"                # 脏乱未清理
    DAMAGED = "DAMAGED"            # 破损/故障
    OTHER = "OTHER"


class VisitConclusion(StrEnum):
    """巡访当日结论（PRD §4.6 提交表单"当日结论"）。"""

    NORMAL = "NORMAL"                        # 正常
    NEEDS_REPLENISHMENT = "NEEDS_REPLENISHMENT"  # 需补货
    NEEDS_RECTIFICATION = "NEEDS_RECTIFICATION"  # 需整改
    RISK_MERCHANT = "RISK_MERCHANT"          # 风险商家
    PROPOSE_TERMINATION = "PROPOSE_TERMINATION"  # 拟取消合作


class ReplenishmentStatus(StrEnum):
    # T17 起以 PRD §4.9 为准：
    # DRAFT → SUBMITTED → CONFIRMED → PARTIALLY_FULFILLED / FULFILLED → CLOSED / CANCELLED
    DRAFT = "DRAFT"                  # 巡访提报生成的草稿
    SUBMITTED = "SUBMITTED"          # 业务员提交待经理确认
    CONFIRMED = "CONFIRMED"          # 经理已确认（可调整确认数量）
    PARTIALLY_FULFILLED = "PARTIALLY_FULFILLED"  # 部分满足，未满足量可继续履约
    FULFILLED = "FULFILLED"          # 已全部满足（已满足=已确认）
    CLOSED = "CLOSED"                # 履约闭环
    CANCELLED = "CANCELLED"
    # ---- 以下为 T12 之前的历史台账词，保留供旧数据/夹具兼容，新流程不再写入 ----
    REQUESTED = "REQUESTED"
    IN_TRANSIT = "IN_TRANSIT"
    DELIVERED = "DELIVERED"
    OVERDUE = "OVERDUE"


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


class SupervisionVisitStatus(StrEnum):
    """督导跟访状态（PRD §4.8）：周计划 → 现场提交。"""

    PLANNED = "PLANNED"    # 每周跟访计划
    SUBMITTED = "SUBMITTED"  # 现场记录、评分、问题已提交


class IssueCategory(StrEnum):
    """跟访发现问题分类：陈列 / 服务 / 设备（PRD §4.8）。"""

    DISPLAY = "DISPLAY"    # 陈列
    SERVICE = "SERVICE"    # 服务
    EQUIPMENT = "EQUIPMENT"  # 设备
    OTHER = "OTHER"


class RectificationStatus(StrEnum):
    """整改单五态状态机（PRD §4.8）。

    OPEN → IN_PROGRESS → SUBMITTED → VERIFIED → CLOSED
    验证人（VERIFIED/CLOSED）不得为整改提交人（R：职责分离）。
    """

    OPEN = "OPEN"
    IN_PROGRESS = "IN_PROGRESS"
    SUBMITTED = "SUBMITTED"
    VERIFIED = "VERIFIED"
    CLOSED = "CLOSED"


class ProcurementStatus(StrEnum):
    REQUESTED = "REQUESTED"
    APPROVED = "APPROVED"
    ORDERED = "ORDERED"
    RECEIVED = "RECEIVED"
    CANCELLED = "CANCELLED"


class PurchaseOrderStatus(StrEnum):
    """采购单状态（T18，PRD §4.9 / §5.1 行 supplier.confirm_supply）。

    DRAFT 内部起草 → CONFIRMED 内部已确认（金额服务端重算落账）→
    SUPPLIER_CONFIRMED 厂商确认供货（量/日期/凭证）→ RECEIVED 内部接收；
    SUPPLY_REJECTED 为厂商驳回终态，CANCELLED 为内部取消终态。
    厂商确认不等于付款或入库（Q100）。
    """

    DRAFT = "DRAFT"
    CONFIRMED = "CONFIRMED"
    SUPPLIER_CONFIRMED = "SUPPLIER_CONFIRMED"
    SUPPLY_REJECTED = "SUPPLY_REJECTED"
    RECEIVED = "RECEIVED"
    CANCELLED = "CANCELLED"


class SupplyConfirmationStatus(StrEnum):
    """单条供货确认（PRD §5.1：仅确认量与日期，可驳回）。重复确认幂等。"""

    CONFIRMED = "CONFIRMED"
    REJECTED = "REJECTED"


class ApprovalStatus(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    RETURNED = "RETURNED"  # 退回申请人补充材料（区别于驳回）
    WITHDRAWN = "WITHDRAWN"


class PayrollStatus(StrEnum):
    DRAFT = "DRAFT"
    REVIEWED = "REVIEWED"
    APPROVED = "APPROVED"
    PAID = "PAID"


class ExpenseType(StrEnum):
    """T20 费用类型（PRD §4.10：采购/冰柜购置/冰柜维修/活动/其他）。"""

    PROCUREMENT = "PROCUREMENT"              # 采购费用
    FREEZER_PURCHASE = "FREEZER_PURCHASE"    # 冰柜购置
    FREEZER_REPAIR = "FREEZER_REPAIR"        # 冰柜维修
    ACTIVITY = "ACTIVITY"                    # 活动费用
    OTHER = "OTHER"                          # 其他费用


class ExpenseStatus(StrEnum):
    """T20 费用审批状态：未审批（PENDING/REJECTED）不进损益。"""

    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class PayrollAdjustmentKind(StrEnum):
    """T20 工资审批调整类型（BONUS 非负 / PENALTY 为负，均须引用已审批单据）。"""

    BONUS = "BONUS"
    PENALTY = "PENALTY"


class PerformanceRuleStatus(StrEnum):
    """绩效规则版本状态（T19 / PRD §4.10 R19）。

    DRAFT 草稿 → ACTIVE 已配置人保存并经审核人审核、对生效日起的新快照可用；
    SUPERSEDED 被更新版本取代（历史快照继续引用旧版本，不回算）。
    """

    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
    SUPERSEDED = "SUPERSEDED"


class PerformanceSnapshotStatus(StrEnum):
    """月度绩效快照状态（T19 / PRD §4.10、§6.5）。

    DRAFT 月末冻结生成 → CONFIRMED 经理确认 → REVIEWED 财务复核；
    复核可退回 CONFIRMED；CONFIRMED/REVIEWED 后任何调整只追加更正单，不改原行。
    """

    DRAFT = "DRAFT"
    CONFIRMED = "CONFIRMED"
    REVIEWED = "REVIEWED"


class PerformanceAdjustmentKind(StrEnum):
    """快照更正单类型（T19）：扣减（责任扣款）/ 调整（审批的正向或修正项）。"""

    DEDUCTION = "DEDUCTION"
    ADJUSTMENT = "ADJUSTMENT"


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


class ExportFormat(StrEnum):
    """T21 导出格式（PRD §4.10 / §9.4：Excel/CSV/PDF）。"""

    XLSX = "XLSX"
    CSV = "CSV"
    PDF = "PDF"


class ExportStatus(StrEnum):
    """T21 异步导出任务状态（PRD §5.3 report.export_request → QUEUED）。"""

    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class ReportKey(StrEnum):
    """T21 经营报表数据集（PRD §4.10 经营报表多维筛选输出）。"""

    METRICS = "METRICS"                    # 指标汇总（MetricsService 唯一口径）
    CONTRACTS = "CONTRACTS"
    MERCHANTS = "MERCHANTS"
    FREEZERS = "FREEZERS"
    VISITS = "VISITS"
    SALES = "SALES"
    REPLENISHMENT = "REPLENISHMENT"
    PROCUREMENT = "PROCUREMENT"
    EXPENSES = "EXPENSES"
    PERFORMANCE = "PERFORMANCE"
    PROFIT = "PROFIT"


class AuditEventType(StrEnum):
    CREATE = "CREATE"
    UPDATE = "UPDATE"
    STATE_TRANSITION = "STATE_TRANSITION"
    DELETE_DENIED = "DELETE_DENIED"


class UserStatus(StrEnum):
    """账号生命周期状态（PRD §2.1 账号生命周期）。"""

    ACTIVE = "ACTIVE"  # 正常可用
    INACTIVE = "INACTIVE"  # 管理员停用，登录与 API 一律拒绝；历史记录保留
    PENDING_ACTIVATION = "PENDING_ACTIVATION"  # 一次性凭据未消费；或厂商门户未开启


class RoleCode(StrEnum):
    """受控的 6 类角色（PRD §2.2 角色全景）。

    与 Role.role_code 字段一一对应；代码层权限矩阵以此为锚，
    不允许来自 Excel 文本的覆盖。
    """

    SALES_REP = "SALES_REP"  # 业务员
    REGIONAL_MANAGER = "REGIONAL_MANAGER"  # 片区经理
    SUPERVISOR = "SUPERVISOR"  # 督导/运营管理员
    FINANCE = "FINANCE"  # 财务
    SYSTEM_ADMIN = "SYSTEM_ADMIN"  # 系统管理员
    SUPPLIER = "SUPPLIER"  # 厂商协作方（默认待激活）


class AuthEventType(StrEnum):
    """认证域审计事件（PRD §2.1、§9.4）。

    T03 先覆盖登录/激活/改密/重置/登出/被踢；通用业务审计由 T04 接管。
    """

    LOGIN_SUCCESS = "LOGIN_SUCCESS"
    LOGIN_FAILED = "LOGIN_FAILED"
    ACTIVATION_CONSUMED = "ACTIVATION_CONSUMED"
    PASSWORD_CHANGED = "PASSWORD_CHANGED"
    PASSWORD_RESET_ISSUED = "PASSWORD_RESET_ISSUED"
    PASSWORD_RESET_CONSUMED = "PASSWORD_RESET_CONSUMED"
    TOKEN_REVOKED = "TOKEN_REVOKED"
    LOGOUT = "LOGOUT"
    ACCOUNT_LOCKED = "ACCOUNT_LOCKED"


class TokenKind(StrEnum):
    """令牌类型（PRD §2.1 access/refresh）。"""

    ACCESS = "ACCESS"
    REFRESH = "REFRESH"
