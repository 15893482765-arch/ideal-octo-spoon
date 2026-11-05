/** 工资发放（T20 / PRD §4.10、§6.5）。
 *
 * 接入后端：
 * - `GET /api/payroll/statements?period_month=&status=&page=&page_size=`（财务专用，含员工姓名）；
 * - `GET /api/payroll/statements/{id}`（构成行 + 调整/更正单）；
 * - `POST /api/payroll/statements/generate`（底薪 + 已复核绩效快照自动计入）；
 * - `POST /{id}/review`（财务复核）、`POST /{id}/pay`（财务发放）、
 * - `POST /{id}/adjustments`（发放前调整）、`POST /{id}/corrections`（发放后更正）。
 *
 * 业务红线由后端裁决，页面只做展示与受控交互：
 * - 经理确认（DRAFT→REVIEWED）在经理侧完成，财务页只复核/发放；
 * - 双人复核开启时，发放人不得与复核人同一（后端 403）；
 * - 已发放不可改，只能追加更正单；系统管理员无工资发放能力（T03 红线）。
 */
import { useMemo, useState } from "react";

import { api } from "../lib/api";
import { useAuth } from "../lib/auth";
import {
  currentPeriodMonth,
  formatDate,
  formatDateTime,
  label,
  money,
  statusTone,
  todayInTz,
} from "../lib/format";
import { MODULE } from "../lib/permissions";
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
const STATUS_OPTIONS = ["DRAFT", "REVIEWED", "APPROVED", "PAID"] as const;
const ADJUSTABLE = new Set(["DRAFT", "REVIEWED"]);

interface OptionItem {
  id: string;
  code: string;
  name: string;
}

interface StatementRow {
  id: string;
  statement_no: string;
  user_id: string;
  user_name: string | null;
  period_month: string;
  base_salary_fen: number;
  performance_fen: number;
  adjustment_fen: number;
  gross_pay_fen: number;
  correction_fen: number;
  corrected_pay_fen: number | null;
  status: string;
  confirmed_at: string | null;
  reviewed_at: string | null;
  paid_at: string | null;
  paid_on: string | null;
}

interface StatementLine {
  id: string;
  line_type: string;
  source_object_type: string;
  source_object_id: string;
  amount_fen: number;
  basis_json: Record<string, unknown>;
}

interface AdjustmentRow {
  id: string;
  kind: string;
  amount_fen: number;
  approval_id: string;
  reason: string;
  is_correction: boolean;
}

interface StatementDetail extends StatementRow {
  lines: StatementLine[];
  adjustments: AdjustmentRow[];
}

interface StatementListResponse {
  items: StatementRow[];
  total: number;
  page: number;
  page_size: number;
}

const LINE_LABELS: Record<string, string> = {
  BASE_SALARY: "底薪",
  PERFORMANCE: "绩效",
  ADJUSTMENT: "审批调整",
  CORRECTION: "更正",
};

/** 薪资配置（T35）：按员工维护月薪与生效日期，支持历史版本。 */
interface SalaryConfigRow {
  id: string;
  user_id: string;
  user_name: string | null;
  base_salary_fen: number;
  effective_from: string;
  effective_to: string | null;
  is_active: boolean;
  note: string | null;
  created_at: string;
}

interface EffectiveSalary {
  user_id: string;
  period_month: string;
  base_salary_fen: number | null;
  config_id: string | null;
  effective_from: string | null;
}

/** 元 → 分（只接受最多两位小数，避免浮点误差累计）。 */
function yuanToFen(value: string): number | null {
  const text = value.trim();
  if (!text) return null;
  if (!/^\d+(\.\d{1,2})?$/.test(text)) return null;
  return Math.round(Number(text) * 100);
}

