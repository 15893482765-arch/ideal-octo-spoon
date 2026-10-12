/** AuthContext（T06 / PRD §4.1）。
 *
 * T06 范围：
 * - 仅前端占位；不接真实后端 /auth/login（T03 已实现）。
 * - 暴露 currentUser、role、abilities 三个量；用于路由守卫 / 导航裁剪。
 * - `login`/`logout` 仅在开发环境模拟（URL ?role=SALES_REP）。
 */

import { createContext, useContext, useMemo, useState, type ReactNode } from "react";

import {
  ROLE_ABILITIES,
  hasAbility as hasAbilityFn,
  type ModuleCode,
  type RoleCode,
} from "./permissions";

export interface AuthUser {
  id: string;
  username: string;
  displayName: string;
  role: RoleCode;
}

export interface AuthContextValue {
  user: AuthUser | null;
  login: (user: AuthUser) => void;
  logout: () => void;
  hasAbility: (m: ModuleCode) => boolean;
}

const Ctx = createContext<AuthContextValue | null>(null);

export function AuthProvider(props: { children: ReactNode; initial?: AuthUser | null }) {
  const [user, setUser] = useState<AuthUser | null>(props.initial ?? null);
  const value = useMemo<AuthContextValue>(
    () => ({
      user,
      login: setUser,
      logout: () => setUser(null),
      hasAbility: (m) => hasAbilityFn(user?.role ?? null, m),
    }),
    [user],
  );
  return <Ctx.Provider value={value}>{props.children}</Ctx.Provider>;
}

export function useAuth(): AuthContextValue {
  const v = useContext(Ctx);
  if (!v) throw new Error("useAuth 必须在 <AuthProvider> 内使用");
  return v;
}

/** 工具：从 URL ?role= 解析开发态 mock 登录（仅供 Q028 测试与 dev 体验）。 */
export function devRoleFromUrl(search: string): AuthUser | null {
  const params = new URLSearchParams(search);
  const role = params.get("role") as RoleCode | null;
  if (!role || !(role in ROLE_ABILITIES)) return null;
  return {
    id: `dev-${role.toLowerCase()}`,
    username: `dev_${role.toLowerCase()}`,
    displayName: `开发账号 ${role}`,
    role,
  };
}
