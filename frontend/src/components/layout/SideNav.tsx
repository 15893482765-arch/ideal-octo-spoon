/** 侧栏导航：按角色裁剪（T06 / PRD §3.1 "左侧导航按模块权限裁剪"）。
 *
 * UI 迭代：改为分组导航 + 图标 + 紫色胶囊选中态。
 * 分组只是本文件的展示层映射（path → 分组标题），不改变路由与权限裁剪逻辑：
 * 未命中任何分组的条目会自动落到最后的"更多"组，保证新路由不会消失。
 */
import { NavLink } from "react-router-dom";

import { useAuth } from "../../lib/auth";
import { filterNavByRole, type NavItem } from "../../lib/nav-config";

export interface SideNavProps {
  items?: ReadonlyArray<NavItem>;
  onNavigate?: () => void;
}

/** 分组顺序即侧栏顺序；路径必须与 nav-config 的 `to` 一致。 */
const NAV_GROUPS: ReadonlyArray<{ title: string; paths: ReadonlyArray<string> }> = [
  { title: "总览", paths: ["/workspace"] },
  {
    title: "业务运营",
    paths: [
      "/merchants",
      "/contracts",
      "/assets/freezers",
      "/visits",
      "/schedule",
      "/replenishments",
      "/campaigns",
      "/supervision",
    ],
  },
  {
    title: "经营分析",
    paths: [
      "/procurement",
      "/finance/performance",
      "/finance/payroll",
      "/finance/ledger",
      "/finance/profit",
      "/reports",
      "/assistant",
    ],
  },
  {
    title: "系统管理",
    paths: ["/admin/users", "/admin/regions", "/admin/master-data", "/admin/audit", "/supplier"],
  },
];

/** 线性图标：统一 20×20、1.6 描边，颜色随 currentColor。 */
const ICON_PATHS: Record<string, string> = {
  "/workspace": "M3 10.5 12 3l9 7.5M5.5 9.5V20h13V9.5",
  "/merchants": "M4 9.5V20h16V9.5M3 9.5 12 4l9 5.5M9 20v-5.5h6V20",
  "/contracts": "M7 3.5h7l4 4V20H7zM14 3.5V8h4M9.5 12h6M9.5 15.5h4",
  "/assets/freezers": "M6 3.5h12v17H6zM6 10h12M9.5 6.5h1M9.5 14h1",
  "/visits": "M12 21s7-5.6 7-11a7 7 0 1 0-14 0c0 5.4 7 11 7 11zM12 12.5a2.5 2.5 0 1 0 0-5 2.5 2.5 0 0 0 0 5z",
  "/schedule": "M4 6.5h16V20H4zM4 10.5h16M8.5 3.5v4M15.5 3.5v4",
  "/replenishments": "M4 7.5 12 3.5l8 4v9l-8 4-8-4zM4 7.5l8 4 8-4M12 11.5V20",
  "/campaigns": "M4 11v3l10 4.5V6.5zM14 8.5a3.5 3.5 0 0 1 0 6.5M5 14v4.5",
  "/supervision": "M12 3.5 4.5 6.5v6c0 4.2 3.2 7.2 7.5 8 4.3-.8 7.5-3.8 7.5-8v-6zM9 12l2.2 2.2L15.5 10",
  "/procurement": "M4 6h16l-1.5 10.5H6.5zM9 6V4.5h6V6M9 20h.01M16 20h.01",
  "/finance/performance": "M4 19.5V13M9.5 19.5V8M15 19.5v-9M20.5 19.5V4.5",
  "/finance/payroll": "M3.5 7.5h17v11h-17zM12 15.5a2.5 2.5 0 1 0 0-5 2.5 2.5 0 0 0 0 5zM6 11v.01M18 15v.01",
  "/finance/ledger": "M5 3.5h11l3 3V20.5H5zM8 8h8M8 12h8M8 16h5",
  "/finance/profit": "M4 16.5 9 11l3.5 3.5L20 6.5M20 6.5h-4.5M20 6.5V11",
  "/reports": "M12 3.5a8.5 8.5 0 1 0 0 17 8.5 8.5 0 0 0 0-17zM12 8v4.5l3 1.8",
  "/assistant": "M12 3.5 13.8 9l5.7 1.8-5.7 1.9L12 18.5 10.2 12.7 4.5 10.8 10.2 9zM18.5 17.5l.7 2 2 .7-2 .7-.7 2-.7-2-2-.7 2-.7z",
  "/admin/users": "M9 11a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7zM2.5 20c0-3.3 2.9-6 6.5-6s6.5 2.7 6.5 6M17 8.5h4M19 6.5v4",
  "/admin/regions": "M12 3.5a8.5 8.5 0 1 0 0 17 8.5 8.5 0 0 0 0-17zM3.5 12h17M12 3.5c2.5 2.4 3.8 5.4 3.8 8.5S14.5 18.1 12 20.5c-2.5-2.4-3.8-5.4-3.8-8.5S9.5 5.9 12 3.5z",
  "/admin/master-data": "M4 6.5c0-1.4 3.6-2.5 8-2.5s8 1.1 8 2.5S16.4 9 12 9 4 7.9 4 6.5zM4 6.5v11c0 1.4 3.6 2.5 8 2.5s8-1.1 8-2.5v-11M4 12c0 1.4 3.6 2.5 8 2.5s8-1.1 8-2.5",
  "/admin/audit": "M12 3.5a8.5 8.5 0 1 0 0 17 8.5 8.5 0 0 0 0-17zM8 12.5l2.5 2.5L16 9.5",
  "/supplier": "M3.5 8.5 12 4l8.5 4.5v7L12 20l-8.5-4.5zM12 12v8M3.5 8.5 12 12l8.5-3.5",
};

