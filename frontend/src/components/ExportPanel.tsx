/** 报表导出（T21 / PRD §4.10、R34：异步导出 + 短期链接；经营报表与经营损益共用）。
 *
 * 接入后端：
 * - `POST /api/reporting/exports?scope=PRODUCTION`（创建 QUEUED 任务，沿用当前用户行/字段权限）；
 * - `POST /api/reporting/exports/{id}/run`（执行；服务端重新校验数据域与发起人权限）；
 * - `GET /api/reporting/exports/{id}/download`（短期链接要素：对象键 + TTL + 过期时间）；
 * - `GET /api/reporting/exports`（任务列表：全局角色可读本数据域全部，其余仅本人）。
 *
 * 说明：后端未返回可点击的签名 URL，页面只展示链接要素与有效期（Q120 链接过期由后端判定）。
 */
import { useState } from "react";

import { api } from "../lib/api";
import { formatDateTime, label, statusTone } from "../lib/format";
import { useApiMutation, useApiQuery } from "../lib/useApi";
import { ErrorState, LoadingBlock } from "./AsyncState";
import { DataTable, type DataTableColumn } from "./DataTable";
import { StatusBadge } from "./StatusBadge";
import type { ToastTone } from "./Toast";

/** 正式口径必须显式声明 PRODUCTION（R34）。 */
export const REPORT_SCOPE = "PRODUCTION";

/** 可导出报表（与后端 ReportKey 对齐）。 */
export const EXPORT_REPORTS = [
  ["METRICS", "经营指标汇总"],
  ["PROFIT", "损益报表"],
  ["SALES", "销量报表"],
  ["PROCUREMENT", "采购报表"],
  ["EXPENSES", "费用报表"],
  ["PERFORMANCE", "绩效工资报表"],
  ["CONTRACTS", "合同报表"],
  ["MERCHANTS", "商家报表"],
  ["FREEZERS", "冰柜报表"],
  ["VISITS", "巡访报表"],
  ["REPLENISHMENT", "补货报表"],
] as const;

export type ExportReportKey = (typeof EXPORT_REPORTS)[number][0];

const EXPORT_FORMATS = ["XLSX", "CSV", "PDF"] as const;

export function reportLabel(key: string): string {
  const hit = EXPORT_REPORTS.find(([k]) => k === key);
  return hit ? hit[1] : key;
}

export interface ExportFilters {
  period: string;
  region_id: string;
  brand_id: string;
}

export interface ExportJob {
  id: string;
  job_no: string;
  report_key: string;
  format: string;
  status: string;
  row_count: number | null;
  object_key: string | null;
  content_type: string | null;
  requested_by_id: string;
  scope: string;
  expires_at: string | null;
  created_at: string;
}

interface ExportDownload {
  job_id: string;
  job_no: string;
  object_key: string;
  content_type: string | null;
  row_count: number | null;
  expires_at: string | null;
  ttl_seconds: number;
}

export interface ExportActionsProps {
  reportKey: string;
  applied: ExportFilters;
  onToast: (message: string, tone?: ToastTone) => void;
  /** inline：明细区小按钮；full：导出面板主按钮 */
  variant?: "inline" | "full";
}

/** 创建导出任务（XLSX / CSV / PDF）。 */
export function ExportActions({
  reportKey,
  applied,
  onToast,
  variant = "inline",
}: ExportActionsProps) {
  const requestMut = useApiMutation((fmt: string) =>
    api.post<ExportJob>(
      "/reporting/exports",
      {
        report_key: reportKey,
        format: fmt,
        filters: {
          period: applied.period,
          region_id: applied.region_id || undefined,
          brand_id: applied.brand_id || undefined,
        },
      },
      { scope: REPORT_SCOPE },
    ),
  );

  return (
    <div className="flex flex-wrap items-center gap-2">
      {EXPORT_FORMATS.map((fmt) => (
        <button
          key={fmt}
          type="button"
          disabled={requestMut.submitting || !applied.period}
          title={!applied.period ? "请先选择统计周期" : undefined}
          onClick={() => {
            requestMut.run(fmt).then((res) => {
              if (res) {
                onToast(`导出任务 ${res.job_no} 已创建（${fmt}）`, "success");
              } else {
                onToast(requestMut.error || "导出申请失败", "danger");
              }
            });
          }}
          className={
            variant === "inline"
              ? "rounded-md border border-slate-300 bg-white px-2 py-1 text-xs text-slate-700 hover:bg-slate-50 disabled:opacity-50"
              : "rounded-md bg-[var(--color-brand)] px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50"
          }
        >
          {requestMut.submitting
            ? "提交中…"
            : variant === "inline"
              ? `导出 ${fmt}`
              : `创建 ${fmt} 导出任务`}
        </button>
      ))}
    </div>
  );
}

