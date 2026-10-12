/** 通用筛选条（占位实现：仅提供可访问的表单布局，业务卡接入时挂接受控状态）。 */
import type { ReactNode } from "react";

import { cn } from "../lib/utils";

export interface FilterBarProps {
  children: ReactNode;
  onReset?: () => void;
  onApply?: () => void;
  className?: string;
}

export function FilterBar({ children, onReset, onApply, className }: FilterBarProps) {
  return (
    <form
      role="search"
      onSubmit={(e) => {
        e.preventDefault();
        onApply?.();
      }}
      className={cn(
        "flex flex-wrap items-end gap-3 rounded-md border border-slate-200 bg-white p-3",
        className,
      )}
    >
      <div className="flex flex-1 flex-wrap items-end gap-3">{children}</div>
      <div className="flex gap-2">
        <button
          type="button"
          onClick={onReset}
          className="rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm text-slate-600 hover:bg-slate-50"
        >
          重置
        </button>
        <button
          type="submit"
          className="rounded-md bg-[var(--color-brand)] px-3 py-1.5 text-sm font-medium text-white hover:opacity-90"
        >
          应用
        </button>
      </div>
    </form>
  );
}
