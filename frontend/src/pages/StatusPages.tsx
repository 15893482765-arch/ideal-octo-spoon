/** 状态页：404 / 403。 */
import { Link } from "react-router-dom";

export function NotFound() {
  return (
    <div className="flex min-h-[60vh] flex-col items-center justify-center text-slate-600">
      <h1 className="text-3xl font-semibold text-slate-800">404</h1>
      <p className="mt-2 text-sm">页面不存在</p>
      <Link to="/workspace" className="mt-3 text-sm text-[var(--color-brand)] underline">
        返回工作台
      </Link>
    </div>
  );
}

export function Forbidden() {
  return (
    <div className="flex min-h-[60vh] flex-col items-center justify-center text-slate-600">
      <h1 className="text-3xl font-semibold text-slate-800">403</h1>
      <p className="mt-2 text-sm">当前角色无权访问该页面</p>
      <Link to="/workspace" className="mt-3 text-sm text-[var(--color-brand)] underline">
        返回工作台
      </Link>
    </div>
  );
}