/** 导出任务列表 + 执行 + 取短期链接。 */
export function ExportJobList({
  onToast,
  canExport = true,
}: {
  onToast: (message: string, tone?: ToastTone) => void;
  canExport?: boolean;
}) {
  const jobs = useApiQuery<ExportJob[]>("/reporting/exports", undefined, {
    enabled: canExport,
  });

  const [download, setDownload] = useState<ExportDownload | null>(null);

  const runMut = useApiMutation((id: string) =>
    api.post<ExportJob>(`/reporting/exports/${id}/run`),
  );
  const linkMut = useApiMutation((id: string) =>
    api.get<ExportDownload>(`/reporting/exports/${id}/download`),
  );

  const columns: DataTableColumn<ExportJob>[] = [
    { key: "job_no", header: "任务号", render: (r) => <span className="font-mono text-xs">{r.job_no}</span> },
    { key: "report_key", header: "报表", render: (r) => reportLabel(r.report_key) },
    { key: "format", header: "格式", render: (r) => r.format },
    {
      key: "status",
      header: "状态",
      render: (r) => <StatusBadge tone={statusTone(r.status)} label={label(r.status)} />,
    },
    { key: "row_count", header: "行数", align: "right", render: (r) => r.row_count ?? "—" },
    {
      key: "expires_at",
      header: "链接过期",
      render: (r) => <span className="text-xs text-slate-500">{formatDateTime(r.expires_at)}</span>,
    },
    {
      key: "created_at",
      header: "创建时间",
      render: (r) => <span className="text-xs text-slate-500">{formatDateTime(r.created_at)}</span>,
    },
    {
      key: "actions",
      header: "操作",
      render: (r) => (
        <div className="flex gap-2">
          <button
            type="button"
            disabled={runMut.submitting || r.status === "SUCCEEDED"}
            onClick={() => {
              runMut.run(r.id).then((res) => {
                if (res) {
                  onToast(`导出任务 ${res.job_no} 已执行（${label(res.status)}）`, "success");
                  jobs.refetch();
                } else {
                  onToast(runMut.error || "执行导出失败", "danger");
                }
              });
            }}
            className="rounded-md border border-slate-300 bg-white px-2 py-1 text-xs text-slate-700 hover:bg-slate-50 disabled:opacity-40"
          >
            执行
          </button>
          <button
            type="button"
            disabled={linkMut.submitting || r.status !== "SUCCEEDED"}
            onClick={() => {
              linkMut.run(r.id).then((res) => {
                if (res) {
                  setDownload(res);
                  onToast(`已生成短期链接（${res.ttl_seconds}s 内有效）`, "success");
                } else {
                  onToast(linkMut.error || "获取下载链接失败", "danger");
                }
              });
            }}
            className="rounded-md border border-slate-300 bg-white px-2 py-1 text-xs text-slate-700 hover:bg-slate-50 disabled:opacity-40"
          >
            取链接
          </button>
        </div>
      ),
    },
  ];

  if (!canExport) {
    return (
      <p className="text-xs text-slate-400">
        当前角色无导出权限（后端 <code>report.export_request</code>：片区经理 / 财务 / 系统管理员）。
      </p>
    );
  }

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <div className="text-xs font-medium text-slate-600">导出任务</div>
        <button
          type="button"
          onClick={jobs.refetch}
          className="rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm text-slate-600 hover:bg-slate-50"
        >
          刷新任务列表
        </button>
      </div>

      {jobs.loading ? <LoadingBlock text="加载导出任务…" /> : null}
      {jobs.error ? (
        <ErrorState message={jobs.error} status={jobs.status} onRetry={jobs.refetch} />
      ) : null}

      {!jobs.loading && !jobs.error ? (
        <DataTable
          rows={jobs.data ?? []}
          columns={columns}
          rowKey={(r) => r.id}
          emptyText="暂无导出任务（选择报表与格式后创建）"
        />
      ) : null}

      {download ? (
        <div className="rounded-md border border-emerald-200 bg-emerald-50 p-3 text-xs text-emerald-900">
          <div className="font-medium">下载链接已生成（{download.ttl_seconds}s 内有效）</div>
          <div className="mt-1 font-mono break-all">对象键：{download.object_key}</div>
          <div>
            行数：{download.row_count ?? "—"} · 内容类型：{download.content_type ?? "—"} · 过期：
            {formatDateTime(download.expires_at)}
          </div>
          <div className="mt-1 text-emerald-700">
            实际下载请通过对象存储签名 URL 完成（本页只展示服务端签发的短期链接要素）。
          </div>
        </div>
      ) : null}
    </div>
  );
}
