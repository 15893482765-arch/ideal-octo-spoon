/** 收支趋势：按月 12 根双色柱，按年近 5 年横条（纯 CSS，无图表库依赖）。 */
import { fenToYuan, money } from "../../lib/format";
import type { Granularity, LedgerBucket } from "./types";

function maxOf(buckets: LedgerBucket[]): number {
  return Math.max(1, ...buckets.flatMap((b) => [b.income_fen, b.expense_fen]));
}

export function LedgerTrend({
  buckets,
  granularity,
}: {
  buckets: LedgerBucket[];
  granularity: Granularity;
}) {
  const max = maxOf(buckets);
  const empty = buckets.every((b) => b.income_fen === 0 && b.expense_fen === 0);

  return (
    <section className="rounded-xl border border-slate-200 bg-white p-5 shadow-sm">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 className="text-sm font-semibold text-slate-700">
          {granularity === "month" ? "月度收支趋势" : "年度收支对比（近 5 年）"}
        </h2>
        <div className="flex items-center gap-3 text-xs text-slate-500">
          <span className="inline-flex items-center gap-1">
            <span className="h-2 w-2 rounded-sm bg-teal-600" aria-hidden />
            收入
          </span>
          <span className="inline-flex items-center gap-1">
            <span className="h-2 w-2 rounded-sm bg-rose-500" aria-hidden />
            支出
          </span>
        </div>
      </div>

      {empty ? (
        <p className="mt-6 text-center text-xs text-slate-400">
          统计区间内暂无流水；发生送达确认、采购收货或费用审批后自动汇总。
        </p>
      ) : granularity === "month" ? (
        <div className="mt-4 flex items-end gap-2">
          {buckets.map((b) => (
            <div key={b.period} className="flex min-w-0 flex-1 flex-col items-center gap-1">
              <div className="flex h-32 w-full items-end justify-center gap-1">
                <div
                  title={`${b.period} 收入 ${money(b.income_fen)}`}
                  style={{ height: `${Math.max(2, (b.income_fen / max) * 100)}%` }}
                  className="w-2.5 rounded-t bg-teal-600/85 transition-all hover:bg-teal-600"
                />
                <div
                  title={`${b.period} 支出 ${money(b.expense_fen)}`}
                  style={{ height: `${Math.max(2, (b.expense_fen / max) * 100)}%` }}
                  className="w-2.5 rounded-t bg-rose-500/80 transition-all hover:bg-rose-500"
                />
              </div>
              <span className="text-[10px] text-slate-400">{b.period.slice(5)}月</span>
            </div>
          ))}
        </div>
      ) : (
        <div className="mt-4 space-y-3">
          {buckets.map((b) => (
            <div key={b.period} className="flex items-center gap-3">
              <span className="w-12 shrink-0 text-xs text-slate-500">{b.period}</span>
              <div className="flex-1 space-y-1">
                <div className="h-2 rounded-full bg-slate-100">
                  <div
                    title={`${b.period} 收入 ${money(b.income_fen)}`}
                    style={{ width: `${Math.max(1, (b.income_fen / max) * 100)}%` }}
                    className="h-2 rounded-full bg-teal-600/85"
                  />
                </div>
                <div className="h-2 rounded-full bg-slate-100">
                  <div
                    title={`${b.period} 支出 ${money(b.expense_fen)}`}
                    style={{ width: `${Math.max(1, (b.expense_fen / max) * 100)}%` }}
                    className="h-2 rounded-full bg-rose-500/80"
                  />
                </div>
              </div>
              <span className="w-28 shrink-0 text-right text-xs tabular-nums text-slate-500">
                {fenToYuan(b.net_fen)}
              </span>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}
