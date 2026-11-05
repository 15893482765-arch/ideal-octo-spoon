/** 地推活动（T15 前端联调 / PRD §4.7 / R39）。
 *
 * 接入后端：
 * - `GET /api/activities`（列表：含草稿，行级范围由服务端按角色注入）；
 * - `GET /api/activities/options/users|skus?region_id=`（负责人/参与人、样品 SKU 下拉）；
 * - `GET /api/activities/{id}`（详情：参与人、人员占用、样品台账）；
 * - `POST /api/activities`（创建草稿）、`/{id}/publish`（发布，写人员时段占用）、
 *   `/{id}/start`、`/{id}/cancel`（必填原因，释放未来时段占用）、`/{id}/review`（复盘）。
 *
 * 冲突裁决只来自 T13 ScheduleConflictService（发布冲突整体拒绝、零写入）；样品只做
 * 领用/消耗/归还/缺失台账，成本计入活动费用，不进确认销量与库存（R39）。页面只做
 * 展示与受控交互，前端隐藏不作为鉴权。
 */
import { useEffect, useMemo, useState } from "react";

import { api } from "../lib/api";
import { useAuth } from "../lib/auth";
import { boxes, formatDateTime, label, money, statusTone } from "../lib/format";
import { useApiMutation, useApiQuery } from "../lib/useApi";
import { useToast } from "../lib/useToast";
import { ErrorState, LoadingBlock, SessionNotice } from "../components/AsyncState";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { DataTable, type DataTableColumn } from "../components/DataTable";
import { EmptyState } from "../components/EmptyState";
import { FilterBar } from "../components/FilterBar";
import { PageHeader } from "../components/PageHeader";
import { Pagination } from "../components/Pagination";
import { StatusBadge } from "../components/StatusBadge";
import { ToastStack } from "../components/Toast";

/** 受控状态枚举（与后端 ActivityStatus 对齐）。 */
const STATUS_OPTIONS = [
  "DRAFT",
  "PUBLISHED",
  "IN_PROGRESS",
  "REVIEWED",
  "CLOSED",
  "CANCELLED",
] as const;

/** 样品台账阶段（与后端 activity_samples_stage_check 对齐）。 */
const STAGE_LABELS: Record<string, string> = {
  TAKEN_OUT: "领用",
  CONSUMED: "消耗",
  RETURNED: "归还",
  MISSING: "缺失未还",
};

const REVIEW_STAGES = ["CONSUMED", "RETURNED", "MISSING"] as const;

interface ActivityRow {
  id: string;
  name: string;
  status: string;
  region_id: string;
  region_name: string;
  owner_user_id: string;
  owner_name: string | null;
  start_at: string;
  end_at: string;
  location: string;
  budget_fen: number;
  sample_cost_fen: number;
  participant_count: number;
  active_reservation_count: number;
  notes: string | null;
  cancel_reason: string | null;
  reviewed_at: string | null;
}

interface ListResponse {
  items: ActivityRow[];
  pagination: { page: number; page_size: number; total: number };
}

interface LedgerRow {
  sku_id: string;
  stage: string;
  boxes: string;
  operator_id: string;
  note: string | null;
}

interface ActivityDetail {
  id: string;
  name: string;
  status: string;
  region_id: string;
  start_at: string;
  end_at: string;
  location: string;
  owner_user_id: string;
  budget_fen: number;
  sample_cost_fen: number;
  notes: string | null;
  cancel_reason: string | null;
  cancelled_at: string | null;
  reviewed_at: string | null;
  participant_user_ids: string[];
  active_reservation_ids: string[];
  sample_ledger: LedgerRow[];
}

interface OptionItem {
  id: string;
  code: string;
  name: string;
}

interface SampleInput {
  sku_id: string;
  boxes: string;
}

