/** 经营损益（T21 前端联调 / PRD §1.5、§4.10、R22、R34）。
 *
 * 接入后端（全部显式传 `scope=PRODUCTION`，缺省即 422/被拒，R34）：
 * - `GET /api/reporting/metrics?period=&scope=`（MetricsService 唯一口径，含月度净利润）；
 * - `GET /api/reporting/reports/{report_key}?period=&region_id=&brand_id=&scope=`（损益构成明细）；
 * - 导出复用共享组件 `ExportActions` / `ExportJobList`。
 *
 * 红线：本页不做任何口径重算，只展示 MetricsService/ExportService 的结果；
 * 每张指标卡必须同时展示口径（definition）与数据截至时间（as_of），
 * 依赖事实缺失时标注「数据不足，暂不判定」，不得默认当作 0。
 */
import { useState } from "react";

import { useAuth } from "../lib/auth";
import { currentPeriodMonth, formatDateTime, money } from "../lib/format";
import { useApiQuery } from "../lib/useApi";
import { useToast } from "../lib/useToast";
import { ErrorState, LoadingBlock, SessionNotice } from "../components/AsyncState";
import { EmptyState } from "../components/EmptyState";
import {
  EXPORT_REPORTS,
  ExportActions,
  ExportJobList,
  REPORT_SCOPE,
} from "../components/ExportPanel";
import { FilterBar } from "../components/FilterBar";
import { MetricCard } from "../components/MetricCard";
import { PageHeader } from "../components/PageHeader";
import { ReportTable } from "../components/ReportTable";
import { StatusBadge } from "../components/StatusBadge";
import { ToastStack } from "../components/Toast";
import type { ToastTone } from "../components/Toast";

const PROFIT_METRIC = "MONTHLY_NET_PROFIT";

/** 损益构成明细：报表键（与后端 ReportKey 对齐）+ 中文名。 */
const DETAIL_REPORTS = [
  ["SALES", "销售收入（已确认销量）"],
  ["PROCUREMENT", "进货成本（采购单）"],
  ["EXPENSES", "费用（活动/冰柜/其他）"],
  ["PERFORMANCE", "绩效工资（工资单）"],
] as const;

type DetailReportKey = (typeof DETAIL_REPORTS)[number][0];

interface MetricRow {
  key: string;
  name: string;
  value: number | string | null;
  unit: string;
  definition: string;
  as_of: string;
  period: string;
  filters: Record<string, unknown>;
  data_complete: boolean;
  note: string | null;
  source_refs: string[];
}

interface ReportRows {
  report_key: string;
  columns: string[];
  rows: Record<string, unknown>[];
  row_count: number;
}

interface OptionItem {
  id: string;
  code: string;
  name: string;
}

interface AppliedFilters {
  period: string;
  region_id: string;
  brand_id: string;
}

/** 指标值展示：分→元、% 直接带符号、缺失值明确标注「数据不足」。 */
function metricDisplay(m: MetricRow): string {
  if (m.value === null || m.value === undefined || m.value === "") {
    return "数据不足";
  }
  if (m.unit === "分") return money(Number(m.value));
  if (m.unit === "%") return `${m.value}%`;
  return String(m.value);
}

