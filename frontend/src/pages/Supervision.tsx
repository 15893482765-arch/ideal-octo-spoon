/** 督导跟访与整改（T16 前端联调 / PRD §4.8 / R14 / R15）。
 *
 * 接入后端：
 * - `GET /api/supervision/visits`（跟访计划/记录列表）、`GET /api/supervision/visits/{id}`（详情）；
 * - `GET /api/supervision/rectifications`（片区整改列表）、`POST /rectifications/{id}/{start|submit|verify|close}`；
 * - `GET /api/supervision/weekly-coverage?region_id=&week_start=`（每周覆盖口径 R15）；
 * - `POST /api/supervision/visits`（建计划）、`POST /visits/{id}/submit`（现场记录 + 生成整改）；
 * - 人员下拉 `GET /api/supervision/options/users?region_id=&role_code=`，商家下拉复用 `GET /api/merchants`。
 *
 * 红线：督导跟访是独立事实（不替代业务员巡访、不计覆盖率，R14）；验证/关闭人不能是整改
 * 提交人（后端硬门禁，前端仅作可见性提示）；覆盖达标 = 同一业务周内跟访 ≥2 次且覆盖 ≥2 名
 * 不同业务员（R15）。页面只做展示与受控交互。
 */
import { useMemo, useState } from "react";

import { api } from "../lib/api";
import { useAuth } from "../lib/auth";
import { formatDate, formatDateTime, label, statusTone } from "../lib/format";
import { useApiMutation, useApiQuery } from "../lib/useApi";
import { useToast } from "../lib/useToast";
import { ErrorState, LoadingBlock, SessionNotice } from "../components/AsyncState";
import { DataTable, type DataTableColumn } from "../components/DataTable";
import { EmptyState } from "../components/EmptyState";
import { FilterBar } from "../components/FilterBar";
import { MetricCard } from "../components/MetricCard";
import { PageHeader } from "../components/PageHeader";
import { Pagination } from "../components/Pagination";
import { StatusBadge } from "../components/StatusBadge";
import { ToastStack } from "../components/Toast";

/** 受控枚举（与后端 SupervisionVisitStatus / RectificationStatus / IssueCategory 对齐）。 */
const VISIT_STATUS_OPTIONS = ["PLANNED", "SUBMITTED"] as const;
const RECT_STATUS_OPTIONS = ["OPEN", "IN_PROGRESS", "SUBMITTED", "VERIFIED", "CLOSED"] as const;
const CATEGORY_OPTIONS = ["DISPLAY", "SERVICE", "EQUIPMENT", "OTHER"] as const;

const RECT_CLOSED = new Set(["VERIFIED", "CLOSED"]);

interface OptionItem {
  id: string;
  code: string;
  name: string;
}

interface VisitRow {
  id: string;
  region_id: string;
  region_name: string;
  status: string;
  supervisor_id: string;
  supervisor_name: string | null;
  shadowed_user_id: string;
  shadowed_user_name: string | null;
  merchant_id: string;
  merchant_name: string;
  planned_date: string;
  visited_at: string | null;
  photo_count: number;
  action_score: number | null;
  note: string | null;
  rectification_count: number;
  pending_rectification_count: number;
}

interface RectRow {
  id: string;
  supervision_visit_id: string;
  merchant_id: string;
  region_id: string;
  category: string;
  description: string;
  owner_user_id: string;
  due_date: string;
  status: string;
  submitter_id: string | null;
  verifier_id: string | null;
}

interface RectListItem extends RectRow {
  region_name: string;
  merchant_name: string;
  owner_name: string | null;
  overdue: boolean;
}

interface VisitDetail {
  id: string;
  region_id: string;
  status: string;
  supervisor_id: string;
  shadowed_user_id: string;
  merchant_id: string;
  planned_date: string;
  visited_at: string | null;
  photo_count: number;
  action_score: number | null;
  display_issue: string | null;
  service_issue: string | null;
  equipment_issue: string | null;
  note: string | null;
  rectifications: RectRow[];
}

interface CoverageRow {
  region_id: string;
  supervisor_id: string;
  week_start: string;
  visit_count: number;
  distinct_rep_count: number;
  compliant: boolean;
  warnings: string[];
}

interface ListResponse<T> {
  items: T[];
  pagination: { page: number; page_size: number; total: number };
}

interface IssueInput {
  category: string;
  description: string;
  owner_user_id: string;
  due_date: string;
}

function currentWeekMonday(): string {
  const today = new Date();
  const dow = (today.getUTCDay() + 6) % 7;
  today.setUTCDate(today.getUTCDate() - dow);
  return today.toISOString().slice(0, 10);
}

