/** 今日工作台占位（角色自适应占位，T11/T19 接入）。 */
import { EmptyState } from "../components/EmptyState";
import { MetricCard } from "../components/MetricCard";
import { useAuth } from "../lib/auth";

export function Workspace() {
  const { user } = useAuth();
  return (
    <div className="space-y-4">
      <h1 className="text-lg font-semibold text-slate-800">今日工作台</h1>
      <p className="text-sm text-slate-500">
        当前角色：<span className="font-medium">{user?.role ?? "未登录"}</span>
      </p>
      <section
        aria-label="关键指标"
        className="grid grid-cols-1 gap-3 md:grid-cols-2 lg:grid-cols-4"
      >
        <MetricCard label="待办" value="—" />
        <MetricCard label="本周新签" value="—" />
        <MetricCard label="待补货商家" value="—" />
        <MetricCard label="本周业绩" value="—" />
      </section>
      <EmptyState
        title="指标与地图"
        description="指标出自 MetricsService，地图出自排班/打卡模块。"
        nextStep="T19 指标 + T13 排班"
      />
    </div>
  );
}
