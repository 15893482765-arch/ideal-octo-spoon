/** 登录页占位（T06 / PRD §4.1 登录页）。 */
import { useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";

import { useAuth, type AuthUser } from "../lib/auth";
import type { RoleCode } from "../lib/permissions";

const ROLES: ReadonlyArray<RoleCode> = [
  "SALES_REP",
  "REGIONAL_MANAGER",
  "SUPERVISOR",
  "FINANCE",
  "SYSTEM_ADMIN",
  "SUPPLIER",
];

export function Login() {
  const { login } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const [role, setRole] = useState<RoleCode>("SALES_REP");
  const from = (location.state as { from?: string } | null)?.from ?? "/workspace";
  function submit(e: React.FormEvent) {
    e.preventDefault();
    const user: AuthUser = {
      id: `dev-${role.toLowerCase()}`,
      username: `dev_${role.toLowerCase()}`,
      displayName: `开发账号 ${role}`,
      role,
    };
    login(user);
    navigate(from, { replace: true });
  }
  return (
    <div className="flex min-h-screen items-center justify-center bg-slate-50">
      <form
        onSubmit={submit}
        className="w-full max-w-sm rounded-lg border border-slate-200 bg-white p-6 shadow-sm"
      >
        <h1 className="text-base font-semibold text-slate-800">沁爽饮料分销管理平台</h1>
        <p className="mt-1 text-xs text-slate-500">无注册入口；T06 占位仅供开发与单测。</p>
        <p className="mt-2 text-xs text-slate-400">下一步：T07 主数据（账号/品牌/SKU/片区/规则）</p>
        <label className="mt-4 block text-xs text-slate-600" htmlFor="role">
          模拟角色
        </label>
        <select
          id="role"
          value={role}
          onChange={(e) => setRole(e.target.value as RoleCode)}
          className="mt-1 w-full rounded-md border border-slate-300 bg-white px-2 py-1.5 text-sm"
        >
          {ROLES.map((r) => (
            <option key={r} value={r}>
              {r}
            </option>
          ))}
        </select>
        <button
          type="submit"
          className="mt-4 w-full rounded-md bg-[var(--color-brand)] py-1.5 text-sm font-medium text-white"
        >
          登录
        </button>
      </form>
    </div>
  );
}