export function Supervision() {
  const { authenticated, user } = useAuth();
  const { items: toasts, push, dismiss } = useToast();

  const [tab, setTab] = useState<"visits" | "rectifications" | "coverage">("visits");

  const role = user?.role;
  // 后端 MODULE_SUPERVISION 只授予督导/片区经理（系统管理员不含该模块能力）
  const canManage = role === "SUPERVISOR" || role === "REGIONAL_MANAGER";

  const regions = useApiQuery<{ items: OptionItem[] }>("/master-data/regions", undefined, {
    enabled: authenticated,
  });

  return (
    <div className="space-y-4">
      <PageHeader
        title="督导跟访与整改"
        description="周跟访计划 → 现场记录与问题整改单 → 非同人验证关闭；跟访是独立事实，不替代业务员巡访"
      />

      {!authenticated ? (
        <SessionNotice hint="当前是开发注入账号，请从登录页用真实账号登录后再查看业务数据。" />
      ) : (
        <>
          {role === "SYSTEM_ADMIN" ? (
            <p className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800">
              系统管理员未被授予督导模块能力（后端 RBAC），本页数据接口会返回 403。如需访问，
              应由权限矩阵显式授予 <code>supervision</code> 能力。
            </p>
          ) : null}

          <div className="flex gap-1 rounded-md border border-slate-200 bg-white p-1">
            {(
              [
                ["visits", "跟访计划"],
                ["rectifications", "整改闭环"],
                ["coverage", "每周覆盖"],
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

          {tab === "visits" ? (
            <VisitsTab
              regions={regions.data?.items ?? []}
              canManage={canManage}
              onToast={push}
            />
          ) : null}
          {tab === "rectifications" ? (
            <RectificationsTab
              regions={regions.data?.items ?? []}
              canManage={canManage}
              onToast={push}
            />
          ) : null}
          {tab === "coverage" ? <CoverageTab regions={regions.data?.items ?? []} /> : null}
        </>
      )}

      <ToastStack items={toasts} onDismiss={dismiss} />
    </div>
  );
}

function Field({ label: text, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="flex flex-col gap-1 text-xs text-slate-600">
      <span>{text}</span>
      {children}
    </label>
  );
}

function useRegionUsers(regionId: string, roleCode: "SALES_REP" | "SUPERVISOR") {
  const query = useApiQuery<{ items: OptionItem[] }>(
    "/supervision/options/users",
    { region_id: regionId, role_code: roleCode },
    { enabled: Boolean(regionId) },
  );
  const items = useMemo(() => query.data?.items ?? [], [query.data]);
  return { items, loading: query.loading };
}

// ============================ 跟访计划 ============================

function VisitsTab({
  regions,
  canManage,
  onToast,
}: {
  regions: OptionItem[];
  canManage: boolean;
  onToast: (message: string, tone?: "info" | "success" | "warning" | "danger") => void;
}) {
  const [regionId, setRegionId] = useState("");
  const [statusInput, setStatusInput] = useState("");
  const [supervisorInput, setSupervisorInput] = useState("");
  const [fromInput, setFromInput] = useState("");
  const [toInput, setToInput] = useState("");
  const [page, setPage] = useState(1);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);

  const effectiveRegion = regionId || regions[0]?.id || "";

  const list = useApiQuery<ListResponse<VisitRow>>(
    "/supervision/visits",
    {
      region_id: regionId,
      status: statusInput,
      supervisor_id: supervisorInput,
      planned_from: fromInput,
      planned_to: toInput,
      page,
      page_size: 20,
    },
    { enabled: true },
  );

  const detail = useApiQuery<VisitDetail>(
    `/supervision/visits/${selectedId ?? ""}`,
    undefined,
    { enabled: Boolean(selectedId) },
  );

  const supervisors = useRegionUsers(effectiveRegion, "SUPERVISOR");
  const reps = useRegionUsers(effectiveRegion, "SALES_REP");
  const merchants = useApiQuery<{ items: { id: string; code: string; name: string }[] }>(
    "/merchants",
    { region_id: effectiveRegion, page: 1, page_size: 100 },
    { enabled: Boolean(effectiveRegion) },
  );

  const planMut = useApiMutation(
    (p: {
      region_id: string;
      shadowed_user_id: string;
      merchant_id: string;
      planned_date: string;
    }) => api.post<VisitDetail>("/supervision/visits", p),
  );
  const submitMut = useApiMutation(
    (p: {
      id: string;
      action_score: number;
      photo_count: number;
      visited_at: string;
      display_issue: string | null;
      service_issue: string | null;
      equipment_issue: string | null;
      note: string | null;
      issues: IssueInput[];
    }) =>
      api.post<VisitDetail>(`/supervision/visits/${p.id}/submit`, {
        action_score: p.action_score,
        photo_count: p.photo_count,
        visited_at: p.visited_at,
        display_issue: p.display_issue,
        service_issue: p.service_issue,
        equipment_issue: p.equipment_issue,
        note: p.note,
        issues: p.issues,
      }),
  );

  const repName = useMemo(() => {
    const map = new Map(reps.items.map((r) => [r.id, r.name]));
    return (id: string) => map.get(id) ?? id.slice(0, 8);
  }, [reps.items]);
  const supervisorName = useMemo(() => {
    const map = new Map(supervisors.items.map((s) => [s.id, s.name]));
    return (id: string) => map.get(id) ?? id.slice(0, 8);
  }, [supervisors.items]);

  const total = list.data?.pagination.total ?? 0;
  const totalPages = Math.max(1, Math.ceil(total / (list.data?.pagination.page_size ?? 20)));

  const columns: DataTableColumn<VisitRow>[] = [
    { key: "planned_date", header: "计划日", render: (r) => formatDate(r.planned_date) },
    {
      key: "merchant",
      header: "商家",
      render: (r) => (
        <button
          type="button"
          onClick={() => setSelectedId(r.id)}
          className="text-left font-medium text-[var(--color-brand)] hover:underline"
        >
          {r.merchant_name}
        </button>
      ),
    },
    {
      key: "rep",
      header: "被跟访业务员",
      render: (r) => r.shadowed_user_name ?? repName(r.shadowed_user_id),
    },
    {
      key: "supervisor",
      header: "督导/记录人",
      render: (r) => r.supervisor_name ?? supervisorName(r.supervisor_id),
    },
    { key: "region", header: "片区", render: (r) => r.region_name },
    {
      key: "score",
      header: "动作评分",
      align: "right",
      render: (r) => (r.action_score === null ? "—" : r.action_score),
    },
    {
      key: "rect",
      header: "整改（未闭环/总）",
      align: "right",
      render: (r) => (
        <span className={r.pending_rectification_count > 0 ? "text-amber-700" : "text-slate-600"}>
          {r.pending_rectification_count} / {r.rectification_count}
        </span>
      ),
    },
    {
      key: "status",
      header: "状态",
      render: (r) => <StatusBadge tone={statusTone(r.status)} label={label(r.status)} />,
    },
  ];

  return (
    <div className="space-y-4">
      {canManage ? (
        <div className="flex justify-end">
          <button
            type="button"
            onClick={() => setCreating((v) => !v)}
            className="rounded-md bg-[var(--color-brand)] px-3 py-1.5 text-sm font-medium text-white hover:opacity-90"
          >
            {creating ? "收起新建" : "新建跟访计划"}
          </button>
        </div>
      ) : null}

      {creating && canManage ? (
        <PlanForm
          regions={regions}
          reps={reps.items}
          merchants={merchants.data?.items ?? []}
          submitting={planMut.submitting}
          error={planMut.error}
          onCancel={() => setCreating(false)}
          onSubmit={async (payload) => {
            const res = await planMut.run(payload);
            if (res) {
              onToast("跟访计划已创建（PLANNED）", "success");
              setCreating(false);
              setSelectedId(res.id);
              list.refetch();
            } else {
              onToast(planMut.error || "创建失败", "danger");
            }
          }}
        />
      ) : null}

      <FilterBar
        onApply={() => setPage(1)}
        onReset={() => {
          setStatusInput("");
          setSupervisorInput("");
          setFromInput("");
          setToInput("");
          setPage(1);
        }}
      >
        <Field label="片区">
          <select
            value={regionId}
            onChange={(e) => {
              setRegionId(e.target.value);
              setSupervisorInput("");
              setPage(1);
            }}
            className="rounded-md border border-slate-300 bg-white px-2 py-1.5 text-sm"
          >
            <option value="">全部可见片区</option>
            {regions.map((r) => (
              <option key={r.id} value={r.id}>
                {r.code} · {r.name}
              </option>
            ))}
          </select>
        </Field>
        <Field label="状态">
          <select
            value={statusInput}
            onChange={(e) => {
              setStatusInput(e.target.value);
              setPage(1);
            }}
            className="rounded-md border border-slate-300 bg-white px-2 py-1.5 text-sm"
          >
            <option value="">全部</option>
            {VISIT_STATUS_OPTIONS.map((s) => (
              <option key={s} value={s}>
                {label(s)}
              </option>
            ))}
          </select>
        </Field>
        <Field label="督导">
          <select
            value={supervisorInput}
            onChange={(e) => {
              setSupervisorInput(e.target.value);
              setPage(1);
            }}
            className="rounded-md border border-slate-300 bg-white px-2 py-1.5 text-sm"
          >
            <option value="">全部</option>
            {supervisors.items.map((s) => (
              <option key={s.id} value={s.id}>
                {s.name}
              </option>
            ))}
          </select>
        </Field>
        <Field label="计划日（起）">
          <input
            type="date"
            value={fromInput}
            onChange={(e) => setFromInput(e.target.value)}
            className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
          />
        </Field>
        <Field label="计划日（止）">
          <input
            type="date"
            value={toInput}
            onChange={(e) => setToInput(e.target.value)}
            className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
          />
        </Field>
      </FilterBar>

      {list.loading ? <LoadingBlock text="加载跟访列表…" /> : null}
      {list.error ? (
        <ErrorState message={list.error} status={list.status} onRetry={list.refetch} />
      ) : null}

      {!list.loading && !list.error ? (
        <>
          <div className="text-xs text-slate-500">
            共 {total} 条跟访记录（计划未提交不计入每周覆盖）
          </div>
          <DataTable
            rows={list.data?.items ?? []}
            columns={columns}
            rowKey={(r) => r.id}
            emptyText={
              canManage
                ? "暂无跟访记录（可用右上角新建计划）"
                : "暂无可见跟访记录（跟访计划由督导/片区经理创建）"
            }
          />
          <Pagination page={page} totalPages={totalPages} onPageChange={setPage} />
        </>
      ) : null}

      {selectedId ? (
        <VisitDetailPanel
          detail={detail.data}
          loading={detail.loading}
          error={detail.error}
          status={detail.status}
          onClose={() => setSelectedId(null)}
          onRetry={detail.refetch}
          canManage={canManage}
          reps={reps.items}
          repName={repName}
          merchantName={(id) =>
            (merchants.data?.items ?? []).find((m) => m.id === id)?.name ?? id.slice(0, 8)
          }
          submitting={submitMut.submitting}
          actionError={submitMut.error}
          onSubmit={async (payload) => {
            const res = await submitMut.run(payload);
            if (res) {
              onToast(
                `现场记录已提交，生成整改单 ${res.rectifications.length} 条`,
                "success",
              );
              detail.refetch();
              list.refetch();
            } else {
              onToast(submitMut.error || "提交失败", "danger");
            }
          }}
        />
      ) : null}
    </div>
  );
}

function PlanForm({
  regions,
  reps,
  merchants,
  submitting,
  error,
  onSubmit,
  onCancel,
}: {
  regions: OptionItem[];
  reps: OptionItem[];
  merchants: { id: string; code: string; name: string }[];
  submitting: boolean;
  error: string;
  onSubmit: (p: {
    region_id: string;
    shadowed_user_id: string;
    merchant_id: string;
    planned_date: string;
  }) => void;
  onCancel: () => void;
}) {
  const [regionId, setRegionId] = useState(regions[0]?.id ?? "");
  const [repId, setRepId] = useState("");
  const [merchantId, setMerchantId] = useState("");
  const [plannedDate, setPlannedDate] = useState(currentWeekMonday());
  const [formError, setFormError] = useState("");

  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        if (!regionId || !repId || !merchantId || !plannedDate) {
          setFormError("请选择片区、被跟访业务员、商家与计划日");
          return;
        }
        setFormError("");
        onSubmit({
          region_id: regionId,
          shadowed_user_id: repId,
          merchant_id: merchantId,
          planned_date: plannedDate,
        });
      }}
      className="space-y-3 rounded-md border border-slate-200 bg-white p-4"
    >
      <div className="text-sm font-medium text-slate-700">新建督导跟访计划</div>
      <p className="text-xs text-slate-500">
        跟访计划只记录督导自身动作；不生成/不修改业务员的巡访任务与记录（R14）。
      </p>
      <div className="grid grid-cols-1 gap-3 md:grid-cols-4">
        <Field label="片区 *">
          <select
            required
            value={regionId}
            onChange={(e) => {
              setRegionId(e.target.value);
              setRepId("");
              setMerchantId("");
            }}
            className="rounded-md border border-slate-300 bg-white px-2 py-1.5 text-sm"
          >
            <option value="">请选择</option>
            {regions.map((r) => (
              <option key={r.id} value={r.id}>
                {r.code} · {r.name}
              </option>
            ))}
          </select>
        </Field>
        <Field label="被跟访业务员 *">
          <select
            required
            value={repId}
            onChange={(e) => setRepId(e.target.value)}
            className="rounded-md border border-slate-300 bg-white px-2 py-1.5 text-sm"
          >
            <option value="">请选择</option>
            {reps.map((u) => (
              <option key={u.id} value={u.id}>
                {u.name}（{u.code}）
              </option>
            ))}
          </select>
        </Field>
        <Field label="商家 *">
          <select
            required
            value={merchantId}
            onChange={(e) => setMerchantId(e.target.value)}
            className="rounded-md border border-slate-300 bg-white px-2 py-1.5 text-sm"
          >
            <option value="">请选择</option>
            {merchants.map((m) => (
              <option key={m.id} value={m.id}>
                {m.code} · {m.name}
              </option>
            ))}
          </select>
        </Field>
        <Field label="计划日 *">
          <input
            required
            type="date"
            value={plannedDate}
            onChange={(e) => setPlannedDate(e.target.value)}
            className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
          />
        </Field>
      </div>
      {reps.length === 0 ? (
        <p className="text-xs text-amber-700">
          该片区暂无可选业务员（需先由管理员建号并授权片区）。
        </p>
      ) : null}
      {merchants.length === 0 ? (
        <p className="text-xs text-amber-700">该片区暂无商家（需先导入主数据/建商家）。</p>
      ) : null}
      {formError ? <div className="text-xs text-rose-600">{formError}</div> : null}
      {error ? <div className="text-xs text-rose-600">{error}</div> : null}
      <div className="flex gap-2">
        <button
          type="submit"
          disabled={submitting}
          className="rounded-md bg-[var(--color-brand)] px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50"
        >
          {submitting ? "提交中…" : "创建计划"}
        </button>
        <button
          type="button"
          onClick={onCancel}
          className="rounded-md border border-slate-300 px-3 py-1.5 text-sm text-slate-600"
        >
          取消
        </button>
      </div>
    </form>
  );
}

