/** 商家中心（T08 前端联调 / PRD §4.2）。
 *
 * 接入后端：`GET/POST /api/merchants`、`GET /api/merchants/{id}`、
 * `POST /api/merchants/{id}/status-change`（负责业务员维护状态）、
 * `POST /api/merchants/{id}/grade-change`；下拉依赖 `GET /api/master-data/{regions,brands}`；
 * 商家调拨（T14）走 `/api/v1/merchants/transfers`：options / preview / 创建 / apply。
 * 页面只做展示与受控交互，业务规则（行级范围、状态机、审批）全部由后端 Service 裁决。
 *
 * 深链：`/merchants?merchant_id=<id>` 直接展开该商家详情面板，`?q=` 预填关键字——
 * 工作台风险卡、全局搜索命中都使用这两个参数（Q157 深链筛选恢复）。
 */
import { useState } from "react";
import { useSearchParams } from "react-router-dom";

import { api } from "../lib/api";
import { useAuth } from "../lib/auth";
import { boxes, formatDate, label, statusTone } from "../lib/format";
import { useApiMutation, useApiQuery } from "../lib/useApi";
import { useToast } from "../lib/useToast";
import { ErrorState, LoadingBlock, SessionNotice } from "../components/AsyncState";
import { DataTable, type DataTableColumn } from "../components/DataTable";
import { EmptyState } from "../components/EmptyState";
import { FilterBar } from "../components/FilterBar";
import { PageHeader } from "../components/PageHeader";
import { Pagination } from "../components/Pagination";
import { StatusBadge } from "../components/StatusBadge";
import { ToastStack } from "../components/Toast";

interface MerchantRow {
  id: string;
  code: string;
  name: string;
  status: string;
  grade: string | null;
  region_id: string;
  brand_id: string;
  address: string | null;
  contact_phone: string | null;
  primary_contact_name: string | null;
  current_owner_id: string | null;
  created_at: string;
}

interface MerchantListResponse {
  items: MerchantRow[];
  pagination: { page: number; page_size: number; total: number };
}

interface MerchantDetail extends MerchantRow {
  business_hours: string | null;
  contact_masked: boolean;
  location_masked: boolean;
  /** 当前主责任归属（详情接口返回 current_owner；列表接口才返回 current_owner_id）。 */
  current_owner: {
    id: string;
    owner_user_id: string;
    assignment_type: string;
    start_at: string;
    end_at: string | null;
    reason: string | null;
  } | null;
  contracts: { id: string; contract_no: string; status: string; expire_at?: string }[];
  freezers: { id: string; asset_code: string; status: string }[];
  visits: number;
  replenishments: { id: string; code: string; status: string; requested_boxes?: string }[];
}

interface TransferUser {
  id: string;
  display_name: string;
}

interface TransferOptions {
  merchant_id: string;
  from_user: TransferUser | null;
  items: TransferUser[];
}

interface TransferPreview {
  from_user: TransferUser;
  to_user: TransferUser;
  effective_date: string;
  open_visit_count: number;
  open_todo_count: number;
  temporary_grants: { user_id: string; expires_at: string | null }[];
  contracts: { id: string; code: string; status: string }[];
  performance_boundary: string;
}

interface TransferOut {
  id: string;
  status: string;
  effective_date: string;
  from_user_id: string;
  to_user_id: string;
}

interface OptionItem {
  id: string;
  code: string;
  name: string;
}

const STATUS_OPTIONS = [
  "LEAD",
  "NEGOTIATING",
  "ACTIVE",
  "PAUSED",
  "TERMINATED",
];
const GRADE_OPTIONS = ["A", "B", "C"];

/** PRD §4.3:222 商家状态机，与后端 `_SALES_REP_STATUS_TRANSITIONS` 对齐。
 *  仅用于前端提示可选目标状态，最终裁决在后端；TERMINATED 只能走
 *  「取消合作申请 → 经理审批 → 撤柜」，不在此表中。
 */
const STATUS_TRANSITIONS: Record<string, string[]> = {
  LEAD: ["NEGOTIATING"],
  NEGOTIATING: ["LEAD", "ACTIVE"],
  ACTIVE: ["PAUSED"],
  PAUSED: ["ACTIVE"],
  TERMINATED: [],
};

/** 商家语境的状态文案：全局 label() 把 ACTIVE 译成「启用」，
 *  商家域里 ACTIVE 是「合作中」（PRD §4.3），本页统一走这里。
 */
