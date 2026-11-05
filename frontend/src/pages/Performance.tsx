/** 绩效核算（T19 前端 / T35 口径精简）。
 *
 * 接入后端：
 * - `GET /api/performance/snapshots?period_month=&status=&user_id=`（行级范围由服务端按角色收窄）；
 * - `GET /api/performance/snapshots/{id}`（快照头 + 逐项依据行，Q108 可反查）；
 * - `POST /api/performance/snapshots/freeze`、`/{id}/confirm|review|return`；
 * - `GET|POST /api/performance/rule-versions`、`POST /rule-versions/{id}/review`；
 * - 下拉：`GET /api/performance/options/users`。
 *
 * 业务红线（全部由后端 Service 裁决，页面只做展示与受控交互）：
 * - T35：绩效只按「新签合同奖励 + 活动达成奖励」计算，巡访/销量/扣减/调整恒 0；
 * - 巡访任务完成时后端自动重算当月未复核快照，页面展示最新一次重算结果；
 * - 历史不回算：已确认/已复核快照不再自动改写；
 * - 确认（经理）→ 复核（财务）为不同角色，自审由后端拒绝；
 * - 金额一律整数分展示，前端不做任何金额计算。
 */
import { useMemo, useState } from "react";

import { api } from "../lib/api";
import { useAuth } from "../lib/auth";
import {
  boxes,
  currentPeriodMonth,
  fenToYuan,
  formatDate,
  formatDateTime,
  label,
  money,
  statusTone,
} from "../lib/format";
import { useAction, useApiMutation, useApiQuery } from "../lib/useApi";
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
import type { ToastTone } from "../components/Toast";

const PAGE_SIZE = 20;

/** 受控枚举（与后端 PerformanceSnapshotStatus 对齐）。 */
const SNAPSHOT_STATUS_OPTIONS = ["DRAFT", "CONFIRMED", "REVIEWED"] as const;

interface OptionItem {
  id: string;
  code: string;
  name: string;
}

interface SnapshotRow {
  id: string;
  user_id: string;
  user_username: string;
  user_name: string;
  period_month: string;
  period_start: string;
  period_end: string;
  rule_version_id: string;
  rule_code: string;
  rule_version: number;
  new_contract_fen: number;
  visit_bonus_fen: number;
  sales_commission_fen: number;
  activity_bonus_fen: number;
  deduction_fen: number;
  adjustment_fen: number;
  net_performance_fen: number;
  status: string;
  frozen_at: string | null;
  confirmed_at: string | null;
  reviewed_at: string | null;
}

interface SnapshotLine {
  id: string;
  line_type: string;
  source_object_type: string;
  source_object_id: string;
  merchant_id: string | null;
  owner_user_id: string;
  fact_date: string;
  quantity: string | null;
  amount_fen: number;
  basis_json: Record<string, unknown>;
}

interface SnapshotDetail extends SnapshotRow {
  lines: SnapshotLine[];
}

interface RuleParamsJson {
  new_contract_award_fen: number;
  valid_visit_award_fen: number;
  activity_award_fen: number;
  sales_tiers: [number | null, number][];
}

interface RuleVersionRow {
  id: string;
  code: string;
  version: number;
  name: string;
  status: string;
  effective_from: string;
  effective_to: string | null;
  params_json: RuleParamsJson;
  configured_by_id: string;
  reviewed_by_id: string | null;
  review_note: string | null;
}

