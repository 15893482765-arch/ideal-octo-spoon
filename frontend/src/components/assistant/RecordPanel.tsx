/** 助手右栏「命中记录」面板（PRD §5.1）：
 *
 * 助手回复命中业务记录（合同/商家等）时，右栏列出记录并就地展示详情：
 * - 合同：凭 object_id 拉 `/contracts/{id}` 详情（仍受行级权限约束）；
 * - 其余类型：展示标签并提供业务页面链接（页面自身再鉴权）。
 * 面板只读，不提供任何写操作入口。
 */
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { api } from "../../lib/api";
import type { AssistantRecordRef } from "../../lib/assistant";
import { formatDate, label, money, percent, statusTone } from "../../lib/format";
import { cn } from "../../lib/utils";
import { StatusBadge } from "../StatusBadge";

interface ContractDetail {
  contract_no: string;
  title: string;
  party_name: string;
  status: string;
  version: number;
  signed_at: string | null;
  effective_from: string | null;
  expire_at: string | null;
  commission_rate: number | null;
  display_subsidy_amount: number | null;
  display_threshold_pct: number | null;
  commission_rule_version: string | null;
  store_manager_name: string | null;
}

interface ContractDetailResponse {
  contract: ContractDetail;
  versions: { version: number; change_type: string }[];
  renewals: { contract_no: string; status: string }[];
}

export interface RecordPanelProps {
  records: AssistantRecordRef[];
  onClose: () => void;
}

/** 对象类型 → 前端业务页面路由（record.path 是后端 API 路径，不作页面跳转用）。
 *  与 App.tsx 实际路由一一对应；未映射的类型不提供页面链接。 */
const PAGE_ROUTES: Record<string, (id: string) => string> = {
  CONTRACT: (id) => `/contracts/${id}`,
  MERCHANT: (id) => `/merchants/${id}`,
  FREEZER: (id) => `/assets/freezers/${id}`,
};

function pagePath(record: AssistantRecordRef): string | null {
  const to = PAGE_ROUTES[record.object_type];
  return to ? to(record.object_id) : null;
}

/** 对象类型中文名（LABELS 只覆盖状态枚举，不含对象类型） */
const TYPE_LABELS: Record<string, string> = {
  CONTRACT: "合同",
  MERCHANT: "商家",
  FREEZER: "冰柜",
  VISIT_TASK: "拜访任务",
  VISIT_RECORD: "拜访记录",
  REPLENISHMENT: "补货单",
};

function typeLabel(record: AssistantRecordRef): string {
  return TYPE_LABELS[record.object_type] ?? label(record.object_type);
}

export function RecordPanel({ records, onClose }: RecordPanelProps) {
  const [selectedId, setSelectedId] = useState(records[0]?.object_id ?? null);

  // 新一轮回复换了记录集 → 默认选中第一条
  useEffect(() => {
    setSelectedId(records[0]?.object_id ?? null);
  }, [records]);

  const selected =
    records.find((r) => r.object_id === selectedId) ?? records[0] ?? null;

  return (
    <aside
      aria-label="命中记录"
      data-testid="assistant-record-panel"
      className="flex max-h-72 w-full shrink-0 flex-col overflow-hidden rounded-md border border-slate-200 bg-white md:max-h-none md:w-80"
    >
      <header className="flex items-center justify-between border-b border-slate-200 px-3 py-2.5">
        <h3 className="text-sm font-semibold text-slate-800">命中记录</h3>
        <button
          type="button"
          aria-label="关闭命中记录面板"
          onClick={onClose}
          className="rounded px-1.5 text-slate-400 hover:bg-slate-100 hover:text-slate-600"
        >
          ×
        </button>
      </header>

      {records.length > 1 ? (
        <ul className="flex gap-1.5 overflow-x-auto border-b border-slate-100 px-3 py-2">
          {records.map((r) => (
            <li key={r.object_id}>
              <button
                type="button"
                onClick={() => setSelectedId(r.object_id)}
                className={cn(
                  "whitespace-nowrap rounded-full border px-2.5 py-1 text-xs",
                  selected?.object_id === r.object_id
                    ? "border-[var(--color-brand)] bg-blue-50/60 text-[var(--color-brand)]"
                    : "border-slate-200 text-slate-600 hover:bg-slate-50",
                )}
              >
                {r.label}
              </button>
            </li>
          ))}
        </ul>
      ) : null}

      <div className="min-h-0 flex-1 overflow-y-auto p-3">
        {selected ? <RecordDetail record={selected} /> : null}
      </div>
    </aside>
  );
}

