/** 通用筛选条（占位实现：仅提供可访问的表单布局，业务卡接入时挂接受控状态）。
 *
 * UI 迭代：改白卡容器、按钮统一为圆角胶囊（主按钮紫、次按钮描边），
 * 表单 onSubmit / onReset / onApply 回调完全未动。
 */
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
      className={cn("qs-card flex flex-wrap items-end gap-3 p-4", className)}
    >
      <div className="flex flex-1 flex-wrap items-end gap-3">{children}</div>
      <div className="flex gap-2">
        <button type="button" onClick={onReset} className="qs-btn qs-btn-ghost">
          重置
        </button>
        <button type="submit" className="qs-btn qs-btn-primary">
          应用
        </button>
      </div>
    </form>
  );
}
