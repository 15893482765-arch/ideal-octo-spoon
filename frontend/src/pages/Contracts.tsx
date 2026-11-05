/** 合同中心（T09 前端联调 / PRD §4.4、§4.6 / R38）。
 *
 * 接入后端：
 * - `GET /api/contracts?status=&keyword=`（行级范围由服务端按角色注入，返回数组）；
 * - `GET /api/contracts/{id}`（合同 + 不可变版本快照）；
 * - `POST /api/contracts/{id}/{submit|decision|change|termination-request|termination-approve}`。
 *
 * 业务规则（状态机、自审拦截 Q044、版本固化 Q045/Q046、终止不删除 Q047）全部由后端
 * Service 裁决；页面只做展示与受控交互，失败一律展示后端 message。
 */
import { useMemo, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import { api } from "../lib/api";
import { useAuth } from "../lib/auth";
import {
  formatDate,
  formatDateTime,
  label,
  money,
  percent,
  statusTone,
} from "../lib/format";
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

const PAGE_SIZE = 20;

/** 受控状态枚举（与后端 ContractStatus 对齐）。 */
const STATUS_OPTIONS = [
  "DRAFT",
  "PENDING_APPROVAL",
  "RETURNED",
  "REJECTED",
  "APPROVED",
  "EFFECTIVE",
  "TERMINATION_PENDING",
  "TERMINATED",
] as const;

const DRAFT_STATUSES = new Set(["DRAFT", "RETURNED"]);

interface ContractRow {
  id: string;
  merchant_id: string;
  contract_no: string;
  title: string;
  party_name: string;
  status: string;
  version: number;
  template_version: string | null;
  signed_at: string | null;
  effective_from: string | null;
  expire_at: string | null;
  commission_rate: number | null;
  display_subsidy_amount: number | null;
  display_threshold_pct: number | null;
  commission_rule_version: string | null;
  /** 店长姓名：纸质合同乙方签署人 */
  store_manager_name: string | null;
  /** 纸质合同正文（模板渲染或手工修订） */
  body_text: string | null;
  sales_rep_user_id: string | null;
  manager_user_id: string | null;
  attachment_id: string | null;
  has_attachment: boolean;
  renewal_generated_for_expire_at: string | null;
  /** 续约来源：本份合同由哪份合同续约而来 */
  renewed_from_contract_id: string | null;
  created_at: string | null;
}

interface ContractRenewalRow {
  id: string;
  contract_no: string;
  status: string;
  effective_from: string | null;
  expire_at: string | null;
}

interface ContractVersionRow {
  id: string;
  contract_id: string;
  version: number;
  change_type: string;
  title: string;
  party_name: string;
  effective_from: string | null;
  expire_at: string | null;
  commission_rate: number | null;
  display_subsidy_amount: number | null;
  display_threshold_pct: number | null;
  commission_rule_version: string | null;
  attachment_id: string | null;
  created_at: string | null;
}

interface ContractDetailResponse {
  contract: ContractRow;
  versions: ContractVersionRow[];
  renewals: ContractRenewalRow[];
}

export function Contracts() {
  const { authenticated, user, hasAbility } = useAuth();
  const { items: toasts, push, dismiss } = useToast();
  const navigate = useNavigate();

  const [keywordInput, setKeywordInput] = useState("");
  const [statusInput, setStatusInput] = useState("");
  const [applied, setApplied] = useState({ keyword: "", status: "" });
  const [page, setPage] = useState(1);

  const canDraft =
    hasAbility("contract") &&
    (user?.role === "SALES_REP" ||
      user?.role === "REGIONAL_MANAGER" ||
      user?.role === "SYSTEM_ADMIN");
  const canApprove =
    hasAbility("contract") &&
    (user?.role === "REGIONAL_MANAGER" || user?.role === "SYSTEM_ADMIN");

  const list = useApiQuery<ContractRow[]>(
    "/contracts",
    { status: applied.status, keyword: applied.keyword },
    { enabled: authenticated },
  );

  const rows = useMemo(() => list.data ?? [], [list.data]);
  const total = rows.length;
  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const safePage = Math.min(page, totalPages);
  const pageRows = rows.slice((safePage - 1) * PAGE_SIZE, safePage * PAGE_SIZE);

  const columns: DataTableColumn<ContractRow>[] = [
    {
      key: "contract_no",
      header: "合同编号",
      render: (r) => (
        <button
          type="button"
          onClick={() => navigate(`/contracts/${r.id}`)}
          className="font-mono text-xs font-medium text-[var(--color-brand)] hover:underline"
        >
          {r.contract_no}
        </button>
      ),
    },
    { key: "title", header: "合同标题", render: (r) => r.title },
    { key: "party_name", header: "合作方", render: (r) => r.party_name },
    {
      key: "status",
      header: "状态",
      render: (r) => <StatusBadge tone={statusTone(r.status)} label={label(r.status)} />,
    },
    { key: "version", header: "版本", render: (r) => `v${r.version}` },
    {
      key: "effective_from",
      header: "生效日",
      render: (r) => <span className="text-xs text-slate-600">{formatDate(r.effective_from)}</span>,
    },
    {
      key: "expire_at",
      header: "到期日",
      render: (r) => <span className="text-xs text-slate-600">{formatDate(r.expire_at)}</span>,
    },
    {
      key: "commission_rate",
      header: "提成率",
      align: "right",
      render: (r) => percent(r.commission_rate),
    },
    {
      key: "attachment",
      header: "附件",
      render: (r) => (r.has_attachment ? "有" : "—"),
    },
    {
      key: "created_at",
      header: "创建时间",
      render: (r) => (
        <span className="text-xs text-slate-500">{formatDateTime(r.created_at)}</span>
      ),
    },
  ];

  if (!authenticated) {
    return (
      <div className="space-y-4">
        <PageHeader title="合同中心" description="合同申请、审批与版本快照" />
        <SessionNotice hint="当前是开发注入账号，请从登录页用真实账号登录后再查看业务数据。" />
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <PageHeader
        title="合同中心"
        description="合同申请、审批与不可变版本快照（行级范围与状态机由服务端裁决）"
      />

      <FilterBar
        onApply={() => {
          setApplied({ keyword: keywordInput, status: statusInput });
          setPage(1);
        }}
        onReset={() => {
          setKeywordInput("");
          setStatusInput("");
          setApplied({ keyword: "", status: "" });
          setPage(1);
        }}
      >
        <Field label="关键字">
          <input
            value={keywordInput}
            onChange={(e) => setKeywordInput(e.target.value)}
            placeholder="合同编号 / 标题 / 合作方"
            className="w-56 rounded-md border border-slate-300 px-2 py-1.5 text-sm"
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

      {list.loading ? <LoadingBlock text="加载合同列表…" /> : null}
      {list.error ? (
        <ErrorState message={list.error} status={list.status} onRetry={list.refetch} />
      ) : null}

      {!list.loading && !list.error ? (
        <>
          <div className="text-xs text-slate-500">共 {total} 份合同</div>
          <DataTable
            rows={pageRows}
            columns={columns}
            rowKey={(r) => r.id}
            emptyText="暂无合同数据（可用「新建合同」起草，或先确认商家与片区已就绪）"
          />
          <Pagination page={safePage} totalPages={totalPages} onPageChange={setPage} />
          <CreateContractForm
            enabled={canDraft}
            onCreated={(id, no) => {
              push(`合同 ${no} 草稿已创建`, "success");
              list.refetch();
              navigate(`/contracts/${id}`);
            }}
          />
        </>
      ) : null}

      <ToastStack items={toasts} onDismiss={dismiss} />
      <p className="text-xs text-slate-400">
        审批动作（通过/退回/驳回）仅片区经理与系统管理员可执行；业务员不能自审（Q044）。
        {canApprove ? "" : " 当前角色只读。"}
      </p>
    </div>
  );
}

/** 合同详情路由（`/contracts/:id`）：与列表共用同一详情面板。 */
export function ContractDetailPage() {
  const { id } = useParams<{ id: string }>();
  if (!id) {
    return (
      <div className="space-y-4">
        <PageHeader title="合同详情 / 审批" />
        <EmptyState title="缺少合同 ID" description="请从合同中心列表进入详情。" />
      </div>
    );
  }
  return (
    <div className="space-y-4">
      <PageHeader
        title="合同详情 / 审批"
        description="版本快照只读；生效后变更走变更申请生成新版本（Q046）"
        actions={
          <Link
            to="/contracts"
            className="rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm text-slate-600 hover:bg-slate-50"
          >
            返回合同中心
          </Link>
        }
      />
      <ContractDetail id={id} />
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

/** 日期偏移（ISO 日期串，UTC 计算，避免时区跨日）。 */
function shiftDate(iso: string | null, days: number): string {
  const base = iso ? new Date(`${iso}T00:00:00Z`) : new Date();
  if (Number.isNaN(base.getTime())) return "";
  base.setUTCDate(base.getUTCDate() + days);
  return base.toISOString().slice(0, 10);
}

function ContractDetail({ id }: { id: string }) {
  const { authenticated, user, hasAbility } = useAuth();
  const { items: toasts, push, dismiss } = useToast();
  const navigate = useNavigate();

  const canDraft =
    hasAbility("contract") &&
    (user?.role === "SALES_REP" ||
      user?.role === "REGIONAL_MANAGER" ||
      user?.role === "SYSTEM_ADMIN");
  const canApprove =
    hasAbility("contract") &&
    (user?.role === "REGIONAL_MANAGER" || user?.role === "SYSTEM_ADMIN");

  const detail = useApiQuery<ContractDetailResponse>(
    `/contracts/${id}`,
    undefined,
    { enabled: authenticated },
  );

  const submitMut = useApiMutation((reason: string) =>
    api.post(`/contracts/${id}/submit`, { reason }),
  );
  const decideMut = useApiMutation(
    (payload: { decision: "APPROVE" | "RETURN" | "REJECT"; comment: string }) =>
      api.post(`/contracts/${id}/decision`, payload),
  );
  const changeMut = useApiMutation(
    (payload: {
      reason: string;
      expire_at?: string;
      commission_rate?: number;
      title?: string;
      party_name?: string;
    }) => api.post(`/contracts/${id}/change`, payload),
  );
  const terminateReqMut = useApiMutation(
    (payload: { reason: string; terminate_date?: string }) =>
      api.post(`/contracts/${id}/termination-request`, payload),
  );
  const terminateApproveMut = useApiMutation((reason: string) =>
    api.post(`/contracts/${id}/termination-approve`, { reason }),
  );
  // 续约：追加一份接续的新合同（草稿），审批通过后按新生效日生效；旧合同不终止
  const renewMut = useApiMutation(
    (payload: {
      contract_no?: string;
      effective_from: string;
      expire_at: string;
      reason: string;
    }) =>
      api.post<{ id: string; contract_no: string }>(`/contracts/${id}/renewals`, payload),
  );

  const [reason, setReason] = useState("");
  const [comment, setComment] = useState("");
  const [terminateDate, setTerminateDate] = useState("");
  const [newExpireAt, setNewExpireAt] = useState("");
  const [newRate, setNewRate] = useState("");
  const [renewalOpen, setRenewalOpen] = useState(false);
  const [renewalNo, setRenewalNo] = useState("");
  const [renewalFrom, setRenewalFrom] = useState("");
  const [renewalExpire, setRenewalExpire] = useState("");
  const [renewalReason, setRenewalReason] = useState("");

  if (!authenticated) {
    return <SessionNotice hint="合同详情需要真实登录后的后端会话。" />;
  }

  const contract = detail.data?.contract ?? null;
  const versions = detail.data?.versions ?? [];
  const renewals = detail.data?.renewals ?? [];
  // 同一旧合同同时只允许一份未终结的续约申请（后端 CONTRACT_RENEWAL_EXISTS 兜底）
  const openRenewals = renewals.filter(
    (r) => !["REJECTED", "TERMINATED", "EXPIRED"].includes(r.status),
  );
  const status = contract?.status ?? "";

  function openRenewal() {
    const nextFrom = shiftDate(contract?.expire_at ?? null, 1);
    setRenewalNo(`${contract?.contract_no ?? ""}-R1`);
    setRenewalFrom(nextFrom);
    setRenewalExpire(shiftDate(nextFrom, 364));
    setRenewalReason("");
    setRenewalOpen(true);
  }

  function afterAction(ok: boolean, message: string, error: string) {
    if (ok) {
      push(message, "success");
      setReason("");
      setComment("");
      setTerminateDate("");
      setNewExpireAt("");
      setNewRate("");
      detail.refetch();
    } else {
      push(error || "操作失败", "danger");
    }
  }

  const versionColumns: DataTableColumn<ContractVersionRow>[] = [
    { key: "version", header: "版本", render: (r) => `v${r.version}` },
    {
      key: "change_type",
      header: "变更类型",
      render: (r) => <StatusBadge tone="neutral" label={label(r.change_type)} />,
    },
    { key: "title", header: "标题", render: (r) => r.title },
    {
      key: "expire_at",
      header: "到期日",
      render: (r) => <span className="text-xs text-slate-600">{formatDate(r.expire_at)}</span>,
    },
    {
      key: "commission_rate",
      header: "提成率",
      align: "right",
      render: (r) => percent(r.commission_rate),
    },
    {
      key: "commission_rule_version",
      header: "规则版本",
      render: (r) => <span className="font-mono text-xs">{r.commission_rule_version ?? "—"}</span>,
    },
    {
      key: "created_at",
      header: "固化时间",
      render: (r) => (
        <span className="text-xs text-slate-500">{formatDateTime(r.created_at)}</span>
      ),
    },
  ];

  return (
    <section className="space-y-3 rounded-md border border-slate-200 bg-white p-4">
      <h2 className="text-sm font-semibold text-slate-700">合同档案</h2>

      {detail.loading ? <LoadingBlock text="加载合同详情…" /> : null}
      {detail.error ? (
        <ErrorState message={detail.error} status={detail.status} onRetry={detail.refetch} />
      ) : null}

      {contract && !detail.loading && !detail.error ? (
        <>
          <div className="grid grid-cols-2 gap-3 text-sm md:grid-cols-4">
            <Info label="合同编号" value={<span className="font-mono text-xs">{contract.contract_no}</span>} />
            <Info
              label="状态"
              value={<StatusBadge tone={statusTone(contract.status)} label={label(contract.status)} />}
            />
            <Info label="版本" value={`v${contract.version}`} />
            <Info label="模板版本" value={contract.template_version ?? "—"} />
            <Info label="标题" value={contract.title} />
            <Info label="合作方" value={contract.party_name} />
            <Info label="店长姓名" value={contract.store_manager_name ?? "—"} />
            <Info label="生效日" value={formatDate(contract.effective_from)} />
            <Info label="到期日" value={formatDate(contract.expire_at)} />
            <Info label="签订日" value={formatDate(contract.signed_at)} />
            <Info label="提成率" value={percent(contract.commission_rate)} />
            <Info label="陈列补贴" value={money(contract.display_subsidy_amount)} />
            <Info label="陈列达标阈值" value={percent(contract.display_threshold_pct)} />
            <Info label="提成规则版本" value={contract.commission_rule_version ?? "—"} />
            <Info label="附件" value={contract.has_attachment ? "已上传" : "—"} />
            <Info
              label="续签待办"
              value={formatDate(contract.renewal_generated_for_expire_at)}
            />
            {contract.renewed_from_contract_id ? (
              <Info
                label="续约自"
                value={
                  <Link
                    to={`/contracts/${contract.renewed_from_contract_id}`}
                    className="text-[var(--color-brand)] hover:underline"
                  >
                    查看被续约合同
                  </Link>
                }
              />
            ) : null}
            <Info label="创建时间" value={formatDateTime(contract.created_at)} />
          </div>

          <div>
            <div className="text-xs font-medium text-slate-600">版本快照（不可变）</div>
            <div className="mt-2">
              <DataTable
                rows={versions}
                columns={versionColumns}
                rowKey={(r) => r.id}
                emptyText="暂无版本快照（首版在审批通过生效时固化）"
              />
            </div>
          </div>

          {renewals.length > 0 ? (
            <div>
              <div className="text-xs font-medium text-slate-600">
                续约关系（本合同续约出的新合同）
              </div>
              <ul className="mt-2 divide-y divide-slate-100 rounded-md border border-slate-200 bg-white">
                {renewals.map((r) => (
                  <li
                    key={r.id}
                    className="flex flex-wrap items-center justify-between gap-2 px-3 py-2 text-sm"
                  >
                    <Link
                      to={`/contracts/${r.id}`}
                      className="font-mono text-xs text-[var(--color-brand)] hover:underline"
                    >
                      {r.contract_no}
                    </Link>
                    <span className="flex items-center gap-2 text-xs text-slate-500">
                      <StatusBadge tone={statusTone(r.status)} label={label(r.status)} />
                      {formatDate(r.effective_from)} ~ {formatDate(r.expire_at)}
                    </span>
                  </li>
                ))}
              </ul>
            </div>
          ) : null}

          {/* ---- 纸质合同：创建草稿即生成，可在线预览/打印，草稿期可手工修订 ---- */}
          <ContractBodyCard
            contract={contract}
            editable={canDraft && DRAFT_STATUSES.has(status)}
            onSaved={() => {
              push("纸质合同正文已保存", "success");
              detail.refetch();
            }}
          />

          {/* ---- 动作区：按状态与角色裁剪，最终仍由后端二次鉴权 ---- */}
          {canDraft && DRAFT_STATUSES.has(status) ? (
            <EditDraftForm
              contract={contract}
              onSaved={() => {
                push("草稿已保存", "success");
                detail.refetch();
              }}
            />
          ) : null}

          {canDraft && DRAFT_STATUSES.has(status) ? (
            <form
              onSubmit={(e) => {
                e.preventDefault();
                submitMut
                  .runSafe(reason.trim() || "提交合同审批")
                  .then((out) => afterAction(out.ok, "已提交审批", out.error));
              }}
              className="space-y-2 rounded-md border border-slate-200 bg-slate-50 p-3"
            >
              <div className="text-xs font-medium text-slate-600">提交审批（草稿 → 待审批）</div>
              <Field label="提交原因 *">
                <input
                  required
                  value={reason}
                  onChange={(e) => setReason(e.target.value)}
                  className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
                />
              </Field>
              <button
                type="submit"
                disabled={submitMut.submitting}
                className="rounded-md bg-[var(--color-brand)] px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50"
              >
                {submitMut.submitting ? "提交中…" : "提交审批"}
              </button>
            </form>
          ) : null}

          {canApprove && status === "PENDING_APPROVAL" ? (
            <div className="space-y-2 rounded-md border border-slate-200 bg-slate-50 p-3">
              <div className="text-xs font-medium text-slate-600">审批（待审批 → 生效 / 退回 / 驳回）</div>
              <Field label="审批意见">
                <input
                  value={comment}
                  onChange={(e) => setComment(e.target.value)}
                  className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
                />
              </Field>
              <div className="flex flex-wrap gap-2">
                {(
                  [
                    ["APPROVE", "审批通过并生效"],
                    ["RETURN", "退回补充"],
                    ["REJECT", "驳回"],
                  ] as const
                ).map(([decision, text]) => (
                  <button
                    key={decision}
                    type="button"
                    disabled={decideMut.submitting}
                    onClick={() => {
                      decideMut
                        .runSafe({ decision, comment: comment.trim() })
                        .then((out) => afterAction(out.ok, `已${text}`, out.error));
                    }}
                    className="rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm text-slate-700 hover:bg-slate-100 disabled:opacity-50"
                  >
                    {text}
                  </button>
                ))}
              </div>
              <p className="text-xs text-slate-400">提交人与审批人不能为同一人（Q044）。</p>
            </div>
          ) : null}

          {canApprove && status === "EFFECTIVE" ? (
            <form
              onSubmit={(e) => {
                e.preventDefault();
                changeMut
                  .runSafe({
                    reason: reason.trim(),
                    expire_at: newExpireAt || undefined,
                    commission_rate: newRate === "" ? undefined : Number(newRate),
                  })
                  .then((out) => afterAction(out.ok, "变更已生成新版本", out.error));
              }}
              className="space-y-2 rounded-md border border-slate-200 bg-slate-50 p-3"
            >
              <div className="text-xs font-medium text-slate-600">生效后变更（生成新版本，不覆盖旧版）</div>
              <div className="grid grid-cols-1 gap-2 md:grid-cols-3">
                <Field label="新到期日">
                  <input
                    type="date"
                    value={newExpireAt}
                    onChange={(e) => setNewExpireAt(e.target.value)}
                    className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
                  />
                </Field>
                <Field label="新提成率（%）">
                  <input
                    type="number"
                    step="0.0001"
                    min="0"
                    max="100"
                    value={newRate}
                    onChange={(e) => setNewRate(e.target.value)}
                    className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
                  />
                </Field>
                <Field label="变更原因 *">
                  <input
                    required
                    value={reason}
                    onChange={(e) => setReason(e.target.value)}
                    className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
                  />
                </Field>
              </div>
              <button
                type="submit"
                disabled={changeMut.submitting}
                className="rounded-md bg-[var(--color-brand)] px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50"
              >
                {changeMut.submitting ? "提交中…" : "发起变更"}
              </button>
            </form>
          ) : null}

          {canDraft && status === "EFFECTIVE" ? (
            <div className="space-y-2 rounded-md border border-emerald-200 bg-emerald-50/40 p-3">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div className="text-xs font-medium text-slate-600">
                  合同续约（追加接续合同，审批通过后按新生效日生效；旧合同到期自然失效）
                </div>
                <button
                  type="button"
                  disabled={openRenewals.length > 0}
                  onClick={() => (renewalOpen ? setRenewalOpen(false) : openRenewal())}
                  className="rounded-md border border-emerald-300 bg-white px-3 py-1.5 text-sm text-emerald-700 hover:bg-emerald-50 disabled:opacity-50"
                >
                  {renewalOpen ? "收起续约" : "发起续约"}
                </button>
              </div>

              {openRenewals.length > 0 ? (
                <p className="text-xs text-amber-700">
                  已有进行中的续约申请：{openRenewals.map((r) => r.contract_no).join("、")}
                  （先处理后再发起）
                </p>
              ) : null}

              {renewalOpen && openRenewals.length === 0 ? (
                <form
                  onSubmit={(e) => {
                    e.preventDefault();
                    renewMut
                      .runSafe({
                        contract_no: renewalNo.trim() || undefined,
                        effective_from: renewalFrom,
                        expire_at: renewalExpire,
                        reason: renewalReason.trim() || "合同续约",
                      })
                      .then((out) => {
                        if (out.ok && out.data) {
                          push(
                            `续约合同 ${out.data.contract_no} 已创建（草稿），请提交审批`,
                            "success",
                          );
                          navigate(`/contracts/${out.data.id}`);
                        } else {
                          push(out.error || "续约申请失败", "danger");
                        }
                      });
                  }}
                  className="space-y-2"
                >
                  <div className="grid grid-cols-1 gap-2 md:grid-cols-4">
                    <Field label="新合同编号">
                      <input
                        value={renewalNo}
                        onChange={(e) => setRenewalNo(e.target.value)}
                        placeholder="留空则自动生成"
                        className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
                      />
                    </Field>
                    <Field label="新生效日 *">
                      <input
                        type="date"
                        required
                        value={renewalFrom}
                        min={shiftDate(contract.expire_at, 1)}
                        onChange={(e) => setRenewalFrom(e.target.value)}
                        className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
                      />
                    </Field>
                    <Field label="新到期日 *">
                      <input
                        type="date"
                        required
                        value={renewalExpire}
                        min={renewalFrom}
                        onChange={(e) => setRenewalExpire(e.target.value)}
                        className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
                      />
                    </Field>
                    <Field label="续约原因 *">
                      <input
                        required
                        value={renewalReason}
                        onChange={(e) => setRenewalReason(e.target.value)}
                        className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
                      />
                    </Field>
                  </div>
                  <button
                    type="submit"
                    disabled={renewMut.submitting}
                    className="rounded-md bg-[var(--color-brand)] px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50"
                  >
                    {renewMut.submitting ? "提交中…" : "创建续约合同（草稿）"}
                  </button>
                  <p className="text-xs text-slate-500">
                    新生效日必须晚于原合同到期日（{formatDate(contract.expire_at)}）；
                    续约合同仍需提交审批，审批通过后按新生效日生效。
                  </p>
                </form>
              ) : null}
            </div>
          ) : null}

          {status === "EFFECTIVE" ? (
            <form
              onSubmit={(e) => {
                e.preventDefault();
                terminateReqMut
                  .runSafe({ reason: reason.trim(), terminate_date: terminateDate || undefined })
                  .then((out) => afterAction(out.ok, "终止申请已提交", out.error));
              }}
              className="space-y-2 rounded-md border border-slate-200 bg-slate-50 p-3"
            >
              <div className="text-xs font-medium text-slate-600">终止申请（终止不是删除，记录保留）</div>
              <div className="grid grid-cols-1 gap-2 md:grid-cols-2">
                <Field label="申请原因 *">
                  <input
                    required
                    value={reason}
                    onChange={(e) => setReason(e.target.value)}
                    className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
                  />
                </Field>
                <Field label="终止日期">
                  <input
                    type="date"
                    value={terminateDate}
                    onChange={(e) => setTerminateDate(e.target.value)}
                    className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
                  />
                </Field>
              </div>
              <button
                type="submit"
                disabled={terminateReqMut.submitting}
                className="rounded-md border border-rose-300 bg-white px-3 py-1.5 text-sm text-rose-700 hover:bg-rose-50 disabled:opacity-50"
              >
                {terminateReqMut.submitting ? "提交中…" : "提交终止申请"}
              </button>
            </form>
          ) : null}

          {canApprove && status === "TERMINATION_PENDING" ? (
            <form
              onSubmit={(e) => {
                e.preventDefault();
                terminateApproveMut
                  .runSafe(reason.trim() || "同意终止")
                  .then((out) => afterAction(out.ok, "终止已批准", out.error));
              }}
              className="space-y-2 rounded-md border border-slate-200 bg-slate-50 p-3"
            >
              <div className="text-xs font-medium text-slate-600">终止审批（终止待审 → 终止）</div>
              <Field label="审批原因 *">
                <input
                  required
                  value={reason}
                  onChange={(e) => setReason(e.target.value)}
                  className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
                />
              </Field>
              <button
                type="submit"
                disabled={terminateApproveMut.submitting}
                className="rounded-md bg-[var(--color-brand)] px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50"
              >
                {terminateApproveMut.submitting ? "提交中…" : "批准终止"}
              </button>
            </form>
          ) : null}
        </>
      ) : null}

      {!contract && !detail.loading && !detail.error ? (
        <EmptyState title="未找到合同" description="可能已超出你的行级可见范围。" />
      ) : null}

      <ToastStack items={toasts} onDismiss={dismiss} />
    </section>
  );
}

function CreateContractForm({
  enabled,
  onCreated,
}: {
  enabled: boolean;
  onCreated: (id: string, contractNo: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [contractNo, setContractNo] = useState("");
  const [title, setTitle] = useState("");
  const [partyName, setPartyName] = useState("");
  const [merchantId, setMerchantId] = useState("");
  const [effectiveFrom, setEffectiveFrom] = useState("");
  const [expireAt, setExpireAt] = useState("");
  const [threshold, setThreshold] = useState("50");
  // 提成规则版本：提交审批前必须固化（后端 _assert_fixed_terms_ready），
  // 给一个符合命名惯例的默认值，业务员可直接接受或改写。
  const [ruleVersion, setRuleVersion] = useState(
    () => `comm-v${new Date().getFullYear()}-01`,
  );
  // 店长姓名：纸质合同签署人；留空由服务端取商家主联系人自动填入
  const [managerName, setManagerName] = useState("");

  const merchants = useApiQuery<{ items: { id: string; code: string; name: string }[] }>(
    "/merchants",
    { page: 1, page_size: 100 },
    { enabled: open },
  );

  const createMut = useApiMutation((payload: Record<string, unknown>) =>
    api.post<{ id: string; contract_no: string }>("/contracts", payload),
  );

  const merchantOptions = merchants.data?.items ?? [];

  return (
    <div className="space-y-2 rounded-md border border-slate-200 bg-white p-3">
      <button
        type="button"
        disabled={!enabled}
        onClick={() => setOpen((v) => !v)}
        className="rounded-md bg-[var(--color-brand)] px-3 py-1.5 text-sm font-medium text-white hover:opacity-90 disabled:opacity-50"
      >
        {open ? "收起新建" : "新建合同"}
      </button>
      {!enabled ? (
        <p className="text-xs text-slate-400">当前角色无起草权限（业务员 / 片区经理 / 系统管理员）。</p>
      ) : null}

      {open && enabled ? (
        <form
          onSubmit={(e) => {
            e.preventDefault();
            createMut
              .run({
                merchant_id: merchantId || merchantOptions[0]?.id || "",
                contract_no: contractNo.trim(),
                title: title.trim(),
                party_name: partyName.trim(),
                effective_from: effectiveFrom || undefined,
                expire_at: expireAt || undefined,
                display_threshold_pct: threshold === "" ? undefined : Number(threshold),
                commission_rule_version: ruleVersion.trim() || undefined,
                store_manager_name: managerName.trim() || undefined,
                reason: "创建合同草稿",
              })
              .then((res) => {
                if (res) {
                  onCreated(res.id, res.contract_no);
                  setContractNo("");
                  setTitle("");
                  setPartyName("");
                  setManagerName("");
                  setOpen(false);
                }
              });
          }}
          className="space-y-3"
        >
          {merchantOptions.length === 0 && !merchants.loading ? (
            <div className="rounded-md border border-amber-200 bg-amber-50 p-2 text-xs text-amber-800">
              尚未取到商家选项（`GET /api/merchants`）。请先在商家中心建立商家。
            </div>
          ) : null}
          <div className="grid grid-cols-1 gap-3 md:grid-cols-3">
            <Field label="合同编号 *">
              <input
                required
                value={contractNo}
                onChange={(e) => setContractNo(e.target.value)}
                className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
              />
            </Field>
            <Field label="合同标题 *">
              <input
                required
                value={title}
                onChange={(e) => setTitle(e.target.value)}
                className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
              />
            </Field>
            <Field label="合作方 *">
              <input
                required
                value={partyName}
                onChange={(e) => setPartyName(e.target.value)}
                className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
              />
            </Field>
            <Field label="关联商家 *">
              <select
                required
                value={merchantId}
                onChange={(e) => setMerchantId(e.target.value)}
                className="rounded-md border border-slate-300 bg-white px-2 py-1.5 text-sm"
              >
                <option value="">请选择</option>
                {merchantOptions.map((m) => (
                  <option key={m.id} value={m.id}>
                    {m.code} · {m.name}
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
            <Field label="到期日 *">
              <input
                required
                type="date"
                value={expireAt}
                onChange={(e) => setExpireAt(e.target.value)}
                className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
              />
            </Field>
            <Field label="陈列达标阈值（%）">
              <input
                type="number"
                min="0"
                max="100"
                value={threshold}
                onChange={(e) => setThreshold(e.target.value)}
                className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
              />
            </Field>
            <Field label="提成规则版本 *">
              <input
                required
                value={ruleVersion}
                onChange={(e) => setRuleVersion(e.target.value)}
                placeholder="如 comm-v2026-01"
                className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
              />
            </Field>
            <Field label="店长姓名（纸质合同签署人）">
              <input
                value={managerName}
                onChange={(e) => setManagerName(e.target.value)}
                placeholder="留空自动取商家主联系人"
                className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
              />
            </Field>
          </div>
          <p className="text-xs text-slate-400">
            生效日、到期日与提成规则版本在提交审批前必须固化，否则服务端会拒绝提交
            （CONTRACT_TERMS_INCOMPLETE）。创建草稿即生成纸质合同正文，可在详情页预览/打印。
          </p>
          {createMut.error ? <div className="text-xs text-rose-600">{createMut.error}</div> : null}
          <div className="flex gap-2">
            <button
              type="submit"
              disabled={createMut.submitting || merchantOptions.length === 0}
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
    </div>
  );
}

/** 草稿补齐：提交审批前必须固化「生效日 / 到期日 / 提成规则版本」（后端 CONTRACT_TERMS_INCOMPLETE）。
 *
 * 「新建合同」早期不采集这三项，草稿创建后永远无法提交；这里提供补齐入口，
 * 让已存在的草稿修正后继续提交（`PUT /api/contracts/{id}`）。
 */
function EditDraftForm({
  contract,
  onSaved,
}: {
  contract: ContractRow;
  onSaved: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [title, setTitle] = useState(contract.title);
  const [partyName, setPartyName] = useState(contract.party_name);
  const [effectiveFrom, setEffectiveFrom] = useState(contract.effective_from ?? "");
  const [expireAt, setExpireAt] = useState(contract.expire_at ?? "");
  const [rate, setRate] = useState(
    contract.commission_rate === null ? "" : String(contract.commission_rate),
  );
  const [threshold, setThreshold] = useState(
    String(contract.display_threshold_pct ?? 50),
  );
  const [ruleVersion, setRuleVersion] = useState(contract.commission_rule_version ?? "");
  const [managerName, setManagerName] = useState(contract.store_manager_name ?? "");

  const saveMut = useApiMutation((payload: Record<string, unknown>) =>
    api.put<ContractRow>(`/contracts/${contract.id}`, payload),
  );

  const missing = [
    effectiveFrom ? null : "生效日",
    expireAt ? null : "到期日",
    ruleVersion.trim() ? null : "提成规则版本",
  ].filter((x): x is string => x !== null);

  return (
    <div className="space-y-2 rounded-md border border-slate-200 bg-white p-3">
      <div className="flex items-center justify-between">
        <div className="text-xs font-medium text-slate-600">草稿条款（补齐后才能提交审批）</div>
        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          className="rounded-md border border-slate-300 bg-white px-3 py-1.5 text-xs text-slate-700 hover:bg-slate-50"
        >
          {open ? "收起编辑" : "编辑草稿"}
        </button>
      </div>

      {missing.length > 0 ? (
        <p className="rounded-md border border-amber-200 bg-amber-50 px-2 py-1.5 text-xs text-amber-800">
          提交前仍缺：{missing.join("、")}
        </p>
      ) : null}

      {open ? (
        <form
          onSubmit={(e) => {
            e.preventDefault();
            saveMut
              .runSafe({
                merchant_id: contract.merchant_id,
                contract_no: contract.contract_no,
                title: title.trim(),
                party_name: partyName.trim(),
                effective_from: effectiveFrom || undefined,
                expire_at: expireAt || undefined,
                commission_rate: rate === "" ? undefined : Number(rate),
                display_threshold_pct: threshold === "" ? undefined : Number(threshold),
                commission_rule_version: ruleVersion.trim() || undefined,
                store_manager_name: managerName.trim() || undefined,
                // 本表单未暴露的字段原样回传，避免被 update_draft 置空
                display_subsidy_amount: contract.display_subsidy_amount ?? undefined,
                reason: "补齐合同固化条款",
              })
              .then((out) => {
                if (out.ok) {
                  setOpen(false);
                  onSaved();
                }
              });
          }}
          className="space-y-3"
        >
          <div className="grid grid-cols-1 gap-3 md:grid-cols-3">
            <Field label="合同编号（不可改）">
              <input
                readOnly
                value={contract.contract_no}
                className="rounded-md border border-slate-200 bg-slate-100 px-2 py-1.5 text-sm text-slate-500"
              />
            </Field>
            <Field label="合同标题 *">
              <input
                required
                value={title}
                onChange={(e) => setTitle(e.target.value)}
                className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
              />
            </Field>
            <Field label="合作方 *">
              <input
                required
                value={partyName}
                onChange={(e) => setPartyName(e.target.value)}
                className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
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
            <Field label="到期日 *">
              <input
                required
                type="date"
                value={expireAt}
                onChange={(e) => setExpireAt(e.target.value)}
                className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
              />
            </Field>
            <Field label="提成率（%）">
              <input
                type="number"
                step="0.0001"
                min="0"
                max="100"
                value={rate}
                onChange={(e) => setRate(e.target.value)}
                className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
              />
            </Field>
            <Field label="陈列达标阈值（%）">
              <input
                type="number"
                min="0"
                max="100"
                value={threshold}
                onChange={(e) => setThreshold(e.target.value)}
                className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
              />
            </Field>
            <Field label="提成规则版本 *">
              <input
                required
                value={ruleVersion}
                onChange={(e) => setRuleVersion(e.target.value)}
                placeholder="如 comm-v2026-01"
                className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
              />
            </Field>
            <Field label="店长姓名（纸质合同签署人）">
              <input
                value={managerName}
                onChange={(e) => setManagerName(e.target.value)}
                placeholder="留空自动取商家主联系人"
                className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
              />
            </Field>
          </div>
          {saveMut.error ? (
            <div className="text-xs text-rose-600">{saveMut.error}</div>
          ) : null}
          <button
            type="submit"
            disabled={saveMut.submitting}
            className="rounded-md bg-[var(--color-brand)] px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50"
          >
            {saveMut.submitting ? "保存中…" : "保存草稿"}
          </button>
        </form>
      ) : null}
    </div>
  );
}

/** 打开打印窗口输出 A4 版式合同正文（浏览器可打印或另存 PDF）。 */
function printContractBody(title: string, body: string): void {
  const win = window.open("", "_blank", "width=900,height=1200");
  if (!win) return;
  const escaped = body
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
  win.document.write(`<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<title>${title}</title>
<style>
  @page { size: A4; margin: 22mm 20mm; }
  body { font-family: "SimSun", "Songti SC", "Noto Serif CJK SC", serif;
         font-size: 12pt; line-height: 2; color: #000; margin: 0; }
  pre { font-family: inherit; white-space: pre-wrap; word-break: break-word; margin: 0; }
</style>
</head>
<body><pre>${escaped}</pre></body>
</html>`);
  win.document.close();
  win.focus();
  win.print();
}

/** 纸质合同区块：创建草稿即由服务端按模板生成正文；支持在线预览、打印与草稿期手工修订。 */
export function ContractBodyCard({
  contract,
  editable,
  onSaved,
}: {
  contract: ContractRow;
  editable: boolean;
  onSaved: () => void;
}) {
  const [editing, setEditing] = useState(false);
  const [draftBody, setDraftBody] = useState(contract.body_text ?? "");
  const body = contract.body_text ?? "";

  const saveMut = useApiMutation((payload: Record<string, unknown>) =>
    api.put<ContractRow>(`/contracts/${contract.id}`, payload),
  );

  const saveManualBody = () => {
    saveMut
      .runSafe({
        merchant_id: contract.merchant_id,
        contract_no: contract.contract_no,
        title: contract.title,
        party_name: contract.party_name,
        effective_from: contract.effective_from ?? undefined,
        expire_at: contract.expire_at ?? undefined,
        commission_rate: contract.commission_rate ?? undefined,
        display_threshold_pct: contract.display_threshold_pct ?? undefined,
        commission_rule_version: contract.commission_rule_version ?? undefined,
        store_manager_name: contract.store_manager_name ?? undefined,
        display_subsidy_amount: contract.display_subsidy_amount ?? undefined,
        body_text: draftBody,
        reason: "手工修订纸质合同正文",
      })
      .then((out) => {
        if (out.ok) {
          setEditing(false);
          onSaved();
        }
      });
  };

  return (
    <div className="space-y-2 rounded-md border border-slate-200 bg-white p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="text-xs font-medium text-slate-600">
          纸质合同（A4，创建草稿时自动生成；商家店名/店长姓名/日期已填入）
        </div>
        <div className="flex gap-2">
          <button
            type="button"
            disabled={!body}
            onClick={() => printContractBody(contract.title, body)}
            className="rounded-md border border-slate-300 bg-white px-3 py-1.5 text-xs text-slate-700 hover:bg-slate-50 disabled:opacity-50"
          >
            打印 / 另存 PDF
          </button>
          {editable ? (
            <button
              type="button"
              onClick={() => {
                setDraftBody(body);
                setEditing((v) => !v);
              }}
              className="rounded-md border border-slate-300 bg-white px-3 py-1.5 text-xs text-slate-700 hover:bg-slate-50"
            >
              {editing ? "取消修订" : "修订正文"}
            </button>
          ) : null}
        </div>
      </div>

      {body ? (
        <div className="overflow-x-auto rounded-md border border-slate-200 bg-slate-50 p-3">
          {/* A4 预览：210mm 在 96dpi 下约 794px */}
          <pre className="mx-auto w-[794px] max-w-full whitespace-pre-wrap break-words bg-white px-12 py-10 font-serif text-[13px] leading-7 text-slate-900 shadow-sm">
            {body}
          </pre>
        </div>
      ) : (
        <p className="rounded-md border border-amber-200 bg-amber-50 px-2 py-1.5 text-xs text-amber-800">
          正文尚未生成：保存一次草稿条款即可按模板生成。
        </p>
      )}

      {editing ? (
        <div className="space-y-2">
          <textarea
            value={draftBody}
            onChange={(e) => setDraftBody(e.target.value)}
            rows={16}
            className="w-full rounded-md border border-slate-300 px-2 py-1.5 font-mono text-xs leading-6"
          />
          {saveMut.error ? (
            <div className="text-xs text-rose-600">{saveMut.error}</div>
          ) : null}
          <button
            type="button"
            onClick={saveManualBody}
            disabled={saveMut.submitting}
            className="rounded-md bg-[var(--color-brand)] px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50"
          >
            {saveMut.submitting ? "保存中…" : "保存正文"}
          </button>
        </div>
      ) : null}
    </div>
  );
}
