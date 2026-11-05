/** 经营报表（T21 前端联调 / PRD §1.5、§4.10、R22、R34）。
 *
 * 接入后端（全部显式传 `scope=PRODUCTION`，缺省即被拒，R34）：
 * - `GET /api/reporting/reports/{report_key}?period=&region_id=&brand_id=&sku_id=&sales_rep_id=&scope=`
 *   单请求返回「列 + 行」聚合结果，避免前端多请求拼装（PRD §4.10 多维筛选）；
 * - 导出复用 `POST /api/reporting/exports`、`/{id}/run`、`/{id}/download`。
 *
 * 红线：报表不重算口径——指标类（METRICS / PROFIT）由 MetricsService 唯一计算；
 * 明细类报表只读已审核事实，并按当前用户的行级/字段权限裁剪（服务端执行）。
 */
import { useState } from "react";

import { useAuth } from "../lib/auth";
import { currentPeriodMonth } from "../lib/format";
import { useApiQuery } from "../lib/useApi";
import { useToast } from "../lib/useToast";
import { ErrorState, LoadingBlock, SessionNotice } from "../components/AsyncState";
import {
  EXPORT_REPORTS,
  ExportActions,
  ExportJobList,
  REPORT_SCOPE,
  reportLabel,
} from "../components/ExportPanel";
import { FilterBar } from "../components/FilterBar";
import { PageHeader } from "../components/PageHeader";
import { ReportTable } from "../components/ReportTable";
import { ToastStack } from "../components/Toast";

interface OptionItem {
  id: string;
  code: string;
  name: string;
}

interface ReportRows {
  report_key: string;
  columns: string[];
  rows: Record<string, unknown>[];
  row_count: number;
}

/** 指标类报表只吃统计周期，不吃片区/品牌/SKU/业务员维度（后端实现如此）。 */
const PERIOD_ONLY_REPORTS = new Set(["METRICS", "PROFIT"]);