export function Payroll() {
  const { authenticated, hasAbility } = useAuth();
  const { items: toasts, push, dismiss } = useToast();

  const [month, setMonth] = useState(currentPeriodMonth());
  const [monthInput, setMonthInput] = useState(currentPeriodMonth());
  const [status, setStatus] = useState("");
  const [statusInput, setStatusInput] = useState("");
  const [page, setPage] = useState(1);
  const [selectedId, setSelectedId] = useState("");
  const [generating, setGenerating] = useState(false);
  const [showSalaryConfig, setShowSalaryConfig] = useState(false);

  const canReview = hasAbility(MODULE.PAYROLL);
  const canPay = hasAbility(MODULE.PAYROLL_DISBURSE);

  const list = useApiQuery<StatementListResponse>(
    "/payroll/statements",
    { period_month: month || undefined, status: status || undefined, page, page_size: PAGE_SIZE },
    { enabled: authenticated },
  );
  const detail = useApiQuery<StatementDetail>(
    `/payroll/statements/${selectedId}`,
    undefined,
    { enabled: authenticated && Boolean(selectedId) },
  );
  const users = useApiQuery<{ items: OptionItem[] }>(
    "/performance/options/users",
    undefined,
    { enabled: authenticated && generating },
  );

  const reviewMut = useApiMutation((id: string) =>
    api.post<StatementRow>(`/payroll/statements/${id}/review`),
  );
  const payMut = useApiMutation((payload: { id: string; paid_on: string }) =>
    api.post<StatementRow>(`/payroll/statements/${payload.id}/pay`, {
      paid_on: payload.paid_on || undefined,
    }),
  );
  const generateMut = useApiMutation((payload: {
    user_id: string;
    period_month: string;
    base_salary_fen?: number;
  }) => api.post<StatementDetail>("/payroll/statements/generate", payload));
  const adjustMut = useApiMutation((payload: {
    id: string;
    kind: string;
    amount_fen: number;
    approval_id: string;
    reason: string;
    correction: boolean;
  }) =>
    api.post(
      `/payroll/statements/${payload.id}/${payload.correction ? "corrections" : "adjustments"}`,
      {
        kind: payload.kind,
        amount_fen: payload.amount_fen,
        approval_id: payload.approval_id,
        reason: payload.reason,
      },
    ),
  );

  const rows = useMemo(() => list.data?.items ?? [], [list.data]);
  const total = list.data?.total ?? 0;
  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));

  const summary = useMemo(() => {
    const sum = (predicate: (r: StatementRow) => boolean) =>
      rows.filter(predicate).reduce((acc, r) => acc + r.gross_pay_fen, 0);
    return {
      reviewed: rows.filter((r) => r.status === "REVIEWED").length,
      approved: rows.filter((r) => r.status === "APPROVED").length,
      paid: rows.filter((r) => r.status === "PAID").length,
      pageGross: sum(() => true),
    };
  }, [rows]);

  function applyFilters() {
    setMonth(monthInput);
    setStatus(statusInput);
    setPage(1);
  }

  function resetFilters() {
    const next = currentPeriodMonth();
    setMonthInput(next);
    setMonth(next);
    setStatusInput("");
    setStatus("");
    setPage(1);
  }

  function openDetail(id: string) {
    setSelectedId((current) => (current === id ? "" : id));
  }

  const columns: DataTableColumn<StatementRow>[] = [
    {
      key: "no",
      header: "工资单号",
      render: (r) => (
        <button
          type="button"
          onClick={() => openDetail(r.id)}
          className="font-mono text-xs text-[var(--color-brand)] hover:underline"
        >
          {r.statement_no}
        </button>
      ),
    },
    { key: "user", header: "员工", render: (r) => r.user_name ?? r.user_id.slice(0, 8) },
    { key: "month", header: "月份", render: (r) => r.period_month },
    { key: "base", header: "底薪", align: "right", render: (r) => money(r.base_salary_fen) },
    { key: "perf", header: "绩效", align: "right", render: (r) => money(r.performance_fen) },
    {
      key: "adjust",
      header: "调整",
      align: "right",
      render: (r) => money(r.adjustment_fen),
    },
    {
      key: "gross",
      header: "应发合计",
      align: "right",
      render: (r) => (
        <span className="font-medium">{money(r.corrected_pay_fen ?? r.gross_pay_fen)}</span>
      ),
    },
    {
      key: "status",
      header: "状态",
      render: (r) => <StatusBadge tone={statusTone(r.status)} label={label(r.status)} />,
    },
    {
      key: "paid_on",
      header: "发放日",
      render: (r) => (r.paid_on ? formatDate(r.paid_on) : "—"),
    },
    {
      key: "action",
      header: "操作",
      render: (r) => (
        <div className="flex gap-2">
          {r.status === "REVIEWED" && canReview ? (
            <button
              type="button"
              disabled={reviewMut.submitting}
              onClick={() =>
                reviewMut.runSafe(r.id).then((out) => {
                  if (out.ok) {
                    push("复核通过，待财务发放", "success");
                    list.refetch();
                    if (selectedId === r.id) detail.refetch();
                  } else {
                    push(out.error || "复核失败", "danger");
                  }
                })
              }
              className="rounded border border-slate-300 px-2 py-1 text-xs text-slate-700 hover:bg-slate-50 disabled:opacity-50"
            >
              复核通过
            </button>
          ) : null}
          <button
            type="button"
            onClick={() => openDetail(r.id)}
            className="rounded border border-slate-300 px-2 py-1 text-xs text-slate-700 hover:bg-slate-50"
          >
            明细
          </button>
        </div>
      ),
    },
  ];

  if (!authenticated) {
    return (
      <div className="space-y-4">
        <PageHeader title="工资发放" description="月度工资单复核与发放" />
        <SessionNotice hint="工资发放需要真实登录：后端按角色能力（仅财务）与数据域裁剪。" />
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <PageHeader
        title="工资发放"
        description="薪资配置自动带出底薪 + 当月绩效 = 应发；经理确认 → 财务复核 → 财务发放（线下打款，系统只记流水）"
        actions={
          <div className="flex gap-2">
            <button
              type="button"
              onClick={() => setShowSalaryConfig((v) => !v)}
              className="rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm text-slate-700 hover:bg-slate-50"
            >
              {showSalaryConfig ? "收起薪资配置" : "薪资配置"}
            </button>
            <button
              type="button"
              onClick={() => setGenerating((v) => !v)}
              className="rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm text-slate-700 hover:bg-slate-50"
            >
              {generating ? "收起生成" : "生成工资单"}
            </button>
          </div>
        }
      />

      {showSalaryConfig ? (
        <SalaryConfigSection onToast={push} />
      ) : null}

      {generating ? (
        <GenerateForm
          users={users.data?.items ?? []}
          loading={users.loading}
          submitting={generateMut.submitting}
          onSubmit={(payload) =>
            generateMut.runSafe(payload).then((out) => {
              if (out.ok && out.data) {
                push(`工资单 ${out.data.statement_no} 已生成（草稿，待经理确认）`, "success");
                setGenerating(false);
                list.refetch();
                setSelectedId(out.data.id);
              } else {
                push(out.error || "生成失败（请先配置该员工月薪）", "danger");
              }
            })
          }
        />
      ) : null}

      <FilterBar onApply={applyFilters} onReset={resetFilters}>
        <label className="flex flex-col gap-1 text-xs text-slate-600">
          <span>月份</span>
          <input
            type="month"
            value={monthInput}
            onChange={(e) => setMonthInput(e.target.value)}
            className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
          />
        </label>
        <label className="flex flex-col gap-1 text-xs text-slate-600">
          <span>状态</span>
          <select
            value={statusInput}
            onChange={(e) => setStatusInput(e.target.value)}
            className="w-36 rounded-md border border-slate-300 bg-white px-2 py-1.5 text-sm"
          >
            <option value="">全部</option>
            {STATUS_OPTIONS.map((s) => (
              <option key={s} value={s}>
                {label(s)}
              </option>
            ))}
          </select>
        </label>
      </FilterBar>

      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <MetricCard label="待复核" value={summary.reviewed} unit="单" />
        <MetricCard label="待发放" value={summary.approved} unit="单" />
        <MetricCard label="已发放" value={summary.paid} unit="单" />
        <MetricCard label="本页应发" value={money(summary.pageGross)} />
      </div>

      {list.loading ? <LoadingBlock text="加载工资单…" /> : null}
      {list.error ? (
        <ErrorState message={list.error} status={list.status} onRetry={list.refetch} />
      ) : null}

      {!list.loading && !list.error ? (
        rows.length === 0 ? (
          <EmptyState
            title="本月没有工资单"
            description="先由经理确认绩效快照并生成工资单；财务再复核、发放。"
          />
        ) : (
          <section className="space-y-2">
            <DataTable
              rows={rows}
              columns={columns}
              rowKey={(r) => r.id}
              emptyText="暂无工资单"
            />
            <div className="flex items-center justify-between">
              <div className="text-xs text-slate-500">共 {total} 条</div>
              <Pagination page={page} totalPages={totalPages} onPageChange={setPage} />
            </div>
          </section>
        )
      ) : null}

      {selectedId ? (
        detail.loading ? (
          <LoadingBlock text="加载工资单明细…" />
        ) : detail.error ? (
          <ErrorState message={detail.error} status={detail.status} onRetry={detail.refetch} />
        ) : detail.data ? (
          <DetailPanel
            detail={detail.data}
            canReview={canReview}
            canPay={canPay}
            reviewSubmitting={reviewMut.submitting}
            paySubmitting={payMut.submitting}
            adjustSubmitting={adjustMut.submitting}
            onReview={(id) =>
              reviewMut.runSafe(id).then((out) => {
                if (out.ok) {
                  push("复核通过，待发放", "success");
                  detail.refetch();
                  list.refetch();
                } else {
                  push(out.error || "复核失败", "danger");
                }
              })
            }
            onPay={(id, paidOn) =>
              payMut.runSafe({ id, paid_on: paidOn }).then((out) => {
                if (out.ok) {
                  push("已标记发放", "success");
                  detail.refetch();
                  list.refetch();
                } else {
                  push(out.error || "发放失败", "danger");
                }
              })
            }
            onAdjust={(payload) =>
              adjustMut.runSafe(payload).then((out) => {
                if (out.ok) {
                  push(payload.correction ? "更正单已追加" : "调整已追加", "success");
                  detail.refetch();
                  list.refetch();
                } else {
                  push(out.error || "提交失败", "danger");
                }
              })
            }
            onClose={() => setSelectedId("")}
          />
        ) : null
      ) : null}

      <ToastStack items={toasts} onDismiss={dismiss} />
    </div>
  );
}

