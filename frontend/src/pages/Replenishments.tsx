/** 补货中心（T17 前端联调 / PRD §4.9）。
 *
 * 接入后端：
 * - `GET /api/replenishment/orders`（列表：含 DRAFT/SUBMITTED，行级范围由服务端按角色注入）；
 * - `GET /api/replenishment/orders/{id}`（详情 + 履约流水）；
 * - `GET /api/replenishment/summary/confirmed`（已确认口径箱数，R8 未确认不汇总）；
 * - `GET /api/replenishment/fulfillment-rate`（箱数满足率 / 按时满足率，Q094）；
 * - `POST /api/replenishment/orders/{id}/{submit|confirm|fulfillments|close|cancel}`。
 *
 * 状态链、数量口径（0 ≤ 已满足 ≤ 已确认）、满足率、幂等键与事务边界全在后端
 * ReplenishmentService 裁决；页面只做展示与受控交互（前端隐藏不作为鉴权）。
 */
import { useState } from "react";

import { api } from "../lib/api";
import { useAuth } from "../lib/auth";
import {
  boxes,
  formatDate,
  label,
  money,
  percent,
  statusTone,
  todayInTz,
} from "../lib/format";
import { useApiMutation, useApiQuery } from "../lib/useApi";
import { useToast } from "../lib/useToast";
import { ErrorState, LoadingBlock, SessionNotice } from "../components/AsyncState";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { DataTable, type DataTableColumn } from "../components/DataTable";
import { EmptyState } from "../components/EmptyState";
import { FilterBar } from "../components/FilterBar";
import { MetricCard } from "../components/MetricCard";
import { PageHeader } from "../components/PageHeader";
import { Pagination } from "../components/Pagination";
import { StatusBadge } from "../components/StatusBadge";
import { ToastStack } from "../components/Toast";

/** 受控状态枚举（与后端 ReplenishmentStatus 对齐）。 */
const STATUS_OPTIONS = [
  "DRAFT",
  "SUBMITTED",
  "CONFIRMED",
  "PARTIALLY_FULFILLED",
  "FULFILLED",
  "CLOSED",
  "CANCELLED",
] as const;

/** 可取消状态（与后端 _CANCELLABLE 一致；已闭环/已全履约不可取消）。 */
const CANCELLABLE = new Set([
  "DRAFT",
  "SUBMITTED",
  "CONFIRMED",
  "PARTIALLY_FULFILLED",
  "REQUESTED",
  "IN_TRANSIT",
  "OVERDUE",
]);

const FULFILLABLE = new Set(["CONFIRMED", "PARTIALLY_FULFILLED"]);

interface OrderRow {
  id: string;
  code: string;
  merchant_id: string;
  merchant_code: string;
  merchant_name: string;
  sku_id: string;
  sku_code: string;
  sku_name: string;
  requested_boxes: string;
  committed_boxes: string;
  fulfilled_boxes: string;
  remaining_boxes: string;
  request_date: string;
  promised_delivery_date: string | null;
  actual_delivery_date: string | null;
  status: string;
  confirmer_id: string | null;
  confirmed_at: string | null;
  adjust_reason: string | null;
  cancel_reason: string | null;
}

interface ListResponse {
  items: OrderRow[];
  pagination: { page: number; page_size: number; total: number };
}

interface FulfillmentRow {
  id: string;
  order_id: string;
  delivered_boxes: string;
  delivered_on: string;
  proof_file_id: string | null;
  idempotency_key: string;
  note: string | null;
  registered_by: string;
  /** T36：送达即固化收入/成本/毛利（建议零售价 − 参考进价），单位分 */
  sales_amount_fen?: number | null;
  cost_fen?: number | null;
  profit_fen?: number | null;
  unit_retail_price_fen?: number | null;
  unit_purchase_price_fen?: number | null;
}

interface OrderDetail {
  id: string;
  code: string;
  merchant_id: string;
  sku_id: string;
  requested_boxes: string;
  committed_boxes: string;
  fulfilled_boxes: string;
  request_date: string;
  promised_delivery_date: string | null;
  actual_delivery_date: string | null;
  status: string;
  confirmer_id: string | null;
  confirmed_at: string | null;
  adjust_reason: string | null;
  cancel_reason: string | null;
  fulfillments: FulfillmentRow[];
}

