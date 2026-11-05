/** 通用指标卡（占位实现：待 T21 接入 MetricsService）。
 *
 * UI 迭代：改白卡 + 大圆角 + 柔和阴影，数值加大到 30px，涨跌用胶囊标签。
 * props 与 role/aria-label 未变。
 */
import type { ReactNode } from "react";

import { cn } from "../lib/utils";

export interface MetricCardProps {
  label: string;
  value: ReactNode;
  unit?: string;
  delta?: { value: string; tone: "up" | "down" | "flat" };
  className?: string;
}

const deltaTone: Record<NonNullable<MetricCardProps["delta"]>["tone"], string> = {
  up: "bg-emerald-50 text-emerald-600",
  down: "bg-rose-50 text-rose-600",
  flat: "bg-page text-ink-3",
};

export function MetricCard({ label, value, unit, delta, className }: MetricCardProps) {
  return (
    <div
      role="group"
      aria-label={label}
      className={cn("qs-card qs-card-pad", className)}
    >
      <div className="text-[12px] font-medium tracking-wide text-ink-3">{label}</div>
      <div className="mt-2 flex items-end gap-1.5">
        <span className="text-[28px] leading-8 font-semibold tracking-tight text-ink">
          {value}
        </span>
        {unit ? <span className="pb-1 text-[12px] text-ink-3">{unit}</span> : null}
      </div>
      {delta ? (
        <div
          className={cn(
            "mt-2 inline-flex items-center rounded-full px-2 py-0.5 text-[11px] font-medium",
            deltaTone[delta.tone],
          )}
        >
          {delta.value}
        </div>
      ) : null}
    </div>
  );
}
