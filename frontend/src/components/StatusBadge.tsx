/** 通用状态徽章（PRD §1.3 设计令牌：emerald/amber/rose/sky/slate）。 */
import { cn } from "../lib/utils";

export type StatusTone = "success" | "warning" | "danger" | "info" | "neutral";

const toneClass: Record<StatusTone, string> = {
  success: "bg-emerald-50 text-emerald-700 ring-emerald-200",
  warning: "bg-amber-50 text-amber-700 ring-amber-200",
  danger: "bg-rose-50 text-rose-700 ring-rose-200",
  info: "bg-sky-50 text-sky-700 ring-sky-200",
  neutral: "bg-slate-100 text-slate-600 ring-slate-200",
};

export interface StatusBadgeProps {
  tone?: StatusTone;
  label: string;
  className?: string;
}

export function StatusBadge({ tone = "neutral", label, className }: StatusBadgeProps) {
  return (
    <span
      data-testid="status-badge"
      data-tone={tone}
      className={cn(
        "inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium ring-1 ring-inset",
        toneClass[tone],
        className,
      )}
    >
      {label}
    </span>
  );
}
