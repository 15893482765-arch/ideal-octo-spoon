/** 通用确认弹窗（受控：open + onClose + onConfirm；T08+ 业务卡用）。
 *
 * UI 迭代：遮罩改深墨半透明 + 轻模糊，弹窗圆角 20px、按钮胶囊化。
 * props / Esc 关闭 / aria 语义均未变。
 */
import { useEffect } from "react";

import { cn } from "../lib/utils";

export interface ConfirmDialogProps {
  open: boolean;
  title: string;
  description?: string;
  confirmText?: string;
  cancelText?: string;
  destructive?: boolean;
  onConfirm: () => void;
  onClose: () => void;
}

export function ConfirmDialog({
  open,
  title,
  description,
  confirmText = "确认",
  cancelText = "取消",
  destructive = false,
  onConfirm,
  onClose,
}: ConfirmDialogProps) {
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);
  if (!open) return null;
  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label={title}
      className="fixed inset-0 z-40 flex items-center justify-center bg-ink/45 p-4 backdrop-blur-[3px]"
    >
      <div className="w-full max-w-sm rounded-[20px] bg-surface p-6 shadow-pop">
        <h2 className="qs-card-title text-[16px]">{title}</h2>
        {description ? <p className="qs-body mt-2">{description}</p> : null}
        <div className="mt-5 flex justify-end gap-2">
          <button type="button" onClick={onClose} className="qs-btn qs-btn-ghost">
            {cancelText}
          </button>
          <button
            type="button"
            onClick={onConfirm}
            className={cn(
              "qs-btn",
              destructive
                ? "bg-rose-600 text-white hover:bg-rose-700"
                : "qs-btn-primary",
            )}
          >
            {confirmText}
          </button>
        </div>
      </div>
    </div>
  );
}
