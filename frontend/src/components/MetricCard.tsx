/** 通用指标卡（占位实现：T19 接入 MetricsService）。 */
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
  up: "text-emerald-600",
  down: "text-rose-600",
  flat: "text-slate-500",
};

export function MetricCard({ label, value, unit, delta, className }: MetricCardProps) {
  return (
    <div
      role="group"
      aria-label={label}
      className={cn(
        "rounded-lg border border-slate-200 bg-white p-4 shadow-sm",
        className,
      )}
    >
      <div className="text-xs tracking-wide text-slate-500 uppercase">{label}</div>
      <div className="mt-1 flex items-baseline gap-1">
        <span className="text-2xl font-semibold text-slate-800">{value}</span>
        {unit ? <span className="text-sm text-slate-500">{unit}</span> : null}
      </div>
      {delta ? (
        <div className={cn("mt-1 text-xs", deltaTone[delta.tone])}>{delta.value}</div>
      ) : null}
    </div>
  );
}
