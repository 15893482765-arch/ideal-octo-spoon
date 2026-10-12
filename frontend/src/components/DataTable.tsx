/** 通用数据表（占位实现；T08+ 业务表按需扩展排序/筛选/分页）。 */
import type { ReactNode } from "react";

import { cn } from "../lib/utils";

export interface DataTableColumn<Row> {
  key: string;
  header: string;
  render: (row: Row) => ReactNode;
  width?: string;
  align?: "left" | "right" | "center";
}

export interface DataTableProps<Row> {
  rows: ReadonlyArray<Row>;
  columns: ReadonlyArray<DataTableColumn<Row>>;
  rowKey: (row: Row) => string;
  emptyText?: string;
  className?: string;
}

export function DataTable<Row>(props: DataTableProps<Row>) {
  const { rows, columns, rowKey, emptyText = "暂无数据", className } = props;
  if (rows.length === 0) {
    return (
      <div
        role="status"
        className={cn("rounded-md border border-slate-200 bg-slate-50 p-4 text-sm text-slate-500", className)}
      >
        {emptyText}
      </div>
    );
  }
  return (
    <div className={cn("overflow-x-auto rounded-md border border-slate-200", className)}>
      <table className="min-w-full divide-y divide-slate-200 text-sm">
        <thead className="bg-slate-50">
          <tr>
            {columns.map((c) => (
              <th
                key={c.key}
                scope="col"
                style={c.width ? { width: c.width } : undefined}
                className={cn(
                  "px-3 py-2 text-xs font-semibold tracking-wide text-slate-600 uppercase",
                  c.align === "right" ? "text-right" : c.align === "center" ? "text-center" : "text-left",
                )}
              >
                {c.header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-100 bg-white">
          {rows.map((row) => (
            <tr key={rowKey(row)} className="hover:bg-slate-50">
              {columns.map((c) => (
                <td
                  key={c.key}
                  className={cn(
                    "px-3 py-2 text-slate-700",
                    c.align === "right" ? "text-right" : c.align === "center" ? "text-center" : "text-left",
                  )}
                >
                  {c.render(row)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
