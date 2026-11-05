/** 业务页面通用占位（T06 / PRD §3.2 "页面路由" + §3.1 "业务页面先放带下一步的空态"）。
 *
 * 标题与「下一步」提示由 `lib/nav-config.ts` 的 `PAGE_ROUTES` 注入（单一真相源），
 * 页面不再各自硬编码任务卡编号。
 */
import { EmptyState } from "../components/EmptyState";

export function Placeholder({ title, nextStep }: { title: string; nextStep: string }) {
  return (
    <div className="space-y-4">
      <h1 className="text-lg font-semibold text-slate-800">{title}</h1>
      <EmptyState
        title={`${title}尚未实现`}
        description="本页为路由占位，待后续任务卡按 PRD §3.2 页面路由交付。"
        nextStep={nextStep}
      />
    </div>
  );
}
