/** 通用 Toast（极简：栈式、3s 自动消失）。 */
import { useEffect, useState } from "react";

import { cn } from "../lib/utils";

export type ToastTone = "info" | "success" | "warning" | "danger";

export interface ToastItem {
  id: string;
  message: string;
  tone?: ToastTone;
  ttlMs?: number;
}

const toneClass: Record<ToastTone, string> = {
  info: "bg-sky-50 text-sky-800 ring-sky-200",
  success: "bg-emerald-50 text-emerald-800 ring-emerald-200",
  warning: "bg-amber-50 text-amber-800 ring-amber-200",
  danger: "bg-rose-50 text-rose-800 ring-rose-200",
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
      className="pointer-events-none fixed right-4 bottom-4 z-50 flex flex-col gap-2"
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
        "pointer-events-auto min-w-60 rounded-md px-3 py-2 text-sm shadow-md ring-1 ring-inset",
        toneClass[tone],
      )}
    >
      {item.message}
    </div>
  );
}
