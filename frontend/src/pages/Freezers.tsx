/** 冰柜资产台账（T10 前端联调 / PRD §4.5 / R11、R12、R13、R37）。
 *
 * 接入后端：
 * - `GET /api/freezers?asset_code=&status=&limit=`（行级范围由服务端按角色解析）；
 * - `GET /api/freezers/stock/count`（可用库存口径：仅 IN_STOCK，待验收不计）；
 * - `GET /api/freezers/{id}`（资产 + 全量流水）；
 * - `POST /api/freezers`（购置入库）与投放/回收/验收/维修/报废/退押金各动作端点。
 *
 * 状态机、行锁唯一绑定（一台冰柜任一时刻只绑一个商家）、库存口径、押金不入损益等
 * 业务规则全部由后端 FreezerService 裁决；页面只做展示与受控交互。
 */
import { useMemo, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import { api } from "../lib/api";
import { useAuth } from "../lib/auth";
import { formatDate, formatDateTime, label, money, statusTone } from "../lib/format";
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

const PAGE_SIZE = 20;
const LIST_LIMIT = 100;

/** 受控状态枚举（与后端 FreezerAssetStatus 对齐）。 */
const STATUS_OPTIONS = [
  "IN_STOCK",
  "RESERVED",
  "DEPLOYED",
  "PENDING_PICKUP",
  "PENDING_ACCEPTANCE",
  "REPAIRING",
  "SCRAPPED",
  "IN_TRANSIT",
  "UNDER_MAINTENANCE",
] as const;

/** 与后端 FreezerService 的角色门槛一致（仅用于元素可见性，真正拦截仍由后端）。 */
const FIELD_ROLES = new Set(["SALES_REP", "REGIONAL_MANAGER", "SYSTEM_ADMIN", "SUPERVISOR"]);
const WAREHOUSE_ROLES = new Set(["SUPERVISOR", "SYSTEM_ADMIN", "REGIONAL_MANAGER"]);
const FINANCE_ROLES = new Set(["FINANCE", "SYSTEM_ADMIN"]);
/** 撤回申请审批人（不可自审） */
const RECALL_APPROVER_ROLES = new Set(["REGIONAL_MANAGER"]);

const RECALL_STATUS_LABEL: Record<string, string> = {
  PENDING: "撤回待审批",
  APPROVED: "撤回已同意",
  REJECTED: "撤回已驳回",
};

interface FreezerRow {
  id: string;
  asset_code: string;
  brand_id: string;
  model: string;
  purchase_date: string;
  purchase_price_fen: number;
  deposit_fen: number | null;
  status: string;
  current_merchant_id: string | null;
  responsible_user_id: string | null;
  last_deployed_at: string | null;
  last_recalled_at: string | null;
  repair_count: number;
  is_active: boolean;
  recall_status: string | null;
  recall_planned_date: string | null;
  recall_reason: string | null;
  recall_requested_by: string | null;
  recall_requested_at: string | null;
  recall_decided_by: string | null;
  recall_decided_at: string | null;
  recall_decision_note: string | null;
}

interface FreezerMovementRow {
  id: string;
  asset_id: string;
  movement_type: string;
  doc_no: string | null;
  merchant_id: string | null;
  contract_id: string | null;
  related_movement_id: string | null;
  amount_fen: number | null;
  from_status: string | null;
  to_status: string | null;
  occurred_at: string;
  attachment_id: string | null;
  operator_id: string;
  note: string | null;
}

interface FreezerDetailResponse {
  asset: FreezerRow;
  movements: FreezerMovementRow[];
}

interface OptionItem {
  id: string;
  code: string;
  name: string;
}

/** 元 → 分（空串返回 undefined，交给后端用台账默认值）。 */
function yuanToFenInput(value: string): number | undefined {
  if (value.trim() === "") return undefined;
  const n = Number(value);
  if (Number.isNaN(n) || n < 0) return undefined;
  return Math.round(n * 100);
}

export function Freezers() {
  const { authenticated, user } = useAuth();
  const { items: toasts, push, dismiss } = useToast();
  const navigate = useNavigate();

  const [codeInput, setCodeInput] = useState("");
  const [statusInput, setStatusInput] = useState("");
  const [applied, setApplied] = useState({ asset_code: "", status: "" });
  const [page, setPage] = useState(1);

  const canPurchase =
    Boolean(user?.role) &&
    (WAREHOUSE_ROLES.has(user?.role ?? "") || FINANCE_ROLES.has(user?.role ?? ""));

  const list = useApiQuery<FreezerRow[]>(
    "/freezers",
    { asset_code: applied.asset_code, status: applied.status, limit: LIST_LIMIT },
    { enabled: authenticated },
  );
  const stock = useApiQuery<{ in_stock: number }>("/freezers/stock/count", undefined, {
    enabled: authenticated,
  });

  const rows = useMemo(() => list.data ?? [], [list.data]);
  const total = rows.length;
  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const safePage = Math.min(page, totalPages);
  const pageRows = rows.slice((safePage - 1) * PAGE_SIZE, safePage * PAGE_SIZE);

  const columns: DataTableColumn<FreezerRow>[] = [
    {
      key: "asset_code",
      header: "资产编码",
      render: (r) => (
        <button
          type="button"
          onClick={() => navigate(`/assets/freezers/${r.id}`)}
          className="font-mono text-xs font-medium text-[var(--color-brand)] hover:underline"
        >
          {r.asset_code}
        </button>
      ),
    },
    { key: "model", header: "型号", render: (r) => r.model },
    {
      key: "status",
      header: "状态",
      render: (r) => <StatusBadge tone={statusTone(r.status)} label={label(r.status)} />,
    },
    {
      key: "merchant",
      header: "当前商家",
      render: (r) => (
        <span className="font-mono text-xs text-slate-500">
          {r.current_merchant_id ? `${r.current_merchant_id.slice(0, 8)}…` : "—"}
        </span>
      ),
    },
    {
      key: "purchase_date",
      header: "购置日期",
      render: (r) => <span className="text-xs text-slate-600">{formatDate(r.purchase_date)}</span>,
    },
    { key: "price", header: "购置价", align: "right", render: (r) => money(r.purchase_price_fen) },
    { key: "deposit", header: "押金", align: "right", render: (r) => money(r.deposit_fen) },
    { key: "repair_count", header: "维修次数", align: "right", render: (r) => String(r.repair_count) },
    {
      key: "last_deployed_at",
      header: "最近投放",
      render: (r) => (
        <span className="text-xs text-slate-500">{formatDateTime(r.last_deployed_at)}</span>
      ),
    },
    {
      key: "last_recalled_at",
      header: "最近回收",
      render: (r) => (
        <span className="text-xs text-slate-500">{formatDateTime(r.last_recalled_at)}</span>
      ),
    },
    {
      key: "recall",
      header: "撤回申请",
      render: (r) =>
        r.recall_status ? (
          <StatusBadge
            tone={r.recall_status === "PENDING" ? "warning" : r.recall_status === "APPROVED" ? "success" : "danger"}
            label={RECALL_STATUS_LABEL[r.recall_status] ?? r.recall_status}
          />
        ) : (
          <span className="text-xs text-slate-400">—</span>
        ),
    },
    {
      key: "is_active",
      header: "在用",
      render: (r) => (r.is_active ? "是" : "停用"),
    },
  ];

  if (!authenticated) {
    return (
      <div className="space-y-4">
        <PageHeader title="冰柜资产台账" description="冰柜投放、撤回、验收与维修" />
        <SessionNotice hint="当前是开发注入账号，请从登录页用真实账号登录后再查看业务数据。" />
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <PageHeader
        title="冰柜资产台账"
        description="投放、撤回、验收与维修全流水（行级范围与状态机由服务端裁决）"
      />

      <div className="grid grid-cols-1 gap-3 md:grid-cols-3">
        <MetricCard label="可用库存（仅在用·在库）" value={stock.data?.in_stock ?? "—"} unit="台" />
        <MetricCard label="台账记录" value={total} unit="台" />
        <MetricCard
          label="在店（已投放/待撤回）"
          value={rows.filter((r) => ["DEPLOYED", "PENDING_PICKUP"].includes(r.status)).length}
          unit="台"
        />
      </div>
      <p className="text-xs text-slate-400">
        库存口径说明：回收完成只到「待验收」，仓库验收为「在库」才计入可用库存（R11）。
      </p>

      <FilterBar
        onApply={() => {
          setApplied({ asset_code: codeInput, status: statusInput });
          setPage(1);
        }}
        onReset={() => {
          setCodeInput("");
          setStatusInput("");
          setApplied({ asset_code: "", status: "" });
          setPage(1);
        }}
      >
        <Field label="资产编码">
          <input
            value={codeInput}
            onChange={(e) => setCodeInput(e.target.value)}
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
      </FilterBar>

      {list.loading ? <LoadingBlock text="加载冰柜台账…" /> : null}
      {list.error ? (
        <ErrorState message={list.error} status={list.status} onRetry={list.refetch} />
      ) : null}

      {!list.loading && !list.error ? (
        <>
          <div className="text-xs text-slate-500">
            共 {total} 台（单次最多返回 {LIST_LIMIT} 条，可用筛选缩小范围）
          </div>
          <DataTable
            rows={pageRows}
            columns={columns}
            rowKey={(r) => r.id}
            emptyText="暂无冰柜资产（可用下方「购置入库」建台账）"
          />
          <Pagination page={safePage} totalPages={totalPages} onPageChange={setPage} />
          <PurchaseFreezerForm
            enabled={canPurchase}
            onCreated={(code) => {
              push(`冰柜 ${code} 已入库`, "success");
              list.refetch();
              stock.refetch();
            }}
          />
        </>
      ) : null}

      <ToastStack items={toasts} onDismiss={dismiss} />
    </div>
  );
}

/** 冰柜详情与流水路由（`/assets/freezers/:id`）。 */
export function FreezerDetailPage() {
  const { id } = useParams<{ id: string }>();
  if (!id) {
    return (
      <div className="space-y-4">
        <PageHeader title="冰柜详情与流水" />
        <EmptyState title="缺少冰柜 ID" description="请从冰柜资产台账列表进入详情。" />
      </div>
    );
  }
  return (
    <div className="space-y-4">
      <PageHeader
        title="冰柜详情与流水"
        description="状态迁移严格按状态机执行；每次迁移都留资产流水"
        actions={
          <Link
            to="/assets/freezers"
            className="rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm text-slate-600 hover:bg-slate-50"
          >
            返回台账
          </Link>
        }
      />
      <FreezerDetail id={id} />
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

function Info({ label: text, value }: { label: string; value: React.ReactNode }) {
  return (
    <div>
      <div className="text-xs text-slate-500">{text}</div>
      <div className="mt-0.5 text-slate-800">{value}</div>
    </div>
  );
}

function FreezerDetail({ id }: { id: string }) {
  const { authenticated, user } = useAuth();
  const { items: toasts, push, dismiss } = useToast();
  const role = user?.role ?? "";

  const isField = FIELD_ROLES.has(role);
  const isWarehouse = WAREHOUSE_ROLES.has(role);
  const isFinance = FINANCE_ROLES.has(role);
  const isRecallApprover = RECALL_APPROVER_ROLES.has(role);
  const inputCls = "rounded-md border border-slate-300 px-2 py-1.5 text-sm";

  const detail = useApiQuery<FreezerDetailResponse>(`/freezers/${id}`, undefined, {
    enabled: authenticated,
  });
  const contracts = useApiQuery<{ id: string; contract_no: string; title: string; status: string }[]>(
    "/contracts",
    undefined,
    { enabled: authenticated && isField },
  );

  const deployMut = useApiMutation(
    (payload: { contract_id: string; deposit_yuan: string; note: string }) =>
      api.post(`/freezers/${id}/deployments`, {
        contract_id: payload.contract_id,
        deposit_fen: yuanToFenInput(payload.deposit_yuan),
        note: payload.note || undefined,
      }),
  );
  const deployConfirmMut = useApiMutation((note: string) =>
    api.post(`/freezers/${id}/deployments/confirm`, { note: note || undefined }),
  );
  const pickupMut = useApiMutation((note: string) =>
    api.post(`/freezers/${id}/pickup/confirm`, { note: note || undefined }),
  );
  const acceptMut = useApiMutation((payload: { result: string; note: string }) =>
    api.post(`/freezers/${id}/acceptance`, {
      result: payload.result,
      note: payload.note || undefined,
    }),
  );
  const repairStartMut = useApiMutation((note: string) =>
    api.post(`/freezers/${id}/repair/start`, { note: note || undefined }),
  );
  const repairEndMut = useApiMutation((note: string) =>
    api.post(`/freezers/${id}/repair/complete`, { note: note || undefined }),
  );
  const scrapMut = useApiMutation((reason: string) =>
    api.post(`/freezers/${id}/scrap`, { reason }),
  );
  const refundMut = useApiMutation((payload: { amount_yuan: string; note: string }) =>
    api.post(`/freezers/${id}/deposit/refund`, {
      amount_fen: yuanToFenInput(payload.amount_yuan),
      note: payload.note || undefined,
    }),
  );
  // 撤回申请：业务员提交 → 片区经理审批 → 待撤回
  const recallRequestMut = useApiMutation((payload: { planned_pickup_date: string; reason: string }) =>
    api.post(`/freezers/${id}/recall/request`, payload),
  );
  const recallDecisionMut = useApiMutation(
    (payload: { decision: "APPROVE" | "REJECT"; note: string; planned_pickup_date?: string }) =>
      api.post(`/freezers/${id}/recall/decision`, {
        decision: payload.decision,
        note: payload.note || undefined,
        planned_pickup_date: payload.planned_pickup_date || undefined,
      }),
  );
  const recallRescheduleMut = useApiMutation(
    (payload: { planned_pickup_date: string; note: string }) =>
      api.post(`/freezers/${id}/recall/reschedule`, {
        planned_pickup_date: payload.planned_pickup_date,
        note: payload.note || undefined,
      }),
  );

  const [note, setNote] = useState("");
  const [reason, setReason] = useState("");
  const [contractId, setContractId] = useState("");
  const [depositYuan, setDepositYuan] = useState("");
  const [acceptResult, setAcceptResult] = useState("IN_STOCK");
  const [recallDate, setRecallDate] = useState("");
  const [recallReason, setRecallReason] = useState("");
  const [recallDecisionNote, setRecallDecisionNote] = useState("");
  const [rescheduleDate, setRescheduleDate] = useState("");
  const [refundYuan, setRefundYuan] = useState("");

  if (!authenticated) {
    return <SessionNotice hint="冰柜详情需要真实登录后的后端会话。" />;
  }

  const asset = detail.data?.asset ?? null;
  const movements = detail.data?.movements ?? [];
  const status = asset?.status ?? "";
  const effectiveContracts = (contracts.data ?? []).filter((c) => c.status === "EFFECTIVE");

  function after(ok: boolean, message: string, error: string) {
    if (ok) {
      push(message, "success");
      setNote("");
      setReason("");
      setDepositYuan("");
      setRefundYuan("");
      detail.refetch();
    } else {
      push(error || "操作失败", "danger");
    }
  }

  const movementColumns: DataTableColumn<FreezerMovementRow>[] = [
    {
      key: "occurred_at",
      header: "时间",
      render: (m) => <span className="text-xs text-slate-500">{formatDateTime(m.occurred_at)}</span>,
    },
    {
      key: "movement_type",
      header: "流水类型",
      render: (m) => <StatusBadge tone="neutral" label={label(m.movement_type)} />,
    },
    {
      key: "doc_no",
      header: "单据号",
      render: (m) => <span className="font-mono text-xs">{m.doc_no ?? "—"}</span>,
    },
    {
      key: "status_change",
      header: "状态变化",
      render: (m) => (
        <span className="text-xs text-slate-600">
          {m.from_status ? label(m.from_status) : "—"} → {m.to_status ? label(m.to_status) : "—"}
        </span>
      ),
    },
    { key: "amount", header: "金额", align: "right", render: (m) => money(m.amount_fen) },
    { key: "note", header: "备注", render: (m) => m.note ?? "—" },
  ];

  return (
    <section className="space-y-3 rounded-md border border-slate-200 bg-white p-4">
      <h2 className="text-sm font-semibold text-slate-700">资产档案与流水</h2>

      {detail.loading ? <LoadingBlock text="加载冰柜详情…" /> : null}
      {detail.error ? (
        <ErrorState message={detail.error} status={detail.status} onRetry={detail.refetch} />
      ) : null}

      {asset && !detail.loading && !detail.error ? (
        <>
          <div className="grid grid-cols-2 gap-3 text-sm md:grid-cols-4">
            <Info label="资产编码" value={<span className="font-mono text-xs">{asset.asset_code}</span>} />
            <Info
              label="状态"
              value={<StatusBadge tone={statusTone(asset.status)} label={label(asset.status)} />}
            />
            <Info label="型号" value={asset.model} />
            <Info label="在用" value={asset.is_active ? "是" : "停用"} />
            <Info label="购置日期" value={formatDate(asset.purchase_date)} />
            <Info label="购置价" value={money(asset.purchase_price_fen)} />
            <Info label="押金基准" value={money(asset.deposit_fen)} />
            <Info label="维修次数" value={String(asset.repair_count)} />
            <Info label="最近投放" value={formatDateTime(asset.last_deployed_at)} />
            <Info label="最近回收" value={formatDateTime(asset.last_recalled_at)} />
            <Info
              label="当前商家"
              value={
                <span className="font-mono text-xs">{asset.current_merchant_id ?? "—"}</span>
              }
            />
            <Info
              label="责任人"
              value={<span className="font-mono text-xs">{asset.responsible_user_id ?? "—"}</span>}
            />
          </div>

          <div>
            <div className="text-xs font-medium text-slate-600">
              资产流水（{movements.length} 条，只增不改）
            </div>
            <div className="mt-2">
              <DataTable
                rows={movements}
                columns={movementColumns}
                rowKey={(m) => m.id}
                emptyText="暂无流水"
              />
            </div>
          </div>

          {/* ---- 撤回申请：业务员发起 → 片区经理审批（计划回收日期可改）---- */}
          {asset.recall_status ? (
            <div className="space-y-3 rounded-md border border-slate-200 bg-slate-50 p-3">
              <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-slate-600">
                <StatusBadge
                  tone={
                    asset.recall_status === "PENDING"
                      ? "warning"
                      : asset.recall_status === "APPROVED"
                        ? "success"
                        : "danger"
                  }
                  label={RECALL_STATUS_LABEL[asset.recall_status] ?? asset.recall_status}
                />
                <span>
                  计划回收：{asset.recall_planned_date ? formatDate(asset.recall_planned_date) : "—"}
                </span>
                {asset.recall_reason ? <span>原因：{asset.recall_reason}</span> : null}
                {asset.recall_decision_note ? (
                  <span>审批意见：{asset.recall_decision_note}</span>
                ) : null}
              </div>
              <div className="text-[11px] text-slate-400">
                申请时间 {formatDateTime(asset.recall_requested_at)}
                {asset.recall_decided_at
                  ? ` ｜ 审批时间 ${formatDateTime(asset.recall_decided_at)}`
                  : ""}
              </div>

              {isRecallApprover && asset.recall_status === "PENDING" ? (
                asset.recall_requested_by === user?.id ? (
                  <p className="text-xs text-amber-700">
                    该申请由你本人提交，需由其他片区经理审批（撤回申请不可自审）。
                  </p>
                ) : (
                  <form
                    onSubmit={(e) => {
                      e.preventDefault();
                    }}
                    className="space-y-2"
                  >
                    <div className="grid grid-cols-1 gap-2 md:grid-cols-2">
                      <Field label="计划回收日期（审批时可调整）">
                        <input
                          type="date"
                          value={rescheduleDate}
                          onChange={(e) => setRescheduleDate(e.target.value)}
                          className={inputCls}
                        />
                      </Field>
                      <Field label="审批意见">
                        <input
                          value={recallDecisionNote}
                          onChange={(e) => setRecallDecisionNote(e.target.value)}
                          placeholder="同意/驳回原因"
                          className={inputCls}
                        />
                      </Field>
                    </div>
                    <div className="flex flex-wrap gap-2">
                      <button
                        type="button"
                        disabled={recallDecisionMut.submitting}
                        onClick={() =>
                          recallDecisionMut
                            .runSafe({
                              decision: "APPROVE",
                              note: recallDecisionNote,
                              planned_pickup_date: rescheduleDate,
                            })
                            .then((out) => after(out.ok, "已同意撤回申请", out.error))
                        }
                        className="rounded-md bg-[var(--color-brand)] px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50"
                      >
                        同意撤回
                      </button>
                      <button
                        type="button"
                        disabled={recallDecisionMut.submitting}
                        onClick={() =>
                          recallDecisionMut
                            .runSafe({ decision: "REJECT", note: recallDecisionNote })
                            .then((out) => after(out.ok, "已驳回撤回申请", out.error))
                        }
                        className="rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm text-slate-700 disabled:opacity-50"
                      >
                        驳回
                      </button>
                    </div>
                    {recallDecisionMut.error ? (
                      <div className="text-xs text-rose-600">{recallDecisionMut.error}</div>
                    ) : null}
                  </form>
                )
              ) : null}

              {(asset.recall_status === "PENDING" || asset.recall_status === "APPROVED") &&
              (asset.status === "DEPLOYED" || asset.status === "PENDING_PICKUP") &&
              (asset.recall_requested_by === user?.id || isRecallApprover) ? (
                <form
                  onSubmit={(e) => {
                    e.preventDefault();
                    recallRescheduleMut
                      .runSafe({ planned_pickup_date: rescheduleDate, note: recallDecisionNote })
                      .then((out) => after(out.ok, "计划回收日期已更新", out.error));
                  }}
                  className="flex flex-wrap items-end gap-2"
                >
                  <Field label="修改计划回收日期 *">
                    <input
                      required
                      type="date"
                      value={rescheduleDate}
                      onChange={(e) => setRescheduleDate(e.target.value)}
                      className={inputCls}
                    />
                  </Field>
                  <button
                    type="submit"
                    disabled={recallRescheduleMut.submitting}
                    className="rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm text-slate-700 disabled:opacity-50"
                  >
                    {recallRescheduleMut.submitting ? "保存中…" : "保存计划日期"}
                  </button>
                  {recallRescheduleMut.error ? (
                    <span className="text-xs text-rose-600">{recallRescheduleMut.error}</span>
                  ) : null}
                </form>
              ) : null}
            </div>
          ) : null}

          {isField &&
          status === "DEPLOYED" &&
          asset.recall_status !== "PENDING" &&
          asset.recall_status !== "APPROVED" ? (
            <form
              onSubmit={(e) => {
                e.preventDefault();
                recallRequestMut
                  .runSafe({
                    planned_pickup_date: recallDate,
                    reason: recallReason.trim(),
                  })
                  .then((out) => after(out.ok, "撤回申请已提交，等待片区经理审批", out.error));
              }}
              className="space-y-2 rounded-md border border-slate-200 bg-white p-3"
            >
              <div className="text-xs font-medium text-slate-600">
                申请撤回（提交后由片区经理审批，通过后转「待撤回」）
              </div>
              <div className="grid grid-cols-1 gap-2 md:grid-cols-2">
                <Field label="计划回收日期 *">
                  <input
                    required
                    type="date"
                    value={recallDate}
                    onChange={(e) => setRecallDate(e.target.value)}
                    className={inputCls}
                  />
                </Field>
                <Field label="撤回原因 *">
                  <input
                    required
                    value={recallReason}
                    onChange={(e) => setRecallReason(e.target.value)}
                    placeholder="如：商家闭店 / 合同即将到期"
                    className={inputCls}
                  />
                </Field>
              </div>
              {recallRequestMut.error ? (
                <div className="text-xs text-rose-600">{recallRequestMut.error}</div>
              ) : null}
              <button
                type="submit"
                disabled={recallRequestMut.submitting}
                className="rounded-md bg-[var(--color-brand)] px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50"
              >
                {recallRequestMut.submitting ? "提交中…" : "提交撤回申请"}
              </button>
            </form>
          ) : null}

          {/* ---- 投放单：在库 → 已预留（必须关联生效合同）---- */}
          {isField && status === "IN_STOCK" ? (
            <form
              onSubmit={(e) => {
                e.preventDefault();
                deployMut
                  .run({
                    contract_id: contractId || effectiveContracts[0]?.id || "",
                    deposit_yuan: depositYuan,
                    note,
                  })
                  .then((res) => after(res !== null, "投放单已创建（已预留）", deployMut.error));
              }}
              className="space-y-2 rounded-md border border-slate-200 bg-slate-50 p-3"
            >
              <div className="text-xs font-medium text-slate-600">
                创建投放单（在库 → 已预留，出库前；合同必须已生效）
              </div>
              {effectiveContracts.length === 0 && !contracts.loading ? (
                <div className="rounded-md border border-amber-200 bg-amber-50 p-2 text-xs text-amber-800">
                  没有可用生效合同。请先在合同中心完成审批，使合同状态为「已生效」。
                </div>
              ) : null}
              <div className="grid grid-cols-1 gap-2 md:grid-cols-3">
                <Field label="生效合同 *">
                  <select
                    required
                    value={contractId}
                    onChange={(e) => setContractId(e.target.value)}
                    className="rounded-md border border-slate-300 bg-white px-2 py-1.5 text-sm"
                  >
                    <option value="">请选择</option>
                    {effectiveContracts.map((c) => (
                      <option key={c.id} value={c.id}>
                        {c.contract_no} · {c.title}
                      </option>
                    ))}
                  </select>
                </Field>
                <Field label="本次押金（元）">
                  <input
                    type="number"
                    min="0"
                    step="0.01"
                    value={depositYuan}
                    onChange={(e) => setDepositYuan(e.target.value)}
                    placeholder="留空取台账基准"
                    className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
                  />
                </Field>
                <Field label="备注">
                  <input
                    value={note}
                    onChange={(e) => setNote(e.target.value)}
                    className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
                  />
                </Field>
              </div>
              <button
                type="submit"
                disabled={deployMut.submitting || effectiveContracts.length === 0}
                className="rounded-md bg-[var(--color-brand)] px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50"
              >
                {deployMut.submitting ? "提交中…" : "创建投放单"}
              </button>
            </form>
          ) : null}

          {/* ---- 现场投放：已预留 → 已投放（收缴押金）---- */}
          {isField && status === "RESERVED" ? (
            <SimpleAction
              title="现场投放确认（已预留 → 已投放，此刻收缴押金）"
              note={note}
              onNote={setNote}
              submitting={deployConfirmMut.submitting}
              submitText="确认投放"
              onSubmit={() =>
                deployConfirmMut
                  .run(note)
                  .then((res) => after(res !== null, "投放已确认", deployConfirmMut.error))
              }
            />
          ) : null}

          {/* ---- 现场回收：待撤回 → 待验收（不等于在库）---- */}
          {isField && status === "PENDING_PICKUP" ? (
            <SimpleAction
              title="现场回收确认（待撤回 → 待验收；回收完成不等于在库）"
              note={note}
              onNote={setNote}
              submitting={pickupMut.submitting}
              submitText="确认回收"
              onSubmit={() =>
                pickupMut
                  .run(note)
                  .then((res) => after(res !== null, "回收已确认", pickupMut.error))
              }
            />
          ) : null}

          {/* ---- 仓库验收：待验收 → 在库/维修/报废 ---- */}
          {isWarehouse && status === "PENDING_ACCEPTANCE" ? (
            <form
              onSubmit={(e) => {
                e.preventDefault();
                acceptMut
                  .run({ result: acceptResult, note })
                  .then((res) => after(res !== null, "验收已完成", acceptMut.error));
              }}
              className="space-y-2 rounded-md border border-slate-200 bg-slate-50 p-3"
            >
              <div className="text-xs font-medium text-slate-600">
                仓库验收（只有「在库」结论才增加可用库存）
              </div>
              <div className="grid grid-cols-1 gap-2 md:grid-cols-2">
                <Field label="验收结论 *">
                  <select
                    required
                    value={acceptResult}
                    onChange={(e) => setAcceptResult(e.target.value)}
                    className="rounded-md border border-slate-300 bg-white px-2 py-1.5 text-sm"
                  >
                    <option value="IN_STOCK">在库（库存 +1）</option>
                    <option value="REPAIR">转维修</option>
                    <option value="SCRAP">直接报废</option>
                  </select>
                </Field>
                <Field label="备注">
                  <input
                    value={note}
                    onChange={(e) => setNote(e.target.value)}
                    className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
                  />
                </Field>
              </div>
              <button
                type="submit"
                disabled={acceptMut.submitting}
                className="rounded-md bg-[var(--color-brand)] px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50"
              >
                {acceptMut.submitting ? "提交中…" : "提交验收"}
              </button>
            </form>
          ) : null}

          {/* ---- 维修 ---- */}
          {isWarehouse && status === "IN_STOCK" ? (
            <SimpleAction
              title="故障送修（在库 → 维修中）"
              note={note}
              onNote={setNote}
              submitting={repairStartMut.submitting}
              submitText="登记送修"
              onSubmit={() =>
                repairStartMut
                  .run(note)
                  .then((res) => after(res !== null, "已转维修", repairStartMut.error))
              }
            />
          ) : null}

          {isWarehouse && status === "REPAIRING" ? (
            <div className="space-y-2 rounded-md border border-slate-200 bg-slate-50 p-3">
              <SimpleAction
                title="维修完成归库（维修中 → 在库）"
                note={note}
                onNote={setNote}
                submitting={repairEndMut.submitting}
                submitText="维修完成"
                onSubmit={() =>
                  repairEndMut
                    .run(note)
                    .then((res) => after(res !== null, "维修已完成归库", repairEndMut.error))
                }
              />
              <form
                onSubmit={(e) => {
                  e.preventDefault();
                  scrapMut
                    .run(reason.trim())
                    .then((res) => after(res !== null, "冰柜已报废（不可逆）", scrapMut.error));
                }}
                className="space-y-2 border-t border-slate-200 pt-2"
              >
                <div className="text-xs font-medium text-rose-700">报废（不可逆，R13）</div>
                <Field label="报废原因 *">
                  <input
                    required
                    value={reason}
                    onChange={(e) => setReason(e.target.value)}
                    className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
                  />
                </Field>
                <button
                  type="submit"
                  disabled={scrapMut.submitting}
                  className="rounded-md border border-rose-300 bg-white px-3 py-1.5 text-sm text-rose-700 hover:bg-rose-50 disabled:opacity-50"
                >
                  {scrapMut.submitting ? "提交中…" : "确认报废"}
                </button>
              </form>
            </div>
          ) : null}

          {/* ---- 押金退还（财务确认；往来款，不入损益）---- */}
          {isFinance && asset.deposit_fen !== null ? (
            <form
              onSubmit={(e) => {
                e.preventDefault();
                refundMut
                  .run({ amount_yuan: refundYuan, note })
                  .then((res) => after(res !== null, "押金退还已入账", refundMut.error));
              }}
              className="space-y-2 rounded-md border border-slate-200 bg-slate-50 p-3"
            >
              <div className="text-xs font-medium text-slate-600">
                押金退还确认（精确到分，留空按收缴金额全额退；不写损益）
              </div>
              <div className="grid grid-cols-1 gap-2 md:grid-cols-2">
                <Field label="实际退款（元）">
                  <input
                    type="number"
                    min="0"
                    step="0.01"
                    value={refundYuan}
                    onChange={(e) => setRefundYuan(e.target.value)}
                    placeholder="留空全额退"
                    className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
                  />
                </Field>
                <Field label="备注">
                  <input
                    value={note}
                    onChange={(e) => setNote(e.target.value)}
                    className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
                  />
                </Field>
              </div>
              <button
                type="submit"
                disabled={refundMut.submitting}
                className="rounded-md bg-[var(--color-brand)] px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50"
              >
                {refundMut.submitting ? "提交中…" : "确认退押金"}
              </button>
            </form>
          ) : null}

          {status === "SCRAPPED" ? (
            <p className="rounded-md border border-rose-200 bg-rose-50 px-3 py-2 text-xs text-rose-700">
              该资产已报废，状态不可逆，仅保留流水追溯。
            </p>
          ) : null}
        </>
      ) : null}

      {!asset && !detail.loading && !detail.error ? (
        <EmptyState title="未找到冰柜" description="可能已超出你的行级可见范围。" />
      ) : null}

      <ToastStack items={toasts} onDismiss={dismiss} />
    </section>
  );
}

function SimpleAction({
  title,
  note,
  onNote,
  submitting,
  submitText,
  onSubmit,
}: {
  title: string;
  note: string;
  onNote: (v: string) => void;
  submitting: boolean;
  submitText: string;
  onSubmit: () => void;
}) {
  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        onSubmit();
      }}
      className="space-y-2 rounded-md border border-slate-200 bg-slate-50 p-3"
    >
      <div className="text-xs font-medium text-slate-600">{title}</div>
      <Field label="备注">
        <input
          value={note}
          onChange={(e) => onNote(e.target.value)}
          className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
        />
      </Field>
      <button
        type="submit"
        disabled={submitting}
        className="rounded-md bg-[var(--color-brand)] px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50"
      >
        {submitting ? "提交中…" : submitText}
      </button>
    </form>
  );
}

function PurchaseFreezerForm({
  enabled,
  onCreated,
}: {
  enabled: boolean;
  onCreated: (assetCode: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [assetCode, setAssetCode] = useState("");
  const [brandId, setBrandId] = useState("");
  const [model, setModel] = useState("");
  const [purchaseDate, setPurchaseDate] = useState("");
  const [priceYuan, setPriceYuan] = useState("");
  const [depositYuan, setDepositYuan] = useState("");

  const brands = useApiQuery<{ items: OptionItem[] }>("/master-data/brands", undefined, {
    enabled: open,
  });
  const createMut = useApiMutation((payload: Record<string, unknown>) =>
    api.post<{ asset_code: string }>("/freezers", payload),
  );

  const brandOptions = brands.data?.items ?? [];

  return (
    <div className="space-y-2 rounded-md border border-slate-200 bg-white p-3">
      <button
        type="button"
        disabled={!enabled}
        onClick={() => setOpen((v) => !v)}
        className="rounded-md bg-[var(--color-brand)] px-3 py-1.5 text-sm font-medium text-white hover:opacity-90 disabled:opacity-50"
      >
        {open ? "收起新建" : "购置入库"}
      </button>
      {!enabled ? (
        <p className="text-xs text-slate-400">
          当前角色无登记权限（督导 / 片区经理 / 系统管理员 / 财务）。
        </p>
      ) : null}

      {open && enabled ? (
        <form
          onSubmit={(e) => {
            e.preventDefault();
            createMut
              .run({
                asset_code: assetCode.trim(),
                brand_id: brandId || brandOptions[0]?.id || "",
                model: model.trim(),
                purchase_date: purchaseDate || undefined,
                purchase_price_fen: yuanToFenInput(priceYuan) ?? 0,
                deposit_fen: yuanToFenInput(depositYuan),
              })
              .then((res) => {
                if (res) {
                  onCreated(res.asset_code);
                  setAssetCode("");
                  setModel("");
                  setPriceYuan("");
                  setDepositYuan("");
                  setOpen(false);
                }
              });
          }}
          className="space-y-3"
        >
          {brandOptions.length === 0 && !brands.loading ? (
            <div className="rounded-md border border-amber-200 bg-amber-50 p-2 text-xs text-amber-800">
              尚未取到品牌选项（`GET /api/master-data/brands`）。请先导入主数据。
            </div>
          ) : null}
          <div className="grid grid-cols-1 gap-3 md:grid-cols-3">
            <Field label="资产编码 *">
              <input
                required
                value={assetCode}
                onChange={(e) => setAssetCode(e.target.value)}
                className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
              />
            </Field>
            <Field label="品牌 *">
              <select
                required
                value={brandId}
                onChange={(e) => setBrandId(e.target.value)}
                className="rounded-md border border-slate-300 bg-white px-2 py-1.5 text-sm"
              >
                <option value="">请选择</option>
                {brandOptions.map((b) => (
                  <option key={b.id} value={b.id}>
                    {b.name}
                  </option>
                ))}
              </select>
            </Field>
            <Field label="型号 *">
              <input
                required
                value={model}
                onChange={(e) => setModel(e.target.value)}
                className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
              />
            </Field>
            <Field label="购置日期 *">
              <input
                required
                type="date"
                value={purchaseDate}
                onChange={(e) => setPurchaseDate(e.target.value)}
                className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
              />
            </Field>
            <Field label="购置价（元）*">
              <input
                required
                type="number"
                min="0"
                step="0.01"
                value={priceYuan}
                onChange={(e) => setPriceYuan(e.target.value)}
                className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
              />
            </Field>
            <Field label="押金基准（元）">
              <input
                type="number"
                min="0"
                step="0.01"
                value={depositYuan}
                onChange={(e) => setDepositYuan(e.target.value)}
                className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
              />
            </Field>
          </div>
          {createMut.error ? <div className="text-xs text-rose-600">{createMut.error}</div> : null}
          <div className="flex gap-2">
            <button
              type="submit"
              disabled={createMut.submitting || brandOptions.length === 0}
              className="rounded-md bg-[var(--color-brand)] px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50"
            >
              {createMut.submitting ? "提交中…" : "登记入库"}
            </button>
            <button
              type="button"
              onClick={() => setOpen(false)}
              className="rounded-md border border-slate-300 px-3 py-1.5 text-sm text-slate-600"
            >
              取消
            </button>
          </div>
        </form>
      ) : null}
    </div>
  );
}