interface SummaryView {
  confirmed_order_count: number;
  confirmed_boxes_total: string;
  by_sku: Record<string, string>;
}

interface RateView {
  confirmed_boxes: string;
  fulfilled_boxes: string;
  on_time_fulfilled_boxes: string;
  order_count: number;
  fulfillment_rate: string;
  on_time_rate: string;
}

interface OptionItem {
  id: string;
  code: string;
  name: string;
}

/** Decimal 比率（"0.7500"）→ 百分比文案。 */
function rateText(value: string | number | null | undefined): string {
  if (value === null || value === undefined || value === "") return "—";
  const n = typeof value === "number" ? value : Number(value);
  if (Number.isNaN(n)) return String(value);
  return percent(n * 100, 1);
}

function newIdempotencyKey(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) return crypto.randomUUID();
  return `repl-${Date.now()}-${Math.floor(Math.random() * 1e6)}`;
}

export function Replenishments() {
  const { authenticated, user } = useAuth();
  const { items: toasts, push, dismiss } = useToast();

  const [statusInput, setStatusInput] = useState("");
  const [regionInput, setRegionInput] = useState("");
  const [fromInput, setFromInput] = useState("");
  const [toInput, setToInput] = useState("");
  const [applied, setApplied] = useState({ status: "", region: "", from: "", to: "" });
  const [page, setPage] = useState(1);
  const [selectedId, setSelectedId] = useState<string | null>(null);

  const role = user?.role;
  const canManage = role === "REGIONAL_MANAGER" || role === "SYSTEM_ADMIN";
  const canSubmit = role === "SALES_REP" || role === "SYSTEM_ADMIN";

  const regions = useApiQuery<{ items: OptionItem[] }>("/master-data/regions", undefined, {
    enabled: authenticated,
  });

  const list = useApiQuery<ListResponse>(
    "/replenishment/orders",
    {
      region_id: applied.region,
      status: applied.status,
      requested_from: applied.from,
      requested_to: applied.to,
      page,
      page_size: 20,
    },
    { enabled: authenticated },
  );

  const metricsQuery = {
    region_id: applied.region,
    requested_from: applied.from,
    requested_to: applied.to,
  };
  const summary = useApiQuery<SummaryView>("/replenishment/summary/confirmed", metricsQuery, {
    enabled: authenticated,
  });
  const rate = useApiQuery<RateView>("/replenishment/fulfillment-rate", metricsQuery, {
    enabled: authenticated,
  });

  const detail = useApiQuery<OrderDetail>(
    `/replenishment/orders/${selectedId ?? ""}`,
    undefined,
    { enabled: authenticated && Boolean(selectedId) },
  );

  const submitMut = useApiMutation((orderId: string) =>
    api.post<OrderDetail>(`/replenishment/orders/${orderId}/submit`),
  );
  const confirmMut = useApiMutation(
    (p: {
      id: string;
      confirmed_boxes: number;
      promised_delivery_date: string | null;
      adjust_reason: string | null;
    }) =>
      api.post<OrderDetail>(`/replenishment/orders/${p.id}/confirm`, {
        confirmed_boxes: p.confirmed_boxes,
        promised_delivery_date: p.promised_delivery_date || null,
        adjust_reason: p.adjust_reason || null,
      }),
  );
  const fulfillMut = useApiMutation(
    (p: {
      id: string;
      delivered_boxes: number;
      delivered_on: string;
      idempotency_key: string;
      note: string | null;
    }) =>
      api.post<FulfillmentRow>(`/replenishment/orders/${p.id}/fulfillments`, {
        delivered_boxes: p.delivered_boxes,
        delivered_on: p.delivered_on,
        idempotency_key: p.idempotency_key,
        note: p.note || null,
      }),
  );
  const closeMut = useApiMutation((p: { id: string; note: string | null }) =>
    api.post<OrderDetail>(`/replenishment/orders/${p.id}/close`, {
      note: p.note || null,
    }),
  );
  const cancelMut = useApiMutation((p: { id: string; reason: string }) =>
    api.post<OrderDetail>(`/replenishment/orders/${p.id}/cancel`, { reason: p.reason }),
  );

  const activeRow = (list.data?.items ?? []).find((r) => r.id === selectedId) ?? null;

  function refreshAll() {
    list.refetch();
    summary.refetch();
    rate.refetch();
    detail.refetch();
  }

  const columns: DataTableColumn<OrderRow>[] = [
    {
      key: "code",
      header: "补货单号",
      render: (r) => (
        <button
          type="button"
          onClick={() => setSelectedId(r.id)}
          className="font-mono text-xs text-[var(--color-brand)] hover:underline"
        >
          {r.code}
        </button>
      ),
    },
    {
      key: "merchant",
      header: "商家",
      render: (r) => (
        <span className="text-slate-800">
          {r.merchant_name}
          <span className="ml-1 font-mono text-xs text-slate-400">{r.merchant_code}</span>
        </span>
      ),
    },
    { key: "sku", header: "SKU", render: (r) => r.sku_name },
    {
      key: "requested",
      header: "需求箱数",
      align: "right",
      render: (r) => <span className="font-mono">{boxes(r.requested_boxes)}</span>,
    },
    {
      key: "committed",
      header: "确认箱数",
      align: "right",
      render: (r) => <span className="font-mono">{boxes(r.committed_boxes)}</span>,
    },
    {
      key: "fulfilled",
      header: "已满足 / 余额",
      align: "right",
      render: (r) => (
        <span className="font-mono">
          {boxes(r.fulfilled_boxes)}
          <span className="text-slate-400"> / {boxes(r.remaining_boxes)}</span>
        </span>
      ),
    },
    { key: "request_date", header: "需求日", render: (r) => formatDate(r.request_date) },
    {
      key: "promised",
      header: "承诺到货",
      render: (r) => (
        <span className={r.promised_delivery_date ? "" : "text-slate-400"}>
          {formatDate(r.promised_delivery_date)}
        </span>
      ),
    },
    {
      key: "status",
      header: "状态",
      render: (r) => <StatusBadge tone={statusTone(r.status)} label={label(r.status)} />,
    },
    {
      key: "action",
      header: "待办理",
      render: (r) => {
        // 让"部分履约怎么继续"可被发现：直接给出当前该做的动作
        const action =
          r.status === "SUBMITTED" && canManage
            ? "去确认"
            : FULFILLABLE.has(r.status) && canManage
              ? "登记送达"
              : r.status === "DRAFT" && canSubmit
                ? "去提交"
                : "查看";
        return (
          <button
            type="button"
            onClick={() => setSelectedId(r.id)}
            className="rounded border border-slate-300 px-2 py-1 text-xs text-slate-700 hover:bg-slate-50"
          >
            {action}
          </button>
        );
      },
    },
  ];

  if (!authenticated) {
    return (
      <div className="space-y-4">
        <PageHeader title="补货中心" description="补货确认与履约（确认数量、承诺到货、送达登记）" />
        <SessionNotice hint="当前是开发注入账号，请从登录页用真实账号登录后再查看业务数据。" />
      </div>
    );
  }

  const total = list.data?.pagination.total ?? 0;
  const totalPages = Math.max(1, Math.ceil(total / (list.data?.pagination.page_size ?? 20)));

  return (
    <div className="space-y-4">
      <PageHeader
        title="补货中心"
        description="补货需求 → 经理确认（可调整数量，须留原因）→ 履约登记 → 闭环；满足量不得超过确认量"
      />

      <FilterBar
        onApply={() => {
          setApplied({
            status: statusInput,
            region: regionInput,
            from: fromInput,
            to: toInput,
          });
          setPage(1);
        }}
        onReset={() => {
          setStatusInput("");
          setRegionInput("");
          setFromInput("");
          setToInput("");
          setApplied({ status: "", region: "", from: "", to: "" });
          setPage(1);
        }}
      >
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
        <Field label="需求日（起）">
          <input
            type="date"
            value={fromInput}
            onChange={(e) => setFromInput(e.target.value)}
            className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
          />
        </Field>
        <Field label="需求日（止）">
          <input
            type="date"
            value={toInput}
            onChange={(e) => setToInput(e.target.value)}
            className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
          />
        </Field>
      </FilterBar>

      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <MetricCard
          label="已确认箱数（未确认不汇总）"
          value={summary.data ? boxes(summary.data.confirmed_boxes_total) : "—"}
          unit="箱"
        />
        <MetricCard
          label="已满足箱数"
          value={rate.data ? boxes(rate.data.fulfilled_boxes) : "—"}
          unit="箱"
        />
        <MetricCard
          label="箱数满足率"
          value={rate.data ? rateText(rate.data.fulfillment_rate) : "—"}
        />
        <MetricCard
          label="按时满足率"
          value={rate.data ? rateText(rate.data.on_time_rate) : "—"}
        />
      </div>

      {list.loading ? <LoadingBlock text="加载补货单…" /> : null}
      {list.error ? (
        <ErrorState message={list.error} status={list.status} onRetry={list.refetch} />
      ) : null}

      {!list.loading && !list.error ? (
        <>
          <div className="text-xs text-slate-500">
            共 {total} 张补货单（含待确认草稿；已确认口径共{" "}
            {summary.data?.confirmed_order_count ?? 0} 张）
          </div>
          <DataTable
            rows={list.data?.items ?? []}
            columns={columns}
            rowKey={(r) => r.id}
            emptyText="暂无补货单（补货草稿由巡访提报生成，或调整筛选条件）"
          />
          <Pagination page={page} totalPages={totalPages} onPageChange={setPage} />
        </>
      ) : null}

      {selectedId ? (
        <OrderDetailPanel
          row={activeRow}
          detail={detail.data}
          loading={detail.loading}
          error={detail.error}
          status={detail.status}
          onClose={() => setSelectedId(null)}
          onRetry={detail.refetch}
          canManage={canManage}
          canSubmit={canSubmit}
          submitting={
            submitMut.submitting ||
            confirmMut.submitting ||
            fulfillMut.submitting ||
            closeMut.submitting ||
            cancelMut.submitting
          }
          actionError={
            submitMut.error ||
            confirmMut.error ||
            fulfillMut.error ||
            closeMut.error ||
            cancelMut.error
          }
          onSubmit={async (id) => {
            const res = await submitMut.run(id);
            if (res) {
              push("补货需求已提交，等待经理确认", "success");
              refreshAll();
            } else {
              push(submitMut.error || "提交失败", "danger");
            }
          }}
          onConfirm={async (p) => {
            const res = await confirmMut.run({ id: p.id, ...p.body });
            if (res) {
              // 确认≠通知厂商：还需到采购页按建议生成采购单，厂商门户才能看到并供货
              push(
                "已确认。请到「采购与供货 → 采购建议」生成采购单，厂商才能看到并确认供货",
                "success",
              );
              refreshAll();
            } else {
              push(confirmMut.error || "确认失败", "danger");
            }
          }}
          onFulfill={async (p) => {
            const res = await fulfillMut.run({
              id: p.id,
              delivered_boxes: p.delivered_boxes,
              delivered_on: p.delivered_on,
              idempotency_key: newIdempotencyKey(),
              note: p.note,
            });
            if (res) {
              // T36：送达即按「建议零售价 − 进价」固化毛利，登记成功直接回显
              const margin =
                res.profit_fen != null
                  ? `，销售额 ${money(res.sales_amount_fen ?? 0)}、毛利 ${money(res.profit_fen)}`
                  : "";
              push(
                `已登记送达 ${boxes(res.delivered_boxes)} 箱${margin}（幂等键已自动生成）`,
                "success",
              );
              refreshAll();
            } else {
              push(fulfillMut.error || "履约登记失败", "danger");
            }
          }}
          onCloseOrder={async (id) => {
            const res = await closeMut.run({ id, note: "全部满足，关闭补货单" });
            if (res) {
              push("补货单已关闭", "success");
              refreshAll();
            } else {
              push(closeMut.error || "关闭失败", "danger");
            }
          }}
          onCancel={async (id, reason) => {
            const res = await cancelMut.run({ id, reason });
            if (res) {
              push("补货单已取消（原因已留痕）", "success");
              refreshAll();
            } else {
              push(cancelMut.error || "取消失败", "danger");
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

interface ConfirmBody {
  confirmed_boxes: number;
  promised_delivery_date: string | null;
  adjust_reason: string | null;
}

function OrderDetailPanel({
  row,
  detail,
  loading,
  error,
  status,
  onClose,
  onRetry,
  canManage,
  canSubmit,
  submitting,
  actionError,
  onSubmit,
  onConfirm,
  onFulfill,
  onCloseOrder,
  onCancel,
}: {
  row: OrderRow | null;
  detail: OrderDetail | null;
  loading: boolean;
  error: string;
  status: number | null;
  onClose: () => void;
  onRetry: () => void;
  canManage: boolean;
  canSubmit: boolean;
  submitting: boolean;
  actionError: string;
  onSubmit: (id: string) => void;
  onConfirm: (p: { id: string; body: ConfirmBody }) => void;
  onFulfill: (p: {
    id: string;
    delivered_boxes: number;
    delivered_on: string;
    note: string | null;
  }) => void;
  onCloseOrder: (id: string) => void;
  onCancel: (id: string, reason: string) => void;
}) {
  const orderId = detail?.id ?? row?.id ?? "";
  const orderStatus = detail?.status ?? row?.status ?? "";
  const committed = Number(detail?.committed_boxes ?? row?.committed_boxes ?? 0);
  const fulfilled = Number(detail?.fulfilled_boxes ?? row?.fulfilled_boxes ?? 0);
  const remaining = Number.isNaN(committed - fulfilled) ? 0 : committed - fulfilled;
  const requested = Number(detail?.requested_boxes ?? row?.requested_boxes ?? 0);
  const promised = detail?.promised_delivery_date ?? row?.promised_delivery_date ?? null;
  // 部分履约 + 已过承诺到货日：明确提示"继续登记 or 差额取消"，避免单子悬空
  const overduePartial =
    orderStatus === "PARTIALLY_FULFILLED" && Boolean(promised && promised < todayInTz());

  const [confirmedBoxes, setConfirmedBoxes] = useState("");
  const [promisedDate, setPromisedDate] = useState("");
  const [adjustReason, setAdjustReason] = useState("");
  const [deliveredBoxes, setDeliveredBoxes] = useState("");
  const [deliveredOn, setDeliveredOn] = useState(todayInTz());
  const [fulfillNote, setFulfillNote] = useState("");
  const [cancelReason, setCancelReason] = useState("");
  const [askingCancel, setAskingCancel] = useState(false);

  const confirmedNumber = Number(confirmedBoxes);
  const boxesInput =
    confirmedBoxes === "" || Number.isNaN(confirmedNumber) ? requested : confirmedNumber;
  const adjusted = boxesInput !== requested;
  const confirmBlocked =
    submitting || boxesInput <= 0 || (adjusted && adjustReason.trim().length === 0);

  return (
    <section className="space-y-3 rounded-md border border-slate-200 bg-white p-4">
      <div className="flex items-start justify-between">
        <div>
          <h2 className="text-sm font-semibold text-slate-700">
            补货单详情
            <span className="ml-2 font-mono text-xs text-slate-500">{orderId.slice(0, 8)}…</span>
          </h2>
          {row ? (
            <div className="mt-0.5 text-xs text-slate-500">
              {row.merchant_name}（{row.merchant_code}）· {row.sku_name}
            </div>
          ) : null}
        </div>
        <button type="button" onClick={onClose} className="text-xs text-slate-500 hover:underline">
          关闭
        </button>
      </div>

      {loading ? <LoadingBlock text="加载补货单…" /> : null}
      {error ? <ErrorState message={error} status={status} onRetry={onRetry} /> : null}

      {detail || row ? (
        <>
          <div className="grid grid-cols-2 gap-3 text-sm md:grid-cols-4">
            <Info label="单号" value={detail?.code ?? row?.code ?? "—"} />
            <Info
              label="状态"
              value={
                <StatusBadge tone={statusTone(orderStatus)} label={label(orderStatus)} />
              }
            />
            <Info label="需求箱数" value={boxes(requested)} />
            <Info label="确认箱数（已确认口径）" value={boxes(committed)} />
            <Info label="已满足箱数" value={boxes(fulfilled)} />
            <Info label="未满足余额" value={boxes(remaining)} />
            <Info label="需求日" value={formatDate(detail?.request_date ?? row?.request_date)} />
            <Info
              label="承诺到货日"
              value={formatDate(detail?.promised_delivery_date ?? row?.promised_delivery_date)}
            />
            <Info
              label="实际到货日"
              value={formatDate(detail?.actual_delivery_date ?? row?.actual_delivery_date)}
            />
            <Info label="确认时间" value={formatDate(detail?.confirmed_at ?? row?.confirmed_at)} />
            <Info
              label="调整原因"
              value={detail?.adjust_reason ?? row?.adjust_reason ?? "—"}
            />
            <Info
              label="取消原因"
              value={detail?.cancel_reason ?? row?.cancel_reason ?? "—"}
            />
          </div>

          {overduePartial ? (
            <div className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800">
              已过承诺到货日（{formatDate(promised)}），仍未满足 {boxes(remaining)} 箱：
              可继续分次登记送达；若厂商确认无法补齐，请填写原因取消订单（差额关闭，已满足量保留）。
            </div>
          ) : null}

          {!canManage && !canSubmit ? (
            <p className="rounded-md border border-slate-200 bg-slate-50 px-3 py-2 text-xs text-slate-600">
              当前角色只读：提交/确认/履约/取消由业务员与片区经理分工执行（后端二次鉴权）。
            </p>
          ) : null}

          {actionError ? <div className="text-xs text-rose-600">{actionError}</div> : null}

          <div className="grid grid-cols-1 gap-3 md:grid-cols-3">
            {canSubmit && orderStatus === "DRAFT" ? (
              <ActionBox title="业务员提交">
                <p className="text-xs text-slate-500">
                  提报需求后由片区经理确认才会进入正式口径（未确认不汇总）。
                </p>
                <button
                  type="button"
                  disabled={submitting}
                  onClick={() => onSubmit(orderId)}
                  className="rounded-md bg-[var(--color-brand)] px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50"
                >
                  {submitting ? "提交中…" : "提交待确认"}
                </button>
              </ActionBox>
            ) : null}

            {canManage && orderStatus === "SUBMITTED" ? (
              <ActionBox title="经理确认">
                <Field label={`确认箱数 *（提报 ${boxes(requested)} 箱，可调整）`}>
                  <input
                    type="number"
                    min="0.01"
                    step="0.01"
                    value={confirmedBoxes}
                    onChange={(e) => setConfirmedBoxes(e.target.value)}
                    placeholder={String(requested)}
                    className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
                  />
                </Field>
                <Field label="承诺到货日（不早于需求日）">
                  <input
                    type="date"
                    value={promisedDate}
                    onChange={(e) => setPromisedDate(e.target.value)}
                    className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
                  />
                </Field>
                {adjusted ? (
                  <Field label="调整原因 *（与提报数量不一致时必填）">
                    <input
                      value={adjustReason}
                      onChange={(e) => setAdjustReason(e.target.value)}
                      className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
                    />
                  </Field>
                ) : null}
                <button
                  type="button"
                  disabled={confirmBlocked}
                  onClick={() =>
                    onConfirm({
                      id: orderId,
                      body: {
                        confirmed_boxes: boxesInput,
                        promised_delivery_date: promisedDate || null,
                        adjust_reason: adjustReason.trim() || null,
                      },
                    })
                  }
                  className="rounded-md bg-[var(--color-brand)] px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50"
                >
                  {submitting ? "提交中…" : "确认"}
                </button>
              </ActionBox>
            ) : null}

            {canManage && FULFILLABLE.has(orderStatus) ? (
              <ActionBox title="登记送达履约">
                <Field label={`本次送达箱数 *（余额 ${boxes(remaining)} 箱，超量整体拒绝）`}>
                  <input
                    type="number"
                    min="0.01"
                    step="0.01"
                    value={deliveredBoxes}
                    onChange={(e) => setDeliveredBoxes(e.target.value)}
                    className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
                  />
                </Field>
                <Field label="送达日期 *">
                  <input
                    type="date"
                    value={deliveredOn}
                    onChange={(e) => setDeliveredOn(e.target.value)}
                    className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
                  />
                </Field>
                <Field label="备注（送货单 / 签收说明）">
                  <input
                    value={fulfillNote}
                    onChange={(e) => setFulfillNote(e.target.value)}
                    className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
                  />
                </Field>
                <button
                  type="button"
                  disabled={submitting || !deliveredBoxes || deliveredBoxes === "0"}
                  onClick={() =>
                    onFulfill({
                      id: orderId,
                      delivered_boxes: Number(deliveredBoxes),
                      delivered_on: deliveredOn || todayInTz(),
                      note: fulfillNote.trim() || null,
                    })
                  }
                  className="rounded-md bg-[var(--color-brand)] px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50"
                >
                  {submitting ? "登记中…" : "登记送达"}
                </button>
                <p className="text-xs text-slate-400">
                  幂等键由页面自动生成：重复提交不会重复累计（A17/Q092）。
                  {remaining > 0
                    ? ` 剩余 ${boxes(remaining)} 箱可继续登记；长期无法补齐时用下方「取消」关闭差额（已满足量保留）。`
                    : ""}
                </p>
              </ActionBox>
            ) : null}

            {canManage && orderStatus === "FULFILLED" ? (
              <ActionBox title="闭环">
                <p className="text-xs text-slate-500">
                  已满足量等于确认量，可关闭补货单（历史不改写）。
                </p>
                <button
                  type="button"
                  disabled={submitting}
                  onClick={() => onCloseOrder(orderId)}
                  className="rounded-md bg-[var(--color-brand)] px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50"
                >
                  {submitting ? "处理中…" : "关闭补货单"}
                </button>
              </ActionBox>
            ) : null}

            {canManage && CANCELLABLE.has(orderStatus) ? (
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
                  取消补货单
                </button>
                <p className="text-xs text-slate-400">已满足量保留，不因取消被抹除（Q093）。</p>
              </ActionBox>
            ) : null}
          </div>

          <div>
            <div className="text-xs font-medium text-slate-600">
              履约流水（{detail?.fulfillments.length ?? 0} 笔）
            </div>
            <div className="mt-2">
              <DataTable
                rows={detail?.fulfillments ?? []}
                columns={[
                  {
                    key: "delivered_on",
                    header: "送达日",
                    render: (f) => formatDate(f.delivered_on),
                  },
                  {
                    key: "boxes",
                    header: "送达箱数",
                    align: "right",
                    render: (f) => <span className="font-mono">{boxes(f.delivered_boxes)}</span>,
                  },
                  {
                    key: "retail",
                    header: "建议零售价/箱",
                    align: "right",
                    render: (f) =>
                      f.unit_retail_price_fen != null
                        ? money(f.unit_retail_price_fen)
                        : "—",
                  },
                  {
                    key: "purchase",
                    header: "进价/箱",
                    align: "right",
                    render: (f) =>
                      f.unit_purchase_price_fen != null
                        ? money(f.unit_purchase_price_fen)
                        : "—",
                  },
                  {
                    key: "revenue",
                    header: "销售额",
                    align: "right",
                    render: (f) =>
                      f.sales_amount_fen != null ? money(f.sales_amount_fen) : "—",
                  },
                  {
                    key: "profit",
                    header: "毛利（售价−进价）",
                    align: "right",
                    render: (f) =>
                      f.profit_fen == null ? (
                        "—"
                      ) : (
                        <span
                          className={
                            f.profit_fen < 0
                              ? "font-medium text-rose-600"
                              : "font-medium text-emerald-700"
                          }
                        >
                          {money(f.profit_fen)}
                        </span>
                      ),
                  },
                  {
                    key: "key",
                    header: "幂等键",
                    render: (f) => (
                      <span className="font-mono text-xs text-slate-500">{f.idempotency_key}</span>
                    ),
                  },
                  { key: "note", header: "备注", render: (f) => f.note ?? "—" },
                ]}
                rowKey={(f) => f.id}
                emptyText="暂无送达记录（仅 CONFIRMED/部分满足可登记）"
              />
            </div>
          </div>
        </>
      ) : (
        <EmptyState title="请选择补货单" description="从左侧列表点击单号查看详情与可执行动作。" />
      )}

      <ConfirmDialog
        open={askingCancel}
        destructive
        title="确认取消补货单？"
        description="取消只做状态迁移并留原因，已满足量保留；取消后不可恢复。"
        confirmText="确认取消"
        onClose={() => setAskingCancel(false)}
        onConfirm={() => {
          setAskingCancel(false);
          onCancel(orderId, cancelReason.trim());
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
