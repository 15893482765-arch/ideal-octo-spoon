/** 展示层格式化（PRD：金额存「分」、时间 UTC 存储 / Asia/Shanghai 展示）。 */

const TZ = "Asia/Shanghai";

const dateTimeFmt = new Intl.DateTimeFormat("zh-CN", {
  timeZone: TZ,
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  hour12: false,
});

const dateFmt = new Intl.DateTimeFormat("zh-CN", {
  timeZone: TZ,
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
});

/** 分 → 元（保留 2 位小数，千分位）。非数字返回 "—"。 */
export function fenToYuan(fen: number | null | undefined): string {
  if (fen === null || fen === undefined || Number.isNaN(fen)) return "—";
  const yuan = fen / 100;
  return yuan.toLocaleString("zh-CN", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

/** 分为整数的金额（用于表格右对齐显示，带 ¥ 前缀）。 */
export function money(fen: number | null | undefined): string {
  const v = fenToYuan(fen);
  return v === "—" ? v : `¥${v}`;
}

/** Decimal/字符串箱数去尾零：`"12.50"` → `12.5`。 */
export function boxes(value: string | number | null | undefined): string {
  if (value === null || value === undefined || value === "") return "—";
  const n = typeof value === "number" ? value : Number(value);
  if (Number.isNaN(n)) return String(value);
  return String(Number(n.toFixed(2)));
}

function toDate(value: string | Date | null | undefined): Date | null {
  if (!value) return null;
  const d = value instanceof Date ? value : new Date(value);
  return Number.isNaN(d.getTime()) ? null : d;
}

/** UTC ISO → Asia/Shanghai `YYYY/MM/DD HH:mm`。 */
export function formatDateTime(value: string | Date | null | undefined): string {
  const d = toDate(value);
  return d ? dateTimeFmt.format(d) : "—";
}

/** UTC ISO → Asia/Shanghai `YYYY/MM/DD`。 */
export function formatDate(value: string | Date | null | undefined): string {
  const d = toDate(value);
  return d ? dateFmt.format(d) : "—";
}

/** 取 Asia/Shanghai 的今天（YYYY-MM-DD），用于默认查询周期。 */
export function todayInTz(): string {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: TZ,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(new Date());
  return parts;
}

/** 取 Asia/Shanghai 的当期月份 YYYY-MM。 */
export function currentPeriodMonth(): string {
  return todayInTz().slice(0, 7);
}

/** 取 Asia/Shanghai 当周周一（业务周从周一起算）。 */
export function currentWeekStart(): string {
  const today = todayInTz();
  const d = new Date(`${today}T00:00:00Z`);
  const dow = (d.getUTCDay() + 6) % 7; // 周一=0
  d.setUTCDate(d.getUTCDate() - dow);
  return d.toISOString().slice(0, 10);
}

export function percent(value: number | string | null | undefined, digits = 2): string {
  if (value === null || value === undefined || value === "") return "—";
  const n = typeof value === "number" ? value : Number(value);
  if (Number.isNaN(n)) return String(value);
  return `${n.toFixed(digits)}%`;
}

/** 业务枚举 → 中文标签（后端返回英文码值）。 */
const LABELS: Record<string, string> = {
  // 通用状态
  ACTIVE: "启用", INACTIVE: "停用", PENDING_ACTIVATION: "待激活",
  LEAD: "线索", NEGOTIATING: "洽谈中", PAUSED: "暂停", TERMINATED: "已终止",
  DRAFT: "草稿", SUBMITTED: "已提交", CONFIRMED: "已确认", CLOSED: "已关闭",
  CANCELLED: "已取消", REVIEWED: "已复核", APPROVED: "已审批", REJECTED: "已驳回",
  RETURNED: "已退回", PAID: "已发放", QUEUED: "排队中", RUNNING: "执行中",
  SUCCEEDED: "成功", FAILED: "失败", PENDING: "待处理", IN_PROGRESS: "进行中",
  COMPLETED: "已完成", OVERDUE: "已逾期", ABNORMAL: "异常", INVALIDATED: "已作废",
  PENDING_APPROVAL: "待审批", PENDING_PICKUP: "待回收", PENDING_ACCEPTANCE: "待验收",
  IN_STOCK: "在库", RESERVED: "已预留", DEPLOYED: "已投放", REPAIRING: "维修中",
  SCRAPPED: "已报废", IN_TRANSIT: "在途", UNDER_MAINTENANCE: "保养中",
  PUBLISHED: "已发布", IN_PROGRESS_ACTIVITY: "进行中", OPERATING: "进行中",
  // 合同
  EFFECTIVE: "已生效", TERMINATION_PENDING: "终止待审", EXPIRED: "已到期",
  // 巡访
  RECOGNIZED: "已认可", ABNORMAL_PENDING: "异常待判定", VALID: "有效", INVALID: "无效",
  WEEKLY_SCHEDULE: "周排班", AD_HOC: "临时", RECTIFICATION: "整改", SUPERVISION: "督导",
  NORMAL: "正常", NEEDS_REPLENISHMENT: "需补货", NEEDS_RECTIFICATION: "需整改",
  RISK_MERCHANT: "风险商家", PROPOSE_TERMINATION: "建议终止",
  ABSENT: "缺失", POWERED_OFF: "未通电", DIRTY: "不洁", DAMAGED: "损坏", OTHER: "其他",
  STOREFRONT: "门头照", FREEZER: "冰柜照",
  // 冰柜流水
  PURCHASE: "购置入库", DEPLOYMENT_ORDER: "投放单", DEPLOY: "投放",
  RECALL: "撤回", RECALL_REQUEST: "撤回申请", RECALL_REJECT: "撤回驳回",
  RECALL_RESCHEDULE: "改回收日期",
  PICKUP: "回收", ACCEPT: "验收", REPAIR_START: "送修",
  REPAIR_END: "维修完成", SCRAP: "报废", DEPOSIT_COLLECTED: "押金收缴",
  DEPOSIT_REFUNDED: "押金退还",
  // 补货 / 采购
  REQUESTED: "已申请", PARTIALLY_FULFILLED: "部分履约", FULFILLED: "已履约",
  DELIVERED: "已送达", SUPPLIER_CONFIRMED: "厂商已确认", SUPPLY_REJECTED: "厂商已拒绝",
  RECEIVED: "已收货",
  // 活动 / 样品
  CONSUMED: "已消耗", MISSING: "缺失未还",
  // 整改
  OPEN: "待处理", VERIFIED: "已核实",
  // 风险 / 导出
  ACKNOWLEDGED: "已确认", XLSX: "Excel", CSV: "CSV", PDF: "PDF",
  // 角色
  SALES_REP: "业务员", REGIONAL_MANAGER: "片区经理", SUPERVISOR: "督导",
  FINANCE: "财务", SYSTEM_ADMIN: "系统管理员", SUPPLIER: "厂商",
  // 绩效 / 工资
  SUPERSEDED: "已取代", DEDUCTION: "扣减", ADJUSTMENT: "调整",
  BONUS: "奖金", PENALTY: "罚扣",
  // 绩效快照依据行类型 / 审批依据动作
  NEW_CONTRACT: "新签合同", VALID_VISIT: "有效巡访", CONFIRMED_SALES: "确认销量",
  ACTIVITY_ACHIEVED: "活动达成", SALES_TIER_SUMMARY: "销量阶梯汇总",
  "performance.deduction": "绩效扣减", "performance.adjustment": "绩效调整",
  PROCUREMENT: "采购", FREEZER_PURCHASE: "冰柜购置", FREEZER_REPAIR: "冰柜维修",
  ACTIVITY: "活动", BI: "—",
  // 排班批次
  PLANNED: "已计划", COMPLIANT: "达标",
  // 结算类型
  PRIMARY: "主责", TEMPORARY: "临时",
  // 整改类别
  DISPLAY: "陈列", SERVICE: "服务", EQUIPMENT: "设备",
  // 变更类型
  INIT: "初始", CHANGE: "变更", TERMINATE: "终止",
  // 主数据导入批次阶段 / 回滚
  UPLOADED: "已上传", PREFLIGHT: "已预检", EXECUTED: "已执行", ROLLED_BACK: "已回滚",
  // 审计事件（audit_logs.event_type）
  CREATE: "创建", UPDATE: "更新", STATE_TRANSITION: "状态迁移", DELETE_DENIED: "删除被拒",
  // 认证事件（auth_events.event_type）
  LOGIN_SUCCESS: "登录成功", LOGIN_FAILED: "登录失败",
  ACTIVATION_CONSUMED: "激活凭据已消费", PASSWORD_CHANGED: "已改密",
  PASSWORD_RESET_ISSUED: "重置凭据已签发", PASSWORD_RESET_CONSUMED: "重置凭据已消费",
  TOKEN_REVOKED: "令牌已吊销", LOGOUT: "登出", ACCOUNT_LOCKED: "账号已锁定",
};

export function label(value: string | null | undefined): string {
  if (!value) return "—";
  return LABELS[value] ?? value;
}

/** 状态 → 徽标配色（取值与 components/StatusBadge 的 tone 对齐）。 */
export type StatusTone =
  | "success"
  | "warning"
  | "danger"
  | "info"
  | "neutral";

export function statusTone(status: string | null | undefined): StatusTone {
  switch (status) {
    case "ACTIVE": case "EFFECTIVE": case "VALID": case "PAID": case "SUCCEEDED":
    case "FULFILLED": case "COMPLETED": case "APPROVED": case "IN_STOCK": case "CLOSED":
    case "REVIEWED": case "RECEIVED": case "VERIFIED": case "CONFIRMED":
    case "LOGIN_SUCCESS": case "CREATE":
      return "success";
    case "PENDING": case "PENDING_APPROVAL": case "PENDING_PICKUP": case "PENDING_ACCEPTANCE":
    case "PENDING_ACTIVATION": case "SUBMITTED": case "NEGOTIATING": case "OPEN":
    case "QUEUED": case "RUNNING": case "IN_PROGRESS": case "ACKNOWLEDGED":
    case "PARTIALLY_FULFILLED": case "IN_TRANSIT": case "DRAFT":
    case "PASSWORD_RESET_ISSUED": case "TOKEN_REVOKED": case "LOGOUT":
      return "warning";
    case "TERMINATED": case "REJECTED": case "SCRAPPED": case "ABNORMAL":
    case "OVERDUE": case "FAILED": case "INVALIDATED": case "SUPPLY_REJECTED":
    case "INVALID": case "MISSING":
    case "LOGIN_FAILED": case "ACCOUNT_LOCKED": case "DELETE_DENIED":
      return "danger";
    case "DEPLOYED": case "PUBLISHED": case "REPAIRING": case "RESERVED": case "SUPERSEDED":
    case "UPDATE": case "STATE_TRANSITION": case "PASSWORD_CHANGED":
    case "ACTIVATION_CONSUMED": case "PASSWORD_RESET_CONSUMED":
      return "info";
    default:
      return "neutral";
  }
}
