/** 导航与路由表（T06 · PRD §3.1 全局框架、§3.2 页面路由）。
 *
 * 单一真相源：左侧导航（SideNav）与占位路由（App）都从 `PAGE_ROUTES` 派生，
 * 避免此前 label/nextStep 在 nav-config 与 App.tsx 各写一份而漂移。
 *
 * `nextStep` 的任务卡编号必须与
 * 《沁爽饮料分销管理平台-AI-Agent任务卡开发流程-Python版-v2.0.md》一致。
 */

import { MODULE, type ModuleCode, type RoleCode } from "./permissions";

export interface PageRoute {
  /** 路由路径（PRD §3.2 页面路由） */
  to: string;
  /** 侧栏与占位页标题 */
  label: string;
  /** 业务领域描述，写在路由占位页"下一步"提示（含权威任务卡编号） */
  nextStep: string;
  required: ModuleCode;
  /** 仅在某几类角色下显示；为空则按 required 能力校验。 */
  onlyFor?: ReadonlyArray<RoleCode>;
  /** 已实现页面：不渲染占位页。 */
  implemented?: boolean;
  /** 详情路由：不出现在左侧导航，但 PRD §3.2 仍要求可直达。 */
  hidden?: boolean;
}

export type NavItem = PageRoute;

export const PAGE_ROUTES: ReadonlyArray<PageRoute> = [
  {
    to: "/workspace",
    label: "今日工作台",
    nextStep: "T27 角色工作台、通知与全局搜索",
    required: MODULE.DASHBOARD,
    implemented: true,
  },
  { to: "/merchants", label: "商家中心", nextStep: "T08 商家中心与责任归属", required: MODULE.MERCHANT },
  { to: "/contracts", label: "合同中心", nextStep: "T09 合同申请、审批与版本", required: MODULE.CONTRACT },
  {
    to: "/assets/freezers",
    label: "冰柜资产台账",
    nextStep: "T10 冰柜投放、撤回、验收与维修",
    required: MODULE.FREEZER,
    // 台账是现场实物视图：财务购置、片区经理/督导看管并投放；系统管理员不参与实物管理
    onlyFor: ["SALES_REP", "REGIONAL_MANAGER", "SUPERVISOR", "FINANCE"],
  },
  {
    to: "/visits",
    label: "巡访与打卡",
    nextStep: "T11 巡访任务、定位、照片与有效性",
    required: MODULE.VISIT,
  },
  { to: "/schedule", label: "周排班与地图", nextStep: "T13 周排班与冲突原子校验", required: MODULE.SCHEDULE },
  {
    to: "/replenishments",
    label: "补货中心",
    nextStep: "T17 补货确认与履约",
    required: MODULE.REPLENISHMENT,
  },
  { to: "/campaigns", label: "地推活动", nextStep: "T15 地推活动与资源占用", required: MODULE.ACTIVITY },
  {
    to: "/supervision",
    label: "督导跟访与整改",
    nextStep: "T16 督导跟访与整改闭环",
    required: MODULE.VISIT,
    onlyFor: ["SUPERVISOR", "REGIONAL_MANAGER"],
  },
  {
    to: "/procurement",
    label: "采购与供货",
    nextStep: "T18 采购、供货与可选厂商门户",
    required: MODULE.PROCUREMENT,
  },
  {
    to: "/finance/performance",
    label: "绩效核算",
    nextStep: "T19 绩效规则版本与月度快照",
    required: MODULE.PERFORMANCE,
    implemented: true,
  },
  {
    to: "/finance/payroll",
    label: "工资发放",
    nextStep: "T20 费用、工资与发放状态",
    required: MODULE.PAYROLL,
    implemented: true,
  },
  {
    to: "/finance/ledger",
    label: "财务总账",
    nextStep: "T35 财务总账、公司物品采购与流水",
    required: MODULE.FINANCE,
    onlyFor: ["FINANCE", "SYSTEM_ADMIN"],
    implemented: true,
  },
  {
    to: "/finance/profit",
    label: "经营损益",
    nextStep: "T21 MetricsService、RiskService、报表与导出",
    required: MODULE.FINANCE,
    onlyFor: ["FINANCE", "SYSTEM_ADMIN"],
    implemented: true,
  },
  {
    to: "/reports",
    label: "经营报表",
    nextStep: "T21 MetricsService、RiskService、报表与导出",
    required: MODULE.DASHBOARD,
    onlyFor: ["REGIONAL_MANAGER", "FINANCE", "SYSTEM_ADMIN"],
    implemented: true,
  },
  {
    to: "/assistant",
    label: "AI 助手",
    nextStep: "T23 AI 助手对话与会话持久化",
    required: MODULE.DASHBOARD,
    // 助手面向内部业务角色；供应商门户不进助手
    onlyFor: ["SALES_REP", "REGIONAL_MANAGER", "SUPERVISOR", "FINANCE", "SYSTEM_ADMIN"],
    implemented: true,
  },
  {
    to: "/admin/users",
    label: "用户与账号管理",
    nextStep: "T03 登录、管理员建号、RBAC/ABAC",
    required: MODULE.USER_ADMIN,
    onlyFor: ["SYSTEM_ADMIN"],
    implemented: true,
  },
  {
    to: "/admin/regions",
    label: "片区管理",
    nextStep: "T02 片区新建与台账（片区经理可用）",
    required: MODULE.REGION_MANAGE,
    onlyFor: ["SYSTEM_ADMIN", "REGIONAL_MANAGER"],
    implemented: true,
  },
  {
    to: "/admin/master-data",
    label: "品牌/SKU/片区/规则/导入",
    nextStep: "T07 沁爽主数据 Excel 导入",
    required: MODULE.SETTINGS,
    onlyFor: ["SYSTEM_ADMIN"],
    implemented: true,
  },
  {
    to: "/admin/audit",
    label: "审计日志",
    nextStep: "T04 审计、幂等、文件与敏感字段",
    required: MODULE.AUDIT,
    // 与后端能力矩阵一致：audit 能力按 PRD 角色职责只授予系统管理员
    onlyFor: ["SYSTEM_ADMIN"],
    implemented: true,
  },
  {
    to: "/supplier",
    label: "厂商门户",
    nextStep: "T18 采购、供货与可选厂商门户",
    required: MODULE.SUPPLIER_CONFIRM,
    onlyFor: ["SUPPLIER"],
  },

  // ---- 深链落点：不进侧栏，但 WorkbenchService 的 deep_link 会直达 ----
  {
    to: "/risks",
    label: "风险清单",
    nextStep: "T21 MetricsService、RiskService、报表与导出",
    // 与后端 RiskService.list_risks 一致：只要求 dashboard 能力，行级范围由服务端裁剪
    required: MODULE.DASHBOARD,
    implemented: true,
    hidden: true,
  },

  // ---- PRD §3.2 详情路由：不进侧栏，但必须可直达 ----
  { to: "/merchants/:id", label: "商家档案", nextStep: "T08 商家中心与责任归属", required: MODULE.MERCHANT, hidden: true },
  { to: "/contracts/:id", label: "合同详情/审批", nextStep: "T09 合同申请、审批与版本", required: MODULE.CONTRACT, hidden: true },
  {
    to: "/assets/freezers/:id",
    label: "冰柜详情与流水",
    nextStep: "T10 冰柜投放、撤回、验收与维修",
    required: MODULE.FREEZER,
    hidden: true,
  },
  { to: "/visits/:id", label: "巡访详情", nextStep: "T11 巡访任务、定位、照片与有效性", required: MODULE.VISIT, hidden: true },
];

/** 左侧导航：排除详情路由。 */
export const PRIMARY_NAV: ReadonlyArray<NavItem> = PAGE_ROUTES.filter((r) => !r.hidden);

/** 按路径取路由配置（占位页/守卫取 label 与 nextStep）。 */
export function routeByPath(path: string): PageRoute | undefined {
  return PAGE_ROUTES.find((r) => r.to === path);
}

export function filterNavByRole(role: RoleCode | null, has: (m: ModuleCode) => boolean): NavItem[] {
  return PRIMARY_NAV.filter((item) => {
    if (item.onlyFor && (!role || !item.onlyFor.includes(role))) return false;
    return has(item.required);
  });
}
