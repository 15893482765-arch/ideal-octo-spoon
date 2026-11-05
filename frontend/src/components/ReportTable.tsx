/** 动态报表表格（T21 经营报表 / 经营损益共用）。
 *
 * 后端 `ReportRowsView` 只返回列名数组与行数据，列集合随报表键变化，
 * 因此表格必须动态渲染。这里统一收敛展示口径：
 * - 列名 → 中文表头（未知列回落为原始字段名）；
 * - `*_fen` 列按「分 → 元」展示（金额统一整数分，前端不参与计算）；
 * - `*_at` / `as_of` 列按 Asia/Shanghai 本地时间展示；
 * - 状态/类别/来源等列套用统一徽标；
 * - 前端分页（后端聚合接口一次性返回，行数在调用方另行展示）。
 */
import { useMemo, useState } from "react";

import { boxes, formatDateTime, label, money, statusTone } from "../lib/format";
import { DataTable, type DataTableColumn } from "./DataTable";
import { Pagination } from "./Pagination";
import { StatusBadge } from "./StatusBadge";

const DEFAULT_PAGE_SIZE = 20;

/** 字段名 → 中文表头（与后端 ReportKey 各报表列对齐）。 */
export const COLUMN_LABELS: Record<string, string> = {
  id: "ID", code: "编码", name: "名称", status: "状态", grade: "等级",
  region_id: "片区", brand_id: "品牌", merchant_id: "商家", sku_id: "SKU",
  contract_no: "合同编号", title: "标题", effective_from: "生效日",
  expire_at: "到期日", biz_date: "业务日", confirmed_boxes: "确认箱数",
  amount_fen: "金额", asset_code: "资产编码", model: "型号",
  purchase_price_fen: "购置价", current_merchant_id: "当前商家",
  committed_boxes: "承诺箱数", fulfilled_boxes: "已履约箱数",
  promised_delivery_date: "承诺到货日", quantity_cases: "数量（箱）",
  unit_price_fen: "含税单价", expected_date: "预计到货",
  expense_no: "费用单号", type: "类别", period_month: "归属月份",
  statement_no: "工资单号", user_id: "人员", base_salary_fen: "底薪",
  performance_fen: "绩效", gross_pay_fen: "应发合计", validity: "有效性",
  source: "来源", display_pct: "陈列比例", submitted_at: "提交时间",
  value: "值", unit: "单位", definition: "口径", as_of: "数据截至",
  data_complete: "数据完整", note: "说明", key: "指标键",
  job_no: "任务号", report_key: "报表", format: "格式", row_count: "行数",
  expires_at: "链接过期", created_at: "创建时间", object_key: "对象键",
  content_type: "内容类型", requested_by_id: "发起人", scope: "数据域",
};

export function columnLabel(key: string): string {
  return COLUMN_LABELS[key] ?? key;
}

/** 单元格渲染：按列语义决定展示形式，原始值不做业务计算。 */
export function renderReportCell(key: string, value: unknown): React.ReactNode {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "boolean") return value ? "是" : "否";
  if (key.endsWith("_fen")) return money(Number(value));
  if (key === "confirmed_boxes") return boxes(String(value));
  if (key.endsWith("_at") || key === "as_of") {
    return <span className="text-xs text-slate-500">{formatDateTime(String(value))}</span>;
  }
  if (key.endsWith("_date") || key === "period_month") {
    return <span className="text-xs text-slate-600">{String(value)}</span>;
  }
  if (key === "status") {
    const text = String(value);
    return <StatusBadge tone={statusTone(text)} label={label(text)} />;
  }
  if (key === "type" || key === "format" || key === "validity" || key === "source") {
    const text = String(value);
    return <StatusBadge tone="neutral" label={label(text)} />;
  }
  if (key === "definition") {
    return <span className="text-xs text-slate-500">{String(value)}</span>;
  }
  return String(value);
}

export interface ReportTableProps {
  columns: ReadonlyArray<string>;
  rows: ReadonlyArray<Record<string, unknown>>;
  emptyText?: string;
  pageSize?: number;
}

/** 动态列报表 + 前端分页。 */
export function ReportTable({
  columns,
  rows,
  emptyText = "该区间暂无数据（或当前角色行级范围外）",
  pageSize = DEFAULT_PAGE_SIZE,
}: ReportTableProps) {
  const [page, setPage] = useState(1);

  const tableColumns: DataTableColumn<Record<string, unknown>>[] = useMemo(
    () =>
      columns.map((c) => ({
        key: c,
        header: columnLabel(c),
        align: c.endsWith("_fen") || c === "confirmed_boxes" ? "right" : "left",
        render: (row: Record<string, unknown>) => renderReportCell(c, row[c]),
      })),
    [columns],
  );

  const totalPages = Math.max(1, Math.ceil(rows.length / pageSize));
  const safePage = Math.min(page, totalPages);
  const pageRows = rows.slice((safePage - 1) * pageSize, safePage * pageSize);

  return (
    <>
      <DataTable
        rows={pageRows}
        columns={tableColumns}
        rowKey={(r) => String(r.id ?? JSON.stringify(r))}
        emptyText={emptyText}
      />
      <Pagination page={safePage} totalPages={totalPages} onPageChange={setPage} />
    </>
  );
}