function VisitDetailPanel({
  detail,
  loading,
  error,
  status,
  onClose,
  onRetry,
  canManage,
  reps,
  repName,
  merchantName,
  submitting,
  actionError,
  onSubmit,
}: {
  detail: VisitDetail | null;
  loading: boolean;
  error: string;
  status: number | null;
  onClose: () => void;
  onRetry: () => void;
  canManage: boolean;
  reps: OptionItem[];
  repName: (id: string) => string;
  merchantName: (id: string) => string;
  submitting: boolean;
  actionError: string;
  onSubmit: (p: {
    id: string;
    action_score: number;
    photo_count: number;
    visited_at: string;
    display_issue: string | null;
    service_issue: string | null;
    equipment_issue: string | null;
    note: string | null;
    issues: IssueInput[];
  }) => void;
}) {
  const [score, setScore] = useState("80");
  const [photoCount, setPhotoCount] = useState("0");
  const [visitedAt, setVisitedAt] = useState("");
  const [displayIssue, setDisplayIssue] = useState("");
  const [serviceIssue, setServiceIssue] = useState("");
  const [equipmentIssue, setEquipmentIssue] = useState("");
  const [note, setNote] = useState("");
  const [issues, setIssues] = useState<IssueInput[]>([]);
  const [formError, setFormError] = useState("");

  const planned = detail?.status === "PLANNED";

  return (
    <section className="space-y-3 rounded-md border border-slate-200 bg-white p-4">
      <div className="flex items-start justify-between">
        <h2 className="text-sm font-semibold text-slate-700">跟访详情</h2>
        <button type="button" onClick={onClose} className="text-xs text-slate-500 hover:underline">
          关闭
        </button>
      </div>

      {loading ? <LoadingBlock text="加载跟访详情…" /> : null}
      {error ? <ErrorState message={error} status={status} onRetry={onRetry} /> : null}

      {detail ? (
        <>
          <div className="grid grid-cols-2 gap-3 text-sm md:grid-cols-4">
            <Info label="计划日" value={formatDate(detail.planned_date)} />
            <Info
              label="状态"
              value={<StatusBadge tone={statusTone(detail.status)} label={label(detail.status)} />}
            />
            <Info label="商家" value={merchantName(detail.merchant_id)} />
            <Info label="被跟访业务员" value={repName(detail.shadowed_user_id)} />
            <Info label="督导/记录人" value={repName(detail.supervisor_id)} />
            <Info label="现场时间" value={formatDateTime(detail.visited_at)} />
            <Info label="照片数" value={String(detail.photo_count)} />
            <Info label="动作评分" value={detail.action_score === null ? "—" : detail.action_score} />
            <Info label="陈列问题" value={detail.display_issue ?? "—"} />
            <Info label="服务问题" value={detail.service_issue ?? "—"} />
            <Info label="设备问题" value={detail.equipment_issue ?? "—"} />
            <Info label="现场说明" value={detail.note ?? "—"} />
          </div>

          <div>
            <div className="text-xs font-medium text-slate-600">
              整改项（{detail.rectifications.length} 条）
            </div>
            <div className="mt-2">
              <DataTable
                rows={detail.rectifications}
                columns={[
                  { key: "cat", header: "分类", render: (r) => label(r.category) },
                  { key: "desc", header: "问题描述", render: (r) => r.description },
                  { key: "owner", header: "责任人", render: (r) => repName(r.owner_user_id) },
                  { key: "due", header: "截止日", render: (r) => formatDate(r.due_date) },
                  {
                    key: "status",
                    header: "状态",
                    render: (r) => (
                      <StatusBadge tone={statusTone(r.status)} label={label(r.status)} />
                    ),
                  },
                ]}
                rowKey={(r) => r.id}
                emptyText="现场记录未提交或未发现问题"
              />
            </div>
          </div>

          {!canManage ? (
            <p className="rounded-md border border-slate-200 bg-slate-50 px-3 py-2 text-xs text-slate-600">
              当前角色只读：跟访计划与现场记录由督导/片区经理维护（后端二次鉴权）。
            </p>
          ) : null}

          {actionError ? <div className="text-xs text-rose-600">{actionError}</div> : null}

          {canManage && planned ? (
            <form
              onSubmit={(e) => {
                e.preventDefault();
                const scoreNum = Number(score);
                if (Number.isNaN(scoreNum) || scoreNum < 0 || scoreNum > 100) {
                  setFormError("动作评分必须是 0-100 的整数");
                  return;
                }
                if (!visitedAt) {
                  setFormError("请填写现场时间");
                  return;
                }
                if (issues.some((i) => !i.owner_user_id || !i.due_date || !i.description.trim())) {
                  setFormError("整改项需要填齐责任人、截止日与问题描述");
                  return;
                }
                setFormError("");
                onSubmit({
                  id: detail.id,
                  action_score: scoreNum,
                  photo_count: Number(photoCount || "0"),
                  visited_at: visitedAt,
                  display_issue: displayIssue.trim() || null,
                  service_issue: serviceIssue.trim() || null,
                  equipment_issue: equipmentIssue.trim() || null,
                  note: note.trim() || null,
                  issues: issues.map((i) => ({
                    ...i,
                    description: i.description.trim(),
                  })),
                });
              }}
              className="space-y-3 rounded-md border border-slate-200 bg-slate-50 p-3"
            >
              <div className="text-xs font-medium text-slate-600">
                提交现场记录（提交后自动为每条问题生成整改单与责任人待办）
              </div>
              <div className="grid grid-cols-1 gap-3 md:grid-cols-4">
                <Field label="动作评分 *（0-100）">
                  <input
                    type="number"
                    min="0"
                    max="100"
                    value={score}
                    onChange={(e) => setScore(e.target.value)}
                    className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
                  />
                </Field>
                <Field label="现场照片数">
                  <input
                    type="number"
                    min="0"
                    value={photoCount}
                    onChange={(e) => setPhotoCount(e.target.value)}
                    className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
                  />
                </Field>
                <Field label="现场时间 *">
                  <input
                    required
                    type="datetime-local"
                    value={visitedAt}
                    onChange={(e) => setVisitedAt(e.target.value)}
                    className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
                  />
                </Field>
                <Field label="现场说明">
                  <input
                    value={note}
                    onChange={(e) => setNote(e.target.value)}
                    className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
                  />
                </Field>
                <Field label="陈列问题">
                  <input
                    value={displayIssue}
                    onChange={(e) => setDisplayIssue(e.target.value)}
                    className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
                  />
                </Field>
                <Field label="服务问题">
                  <input
                    value={serviceIssue}
                    onChange={(e) => setServiceIssue(e.target.value)}
                    className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
                  />
                </Field>
                <Field label="设备问题">
                  <input
                    value={equipmentIssue}
                    onChange={(e) => setEquipmentIssue(e.target.value)}
                    className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
                  />
                </Field>
              </div>

              <div className="rounded-md border border-slate-200 bg-white p-3">
                <div className="flex items-center justify-between">
                  <div className="text-xs font-medium text-slate-600">
                    问题整改项（提交后生成整改单）
                  </div>
                  <button
                    type="button"
                    onClick={() =>
                      setIssues((prev) => [
                        ...prev,
                        {
                          category: "DISPLAY",
                          description: "",
                          owner_user_id: reps[0]?.id ?? "",
                          due_date: detail.planned_date,
                        },
                      ])
                    }
                    disabled={reps.length === 0}
                    className="rounded-md border border-slate-300 bg-white px-2 py-1 text-xs text-slate-600 disabled:opacity-50"
                  >
                    添加整改项
                  </button>
                </div>
                <div className="mt-2 space-y-2">
                  {issues.map((it, idx) => (
                    <div key={idx} className="flex flex-wrap items-end gap-2">
                      <Field label="分类">
                        <select
                          value={it.category}
                          onChange={(e) =>
                            setIssues((prev) =>
                              prev.map((x, i) =>
                                i === idx ? { ...x, category: e.target.value } : x,
                              ),
                            )
                          }
                          className="rounded-md border border-slate-300 bg-white px-2 py-1.5 text-sm"
                        >
                          {CATEGORY_OPTIONS.map((c) => (
                            <option key={c} value={c}>
                              {label(c)}
                            </option>
                          ))}
                        </select>
                      </Field>
                      <Field label="问题描述">
                        <input
                          value={it.description}
                          onChange={(e) =>
                            setIssues((prev) =>
                              prev.map((x, i) =>
                                i === idx ? { ...x, description: e.target.value } : x,
                              ),
                            )
                          }
                          className="w-64 rounded-md border border-slate-300 px-2 py-1.5 text-sm"
                        />
                      </Field>
                      <Field label="责任人">
                        <select
                          value={it.owner_user_id}
                          onChange={(e) =>
                            setIssues((prev) =>
                              prev.map((x, i) =>
                                i === idx ? { ...x, owner_user_id: e.target.value } : x,
                              ),
                            )
                          }
                          className="rounded-md border border-slate-300 bg-white px-2 py-1.5 text-sm"
                        >
                          <option value="">请选择</option>
                          {reps.map((u) => (
                            <option key={u.id} value={u.id}>
                              {u.name}
                            </option>
                          ))}
                        </select>
                      </Field>
                      <Field label="截止日">
                        <input
                          type="date"
                          value={it.due_date}
                          onChange={(e) =>
                            setIssues((prev) =>
                              prev.map((x, i) =>
                                i === idx ? { ...x, due_date: e.target.value } : x,
                              ),
                            )
                          }
                          className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
                        />
                      </Field>
                      <button
                        type="button"
                        onClick={() => setIssues((prev) => prev.filter((_, i) => i !== idx))}
                        className="rounded-md border border-slate-300 bg-white px-2 py-1.5 text-xs text-slate-600"
                      >
                        移除
                      </button>
                    </div>
                  ))}
                </div>
              </div>

              {formError ? <div className="text-xs text-rose-600">{formError}</div> : null}
              <button
                type="submit"
                disabled={submitting}
                className="rounded-md bg-[var(--color-brand)] px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50"
              >
                {submitting ? "提交中…" : "提交现场记录"}
              </button>
            </form>
          ) : null}
        </>
      ) : null}
    </section>
  );
}

