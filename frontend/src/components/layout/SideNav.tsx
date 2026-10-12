/** 侧栏导航：按角色裁剪（T06 / PRD §3.1 "左侧导航按模块权限裁剪"）。 */
import { NavLink } from "react-router-dom";

import { useAuth } from "../../lib/auth";
import { filterNavByRole, type NavItem } from "../../lib/nav-config";

export interface SideNavProps {
  items?: ReadonlyArray<NavItem>;
  onNavigate?: () => void;
}

export function SideNav({ items, onNavigate }: SideNavProps) {
  const { user, hasAbility } = useAuth();
  const nav = items ?? filterNavByRole(user?.role ?? null, hasAbility);
  return (
    <nav
      aria-label="角色导航"
      data-testid="sidenav"
      className="h-full w-60 shrink-0 border-r border-slate-200 bg-white p-3"
    >
      <ul className="space-y-1">
        {nav.map((item) => (
          <li key={item.to}>
            <NavLink
              to={item.to}
              onClick={onNavigate}
              className={({ isActive }) =>
                `block rounded-md px-3 py-2 text-sm ${
                  isActive
                    ? "bg-[var(--color-brand)]/10 font-medium text-[var(--color-brand)]"
                    : "text-slate-700 hover:bg-slate-100"
                }`
              }
            >
              {item.label}
            </NavLink>
          </li>
        ))}
      </ul>
    </nav>
  );
}