function GenerateForm({
  users,
  loading,
  submitting,
  onSubmit,
}: {
  users: OptionItem[];
  loading: boolean;
  submitting: boolean;
  onSubmit: (payload: {
    user_id: string;
    period_month: string;
    base_salary_fen?: number;
  }) => void;
}) {
  const [userId, setUserId] = useState("");
  const [month, setMonth] = useState(currentPeriodMonth());
  /** 手动覆盖底薪（留空 = 按薪资配置自动带出） */
  const [baseSalary, setBaseSalary] = useState("");

  const effective = useApiQuery<EffectiveSalary>(
    "/payroll/salary-configs/effective",
    { user_id: userId, period_month: month },
    { enabled: Boolean(userId) && Boolean(month) },
  );
  const autoFen = effective.data?.base_salary_fen ?? null;
  const autoText = autoFen === null ? "" : (autoFen / 100).toFixed(2);
  const displayValue = baseSalary !== "" ? baseSalary : autoText;

  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        if (!userId) return;
        if (baseSalary === "") {
          // 交给后端按薪资配置解析；缺配置会返回 SALARY_CONFIG_MISSING
          onSubmit({ user_id: userId, period_month: month });
          return;
        }
        const fen = yuanToFen(baseSalary);
        if (fen === null) return;
        onSubmit({ user_id: userId, period_month: month, base_salary_fen: fen });
      }}
      className="space-y-2 rounded-md border border-slate-200 bg-white p-3"
    >
      <div className="grid grid-cols-1 gap-3 md:grid-cols-4">
        <Field label="员工 *">
          <select
            required
            value={userId}
            onChange={(e) => {
              setUserId(e.target.value);
              setBaseSalary("");
            }}
            className="rounded-md border border-slate-300 bg-white px-2 py-1.5 text-sm"
          >
            <option value="">{loading ? "加载中…" : "请选择"}</option>
            {users.map((u) => (
              <option key={u.id} value={u.id}>
                {u.name}
                {u.code ? `（${u.code}）` : ""}
              </option>
            ))}
          </select>
        </Field>
        <Field label="月份 *">
          <input
            required
            type="month"
            value={month}
            onChange={(e) => {
              setMonth(e.target.value);
              setBaseSalary("");
            }}
            className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
          />
        </Field>
        <Field label="底薪（元，自动带出，可覆盖）">
          <input
            inputMode="decimal"
            placeholder={autoFen === null ? "未配置月薪，请先录入" : "按薪资配置自动带出"}
            value={displayValue}
            onChange={(e) => setBaseSalary(e.target.value)}
            className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
          />
        </Field>
        <div className="flex items-end">
          <button
            type="submit"
            disabled={submitting || users.length === 0}
            className="rounded-md bg-[var(--color-brand)] px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50"
          >
            {submitting ? "生成中…" : "生成工资单（草稿）"}
          </button>
        </div>
      </div>
      {userId && !effective.loading && autoFen === null ? (
        <div className="rounded-md border border-amber-200 bg-amber-50 p-2 text-xs text-amber-800">
          该员工在此月份没有生效的薪资配置：请先在「薪资配置」录入月薪，或直接手填底薪。
        </div>
      ) : null}
      <p className="text-xs text-slate-500">
        底薪按薪资配置在该月首日生效的版本自动带出；绩效自动取该员工当月最新绩效快照（含未复核）。
        同一员工同月重复生成返回原单，不重算。
      </p>
    </form>
  );
}

