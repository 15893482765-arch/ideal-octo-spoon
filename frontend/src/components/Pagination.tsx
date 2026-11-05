/** 通用分页（占位：受控 page/total，调用方决定数据切片）。
 *
 * UI 迭代：按钮改圆角胶囊、页码文字降一级；onPageChange 逻辑未动。
 */
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
      className={cn("flex items-center justify-end gap-2 text-[13px]", className)}
    >
      <button
        type="button"
        onClick={() => onPageChange(prev)}
        disabled={page <= 1}
        className="qs-btn qs-btn-ghost disabled:opacity-40"
      >
        上一页
      </button>
      <span className="rounded-full bg-surface px-3 py-1 text-[12px] text-ink-3 shadow-[inset_0_0_0_1px_var(--color-line)]">
        第 {page} / {Math.max(1, totalPages)} 页
      </span>
      <button
        type="button"
        onClick={() => onPageChange(next)}
        disabled={page >= totalPages}
        className="qs-btn qs-btn-ghost disabled:opacity-40"
      >
        下一页
      </button>
    </nav>
  );
}
