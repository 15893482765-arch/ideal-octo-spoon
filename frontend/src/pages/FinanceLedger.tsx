/** 财务总账（T35）：收入/支出/净利润汇总 + 分桶趋势 + 公司物品采购 + 流水明细。
 *
 * 数据唯一来源是流水表（`finance_transactions`）：
 * - `GET /api/finance/ledger/summary`（按月 12 桶 / 按年近 5 年）；
 * - `GET /api/finance/ledger/transactions`（类型/来源/区间筛选）；
 * - 公司物品采购：凭证上传 → 待审批 → 审批通过自动进支出流水。
 *
 * 红线：交易全部线下完成，系统只记账，不提供任何在线支付入口。
 */
import { useMemo, useState } from "react";

import { ErrorState, LoadingBlock, SessionNotice } from "../components/AsyncState";
import { PageHeader } from "../components/PageHeader";
import { ToastStack } from "../components/Toast";
import { useAuth } from "../lib/auth";
import { todayInTz } from "../lib/format";
import { useApiQuery } from "../lib/useApi";
import { useToast } from "../lib/useToast";
import { CompanyPurchasePanel } from "./finance-ledger/CompanyPurchasePanel";
import { LedgerTrend } from "./finance-ledger/LedgerTrend";
import { SummaryCards } from "./finance-ledger/SummaryCards";
import { TransactionsPanel } from "./finance-ledger/TransactionsPanel";
import {
  EMPTY_TX_FILTERS,
  RELATION_LABELS,
  TX_TYPE_LABELS,
  type Granularity,
  type LedgerSummary,
  type LedgerTransaction,
  type TxFilters,
} from "./finance-ledger/types";

const controlClass =
  "rounded-md border border-slate-300 bg-white px-2.5 py-1.5 text-sm text-slate-700 " +
  "outline-none transition focus:border-teal-600 focus:ring-2 focus:ring-teal-600/20";

function lastDayOfMonth(year: number, month: number): string {
  const d = new Date(Date.UTC(year, month, 0));
  return d.toISOString().slice(0, 10);
}