function DetailPanel({
  detail,
  canReview,
  canPay,
  reviewSubmitting,
  paySubmitting,
  adjustSubmitting,
  onReview,
  onPay,
  onAdjust,
  onClose,
}: {
  detail: StatementDetail;
  canReview: boolean;
  canPay: boolean;
  reviewSubmitting: boolean;
  paySubmitting: boolean;
  adjustSubmitting: boolean;
  onReview: (id: string) => void;
  onPay: (id: string, paidOn: string) => void;
  onAdjust: (payload: {
    id: string;
    kind: string;
    amount_fen: number;
    approval_id: string;
    reason: string;
    correction: boolean;
  }) => void;
  onClose: () => void;
}) {
  const [paidOn, setPaidOn] = useState(todayInTz());
  const [kind, setKind] = useState("BONUS");
  const [amount, setAmount] = useState("");
  const [approvalId, setApprovalId] = useState("");
  const [reason, setReason] = useState("");

  const lineColumns: DataTableColumn<StatementLine>[] = [
    {
      key: "type",
      header: "构成",
      render: (ln) => LINE_LABELS[ln.line_type] ?? ln.line_type,
    },
    { key: "amount", header: "金额", align: "right", render: (ln) => money(ln.amount_fen) },
    {
      key: "source",
      header: "依据",
      render: (ln) => (
        <span className="font-mono text-xs text-slate-400">
          {ln.source_object_type} · {ln.source_object_id.slice(0, 8)}
        </span>
      ),
    },
  ];

  const adjustable = ADJUSTABLE.has(detail.status);
  const correctionMode = detail.status === "PAID";

  function submitAdjustment() {
    const fen = yuanToFen(amount);
    if (fen === null || fen <= 0 || !approvalId.trim() || !reason.trim()) return;
    onAdjust({
      id: detail.id,
      kind,
      amount_fen: kind === "PENALTY" ? -fen : fen,
      approval_id: approvalId.trim(),
      reason: reason.trim(),
      correction: correctionMode,
    });
  }

  return (
    <section className="space-y-3 rounded-md border border-slate-200 bg-white p-3">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <div className="text-sm font-semibold text-slate-800">
            {detail.user_name ?? detail.user_id.slice(0, 8)} · {detail.period_month}
          </div>
          <div className="font-mono text-xs text-slate-400">{detail.statement_no}</div>
        </div>
        <div className="flex items-center gap-2">
          <StatusBadge tone={statusTone(detail.status)} label={label(detail.status)} />
          <button
            type="button"
            onClick={onClose}
            className="rounded border border-slate-300 px-2 py-1 text-xs text-slate-600"
          >
            关闭
          </button>
        </div>
      </div>

      <div className="grid grid-cols-2 gap-3 text-sm md:grid-cols-5">
        <Info label="底薪" value={money(detail.base_salary_fen)} />
        <Info label="绩效" value={money(detail.performance_fen)} />
        <Info label="调整" value={money(detail.adjustment_fen)} />
        <Info label="应发合计" value={money(detail.gross_pay_fen)} />
        <Info
          label="更正后应发"
          value={detail.corrected_pay_fen === null ? "—" : money(detail.corrected_pay_fen)}
        />
      </div>
      <div className="flex flex-wrap gap-4 text-xs text-slate-500">
        <span>经理确认：{formatDateTime(detail.confirmed_at)}</span>
        <span>财务复核：{formatDateTime(detail.reviewed_at)}</span>
        <span>发放：{formatDate(detail.paid_on)}</span>
      </div>

      <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
        <div>
          <div className="text-xs font-medium text-slate-600">金额构成（整数分）</div>
          <div className="mt-2">
            <DataTable
              rows={detail.lines}
              columns={lineColumns}
              rowKey={(ln) => ln.id}
              emptyText="暂无构成行"
            />
          </div>
        </div>
        <div>
          <div className="text-xs font-medium text-slate-600">审批调整 / 更正单</div>
          <ul className="mt-2 divide-y divide-slate-100 rounded-md border border-slate-200">
            {detail.adjustments.length === 0 ? (
              <li className="px-3 py-2 text-xs text-slate-400">暂无</li>
            ) : (
              detail.adjustments.map((a) => (
                <li key={a.id} className="flex items-center justify-between gap-2 px-3 py-2">
                  <div className="text-xs text-slate-600">
                    <span className={a.amount_fen < 0 ? "text-rose-600" : "text-emerald-700"}>
                      {money(a.amount_fen)}
                    </span>{" "}
                    {a.is_correction ? "更正" : a.kind === "BONUS" ? "奖金" : "罚扣"} · {a.reason}
                  </div>
                  <span className="font-mono text-[10px] text-slate-400">
                    {a.approval_id.slice(0, 8)}
                  </span>
                </li>
              ))
            )}
          </ul>
        </div>
      </div>

      {detail.status === "REVIEWED" && canReview ? (
        <button
          type="button"
          disabled={reviewSubmitting}
          onClick={() => onReview(detail.id)}
          className="rounded-md bg-[var(--color-brand)] px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50"
        >
          {reviewSubmitting ? "复核中…" : "复核通过（待发放）"}
        </button>
      ) : null}

      {detail.status === "APPROVED" && canPay ? (
        <div className="flex flex-wrap items-end gap-2 rounded-md border border-emerald-200 bg-emerald-50/40 p-3">
          <Field label="发放日期">
            <input
              type="date"
              value={paidOn}
              onChange={(e) => setPaidOn(e.target.value)}
              className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
            />
          </Field>
          <button
            type="button"
            disabled={paySubmitting}
            onClick={() => onPay(detail.id, paidOn)}
            className="rounded-md bg-[var(--color-brand)] px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50"
          >
            {paySubmitting ? "发放中…" : "标记已发放"}
          </button>
          <p className="text-xs text-slate-500">
            双人复核开启时，发放人不得与复核人为同一财务（后端校验）。
          </p>
        </div>
      ) : null}

      {detail.status === "DRAFT" ? (
        <p className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800">
          草稿态需片区经理确认后才进入财务复核；财务不能代替经理确认。
        </p>
      ) : null}

      {adjustable || correctionMode ? (
        <div className="space-y-2 rounded-md border border-slate-200 p-3">
          <div className="text-xs font-medium text-slate-600">
            {correctionMode ? "追加更正单（已发放不可改原单）" : "追加审批调整（发放前）"}
          </div>
          <div className="grid grid-cols-1 gap-2 md:grid-cols-5">
            <Field label="类型">
              <select
                value={kind}
                onChange={(e) => setKind(e.target.value)}
                className="rounded-md border border-slate-300 bg-white px-2 py-1.5 text-sm"
              >
                <option value="BONUS">奖金（加）</option>
                <option value="PENALTY">罚扣（减）</option>
              </select>
            </Field>
            <Field label="金额（元）*">
              <input
                value={amount}
                onChange={(e) => setAmount(e.target.value)}
                inputMode="decimal"
                className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
              />
            </Field>
            <Field label="审批单 ID *">
              <input
                value={approvalId}
                onChange={(e) => setApprovalId(e.target.value)}
                placeholder="从审批中心复制"
                className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
              />
            </Field>
            <Field label="原因 *">
              <input
                value={reason}
                onChange={(e) => setReason(e.target.value)}
                className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
              />
            </Field>
            <div className="flex items-end">
              <button
                type="button"
                disabled={adjustSubmitting}
                onClick={submitAdjustment}
                className="rounded-md bg-[var(--color-brand)] px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50"
              >
                {adjustSubmitting ? "提交中…" : correctionMode ? "追加更正单" : "追加调整"}
              </button>
            </div>
          </div>
          <p className="text-xs text-slate-400">
            必须引用已审批且同数据域的审批单（后端校验），未通过的审批会被拒绝。
          </p>
        </div>
      ) : null}
    </section>
  );
}

