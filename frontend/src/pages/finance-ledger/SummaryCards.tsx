/** 财务总账汇总卡：收入 / 支出 / 净利润（数字优先，净利润按正负着色）。 */
import { cn } from "../../lib/utils";
import { fenToYuan } from "../../lib/format";
import type { LedgerSummary } from "./types";

interface CardProps {
  label: string;
  valueFen: number;
  hint: string;
  tone?: "income" | "expense" | "neutral" | "profit" | "loss";
}

const toneClass: Record<NonNullable<CardProps["tone"]>, string> = {
  income: "text-emerald-700",
  expense: "text-rose-700",
  neutral: "text-slate-800",
  profit: "text-emerald-700",
  loss: "text-rose-700",
};

const accentClass: Record<NonNullable<CardProps["tone"]>, string> = {
  income: "from-emerald-500/10",
  expense: "from-rose-500/10",
  neutral: "from-slate-400/10",
  profit: "from-emerald-500/15",
  loss: "from-rose-500/15",
};

function SummaryCard({ label, valueFen, hint, tone = "neutral" }: CardProps) {
  return (
    <div className="relative overflow-hidden rounded-xl border border-slate-200 bg-white p-5 shadow-sm transition-shadow hover:shadow-md">
      <div
        className={cn(
          "pointer-events-none absolute inset-x-0 top-0 h-16 bg-gradient-to-b to-transparent",
          accentClass[tone],
        )}
        aria-hidden
      />
      <div className="relative">
        <div className="text-xs font-medium tracking-wide text-slate-500 uppercase">{label}</div>
        <div className={cn("mt-2 flex items-baseline gap-1.5", toneClass[tone])}>
          <span className="text-3xl font-semibold tabular-nums">{fenToYuan(valueFen)}</span>
          <span className="text-sm text-slate-500">元</span>
        </div>
        <div className="mt-2 text-xs text-slate-400">{hint}</div>
      </div>
    </div>
  );
}

export function SummaryCards({
  summary,
  rangeLabel,
}: {
  summary: LedgerSummary;
  rangeLabel: string;
}) {
  const netTone = summary.net_fen >= 0 ? "profit" : "loss";
  return (
    <div className="grid grid-cols-1 gap-3 md:grid-cols-3">
      <SummaryCard
        label="收入"
        valueFen={summary.income_fen}
        hint={`${rangeLabel} · 送达确认等线下收款`}
        tone="income"
      />
      <SummaryCard
        label="支出"
        valueFen={summary.expense_fen}
        hint={`${rangeLabel} · 进货/工资/费用/资产`}
        tone="expense"
      />
      <SummaryCard
        label="净利润"
        valueFen={summary.net_fen}
        hint={`${rangeLabel} · 收入 − 支出${
          summary.net_fen >= 0 ? "" : "（当前为净流出）"
        }`}
        tone={netTone}
      />
    </div>
  );
}
