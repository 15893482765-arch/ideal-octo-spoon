/** 风险清单（T21 RiskService · T27 深链落点 / PRD §4.11）。
 *
 * 接入后端：
 * - `GET /api/reporting/risks?scope=PRODUCTION&status=&rule_code=`（RiskService 判定结果）；
 * - `POST /api/reporting/risks/{id}/acknowledge`、`/{id}/close`（需 `risk.handle` 能力）。
 *
 * 深链（工作台风险卡 `deep_link`）携带 `status` / `rule_code` / `period` / `risk_id`，
 * 本页从 URL 复原筛选条件，避免"点了处置却丢失上下文"（Q157）；
 * 本页不做任何规则重算，也不在本地伪造风险项（数据不足由后端标注）。
 */
import { Fragment, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { api } from "../lib/api";
import { useAuth } from "../lib/auth";
import { formatDateTime, label, statusTone } from "../lib/format";
import { hasAbility, MODULE } from "../lib/permissions";
import { useApiQuery } from "../lib/useApi";
import { useToast } from "../lib/useToast";
import { ErrorState, LoadingBlock, SessionNotice } from "../components/AsyncState";
import { EmptyState } from "../components/EmptyState";
import { REPORT_SCOPE } from "../components/ExportPanel";
import { PageHeader } from "../components/PageHeader";
import { StatusBadge } from "../components/StatusBadge";
import { ToastStack } from "../components/Toast";

interface RiskView {
  id: string;
  rule_code: string;
  object_type: string;
  object_id: string;
  period: string;
  status: string;
  severity: string;
  first_seen_at: string;
  last_seen_at: string;
  closed_at: string | null;
  resolution: string | null;
  data_incomplete: boolean;
}

/** 规则中文名（与后端 RiskRuleCode 对齐）。 */
const RULE_LABELS: Record<string, string> = {
  "RK-1": "未按期巡访",
  "RK-2": "连续定位异常",
  "RK-3": "陈列连续不达标",
  "RK-4": "零销量",
  "RK-5": "补货超时未履约",
  "RK-6": "合同即将到期",
  "RK-7": "冰柜维修超时",
};

/** 对象类型中文名 + 可直达的详情页（无详情页的返回列表页）。 */
const OBJECT_LABELS: Record<string, string> = {
  MERCHANT: "商家",
  CONTRACT: "合同",
  FREEZER: "冰柜",
  REPLENISHMENT: "补货单",
};

const OBJECT_LINKS: Record<string, (id: string) => string> = {
  // 商家档案在商家中心以详情面板呈现（`/merchants/:id` 会重定向到这里）
  MERCHANT: (id) => `/merchants?merchant_id=${id}`,
  CONTRACT: (id) => `/contracts/${id}`,
  FREEZER: (id) => `/assets/freezers/${id}`,
  REPLENISHMENT: () => "/replenishments",
};

/** 工作台同一优先级口径（RK-1 最前）；OPEN 先于 ACKNOWLEDGED，同优先级早发现在前。 */
const RULE_PRIORITY: Record<string, number> = {
  "RK-1": 1,
  "RK-2": 2,
  "RK-3": 3,
  "RK-4": 4,
  "RK-5": 5,
  "RK-6": 6,
  "RK-7": 7,
};

const SEVERITY_TONE: Record<string, "danger" | "warning" | "info" | "neutral"> = {
  CRITICAL: "danger",
  HIGH: "danger",
  MEDIUM: "warning",
  LOW: "info",
};

function sortRisks(a: RiskView, b: RiskView): number {
  const byPriority = (RULE_PRIORITY[a.rule_code] ?? 99) - (RULE_PRIORITY[b.rule_code] ?? 99);
  if (byPriority !== 0) return byPriority;
  const byStatus = (a.status === "OPEN" ? 0 : 1) - (b.status === "OPEN" ? 0 : 1);
  if (byStatus !== 0) return byStatus;
  return a.first_seen_at.localeCompare(b.first_seen_at);
}

function errorMessage(err: unknown): string {
  return err instanceof Error && err.message ? err.message : "操作失败";
}

export function Risks() {
  const { authenticated, user } = useAuth();
  const { items: toasts, push, dismiss } = useToast();
  const [params, setParams] = useSearchParams();

  // URL 即筛选状态：深链直接生效，刷新/分享不丢上下文
  const status = params.get("status") ?? "";
  const ruleCode = params.get("rule_code") ?? "";
  const period = params.get("period") ?? "";
  const focusId = params.get("risk_id") ?? "";

  const [statusInput, setStatusInput] = useState(status);
  const [ruleInput, setRuleInput] = useState(ruleCode);
  const [periodInput, setPeriodInput] = useState(period);

  const [pendingId, setPendingId] = useState("");
  const [closingId, setClosingId] = useState("");
  const [resolution, setResolution] = useState("");

  const canHandle = hasAbility(user?.role ?? null, MODULE.RISK_HANDLE);

  const risks = useApiQuery<RiskView[]>(
    "/reporting/risks",
    { scope: REPORT_SCOPE, status, rule_code: ruleCode },
    { enabled: authenticated },
  );

  const rows = useMemo(() => {
    const all = risks.data ?? [];
    // 统计周期为月份（YYYY-MM）时按周期过滤；RK-6 的 period 是 EXP:到期日，不受月份过滤
    const filtered = all.filter(
      (r) => !period || r.period === period || r.period.startsWith("EXP:"),
    );
    return [...filtered].sort(sortRisks);
  }, [risks.data, period]);

  const openCount = rows.filter((r) => r.status === "OPEN").length;

  function applyFilters() {
    const next = new URLSearchParams();
    if (statusInput) next.set("status", statusInput);
    if (ruleInput.trim()) next.set("rule_code", ruleInput.trim());
    if (periodInput.trim()) next.set("period", periodInput.trim());
    if (focusId) next.set("risk_id", focusId);
    setParams(next, { replace: true });
  }

  function resetFilters() {
    setStatusInput("");
    setRuleInput("");
    setPeriodInput("");
    setParams(new URLSearchParams(), { replace: true });
  }

  async function acknowledge(id: string) {
    setPendingId(id);
    try {
      await api.post(`/reporting/risks/${id}/acknowledge`);
      push("已确认风险，进入处理中", "success");
      risks.refetch();
    } catch (err) {
      push(errorMessage(err), "danger");
    } finally {
      setPendingId("");
    }
  }

  async function closeRisk(id: string) {
    const text = resolution.trim();
    if (!text) {
      push("关闭风险必须填写处理结果", "warning");
      return;
    }
    setPendingId(id);
    try {
      await api.post(`/reporting/risks/${id}/close`, { resolution: text });
      push("风险已关闭并留痕", "success");
      setClosingId("");
      setResolution("");
      risks.refetch();
    } catch (err) {
      push(errorMessage(err), "danger");
    } finally {
      setPendingId("");
    }
  }

  if (!authenticated) {
    return (
      <div className="space-y-4">
        <PageHeader title="风险清单" description="风险判定结果与处置" />
        <SessionNotice hint="风险清单需要真实登录：后端按角色与数据域裁剪可见范围。" />
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <PageHeader
        title="风险清单"
        description="由 RiskService 按规则判定（RK-1～RK-7）并按数据域裁剪；本页只展示与处置，不重算规则"
        actions={
          <Link to="/workspace" className="text-sm text-sky-600 hover:text-sky-800">
            返回工作台
          </Link>
        }
      />

      <form
        role="search"
        onSubmit={(e) => {
          e.preventDefault();
          applyFilters();
        }}
        className="flex flex-wrap items-end gap-3 rounded-md border border-slate-200 bg-white p-3"
      >
        <label className="flex flex-col gap-1 text-xs text-slate-600">
          <span>状态</span>
          <select
            value={statusInput}
            onChange={(e) => setStatusInput(e.target.value)}
            className="w-32 rounded-md border border-slate-300 bg-white px-2 py-1.5 text-sm"
          >
            <option value="">全部</option>
            <option value="OPEN">待处理</option>
            <option value="ACKNOWLEDGED">已确认</option>
            <option value="CLOSED">已关闭</option>
          </select>
        </label>
        <label className="flex flex-col gap-1 text-xs text-slate-600">
          <span>规则</span>
          <select
            value={ruleInput}
            onChange={(e) => setRuleInput(e.target.value)}
            className="w-44 rounded-md border border-slate-300 bg-white px-2 py-1.5 text-sm"
          >
            <option value="">全部规则</option>
            {Object.entries(RULE_LABELS).map(([code, text]) => (
              <option key={code} value={code}>
                {code} {text}
              </option>
            ))}
          </select>
        </label>
        <label className="flex flex-col gap-1 text-xs text-slate-600">
          <span>统计周期</span>
          <input
            type="month"
            value={periodInput}
            onChange={(e) => setPeriodInput(e.target.value)}
            className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
          />
        </label>
        <div className="ml-auto flex gap-2">
          <button
            type="button"
            onClick={resetFilters}
            className="rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm text-slate-600 hover:bg-slate-50"
          >
            重置
          </button>
          <button
            type="submit"
            className="rounded-md bg-[var(--color-brand)] px-3 py-1.5 text-sm font-medium text-white hover:opacity-90"
          >
            应用
          </button>
        </div>
      </form>

      {focusId ? (
        <p className="text-xs text-sky-700">
          已定位到工作台处置的风险项；确认/关闭需 <code>risk.handle</code> 能力。
        </p>
      ) : null}

      {risks.loading ? <LoadingBlock text="加载风险清单…" /> : null}
      {risks.error ? (
        <ErrorState message={risks.error} status={risks.status} onRetry={risks.refetch} />
      ) : null}

      {!risks.loading && !risks.error ? (
        rows.length === 0 ? (
          <EmptyState
            title="没有符合条件的风险"
            description={
              period || status || ruleCode
                ? "当前筛选条件下没有风险项，可重置筛选查看全部。"
                : "RiskService 尚未判定出风险项。"
            }
          />
        ) : (
          <section className="space-y-2">
            <div className="text-xs text-slate-500">
              共 {rows.length} 条，其中待处理 {openCount} 条
            </div>
            <div className="overflow-x-auto rounded-md border border-slate-200 bg-white">
              <table className="min-w-full text-sm">
                <thead className="bg-slate-50 text-xs text-slate-500">
                  <tr>
                    <th className="px-3 py-2 text-left font-medium">规则</th>
                    <th className="px-3 py-2 text-left font-medium">对象</th>
                    <th className="px-3 py-2 text-left font-medium">统计周期</th>
                    <th className="px-3 py-2 text-left font-medium">状态</th>
                    <th className="px-3 py-2 text-left font-medium">等级</th>
                    <th className="px-3 py-2 text-left font-medium">首次发现</th>
                    <th className="px-3 py-2 text-left font-medium">操作</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100">
                  {rows.map((r) => {
                    const link = OBJECT_LINKS[r.object_type]?.(r.object_id);
                    return (
                      <Fragment key={r.id}>
                        <tr
                          className={
                            r.id === focusId
                              ? "bg-sky-50"
                              : r.status === "CLOSED"
                                ? "text-slate-400"
                                : undefined
                          }
                        >
                          <td className="px-3 py-2">
                            <div className="font-medium text-slate-700">
                              {RULE_LABELS[r.rule_code] ?? r.rule_code}
                            </div>
                            <div className="text-xs text-slate-400">{r.rule_code}</div>
                          </td>
                          <td className="px-3 py-2">
                            {link ? (
                              <Link to={link} className="text-sky-600 hover:text-sky-800">
                                {OBJECT_LABELS[r.object_type] ?? r.object_type}
                              </Link>
                            ) : (
                              <span>{OBJECT_LABELS[r.object_type] ?? r.object_type}</span>
                            )}
                            {r.id === focusId ? (
                              <span className="ml-2 rounded bg-sky-100 px-1.5 py-0.5 text-xs text-sky-700">
                                工作台处置项
                              </span>
                            ) : null}
                          </td>
                          <td className="px-3 py-2 text-xs text-slate-500">{r.period}</td>
                          <td className="px-3 py-2">
                            <StatusBadge tone={statusTone(r.status)} label={label(r.status)} />
                            {r.data_incomplete ? (
                              <span className="ml-2 text-xs text-amber-700">数据不足</span>
                            ) : null}
                          </td>
                          <td className="px-3 py-2">
                            <StatusBadge
                              tone={SEVERITY_TONE[r.severity] ?? "neutral"}
                              label={label(r.severity)}
                            />
                          </td>
                          <td className="px-3 py-2 text-xs text-slate-500">
                            {formatDateTime(r.first_seen_at)}
                          </td>
                          <td className="px-3 py-2">
                            {canHandle && r.status !== "CLOSED" ? (
                              <div className="flex gap-2">
                                {r.status === "OPEN" ? (
                                  <button
                                    type="button"
                                    disabled={pendingId === r.id}
                                    onClick={() => acknowledge(r.id)}
                                    className="rounded border border-slate-300 px-2 py-1 text-xs text-slate-700 hover:bg-slate-50 disabled:opacity-50"
                                  >
                                    确认
                                  </button>
                                ) : null}
                                <button
                                  type="button"
                                  disabled={pendingId === r.id}
                                  onClick={() => {
                                    setClosingId(closingId === r.id ? "" : r.id);
                                    setResolution("");
                                  }}
                                  className="rounded border border-slate-300 px-2 py-1 text-xs text-slate-700 hover:bg-slate-50 disabled:opacity-50"
                                >
                                  关闭
                                </button>
                              </div>
                            ) : (
                              <span className="text-xs text-slate-400">
                                {r.status === "CLOSED" ? r.resolution ?? "已关闭" : "只读"}
                              </span>
                            )}
                          </td>
                        </tr>
                        {closingId === r.id ? (
                          <tr className="bg-slate-50">
                            <td colSpan={7} className="px-3 py-2">
                              <div className="flex flex-wrap items-center gap-2">
                                <input
                                  value={resolution}
                                  onChange={(e) => setResolution(e.target.value)}
                                  placeholder="处理结果（必填，写入风险留痕）"
                                  className="min-w-64 flex-1 rounded-md border border-slate-300 px-2 py-1.5 text-sm"
                                />
                                <button
                                  type="button"
                                  disabled={pendingId === r.id}
                                  onClick={() => closeRisk(r.id)}
                                  className="rounded-md bg-[var(--color-brand)] px-3 py-1.5 text-sm text-white hover:opacity-90 disabled:opacity-50"
                                >
                                  提交关闭
                                </button>
                                <button
                                  type="button"
                                  onClick={() => {
                                    setClosingId("");
                                    setResolution("");
                                  }}
                                  className="rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm text-slate-600 hover:bg-slate-50"
                                >
                                  取消
                                </button>
                              </div>
                            </td>
                          </tr>
                        ) : null}
                      </Fragment>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </section>
        )
      ) : null}

      <ToastStack items={toasts} onDismiss={dismiss} />
    </div>
  );
}