export function Performance() {
  const { authenticated, user } = useAuth();
  const { items: toasts, push, dismiss } = useToast();
  const [tab, setTab] = useState<"snapshots" | "rules">("snapshots");

  const role = user?.role;
  const canFreeze =
    role === "REGIONAL_MANAGER" || role === "FINANCE" || role === "SYSTEM_ADMIN";
  const canConfirm = role === "REGIONAL_MANAGER" || role === "SYSTEM_ADMIN";
  const canReview = role === "FINANCE";

  return (
    <div className="space-y-4">
      <PageHeader
        title="绩效核算"
        description="只按新签合同奖励 + 活动达成奖励计算；巡访完成后自动重算当月未复核快照，经理确认 → 财务复核后不再改写"
      />

      {!authenticated ? (
        <SessionNotice hint="绩效快照需要真实登录后的后端会话（后端按角色二次鉴权）。" />
      ) : (
        <>
          <div className="flex gap-1 rounded-md border border-slate-200 bg-white p-1">
            {(
              [
                ["snapshots", "月度快照"],
                ["rules", "规则版本"],
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

          {tab === "snapshots" ? (
            <SnapshotsTab
              canFreeze={canFreeze}
              canConfirm={canConfirm}
              canReview={canReview}
              onToast={push}
            />
          ) : (
            <RulesTab canConfigure={canFreeze} onToast={push} />
          )}
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

function Info({ label: text, value }: { label: string; value: React.ReactNode }) {
  return (
    <div>
      <div className="text-xs text-slate-500">{text}</div>
      <div className="mt-0.5 text-slate-800">{value}</div>
    </div>
  );
}

function shortId(id: string | null): string {
  return id ? id.slice(0, 8) : "—";
}

/** 依据摘要：把 basis_json 的关键参数翻成人话，便于逐项反查（Q108）。 */
function basisSummary(basis: Record<string, unknown>): string {
  const parts: string[] = [];
  const fenOf = (v: unknown) => (typeof v === "number" ? fenToYuan(v) : null);
  const perContract = fenOf(basis.award_per_contract_fen);
  if (perContract) parts.push(`每单 ¥${perContract}`);
  const perActivity = fenOf(basis.award_per_activity_fen);
  if (perActivity) parts.push(`每场 ¥${perActivity}`);
  return parts.length > 0 ? parts.join("；") : "—";
}

// ============================ 月度快照 ============================

function SnapshotsTab({
  canFreeze,
  canConfirm,
  canReview,
  onToast,
}: {
  canFreeze: boolean;
  canConfirm: boolean;
  canReview: boolean;
  onToast: (message: string, tone?: ToastTone) => void;
}) {
  const [monthInput, setMonthInput] = useState("");
  const [statusInput, setStatusInput] = useState("");
  const [userInput, setUserInput] = useState("");
  const [applied, setApplied] = useState({ period_month: "", status: "", user_id: "" });
  const [page, setPage] = useState(1);
  const [selectedId, setSelectedId] = useState<string | null>(null);

  const list = useApiQuery<SnapshotRow[]>("/performance/snapshots", applied);

  const rows = useMemo(() => list.data ?? [], [list.data]);
  const totalPages = Math.max(1, Math.ceil(rows.length / PAGE_SIZE));
  const safePage = Math.min(page, totalPages);
  const pageRows = rows.slice((safePage - 1) * PAGE_SIZE, safePage * PAGE_SIZE);

  const columns: DataTableColumn<SnapshotRow>[] = [
    {
      key: "period_month",
      header: "业务月",
      render: (r) => <span className="font-mono text-xs">{r.period_month}</span>,
    },
    {
      key: "user_name",
      header: "业务员",
      render: (r) => (
        <div>
          <button
            type="button"
            onClick={() => setSelectedId(r.id)}
            className="font-medium text-[var(--color-brand)] hover:underline"
          >
            {r.user_name}
          </button>
          <div className="text-xs text-slate-400">{r.user_username}</div>
        </div>
      ),
    },
    {
      key: "status",
      header: "状态",
      render: (r) => <StatusBadge tone={statusTone(r.status)} label={label(r.status)} />,
    },
    {
      // 构成只保留两项奖励（T35）；净额单独成列，一眼可读
      key: "composition",
      header: "构成（新签 / 活动）",
      render: (r) => (
        <div className="text-xs">
          <div className="text-slate-600">
            {money(r.new_contract_fen)} / {money(r.activity_bonus_fen)}
          </div>
          <div className="mt-0.5 text-slate-400">
            {r.rule_code} v{r.rule_version} · 冻结 {formatDateTime(r.frozen_at)}
          </div>
        </div>
      ),
    },
    {
      key: "net_performance_fen",
      header: "绩效净额",
      align: "right",
      render: (r) => <span className="font-semibold">{money(r.net_performance_fen)}</span>,
    },
    {
      key: "actions",
      header: "操作",
      render: (r) => (
        <button
          type="button"
          onClick={() => setSelectedId(r.id)}
          className="rounded-md border border-slate-300 bg-white px-2 py-1 text-xs text-slate-700 hover:bg-slate-50"
        >
          查看明细
        </button>
      ),
    },
  ];

  return (
    <div className="space-y-3">
      <SnapshotFilter
        monthInput={monthInput}
        statusInput={statusInput}
        userInput={userInput}
        onMonthChange={setMonthInput}
        onStatusChange={setStatusInput}
        onUserChange={setUserInput}
        onApply={() => {
          setApplied({ period_month: monthInput, status: statusInput, user_id: userInput });
          setPage(1);
        }}
        onReset={() => {
          setMonthInput("");
          setStatusInput("");
          setUserInput("");
          setApplied({ period_month: "", status: "", user_id: "" });
          setPage(1);
        }}
      />

      <FreezeForm
        enabled={canFreeze}
        onFrozen={(id, message) => {
          onToast(message, "success");
          list.refetch();
          setSelectedId(id);
        }}
        onToast={onToast}
      />

      {list.loading ? <LoadingBlock text="加载月度快照…" /> : null}
      {list.error ? (
        <ErrorState message={list.error} status={list.status} onRetry={list.refetch} />
      ) : null}

      {!list.loading && !list.error ? (
        <>
          <div className="text-xs text-slate-500">共 {rows.length} 条快照</div>
          <DataTable
            rows={pageRows}
            columns={columns}
            rowKey={(r) => r.id}
            emptyText="暂无绩效快照（巡访完成后自动生成，或手动选择业务员与业务月冻结）"
          />
          <Pagination page={safePage} totalPages={totalPages} onPageChange={setPage} />
        </>
      ) : null}

      {selectedId ? (
        <SnapshotDetailPanel
          id={selectedId}
          canConfirm={canConfirm}
          canReview={canReview}
          onClose={() => setSelectedId(null)}
          onChanged={() => list.refetch()}
          onToast={onToast}
        />
      ) : null}

      <p className="text-xs text-slate-400">
        绩效只按「新签合同奖励 + 活动达成奖励」计算（生效合同 / 已复盘活动），并按调拨生效日归属
        当月负责人；巡访完成时后台自动重算当月未复核快照，已确认/已复核快照不再改写。
      </p>
    </div>
  );
}

function useUserOptions(enabled: boolean, roleCode?: string) {
  const query = useApiQuery<{ items: OptionItem[] }>(
    "/performance/options/users",
    { role_code: roleCode },
    { enabled },
  );
  const items = useMemo(() => query.data?.items ?? [], [query.data]);
  return { items, loading: query.loading, error: query.error };
}

function SnapshotFilter({
  monthInput,
  statusInput,
  userInput,
  onMonthChange,
  onStatusChange,
  onUserChange,
  onApply,
  onReset,
}: {
  monthInput: string;
  statusInput: string;
  userInput: string;
  onMonthChange: (v: string) => void;
  onStatusChange: (v: string) => void;
  onUserChange: (v: string) => void;
  onApply: () => void;
  onReset: () => void;
}) {
  const users = useUserOptions(true, "SALES_REP");
  return (
    <FilterBar onApply={onApply} onReset={onReset}>
      <Field label="业务月">
        <input
          type="month"
          value={monthInput}
          onChange={(e) => onMonthChange(e.target.value)}
          className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
        />
      </Field>
      <Field label="状态">
        <select
          value={statusInput}
          onChange={(e) => onStatusChange(e.target.value)}
          className="rounded-md border border-slate-300 bg-white px-2 py-1.5 text-sm"
        >
          <option value="">全部</option>
          {SNAPSHOT_STATUS_OPTIONS.map((s) => (
            <option key={s} value={s}>
              {label(s)}
            </option>
          ))}
        </select>
      </Field>
      <Field label="业务员">
        <select
          value={userInput}
          onChange={(e) => onUserChange(e.target.value)}
          className="w-44 rounded-md border border-slate-300 bg-white px-2 py-1.5 text-sm"
        >
          <option value="">全部</option>
          {users.items.map((u) => (
            <option key={u.id} value={u.id}>
              {u.name}
            </option>
          ))}
        </select>
      </Field>
    </FilterBar>
  );
}

function FreezeForm({
  enabled,
  onFrozen,
  onToast,
}: {
  enabled: boolean;
  onFrozen: (id: string, message: string) => void;
  onToast: (message: string, tone?: ToastTone) => void;
}) {
  const [open, setOpen] = useState(false);
  const [userId, setUserId] = useState("");
  const [month, setMonth] = useState(currentPeriodMonth());
  const [ruleVersionId, setRuleVersionId] = useState("");

  const users = useUserOptions(open, "SALES_REP");
  const rules = useApiQuery<RuleVersionRow[]>("/performance/rule-versions", undefined, {
    enabled: open,
  });

  const activeRules = useMemo(
    () => (rules.data ?? []).filter((r) => r.status === "ACTIVE"),
    [rules.data],
  );

  const freezeMut = useApiMutation(
    (p: { user_id: string; period_month: string; rule_version_id?: string }) =>
      api.post<SnapshotDetail>("/performance/snapshots/freeze", p),
  );

  const effectiveUserId = userId || users.items[0]?.id || "";

  return (
    <section className="space-y-3 rounded-md border border-slate-200 bg-white p-3">
      <button
        type="button"
        disabled={!enabled}
        onClick={() => setOpen((v) => !v)}
        className="rounded-md bg-[var(--color-brand)] px-3 py-1.5 text-sm font-medium text-white hover:opacity-90 disabled:opacity-50"
      >
        {open ? "收起冻结" : "冻结月度快照"}
      </button>
      {!enabled ? (
        <p className="text-xs text-slate-400">
          当前角色无绩效冻结权限（片区经理 / 财务 / 系统管理员）。
        </p>
      ) : null}

      {open && enabled ? (
        <form
          onSubmit={(e) => {
            e.preventDefault();
            freezeMut
              .run({
                user_id: effectiveUserId,
                period_month: month,
                rule_version_id: ruleVersionId || undefined,
              })
              .then((res) => {
                if (res) {
                  onFrozen(res.id, `${res.period_month} ${res.user_name} 绩效快照已冻结`);
                  setOpen(false);
                } else {
                  onToast(freezeMut.error || "冻结失败", "danger");
                }
              });
          }}
          className="space-y-3"
        >
          {users.items.length === 0 && !users.loading ? (
            <div className="rounded-md border border-amber-200 bg-amber-50 p-2 text-xs text-amber-800">
              未取到在职业务员（`GET /api/performance/options/users`）。请先在用户管理中建立并激活业务员账号。
            </div>
          ) : null}
          <div className="grid grid-cols-1 gap-3 md:grid-cols-3">
            <Field label="业务员 *">
              <select
                required
                value={effectiveUserId}
                onChange={(e) => setUserId(e.target.value)}
                className="rounded-md border border-slate-300 bg-white px-2 py-1.5 text-sm"
              >
                <option value="">请选择</option>
                {users.items.map((u) => (
                  <option key={u.id} value={u.id}>
                    {u.name}（{u.code}）
                  </option>
                ))}
              </select>
            </Field>
            <Field label="业务月 *">
              <input
                required
                type="month"
                value={month}
                onChange={(e) => setMonth(e.target.value)}
                className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
              />
            </Field>
            <Field label="规则版本（默认按月末生效版本）">
              <select
                value={ruleVersionId}
                onChange={(e) => setRuleVersionId(e.target.value)}
                className="rounded-md border border-slate-300 bg-white px-2 py-1.5 text-sm"
              >
                <option value="">自动匹配</option>
                {activeRules.map((r) => (
                  <option key={r.id} value={r.id}>
                    {r.code} v{r.version} · 自 {formatDate(r.effective_from)}
                  </option>
                ))}
              </select>
            </Field>
          </div>
          <p className="text-xs text-slate-400">
            同一业务员同一业务月重复冻结直接返回原快照（幂等），不重算历史。
          </p>
          {freezeMut.error ? (
            <div className="text-xs text-rose-600">{freezeMut.error}</div>
          ) : null}
          <div className="flex gap-2">
            <button
              type="submit"
              disabled={freezeMut.submitting || !effectiveUserId || !month}
              className="rounded-md bg-[var(--color-brand)] px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50"
            >
              {freezeMut.submitting ? "冻结中…" : "冻结快照"}
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
    </section>
  );
}

function SnapshotDetailPanel({
  id,
  canConfirm,
  canReview,
  onClose,
  onChanged,
  onToast,
}: {
  id: string;
  canConfirm: boolean;
  canReview: boolean;
  onClose: () => void;
  onChanged: () => void;
  onToast: (message: string, tone?: ToastTone) => void;
}) {
  const detail = useApiQuery<SnapshotDetail>(`/performance/snapshots/${id}`, undefined, {
    enabled: Boolean(id),
  });

  const confirmMut = useAction(() =>
    api.post<SnapshotRow>(`/performance/snapshots/${id}/confirm`),
  );
  const reviewMut = useAction(() =>
    api.post<SnapshotRow>(`/performance/snapshots/${id}/review`),
  );
  const returnMut = useApiMutation((reason: string) =>
    api.post<SnapshotRow>(`/performance/snapshots/${id}/return`, { reason }),
  );

  const [returnReason, setReturnReason] = useState("");
  const [showReturn, setShowReturn] = useState(false);
  const [confirmingAction, setConfirmingAction] = useState<"confirm" | "review" | null>(null);

  const snap = detail.data ?? null;
  const status = snap?.status ?? "";

  function afterAction(ok: boolean, message: string, error: string) {
    if (ok) {
      onToast(message, "success");
      setReturnReason("");
      setShowReturn(false);
      detail.refetch();
      onChanged();
    } else {
      onToast(error || "操作失败", "danger");
    }
  }

  const lineColumns: DataTableColumn<SnapshotLine>[] = [
    {
      key: "line_type",
      header: "类型",
      render: (r) => <StatusBadge tone="neutral" label={label(r.line_type)} />,
    },
    {
      key: "source_object_type",
      header: "来源单据",
      render: (r) => (
        <span className="text-xs">
          {r.source_object_type}
          <span className="ml-1 font-mono text-slate-400">{shortId(r.source_object_id)}</span>
        </span>
      ),
    },
    {
      key: "fact_date",
      header: "事实日",
      render: (r) => <span className="text-xs text-slate-600">{formatDate(r.fact_date)}</span>,
    },
    {
      key: "quantity",
      header: "数量（箱）",
      align: "right",
      render: (r) => boxes(r.quantity),
    },
    { key: "amount_fen", header: "金额", align: "right", render: (r) => money(r.amount_fen) },
    {
      key: "basis_json",
      header: "计算依据",
      render: (r) => <span className="text-xs text-slate-500">{basisSummary(r.basis_json)}</span>,
    },
  ];

  return (
    <section className="space-y-3 rounded-md border border-slate-200 bg-white p-4">
      <div className="flex items-center justify-between">
        <h2 className="text-sm font-semibold text-slate-700">快照明细</h2>
        <button
          type="button"
          onClick={onClose}
          className="rounded-md border border-slate-300 px-2 py-1 text-xs text-slate-600 hover:bg-slate-50"
        >
          收起明细
        </button>
      </div>

      {detail.loading ? <LoadingBlock text="加载快照明细…" /> : null}
      {detail.error ? (
        <ErrorState message={detail.error} status={detail.status} onRetry={detail.refetch} />
      ) : null}

      {snap && !detail.loading && !detail.error ? (
        <>
          <div className="grid grid-cols-2 gap-3 text-sm md:grid-cols-4">
            <Info label="业务员" value={`${snap.user_name}（${snap.user_username}）`} />
            <Info label="业务月" value={<span className="font-mono">{snap.period_month}</span>} />
            <Info
              label="状态"
              value={<StatusBadge tone={statusTone(snap.status)} label={label(snap.status)} />}
            />
            <Info
              label="规则版本"
              value={
                <span className="font-mono text-xs">
                  {snap.rule_code} v{snap.rule_version}
                </span>
              }
            />
            <Info label="统计周期" value={`${formatDate(snap.period_start)} ~ ${formatDate(snap.period_end)}`} />
            <Info label="最近重算" value={formatDateTime(snap.frozen_at)} />
            <Info label="经理确认" value={formatDateTime(snap.confirmed_at)} />
            <Info label="财务复核" value={formatDateTime(snap.reviewed_at)} />
          </div>

          <div className="grid grid-cols-2 gap-3 md:grid-cols-3">
            <MetricCard label="新签合同奖励" value={money(snap.new_contract_fen)} />
            <MetricCard label="活动达成奖励" value={money(snap.activity_bonus_fen)} />
            <MetricCard label="绩效净额" value={money(snap.net_performance_fen)} />
          </div>

          <div>
            <div className="text-xs font-medium text-slate-600">
              逐项依据行（可反查来源单据，Q108）
            </div>
            <div className="mt-2">
              <DataTable
                rows={snap.lines}
                columns={lineColumns}
                rowKey={(r) => r.id}
                emptyText="暂无依据行（当月无生效合同或已复盘活动）"
              />
            </div>
            <p className="mt-1 text-xs text-slate-400">
              依据行仅来自生效合同（新签奖励）与已复盘活动（活动奖励）；巡访与销量只做过程与损益口径，
              不再产生绩效金额。
            </p>
          </div>

          {/* ---- 动作区：按状态与角色裁剪，最终仍由后端二次鉴权 ---- */}
          {canConfirm && status === "DRAFT" ? (
            <div className="space-y-2 rounded-md border border-slate-200 bg-slate-50 p-3">
              <div className="text-xs font-medium text-slate-600">
                片区经理确认（草稿 → 已确认）
              </div>
              <button
                type="button"
                disabled={confirmMut.submitting}
                onClick={() => setConfirmingAction("confirm")}
                className="rounded-md bg-[var(--color-brand)] px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50"
              >
                {confirmMut.submitting ? "确认中…" : "确认快照"}
              </button>
              <p className="text-xs text-slate-400">
                确认后原依据行不可修改，也不再提供扣减/调整更正单（T35 口径）。
              </p>
            </div>
          ) : null}

          {canReview && status === "CONFIRMED" ? (
            <div className="space-y-2 rounded-md border border-slate-200 bg-slate-50 p-3">
              <div className="text-xs font-medium text-slate-600">
                财务复核（已确认 → 已复核 / 退回）
              </div>
              <div className="flex flex-wrap gap-2">
                <button
                  type="button"
                  disabled={reviewMut.submitting}
                  onClick={() => setConfirmingAction("review")}
                  className="rounded-md bg-[var(--color-brand)] px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50"
                >
                  {reviewMut.submitting ? "复核中…" : "复核通过"}
                </button>
                <button
                  type="button"
                  onClick={() => setShowReturn((v) => !v)}
                  className="rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm text-slate-700 hover:bg-slate-100"
                >
                  {showReturn ? "取消退回" : "退回修改"}
                </button>
              </div>
              {showReturn ? (
                <div className="space-y-2">
                  <Field label="退回原因 *">
                    <input
                      value={returnReason}
                      onChange={(e) => setReturnReason(e.target.value)}
                      className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
                    />
                  </Field>
                  <button
                    type="button"
                    disabled={returnMut.submitting || !returnReason.trim()}
                    onClick={() => {
                      returnMut
                        .run(returnReason.trim())
                        .then((res) =>
                          afterAction(res !== null, "已退回片区经理", returnMut.error),
                        );
                    }}
                    className="rounded-md border border-rose-300 bg-white px-3 py-1.5 text-sm text-rose-700 hover:bg-rose-50 disabled:opacity-50"
                  >
                    {returnMut.submitting ? "退回中…" : "确认退回"}
                  </button>
                </div>
              ) : null}
              <p className="text-xs text-slate-400">
                退回后状态回到「已确认」，需片区经理重新确认；复核人为财务角色（A28）。
              </p>
            </div>
          ) : null}
        </>
      ) : null}

      {!snap && !detail.loading && !detail.error ? (
        <EmptyState
          title="未找到该绩效快照"
          description="可能已超出你的行级可见范围（业务员仅能查看本人快照）。"
        />
      ) : null}

      <ConfirmDialog
        open={confirmingAction !== null}
        title={confirmingAction === "review" ? "确认财务复核通过？" : "确认经理确认？"}
        description={
          confirmingAction === "review"
            ? "复核通过后状态变为「已复核」，同样不可修改原依据行。"
            : "确认后状态变为「已确认」，原依据行不可修改。"
        }
        confirmText={confirmingAction === "review" ? "复核通过" : "确认"}
        onClose={() => setConfirmingAction(null)}
        onConfirm={() => {
          const action = confirmingAction;
          setConfirmingAction(null);
          if (action === "confirm") {
            confirmMut
              .run()
              .then((res) => afterAction(res !== null, "已确认月度绩效快照", confirmMut.error));
          } else if (action === "review") {
            reviewMut
              .run()
              .then((res) => afterAction(res !== null, "已复核月度绩效快照", reviewMut.error));
          }
        }}
      />
    </section>
  );
}

// ============================ 规则版本 ============================

function RulesTab({
  canConfigure,
  onToast,
}: {
  canConfigure: boolean;
  onToast: (message: string, tone?: ToastTone) => void;
}) {
  const list = useApiQuery<RuleVersionRow[]>("/performance/rule-versions");
  const rules = useMemo(() => list.data ?? [], [list.data]);

  const reviewMut = useApiMutation((id: string) =>
    api.post<RuleVersionRow>(`/performance/rule-versions/${id}/review`, { note: "审核生效" }),
  );

  function afterReview(ok: boolean) {
    if (ok) {
      onToast("规则版本已审核生效，旧版本置为已取代（历史快照不回算）", "success");
      list.refetch();
    } else {
      onToast(reviewMut.error || "审核失败", "danger");
    }
  }

  const columns: DataTableColumn<RuleVersionRow>[] = [
    { key: "code", header: "规则编码", render: (r) => <span className="font-mono text-xs">{r.code}</span> },
    { key: "version", header: "版本", render: (r) => `v${r.version}` },
    { key: "name", header: "名称", render: (r) => r.name },
    {
      key: "status",
      header: "状态",
      render: (r) => <StatusBadge tone={statusTone(r.status)} label={label(r.status)} />,
    },
    {
      key: "effective",
      header: "生效区间",
      render: (r) => (
        <span className="text-xs">
          {formatDate(r.effective_from)} ~ {r.effective_to ? formatDate(r.effective_to) : "未设定"}
        </span>
      ),
    },
    {
      key: "params",
      header: "参数（新签 / 活动）",
      render: (r) => (
        <span className="text-xs text-slate-600">
          新签 {money(r.params_json?.new_contract_award_fen)} / 活动{" "}
          {money(r.params_json?.activity_award_fen)}
        </span>
      ),
    },
    {
      key: "actions",
      header: "操作",
      render: (r) =>
        canConfigure && r.status === "DRAFT" ? (
          <button
            type="button"
            disabled={reviewMut.submitting}
            onClick={() => {
              reviewMut.run(r.id).then((res) => afterReview(res !== null));
            }}
            className="rounded-md border border-slate-300 bg-white px-2 py-1 text-xs text-slate-700 hover:bg-slate-50 disabled:opacity-50"
          >
            审核生效
          </button>
        ) : (
          <span className="text-xs text-slate-400">—</span>
        ),
    },
  ];

  return (
    <div className="space-y-3">
      {list.loading ? <LoadingBlock text="加载规则版本…" /> : null}
      {list.error ? (
        <ErrorState message={list.error} status={list.status} onRetry={list.refetch} />
      ) : null}

      {!list.loading && !list.error ? (
        <>
          <div className="text-xs text-slate-500">共 {rules.length} 个规则版本</div>
          <DataTable
            rows={rules}
            columns={columns}
            rowKey={(r) => r.id}
            emptyText="暂无规则版本（先创建一版规则参数，再由审核人审核生效）"
          />
        </>
      ) : null}

      <CreateRuleForm
        enabled={canConfigure}
        onCreated={() => {
          onToast("规则版本草稿已创建，需由审核人审核后生效", "success");
          list.refetch();
        }}
        onToast={onToast}
      />

      <p className="text-xs text-slate-400">
        规则版本被配置人（DRAFT）与审核人分离：审核人不能审核自己配置的规则；新版本生效后旧版本置为「已取代」，
        已冻结的历史快照仍引用生成时版本（改规则不回算历史）。
      </p>
    </div>
  );
}

function CreateRuleForm({
  enabled,
  onCreated,
  onToast,
}: {
  enabled: boolean;
  onCreated: () => void;
  onToast: (message: string, tone?: ToastTone) => void;
}) {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [code, setCode] = useState("SALES_PERSON_COMMISSION");
  const [effectiveFrom, setEffectiveFrom] = useState(currentPeriodMonth() + "-01");
  const [newContract, setNewContract] = useState("50");
  const [perActivity, setPerActivity] = useState("200");

  const createMut = useApiMutation((payload: Record<string, unknown>) =>
    api.post<RuleVersionRow>("/performance/rule-versions", payload),
  );

  return (
    <section className="space-y-3 rounded-md border border-slate-200 bg-white p-3">
      <button
        type="button"
        disabled={!enabled}
        onClick={() => setOpen((v) => !v)}
        className="rounded-md bg-[var(--color-brand)] px-3 py-1.5 text-sm font-medium text-white hover:opacity-90 disabled:opacity-50"
      >
        {open ? "收起新建" : "新建规则版本"}
      </button>
      {!enabled ? (
        <p className="text-xs text-slate-400">
          当前角色无规则配置权限（片区经理 / 财务 / 系统管理员）。
        </p>
      ) : null}

      {open && enabled ? (
        <form
          onSubmit={(e) => {
            e.preventDefault();
            createMut
              .run({
                code: code.trim(),
                name: name.trim(),
                effective_from: effectiveFrom,
                params: {
                  new_contract_award_fen: Math.round(Number(newContract) * 100),
                  activity_award_fen: Math.round(Number(perActivity) * 100),
                  // T35：巡访奖励与销量阶梯已停用，仍按 0 提交以兼容规则参数结构
                  valid_visit_award_fen: 0,
                  sales_tiers: [[null, 0]],
                },
              })
              .then((res) => {
                if (res) {
                  onCreated();
                  setName("");
                  setOpen(false);
                } else {
                  onToast(createMut.error || "创建失败", "danger");
                }
              });
          }}
          className="space-y-3"
        >
          <div className="grid grid-cols-1 gap-3 md:grid-cols-3">
            <Field label="规则名称 *">
              <input
                required
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="如：2026 年业务员奖励规则"
                className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
              />
            </Field>
            <Field label="规则编码 *">
              <input
                required
                value={code}
                onChange={(e) => setCode(e.target.value)}
                className="font-mono rounded-md border border-slate-300 px-2 py-1.5 text-sm"
              />
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
            <Field label="新签合同奖励（元/单）*">
              <input
                required
                type="number"
                step="0.01"
                min="0"
                value={newContract}
                onChange={(e) => setNewContract(e.target.value)}
                className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
              />
            </Field>
            <Field label="活动达成奖励（元/场）*">
              <input
                required
                type="number"
                step="0.01"
                min="0"
                value={perActivity}
                onChange={(e) => setPerActivity(e.target.value)}
                className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
              />
            </Field>
          </div>

          <p className="text-xs text-slate-400">
            绩效口径已简化为「新签合同奖励 + 活动达成奖励」；有效巡访奖励与确认销量阶梯不再参与计算。
          </p>

          {createMut.error ? <div className="text-xs text-rose-600">{createMut.error}</div> : null}

          <div className="flex gap-2">
            <button
              type="submit"
              disabled={createMut.submitting}
              className="rounded-md bg-[var(--color-brand)] px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50"
            >
              {createMut.submitting ? "提交中…" : "创建草稿"}
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
    </section>
  );
}
