/** 流水明细：类型/来源/日期区间筛选 + 逐笔清单（金额按收/支着色）。 */
import { EmptyState } from "../../components/EmptyState";
import { ErrorState, LoadingBlock } from "../../components/AsyncState";
import { StatusBadge } from "../../components/StatusBadge";
import { formatDate, money } from "../../lib/format";
import {
  RELATION_LABELS,
  SOURCE_OPTIONS,
  TX_TYPE_LABELS,
  TX_TYPE_TONES,
  type LedgerTransaction,
  type TxFilters,
} from "./types";

const controlClass =
  "rounded-md border border-slate-300 bg-white px-2 py-1.5 text-sm text-slate-700 " +
  "outline-none transition focus:border-teal-600 focus:ring-2 focus:ring-teal-600/20";

export function TransactionsPanel({
  filters,
  onFiltersChange,
  rows,
  loading,
  error,
  status,
  onRetry,
}: {
  filters: TxFilters;
  onFiltersChange: (next: TxFilters) => void;
  rows: LedgerTransaction[];
  loading: boolean;
  error: string;
  status: number | null;
  onRetry: () => void;
}) {
  const patch = (next: Partial<TxFilters>) => onFiltersChange({ ...filters, ...next });

  return (
    <section className="rounded-xl border border-slate-200 bg-white p-5 shadow-sm">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 className="text-sm font-semibold text-slate-700">流水明细</h2>
        <span className="text-xs text-slate-400">
          共 {rows.length} 笔 · 收入/支出以带符号金额记入，净额为代数和
        </span>
      </div>

      <div className="mt-3 flex flex-wrap items-end gap-3">
        <label className="flex flex-col gap-1 text-xs text-slate-600">
          <span>类型</span>
          <select
            value={filters.tx_type}
            onChange={(e) => patch({ tx_type: e.target.value })}
            className={controlClass}
          >
            <option value="">全部</option>
            {Object.entries(TX_TYPE_LABELS).map(([value, text]) => (
              <option key={value} value={value}>
                {text}
              </option>
            ))}
          </select>
        </label>
        <label className="flex flex-col gap-1 text-xs text-slate-600">
          <span>来源</span>
          <select
            value={filters.source_kind}
            onChange={(e) => patch({ source_kind: e.target.value })}
            className={controlClass}
          >
            <option value="">全部</option>
            {SOURCE_OPTIONS.map(([value, text]) => (
              <option key={value} value={value}>
                {text}
              </option>
            ))}
          </select>
        </label>
        <label className="flex flex-col gap-1 text-xs text-slate-600">
          <span>开始日期</span>
          <input
            type="date"
            value={filters.date_from}
            onChange={(e) => patch({ date_from: e.target.value })}
            className={controlClass}
          />
        </label>
        <label className="flex flex-col gap-1 text-xs text-slate-600">
          <span>结束日期</span>
          <input
            type="date"
            value={filters.date_to}
            onChange={(e) => patch({ date_to: e.target.value })}
            className={controlClass}
          />
        </label>
        <button
          type="button"
          onClick={() => onFiltersChange({ tx_type: "", source_kind: "", date_from: "", date_to: "" })}
          className="rounded-md border border-slate-300 px-3 py-1.5 text-sm text-slate-600 transition hover:bg-slate-50"
        >
          重置筛选
        </button>
      </div>

      <div className="mt-4">
        {loading ? <LoadingBlock text="加载流水…" /> : null}
        {error ? <ErrorState message={error} status={status} onRetry={onRetry} /> : null}
        {!loading && !error ? (
          rows.length === 0 ? (
            <EmptyState
              title="本月暂无流水"
              description="送达确认、采购收货、工资发放、费用审批通过会自动生成流水；也可在上方录入公司物品采购。"
            />
          ) : (
            <div className="overflow-x-auto">
              <table className="min-w-full text-sm">
                <thead>
                  <tr className="border-b border-slate-200 text-left text-xs text-slate-500">
                    <th className="py-2 pr-3 font-medium">日期</th>
                    <th className="py-2 pr-3 font-medium">类型</th>
                    <th className="py-2 pr-3 font-medium">来源</th>
                    <th className="py-2 pr-3 text-right font-medium">金额</th>
                    <th className="py-2 pr-3 font-medium">关联单据</th>
                    <th className="py-2 pr-3 font-medium">备注</th>
                    <th className="py-2 font-medium">经办人</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((row) => (
                    <tr key={row.id} className="border-b border-slate-100 hover:bg-slate-50/60">
                      <td className="py-2 pr-3 whitespace-nowrap text-slate-600">
                        {formatDate(row.biz_date)}
                      </td>
                      <td className="py-2 pr-3">
                        <StatusBadge
                          tone={TX_TYPE_TONES[row.tx_type] ?? "neutral"}
                          label={TX_TYPE_LABELS[row.tx_type] ?? row.tx_type}
                        />
                      </td>
                      <td className="py-2 pr-3 whitespace-nowrap text-slate-600">
                        {row.source_label}
                      </td>
                      <td
                        className={`py-2 pr-3 text-right font-medium tabular-nums ${
                          row.amount_fen < 0 ? "text-rose-700" : "text-emerald-700"
                        }`}
                      >
                        {money(row.amount_fen)}
                      </td>
                      <td className="py-2 pr-3 text-xs text-slate-500">
                        <div>{RELATION_LABELS[row.related_object_type] ?? row.related_object_type}</div>
                        <div className="font-mono text-[10px] text-slate-400">
                          {row.related_object_id.slice(0, 8)}
                        </div>
                      </td>
                      <td className="max-w-[240px] truncate py-2 pr-3 text-slate-600" title={row.note ?? ""}>
                        {row.note ?? "—"}
                      </td>
                      <td className="py-2 whitespace-nowrap text-slate-600">
                        {row.bookkeeper_name ?? "—"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )
        ) : null}
      </div>
    </section>
  );
}