export function Reports() {
  const { authenticated, user } = useAuth();
  const { items: toasts, push, dismiss } = useToast();

  const [periodInput, setPeriodInput] = useState(currentPeriodMonth());
  const [regionInput, setRegionInput] = useState("");
  const [brandInput, setBrandInput] = useState("");
  const [skuInput, setSkuInput] = useState("");
  const [repInput, setRepInput] = useState("");
  const [applied, setApplied] = useState({
    period: currentPeriodMonth(),
    region_id: "",
    brand_id: "",
    sku_id: "",
    sales_rep_id: "",
  });
  const [reportKey, setReportKey] = useState<string>("SALES");

  const role = user?.role;
  // 后端 MODULE_REPORT（report.export_request）：经理 / 财务 / 管理员
  const canExport =
    role === "REGIONAL_MANAGER" || role === "FINANCE" || role === "SYSTEM_ADMIN";

  const report = useApiQuery<ReportRows>(
    `/reporting/reports/${reportKey}`,
    {
      period: applied.period,
      region_id: applied.region_id,
      brand_id: applied.brand_id,
      sku_id: applied.sku_id,
      sales_rep_id: applied.sales_rep_id,
      scope: REPORT_SCOPE,
    },
    { enabled: authenticated && Boolean(applied.period) },
  );

  if (!authenticated) {
    return (
      <div className="space-y-4">
        <PageHeader title="经营报表" description="多维筛选、聚合报表与导出" />
        <SessionNotice hint="经营报表需要真实登录后的后端会话（服务端按角色与行级范围裁剪）。" />
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <PageHeader
        title="经营报表"
        description="时间 / 片区 / 品牌 / SKU / 业务员多维筛选；口径唯一来自 MetricsService 与已审核事实"
      />

      <FilterBar
        onApply={() =>
          setApplied({
            period: periodInput,
            region_id: regionInput,
            brand_id: brandInput,
            sku_id: skuInput,
            sales_rep_id: repInput,
          })
        }
        onReset={() => {
          const now = currentPeriodMonth();
          setPeriodInput(now);
          setRegionInput("");
          setBrandInput("");
          setSkuInput("");
          setRepInput("");
          setApplied({
            period: now,
            region_id: "",
            brand_id: "",
            sku_id: "",
            sales_rep_id: "",
          });
        }}
      >
        <FilterFields
          periodInput={periodInput}
          regionInput={regionInput}
          brandInput={brandInput}
          skuInput={skuInput}
          repInput={repInput}
          onPeriodChange={setPeriodInput}
          onRegionChange={setRegionInput}
          onBrandChange={setBrandInput}
          onSkuChange={setSkuInput}
          onRepChange={setRepInput}
        />
      </FilterBar>

      <section className="space-y-3 rounded-md border border-slate-200 bg-white p-4">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h2 className="text-sm font-semibold text-slate-700">
            {reportLabel(reportKey)}（{applied.period} · {REPORT_SCOPE}）
          </h2>
          <ExportActions
            reportKey={reportKey}
            applied={applied}
            onToast={push}
            variant="inline"
          />
        </div>

        <div className="flex flex-wrap gap-1">
          {EXPORT_REPORTS.map(([key, text]) => (
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

        {PERIOD_ONLY_REPORTS.has(reportKey) ? (
          <p className="rounded-md border border-sky-200 bg-sky-50 px-3 py-2 text-xs text-sky-800">
            「{reportLabel(reportKey)}」是指标类报表，口径由 MetricsService 唯一计算，仅按统计周期取值；
            片区 / 品牌 / SKU / 业务员维度不适用于本报表（仅影响明细类报表）。
          </p>
        ) : null}

        {!applied.period ? (
          <p className="text-sm text-slate-500">
            请先选择统计周期（YYYY-MM 或 YYYY-Www）后应用筛选。
          </p>
        ) : null}

        {report.loading ? <LoadingBlock text="生成报表…" /> : null}
        {report.error ? (
          <ErrorState message={report.error} status={report.status} onRetry={report.refetch} />
        ) : null}

        {!report.loading && !report.error && applied.period ? (
          <>
            <div className="text-xs text-slate-500">
              共 {report.data?.row_count ?? 0} 行 · 列：{(report.data?.columns ?? []).length} 列
            </div>
            <ReportTable
              columns={report.data?.columns ?? []}
              rows={report.data?.rows ?? []}
            />
          </>
        ) : null}

        <p className="text-xs text-slate-400">
          报表沿用当前用户的行级与字段权限；查看与导出都会写审计（导出另见下方任务列表的短期链接与过期时间）。
        </p>
      </section>

      <section className="space-y-3 rounded-md border border-slate-200 bg-white p-4">
        <h2 className="text-sm font-semibold text-slate-700">报表导出</h2>
        <p className="text-xs text-slate-400">
          导出在服务端重新校验数据域与发起人权限，链接短期有效（Q119/Q120）。
        </p>
        <ExportJobList canExport={canExport} onToast={push} />
      </section>

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

const selectClass = "w-40 rounded-md border border-slate-300 bg-white px-2 py-1.5 text-sm";

function FilterFields({
  periodInput,
  regionInput,
  brandInput,
  skuInput,
  repInput,
  onPeriodChange,
  onRegionChange,
  onBrandChange,
  onSkuChange,
  onRepChange,
}: {
  periodInput: string;
  regionInput: string;
  brandInput: string;
  skuInput: string;
  repInput: string;
  onPeriodChange: (v: string) => void;
  onRegionChange: (v: string) => void;
  onBrandChange: (v: string) => void;
  onSkuChange: (v: string) => void;
  onRepChange: (v: string) => void;
}) {
  const regions = useApiQuery<{ items: OptionItem[] }>("/master-data/regions");
  const brands = useApiQuery<{ items: OptionItem[] }>("/master-data/brands");
  // SKU 下拉复用采购域接口（随品牌联动）；业务员下拉复用绩效域在职人员
  const skus = useApiQuery<{ items: OptionItem[] }>("/procurement/sku-options", {
    brand_id: brandInput,
  });
  const reps = useApiQuery<{ items: OptionItem[] }>("/performance/options/users", {
    role_code: "SALES_REP",
  });

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
      <Field label="片区">
        <select value={regionInput} onChange={(e) => onRegionChange(e.target.value)} className={selectClass}>
          <option value="">全部</option>
          {(regions.data?.items ?? []).map((r) => (
            <option key={r.id} value={r.id}>
              {r.name}
            </option>
          ))}
        </select>
      </Field>
      <Field label="品牌">
        <select
          value={brandInput}
          onChange={(e) => {
            onBrandChange(e.target.value);
            onSkuChange(""); // 品牌变化后原 SKU 可能不属于该品牌，先清空
          }}
          className={selectClass}
        >
          <option value="">全部</option>
          {(brands.data?.items ?? []).map((b) => (
            <option key={b.id} value={b.id}>
              {b.name}
            </option>
          ))}
        </select>
      </Field>
      <Field label="SKU">
        <select value={skuInput} onChange={(e) => onSkuChange(e.target.value)} className={selectClass}>
          <option value="">全部</option>
          {(skus.data?.items ?? []).map((s) => (
            <option key={s.id} value={s.id}>
              {s.code} · {s.name}
            </option>
          ))}
        </select>
      </Field>
      <Field label="业务员">
        <select value={repInput} onChange={(e) => onRepChange(e.target.value)} className={selectClass}>
          <option value="">全部</option>
          {(reps.data?.items ?? []).map((u) => (
            <option key={u.id} value={u.id}>
              {u.name}
            </option>
          ))}
        </select>
      </Field>
    </>
  );
}
