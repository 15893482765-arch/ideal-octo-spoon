/** 导航与路由表（T06 / PRD §3.1、§3.2）。 */

import { MODULE, type ModuleCode, type RoleCode } from "./permissions";

export interface NavItem {
  to: string;
  label: string;
  /** 业务领域描述，写在路由占位页"下一步"提示。 */
  nextStep: string;
  required: ModuleCode;
  /** 仅在某几类角色下显示；为空则按 required 能力校验。 */
  onlyFor?: ReadonlyArray<RoleCode>;
}

export const PRIMARY_NAV: ReadonlyArray<NavItem> = [
  { to: "/workspace", label: "今日工作台", nextStep: "T11/T19 排班/指标", required: MODULE.DASHBOARD },
  { to: "/merchants", label: "商家中心", nextStep: "T08 商家中心卡", required: MODULE.MERCHANT },
  { to: "/contracts", label: "合同中心", nextStep: "T09 合同与审批", required: MODULE.CONTRACT },
  { to: "/assets/freezers", label: "冰柜资产台账", nextStep: "T10 冰柜资产", required: MODULE.FREEZER },
  { to: "/visits", label: "巡访与打卡", nextStep: "T11 巡访与排班", required: MODULE.VISIT },
  { to: "/schedule", label: "周排班与地图", nextStep: "T13 周排班", required: MODULE.SCHEDULE },
  { to: "/replenishments", label: "补货中心", nextStep: "T15 补货与核销", required: MODULE.REPLENISHMENT },
  { to: "/campaigns", label: "地推活动", nextStep: "T16 活动与兑付", required: MODULE.ACTIVITY },
  { to: "/supervision", label: "督导跟访与整改", nextStep: "T11 督导整改", required: MODULE.VISIT, onlyFor: ["SUPERVISOR", "REGIONAL_MANAGER", "SYSTEM_ADMIN"] },
  { to: "/procurement", label: "采购与供货", nextStep: "T14 采购与供货", required: MODULE.PROCUREMENT },
  { to: "/finance/performance", label: "绩效核算", nextStep: "T19 绩效汇总", required: MODULE.PERFORMANCE },
  { to: "/finance/payroll", label: "工资发放", nextStep: "T19 工资发放", required: MODULE.PAYROLL },
  { to: "/reports", label: "经营报表", nextStep: "T20 经营报表", required: MODULE.DASHBOARD, onlyFor: ["REGIONAL_MANAGER", "FINANCE", "SYSTEM_ADMIN"] },
  { to: "/admin/users", label: "用户与账号管理", nextStep: "T07/T03 账号/角色", required: MODULE.USER_ADMIN, onlyFor: ["SYSTEM_ADMIN"] },
  { to: "/admin/master-data", label: "品牌/SKU/片区/规则/导入", nextStep: "T07 主数据", required: MODULE.SETTINGS, onlyFor: ["SYSTEM_ADMIN"] },
  { to: "/admin/audit", label: "审计日志", nextStep: "T18 审计/导出", required: MODULE.AUDIT, onlyFor: ["SYSTEM_ADMIN", "REGIONAL_MANAGER", "FINANCE"] },
  { to: "/supplier", label: "厂商门户", nextStep: "T12 厂商门户", required: MODULE.SUPPLIER_CONFIRM, onlyFor: ["SUPPLIER"] },
];

export function filterNavByRole(role: RoleCode | null, has: (m: ModuleCode) => boolean): NavItem[] {
  return PRIMARY_NAV.filter((item) => {
    if (item.onlyFor && (!role || !item.onlyFor.includes(role))) return false;
    return has(item.required);
  });
}
