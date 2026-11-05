/** 通用空态组件（T06 / PRD §3.1 "带下一步的空态"）。
 *
 * UI 迭代：虚线框改浅色描边 + 插画位圆角容器，标题 15px。
 */
import type { ReactNode } from "react";

import { cn } from "../lib/utils";

export interface EmptyStateProps {
  title: string;
  description?: string;
  /** "下一步"提示：业务页面占位时显式指向后续任务卡。 */
  nextStep?: string;
  action?: ReactNode;
  className?: string;
}

export function EmptyState({ title, description, nextStep, action, className }: EmptyStateProps) {
  return (
    <div
      role="status"
      className={cn(
        "rounded-[16px] bg-surface p-10 text-center shadow-[inset_0_0_0_1px_var(--color-line)]",
        className,
      )}
    >
      <div className="mx-auto mb-3 grid h-11 w-11 place-items-center rounded-full bg-brand-soft text-brand">
        <svg
          aria-hidden="true"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth={1.6}
          strokeLinecap="round"
          strokeLinejoin="round"
          className="h-5 w-5"
        >
          <path d="M4 7.5h16v10.5H4zM4 7.5 12 13l8-5.5" />
        </svg>
      </div>
      <h3 className="qs-card-title">{title}</h3>
      {description ? <p className="qs-body mt-2">{description}</p> : null}
      {nextStep ? <p className="qs-hint mt-2">下一步：{nextStep}</p> : null}
      {action ? <div className="mt-4 flex justify-center">{action}</div> : null}
    </div>
  );
}