// ============================ 整改闭环 ============================

function RectificationsTab({
  regions,
  canManage,
  onToast,
}: {
  regions: OptionItem[];
  canManage: boolean;
  onToast: (message: string, tone?: "info" | "success" | "warning" | "danger") => void;
}) {
  const { user } = useAuth();

  const [regionId, setRegionId] = useState("");
  const [statusInput, setStatusInput] = useState("");
  const [page, setPage] = useState(1);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [submitNotes, setSubmitNotes] = useState<Record<string, string>>({});
  const [verifyNotes, setVerifyNotes] = useState<Record<string, string>>({});

  const list = useApiQuery<ListResponse<RectListItem>>(
    "/supervision/rectifications",
    {
      region_id: regionId,
      status: statusInput,
      page,
      page_size: 20,
    },
    { enabled: true },
  );

  const startMut = useApiMutation((id: string) =>
    api.post<RectRow>(`/supervision/rectifications/${id}/start`),
  );
  const submitMut = useApiMutation((p: { id: string; note: string }) =>
    api.post<RectRow>(`/supervision/rectifications/${p.id}/submit`, { note: p.note }),
  );
  const verifyMut = useApiMutation((p: { id: string; passed: boolean; note: string | null }) =>
    api.post<RectRow>(`/supervision/rectifications/${p.id}/verify`, {
      passed: p.passed,
      note: p.note,
    }),
  );
  const closeMut = useApiMutation((p: { id: string; note: string | null }) =>
    api.post<RectRow>(`/supervision/rectifications/${p.id}/close`, { note: p.note }),
  );

  const rows = list.data?.items ?? [];
  const total = list.data?.pagination.total ?? 0;
  const totalPages = Math.max(1, Math.ceil(total / (list.data?.pagination.page_size ?? 20)));
  const pendingCount = rows.filter((r) => !RECT_CLOSED.has(r.status)).length;
  const overdueCount = rows.filter((r) => r.overdue).length;

  async function runAction(
    id: string,
    fn: () => Promise<unknown>,
    okMessage: string,
    errMessage: string,
  ) {
    setBusyId(id);
    const res = await fn();
    setBusyId(null);
    if (res) {
      onToast(okMessage, "success");
      list.refetch();
    } else {
      onToast(errMessage, "danger");
    }
  }

  const error =
    startMut.error || submitMut.error || verifyMut.error || closeMut.error;

  const columns: DataTableColumn<RectListItem>[] = [
    {
      key: "due",
      header: "截止日",
      render: (r) => (
        <span className={r.overdue ? "font-medium text-rose-600" : ""}>
          {formatDate(r.due_date)}
          {r.overdue ? "（逾期）" : ""}
        </span>
      ),
    },
    { key: "merchant", header: "商家", render: (r) => r.merchant_name },
    { key: "region", header: "片区", render: (r) => r.region_name },
    { key: "category", header: "分类", render: (r) => label(r.category) },
    { key: "description", header: "问题描述", render: (r) => r.description },
    { key: "owner", header: "责任人", render: (r) => r.owner_name ?? "—" },
    {
      key: "status",
      header: "状态",
      render: (r) => <StatusBadge tone={statusTone(r.status)} label={label(r.status)} />,
    },
    {
      key: "actions",
      header: "操作",
      render: (r) => {
        const isOwner = user?.id === r.owner_user_id;
        const isSubmitter = user?.id === r.submitter_id;
        return (
          <div className="flex flex-col gap-1">
            {isOwner && r.status === "OPEN" ? (
              <button
                type="button"
                disabled={busyId === r.id}
                onClick={() =>
                  runAction(r.id, () => startMut.run(r.id), "已开始整改", "操作失败")
                }
                className="rounded-md bg-[var(--color-brand)] px-2 py-1 text-xs font-medium text-white disabled:opacity-50"
              >
                开始整改
              </button>
            ) : null}

            {isOwner && r.status === "IN_PROGRESS" ? (
              <div className="flex flex-col gap-1">
                <input
                  value={submitNotes[r.id] ?? ""}
                  onChange={(e) =>
                    setSubmitNotes((prev) => ({ ...prev, [r.id]: e.target.value }))
                  }
                  placeholder="整改说明 *"
                  className="w-40 rounded-md border border-slate-300 px-2 py-1 text-xs"
                />
                <button
                  type="button"
                  disabled={busyId === r.id || !(submitNotes[r.id] ?? "").trim()}
                  onClick={() =>
                    runAction(
                      r.id,
                      () => submitMut.run({ id: r.id, note: (submitNotes[r.id] ?? "").trim() }),
                      "整改已提交，等待非提交人验证",
                      "提交失败",
                    )
                  }
                  className="rounded-md bg-[var(--color-brand)] px-2 py-1 text-xs font-medium text-white disabled:opacity-50"
                >
                  提交整改
                </button>
              </div>
            ) : null}

            {canManage && r.status === "SUBMITTED" ? (
              <div className="flex flex-col gap-1">
                {isSubmitter ? (
                  <span className="text-xs text-amber-700">你是提交人：需他人验证</span>
                ) : null}
                <input
                  value={verifyNotes[r.id] ?? ""}
                  onChange={(e) => setVerifyNotes((prev) => ({ ...prev, [r.id]: e.target.value }))}
                  placeholder="验证说明"
                  className="w-40 rounded-md border border-slate-300 px-2 py-1 text-xs"
                />
                <div className="flex gap-1">
                  <button
                    type="button"
                    disabled={busyId === r.id || isSubmitter}
                    onClick={() =>
                      runAction(
                        r.id,
                        () =>
                          verifyMut.run({
                            id: r.id,
                            passed: true,
                            note: (verifyNotes[r.id] ?? "").trim() || null,
                          }),
                        "验证通过（VERIFIED）",
                        "验证失败（验证人不能是提交人）",
                      )
                    }
                    className="rounded-md bg-[var(--color-brand)] px-2 py-1 text-xs font-medium text-white disabled:opacity-50"
                  >
                    验证通过
                  </button>
                  <button
                    type="button"
                    disabled={busyId === r.id}
                    onClick={() =>
                      runAction(
                        r.id,
                        () =>
                          verifyMut.run({
                            id: r.id,
                            passed: false,
                            note: (verifyNotes[r.id] ?? "").trim() || "证据不足，请补充后重新提交",
                          }),
                        "已记录未通过（保持 SUBMITTED，留审计）",
                        "操作失败",
                      )
                    }
                    className="rounded-md border border-slate-300 bg-white px-2 py-1 text-xs text-slate-600 disabled:opacity-50"
                  >
                    验证不通过
                  </button>
                </div>
              </div>
            ) : null}

            {canManage && r.status === "VERIFIED" ? (
              <button
                type="button"
                disabled={busyId === r.id || isSubmitter}
                onClick={() =>
                  runAction(
                    r.id,
                    () =>
                      closeMut.run({
                        id: r.id,
                        note: (verifyNotes[r.id] ?? "").trim() || null,
                      }),
                    "整改已关闭（CLOSED）",
                    "关闭失败（关闭人不能是提交人）",
                  )
                }
                className="rounded-md border border-emerald-300 bg-white px-2 py-1 text-xs text-emerald-700 disabled:opacity-50"
              >
                关闭整改
              </button>
            ) : null}

            {!isOwner &&
            !canManage &&
            r.status !== "CLOSED" ? (
              <span className="text-xs text-slate-400">无操作权限</span>
            ) : null}
            {RECT_CLOSED.has(r.status) ? (
              <span className="text-xs text-slate-400">已闭环</span>
            ) : null}
          </div>
        );
      },
    },
  ];

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <MetricCard label="整改单（本页）" value={rows.length} unit="条" />
        <MetricCard label="未闭环（本页）" value={pendingCount} unit="条" />
        <MetricCard label="已逾期（本页）" value={overdueCount} unit="条" />
        <MetricCard label="符合条件总数" value={total} unit="条" />
      </div>

      <FilterBar
        onApply={() => setPage(1)}
        onReset={() => {
          setStatusInput("");
          setPage(1);
        }}
      >
        <Field label="片区">
          <select
            value={regionId}
            onChange={(e) => {
              setRegionId(e.target.value);
              setPage(1);
            }}
            className="rounded-md border border-slate-300 bg-white px-2 py-1.5 text-sm"
          >
            <option value="">全部可见片区</option>
            {regions.map((r) => (
              <option key={r.id} value={r.id}>
                {r.code} · {r.name}
              </option>
            ))}
          </select>
        </Field>
        <Field label="状态">
          <select
            value={statusInput}
            onChange={(e) => {
              setStatusInput(e.target.value);
              setPage(1);
            }}
            className="rounded-md border border-slate-300 bg-white px-2 py-1.5 text-sm"
          >
            <option value="">全部</option>
            {RECT_STATUS_OPTIONS.map((s) => (
              <option key={s} value={s}>
                {label(s)}
              </option>
            ))}
          </select>
        </Field>
      </FilterBar>

      {list.loading ? <LoadingBlock text="加载整改列表…" /> : null}
      {list.error ? (
        <ErrorState message={list.error} status={list.status} onRetry={list.refetch} />
      ) : null}
      {error ? <div className="text-xs text-rose-600">{error}</div> : null}

      {!list.loading && !list.error ? (
        <>
          <DataTable
            rows={rows}
            columns={columns}
            rowKey={(r) => r.id}
            emptyText="暂无整改单（现场记录提交问题后自动生成）"
          />
          <Pagination page={page} totalPages={totalPages} onPageChange={setPage} />
        </>
      ) : null}
    </div>
  );
}