const FALLBACK_ICON = "M4.5 12h15M12 4.5v15";

function NavIcon({ path }: { path: string }) {
  return (
    <svg
      aria-hidden="true"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.6}
      strokeLinecap="round"
      strokeLinejoin="round"
      className="h-[18px] w-[18px] shrink-0"
    >
      <path d={ICON_PATHS[path] ?? FALLBACK_ICON} />
    </svg>
  );
}

export function SideNav({ items, onNavigate }: SideNavProps) {
  const { user, hasAbility } = useAuth();
  const nav = items ?? filterNavByRole(user?.role ?? null, hasAbility);

  // 按分组映射切分；未命中的条目统一收进"更多"，避免新增路由被吞掉。
  const known = new Set(NAV_GROUPS.flatMap((g) => g.paths));
  const sections = [
    ...NAV_GROUPS.map((group) => ({
      title: group.title,
      items: group.paths
        .map((path) => nav.find((item) => item.to === path))
        .filter((item): item is NavItem => Boolean(item)),
    })),
    { title: "更多", items: nav.filter((item) => !known.has(item.to)) },
  ].filter((section) => section.items.length > 0);

  return (
    <nav
      aria-label="角色导航"
      data-testid="sidenav"
      className="h-full w-[232px] shrink-0 overflow-y-auto bg-surface px-3 pt-4 pb-6"
    >
      {sections.map((section) => (
        <div key={section.title} className="mb-5 last:mb-0">
          <p className="qs-nav-group mb-2 px-3">{section.title}</p>
          <ul className="space-y-0.5">
            {section.items.map((item) => (
              <li key={item.to}>
                <NavLink
                  to={item.to}
                  onClick={onNavigate}
                  className={({ isActive }) =>
                    [
                      "group flex items-center gap-2.5 rounded-[12px] px-3 py-2 text-[13px] transition-colors",
                      isActive
                        ? "bg-brand font-medium text-white shadow-[0_10px_22px_-14px_rgba(124,92,252,0.95)]"
                        : "text-ink-2 hover:bg-hover hover:text-ink",
                    ].join(" ")
                  }
                >
                  <NavIcon path={item.to} />
                  <span className="truncate">{item.label}</span>
                </NavLink>
              </li>
            ))}
          </ul>
        </div>
      ))}
    </nav>
  );
}
