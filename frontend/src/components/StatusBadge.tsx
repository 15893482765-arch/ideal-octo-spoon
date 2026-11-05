/** 通用状态徽章（PRD §1.3 设计令牌：emerald/amber/rose/sky/slate）。
 *
 * UI 迭代：去掉 ring 描边，改为更柔和的浅底 + 圆角胶囊，字号 11px。
 * `data-tone` / `data-testid` 保留（测试与业务样式钩子依赖）。
 */
import { cn } from "../lib/utils";

export type StatusTone = "success" | "warning" | "danger" | "info" | "neutral";

const toneClass: Record<StatusTone, string> = {
  success: "bg-emerald-50 text-emerald-700",
  warning: "bg-amber-50 text-amber-700",
  danger: "bg-rose-50 text-rose-700",
  info: "bg-sky-50 text-sky-700",
  neutral: "bg-page text-ink-3",
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
        "inline-flex items-center rounded-full px-2.5 py-[3px] text-[11px] font-medium whitespace-nowrap",
        toneClass[tone],
        className,
      )}
    >
      {label}
    </span>
  );
}