const MERCHANT_STATUS_LABELS: Record<string, string> = {
  LEAD: "线索",
  NEGOTIATING: "洽谈中",
  ACTIVE: "合作中",
  PAUSED: "暂停合作",
  TERMINATED: "已终止",
};

function merchantStatusLabel(status: string | null | undefined): string {
  return MERCHANT_STATUS_LABELS[status ?? ""] ?? label(status);
}

/** 本地日期偏移（YYYY-MM-DD，按本地日历日，避免 UTC 跨日）。 */
function isoDatePlus(days: number): string {
  const d = new Date();
  d.setDate(d.getDate() + days);
  const month = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${d.getFullYear()}-${month}-${day}`;
}

export function Merchants() {
  const { authenticated, hasAbility, user } = useAuth();
  const { items: toasts, push, dismiss } = useToast();

  const [params, setParams] = useSearchParams();

  // 深链参数即初始筛选/选中：?merchant_id= 展开详情，?q= 预填关键字
  const linkedKeyword = params.get("q") ?? "";
  const selectedId = params.get("merchant_id") || null;

  const [keywordInput, setKeywordInput] = useState(linkedKeyword);
  const [statusInput, setStatusInput] = useState("");
  const [gradeInput, setGradeInput] = useState("");
  const [applied, setApplied] = useState({
    keyword: linkedKeyword,
    status: "",
    grade: "",
  });
  const [page, setPage] = useState(1);
  const [creating, setCreating] = useState(false);

  /** 选中商家同步进 URL（可分享/可返回；关面板即清掉 merchant_id）。 */
  function selectMerchant(id: string | null) {
    const next = new URLSearchParams(params);
    if (id) next.set("merchant_id", id);
    else next.delete("merchant_id");
    setParams(next, { replace: true });
  }

  const list = useApiQuery<MerchantListResponse>(
    "/merchants",
    {
      keyword: applied.keyword,
      status: applied.status,
      grade: applied.grade,
      page,
      page_size: 20,
    },
    { enabled: authenticated },
  );

  const detail = useApiQuery<MerchantDetail>(
    `/merchants/${selectedId ?? ""}`,
    undefined,
    { enabled: authenticated && Boolean(selectedId) },
  );

  const regions = useApiQuery<{ items: OptionItem[] }>("/master-data/regions", undefined, {
    enabled: authenticated && hasAbility("merchant"),
  });
  const brands = useApiQuery<{ items: OptionItem[] }>("/master-data/brands", undefined, {
    enabled: authenticated && hasAbility("merchant"),
  });

  const createMut = useApiMutation(
    async (payload: {
      code: string;
      name: string;
      region_id: string;
      brand_id: string;
      address?: string;
      contact_phone?: string;
      primary_contact_name?: string;
    }) => api.post<{ id: string; code: string }>("/merchants", payload),
  );
  const gradeMut = useApiMutation(
    async (payload: { id: string; new_grade: string; effective_from: string; reason: string }) =>
      api.post(`/merchants/${payload.id}/grade-change`, {
        new_grade: payload.new_grade,
        effective_from: payload.effective_from,
        reason: payload.reason,
      }),
  );
  const statusMut = useApiMutation(
    async (payload: { id: string; new_status: string; reason: string }) =>
      api.post<{ merchant_id: string; old_status: string; new_status: string }>(
        `/merchants/${payload.id}/status-change`,
        { new_status: payload.new_status, reason: payload.reason },
      ),
  );

  const columns: DataTableColumn<MerchantRow>[] = [
    { key: "code", header: "编码", render: (r) => <span className="font-mono text-xs">{r.code}</span> },
    {
      key: "name",
      header: "商家名称",
      render: (r) => (
        <button
          type="button"
          onClick={() => selectMerchant(r.id)}
          className="text-left font-medium text-[var(--color-brand)] hover:underline"
        >
          {r.name}
        </button>
      ),
    },
    {
      key: "status",
      header: "状态",
      render: (r) => <StatusBadge tone={statusTone(r.status)} label={merchantStatusLabel(r.status)} />,
    },
    { key: "grade", header: "等级", render: (r) => r.grade ?? "—" },
    { key: "address", header: "地址", render: (r) => r.address ?? "—" },
    { key: "phone", header: "联系电话", render: (r) => r.contact_phone ?? "—" },
    {
      key: "created",
      header: "创建时间",
      render: (r) => <span className="text-xs text-slate-500">{formatDate(r.created_at)}</span>,
    },
  ];

  if (!authenticated) {
    return (
      <div className="space-y-4">
        <PageHeader title="商家中心" description="商家档案、责任归属与等级" />
        <SessionNotice hint="当前是开发注入账号，请从登录页用真实账号登录后再查看业务数据。" />
      </div>
    );
  }

  const total = list.data?.pagination.total ?? 0;
  const totalPages = Math.max(1, Math.ceil(total / (list.data?.pagination.page_size ?? 20)));

  return (
    <div className="space-y-4">
      <PageHeader
        title="商家中心"
        description="商家档案、责任归属与等级（行级范围由服务端按角色注入）"
        actions={
          <button
            type="button"
            onClick={() => setCreating((v) => !v)}
            className="rounded-md bg-[var(--color-brand)] px-3 py-1.5 text-sm font-medium text-white hover:opacity-90"
          >
            {creating ? "收起新建" : "新建商家"}
          </button>
        }
      />

      {creating ? (
        <CreateMerchantForm
          regions={regions.data?.items ?? []}
          brands={brands.data?.items ?? []}
          submitting={createMut.submitting}
          error={createMut.error}
          onCancel={() => setCreating(false)}
          onSubmit={async (payload) => {
            const res = await createMut.run(payload);
            if (res) {
              push(`商家 ${res.code} 已创建`, "success");
              setCreating(false);
              list.refetch();
            } else {
              push(createMut.error || "创建失败", "danger");
            }
          }}
        />
      ) : null}

      <FilterBar
        onApply={() => {
          setApplied({ keyword: keywordInput, status: statusInput, grade: gradeInput });
          setPage(1);
        }}
        onReset={() => {
          setKeywordInput("");
          setStatusInput("");
          setGradeInput("");
          setApplied({ keyword: "", status: "", grade: "" });
          setPage(1);
        }}
      >
        <Field label="关键字">
          <input
            value={keywordInput}
            onChange={(e) => setKeywordInput(e.target.value)}
            placeholder="编码 / 名称"
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
                {merchantStatusLabel(s)}
              </option>
            ))}
          </select>
        </Field>
        <Field label="等级">
          <select
            value={gradeInput}
            onChange={(e) => setGradeInput(e.target.value)}
            className="rounded-md border border-slate-300 bg-white px-2 py-1.5 text-sm"
          >
            <option value="">全部</option>
            {GRADE_OPTIONS.map((g) => (
              <option key={g} value={g}>
                {g}
              </option>
            ))}
          </select>
        </Field>
      </FilterBar>

      {list.loading ? <LoadingBlock text="加载商家列表…" /> : null}
      {list.error ? (
        <ErrorState message={list.error} status={list.status} onRetry={list.refetch} />
      ) : null}

      {!list.loading && !list.error ? (
        <>
          <div className="text-xs text-slate-500">共 {total} 家商家</div>
          <DataTable
            rows={list.data?.items ?? []}
            columns={columns}
            rowKey={(r) => r.id}
            emptyText="暂无商家数据（可用右上角新建，或先导入主数据）"
          />
          <Pagination page={page} totalPages={totalPages} onPageChange={setPage} />
        </>
      ) : null}

      {selectedId ? (
        <MerchantDetailPanel
          detail={detail.data}
          loading={detail.loading}
          error={detail.error}
          status={detail.status}
          onClose={() => selectMerchant(null)}
          onRetry={detail.refetch}
          canChangeGrade={hasAbility("merchant")}
          gradeSubmitting={gradeMut.submitting}
          gradeError={gradeMut.error}
          onChangeGrade={async (payload) => {
            const res = await gradeMut.run({ id: selectedId, ...payload });
            if (res) {
              push("等级变更已生效", "success");
              detail.refetch();
              list.refetch();
            } else {
              push(gradeMut.error || "等级变更失败", "danger");
            }
          }}
          canChangeStatus={
            // 主路径：本人负责的商家（业务员）；兜底：片区经理（列表已按片区过滤）
            user?.role === "REGIONAL_MANAGER" ||
            (user?.role === "SALES_REP" &&
              detail.data?.current_owner?.owner_user_id === user.id)
          }
          statusSubmitting={statusMut.submitting}
          statusError={statusMut.error}
          onChangeStatus={async (payload) => {
            const res = await statusMut.run({ id: selectedId, ...payload });
            if (res) {
              push(`商家状态已更新为「${merchantStatusLabel(res.new_status)}」`, "success");
              detail.refetch();
              list.refetch();
            } else {
              push(statusMut.error || "状态变更失败", "danger");
            }
          }}
          canTransfer={
            // 调拨限片区经理/系统管理员（业务员无片区范围，后端 _assert_region 会拒）
            hasAbility("merchant") &&
            (user?.role === "REGIONAL_MANAGER" || user?.role === "SYSTEM_ADMIN")
          }
          onTransferChanged={() => {
            detail.refetch();
            list.refetch();
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

function CreateMerchantForm({
  regions,
  brands,
  submitting,
  error,
  onSubmit,
  onCancel,
}: {
  regions: OptionItem[];
  brands: OptionItem[];
  submitting: boolean;
  error: string;
  onSubmit: (payload: {
    code: string;
    name: string;
    region_id: string;
    brand_id: string;
    address?: string;
    contact_phone?: string;
    primary_contact_name?: string;
  }) => void;
  onCancel: () => void;
}) {
  const [code, setCode] = useState("");
  const [name, setName] = useState("");
  const [regionId, setRegionId] = useState("");
  const [brandId, setBrandId] = useState("");
  const [address, setAddress] = useState("");
  const [phone, setPhone] = useState("");
  const [contact, setContact] = useState("");

  const missingOptions = regions.length === 0 || brands.length === 0;

  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        onSubmit({
          code: code.trim(),
          name: name.trim(),
          region_id: regionId || regions[0]?.id || "",
          brand_id: brandId || brands[0]?.id || "",
          address: address.trim() || undefined,
          contact_phone: phone.trim() || undefined,
          primary_contact_name: contact.trim() || undefined,
        });
      }}
      className="space-y-3 rounded-md border border-slate-200 bg-white p-4"
    >
      <div className="text-sm font-medium text-slate-700">新建商家</div>
      {missingOptions ? (
        <div className="rounded-md border border-amber-200 bg-amber-50 p-2 text-xs text-amber-800">
          尚未取到片区/品牌选项（`/api/master-data/regions|brands`）。请先导入主数据。
        </div>
      ) : null}
      <div className="grid grid-cols-1 gap-3 md:grid-cols-3">
        <Field label="商家编码 *">
          <input
            required
            value={code}
            onChange={(e) => setCode(e.target.value)}
            className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
          />
        </Field>
        <Field label="商家名称 *">
          <input
            required
            value={name}
            onChange={(e) => setName(e.target.value)}
            className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
          />
        </Field>
        <Field label="片区 *">
          <select
            required
            value={regionId}
            onChange={(e) => setRegionId(e.target.value)}
            className="rounded-md border border-slate-300 bg-white px-2 py-1.5 text-sm"
          >
            <option value="">请选择</option>
            {regions.map((r) => (
              <option key={r.id} value={r.id}>
                {r.name}
              </option>
            ))}
          </select>
        </Field>
        <Field label="品牌 *">
          <select
            required
            value={brandId}
            onChange={(e) => setBrandId(e.target.value)}
            className="rounded-md border border-slate-300 bg-white px-2 py-1.5 text-sm"
          >
            <option value="">请选择</option>
            {brands.map((b) => (
              <option key={b.id} value={b.id}>
                {b.name}
              </option>
            ))}
          </select>
        </Field>
        <Field label="地址">
          <input
            value={address}
            onChange={(e) => setAddress(e.target.value)}
            className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
          />
        </Field>
        <Field label="联系电话">
          <input
            value={phone}
            onChange={(e) => setPhone(e.target.value)}
            className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
          />
        </Field>
        <Field label="主要联系人">
          <input
            value={contact}
            onChange={(e) => setContact(e.target.value)}
            className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
          />
        </Field>
      </div>
      {error ? <div className="text-xs text-rose-600">{error}</div> : null}
      <div className="flex gap-2">
        <button
          type="submit"
          disabled={submitting || missingOptions}
          className="rounded-md bg-[var(--color-brand)] px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50"
        >
          {submitting ? "提交中…" : "创建"}
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

/** 商家调拨（T14）：选新负责人 → 生效日期 → 原因 → 预览 → 提交调拨单 → 立即生效。 */
function MerchantTransferPanel({
  merchantId,
  onChanged,
}: {
  merchantId: string;
  onChanged: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [targetId, setTargetId] = useState("");
  const [effectiveDate, setEffectiveDate] = useState(isoDatePlus(1));
  const [reason, setReason] = useState("");
  const [preview, setPreview] = useState<TransferPreview | null>(null);
  const [created, setCreated] = useState<TransferOut | null>(null);
  const [notice, setNotice] = useState<{ tone: "success" | "danger"; text: string } | null>(
    null,
  );

  const options = useApiQuery<TransferOptions>(
    "/v1/merchants/transfers/options",
    { merchant_id: merchantId },
    { enabled: open },
  );

  const previewMut = useApiMutation(
    (payload: {
      merchant_id: string;
      from_user_id: string;
      to_user_id: string;
      effective_date: string;
      reason: string;
    }) => api.post<TransferPreview>("/v1/merchants/transfers/preview", payload),
  );
  const createMut = useApiMutation(
    (payload: {
      merchant_id: string;
      from_user_id: string;
      to_user_id: string;
      effective_date: string;
      reason: string;
    }) => api.post<TransferOut>("/v1/merchants/transfers", payload),
  );
  const applyMut = useApiMutation((transferId: string) =>
    api.post<TransferOut>(`/v1/merchants/transfers/${transferId}/apply`),
  );

  const fromUser = options.data?.from_user ?? null;
  const candidates = (options.data?.items ?? []).filter((u) => u.id !== fromUser?.id);

  function resetForm() {
    setTargetId("");
    setEffectiveDate(isoDatePlus(1));
    setReason("");
    setPreview(null);
  }

  function toggle() {
    setNotice(null);
    if (open) {
      setOpen(false);
      setCreated(null);
      resetForm();
    } else {
      setOpen(true);
    }
  }

  function requestPreview() {
    if (!fromUser || !targetId) {
      setNotice({ tone: "danger", text: "请选择新负责业务员" });
      return;
    }
    previewMut
      .runSafe({
        merchant_id: merchantId,
        from_user_id: fromUser.id,
        to_user_id: targetId,
        effective_date: effectiveDate,
        reason: reason.trim() || "商家调拨",
      })
      .then((out) => {
        if (out.ok && out.data) {
          setPreview(out.data);
          setNotice(null);
        } else {
          setNotice({ tone: "danger", text: out.error || "预览失败" });
        }
      });
  }

  function submitTransfer() {
    if (!preview || !fromUser) return;
    createMut
      .runSafe({
        merchant_id: merchantId,
        from_user_id: fromUser.id,
        to_user_id: preview.to_user.id,
        effective_date: preview.effective_date,
        reason: reason.trim() || "商家调拨",
      })
      .then((out) => {
        if (out.ok && out.data) {
          setCreated(out.data);
          setPreview(null);
          setNotice({
            tone: "success",
            text: `调拨单已创建（待生效）：${out.data.effective_date}`,
          });
          onChanged();
        } else {
          setNotice({ tone: "danger", text: out.error || "提交失败" });
        }
      });
  }

  function applyTransfer() {
    if (!created) return;
    applyMut.runSafe(created.id).then((out) => {
      if (out.ok && out.data) {
        setNotice({
          tone: "success",
          text: `调拨已生效（${out.data.effective_date}）：商家归属已切给新负责人`,
        });
        setCreated(null);
        setOpen(false);
        resetForm();
        onChanged();
      } else {
        setNotice({ tone: "danger", text: out.error || "生效失败" });
      }
    });
  }

  return (
    <section className="space-y-2 rounded-md border border-sky-200 bg-sky-50/40 p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="text-xs font-medium text-slate-600">
          商家调拨（更换负责业务员；生效日前可撤回，生效后只能反向调拨）
        </div>
        <button
          type="button"
          onClick={toggle}
          className="rounded-md border border-sky-300 bg-white px-3 py-1.5 text-sm text-sky-700 hover:bg-sky-50"
        >
          {open ? "收起调拨" : "发起调拨"}
        </button>
      </div>

      {notice ? (
        <p
          className={
            notice.tone === "danger" ? "text-xs text-rose-600" : "text-xs text-emerald-700"
          }
        >
          {notice.text}
        </p>
      ) : null}

      {open ? (
        options.loading ? (
          <LoadingBlock text="加载业务员选项…" />
        ) : options.error ? (
          <ErrorState
            message={options.error}
            status={options.status}
            onRetry={options.refetch}
          />
        ) : !fromUser ? (
          <p className="text-xs text-amber-700">
            当前商家没有主负责业务员，无法发起调拨（后端 TRANSFER_NO_CURRENT_OWNER）。
          </p>
        ) : created ? (
          <div className="space-y-2 rounded-md border border-emerald-200 bg-white p-3 text-sm">
            <div>
              调拨单 <span className="font-mono text-xs">{created.id.slice(0, 8)}</span>{" "}
              已创建，状态{" "}
              <StatusBadge tone={statusTone(created.status)} label={label(created.status)} />
              生效日 {formatDate(created.effective_date)}
            </div>
            <div className="flex gap-2">
              <button
                type="button"
                disabled={applyMut.submitting}
                onClick={applyTransfer}
                className="rounded-md bg-[var(--color-brand)] px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50"
              >
                {applyMut.submitting ? "生效中…" : "立即生效"}
              </button>
              <button
                type="button"
                onClick={() => {
                  setCreated(null);
                  resetForm();
                }}
                className="rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm text-slate-600"
              >
                稍后生效
              </button>
            </div>
            <p className="text-xs text-slate-500">
              生效会按生效日切分归属区间，并把未完成巡访任务与待办转给新负责人；已完成事实不回写。
            </p>
          </div>
        ) : (
          <form
            onSubmit={(e) => {
              e.preventDefault();
              requestPreview();
            }}
            className="space-y-2 rounded-md border border-slate-200 bg-white p-3"
          >
            <div className="grid grid-cols-1 gap-2 md:grid-cols-4">
              <Field label="原负责人">
                <div className="py-1.5 text-sm text-slate-700">{fromUser.display_name}</div>
              </Field>
              <Field label="新负责业务员 *">
                <select
                  required
                  value={targetId}
                  onChange={(e) => {
                    setTargetId(e.target.value);
                    setPreview(null);
                  }}
                  className="rounded-md border border-slate-300 bg-white px-2 py-1.5 text-sm"
                >
                  <option value="">请选择</option>
                  {candidates.map((u) => (
                    <option key={u.id} value={u.id}>
                      {u.display_name}
                    </option>
                  ))}
                </select>
              </Field>
              <Field label="生效日 *">
                <input
                  required
                  type="date"
                  min={isoDatePlus(0)}
                  value={effectiveDate}
                  onChange={(e) => {
                    setEffectiveDate(e.target.value);
                    setPreview(null);
                  }}
                  className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
                />
              </Field>
              <Field label="调拨原因 *">
                <input
                  required
                  value={reason}
                  onChange={(e) => {
                    setReason(e.target.value);
                    setPreview(null);
                  }}
                  className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
                />
              </Field>
            </div>
            {candidates.length === 0 ? (
              <p className="text-xs text-amber-700">
                本片区没有可承接的其他在职业务员（需已授权该片区）。
              </p>
            ) : null}
            <div className="flex gap-2">
              <button
                type="submit"
                disabled={previewMut.submitting || candidates.length === 0}
                className="rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm text-slate-700 disabled:opacity-50"
              >
                {previewMut.submitting ? "预览中…" : "预览影响"}
              </button>
              <button
                type="button"
                disabled={!preview || createMut.submitting}
                onClick={submitTransfer}
                className="rounded-md bg-[var(--color-brand)] px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50"
              >
                {createMut.submitting ? "提交中…" : "提交调拨单"}
              </button>
            </div>
            <p className="text-xs text-slate-400">
              先预览再提交：预览只读，不写库；提交后调拨单为待生效，可再点击「立即生效」。
            </p>
          </form>
        )
      ) : null}

      {preview ? (
        <div className="rounded-md border border-slate-200 bg-white p-3 text-xs text-slate-600">
          <div className="font-medium text-slate-700">
            {preview.from_user.display_name} → {preview.to_user.display_name}
            （生效日 {formatDate(preview.effective_date)}）
          </div>
          <div className="mt-1">
            未完成巡访 {preview.open_visit_count} 个 · 待办 {preview.open_todo_count} 条 ·
            关联合同 {preview.contracts.length} 份
          </div>
          <div className="mt-1 text-slate-500">{preview.performance_boundary}</div>
        </div>
      ) : null}

      {applyMut.error && !notice ? (
        <p className="text-xs text-rose-600">{applyMut.error}</p>
      ) : null}
    </section>
  );
}

function MerchantDetailPanel({
  detail,
  loading,
  error,
  status,
  onClose,
  onRetry,
  canChangeGrade,
  gradeSubmitting,
  gradeError,
  onChangeGrade,
  canChangeStatus,
  statusSubmitting,
  statusError,
  onChangeStatus,
  canTransfer,
  onTransferChanged,
}: {
  detail: MerchantDetail | null;
  loading: boolean;
  error: string;
  status: number | null;
  onClose: () => void;
  onRetry: () => void;
  canChangeGrade: boolean;
  gradeSubmitting: boolean;
  gradeError: string;
  onChangeGrade: (p: { new_grade: string; effective_from: string; reason: string }) => void;
  canChangeStatus: boolean;
  statusSubmitting: boolean;
  statusError: string;
  onChangeStatus: (p: { new_status: string; reason: string }) => void;
  canTransfer: boolean;
  onTransferChanged: () => void;
}) {
  const [newGrade, setNewGrade] = useState("");
  const [effectiveFrom, setEffectiveFrom] = useState("");
  const [reason, setReason] = useState("");
  const [newStatus, setNewStatus] = useState("");
  const [statusReason, setStatusReason] = useState("");
  const allNextStatuses = STATUS_TRANSITIONS[detail?.status ?? ""] ?? [];
  // 详情接口返回该商家的全部合同（无截断），据此判断「合作中」前置是否满足；
  // 到期与否由后端裁决，这里只挡住"完全没有生效合同"这一必然失败的分支。
  const hasEffectiveContract = (detail?.contracts ?? []).some(
    (c) => c.status === "EFFECTIVE",
  );
  const blockedActive = allNextStatuses.includes("ACTIVE") && !hasEffectiveContract;
  const nextStatuses = allNextStatuses.filter((s) => s !== "ACTIVE" || hasEffectiveContract);

  return (
    <section className="space-y-3 rounded-md border border-slate-200 bg-white p-4">
      <div className="flex items-start justify-between">
        <h2 className="text-sm font-semibold text-slate-700">商家档案</h2>
        <button type="button" onClick={onClose} className="text-xs text-slate-500 hover:underline">
          关闭
        </button>
      </div>

      {loading ? <LoadingBlock text="加载档案…" /> : null}
      {error ? <ErrorState message={error} status={status} onRetry={onRetry} /> : null}

      {detail && !loading && !error ? (
        <>
          <div className="grid grid-cols-2 gap-3 text-sm md:grid-cols-4">
            <Info label="编码" value={detail.code} />
            <Info label="名称" value={detail.name} />
            <Info
              label="状态"
              value={<StatusBadge tone={statusTone(detail.status)} label={merchantStatusLabel(detail.status)} />}
            />
            <Info label="等级" value={detail.grade ?? "—"} />
            <Info label="地址" value={detail.address ?? "—"} />
            <Info
              label="联系电话"
              value={detail.contact_masked ? "已脱敏" : (detail.contact_phone ?? "—")}
            />
            <Info label="营业时间" value={detail.business_hours ?? "—"} />
            <Info label="累计巡访" value={String(detail.visits)} />
          </div>

          <div className="grid grid-cols-1 gap-3 md:grid-cols-3">
            <MiniList title={`合同（${detail.contracts?.length ?? 0}）`}>
              {(detail.contracts ?? []).map((c) => (
                <li key={c.id} className="flex justify-between gap-2">
                  <span className="font-mono text-xs">{c.contract_no}</span>
                  <StatusBadge tone={statusTone(c.status)} label={label(c.status)} />
                </li>
              ))}
            </MiniList>
            <MiniList title={`冰柜（${detail.freezers?.length ?? 0}）`}>
              {(detail.freezers ?? []).map((f) => (
                <li key={f.id} className="flex justify-between gap-2">
                  <span className="font-mono text-xs">{f.asset_code}</span>
                  <StatusBadge tone={statusTone(f.status)} label={label(f.status)} />
                </li>
              ))}
            </MiniList>
            <MiniList title={`补货单（${detail.replenishments?.length ?? 0}）`}>
              {(detail.replenishments ?? []).map((r) => (
                <li key={r.id} className="flex justify-between gap-2">
                  <span className="font-mono text-xs">{r.code}</span>
                  <span className="text-xs text-slate-500">
                    {boxes(r.requested_boxes)} 箱 · {label(r.status)}
                  </span>
                </li>
              ))}
            </MiniList>
          </div>

          {canChangeStatus ? (
            nextStatuses.length > 0 ? (
              <form
                onSubmit={(e) => {
                  e.preventDefault();
                  onChangeStatus({ new_status: newStatus, reason: statusReason });
                }}
                className="space-y-2 rounded-md border border-slate-200 bg-slate-50 p-3"
              >
                <div className="text-xs font-medium text-slate-600">
                  状态变更（由负责业务员维护，留原因可追溯）
                </div>
                <div className="grid grid-cols-1 gap-2 md:grid-cols-2">
                  <Field label="新状态 *">
                    <select
                      required
                      value={newStatus}
                      onChange={(e) => setNewStatus(e.target.value)}
                      className="rounded-md border border-slate-300 bg-white px-2 py-1.5 text-sm"
                    >
                      <option value="">请选择</option>
                      {nextStatuses.map((s) => (
                        <option key={s} value={s}>
                          {merchantStatusLabel(s)}
                        </option>
                      ))}
                    </select>
                  </Field>
                  <Field label="原因 *">
                    <input
                      required
                      value={statusReason}
                      onChange={(e) => setStatusReason(e.target.value)}
                      className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
                    />
                  </Field>
                </div>
                {statusError ? <div className="text-xs text-rose-600">{statusError}</div> : null}
                <button
                  type="submit"
                  disabled={statusSubmitting}
                  className="rounded-md bg-[var(--color-brand)] px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50"
                >
                  {statusSubmitting ? "提交中…" : "提交变更"}
                </button>
                <p className="text-xs text-slate-400">
                  置为「合作中」需先有已生效且未到期的合同；终止合作需发起「取消合作申请」并经经理审批，
                  不能直接改状态。
                </p>
              </form>
            ) : (
              <div className="rounded-md border border-slate-200 bg-slate-50 p-3 text-xs text-slate-500">
                当前状态（{merchantStatusLabel(detail.status)}）暂无可直接变更的目标状态。
                {blockedActive ? "置为「合作中」需先有已生效且未到期的合同；" : ""}
                终止合作请走取消合作申请。
              </div>
            )
          ) : null}

          {canChangeGrade ? (
            <form
              onSubmit={(e) => {
                e.preventDefault();
                onChangeGrade({ new_grade: newGrade, effective_from: effectiveFrom, reason });
              }}
              className="space-y-2 rounded-md border border-slate-200 bg-slate-50 p-3"
            >
              <div className="text-xs font-medium text-slate-600">等级变更（留原因，可追溯）</div>
              <div className="grid grid-cols-1 gap-2 md:grid-cols-3">
                <Field label="新等级 *">
                  <select
                    required
                    value={newGrade}
                    onChange={(e) => setNewGrade(e.target.value)}
                    className="rounded-md border border-slate-300 bg-white px-2 py-1.5 text-sm"
                  >
                    <option value="">请选择</option>
                    {GRADE_OPTIONS.map((g) => (
                      <option key={g} value={g}>
                        {g}
                      </option>
                    ))}
                  </select>
                </Field>
                <Field label="生效日 *">
                  <input
                    required
                    type="date"
                    value={effectiveFrom}
                    onChange={(e) => setEffectiveFrom(e.target.value)}
                    className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
                  />
                </Field>
                <Field label="原因 *">
                  <input
                    required
                    value={reason}
                    onChange={(e) => setReason(e.target.value)}
                    className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
                  />
                </Field>
              </div>
              {gradeError ? <div className="text-xs text-rose-600">{gradeError}</div> : null}
              <button
                type="submit"
                disabled={gradeSubmitting}
                className="rounded-md bg-[var(--color-brand)] px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50"
              >
                {gradeSubmitting ? "提交中…" : "提交变更"}
              </button>
            </form>
          ) : null}

          {canTransfer ? (
            <MerchantTransferPanel merchantId={detail.id} onChanged={onTransferChanged} />
          ) : null}
        </>
      ) : null}

      {!detail && !loading && !error ? <EmptyState title="请选择商家" /> : null}
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

function MiniList({ title, children }: { title: string; children: React.ReactNode }) {
  const hasChildren = Array.isArray(children) ? children.length > 0 : Boolean(children);
  return (
    <div className="rounded-md border border-slate-200 p-3">
      <div className="text-xs font-medium text-slate-600">{title}</div>
      {hasChildren ? (
        <ul className="mt-2 space-y-1 text-sm">{children}</ul>
      ) : (
        <div className="mt-2 text-xs text-slate-400">暂无</div>
      )}
    </div>
  );
}
