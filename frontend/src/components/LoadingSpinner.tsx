/** 通用加载指示器。 */
import { cn } from "../lib/utils";

export interface LoadingSpinnerProps {
  label?: string;
  className?: string;
}

export function LoadingSpinner({ label = "加载中…", className }: LoadingSpinnerProps) {
  return (
    <div
      role="status"
      aria-live="polite"
      className={cn("inline-flex items-center gap-2 text-sm text-slate-500", className)}
    >
      <span
        aria-hidden="true"
        className="inline-block h-4 w-4 animate-spin rounded-full border-2 border-slate-300 border-t-[var(--color-brand)]"
      />
      <span>{label}</span>
    </div>
  );
}
