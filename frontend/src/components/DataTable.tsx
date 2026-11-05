/** 通用数据表（占位实现；T08+ 业务表按需扩展排序/筛选/分页）。
 *
 * UI 迭代：表格改白卡承载、去掉外框线、表头改小字灰色、行 hover 用浅紫底，
 * 单元格纵向留白加大。列定义 / key / 渲染函数完全未动。
 */
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
        className={cn(
          "rounded-[16px] bg-surface p-8 text-center text-[13px] text-ink-3",
          className,
        )}
      >
        {emptyText}
      </div>
    );
  }
  return (
    <div className={cn("qs-card overflow-x-auto", className)}>
      <table className="min-w-full text-[13px]">
        <thead>
          <tr className="border-b border-line">
            {columns.map((c) => (
              <th
                key={c.key}
                scope="col"
                style={c.width ? { width: c.width } : undefined}
                className={cn(
                  "px-4 py-3 text-[11px] font-semibold tracking-wide text-ink-3 uppercase whitespace-nowrap",
                  c.align === "right"
                    ? "text-right"
                    : c.align === "center"
                      ? "text-center"
                      : "text-left",
                )}
              >
                {c.header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr
              key={rowKey(row)}
              className="border-b border-line-soft transition-colors last:border-0 hover:bg-hover"
            >
              {columns.map((c) => (
                <td
                  key={c.key}
                  className={cn(
                    "px-4 py-3 align-middle text-ink-2",
                    c.align === "right"
                      ? "text-right"
                      : c.align === "center"
                        ? "text-center"
                        : "text-left",
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
