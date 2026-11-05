/** 异步状态展示：加载中 / 出错 / 无权限（T30 页面统一骨架）。
 *
 * UI 迭代：加载块改白卡无边框，错误态改浅底 + 圆角卡片，提示文案与逻辑不变。
 */
import { LoadingSpinner } from "./LoadingSpinner";

export function LoadingBlock({ text = "加载中…" }: { text?: string }) {
  return (
    <div className="flex items-center justify-center gap-2 rounded-[16px] bg-surface p-10 text-[13px] text-ink-3">
      <LoadingSpinner />
      <span>{text}</span>
    </div>
  );
}

export interface ErrorStateProps {
  message: string;
  status?: number | null;
  onRetry?: () => void;
}

export function ErrorState({ message, status, onRetry }: ErrorStateProps) {
  const hint =
    status === 403
      ? "当前角色没有该模块权限（后端能力二次鉴权）"
      : status === 401
        ? "登录已失效，请重新登录"
        : status === 404
          ? "接口不存在或对象已删除"
          : undefined;
  return (
    <div
      role="alert"
      className="rounded-[16px] bg-rose-50 p-5 text-[13px] text-rose-700 shadow-[inset_0_0_0_1px_rgba(244,63,94,0.16)]"
    >
      <div className="font-semibold">加载失败</div>
      <div className="mt-1">{message}</div>
      {hint ? <div className="mt-1 text-[12px] text-rose-600">{hint}</div> : null}
      {onRetry ? (
        <button
          type="button"
          onClick={onRetry}
          className="mt-3 rounded-full bg-surface px-3.5 py-1.5 text-[12px] font-medium text-rose-700 shadow-[inset_0_0_0_1px_rgba(244,63,94,0.24)] hover:bg-rose-100"
        >
          重试
        </button>
      ) : null}
    </div>
  );
}

/** 未建立真实会话时的提示（开发注入账号或未登录）。 */
export function SessionNotice({ hint }: { hint?: string }) {
  return (
    <div className="rounded-[16px] bg-amber-50 p-5 text-[13px] text-amber-800 shadow-[inset_0_0_0_1px_rgba(245,158,11,0.18)]">
      <div className="font-semibold">尚未建立后端会话</div>
      <div className="mt-1">
        业务数据需要真实登录（后端 <code>/api/auth/login</code>）。
        {hint ? ` ${hint}` : ""}
      </div>
    </div>
  );
}
