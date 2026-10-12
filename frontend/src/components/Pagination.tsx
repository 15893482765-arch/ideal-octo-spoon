/** 通用分页（占位：受控 page/total，调用方决定数据切片）。 */
import { cn } from "../lib/utils";

export interface PaginationProps {
  page: number;
  totalPages: number;
  onPageChange: (page: number) => void;
  className?: string;
}

export function Pagination({ page, totalPages, onPageChange, className }: PaginationProps) {
  const prev = Math.max(1, page - 1);
  const next = Math.min(totalPages, page + 1);
  return (
    <nav
      aria-label="分页"
      className={cn("flex items-center justify-end gap-2 text-sm", className)}
    >
      <button
        type="button"
        onClick={() => onPageChange(prev)}
        disabled={page <= 1}
        className="rounded-md border border-slate-300 bg-white px-2 py-1 text-slate-700 disabled:opacity-40"
      >
        上一页
      </button>
      <span className="text-slate-500">
        第 {page} / {Math.max(1, totalPages)} 页
      </span>
      <button
        type="button"
        onClick={() => onPageChange(next)}
        disabled={page >= totalPages}
        className="rounded-md border border-slate-300 bg-white px-2 py-1 text-slate-700 disabled:opacity-40"
      >
        下一页
      </button>
    </nav>
  );
}