function Info({ label: text, value }: { label: string; value: string }) {
  return (
    <div>
      <div className="text-xs text-slate-400">{text}</div>
      <div className="text-slate-700">{value}</div>
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

/** 薪资配置：录入月薪 → 工资生成时按归属月自动带出（历史版本可追溯）。 */
function SalaryConfigSection({
  onToast,
}: {
  onToast: (message: string, tone?: "success" | "danger" | "info" | "warning") => void;
}) {
  const users = useApiQuery<{ items: OptionItem[] }>("/performance/options/users");
  const list = useApiQuery<SalaryConfigRow[]>("/payroll/salary-configs");

  const [userId, setUserId] = useState("");
  const [salary, setSalary] = useState("");
  const [effectiveFrom, setEffectiveFrom] = useState(`${currentPeriodMonth()}-01`);
  const [effectiveTo, setEffectiveTo] = useState("");
  const [note, setNote] = useState("");

  const createMut = useApiMutation((payload: Record<string, unknown>) =>
    api.post<SalaryConfigRow>("/payroll/salary-configs", payload),
  );
  const toggleMut = useApiMutation((id: string) =>
    api.post<SalaryConfigRow>(`/payroll/salary-configs/${id}/toggle-active`),
  );

  const itemList = users.data?.items ?? [];
  const rows = list.data ?? [];

  return (
    <section className="space-y-3 rounded-md border border-slate-200 bg-white p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 className="text-sm font-semibold text-slate-700">薪资配置</h2>
        <span className="text-xs text-slate-400">
          工资草稿自动取「归属月首日」生效的月薪版本；新增版本不回写已生成工资单
        </span>
      </div>

      <form
        onSubmit={(e) => {
          e.preventDefault();
          const fen = yuanToFen(salary);
          if (!userId || fen === null) {
            onToast("请选择员工并填写正确的月薪（元）", "danger");
            return;
          }
          createMut
            .runSafe({
              user_id: userId,
              base_salary_fen: fen,
              effective_from: effectiveFrom,
              effective_to: effectiveTo || null,
              note: note.trim() || null,
            })
            .then((out) => {
              if (out.ok) {
                onToast("薪资配置已保存", "success");
                setUserId("");
                setSalary("");
                setNote("");
                setEffectiveTo("");
                list.refetch();
              } else {
                onToast(out.error || "保存失败", "danger");
              }
            });
        }}
        className="space-y-2 rounded-md border border-slate-200 bg-slate-50/60 p-3"
      >
        <div className="grid grid-cols-1 gap-3 md:grid-cols-5">
          <Field label="员工 *">
            <select
              required
              value={userId}
              onChange={(e) => setUserId(e.target.value)}
              className="rounded-md border border-slate-300 bg-white px-2 py-1.5 text-sm"
            >
              <option value="">{users.loading ? "加载中…" : "请选择"}</option>
              {itemList.map((u) => (
                <option key={u.id} value={u.id}>
                  {u.name}
                  {u.code ? `（${u.code}）` : ""}
                </option>
              ))}
            </select>
          </Field>
          <Field label="月薪（元）*">
            <input
              required
              inputMode="decimal"
              placeholder="例如 5000.00"
              value={salary}
              onChange={(e) => setSalary(e.target.value)}
              className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
            />
          </Field>
          <Field label="生效起始日 *">
            <input
              required
              type="date"
              value={effectiveFrom}
              onChange={(e) => setEffectiveFrom(e.target.value)}
              className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
            />
          </Field>
          <Field label="生效结束日（可空）">
            <input
              type="date"
              value={effectiveTo}
              onChange={(e) => setEffectiveTo(e.target.value)}
              className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
            />
          </Field>
          <Field label="备注">
            <input
              value={note}
              onChange={(e) => setNote(e.target.value)}
              placeholder="如：2026 年调薪"
              className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
            />
          </Field>
        </div>
        <div className="flex items-center gap-2">
          <button
            type="submit"
            disabled={createMut.submitting}
            className="rounded-md bg-[var(--color-brand)] px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50"
          >
            {createMut.submitting ? "保存中…" : "保存配置"}
          </button>
          <span className="text-xs text-slate-400">
            同一员工同一生效日只能有一条配置；调薪请用新的生效日新增版本。
          </span>
        </div>
      </form>

      {list.loading ? <LoadingBlock text="加载薪资配置…" /> : null}
      {list.error ? (
        <ErrorState message={list.error} status={list.status} onRetry={list.refetch} />
      ) : null}

      {!list.loading && !list.error ? (
        rows.length === 0 ? (
          <EmptyState
            title="还没有薪资配置"
            description="先为员工录入月薪与生效日期，生成工资单时会自动带出底薪。"
          />
        ) : (
          <DataTable
            rows={rows}
            rowKey={(r) => r.id}
            columns={[
              {
                key: "user",
                header: "员工",
                render: (r) => (
                  <span className="text-slate-800">{r.user_name ?? r.user_id.slice(0, 8)}</span>
                ),
              },
              {
                key: "base",
                header: "月薪",
                align: "right",
                render: (r) => <span className="tabular-nums">{money(r.base_salary_fen)}</span>,
              },
              {
                key: "effective",
                header: "生效区间",
                render: (r) => (
                  <span className="text-xs">
                    {formatDate(r.effective_from)} ~{" "}
                    {r.effective_to ? formatDate(r.effective_to) : "长期"}
                  </span>
                ),
              },
              {
                key: "active",
                header: "状态",
                render: (r) => (
                  <StatusBadge
                    tone={r.is_active ? "success" : "neutral"}
                    label={r.is_active ? "生效中" : "已停用"}
                  />
                ),
              },
              {
                key: "note",
                header: "备注",
                render: (r) => <span className="text-xs text-slate-500">{r.note ?? "—"}</span>,
              },
              {
                key: "actions",
                header: "操作",
                render: (r) => (
                  <button
                    type="button"
                    disabled={toggleMut.submitting}
                    onClick={() => {
                      toggleMut.runSafe(r.id).then((out) => {
                        if (out.ok) {
                          onToast(r.is_active ? "已停用该配置" : "已启用该配置", "info");
                          list.refetch();
                        } else {
                          onToast(out.error || "操作失败", "danger");
                        }
                      });
                    }}
                    className="rounded border border-slate-300 px-2 py-1 text-xs text-slate-700 hover:bg-slate-50 disabled:opacity-50"
                  >
                    {r.is_active ? "停用" : "启用"}
                  </button>
                ),
              },
            ]}
            emptyText="暂无薪资配置"
          />
        )
      ) : null}
    </section>
  );
}
