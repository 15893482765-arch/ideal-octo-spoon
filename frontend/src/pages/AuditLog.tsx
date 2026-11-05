/** 审计日志（T04 写入 → 本页只读查询 / PRD §2.1、§5、§8；R23、R24、R30）。
 *
 * 接入后端（`audit` 能力，按 PRD 角色职责只对系统管理员开放）：
 * - `GET /api/admin/audit-logs`：业务审计分页 + 多条件过滤；
 * - `GET /api/admin/audit-logs/auth-events`：认证域事件（登录失败/改密/凭据签发与消费/锁定/踢出）；
 * - `GET /api/admin/audit-logs/facets`：筛选项（对象类型取库中实际值 + 事件类型受控枚举）。
 *
 * 红线（页面遵从而不改写）：
 * - 审计只增不改：本页无任何编辑/删除入口；
 * - 审计与业务写操作同事务写入，因此"失败的写操作不会留下成功审计"；
 * - `before_json / after_json / payload` 入库前已过 redact()，页面原样展示，不做推断补齐；
 * - 凭据正文（激活/重置 token）永不入审计，页面自然不会出现。
 */
import { useMemo, useState } from "react";

import { useAuth } from "../lib/auth";
import { formatDateTime, label, statusTone, todayInTz } from "../lib/format";
import { useApiQuery } from "../lib/useApi";
import { useToast } from "../lib/useToast";
import { ErrorState, LoadingBlock, SessionNotice } from "../components/AsyncState";
import { DataTable, type DataTableColumn } from "../components/DataTable";
import { FilterBar } from "../components/FilterBar";
import { PageHeader } from "../components/PageHeader";
import { Pagination } from "../components/Pagination";
import { StatusBadge } from "../components/StatusBadge";
import { ToastStack } from "../components/Toast";

const BASE = "/admin/audit-logs";
const PAGE_SIZE = 20;

interface AuditLogRow {
  id: string;
  object_type: string;
  object_id: string;
  event_type: string;
  actor_id: string | null;
  actor_username: string | null;
  actor_name: string | null;
  occurred_at: string;
  request_id: string | null;
  reason: string | null;
  before_json: Record<string, unknown> | null;
  after_json: Record<string, unknown> | null;
  payload: Record<string, unknown>;
}

interface AuditLogPage {
  items: AuditLogRow[];
  total: number;
  limit: number;
  offset: number;
}

interface AuthEventRow {
  id: string;
  event_type: string;
  user_id: string | null;
  user_username: string | null;
  user_name: string | null;
  actor_id: string | null;
  actor_username: string | null;
  actor_name: string | null;
  occurred_at: string;
  request_id: string | null;
  payload: Record<string, unknown>;
}

interface AuthEventPage {
  items: AuthEventRow[];
  total: number;
  limit: number;
  offset: number;
}

interface Facets {
  object_types: string[];
  event_types: string[];
  auth_event_types: string[];
}

/** Asia/Shanghai 的 N 天前日期（YYYY-MM-DD）。 */
function dateMinusDays(days: number): string {
  const today = todayInTz();
  const d = new Date(`${today}T12:00:00+08:00`);
  d.setUTCDate(d.getUTCDate() - days);
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(d);
}

function dayStartIso(date: string): string | undefined {
  return date ? new Date(`${date}T00:00:00+08:00`).toISOString() : undefined;
}

function dayEndIso(date: string): string | undefined {
  return date ? new Date(`${date}T23:59:59.999+08:00`).toISOString() : undefined;
}

function shortId(value: string | null | undefined): string {
  return value ? value.slice(0, 8) : "—";
}

function JsonBlock({ title, value }: { title: string; value: unknown }) {
  return (
    <div>
      <div className="text-xs font-medium text-slate-600">{title}</div>
      <pre className="mt-1 max-h-64 overflow-auto rounded border border-slate-200 bg-slate-50 p-2 text-xs whitespace-pre-wrap">
        {JSON.stringify(value ?? null, null, 2)}
      </pre>
    </div>
  );
}

