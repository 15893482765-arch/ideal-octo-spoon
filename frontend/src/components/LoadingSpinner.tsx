/** 通用加载指示器。
 *
 * UI 迭代：轨道改浅紫，进度弧用主色；尺寸与 aria 语义不变。
 */
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
      className={cn("inline-flex items-center gap-2 text-[13px] text-ink-3", className)}
    >
      <span
        aria-hidden="true"
        className="inline-block h-4 w-4 animate-spin rounded-full border-2 border-brand-soft border-t-[var(--color-brand)]"
      />
      <span>{label}</span>
    </div>
  );
}
