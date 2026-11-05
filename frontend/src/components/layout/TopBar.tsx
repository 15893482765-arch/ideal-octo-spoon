/** 顶栏：品牌标识 / 全局搜索 / 通知 / 用户（T06 / PRD §3.1）。
 *
 * AI 助手入口已移至侧边栏「AI 助手」页（会话列表 + 对话抽屉），
 * 顶栏不再挂抽屉。
 *
 * UI 迭代：仅调整视觉（白底、胶囊搜索框、圆形头像），搜索框仍是受控本地状态，
 * 未接任何接口（与原实现一致）。
 */
import { useState } from "react";

import { useAuth } from "../../lib/auth";

export function TopBar() {
  const { user, logout } = useAuth();
  const [query, setQuery] = useState("");
  const initial = user?.displayName?.trim()?.[0] ?? "·";
  return (
    <header
      className="flex h-16 shrink-0 items-center gap-3 border-b border-line bg-surface px-4 lg:px-6"
      data-testid="topbar"
    >
      <div className="flex w-[196px] items-center gap-2.5">
        <span
          aria-hidden="true"
          className="grid h-8 w-8 place-items-center rounded-[10px] bg-gradient-to-br from-brand to-brand-strong text-[15px] font-bold text-white shadow-[0_8px_18px_-10px_rgba(124,92,252,1)]"
        >
          沁
        </span>
        <span className="text-[15px] font-semibold tracking-tight text-ink">沁爽分销</span>
      </div>

      <form
        role="search"
        onSubmit={(e) => e.preventDefault()}
        className="ml-1 flex max-w-[420px] flex-1 items-center"
      >
        <label className="sr-only" htmlFor="topbar-search">
          全局搜索
        </label>
        <div className="relative w-full">
          <svg
            aria-hidden="true"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth={1.7}
            strokeLinecap="round"
            className="pointer-events-none absolute top-1/2 left-3 h-4 w-4 -translate-y-1/2 text-ink-4"
          >
            <circle cx="11" cy="11" r="6.5" />
            <path d="m16 16 4 4" />
          </svg>
          <input
            id="topbar-search"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="搜索商家 / 合同 / 冰柜 / SKU"
            className="h-9 w-full rounded-full bg-page pr-3 pl-9 text-[13px] text-ink placeholder:text-ink-4 focus:bg-surface focus:shadow-[inset_0_0_0_1px_var(--color-brand-ring)] focus:outline-none"
          />
        </div>
      </form>

      <div className="ml-auto flex items-center gap-2">
        <button
          type="button"
          className="grid h-9 w-9 place-items-center rounded-full text-ink-3 transition-colors hover:bg-hover hover:text-ink"
          aria-label="通知"
        >
          <svg
            aria-hidden="true"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth={1.6}
            strokeLinecap="round"
            strokeLinejoin="round"
            className="h-[18px] w-[18px]"
          >
            <path d="M18 9a6 6 0 1 0-12 0c0 4-2 5-2 5h16s-2-1-2-5" />
            <path d="M10.5 19a1.8 1.8 0 0 0 3 0" />
          </svg>
        </button>

        {user ? (
          <div className="flex items-center gap-2 rounded-full bg-page py-1 pr-1 pl-1.5">
            <span
              aria-hidden="true"
              className="grid h-7 w-7 place-items-center rounded-full bg-brand-soft text-[12px] font-semibold text-brand-strong"
            >
              {initial}
            </span>
            <span className="hidden pr-1 text-[13px] leading-tight sm:block">
              <span className="block font-medium text-ink">{user.displayName}</span>
              <span className="block text-[11px] text-ink-3">{user.role}</span>
            </span>
            <button
              type="button"
              onClick={logout}
              className="rounded-full px-2.5 py-1 text-[12px] text-ink-3 transition-colors hover:bg-surface hover:text-ink-2"
            >
              退出
            </button>
          </div>
        ) : null}
      </div>
    </header>
  );
}
