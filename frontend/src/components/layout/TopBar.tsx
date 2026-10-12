/** 顶栏：品牌标识 / 全局搜索 / AI 助手入口 / 通知 / 用户（T06 / PRD §3.1）。 */
import { useState } from "react";

import { useAuth } from "../../lib/auth";

export function TopBar() {
  const { user, logout } = useAuth();
  const [query, setQuery] = useState("");
  return (
    <header
      className="flex h-14 items-center gap-3 border-b border-slate-200 bg-white px-4"
      data-testid="topbar"
    >
      <div className="flex items-center gap-2">
        <span
          aria-hidden="true"
          className="inline-block h-6 w-6 rounded-sm bg-[var(--color-brand)]"
        />
        <span className="text-sm font-semibold text-slate-800">沁爽分销</span>
      </div>
      <form
        role="search"
        onSubmit={(e) => e.preventDefault()}
        className="ml-4 flex-1 max-w-xl"
      >
        <label className="sr-only" htmlFor="topbar-search">
          全局搜索
        </label>
        <input
          id="topbar-search"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="搜索商家 / 合同 / 冰柜 / SKU"
          className="w-full rounded-md border border-slate-300 bg-slate-50 px-3 py-1.5 text-sm placeholder:text-slate-400"
        />
      </form>
      <button
        type="button"
        className="rounded-md border border-slate-300 bg-white px-2.5 py-1 text-xs text-slate-700 hover:bg-slate-50"
        aria-label="AI 助手"
      >
        AI 助手
      </button>
      <button
        type="button"
        className="rounded-md border border-slate-300 bg-white px-2.5 py-1 text-xs text-slate-700 hover:bg-slate-50"
        aria-label="通知"
      >
        通知
      </button>
      {user ? (
        <div className="flex items-center gap-2 text-sm text-slate-700">
          <span className="font-medium">{user.displayName}</span>
          <span className="text-slate-400">·</span>
          <span className="text-slate-500">{user.role}</span>
          <button
            type="button"
            onClick={logout}
            className="rounded-md border border-slate-300 bg-white px-2 py-1 text-xs text-slate-600"
          >
            退出
          </button>
        </div>
      ) : null}
    </header>
  );
}