export function Profit() {
  const { authenticated, user } = useAuth();
  const { items: toasts, push, dismiss } = useToast();

  const [periodInput, setPeriodInput] = useState(currentPeriodMonth());
  const [regionInput, setRegionInput] = useState("");
  const [brandInput, setBrandInput] = useState("");
  const [applied, setApplied] = useState<AppliedFilters>({
    period: currentPeriodMonth(),
    region_id: "",
    brand_id: "",
  });

  const role = user?.role;
  // 后端 MODULE_REPORT（report.export_request）：经理 / 财务 / 管理员
  const canExport =
    role === "REGIONAL_MANAGER" || role === "FINANCE" || role === "SYSTEM_ADMIN";

  if (!authenticated) {
    return (
      <div className="space-y-4">
        <PageHeader title="经营损益" description="指标、损益构成与报表导出" />
        <SessionNotice hint="经营指标与报表需要真实登录后的后端会话（服务端按角色二次鉴权）。" />
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <PageHeader
        title="经营损益"
        description="口径唯一来自 MetricsService；本页显式使用 PRODUCTION 数据域，展示口径与数据截至时间"
      />

      <FilterBar
        onApply={() => setApplied({ period: periodInput, region_id: regionInput, brand_id: brandInput })}
        onReset={() => {
          const now = currentPeriodMonth();
          setPeriodInput(now);
          setRegionInput("");
          setBrandInput("");
          setApplied({ period: now, region_id: "", brand_id: "" });
        }}
      >
        <PeriodRegionBrandFields
          periodInput={periodInput}
          regionInput={regionInput}
          brandInput={brandInput}
          onPeriodChange={setPeriodInput}
          onRegionChange={setRegionInput}
          onBrandChange={setBrandInput}
        />
      </FilterBar>

      {!applied.period ? (
        <EmptyState
          title="请先选择统计周期"
          description="指标与损益报表必须指定统计周期（YYYY-MM 或 YYYY-Www）。"
        />
      ) : (
        <>
          <MetricsSection applied={applied} />
          <TrendSection />
          <DetailSection applied={applied} onToast={push} />
          <ExportSection applied={applied} canExport={canExport} onToast={push} />
        </>
      )}

      <ToastStack items={toasts} onDismiss={dismiss} />
    </div>
  );
}

function Field({ label: text, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="flex flex-col gap-1 text-xs text-slate-600">
      <span>{text}</span>
      {children}
    </label>
  );
}

function PeriodRegionBrandFields({
  periodInput,
  regionInput,
  brandInput,
  onPeriodChange,
  onRegionChange,
  onBrandChange,
}: {
  periodInput: string;
  regionInput: string;
  brandInput: string;
  onPeriodChange: (v: string) => void;
  onRegionChange: (v: string) => void;
  onBrandChange: (v: string) => void;
}) {
  const regions = useApiQuery<{ items: OptionItem[] }>("/master-data/regions");
  const brands = useApiQuery<{ items: OptionItem[] }>("/master-data/brands");

  return (
    <>
      <Field label="统计周期 *">
        <input
          type="month"
          required
          value={periodInput}
          onChange={(e) => onPeriodChange(e.target.value)}
          className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
        />
      </Field>
      <Field label="片区（明细筛选）">
        <select
          value={regionInput}
          onChange={(e) => onRegionChange(e.target.value)}
          className="w-40 rounded-md border border-slate-300 bg-white px-2 py-1.5 text-sm"
        >
          <option value="">全部</option>
          {(regions.data?.items ?? []).map((r) => (
            <option key={r.id} value={r.id}>
              {r.name}
            </option>
          ))}
        </select>
      </Field>
      <Field label="品牌（明细筛选）">
        <select
          value={brandInput}
          onChange={(e) => onBrandChange(e.target.value)}
          className="w-40 rounded-md border border-slate-300 bg-white px-2 py-1.5 text-sm"
        >
          <option value="">全部</option>
          {(brands.data?.items ?? []).map((b) => (
            <option key={b.id} value={b.id}>
              {b.name}
            </option>
          ))}
        </select>
      </Field>
    </>
  );
}

// ============================== 指标区 ==============================

function MetricsSection({ applied }: { applied: AppliedFilters }) {
  const metrics = useApiQuery<MetricRow[]>(
    "/reporting/metrics",
    { period: applied.period, scope: REPORT_SCOPE },
    { enabled: Boolean(applied.period) },
  );

  const rows = metrics.data ?? [];
  const profit = rows.find((m) => m.key === PROFIT_METRIC) ?? null;
  const others = rows.filter((m) => m.key !== PROFIT_METRIC);

  return (
    <section className="space-y-3 rounded-md border border-slate-200 bg-white p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 className="text-sm font-semibold text-slate-700">
          经营指标（{applied.period} · {REPORT_SCOPE}）
        </h2>
        <span className="text-xs text-slate-400">
          {rows.length > 0 ? `数据截至 ${formatDateTime(rows[0].as_of)}` : ""}
        </span>
      </div>

      {metrics.loading ? <LoadingBlock text="加载经营指标…" /> : null}
      {metrics.error ? (
        <ErrorState message={metrics.error} status={metrics.status} onRetry={metrics.refetch} />
      ) : null}

      {!metrics.loading && !metrics.error ? (
        <>
          {profit ? (
            <div className="rounded-md border border-slate-200 bg-slate-50 p-3">
              <MetricCard
                label={profit.name}
                value={metricDisplay(profit)}
                delta={{
                  value: profit.data_complete ? "口径已确认" : "数据不足，暂不判定",
                  tone: profit.data_complete ? "flat" : "down",
                }}
              />
              <div className="mt-2 space-y-1 text-xs text-slate-500">
                <div>口径：{profit.definition}</div>
                <div>数据截至：{formatDateTime(profit.as_of)}</div>
                <div>来源：{(profit.source_refs ?? []).join("、") || "—"}</div>
                {profit.note ? <div className="text-amber-700">{profit.note}</div> : null}
              </div>
            </div>
          ) : null}

          <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
            {others.map((m) => (
              <div key={m.key} className="rounded-md border border-slate-200 p-3">
                <div className="flex items-baseline justify-between gap-2">
                  <span className="text-xs tracking-wide text-slate-500 uppercase">{m.name}</span>
                  {m.data_complete ? null : <StatusBadge tone="warning" label="数据不足" />}
                </div>
                <div className="mt-1 flex items-baseline gap-1">
                  <span className="text-xl font-semibold text-slate-800">{metricDisplay(m)}</span>
                  {m.unit && m.unit !== "分" ? (
                    <span className="text-xs text-slate-500">{m.unit}</span>
                  ) : null}
                </div>
                <div className="mt-1 text-xs text-slate-500">口径：{m.definition}</div>
                <div className="text-xs text-slate-400">数据截至：{formatDateTime(m.as_of)}</div>
                {m.note ? <div className="mt-1 text-xs text-amber-700">{m.note}</div> : null}
              </div>
            ))}
          </div>
          <p className="text-xs text-slate-400">
            指标由 MetricsService 唯一计算，看板 / 导出 / AI 均复用同一口径，本页不做任何重算（R22）。
          </p>
        </>
      ) : null}
    </section>
  );
}

// ============================== 逐月收支汇总（T33 累计视图） ==============================

interface ProfitTrendRow {
  period: string;
  sales: number;
  procurement: number;
  freezer: number;
  activity: number;
  other_expense: number;
  performance: number;
  expense_total: number;
  net: number;
  cumulative: number;
}

function TrendSection() {
  const trend = useApiQuery<{ items: ProfitTrendRow[] }>(
    "/reporting/profit-trend",
    { months: 6, scope: REPORT_SCOPE },
    { enabled: true },
  );
  const items = trend.data?.items ?? [];

  return (
    <section className="space-y-3 rounded-md border border-slate-200 bg-white p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 className="text-sm font-semibold text-slate-700">
          逐月收支汇总（近 6 个月 · 累计净利润）
        </h2>
        <span className="text-xs text-slate-400">
          口径与月度净利润一致：收入=送达确认销售额；支出=进货+冰柜+费用+绩效工资
        </span>
      </div>

      {trend.loading ? <LoadingBlock text="汇总逐月收支…" /> : null}
      {trend.error ? (
        <ErrorState message={trend.error} status={trend.status} onRetry={trend.refetch} />
      ) : null}

      {!trend.loading && !trend.error ? (
        items.length === 0 ? (
          <EmptyState title="暂无收支数据" description="发生送达、收货或费用审批后自动汇总。" />
        ) : (
          <div className="overflow-x-auto">
            <table className="min-w-full text-sm">
              <thead>
                <tr className="border-b border-slate-200 text-left text-xs text-slate-500">
                  <th className="py-2 pr-4 font-medium">月份</th>
                  <th className="py-2 pr-4 font-medium text-right">收入</th>
                  <th className="py-2 pr-4 font-medium text-right">进货成本</th>
                  <th className="py-2 pr-4 font-medium text-right">冰柜</th>
                  <th className="py-2 pr-4 font-medium text-right">费用</th>
                  <th className="py-2 pr-4 font-medium text-right">绩效工资</th>
                  <th className="py-2 pr-4 font-medium text-right">支出合计</th>
                  <th className="py-2 pr-4 font-medium text-right">净利润</th>
                  <th className="py-2 font-medium text-right">累计净利润</th>
                </tr>
              </thead>
              <tbody>
                {items.map((row) => (
                  <tr key={row.period} className="border-b border-slate-100">
                    <td className="py-2 pr-4 text-slate-700">{row.period}</td>
                    <td className="py-2 pr-4 text-right text-slate-700">{money(row.sales)}</td>
                    <td className="py-2 pr-4 text-right text-slate-500">{money(row.procurement)}</td>
                    <td className="py-2 pr-4 text-right text-slate-500">{money(row.freezer)}</td>
                    <td className="py-2 pr-4 text-right text-slate-500">
                      {money(row.activity + row.other_expense)}
                    </td>
                    <td className="py-2 pr-4 text-right text-slate-500">{money(row.performance)}</td>
                    <td className="py-2 pr-4 text-right text-slate-700">{money(row.expense_total)}</td>
                    <td
                      className={`py-2 pr-4 text-right font-medium ${
                        row.net >= 0 ? "text-emerald-700" : "text-rose-700"
                      }`}
                    >
                      {money(row.net)}
                    </td>
                    <td
                      className={`py-2 text-right font-semibold ${
                        row.cumulative >= 0 ? "text-emerald-800" : "text-rose-800"
                      }`}
                    >
                      {money(row.cumulative)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )
      ) : null}
    </section>
  );
}

// ============================== 损益构成明细 ==============================

function DetailSection({
  applied,
  onToast,
}: {
  applied: AppliedFilters;
  onToast: (message: string, tone?: ToastTone) => void;
}) {
  const [reportKey, setReportKey] = useState<DetailReportKey>("SALES");

  const report = useApiQuery<ReportRows>(
    `/reporting/reports/${reportKey}`,
    {
      period: applied.period,
      region_id: applied.region_id,
      brand_id: applied.brand_id,
      scope: REPORT_SCOPE,
    },
    { enabled: Boolean(applied.period) },
  );

  return (
    <section className="space-y-3 rounded-md border border-slate-200 bg-white p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 className="text-sm font-semibold text-slate-700">损益构成明细</h2>
        <div className="flex flex-wrap gap-1">
          {DETAIL_REPORTS.map(([key, text]) => (
            <button
              key={key}
              type="button"
              onClick={() => setReportKey(key)}
              className={`rounded px-3 py-1.5 text-sm ${
                reportKey === key
                  ? "bg-slate-100 font-medium text-slate-800"
                  : "text-slate-600 hover:bg-slate-50"
              }`}
            >
              {text}
            </button>
          ))}
        </div>
      </div>

      {report.loading ? <LoadingBlock text="加载报表…" /> : null}
      {report.error ? (
        <ErrorState message={report.error} status={report.status} onRetry={report.refetch} />
      ) : null}

      {!report.loading && !report.error ? (
        <>
          <div className="text-xs text-slate-500">共 {report.data?.row_count ?? 0} 行</div>
          <ReportTable
            columns={report.data?.columns ?? []}
            rows={report.data?.rows ?? []}
          />
        </>
      ) : null}

      <div className="flex flex-wrap items-center gap-2">
        <ExportActions reportKey={reportKey} applied={applied} onToast={onToast} variant="inline" />
        <span className="text-xs text-slate-400">
          明细为已审核事实的聚合视图；损益口径以「月度净利润」指标为准（见上方卡片口径）。
        </span>
      </div>
    </section>
  );
}

// ============================== 导出 ==============================

function ExportSection({
  applied,
  canExport,
  onToast,
}: {
  applied: AppliedFilters;
  canExport: boolean;
  onToast: (message: string, tone?: ToastTone) => void;
}) {
  const [reportKey, setReportKey] = useState<string>("METRICS");

  return (
    <section className="space-y-3 rounded-md border border-slate-200 bg-white p-4">
      <h2 className="text-sm font-semibold text-slate-700">报表导出</h2>

      {!canExport ? (
        <p className="text-xs text-slate-400">
          当前角色无导出权限（后端 <code>report.export_request</code>：片区经理 / 财务 / 系统管理员）。
        </p>
      ) : (
        <>
          <div className="flex flex-wrap items-end gap-3">
            <Field label="导出报表">
              <select
                value={reportKey}
                onChange={(e) => setReportKey(e.target.value)}
                className="w-44 rounded-md border border-slate-300 bg-white px-2 py-1.5 text-sm"
              >
                {EXPORT_REPORTS.map(([key, text]) => (
                  <option key={key} value={key}>
                    {text}
                  </option>
                ))}
              </select>
            </Field>
            <ExportActions reportKey={reportKey} applied={applied} onToast={onToast} variant="full" />
          </div>

          <p className="text-xs text-slate-400">
            导出沿用当前用户的行动/字段权限，任务在服务端重新校验数据域与发起人权限，链接短期有效并记录导出审计（Q119/Q120）。
          </p>

          <ExportJobList onToast={onToast} />
        </>
      )}
    </section>
  );
}