export function AuditLog() {
  const { authenticated } = useAuth();
  const { items: toasts, push, dismiss } = useToast();
  const [tab, setTab] = useState<"audit" | "auth">("audit");

  if (!authenticated) {
    return (
      <div className="space-y-4">
        <PageHeader title="审计日志" description="业务审计与认证事件只读查询" />
        <SessionNotice hint="审计日志需要真实登录后的系统管理员会话（后端按 audit 能力二次鉴权）。" />
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <PageHeader
        title="审计日志"
        description="只增不改：审计与业务写操作同事务；before/after 与 payload 入库前已脱敏，凭据正文永不入审计"
      />

      <div className="flex gap-1 rounded-md border border-slate-200 bg-white p-1">
        {(
          [
            ["audit", "业务审计"],
            ["auth", "认证事件"],
          ] as const
        ).map(([key, text]) => (
          <button
            key={key}
            type="button"
            onClick={() => setTab(key)}
            className={`rounded px-3 py-1.5 text-sm ${
              tab === key ? "bg-slate-100 font-medium text-slate-800" : "text-slate-600"
            }`}
          >
            {text}
          </button>
        ))}
      </div>

      {tab === "audit" ? <AuditQueryView onToast={push} /> : <AuthEventView onToast={push} />}

      <ToastStack items={toasts} onDismiss={dismiss} />
    </div>
  );
}

// ============================ 业务审计 ============================

interface AuditFilters {
  period_from: string;
  period_to: string;
  object_type: string;
  event_type: string;
  object_id: string;
  actor_keyword: string;
  keyword: string;
}

function AuditQueryView({ onToast }: { onToast: (m: string) => void }) {
  const [form, setForm] = useState<AuditFilters>({
    period_from: dateMinusDays(7),
    period_to: todayInTz(),
    object_type: "",
    event_type: "",
    object_id: "",
    actor_keyword: "",
    keyword: "",
  });
  const [applied, setApplied] = useState<AuditFilters>(form);
  const [offset, setOffset] = useState(0);
  const [detail, setDetail] = useState<AuditLogRow | null>(null);

  const facets = useApiQuery<Facets>(`${BASE}/facets`);

  const page = useApiQuery<AuditLogPage>(
    BASE,
    {
      object_type: applied.object_type || undefined,
      object_id: applied.object_id || undefined,
      event_type: applied.event_type || undefined,
      actor_keyword: applied.actor_keyword || undefined,
      keyword: applied.keyword || undefined,
      occurred_from: dayStartIso(applied.period_from),
      occurred_to: dayEndIso(applied.period_to),
      limit: PAGE_SIZE,
      offset,
    },
    { enabled: true },
  );

  const total = page.data?.total ?? 0;
  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const currentPage = Math.floor(offset / PAGE_SIZE) + 1;

  const columns: DataTableColumn<AuditLogRow>[] = [
    {
      key: "occurred_at",
      header: "时间",
      render: (r) => <span className="text-xs text-slate-600">{formatDateTime(r.occurred_at)}</span>,
    },
    {
      key: "event_type",
      header: "事件",
      render: (r) => <StatusBadge tone={statusTone(r.event_type)} label={label(r.event_type)} />,
    },
    {
      key: "object_type",
      header: "对象类型",
      render: (r) => <span className="text-xs">{r.object_type}</span>,
    },
    {
      key: "object_id",
      header: "对象 ID",
      render: (r) => <span className="font-mono text-xs text-slate-500">{shortId(r.object_id)}</span>,
    },
    {
      key: "actor",
      header: "操作人",
      render: (r) => (
        <span className="text-xs">
          {r.actor_name ?? r.actor_username ?? <span className="text-slate-400">系统</span>}
        </span>
      ),
    },
    {
      key: "reason",
      header: "原因",
      render: (r) => (
        <span className="text-xs text-slate-600">{r.reason ?? "—"}</span>
      ),
    },
    {
      key: "request_id",
      header: "请求 ID",
      render: (r) => <span className="font-mono text-xs text-slate-400">{shortId(r.request_id)}</span>,
    },
    {
      key: "actions",
      header: "明细",
      render: (r) => (
        <button
          type="button"
          onClick={() => setDetail(detail?.id === r.id ? null : r)}
          className="rounded-md border border-slate-300 bg-white px-2 py-1 text-xs text-slate-700 hover:bg-slate-50"
        >
          {detail?.id === r.id ? "收起" : "查看"}
        </button>
      ),
    },
  ];

  return (
    <div className="space-y-3">
      <FilterBar
        onApply={() => {
          setApplied(form);
          setOffset(0);
          setDetail(null);
        }}
        onReset={() => {
          const fresh: AuditFilters = {
            period_from: dateMinusDays(7),
            period_to: todayInTz(),
            object_type: "",
            event_type: "",
            object_id: "",
            actor_keyword: "",
            keyword: "",
          };
          setForm(fresh);
          setApplied(fresh);
          setOffset(0);
          setDetail(null);
        }}
      >
        <label className="flex flex-col gap-1 text-xs text-slate-600">
          <span>开始日期</span>
          <input
            type="date"
            value={form.period_from}
            onChange={(e) => setForm({ ...form, period_from: e.target.value })}
            className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
          />
        </label>
        <label className="flex flex-col gap-1 text-xs text-slate-600">
          <span>结束日期</span>
          <input
            type="date"
            value={form.period_to}
            onChange={(e) => setForm({ ...form, period_to: e.target.value })}
            className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
          />
        </label>
        <label className="flex flex-col gap-1 text-xs text-slate-600">
          <span>对象类型</span>
          <select
            value={form.object_type}
            onChange={(e) => setForm({ ...form, object_type: e.target.value })}
            className="w-40 rounded-md border border-slate-300 bg-white px-2 py-1.5 text-sm"
          >
            <option value="">全部</option>
            {(facets.data?.object_types ?? []).map((t) => (
              <option key={t} value={t}>
                {t}
              </option>
            ))}
          </select>
        </label>
        <label className="flex flex-col gap-1 text-xs text-slate-600">
          <span>事件类型</span>
          <select
            value={form.event_type}
            onChange={(e) => setForm({ ...form, event_type: e.target.value })}
            className="w-36 rounded-md border border-slate-300 bg-white px-2 py-1.5 text-sm"
          >
            <option value="">全部</option>
            {(facets.data?.event_types ?? []).map((t) => (
              <option key={t} value={t}>
                {label(t)}
              </option>
            ))}
          </select>
        </label>
        <label className="flex flex-col gap-1 text-xs text-slate-600">
          <span>对象 ID（精确）</span>
          <input
            value={form.object_id}
            onChange={(e) => setForm({ ...form, object_id: e.target.value })}
            placeholder="UUID"
            className="w-56 rounded-md border border-slate-300 px-2 py-1.5 font-mono text-sm"
          />
        </label>
        <label className="flex flex-col gap-1 text-xs text-slate-600">
          <span>操作人（用户名/姓名）</span>
          <input
            value={form.actor_keyword}
            onChange={(e) => setForm({ ...form, actor_keyword: e.target.value })}
            className="w-40 rounded-md border border-slate-300 px-2 py-1.5 text-sm"
          />
        </label>
        <label className="flex flex-col gap-1 text-xs text-slate-600">
          <span>原因关键字</span>
          <input
            value={form.keyword}
            onChange={(e) => setForm({ ...form, keyword: e.target.value })}
            className="w-40 rounded-md border border-slate-300 px-2 py-1.5 text-sm"
          />
        </label>
      </FilterBar>

      {page.loading ? <LoadingBlock text="查询审计日志…" /> : null}
      {page.error ? (
        <ErrorState message={page.error} status={page.status} onRetry={page.refetch} />
      ) : null}

      {!page.loading && !page.error ? (
        <>
          <div className="text-xs text-slate-500">
            共 {total} 条（当前第 {currentPage} / {totalPages} 页，单页 {PAGE_SIZE} 条）
          </div>
          <DataTable
            rows={page.data?.items ?? []}
            columns={columns}
            rowKey={(r) => r.id}
            emptyText="该条件下没有审计记录（可放宽时间区间或清空筛选）"
          />
          <Pagination
            page={currentPage}
            totalPages={totalPages}
            onPageChange={(p) => {
              setOffset((Math.max(1, Math.min(p, totalPages)) - 1) * PAGE_SIZE);
              setDetail(null);
            }}
          />
        </>
      ) : null}

      {detail ? (
        <section className="space-y-3 rounded-md border border-slate-200 bg-white p-4">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <h2 className="text-sm font-semibold text-slate-700">
              审计明细 · {label(detail.event_type)} · {detail.object_type}
            </h2>
            <button
              type="button"
              onClick={() => {
                navigator.clipboard
                  ?.writeText(JSON.stringify(detail, null, 2))
                  .then(
                    () => onToast("明细 JSON 已复制"),
                    () => onToast("复制失败，请手动选中"),
                  );
              }}
              className="rounded-md border border-slate-300 bg-white px-2 py-1 text-xs text-slate-700 hover:bg-slate-50"
            >
              复制 JSON
            </button>
          </div>
          <div className="grid grid-cols-1 gap-2 text-xs md:grid-cols-2">
            <div>
              <span className="text-slate-500">时间：</span>
              {formatDateTime(detail.occurred_at)}
            </div>
            <div>
              <span className="text-slate-500">操作人：</span>
              {detail.actor_name ?? detail.actor_username ?? "系统"}（
              {detail.actor_id ?? "无 actor"}）
            </div>
            <div className="font-mono break-all">
              <span className="font-sans text-slate-500">对象 ID：</span>
              {detail.object_id}
            </div>
            <div className="font-mono break-all">
              <span className="font-sans text-slate-500">请求 ID：</span>
              {detail.request_id ?? "—"}
            </div>
            <div className="md:col-span-2">
              <span className="text-slate-500">原因：</span>
              {detail.reason ?? "—"}
            </div>
          </div>
          <div className="grid grid-cols-1 gap-3 md:grid-cols-3">
            <JsonBlock title="变更前（已脱敏）" value={detail.before_json} />
            <JsonBlock title="变更后（已脱敏）" value={detail.after_json} />
            <JsonBlock title="附加载荷（已脱敏）" value={detail.payload} />
          </div>
        </section>
      ) : null}
    </div>
  );
}

// ============================ 认证事件 ============================

function AuthEventView({ onToast }: { onToast: (m: string) => void }) {
  const [form, setForm] = useState({
    period_from: dateMinusDays(7),
    period_to: todayInTz(),
    event_type: "",
    user_keyword: "",
  });
  const [applied, setApplied] = useState(form);
  const [offset, setOffset] = useState(0);
  const [detail, setDetail] = useState<AuthEventRow | null>(null);

  const facets = useApiQuery<Facets>(`${BASE}/facets`);

  const page = useApiQuery<AuthEventPage>(
    `${BASE}/auth-events`,
    {
      event_type: applied.event_type || undefined,
      user_keyword: applied.user_keyword || undefined,
      occurred_from: dayStartIso(applied.period_from),
      occurred_to: dayEndIso(applied.period_to),
      limit: PAGE_SIZE,
      offset,
    },
    { enabled: true },
  );

  const total = page.data?.total ?? 0;
  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const currentPage = Math.floor(offset / PAGE_SIZE) + 1;

  const columns: DataTableColumn<AuthEventRow>[] = useMemo(
    () => [
      {
        key: "occurred_at",
        header: "时间",
        render: (r) => (
          <span className="text-xs text-slate-600">{formatDateTime(r.occurred_at)}</span>
        ),
      },
      {
        key: "event_type",
        header: "认证事件",
        render: (r) => <StatusBadge tone={statusTone(r.event_type)} label={label(r.event_type)} />,
      },
      {
        key: "user",
        header: "目标用户",
        render: (r) => (
          <span className="text-xs">
            {r.user_name ?? r.user_username ?? <span className="text-slate-400">未登录用户</span>}
          </span>
        ),
      },
      {
        key: "actor",
        header: "操作人",
        render: (r) => (
          <span className="text-xs">
            {r.actor_name ?? r.actor_username ?? <span className="text-slate-400">本人 / 系统</span>}
          </span>
        ),
      },
      {
        key: "request_id",
        header: "请求 ID",
        render: (r) => (
          <span className="font-mono text-xs text-slate-400">{shortId(r.request_id)}</span>
        ),
      },
      {
        key: "payload",
        header: "附加载荷",
        render: (r) => (
          <span className="text-xs text-slate-500">
            {Object.keys(r.payload ?? {}).length > 0 ? JSON.stringify(r.payload) : "—"}
          </span>
        ),
      },
      {
        key: "actions",
        header: "明细",
        render: (r) => (
          <button
            type="button"
            onClick={() => setDetail(detail?.id === r.id ? null : r)}
            className="rounded-md border border-slate-300 bg-white px-2 py-1 text-xs text-slate-700 hover:bg-slate-50"
          >
            {detail?.id === r.id ? "收起" : "查看"}
          </button>
        ),
      },
    ],
    [detail],
  );

  return (
    <div className="space-y-3">
      <FilterBar
        onApply={() => {
          setApplied(form);
          setOffset(0);
          setDetail(null);
        }}
        onReset={() => {
          const fresh = {
            period_from: dateMinusDays(7),
            period_to: todayInTz(),
            event_type: "",
            user_keyword: "",
          };
          setForm(fresh);
          setApplied(fresh);
          setOffset(0);
          setDetail(null);
        }}
      >
        <label className="flex flex-col gap-1 text-xs text-slate-600">
          <span>开始日期</span>
          <input
            type="date"
            value={form.period_from}
            onChange={(e) => setForm({ ...form, period_from: e.target.value })}
            className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
          />
        </label>
        <label className="flex flex-col gap-1 text-xs text-slate-600">
          <span>结束日期</span>
          <input
            type="date"
            value={form.period_to}
            onChange={(e) => setForm({ ...form, period_to: e.target.value })}
            className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
          />
        </label>
        <label className="flex flex-col gap-1 text-xs text-slate-600">
          <span>认证事件</span>
          <select
            value={form.event_type}
            onChange={(e) => setForm({ ...form, event_type: e.target.value })}
            className="w-44 rounded-md border border-slate-300 bg-white px-2 py-1.5 text-sm"
          >
            <option value="">全部</option>
            {(facets.data?.auth_event_types ?? []).map((t) => (
              <option key={t} value={t}>
                {label(t)}
              </option>
            ))}
          </select>
        </label>
        <label className="flex flex-col gap-1 text-xs text-slate-600">
          <span>用户 / 操作人（用户名或姓名）</span>
          <input
            value={form.user_keyword}
            onChange={(e) => setForm({ ...form, user_keyword: e.target.value })}
            className="w-48 rounded-md border border-slate-300 px-2 py-1.5 text-sm"
          />
        </label>
      </FilterBar>

      {page.loading ? <LoadingBlock text="查询认证事件…" /> : null}
      {page.error ? (
        <ErrorState message={page.error} status={page.status} onRetry={page.refetch} />
      ) : null}

      {!page.loading && !page.error ? (
        <>
          <div className="text-xs text-slate-500">
            共 {total} 条（当前第 {currentPage} / {totalPages} 页）
          </div>
          <DataTable
            rows={page.data?.items ?? []}
            columns={columns}
            rowKey={(r) => r.id}
            emptyText="该条件下没有认证事件"
          />
          <Pagination
            page={currentPage}
            totalPages={totalPages}
            onPageChange={(p) => {
              setOffset((Math.max(1, Math.min(p, totalPages)) - 1) * PAGE_SIZE);
              setDetail(null);
            }}
          />
        </>
      ) : null}

      {detail ? (
        <section className="space-y-2 rounded-md border border-slate-200 bg-white p-4">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <h2 className="text-sm font-semibold text-slate-700">
              认证事件明细 · {label(detail.event_type)}
            </h2>
            <button
              type="button"
              onClick={() => {
                navigator.clipboard
                  ?.writeText(JSON.stringify(detail, null, 2))
                  .then(
                    () => onToast("明细 JSON 已复制"),
                    () => onToast("复制失败，请手动选中"),
                  );
              }}
              className="rounded-md border border-slate-300 bg-white px-2 py-1 text-xs text-slate-700 hover:bg-slate-50"
            >
              复制 JSON
            </button>
          </div>
          <div className="grid grid-cols-1 gap-2 text-xs md:grid-cols-2">
            <div>
              <span className="text-slate-500">时间：</span>
              {formatDateTime(detail.occurred_at)}
            </div>
            <div className="font-mono break-all">
              <span className="font-sans text-slate-500">请求 ID：</span>
              {detail.request_id ?? "—"}
            </div>
            <div>
              <span className="text-slate-500">目标用户：</span>
              {detail.user_name ?? detail.user_username ?? "—"}
            </div>
            <div>
              <span className="text-slate-500">操作人：</span>
              {detail.actor_name ?? detail.actor_username ?? "本人 / 系统"}
            </div>
          </div>
          <JsonBlock title="附加载荷（已脱敏）" value={detail.payload} />
        </section>
      ) : null}
    </div>
  );
}
