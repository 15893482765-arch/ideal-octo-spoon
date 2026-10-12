/** 通用空态组件（T06 / PRD §3.1 "带下一步的空态"）。 */
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
        "rounded-lg border border-dashed border-slate-300 bg-white p-8 text-center",
        className,
      )}
    >
      <h3 className="text-base font-semibold text-slate-700">{title}</h3>
      {description ? <p className="mt-2 text-sm text-slate-500">{description}</p> : null}
      {nextStep ? (
        <p className="mt-3 text-xs text-slate-400">下一步：{nextStep}</p>
      ) : null}
      {action ? <div className="mt-4 flex justify-center">{action}</div> : null}
    </div>
  );
}