// ============================ 每周覆盖 ============================

function CoverageTab({ regions }: { regions: OptionItem[] }) {
  const [regionId, setRegionId] = useState("");
  const [weekStart, setWeekStart] = useState(currentWeekMonday());

  const effectiveRegion = regionId || regions[0]?.id || "";
  const coverage = useApiQuery<CoverageRow[]>(
    "/supervision/weekly-coverage",
    { region_id: effectiveRegion, week_start: weekStart },
    { enabled: Boolean(effectiveRegion && weekStart) },
  );

  const rows = coverage.data ?? [];
  const compliant = rows.filter((r) => r.compliant).length;

  return (
    <div className="space-y-4">
      <div className="rounded-md border border-slate-200 bg-white p-3">
        <div className="text-xs text-slate-500">
          达标口径（R15/A10）：同一业务周内跟访 ≥2 次且覆盖 ≥2 名不同业务员；
          「同一业务员两次」不计覆盖两人。只统计已提交（SUBMITTED）的跟访。
        </div>
        <div className="mt-3 flex flex-wrap items-end gap-3">
          <Field label="片区">
            <select
              value={regionId}
              onChange={(e) => setRegionId(e.target.value)}
              className="rounded-md border border-slate-300 bg-white px-2 py-1.5 text-sm"
            >
              <option value="">默认首个可见片区</option>
              {regions.map((r) => (
                <option key={r.id} value={r.id}>
                  {r.code} · {r.name}
                </option>
              ))}
            </select>
          </Field>
          <Field label="业务周（自动对齐周一）">
            <input
              type="date"
              value={weekStart}
              onChange={(e) => setWeekStart(e.target.value)}
              className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
            />
          </Field>
          <span className="text-xs text-slate-500">
            共 {rows.length} 名督导有记录，{compliant} 名达标
          </span>
        </div>
      </div>

      {coverage.loading ? <LoadingBlock text="计算每周覆盖…" /> : null}
      {coverage.error ? (
        <ErrorState message={coverage.error} status={coverage.status} onRetry={coverage.refetch} />
      ) : null}

      {!coverage.loading && !coverage.error ? (
        rows.length === 0 ? (
          <EmptyState
            title="该业务周暂无跟访记录"
            description="督导提交现场记录后，这里会显示每名督导的跟访次数与覆盖业务员数。"
          />
        ) : (
          <DataTable
            rows={rows}
            columns={[
              {
                key: "supervisor",
                header: "督导",
                render: (r) => r.supervisor_id.slice(0, 8),
              },
              { key: "week", header: "业务周（周一）", render: (r) => formatDate(r.week_start) },
              {
                key: "visits",
                header: "跟访次数",
                align: "right",
                render: (r) => r.visit_count,
              },
              {
                key: "reps",
                header: "覆盖业务员",
                align: "right",
                render: (r) => r.distinct_rep_count,
              },
              {
                key: "compliant",
                header: "是否达标",
                render: (r) => (
                  <StatusBadge
                    tone={r.compliant ? "success" : "warning"}
                    label={r.compliant ? "达标" : "未达标"}
                  />
                ),
              },
              {
                key: "warnings",
                header: "预警",
                render: (r) =>
                  r.warnings.length === 0 ? (
                    <span className="text-slate-400">—</span>
                  ) : (
                    <span className="text-xs text-amber-700">{r.warnings.join("；")}</span>
                  ),
              },
            ]}
            rowKey={(r) => `${r.region_id}-${r.supervisor_id}`}
            emptyText="暂无数据"
          />
        )
      ) : null}
    </div>
  );
}

function Info({ label: text, value }: { label: string; value: React.ReactNode }) {
  return (
    <div>
      <div className="text-xs text-slate-500">{text}</div>
      <div className="mt-0.5 text-slate-800">{value}</div>
    </div>
  );
}