export function Activities() {
  const { authenticated, user } = useAuth();
  const { items: toasts, push, dismiss } = useToast();

  const [statusInput, setStatusInput] = useState("");
  const [regionInput, setRegionInput] = useState("");
  const [keywordInput, setKeywordInput] = useState("");
  const [fromInput, setFromInput] = useState("");
  const [toInput, setToInput] = useState("");
  const [applied, setApplied] = useState({
    status: "",
    region: "",
    keyword: "",
    from: "",
    to: "",
  });
  const [page, setPage] = useState(1);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);

  const role = user?.role;
  const canManage = role === "REGIONAL_MANAGER" || role === "SYSTEM_ADMIN";

  const regions = useApiQuery<{ items: OptionItem[] }>("/master-data/regions", undefined, {
    enabled: authenticated,
  });

  const list = useApiQuery<ListResponse>(
    "/activities",
    {
      region_id: applied.region,
      status: applied.status,
      keyword: applied.keyword,
      start_from: applied.from,
      start_to: applied.to,
      page,
      page_size: 20,
    },
    { enabled: authenticated },
  );

  const detail = useApiQuery<ActivityDetail>(
    `/activities/${selectedId ?? ""}`,
    undefined,
    { enabled: authenticated && Boolean(selectedId) },
  );

  const publishMut = useApiMutation((id: string) =>
    api.post<ActivityDetail>(`/activities/${id}/publish`),
  );
  const startMut = useApiMutation((id: string) =>
    api.post<ActivityDetail>(`/activities/${id}/start`),
  );
  const cancelMut = useApiMutation((p: { id: string; reason: string }) =>
    api.post<ActivityDetail>(`/activities/${p.id}/cancel`, { reason: p.reason }),
  );
  const reviewMut = useApiMutation(
    (p: { id: string; sample_results: { sku_id: string; stage: string; boxes: number }[] }) =>
      api.post<ActivityDetail>(`/activities/${p.id}/review`, {
        sample_results: p.sample_results,
      }),
  );
  const createMut = useApiMutation(
    (p: {
      region_id: string;
      name: string;
      start_at: string;
      end_at: string;
      location: string;
      owner_user_id: string;
      participant_user_ids: string[];
      budget_fen: number;
      notes: string | null;
      samples: SampleInput[];
    }) =>
      api.post<ActivityDetail>("/activities", {
        region_id: p.region_id,
        name: p.name,
        start_at: p.start_at,
        end_at: p.end_at,
        location: p.location,
        owner_user_id: p.owner_user_id,
        participant_user_ids: p.participant_user_ids,
        budget_fen: p.budget_fen,
        notes: p.notes,
        samples: p.samples
          .filter((s) => s.sku_id && Number(s.boxes) > 0)
          .map((s) => ({ sku_id: s.sku_id, boxes: Number(s.boxes) })),
      }),
  );

  const activeRow = (list.data?.items ?? []).find((r) => r.id === selectedId) ?? null;

  function refreshAll() {
    list.refetch();
    detail.refetch();
  }

  const columns: DataTableColumn<ActivityRow>[] = [
    {
      key: "name",
      header: "活动名称",
      render: (r) => (
        <button
          type="button"
          onClick={() => setSelectedId(r.id)}
          className="text-left font-medium text-[var(--color-brand)] hover:underline"
        >
          {r.name}
        </button>
      ),
    },
    { key: "region", header: "片区", render: (r) => r.region_name },
    { key: "owner", header: "负责人", render: (r) => r.owner_name ?? "—" },
    {
      key: "window",
      header: "时间窗",
      render: (r) => (
        <span className="text-xs text-slate-600">
          {formatDateTime(r.start_at)} → {formatDateTime(r.end_at)}
        </span>
      ),
    },
    { key: "location", header: "地点", render: (r) => r.location },
    {
      key: "participants",
      header: "参与人",
      align: "right",
      render: (r) => r.participant_count,
    },
    {
      key: "reservations",
      header: "生效占用",
      align: "right",
      render: (r) => (
        <span className={r.active_reservation_count > 0 ? "text-slate-700" : "text-slate-400"}>
          {r.active_reservation_count}
        </span>
      ),
    },
    { key: "budget", header: "预算", align: "right", render: (r) => money(r.budget_fen) },
    {
      key: "sample_cost",
      header: "样品成本",
      align: "right",
      render: (r) => money(r.sample_cost_fen),
    },
    {
      key: "status",
      header: "状态",
      render: (r) => <StatusBadge tone={statusTone(r.status)} label={label(r.status)} />,
    },
  ];

  if (!authenticated) {
    return (
      <div className="space-y-4">
        <PageHeader title="地推活动" description="活动草稿、发布占用、执行、取消与复盘" />
        <SessionNotice hint="当前是开发注入账号，请从登录页用真实账号登录后再查看业务数据。" />
      </div>
    );
  }

  const total = list.data?.pagination.total ?? 0;
  const totalPages = Math.max(1, Math.ceil(total / (list.data?.pagination.page_size ?? 20)));

  return (
    <div className="space-y-4">
      <PageHeader
        title="地推活动"
        description="发布时按参与人写人员时段占用（冲突整体拒绝）；取消释放未来时段；样品只做领用/消耗/归还台账"
        actions={
          canManage ? (
            <button
              type="button"
              onClick={() => setCreating((v) => !v)}
              className="rounded-md bg-[var(--color-brand)] px-3 py-1.5 text-sm font-medium text-white hover:opacity-90"
            >
              {creating ? "收起新建" : "新建活动"}
            </button>
          ) : null
        }
      />

      {creating && canManage ? (
        <CreateActivityForm
          regions={regions.data?.items ?? []}
          submitting={createMut.submitting}
          error={createMut.error}
          onCancel={() => setCreating(false)}
          onSubmit={async (payload) => {
            const res = await createMut.run(payload);
            if (res) {
              push("活动草稿已创建（发布后才会占用人员时段）", "success");
              setCreating(false);
              setSelectedId(res.id);
              list.refetch();
            } else {
              push(createMut.error || "创建失败", "danger");
            }
          }}
        />
      ) : null}

      <FilterBar
        onApply={() => {
          setApplied({
            status: statusInput,
            region: regionInput,
            keyword: keywordInput.trim(),
            from: fromInput,
            to: toInput,
          });
          setPage(1);
        }}
        onReset={() => {
          setStatusInput("");
          setRegionInput("");
          setKeywordInput("");
          setFromInput("");
          setToInput("");
          setApplied({ status: "", region: "", keyword: "", from: "", to: "" });
          setPage(1);
        }}
      >
        <Field label="活动名称">
          <input
            value={keywordInput}
            onChange={(e) => setKeywordInput(e.target.value)}
            placeholder="支持模糊匹配"
            className="w-48 rounded-md border border-slate-300 px-2 py-1.5 text-sm"
          />
        </Field>
        <Field label="状态">
          <select
            value={statusInput}
            onChange={(e) => setStatusInput(e.target.value)}
            className="rounded-md border border-slate-300 bg-white px-2 py-1.5 text-sm"
          >
            <option value="">全部</option>
            {STATUS_OPTIONS.map((s) => (
              <option key={s} value={s}>
                {label(s)}
              </option>
            ))}
          </select>
        </Field>
        <Field label="片区">
          <select
            value={regionInput}
            onChange={(e) => setRegionInput(e.target.value)}
            className="rounded-md border border-slate-300 bg-white px-2 py-1.5 text-sm"
          >
            <option value="">全部可见片区</option>
            {(regions.data?.items ?? []).map((r) => (
              <option key={r.id} value={r.id}>
                {r.code} · {r.name}
              </option>
            ))}
          </select>
        </Field>
        <Field label="开始日（起）">
          <input
            type="date"
            value={fromInput}
            onChange={(e) => setFromInput(e.target.value)}
            className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
          />
        </Field>
        <Field label="开始日（止）">
          <input
            type="date"
            value={toInput}
            onChange={(e) => setToInput(e.target.value)}
            className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
          />
        </Field>
      </FilterBar>

      {list.loading ? <LoadingBlock text="加载活动列表…" /> : null}
      {list.error ? (
        <ErrorState message={list.error} status={list.status} onRetry={list.refetch} />
      ) : null}

      {!list.loading && !list.error ? (
        <>
          <div className="text-xs text-slate-500">
            共 {total} 个活动（含草稿；发布后按参与人生成人员时段占用）
          </div>
          <DataTable
            rows={list.data?.items ?? []}
            columns={columns}
            rowKey={(r) => r.id}
            emptyText={
              canManage
                ? "暂无活动（可用右上角新建，或调整筛选条件）"
                : "暂无可见活动（活动由片区经理创建）"
            }
          />
          <Pagination page={page} totalPages={totalPages} onPageChange={setPage} />
        </>
      ) : null}

      {selectedId ? (
        <ActivityDetailPanel
          row={activeRow}
          detail={detail.data}
          loading={detail.loading}
          error={detail.error}
          status={detail.status}
          onClose={() => setSelectedId(null)}
          onRetry={detail.refetch}
          canManage={canManage}
          submitting={
            publishMut.submitting ||
            startMut.submitting ||
            cancelMut.submitting ||
            reviewMut.submitting
          }
          actionError={
            publishMut.error || startMut.error || cancelMut.error || reviewMut.error
          }
          regionId={detail.data?.region_id ?? activeRow?.region_id ?? ""}
          onPublish={async (id) => {
            const res = await publishMut.run(id);
            if (res) {
              push("活动已发布：人员时段占用已生成（冲突则整体拒绝）", "success");
              refreshAll();
            } else {
              push(publishMut.error || "发布失败（存在人员冲突或状态不允许）", "danger");
            }
          }}
          onStart={async (id) => {
            const res = await startMut.run(id);
            if (res) {
              push("活动已进入执行中", "success");
              refreshAll();
            } else {
              push(startMut.error || "开始执行失败", "danger");
            }
          }}
          onCancel={async (id, reason) => {
            const res = await cancelMut.run({ id, reason });
            if (res) {
              push("活动已取消：未来时段占用已释放（原因已留痕）", "success");
              refreshAll();
            } else {
              push(cancelMut.error || "取消失败", "danger");
            }
          }}
          onReview={async (id, lines) => {
            const res = await reviewMut.run({ id, sample_results: lines });
            if (res) {
              push("复盘完成：样品台账与成本已结算", "success");
              refreshAll();
            } else {
              push(reviewMut.error || "复盘失败（样品台账可能不平）", "danger");
            }
          }}
        />
      ) : null}

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

interface CreatePayload {
  region_id: string;
  name: string;
  start_at: string;
  end_at: string;
  location: string;
  owner_user_id: string;
  participant_user_ids: string[];
  budget_fen: number;
  notes: string | null;
  samples: SampleInput[];
}

function CreateActivityForm({
  regions,
  submitting,
  error,
  onSubmit,
  onCancel,
}: {
  regions: OptionItem[];
  submitting: boolean;
  error: string;
  onSubmit: (payload: CreatePayload) => void;
  onCancel: () => void;
}) {
  const [regionId, setRegionId] = useState("");
  const [name, setName] = useState("");
  const [startAt, setStartAt] = useState("");
  const [endAt, setEndAt] = useState("");
  const [location, setLocation] = useState("");
  const [ownerId, setOwnerId] = useState("");
  const [participants, setParticipants] = useState<Record<string, boolean>>({});
  const [budgetYuan, setBudgetYuan] = useState("");
  const [notes, setNotes] = useState("");
  const [samples, setSamples] = useState<SampleInput[]>([]);
  const [formError, setFormError] = useState("");

  useEffect(() => {
    if (!regionId && regions.length > 0) setRegionId(regions[0].id);
  }, [regions, regionId]);

  const users = useApiQuery<{ items: OptionItem[] }>(
    "/activities/options/users",
    { region_id: regionId },
    { enabled: Boolean(regionId) },
  );
  const skus = useApiQuery<{ items: OptionItem[] }>(
    "/activities/options/skus",
    { region_id: regionId },
    { enabled: Boolean(regionId) },
  );

  const userList = useMemo(() => users.data?.items ?? [], [users.data]);
  const skuList = useMemo(() => skus.data?.items ?? [], [skus.data]);
  const picked = userList.filter((u) => participants[u.id]).map((u) => u.id);

  useEffect(() => {
    if (!ownerId && userList.length > 0) setOwnerId(userList[0].id);
  }, [userList, ownerId]);

  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        if (!regionId || !name.trim() || !startAt || !endAt || !location.trim() || !ownerId) {
          setFormError("请填写活动名称、时间窗、地点与负责人");
          return;
        }
        if (endAt <= startAt) {
          setFormError("结束时间必须晚于开始时间");
          return;
        }
        if (picked.length === 0) {
          setFormError("请至少勾选一名参与人");
          return;
        }
        setFormError("");
        onSubmit({
          region_id: regionId,
          name: name.trim(),
          start_at: startAt,
          end_at: endAt,
          location: location.trim(),
          owner_user_id: ownerId,
          participant_user_ids: picked,
          budget_fen: Math.round(Number(budgetYuan || "0") * 100),
          notes: notes.trim() || null,
          samples,
        });
      }}
      className="space-y-3 rounded-md border border-slate-200 bg-white p-4"
    >
      <div className="text-sm font-medium text-slate-700">新建地推活动草稿</div>
      <p className="text-xs text-slate-500">
        草稿不写人员占用（允许时间冲突存在）；发布时才由 T13 冲突服务裁决，任意冲突整体拒绝。
      </p>

      <div className="grid grid-cols-1 gap-3 md:grid-cols-4">
        <Field label="片区 *">
          <select
            required
            value={regionId}
            onChange={(e) => {
              setRegionId(e.target.value);
              setParticipants({});
              setOwnerId("");
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
        <Field label="活动名称 *">
          <input
            required
            value={name}
            onChange={(e) => setName(e.target.value)}
            className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
          />
        </Field>
        <Field label="开始时间 *（本地时间）">
          <input
            required
            type="datetime-local"
            value={startAt}
            onChange={(e) => setStartAt(e.target.value)}
            className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
          />
        </Field>
        <Field label="结束时间 *（本地时间）">
          <input
            required
            type="datetime-local"
            value={endAt}
            onChange={(e) => setEndAt(e.target.value)}
            className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
          />
        </Field>
        <Field label="地点 *">
          <input
            required
            value={location}
            onChange={(e) => setLocation(e.target.value)}
            className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
          />
        </Field>
        <Field label="负责人 *">
          <select
            required
            value={ownerId}
            onChange={(e) => setOwnerId(e.target.value)}
            className="rounded-md border border-slate-300 bg-white px-2 py-1.5 text-sm"
          >
            <option value="">请选择</option>
            {userList.map((u) => (
              <option key={u.id} value={u.id}>
                {u.name}（{u.code}）
              </option>
            ))}
          </select>
        </Field>
        <Field label="预算（元）">
          <input
            type="number"
            min="0"
            step="0.01"
            value={budgetYuan}
            onChange={(e) => setBudgetYuan(e.target.value)}
            className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
          />
        </Field>
        <Field label="备注">
          <input
            value={notes}
            onChange={(e) => setNotes(e.target.value)}
            className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
          />
        </Field>
      </div>

      <div className="rounded-md border border-slate-200 bg-slate-50 p-3">
        <div className="text-xs font-medium text-slate-600">
          参与人 *（发布后每人一条人员时段占用；负责人自动纳入）
        </div>
        {users.loading ? <LoadingBlock text="加载可选人员…" /> : null}
        {!users.loading && userList.length === 0 ? (
          <p className="mt-2 text-xs text-amber-700">
            该片区暂无可选在职人员（需先由管理员建号并授权片区）。
          </p>
        ) : null}
        <div className="mt-2 flex flex-wrap gap-3">
          {userList.map((u) => (
            <label key={u.id} className="flex items-center gap-1.5 text-xs text-slate-700">
              <input
                type="checkbox"
                checked={Boolean(participants[u.id])}
                onChange={(e) =>
                  setParticipants((prev) => ({ ...prev, [u.id]: e.target.checked }))
                }
              />
              {u.name}
              <span className="font-mono text-slate-400">{u.code}</span>
            </label>
          ))}
        </div>
      </div>

      <div className="rounded-md border border-slate-200 bg-slate-50 p-3">
        <div className="flex items-center justify-between">
          <div className="text-xs font-medium text-slate-600">
            携带样品领用（可选；复盘时须按 SKU 平衡：领用 = 消耗 + 归还 + 缺失）
          </div>
          <button
            type="button"
            onClick={() => setSamples((prev) => [...prev, { sku_id: skuList[0]?.id ?? "", boxes: "" }])}
            disabled={skuList.length === 0}
            className="rounded-md border border-slate-300 bg-white px-2 py-1 text-xs text-slate-600 disabled:opacity-50"
          >
            添加样品行
          </button>
        </div>
        {skuList.length === 0 ? (
          <p className="mt-2 text-xs text-amber-700">
            该片区暂无可选 SKU（需先导入主数据）；不填样品不影响活动发布与复盘。
          </p>
        ) : null}
        <div className="mt-2 space-y-2">
          {samples.map((s, idx) => (
            <div key={idx} className="flex flex-wrap items-end gap-2">
              <Field label="SKU">
                <select
                  value={s.sku_id}
                  onChange={(e) =>
                    setSamples((prev) =>
                      prev.map((it, i) => (i === idx ? { ...it, sku_id: e.target.value } : it)),
                    )
                  }
                  className="rounded-md border border-slate-300 bg-white px-2 py-1.5 text-sm"
                >
                  {skuList.map((k) => (
                    <option key={k.id} value={k.id}>
                      {k.code} · {k.name}
                    </option>
                  ))}
                </select>
              </Field>
              <Field label="领用箱数">
                <input
                  type="number"
                  min="0"
                  step="0.01"
                  value={s.boxes}
                  onChange={(e) =>
                    setSamples((prev) =>
                      prev.map((it, i) => (i === idx ? { ...it, boxes: e.target.value } : it)),
                    )
                  }
                  className="w-28 rounded-md border border-slate-300 px-2 py-1.5 text-sm"
                />
              </Field>
              <button
                type="button"
                onClick={() => setSamples((prev) => prev.filter((_, i) => i !== idx))}
                className="rounded-md border border-slate-300 bg-white px-2 py-1.5 text-xs text-slate-600"
              >
                移除
              </button>
            </div>
          ))}
        </div>
      </div>

      {formError ? <div className="text-xs text-rose-600">{formError}</div> : null}
      {error ? <div className="text-xs text-rose-600">{error}</div> : null}
      <div className="flex gap-2">
        <button
          type="submit"
          disabled={submitting}
          className="rounded-md bg-[var(--color-brand)] px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50"
        >
          {submitting ? "提交中…" : "创建草稿"}
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

function ActivityDetailPanel({
  row,
  detail,
  loading,
  error,
  status,
  onClose,
  onRetry,
  canManage,
  submitting,
  actionError,
  regionId,
  onPublish,
  onStart,
  onCancel,
  onReview,
}: {
  row: ActivityRow | null;
  detail: ActivityDetail | null;
  loading: boolean;
  error: string;
  status: number | null;
  onClose: () => void;
  onRetry: () => void;
  canManage: boolean;
  submitting: boolean;
  actionError: string;
  regionId: string;
  onPublish: (id: string) => void;
  onStart: (id: string) => void;
  onCancel: (id: string, reason: string) => void;
  onReview: (
    id: string,
    lines: { sku_id: string; stage: string; boxes: number }[],
  ) => void;
}) {
  const activityId = detail?.id ?? row?.id ?? "";
  const activityStatus = detail?.status ?? row?.status ?? "";

  const [cancelReason, setCancelReason] = useState("");
  const [askingCancel, setAskingCancel] = useState(false);
  const [reviewInputs, setReviewInputs] = useState<
    Record<string, { CONSUMED: string; RETURNED: string; MISSING: string }>
  >({});
  const [reviewError, setReviewError] = useState("");

  // 详情里的参与人/SKU 只有 UUID，按片区取一次下拉选项翻译成名称（选项接口已按角色授权裁剪）
  const userOptions = useApiQuery<{ items: OptionItem[] }>(
    "/activities/options/users",
    { region_id: regionId },
    { enabled: Boolean(regionId) },
  );
  const skuOptions = useApiQuery<{ items: OptionItem[] }>(
    "/activities/options/skus",
    { region_id: regionId },
    { enabled: Boolean(regionId) },
  );

  const nameOfUser = useMemo(() => {
    const map = new Map((userOptions.data?.items ?? []).map((u) => [u.id, u.name]));
    return (id: string) => map.get(id) ?? id.slice(0, 8);
  }, [userOptions.data]);
  const codeOfSku = useMemo(() => {
    const map = new Map(
      (skuOptions.data?.items ?? []).map((s) => [s.id, `${s.code} · ${s.name}`]),
    );
    return (id: string) => map.get(id) ?? id.slice(0, 8);
  }, [skuOptions.data]);

  /** 按 SKU 汇总领用量：复盘必须与实耗+归还+缺失持平。 */
  const takenBySku = useMemo(() => {
    const agg = new Map<string, number>();
    for (const l of detail?.sample_ledger ?? []) {
      if (l.stage !== "TAKEN_OUT") continue;
      agg.set(l.sku_id, (agg.get(l.sku_id) ?? 0) + Number(l.boxes));
    }
    return agg;
  }, [detail?.sample_ledger]);

  const reviewedLines = useMemo(() => {
    const lines: { sku_id: string; stage: string; boxes: number }[] = [];
    for (const [skuId, taken] of takenBySku) {
      const cur = reviewInputs[skuId];
      if (!cur) continue;
      let sum = 0;
      for (const stage of REVIEW_STAGES) {
        const v = Number(cur[stage] || "0");
        if (v > 0) lines.push({ sku_id: skuId, stage, boxes: v });
        sum += v;
      }
      if (Math.abs(sum - taken) > 0.001) return [];
    }
    return lines;
  }, [takenBySku, reviewInputs]);

  const hasSamples = takenBySku.size > 0;
  // 有样品：每个 SKU 都必须填平（消耗+归还+缺失 = 领用）；无样品：复盘即确认结束
  const reviewBalanced =
    !hasSamples ||
    (takenBySku.size === [...takenBySku.keys()].filter((k) => reviewInputs[k]).length &&
      reviewedLines.length > 0);

  return (
    <section className="space-y-3 rounded-md border border-slate-200 bg-white p-4">
      <div className="flex items-start justify-between">
        <div>
          <h2 className="text-sm font-semibold text-slate-700">活动详情</h2>
          {row ? (
            <div className="mt-0.5 text-xs text-slate-500">
              {row.region_name} · 负责人 {row.owner_name ?? "—"}
            </div>
          ) : null}
        </div>
        <button type="button" onClick={onClose} className="text-xs text-slate-500 hover:underline">
          关闭
        </button>
      </div>

      {loading ? <LoadingBlock text="加载活动详情…" /> : null}
      {error ? <ErrorState message={error} status={status} onRetry={onRetry} /> : null}

      {detail || row ? (
        <>
          <div className="grid grid-cols-2 gap-3 text-sm md:grid-cols-4">
            <Info label="活动名称" value={detail?.name ?? row?.name ?? "—"} />
            <Info
              label="状态"
              value={<StatusBadge tone={statusTone(activityStatus)} label={label(activityStatus)} />}
            />
            <Info label="开始时间" value={formatDateTime(detail?.start_at ?? row?.start_at)} />
            <Info label="结束时间" value={formatDateTime(detail?.end_at ?? row?.end_at)} />
            <Info label="地点" value={detail?.location ?? row?.location ?? "—"} />
            <Info label="预算" value={money(detail?.budget_fen ?? row?.budget_fen)} />
            <Info label="样品成本（实耗，不进销量）" value={money(detail?.sample_cost_fen ?? row?.sample_cost_fen)} />
            <Info
              label="生效人员占用"
              value={String(detail?.active_reservation_ids.length ?? row?.active_reservation_count ?? 0)}
            />
            <Info label="复盘时间" value={formatDateTime(detail?.reviewed_at ?? row?.reviewed_at)} />
            <Info label="取消原因" value={detail?.cancel_reason ?? row?.cancel_reason ?? "—"} />
            <Info label="备注" value={detail?.notes ?? row?.notes ?? "—"} />
          </div>

          <div className="rounded-md border border-slate-200 p-3">
            <div className="text-xs font-medium text-slate-600">
              参与人（{detail?.participant_user_ids.length ?? row?.participant_count ?? 0} 人，发布后各一条占用）
            </div>
            <div className="mt-2 flex flex-wrap gap-2 text-xs text-slate-700">
              {(detail?.participant_user_ids ?? []).length === 0 ? (
                <span className="text-slate-400">暂无参与人</span>
              ) : (
                (detail?.participant_user_ids ?? []).map((uid) => (
                  <span
                    key={uid}
                    className="rounded-full bg-slate-100 px-2 py-0.5 ring-1 ring-inset ring-slate-200"
                  >
                    {nameOfUser(uid)}
                  </span>
                ))
              )}
            </div>
          </div>

          {!canManage ? (
            <p className="rounded-md border border-slate-200 bg-slate-50 px-3 py-2 text-xs text-slate-600">
              当前角色只读：活动创建/发布/执行/取消/复盘由片区经理及以上执行（后端二次鉴权）。
            </p>
          ) : null}

          {actionError ? <div className="text-xs text-rose-600">{actionError}</div> : null}

          {canManage ? (
            <div className="grid grid-cols-1 gap-3 md:grid-cols-3">
              {activityStatus === "DRAFT" ? (
                <ActionBox title="发布">
                  <p className="text-xs text-slate-500">
                    发布时按参与人生成人员时段占用；与排班/其他活动冲突则整体拒绝（零写入）。
                  </p>
                  <button
                    type="button"
                    disabled={submitting}
                    onClick={() => onPublish(activityId)}
                    className="rounded-md bg-[var(--color-brand)] px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50"
                  >
                    {submitting ? "发布中…" : "发布活动"}
                  </button>
                </ActionBox>
              ) : null}

              {activityStatus === "PUBLISHED" ? (
                <ActionBox title="开始执行">
                  <p className="text-xs text-slate-500">现场开始后标记执行中（占用保持不变）。</p>
                  <button
                    type="button"
                    disabled={submitting}
                    onClick={() => onStart(activityId)}
                    className="rounded-md bg-[var(--color-brand)] px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50"
                  >
                    {submitting ? "处理中…" : "标记执行中"}
                  </button>
                </ActionBox>
              ) : null}

              {activityStatus === "PUBLISHED" || activityStatus === "IN_PROGRESS" ? (
                <ActionBox title="复盘（样品结算）">
                  <p className="text-xs text-slate-500">
                    {takenBySku.size > 0
                      ? "按 SKU 填实耗：领用 = 消耗 + 归还 + 缺失；缺失会为每个参与人生成提醒待办。"
                      : "本次活动未携带样品，直接提交复盘即可（样品成本按实耗为 0）。"}
                  </p>
                  <div className="space-y-2">
                    {[...takenBySku].map(([skuId, taken]) => {
                      const cur =
                        reviewInputs[skuId] ?? { CONSUMED: "", RETURNED: "", MISSING: "" };
                      const sum =
                        Number(cur.CONSUMED || "0") +
                        Number(cur.RETURNED || "0") +
                        Number(cur.MISSING || "0");
                      return (
                        <div key={skuId} className="rounded border border-slate-200 bg-white p-2">
                          <div className="text-xs text-slate-600">
                            {codeOfSku(skuId)} · 领用 {boxes(taken)} 箱
                          </div>
                          <div className="mt-1 flex flex-wrap gap-2">
                            {REVIEW_STAGES.map((stage) => (
                              <label
                                key={stage}
                                className="flex flex-col gap-0.5 text-xs text-slate-500"
                              >
                                <span>{STAGE_LABELS[stage]}</span>
                                <input
                                  type="number"
                                  min="0"
                                  step="0.01"
                                  value={cur[stage]}
                                  onChange={(e) =>
                                    setReviewInputs((prev) => ({
                                      ...prev,
                                      [skuId]: { ...cur, [stage]: e.target.value },
                                    }))
                                  }
                                  className="w-20 rounded-md border border-slate-300 px-2 py-1 text-sm"
                                />
                              </label>
                            ))}
                          </div>
                          <div
                            className={
                              Math.abs(sum - taken) > 0.001
                                ? "mt-1 text-xs text-rose-600"
                                : "mt-1 text-xs text-emerald-700"
                            }
                          >
                            已填 {boxes(sum)} / 领用 {boxes(taken)} 箱
                          </div>
                        </div>
                      );
                    })}
                  </div>
                  {reviewError ? <div className="text-xs text-rose-600">{reviewError}</div> : null}
                  <button
                    type="button"
                    disabled={submitting}
                    onClick={() => {
                      if (!reviewBalanced) {
                        setReviewError("每个 SKU 的消耗+归还+缺失必须等于领用量");
                        return;
                      }
                      setReviewError("");
                      onReview(activityId, hasSamples ? reviewedLines : []);
                    }}
                    className="rounded-md bg-[var(--color-brand)] px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50"
                  >
                    {submitting ? "提交中…" : "提交复盘"}
                  </button>
                </ActionBox>
              ) : null}

              {canManage &&
              (activityStatus === "PUBLISHED" || activityStatus === "IN_PROGRESS") ? (
                <ActionBox title="取消（必须留原因）">
                  <Field label="取消原因 *">
                    <textarea
                      value={cancelReason}
                      onChange={(e) => setCancelReason(e.target.value)}
                      rows={2}
                      className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
                    />
                  </Field>
                  <button
                    type="button"
                    disabled={submitting || cancelReason.trim().length === 0}
                    onClick={() => setAskingCancel(true)}
                    className="rounded-md border border-rose-300 bg-white px-3 py-1.5 text-sm text-rose-700 disabled:opacity-50"
                  >
                    取消活动
                  </button>
                  <p className="text-xs text-slate-400">
                    只释放**未来时段**占用；已经开始/过去的占用保留为事实。
                  </p>
                </ActionBox>
              ) : null}
            </div>
          ) : null}

          <div>
            <div className="text-xs font-medium text-slate-600">
              样品台账（{detail?.sample_ledger.length ?? 0} 条，追加不覆盖）
            </div>
            <div className="mt-2">
              <DataTable
                rows={detail?.sample_ledger ?? []}
                columns={[
                  { key: "sku", header: "SKU", render: (l) => codeOfSku(l.sku_id) },
                  {
                    key: "stage",
                    header: "阶段",
                    render: (l) => STAGE_LABELS[l.stage] ?? l.stage,
                  },
                  {
                    key: "boxes",
                    header: "箱数",
                    align: "right",
                    render: (l) => <span className="font-mono">{boxes(l.boxes)}</span>,
                  },
                  { key: "operator", header: "登记人", render: (l) => nameOfUser(l.operator_id) },
                  { key: "note", header: "备注", render: (l) => l.note ?? "—" },
                ]}
                rowKey={(l) => `${l.sku_id}-${l.stage}-${l.operator_id}-${l.boxes}`}
                emptyText="暂无样品台账（草稿可携带样品，复盘时登记实耗）"
              />
            </div>
          </div>
        </>
      ) : (
        <EmptyState title="请选择活动" description="从列表点击活动名称查看详情与可执行动作。" />
      )}

      <ConfirmDialog
        open={askingCancel}
        destructive
        title="确认取消该活动？"
        description="取消只做状态迁移并留原因，未来时段的人员占用会被释放，已开始/过去的占用保留。"
        confirmText="确认取消"
        onClose={() => setAskingCancel(false)}
        onConfirm={() => {
          setAskingCancel(false);
          onCancel(activityId, cancelReason.trim());
        }}
      />
    </section>
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

function ActionBox({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="space-y-2 rounded-md border border-slate-200 bg-slate-50 p-3">
      <div className="text-xs font-medium text-slate-600">{title}</div>
      {children}
    </div>
  );
}
