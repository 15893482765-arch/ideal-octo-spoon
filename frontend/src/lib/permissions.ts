/** 前端权限镜像（T06 / PRD §2.2、§2.3）。
 *
 * - 与后端 T03 PermissionService 的模块常量保持同名同值；
 * - 前端只用做"导航裁剪 / 元素可见性"提示；任何真正拦截仍由后端能力校验兜底
 *   （PRD §3.1 "后端接口仍须二次鉴权"）。
 */

export const MODULE = {
  USER_ADMIN: "user.admin",
  AUTH_LOGIN: "auth.login",
  DASHBOARD: "dashboard.view",
  MERCHANT: "merchant",
  CONTRACT: "contract",
  FREEZER: "freezer",
  VISIT: "visit",
  REPLENISHMENT: "replenishment",
  SALES_CONFIRM: "sales.confirm",
  SCHEDULE: "schedule",
  ACTIVITY: "campaign",
  PROCUREMENT: "procurement",
  FINANCE: "finance",
  PERFORMANCE: "performance",
  PAYROLL: "payroll",
  PAYROLL_DISBURSE: "payroll.disburse",
  AUDIT: "audit",
  APPROVAL: "approval",
  SETTINGS: "settings",
  SUPPLIER_CONFIRM: "supplier.confirm_supply",
} as const;

export type ModuleCode = (typeof MODULE)[keyof typeof MODULE];

/** 6 类角色能力（与 T03 _PROFILE_* 一致）。 */
export const ROLE_ABILITIES: Record<string, ReadonlySet<ModuleCode>> = {
  SALES_REP: new Set([
    MODULE.AUTH_LOGIN,
    MODULE.DASHBOARD,
    MODULE.MERCHANT,
    MODULE.CONTRACT,
    MODULE.FREEZER,
    MODULE.VISIT,
    MODULE.REPLENISHMENT,
    MODULE.SCHEDULE,
    MODULE.ACTIVITY,
  ]),
  REGIONAL_MANAGER: new Set([
    MODULE.AUTH_LOGIN,
    MODULE.DASHBOARD,
    MODULE.MERCHANT,
    MODULE.CONTRACT,
    MODULE.FREEZER,
    MODULE.VISIT,
    MODULE.REPLENISHMENT,
    MODULE.SALES_CONFIRM,
    MODULE.SCHEDULE,
    MODULE.ACTIVITY,
    MODULE.PROCUREMENT,
    MODULE.PERFORMANCE,
    MODULE.APPROVAL,
  ]),
  SUPERVISOR: new Set([
    MODULE.AUTH_LOGIN,
    MODULE.DASHBOARD,
    MODULE.VISIT,
    MODULE.MERCHANT,
    MODULE.CONTRACT,
    MODULE.FREEZER,
    MODULE.REPLENISHMENT,
    MODULE.SCHEDULE,
    MODULE.ACTIVITY,
    MODULE.APPROVAL,
  ]),
  FINANCE: new Set([
    MODULE.AUTH_LOGIN,
    MODULE.DASHBOARD,
    MODULE.FINANCE,
    MODULE.PROCUREMENT,
    MODULE.PERFORMANCE,
    MODULE.PAYROLL,
    MODULE.PAYROLL_DISBURSE,
    MODULE.REPLENISHMENT,
    MODULE.APPROVAL,
  ]),
  SYSTEM_ADMIN: new Set([
    MODULE.AUTH_LOGIN,
    MODULE.DASHBOARD,
    MODULE.USER_ADMIN,
    MODULE.AUDIT,
    MODULE.SETTINGS,
    MODULE.MERCHANT,
    MODULE.CONTRACT,
    MODULE.FREEZER,
    MODULE.VISIT,
    MODULE.REPLENISHMENT,
    MODULE.SALES_CONFIRM,
    MODULE.SCHEDULE,
    MODULE.ACTIVITY,
    MODULE.PROCUREMENT,
    MODULE.FINANCE,
    MODULE.PERFORMANCE,
    MODULE.APPROVAL,
  ]),
  SUPPLIER: new Set([
    MODULE.AUTH_LOGIN,
    MODULE.DASHBOARD,
    MODULE.SUPPLIER_CONFIRM,
    MODULE.PROCUREMENT,
  ]),
};

export type RoleCode =
  | "SALES_REP"
  | "REGIONAL_MANAGER"
  | "SUPERVISOR"
  | "FINANCE"
  | "SYSTEM_ADMIN"
  | "SUPPLIER";

export function hasAbility(role: RoleCode | null | undefined, m: ModuleCode): boolean {
  if (!role) return false;
  return ROLE_ABILITIES[role]?.has(m) ?? false;
}
