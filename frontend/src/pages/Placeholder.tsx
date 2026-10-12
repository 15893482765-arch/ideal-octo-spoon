/** 业务页面通用占位（T06 / PRD §3.1 "业务页面先放带下一步的空态"）。 */
import { EmptyState } from "../components/EmptyState";

export function Placeholder({ title, nextStep }: { title: string; nextStep: string }) {
  return (
    <div className="space-y-4">
      <h1 className="text-lg font-semibold text-slate-800">{title}</h1>
      <EmptyState
        title={`${title}尚未实现`}
        description="本占位页面将在后续任务卡中按 §3.1 路由交付。"
        nextStep={nextStep}
      />
    </div>
  );
}
