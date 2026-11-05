/** 通用 Toast（极简：栈式、3s 自动消失）。
 *
 * UI 迭代：改白卡 + 左侧色点 + 柔和阴影，圆角 14px；计时与回调逻辑未动。
 */
import { useEffect, useState } from "react";

import { cn } from "../lib/utils";

export type ToastTone = "info" | "success" | "warning" | "danger";

export interface ToastItem {
  id: string;
  message: string;
  tone?: ToastTone;
  ttlMs?: number;
}

const toneDot: Record<ToastTone, string> = {
  info: "bg-sky-500",
  success: "bg-emerald-500",
  warning: "bg-amber-500",
  danger: "bg-rose-500",
};

export interface ToastStackProps {
  items: ReadonlyArray<ToastItem>;
  onDismiss: (id: string) => void;
}

export function ToastStack({ items, onDismiss }: ToastStackProps) {
  return (
    <div
      role="region"
      aria-label="通知"
      className="pointer-events-none fixed right-5 bottom-5 z-50 flex flex-col gap-2"
    >
      {items.map((t) => (
        <ToastView key={t.id} item={t} onDismiss={onDismiss} />
      ))}
    </div>
  );
}

function ToastView({ item, onDismiss }: { item: ToastItem; onDismiss: (id: string) => void }) {
  const tone = item.tone ?? "info";
  const ttl = item.ttlMs ?? 3000;
  const [visible, setVisible] = useState(true);
  useEffect(() => {
    const handle = window.setTimeout(() => {
      setVisible(false);
      onDismiss(item.id);
    }, ttl);
    return () => window.clearTimeout(handle);
  }, [ttl, item.id, onDismiss]);
  if (!visible) return null;
  return (
    <div
      role="status"
      className={cn(
        "pointer-events-auto flex min-w-[240px] items-start gap-2.5 rounded-[14px] bg-surface px-4 py-3 text-[13px] text-ink-2",
        "shadow-pop",
      )}
    >
      <span aria-hidden="true" className={cn("mt-1.5 h-2 w-2 shrink-0 rounded-full", toneDot[tone])} />
      <span className="leading-5">{item.message}</span>
    </div>
  );
}