function csvCell(value: string): string {
  return /[",\n]/.test(value) ? `"${value.replace(/"/g, '""')}"` : value;
}

export function FinanceLedger() {
  const { authenticated } = useAuth();
  const { items: toasts, push, dismiss } = useToast();

  const today = todayInTz();
  const [granularity, setGranularity] = useState<Granularity>("month");
  const [year, setYear] = useState(Number(today.slice(0, 4)));
  const [month, setMonth] = useState(Number(today.slice(5, 7)));
  const [refreshKey, setRefreshKey] = useState(0);
  const [filters, setFilters] = useState<TxFilters>(EMPTY_TX_FILTERS);

  const periodFrom =
    granularity === "month" ? `${year}-${String(month).padStart(2, "0")}-01` : `${year}-01-01`;
  const periodTo =
    granularity === "month" ? lastDayOfMonth(year, month) : `${year}-12-31`;

  const summaryQuery = useApiQuery<LedgerSummary>("/finance/ledger/summary", {
    granularity,
    year,
    month: granularity === "month" ? month : undefined,
    _r: refreshKey,
  });

  const txQuery = useApiQuery<LedgerTransaction[]>("/finance/ledger/transactions", {
    tx_type: filters.tx_type,
    source_kind: filters.source_kind,
    date_from: filters.date_from || periodFrom,
    date_to: filters.date_to || periodTo,
    limit: 200,
    _r: refreshKey,
  });

  const yearOptions = useMemo(
    () => Array.from({ length: 6 }, (_, i) => Number(today.slice(0, 4)) - 4 + i),
    [today],
  );

  const rows = txQuery.data ?? [];
  const rangeLabel =
    granularity === "month" ? `${year} 年 ${month} 月` : `${year} 年度`;

  const refreshAll = () => setRefreshKey((n) => n + 1);

  const exportCsv = () => {
    if (rows.length === 0) {
      push("当前筛选暂无流水可导出", "warning");
      return;
    }
    const header = ["日期", "类型", "来源", "金额(元)", "关联单据", "备注", "经办人"];
    const body = rows.map((row) =>
      [
        row.biz_date,
        TX_TYPE_LABELS[row.tx_type] ?? row.tx_type,
        row.source_label,
        (row.amount_fen / 100).toFixed(2),
        RELATION_LABELS[row.related_object_type] ?? row.related_object_type,
        row.note ?? "",
        row.bookkeeper_name ?? "",
      ]
        .map((cell) => csvCell(String(cell)))
        .join(","),
    );
    const blob = new Blob(["\uFEFF" + [header.join(","), ...body].join("\n")], {
      type: "text/csv;charset=utf-8",
    });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = `财务流水_${periodFrom}_${periodTo}.csv`;
    anchor.click();
    URL.revokeObjectURL(url);
    push("已导出当前筛选的流水明细", "success");
  };

  if (!authenticated) {
    return (
      <div className="space-y-4">
        <PageHeader title="财务总账" description="收入、支出、净利润与公司物品采购" />
        <SessionNotice hint="财务总账需要财务或管理员会话（服务端按 finance 能力二次鉴权）。" />
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <PageHeader
        title="财务总账"
        description="交易全部线下完成，系统只记流水：收入、支出、调整逐笔可回溯到业务单据"
        actions={
          <>
            <button
              type="button"
              onClick={exportCsv}
              className="rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm text-slate-700 transition hover:bg-slate-50"
            >
              导出流水
            </button>
            <button
              type="button"
              onClick={refreshAll}
              className="rounded-md bg-teal-700 px-3 py-1.5 text-sm font-medium text-white transition hover:bg-teal-800"
            >
              刷新数据
            </button>
          </>
        }
      />

      <div className="flex flex-wrap items-end gap-3 rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
        <div className="flex rounded-md border border-slate-300 p-0.5">
          {(["month", "year"] as const).map((value) => (
            <button
              key={value}
              type="button"
              onClick={() => setGranularity(value)}
              className={`rounded px-3 py-1.5 text-sm transition ${
                granularity === value
                  ? "bg-teal-700 font-medium text-white"
                  : "text-slate-600 hover:bg-slate-100"
              }`}
            >
              {value === "month" ? "按月" : "按年"}
            </button>
          ))}
        </div>

        <label className="flex flex-col gap-1 text-xs text-slate-600">
          <span>年份</span>
          <select
            value={year}
            onChange={(e) => setYear(Number(e.target.value))}
            className={controlClass}
          >
            {yearOptions.map((y) => (
              <option key={y} value={y}>
                {y} 年
              </option>
            ))}
          </select>
        </label>

        {granularity === "month" ? (
          <label className="flex flex-col gap-1 text-xs text-slate-600">
            <span>月份</span>
            <select
              value={month}
              onChange={(e) => setMonth(Number(e.target.value))}
              className={controlClass}
            >
              {Array.from({ length: 12 }, (_, i) => i + 1).map((m) => (
                <option key={m} value={m}>
                  {m} 月
                </option>
              ))}
            </select>
          </label>
        ) : null}

        <span className="ml-auto text-xs text-slate-400">
          {granularity === "month"
            ? "按月统计：12 个月分桶，明细默认取本月"
            : "按年统计：近 5 年分桶，明细默认取本年"}
        </span>
      </div>

      {summaryQuery.loading ? <LoadingBlock text="汇总财务流水…" /> : null}
      {summaryQuery.error ? (
        <ErrorState
          message={summaryQuery.error}
          status={summaryQuery.status}
          onRetry={summaryQuery.refetch}
        />
      ) : null}
      {!summaryQuery.loading && !summaryQuery.error && summaryQuery.data ? (
        <>
          <SummaryCards summary={summaryQuery.data} rangeLabel={rangeLabel} />
          <LedgerTrend
            buckets={summaryQuery.data.buckets}
            granularity={summaryQuery.data.granularity}
          />
        </>
      ) : null}

      <CompanyPurchasePanel
        refreshKey={refreshKey}
        onChanged={() => {
          summaryQuery.refetch();
          txQuery.refetch();
        }}
        onToast={push}
      />

      <TransactionsPanel
        filters={filters}
        onFiltersChange={setFilters}
        rows={rows}
        loading={txQuery.loading}
        error={txQuery.error}
        status={txQuery.status}
        onRetry={txQuery.refetch}
      />

      <ToastStack items={toasts} onDismiss={dismiss} />
    </div>
  );
}