function RecordDetail({ record }: { record: AssistantRecordRef }) {
  if (record.object_type === "CONTRACT") {
    return <ContractCard record={record} />;
  }
  const to = pagePath(record);
  return (
    <div className="text-sm text-slate-700">
      <p className="font-medium">{record.label}</p>
      <p className="mt-1 text-xs text-slate-500">
        {typeLabel(record)} · 详情请到业务页面查看
      </p>
      {to ? (
        <Link
          to={to}
          className="mt-2 inline-block rounded-md border border-slate-200 px-2.5 py-1 text-xs text-[var(--color-brand)] hover:bg-slate-50"
        >
          打开业务页面
        </Link>
      ) : null}
    </div>
  );
}

function ContractCard({ record }: { record: AssistantRecordRef }) {
  const [data, setData] = useState<ContractDetailResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    setData(null);
    api
      .get<ContractDetailResponse>(
        `/contracts/${encodeURIComponent(record.object_id)}`,
      )
      .then((resp) => {
        if (!cancelled) setData(resp);
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : "合同详情加载失败");
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [record.object_id]);

  if (loading) {
    return (
      <p className="text-xs text-slate-400" role="status">
        正在载入合同详情…
      </p>
    );
  }
  if (error || !data) {
    return (
      <p className="rounded-md border border-rose-200 bg-rose-50 px-2.5 py-2 text-xs text-rose-700">
        {error ?? "合同详情加载失败"}
      </p>
    );
  }

  const c = data.contract;
  return (
    <div data-testid="assistant-contract-card" className="text-sm">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <p className="truncate font-medium text-slate-800">{c.title}</p>
          <p className="mt-0.5 text-xs text-slate-500">编号 {c.contract_no}</p>
        </div>
        <StatusBadge tone={statusTone(c.status)} label={label(c.status)} />
      </div>

      <dl className="mt-3 grid grid-cols-2 gap-x-3 gap-y-2 text-xs">
        <Field label="签约主体" value={c.party_name} wide />
        <Field label="签订日" value={formatDate(c.signed_at)} />
        <Field label="版本" value={`v${c.version}`} />
        <Field label="生效日" value={formatDate(c.effective_from)} />
        <Field label="到期日" value={formatDate(c.expire_at)} />
        <Field label="提成率" value={percent(c.commission_rate)} />
        <Field label="陈列补贴" value={money(c.display_subsidy_amount)} />
        <Field label="陈列达标阈值" value={percent(c.display_threshold_pct)} />
        <Field label="店长" value={c.store_manager_name ?? "—"} />
      </dl>

      {data.versions.length > 1 ? (
        <p className="mt-3 text-[11px] text-slate-400">
          共 {data.versions.length} 个历史版本；续约 {data.renewals.length} 份
        </p>
      ) : null}

      {pagePath(record) ? (
        <Link
          to={pagePath(record) as string}
          data-testid="assistant-record-open"
          className="mt-3 inline-block rounded-md bg-[var(--color-brand)] px-2.5 py-1 text-xs font-medium text-white hover:opacity-90"
        >
          打开合同页面
        </Link>
      ) : null}
    </div>
  );
}

function Field({
  label: text,
  value,
  wide,
}: {
  label: string;
  value: React.ReactNode;
  wide?: boolean;
}) {
  return (
    <div className={wide ? "col-span-2" : undefined}>
      <dt className="text-slate-400">{text}</dt>
      <dd className="mt-0.5 text-slate-700">{value}</dd>
    </div>
  );
}
