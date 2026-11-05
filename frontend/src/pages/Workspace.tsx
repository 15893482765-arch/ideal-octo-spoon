/** 今日工作台（T27 / PRD §4.2）。
 *
 * 接入后端：
 * - `GET /api/workbench/home`（角色首页聚合：指标卡 + 待办 + 风险 + 快捷动作）；
 * - `GET /api/workbench/notifications`（通知列表）；
 * - `POST /api/workbench/notifications/read`（标记单条已读）；
 * - `POST /api/workbench/notifications/read-all`（全部已读）。
 *
 * 角色差异、行级裁剪、指标口径与风险判定全在后端 WorkbenchService 裁决；
 * 页面只做展示与受控交互（前端隐藏不作为鉴权）。
 */
import { useMemo } from "react";
import { Link } from "react-router-dom";

import { api } from "../lib/api";
import { useAuth } from "../lib/auth";
import { currentPeriodMonth, formatDateTime, label, money } from "../lib/format";
import { useApiMutation, useApiQuery, useAction } from "../lib/useApi";
import { useToast } from "../lib/useToast";
import { ErrorState, LoadingBlock, SessionNotice } from "../components/AsyncState";
import { EmptyState } from "../components/EmptyState";
import { MetricCard } from "../components/MetricCard";
import { PageHeader } from "../components/PageHeader";
import { StatusBadge } from "../components/StatusBadge";
import { ToastStack } from "../components/Toast";

// ---- 后端响应类型（与 schemas/workbench.py 对齐） ----

interface MetricCardData {
  key: string;
  name: string;
  value: string | number | null;
  unit: string;
  definition: string;
  as_of: string;
  period: string;
  data_complete: boolean;
  note: string | null;
  source_refs: string[];
}

interface TodoItem {
  key: string;
  source: string;
  source_id: string;
  title: string;
  category: string;
  occurred_at: string;
  deep_link: string;
  read: boolean;
}

interface RiskCardData {
  id: string;
  rule_code: string;
  object_type: string;
  object_id: string;
  status: string;
  severity: string;
  first_seen_at: string;
  priority: number;
  deep_link: string;
}

interface QuickAction {
  label: string;
  path: string;
}

interface WorkbenchHome {
  role: string;
  period: string;
  as_of: string | null;
  metric_cards: MetricCardData[];
  todos: TodoItem[];
  unread_count: number;
  risk_list: RiskCardData[];
  quick_actions: QuickAction[];
  deep_links: Record<string, string>;
}

interface NotificationItem {
  key: string;
  source: string;
  source_id: string;
  title: string;
  category: string;
  occurred_at: string;
  deep_link: string;
  read: boolean;
}

// ---- 辅助 ----

/** 指标值渲染：比率类 → 百分比，金额类 → 元，其余原样显示。 */
function metricValue(card: MetricCardData): string {
  if (card.value === null || card.value === undefined) return "—";
  const v = typeof card.value === "number" ? card.value : Number(card.value);
  if (Number.isNaN(v)) return String(card.value);
  // unit 为 % 时按比率展示
  if (card.unit === "%") return `${v.toFixed(2)}%`;
  // unit 为 元 时按金额展示
  if (card.unit === "元") return `¥${v.toLocaleString("zh-CN")}`;
  // unit 为 分（净利润等金额指标的后端存储单位）→ 转元展示，不能原样放大 100 倍
  if (card.unit === "分") return money(v);
  // unit 为 箱 时原样
  return String(v);
}

const RISK_TONE: Record<string, "danger" | "warning" | "info" | "neutral"> = {
  CRITICAL: "danger",
  HIGH: "danger",
  MEDIUM: "warning",
  LOW: "info",
};

function riskTone(severity: string): "danger" | "warning" | "info" | "neutral" {
  return RISK_TONE[severity] ?? "neutral";
}

// ---- 页面 ----

