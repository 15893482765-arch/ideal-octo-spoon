/** 页面标题区（T30 页面统一骨架）。
 *
 * UI 迭代：标题加大到 22px、描述改为辅助灰，操作按钮区靠右对齐。
 * props 未变。
 */
import type { ReactNode } from "react";

export interface PageHeaderProps {
  title: string;
  description?: string;
  actions?: ReactNode;
}

export function PageHeader({ title, description, actions }: PageHeaderProps) {
  return (
    <div className="flex flex-wrap items-start justify-between gap-3 pb-1">
      <div className="min-w-0">
        <h1 className="qs-page-title">{title}</h1>
        {description ? <p className="qs-hint mt-1 max-w-[70ch]">{description}</p> : null}
      </div>
      {actions ? <div className="flex flex-wrap items-center gap-2">{actions}</div> : null}
    </div>
  );
}