export function Workspace() {
  const { authenticated, user } = useAuth();
  const { items: toasts, push, dismiss } = useToast();

  const period = useMemo(() => currentPeriodMonth(), []);

  // 角色首页聚合
  const home = useApiQuery<WorkbenchHome>(
    "/workbench/home",
    { period, risk_limit: 10, todo_limit: 20 },
    { enabled: authenticated },
  );

  // 通知列表
  const notifications = useApiQuery<NotificationItem[]>(
    "/workbench/notifications",
    { limit: 50 },
    { enabled: authenticated },
  );

  // 标记已读
  const markReadMut = useApiMutation((key: string) =>
    api.post<{ key: string; read: boolean }>("/workbench/notifications/read", { key }),
  );

  // 全部已读
  const markAllReadMut = useAction(() =>
    api.post<{ marked: number }>("/workbench/notifications/read-all"),
  );

  function handleMarkRead(key: string) {
    markReadMut.run(key).then((res) => {
      if (res) {
        push("已标记为已读", "success");
        notifications.refetch();
        home.refetch();
      } else if (markReadMut.error) {
        push(markReadMut.error, "danger");
      }
    });
  }

  function handleMarkAllRead() {
    markAllReadMut.run().then((res) => {
      if (res) {
        push(`已标记 ${res.marked} 条为已读`, "success");
        notifications.refetch();
        home.refetch();
      } else if (markAllReadMut.error) {
        push(markAllReadMut.error, "danger");
      }
    });
  }

  // ---- 未登录 / 会话未建立 ----
  if (!authenticated) {
    return (
      <div className="space-y-4">
        <PageHeader title="今日工作台" description="角色自适应首页：指标、待办、风险与快捷动作" />
        <SessionNotice hint="可使用 URL ?role=SALES_REP 注入开发账号预览框架。" />
      </div>
    );
  }

  const homeData = home.data;
  const notifData = notifications.data ?? [];
  const unreadCount = homeData?.unread_count ?? 0;

  return (
    <div className="space-y-4">
      <PageHeader
        title="今日工作台"
        description={`当前角色：${label(user?.role)} · 统计周期：${period}`}
        actions={
          unreadCount > 0 ? (
            <button
              type="button"
              onClick={handleMarkAllRead}
              disabled={markAllReadMut.submitting}
              className="rounded-md border border-slate-300 bg-white px-3 py-1.5 text-xs text-slate-700 hover:bg-slate-100 disabled:opacity-50"
            >
              全部已读（{unreadCount}）
            </button>
          ) : null
        }
      />

      {/* 指标卡片区 */}
      <section aria-label="关键指标" className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
        {home.loading ? (
          <div className="col-span-full">
            <LoadingBlock text="加载指标…" />
          </div>
        ) : home.error ? (
          <div className="col-span-full">
            <ErrorState message={home.error} status={home.status} onRetry={home.refetch} />
          </div>
        ) : homeData && homeData.metric_cards.length > 0 ? (
          homeData.metric_cards.map((card) => (
            <MetricCard
              key={card.key}
              label={card.name}
              value={metricValue(card)}
              unit={card.unit === "%" || card.unit === "元" ? "" : card.unit}
            />
          ))
        ) : (
          <div className="col-span-full">
            <EmptyState title="暂无指标" description="当前角色未配置指标卡。" />
          </div>
        )}
      </section>

      {/* 待办 / 通知 + 风险清单 */}
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        {/* 待办与通知 */}
        <section aria-label="待办与通知" className="space-y-3">
          <div className="flex items-center justify-between">
            <h2 className="text-sm font-semibold text-slate-700">
              待办与通知
              {unreadCount > 0 ? (
                <span className="ml-2 inline-flex items-center rounded-full bg-rose-100 px-2 py-0.5 text-xs font-medium text-rose-700">
                  {unreadCount} 条未读
                </span>
              ) : null}
            </h2>
          </div>
          {notifications.loading ? (
            <LoadingBlock text="加载通知…" />
          ) : notifications.error ? (
            <ErrorState
              message={notifications.error}
              status={notifications.status}
              onRetry={notifications.refetch}
            />
          ) : notifData.length > 0 ? (
            <ul className="divide-y divide-slate-100 rounded-lg border border-slate-200 bg-white">
              {notifData.map((item) => (
                <li key={item.key} className="flex items-start gap-3 p-3">
                  <span
                    className={`mt-1 inline-block h-2 w-2 shrink-0 rounded-full ${
                      item.read ? "bg-slate-200" : "bg-rose-500"
                    }`}
                  />
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2">
                      <span className="text-xs text-slate-500">{label(item.category)}</span>
                      <StatusBadge tone="info" label={label(item.source)} />
                    </div>
                    <p className="mt-0.5 truncate text-sm text-slate-800">{item.title}</p>
                    <p className="mt-0.5 text-xs text-slate-400">
                      {formatDateTime(item.occurred_at)}
                    </p>
                  </div>
                  <div className="flex shrink-0 items-center gap-2">
                    {item.deep_link ? (
                      <Link
                        to={item.deep_link}
                        className="text-xs text-sky-600 hover:text-sky-800"
                      >
                        查看
                      </Link>
                    ) : null}
                    {!item.read ? (
                      <button
                        type="button"
                        onClick={() => handleMarkRead(item.key)}
                        disabled={markReadMut.submitting}
                        className="text-xs text-slate-500 hover:text-slate-700 disabled:opacity-50"
                      >
                        已读
                      </button>
                    ) : null}
                  </div>
                </li>
              ))}
            </ul>
          ) : (
            <EmptyState title="暂无待办" description="当前没有未处理的任务或通知。" />
          )}
        </section>

        {/* 风险清单（仅经理/督导可见） */}
        <section aria-label="风险清单" className="space-y-3">
          <h2 className="text-sm font-semibold text-slate-700">风险清单</h2>
          {home.loading ? (
            <LoadingBlock text="加载风险…" />
          ) : home.error ? null : homeData && homeData.risk_list.length > 0 ? (
            <ul className="divide-y divide-slate-100 rounded-lg border border-slate-200 bg-white">
              {homeData.risk_list.map((risk) => (
                <li key={risk.id} className="flex items-start gap-3 p-3">
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2">
                      <StatusBadge tone={riskTone(risk.severity)} label={risk.severity} />
                      <span className="text-xs text-slate-500">{risk.rule_code}</span>
                    </div>
                    <p className="mt-0.5 text-sm text-slate-800">
                      {label(risk.object_type)} · {risk.object_id}
                    </p>
                    <p className="mt-0.5 text-xs text-slate-400">
                      首次发现：{formatDateTime(risk.first_seen_at)}
                    </p>
                  </div>
                  {risk.deep_link ? (
                    <Link
                      to={risk.deep_link}
                      className="shrink-0 text-xs text-sky-600 hover:text-sky-800"
                    >
                      处置
                    </Link>
                  ) : null}
                </li>
              ))}
            </ul>
          ) : (
            <EmptyState
              title={homeData ? "暂无风险" : "风险清单"}
              description={
                homeData
                  ? "当前没有待处理的风险项。"
                  : "风险清单仅对片区经理与督导角色可见。"
              }
            />
          )}
        </section>
      </div>

      {/* 快捷动作 */}
      {homeData && homeData.quick_actions.length > 0 ? (
        <section aria-label="快捷动作">
          <h2 className="mb-3 text-sm font-semibold text-slate-700">快捷动作</h2>
          <div className="flex flex-wrap gap-2">
            {homeData.quick_actions.map((action) => (
              <Link
                key={action.path}
                to={action.path}
                className="rounded-lg border border-slate-200 bg-white px-4 py-2 text-sm font-medium text-slate-700 shadow-sm transition hover:border-sky-300 hover:bg-sky-50 hover:text-sky-700"
              >
                {action.label}
              </Link>
            ))}
          </div>
        </section>
      ) : null}

      {/* 数据截至时间 */}
      {homeData?.as_of ? (
        <p className="text-xs text-slate-400">数据截至：{formatDateTime(homeData.as_of)}</p>
      ) : null}

      <ToastStack items={toasts} onDismiss={dismiss} />
    </div>
  );
}
