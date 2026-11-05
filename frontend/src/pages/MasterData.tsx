/** 品牌 / SKU / 片区 / 规则 / 导入（T07 前端联调 / PRD §2.1、§2.2、§6 R18、§10.2）。
 *
 * 接入后端：
 * - 台账（只读）：`GET /api/admin/master-data/brands|regions|skus`；
 * - 批次列表：`GET /api/admin/master-data/imports`；
 * - 四步导入：`POST /imports`（上传 + 预检）→ `POST /{id}/preflight` →
 *   `POST /{id}/confirm`（只提交布尔决定）→ `POST /{id}/execute`。
 *
 * 红线（后端 MasterDataImportService 裁决，前端只如实提示）：
 * - 源表「登录密码」列在解析阶段即被丢弃，只作为「已忽略列」回报，页面也绝不回显任何密码；
 * - 角色权限表只做差异预览，不覆盖代码权限矩阵；
 * - 存在未知片区时批次不可确认；账号默认不启用，由管理员逐个勾选；
 * - 执行是单事务「全有或全无」，激活凭据只在执行响应里一次性返回，本页不持久化；
 * - 同一文件重复上传命中成功后直接返回既有批次（幂等），不会把阶段覆盖回预检。
 */
import { useMemo, useState } from "react";
import { Link } from "react-router-dom";

import { api } from "../lib/api";
import { useAuth } from "../lib/auth";
import { formatDateTime, label, money, statusTone } from "../lib/format";
import { useApiMutation, useApiQuery } from "../lib/useApi";
import { useToast } from "../lib/useToast";
import { ErrorState, LoadingBlock, SessionNotice } from "../components/AsyncState";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { DataTable, type DataTableColumn } from "../components/DataTable";
import { PageHeader } from "../components/PageHeader";
import { StatusBadge } from "../components/StatusBadge";
import { ToastStack } from "../components/Toast";
import type { ToastTone } from "../components/Toast";

const BASE = "/admin/master-data";

interface BrandRow {
  id: string;
  code: string;
  name: string;
  is_demo: boolean;
  is_active: boolean;
}

interface RegionRow {
  id: string;
  code: string;
  name: string;
  is_active: boolean;
}

interface SkuRow {
  id: string;
  code: string;
  name: string;
  brand_code: string;
  category_name: string | null;
  capacity_ml: number | null;
  case_spec: string;
  suggested_price_fen: number;
  is_active: boolean;
}

interface RoleRule {
  role_label: string;
  role_code: string | null;
  function_permissions: string | null;
  data_permissions: string | null;
}

interface ImportAccount {
  login_name: string;
  display_name: string;
  source_role_label: string;
  role_code: string;
  region_codes: string[];
  scope_global: boolean;
  is_supplier: boolean;
  pending_activation: boolean;
  suggested_enabled: boolean;
}

interface ImportBatch {
  id: string;
  source_filename: string;
  status: string;
  stage: string;
  file_hash: string;
  rows_total: number;
  rows_imported: number;
  rows_rejected: number;
  created_at: string | null;
  workbook_stored: boolean;
  ready: boolean | null;
  unknown_regions: string[];
  ignored_columns: string[];
}

interface PreflightReport {
  sku_count: number;
  region_codes: string[];
  account_count: number;
  merchant_count: number;
  contract_count: number;
  unknown_regions: string[];
  ignored_columns: string[];
  warnings: string[];
  ready: boolean;
  role_rules: RoleRule[];
}

interface UploadOut {
  batch: ImportBatch;
  report: PreflightReport | null;
  accounts: ImportAccount[];
  idempotent_hit: boolean;
}

interface PreflightOut {
  batch: ImportBatch;
  report: PreflightReport;
  accounts: ImportAccount[];
}

interface Credential {
  login_name: string;
  role_code: string;
  credential: string;
}

interface ExecuteOut {
  batch: ImportBatch;
  credentials: Credential[];
}

type Tab = "ledgers" | "import";

export function MasterData() {
  const { authenticated } = useAuth();
  const { items: toasts, push, dismiss } = useToast();
  const [tab, setTab] = useState<Tab>("ledgers");

  if (!authenticated) {
    return (
      <div className="space-y-4">
        <PageHeader title="品牌 / SKU / 片区 / 规则 / 导入" description="主数据台账与 Excel 四步导入" />
        <SessionNotice hint="主数据与导入需要真实登录后的系统管理员会话（后端按 settings 能力二次鉴权）。" />
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <PageHeader
        title="品牌 / SKU / 片区 / 规则 / 导入"
        description="台账只读（主数据只停用不删除；片区新建走「片区管理」页）；导入走「上传 → 预检 → 确认 → 执行」四步，源表密码列在解析阶段即被丢弃"
      />

      <div className="flex gap-1 rounded-md border border-slate-200 bg-white p-1">
        {(
          [
            ["ledgers", "主数据台账"],
            ["import", "Excel 导入"],
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

      {tab === "ledgers" ? <LedgersTab onToast={push} /> : <ImportTab onToast={push} />}

      <ToastStack items={toasts} onDismiss={dismiss} />
    </div>
  );
}

// ============================== 台账 ==============================

function LedgersTab({
  onToast,
}: {
  onToast: (message: string, tone?: ToastTone) => void;
}) {
  const [kind, setKind] = useState<"brands" | "regions" | "skus">("brands");

  const brands = useApiQuery<BrandRow[]>(`${BASE}/brands`);
  const regions = useApiQuery<RegionRow[]>(`${BASE}/regions`);
  const skus = useApiQuery<SkuRow[]>(`${BASE}/skus`);

  const brandColumns: DataTableColumn<BrandRow>[] = [
    { key: "code", header: "品牌编码", render: (r) => <span className="font-mono text-xs">{r.code}</span> },
    { key: "name", header: "品牌名称", render: (r) => r.name },
    {
      key: "is_demo",
      header: "数据域",
      render: (r) =>
        r.is_demo ? <StatusBadge tone="info" label="演示数据" /> : <StatusBadge tone="neutral" label="正式数据" />,
    },
    {
      key: "is_active",
      header: "状态",
      render: (r) => (
        <StatusBadge tone={r.is_active ? "success" : "neutral"} label={r.is_active ? "启用" : "已停用"} />
      ),
    },
  ];

  const regionColumns: DataTableColumn<RegionRow>[] = [
    { key: "code", header: "片区编码", render: (r) => <span className="font-mono text-xs">{r.code}</span> },
    { key: "name", header: "片区名称", render: (r) => r.name },
    {
      key: "is_active",
      header: "状态",
      render: (r) => (
        <StatusBadge tone={r.is_active ? "success" : "neutral"} label={r.is_active ? "启用" : "已停用"} />
      ),
    },
  ];

  const skuColumns: DataTableColumn<SkuRow>[] = [
    { key: "code", header: "SKU 编码", render: (r) => <span className="font-mono text-xs">{r.code}</span> },
    { key: "name", header: "品名", render: (r) => r.name },
    { key: "brand_code", header: "品牌", render: (r) => <span className="font-mono text-xs">{r.brand_code}</span> },
    { key: "category_name", header: "品类", render: (r) => r.category_name ?? "—" },
    { key: "case_spec", header: "箱规", align: "right", render: (r) => r.case_spec },
    {
      key: "capacity_ml",
      header: "容量(ml)",
      align: "right",
      render: (r) => (r.capacity_ml === null ? "—" : r.capacity_ml),
    },
    {
      key: "suggested_price_fen",
      header: "建议零售价",
      align: "right",
      render: (r) => money(r.suggested_price_fen),
    },
    {
      key: "is_active",
      header: "状态",
      render: (r) => (
        <StatusBadge tone={r.is_active ? "success" : "neutral"} label={r.is_active ? "启用" : "已停用"} />
      ),
    },
  ];

  const active =
    kind === "brands" ? brands : kind === "regions" ? regions : skus;

  return (
    <div className="space-y-3">
      <section className="space-y-3 rounded-md border border-slate-200 bg-white p-4">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h2 className="text-sm font-semibold text-slate-700">主数据台账（只读）</h2>
          <div className="flex gap-1">
            {(
              [
                ["brands", `品牌 ${brands.data?.length ?? 0}`],
                ["regions", `片区 ${regions.data?.length ?? 0}`],
                ["skus", `SKU ${skus.data?.length ?? 0}`],
              ] as const
            ).map(([key, text]) => (
              <button
                key={key}
                type="button"
                onClick={() => setKind(key)}
                className={`rounded px-3 py-1.5 text-sm ${
                  kind === key ? "bg-slate-100 font-medium text-slate-800" : "text-slate-600 hover:bg-slate-50"
                }`}
              >
                {text}
              </button>
            ))}

          </div>
        </div>

        {active.loading ? <LoadingBlock text="加载主数据…" /> : null}
        {active.error ? (
          <ErrorState message={active.error} status={active.status} onRetry={active.refetch} />
        ) : null}

        {!active.loading && !active.error ? (
          kind === "brands" ? (
            <DataTable rows={brands.data ?? []} columns={brandColumns} rowKey={(r) => r.id} emptyText="暂无品牌（导入主数据后出现）" />
          ) : kind === "regions" ? (
            <DataTable rows={regions.data ?? []} columns={regionColumns} rowKey={(r) => r.id} emptyText="暂无片区（导入主数据后出现）" />
          ) : (
            <DataTable rows={skus.data ?? []} columns={skuColumns} rowKey={(r) => r.id} emptyText="暂无 SKU（导入主数据后出现）" />
          )
        ) : null}

        {kind === "skus" ? (
          <CreateSkuPanel
            brands={brands.data ?? []}
            onCreated={() => skus.refetch()}
            onToast={onToast}
          />
        ) : null}

        <p className="text-xs text-slate-400">
          品牌 / SKU / 片区 均遵循「只停用不删除」：台账不提供物理删除与就地改写，修改请走导入流程并保留批次痕迹；
          片区新建请
          <Link to="/admin/regions" className="ml-1 text-[var(--color-brand)] hover:underline">
            前往片区管理
          </Link>
          （需片区管理能力）。
        </p>
      </section>

      <section className="space-y-2 rounded-md border border-slate-200 bg-white p-4">
        <h2 className="text-sm font-semibold text-slate-700">规则</h2>
        <p className="text-xs text-slate-500">
          导入工作簿里的「角色权限」表只做**差异预览**，不覆盖代码权限矩阵（PRD §2.2 红线）：
          角色 → 能力的映射以应用内代码常量 `core/permissions.py` 为唯一事实来源。
        </p>
        <p className="text-xs text-slate-500">
          业务规则（提成阶梯、奖励与扣减）不在主数据里配置，走绩效核算的规则版本：
          <Link to="/finance/performance" className="ml-1 text-[var(--color-brand)] hover:underline">
            绩效核算 → 规则版本
          </Link>
          （需具备绩效能力）。
        </p>
      </section>
    </div>
  );
}

// ============================== 新建单个 SKU ==============================

/** 单条新建 SKU：采购手工加行之前不必为此跑一次 Excel 导入（批量仍走四步导入）。 */
function CreateSkuPanel({
  brands,
  onCreated,
  onToast,
}: {
  brands: BrandRow[];
  onCreated: () => void;
  onToast: (message: string, tone?: ToastTone) => void;
}) {
  const [open, setOpen] = useState(false);
  const [code, setCode] = useState("");
  const [name, setName] = useState("");
  const [brandCode, setBrandCode] = useState("");
  const [capacityMl, setCapacityMl] = useState("");
  const [caseSpec, setCaseSpec] = useState("24");
  const [priceYuan, setPriceYuan] = useState("");
  const [purchaseYuan, setPurchaseYuan] = useState("");
  const [specText, setSpecText] = useState("");
  const [seriesName, setSeriesName] = useState("");

  const createMut = useApiMutation((payload: Record<string, unknown>) =>
    api.post<SkuRow>(`${BASE}/skus`, payload),
  );

  const yuanToFen = (v: string) => Math.round(Number(v || "0") * 100);

  return (
    <section className="space-y-2 rounded-md border border-blue-200 bg-blue-50 p-3">
      <div className="flex items-center justify-between">
        <span className="text-xs font-medium text-blue-800">
          新建 SKU（单条；批量请用「导入」）
        </span>
        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          className="rounded-md border border-blue-300 bg-white px-2 py-0.5 text-xs text-blue-700 hover:bg-blue-100"
        >
          {open ? "收起" : "展开"}
        </button>
      </div>

      {open ? (
        <form
          onSubmit={(e) => {
            e.preventDefault();
            createMut
              .run({
                code: code.trim(),
                name: name.trim(),
                brand_code: brandCode,
                capacity_ml: capacityMl ? Number(capacityMl) : null,
                case_spec: Number(caseSpec || "0"),
                suggested_price_fen: yuanToFen(priceYuan),
                purchase_price_fen: purchaseYuan ? yuanToFen(purchaseYuan) : null,
                spec_text: specText.trim() || null,
                series_name: seriesName.trim() || null,
              })
              .then((res) => {
                if (res) {
                  onToast(`SKU ${res.code}（${res.name}）已创建`, "success");
                  setCode("");
                  setName("");
                  setCapacityMl("");
                  setPriceYuan("");
                  setPurchaseYuan("");
                  setSpecText("");
                  setSeriesName("");
                  setOpen(false);
                  onCreated();
                } else {
                  onToast(createMut.error || "创建失败", "danger");
                }
              });
          }}
          className="space-y-2"
        >
          <div className="grid grid-cols-1 gap-2 md:grid-cols-4">
            <label className="flex flex-col gap-1 text-xs text-slate-700">
              <span>SKU 编码 *（≤32，全局唯一）</span>
              <input
                required
                maxLength={32}
                value={code}
                onChange={(e) => setCode(e.target.value)}
                placeholder="如 QS-SKU-035"
                className="rounded-md border border-slate-300 px-2 py-1 text-sm"
              />
            </label>
            <label className="flex flex-col gap-1 text-xs text-slate-700">
              <span>品名 *</span>
              <input
                required
                maxLength={128}
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="如 沁爽矿泉水"
                className="rounded-md border border-slate-300 px-2 py-1 text-sm"
              />
            </label>
            <label className="flex flex-col gap-1 text-xs text-slate-700">
              <span>品牌 *</span>
              <select
                required
                value={brandCode}
                onChange={(e) => setBrandCode(e.target.value)}
                className="rounded-md border border-slate-300 bg-white px-2 py-1 text-sm"
              >
                <option value="">请选择</option>
                {brands.map((b) => (
                  <option key={b.id} value={b.code}>
                    {b.code} · {b.name}
                  </option>
                ))}
              </select>
            </label>
            <label className="flex flex-col gap-1 text-xs text-slate-700">
              <span>容量（ml）</span>
              <input
                type="number"
                min="0"
                value={capacityMl}
                onChange={(e) => setCapacityMl(e.target.value)}
                placeholder="550"
                className="rounded-md border border-slate-300 px-2 py-1 text-sm"
              />
            </label>
            <label className="flex flex-col gap-1 text-xs text-slate-700">
              <span>箱规（瓶/箱）</span>
              <input
                type="number"
                min="0"
                step="0.01"
                value={caseSpec}
                onChange={(e) => setCaseSpec(e.target.value)}
                className="rounded-md border border-slate-300 px-2 py-1 text-sm"
              />
            </label>
            <label className="flex flex-col gap-1 text-xs text-slate-700">
              <span>建议零售价（元/瓶）</span>
              <input
                type="number"
                min="0"
                step="0.01"
                value={priceYuan}
                onChange={(e) => setPriceYuan(e.target.value)}
                placeholder="2.00"
                className="rounded-md border border-slate-300 px-2 py-1 text-sm"
              />
            </label>
            <label className="flex flex-col gap-1 text-xs text-slate-700">
              <span>参考进价（元/瓶）</span>
              <input
                type="number"
                min="0"
                step="0.01"
                value={purchaseYuan}
                onChange={(e) => setPurchaseYuan(e.target.value)}
                placeholder="1.20"
                className="rounded-md border border-slate-300 px-2 py-1 text-sm"
              />
            </label>
            <label className="flex flex-col gap-1 text-xs text-slate-700">
              <span>系列</span>
              <input
                maxLength={64}
                value={seriesName}
                onChange={(e) => setSeriesName(e.target.value)}
                placeholder="如 饮用水系列"
                className="rounded-md border border-slate-300 px-2 py-1 text-sm"
              />
            </label>
          </div>
          <label className="flex flex-col gap-1 text-xs text-slate-700">
            <span>规格原文</span>
            <input
              maxLength={64}
              value={specText}
              onChange={(e) => setSpecText(e.target.value)}
              placeholder="如 550ml/瓶×24瓶"
              className="rounded-md border border-slate-300 px-2 py-1 text-sm"
            />
          </label>
          <div className="flex gap-2">
            <button
              type="submit"
              disabled={createMut.submitting || !code.trim() || !name.trim() || !brandCode}
              className="rounded-md bg-blue-600 px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50"
            >
              {createMut.submitting ? "创建中…" : "创建 SKU"}
            </button>
            <button
              type="button"
              onClick={() => setOpen(false)}
              className="rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm text-slate-600"
            >
              取消
            </button>
          </div>
          {createMut.error ? <div className="text-xs text-rose-600">{createMut.error}</div> : null}
        </form>
      ) : null}
    </section>
  );
}

// ============================== 导入 ==============================

function ImportTab({ onToast }: { onToast: (message: string, tone?: ToastTone) => void }) {
  const [upload, setUpload] = useState<UploadOut | null>(null);
  const [decisions, setDecisions] = useState<Record<string, boolean>>({});
  const [credentials, setCredentials] = useState<Credential[] | null>(null);
  const [confirmExecute, setConfirmExecute] = useState(false);

  const batches = useApiQuery<ImportBatch[]>(`${BASE}/imports`);

  const uploadMut = useApiMutation((file: File) =>
    api.upload<UploadOut>(`${BASE}/imports`, file),
  );
  const preflightMut = useApiMutation((batchId: string) =>
    api.post<PreflightOut>(`${BASE}/imports/${batchId}/preflight`),
  );
  const confirmMut = useApiMutation((p: { batchId: string; enabled: Record<string, boolean> }) =>
    api.post<ImportBatch>(`${BASE}/imports/${p.batchId}/confirm`, {
      account_enabled: p.enabled,
    }),
  );
  const executeMut = useApiMutation((batchId: string) =>
    api.post<ExecuteOut>(`${BASE}/imports/${batchId}/execute`),
  );

  const report = upload?.report ?? null;
  const accounts = useMemo(() => upload?.accounts ?? [], [upload]);
  const batch = upload?.batch ?? null;

  function applyReport(next: PreflightOut) {
    setUpload({ batch: next.batch, report: next.report, accounts: next.accounts, idempotent_hit: false });
    setDecisions(
      Object.fromEntries(next.accounts.map((a) => [a.login_name, a.suggested_enabled])),
    );
    setCredentials(null);
  }

  const enabledCount = useMemo(
    () => accounts.filter((a) => decisions[a.login_name]).length,
    [accounts, decisions],
  );

  const accountColumns: DataTableColumn<ImportAccount>[] = [
    {
      key: "enable",
      header: "启用",
      render: (a) => (
        <input
          type="checkbox"
          checked={Boolean(decisions[a.login_name])}
          // 只有厂商不可启用（后端执行期强制其保持待激活且不发凭据）；
          // pending_activation 只是「导入后默认待激活」的展示标记，不得据此禁用勾选，
          // 否则用户工作簿（账号数据表无状态列，全量 pending_activation=True）整列无法点击。
          disabled={a.is_supplier}
          onChange={(e) =>
            setDecisions((prev) => ({ ...prev, [a.login_name]: e.target.checked }))
          }
        />
      ),
    },
    {
      key: "login_name",
      header: "登录名",
      render: (a) => <span className="font-mono text-xs">{a.login_name}</span>,
    },
    { key: "display_name", header: "姓名", render: (a) => a.display_name },
    {
      key: "role",
      header: "角色",
      render: (a) => (
        <span>
          {a.source_role_label}
          <span className="ml-1 text-xs text-slate-400">{label(a.role_code)}</span>
        </span>
      ),
    },
    {
      key: "region_codes",
      header: "片区范围",
      render: (a) => (a.scope_global ? "全部（全局）" : a.region_codes.join("、") || "—"),
    },
    {
      key: "flags",
      header: "标记",
      render: (a) => (
        <div className="flex flex-wrap gap-1">
          {a.is_supplier ? <StatusBadge tone="info" label="厂商" /> : null}
          {a.pending_activation ? <StatusBadge tone="warning" label="默认待激活" /> : null}
        </div>
      ),
    },
  ];

  const roleColumns: DataTableColumn<RoleRule>[] = [
    { key: "role_label", header: "源表角色", render: (r) => r.role_label },
    {
      key: "role_code",
      header: "映射角色",
      render: (r) => (r.role_code ? label(r.role_code) : <span className="text-amber-700">未映射（忽略）</span>),
    },
    { key: "function_permissions", header: "功能权限（源表）", render: (r) => r.function_permissions ?? "—" },
    { key: "data_permissions", header: "数据权限（源表）", render: (r) => r.data_permissions ?? "—" },
  ];

  const batchColumns: DataTableColumn<ImportBatch>[] = [
    {
      key: "created_at",
      header: "上传时间",
      render: (b) => <span className="text-xs text-slate-500">{formatDateTime(b.created_at)}</span>,
    },
    { key: "source_filename", header: "文件", render: (b) => b.source_filename },
    { key: "stage", header: "阶段", render: (b) => <StatusBadge tone="neutral" label={label(b.stage)} /> },
    {
      key: "status",
      header: "状态",
      render: (b) => <StatusBadge tone={statusTone(b.status)} label={label(b.status)} />,
    },
    {
      key: "rows",
      header: "行数（导入/总）",
      align: "right",
      render: (b) => `${b.rows_imported}/${b.rows_total}`,
    },
    {
      key: "file_hash",
      header: "文件哈希",
      render: (b) => (
        <span className="font-mono text-xs text-slate-400">{b.file_hash.slice(0, 12)}</span>
      ),
    },
    {
      key: "actions",
      header: "操作",
      render: (b) => (
        <button
          type="button"
          disabled={preflightMut.submitting || !b.workbook_stored}
          title={!b.workbook_stored ? "该批次的工作簿不在对象存储中" : undefined}
          onClick={() => {
            preflightMut.run(b.id).then((res) => {
              if (res) {
                applyReport(res);
                onToast(`批次 ${b.source_filename} 已重新预检`, "success");
              } else {
                onToast(preflightMut.error || "预检失败", "danger");
              }
            });
          }}
          className="rounded-md border border-slate-300 bg-white px-2 py-1 text-xs text-slate-700 hover:bg-slate-50 disabled:opacity-40"
        >
          重新预检
        </button>
      ),
    },
  ];

  const blocked = report !== null && !report.ready;

  return (
    <div className="space-y-3">
      {/* ---------- 第一步：上传 ---------- */}
      <section className="space-y-3 rounded-md border border-slate-200 bg-white p-4">
        <h2 className="text-sm font-semibold text-slate-700">第一步：上传工作簿（.xlsx，≤10MB）</h2>
        <form
          className="flex flex-wrap items-end gap-3"
          onSubmit={(e) => {
            e.preventDefault();
            const input = e.currentTarget.elements.namedItem("workbook");
            const file = input instanceof HTMLInputElement ? input.files?.[0] : undefined;
            if (!file) {
              onToast("请先选择工作簿文件", "warning");
              return;
            }
            uploadMut.run(file).then((res) => {
              if (!res) {
                onToast(uploadMut.error || "上传失败", "danger");
                return;
              }
              setUpload(res);
              setCredentials(null);
              if (res.idempotent_hit) {
                setDecisions({});
                onToast(
                  `该文件此前已成功导入（批次 ${res.batch.id.slice(0, 8)}），未重复处理`,
                  "warning",
                );
              } else {
                setDecisions(
                  Object.fromEntries(
                    res.accounts.map((a) => [a.login_name, a.suggested_enabled]),
                  ),
                );
                onToast("上传并预检完成", "success");
              }
              batches.refetch();
            });
          }}
        >
          <label className="flex flex-col gap-1 text-xs text-slate-600">
            <span>工作簿（.xlsx）*</span>
            <input
              name="workbook"
              type="file"
              accept=".xlsx"
              className="rounded-md border border-slate-300 bg-white px-2 py-1.5 text-sm"
            />
          </label>
          <button
            type="submit"
            disabled={uploadMut.submitting}
            className="rounded-md bg-[var(--color-brand)] px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50"
          >
            {uploadMut.submitting ? "上传中…" : "上传并预检"}
          </button>
        </form>
        <p className="text-xs text-slate-400">
          支持两种工作表结构：《品牌与账号数据》的「品牌与产品 / 账号数据 / 角色权限」，
          以及《初始化夹具》的「片区 / 主数据SKU / 主数据账号」等；另可带「商家DEMO」表，
          商家会随批次落库（负责人账号存在时一并建立责任归属）。源表若含「登录密码」列会被
          显式忽略并回报，系统不读、不存、不回显任何密码。
        </p>
      </section>

      {/* ---------- 第二步：预检 ---------- */}
      {upload ? (
        <section className="space-y-3 rounded-md border border-slate-200 bg-white p-4">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <h2 className="text-sm font-semibold text-slate-700">第二步：预检结果</h2>
            <div className="flex items-center gap-2 text-xs text-slate-500">
              <span>批次 {batch?.id.slice(0, 8)}</span>
              {batch ? <StatusBadge tone="neutral" label={label(batch.stage)} /> : null}
              {batch ? <StatusBadge tone={statusTone(batch.status)} label={label(batch.status)} /> : null}
            </div>
          </div>

          {upload.idempotent_hit ? (
            <div className="rounded-md border border-amber-200 bg-amber-50 p-3 text-xs text-amber-900">
              该文件（SHA-256 相同）此前已成功导入，直接返回既有批次，**未重复落库**；
              如需重新导入请修改文件内容后再次上传。
            </div>
          ) : null}

          {report ? (
            <>
              <div className="grid grid-cols-2 gap-3 text-sm md:grid-cols-6">
                <div>
                  <div className="text-xs text-slate-500">SKU 行数</div>
                  <div className="text-slate-800">{report.sku_count}</div>
                </div>
                <div>
                  <div className="text-xs text-slate-500">片区</div>
                  <div className="text-slate-800">{report.region_codes.join("、") || "—"}</div>
                </div>
                <div>
                  <div className="text-xs text-slate-500">账号行数</div>
                  <div className="text-slate-800">{report.account_count}</div>
                </div>
                <div>
                  <div className="text-xs text-slate-500">商家行数</div>
                  <div className="text-slate-800">{report.merchant_count}</div>
                </div>
                <div>
                  <div className="text-xs text-slate-500">合同行数</div>
                  <div className="text-slate-800">{report.contract_count}</div>
                </div>
                <div>
                  <div className="text-xs text-slate-500">可执行</div>
                  <div className="text-slate-800">
                    {report.ready ? (
                      <StatusBadge tone="success" label="通过" />
                    ) : (
                      <StatusBadge tone="danger" label="存在未知片区" />
                    )}
                  </div>
                </div>
              </div>

              {report.ignored_columns.length > 0 ? (
                <div className="rounded-md border border-sky-200 bg-sky-50 p-3 text-xs text-sky-900">
                  已忽略列：{report.ignored_columns.join("、")}
                  （源表中的密码类列在解析阶段即被丢弃，不入库、不回显）
                </div>
              ) : null}

              {report.unknown_regions.length > 0 ? (
                <div className="rounded-md border border-rose-200 bg-rose-50 p-3 text-xs text-rose-800">
                  未知片区：{report.unknown_regions.join("、")}
                  —— 账号引用了工作簿「片区」表之外的片区，必须先修正后才能确认与执行。
                </div>
              ) : null}

              {report.warnings.length > 0 ? (
                <div className="rounded-md border border-amber-200 bg-amber-50 p-3 text-xs text-amber-900">
                  {report.warnings.map((w) => (
                    <div key={w}>· {w}</div>
                  ))}
                </div>
              ) : null}

              {report.role_rules.length > 0 ? (
                <div className="space-y-2">
                  <div className="text-xs font-medium text-slate-600">
                    角色权限差异预览（不覆盖代码权限矩阵）
                  </div>
                  <DataTable
                    rows={report.role_rules}
                    columns={roleColumns}
                    rowKey={(r) => r.role_label}
                    emptyText="工作簿未提供角色权限表"
                  />
                </div>
              ) : null}
            </>
          ) : null}
        </section>
      ) : null}

      {/* ---------- 第三步：确认 ---------- */}
      {upload && report && !upload.idempotent_hit ? (
        <section className="space-y-3 rounded-md border border-slate-200 bg-white p-4">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <h2 className="text-sm font-semibold text-slate-700">
              第三步：确认账号（默认不启用，已选 {enabledCount} / {accounts.length}）
            </h2>
            <div className="flex gap-2">
              <button
                type="button"
                disabled={confirmMut.submitting || blocked}
                onClick={() => {
                  if (!batch) return;
                  confirmMut
                    .run({ batchId: batch.id, enabled: decisions })
                    .then((res) => {
                      if (res) {
                        setUpload({ ...upload, batch: res });
                        onToast("账号决定已记录（仅布尔值，未接收任何密码）", "success");
                      } else {
                        onToast(confirmMut.error || "确认失败", "danger");
                      }
                    });
                }}
                className="rounded-md bg-[var(--color-brand)] px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50"
              >
                {confirmMut.submitting ? "提交中…" : "确认账号决定"}
              </button>
            </div>
          </div>

          <DataTable
            rows={accounts}
            columns={accountColumns}
            rowKey={(a) => a.login_name}
            emptyText="工作簿未包含账号行"
          />

          <p className="text-xs text-slate-400">
            厂商账号一律保持待激活且不签发凭据；被勾选启用的账号会在执行时一次性签发激活凭据。
          </p>
        </section>
      ) : null}

      {/* ---------- 第四步：执行 ---------- */}
      {upload && report && !upload.idempotent_hit ? (
        <section className="space-y-3 rounded-md border border-slate-200 bg-white p-4">
          <h2 className="text-sm font-semibold text-slate-700">第四步：执行（单事务，全有或全无）</h2>
          <div className="flex flex-wrap items-center gap-3">
            <button
              type="button"
              disabled={executeMut.submitting || blocked || batch?.stage !== "CONFIRMED"}
              title={
                batch?.stage !== "CONFIRMED"
                  ? "请先完成第三步「确认账号决定」"
                  : blocked
                    ? "存在未知片区，无法执行"
                    : undefined
              }
              onClick={() => setConfirmExecute(true)}
              className="rounded-md bg-[var(--color-brand)] px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50"
            >
              {executeMut.submitting ? "执行中…" : "执行导入"}
            </button>
            <span className="text-xs text-slate-400">
              批次执行后不可重复执行；任何错误都会整批回滚（已执行批次无法回退到上一阶段）。
            </span>
          </div>

          {credentials ? (
            <div className="space-y-2 rounded-md border border-amber-300 bg-amber-50 p-3">
              <div className="text-sm font-semibold text-amber-900">
                执行完成 · 一次性激活凭据（仅展示一次，共 {credentials.length} 条）
              </div>
              {credentials.length === 0 ? (
                <div className="text-xs text-amber-800">
                  本次没有签发凭据（可能所有账号均未勾选启用，或该批次已幂等落库）。
                </div>
              ) : (
                <>
                  <div className="space-y-1">
                    {credentials.map((c) => (
                      <div key={c.login_name} className="flex flex-wrap items-center gap-2 text-xs">
                        <span className="font-mono">{c.login_name}</span>
                        <span className="text-amber-700">{label(c.role_code)}</span>
                        <code className="max-w-full flex-1 overflow-x-auto rounded border border-amber-300 bg-white px-2 py-1 font-mono break-all">
                          {c.credential}
                        </code>
                      </div>
                    ))}
                  </div>
                  <button
                    type="button"
                    onClick={async () => {
                      try {
                        await navigator.clipboard.writeText(
                          credentials.map((c) => `${c.login_name}\t${c.credential}`).join("\n"),
                        );
                        onToast("凭据已复制（登录名 + 凭据）", "success");
                      } catch {
                        onToast("复制失败，请手动选中后复制", "warning");
                      }
                    }}
                    className="rounded-md bg-amber-600 px-3 py-1.5 text-sm font-medium text-white"
                  >
                    复制全部
                  </button>
                </>
              )}
              <div className="text-xs text-amber-800">
                关闭或刷新本页后无法再次获取；需要重发请到「用户与账号管理」重新激活该账号。
              </div>
            </div>
          ) : null}
        </section>
      ) : null}

      {/* ---------- 批次历史 ---------- */}
      <section className="space-y-3 rounded-md border border-slate-200 bg-white p-4">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h2 className="text-sm font-semibold text-slate-700">导入批次（最近 50 个）</h2>
          <button
            type="button"
            onClick={batches.refetch}
            className="rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm text-slate-600 hover:bg-slate-50"
          >
            刷新
          </button>
        </div>

        {batches.loading ? <LoadingBlock text="加载批次…" /> : null}
        {batches.error ? (
          <ErrorState message={batches.error} status={batches.status} onRetry={batches.refetch} />
        ) : null}
        {!batches.loading && !batches.error ? (
          <DataTable
            rows={batches.data ?? []}
            columns={batchColumns}
            rowKey={(b) => b.id}
            emptyText="暂无导入批次"
          />
        ) : null}
      </section>

      <ConfirmDialog
        open={confirmExecute}
        title="确认执行主数据导入？"
        description="执行在单一事务内完成（全有或全无），并会一次性签发被启用账号的激活凭据；执行后不可重复执行。"
        confirmText="执行导入"
        onClose={() => setConfirmExecute(false)}
        onConfirm={() => {
          setConfirmExecute(false);
          if (!batch) return;
          executeMut.run(batch.id).then((res) => {
            if (res) {
              setUpload((prev) => (prev ? { ...prev, batch: res.batch } : prev));
              setCredentials(res.credentials);
              onToast("主数据导入执行完成", "success");
              batches.refetch();
            } else {
              onToast(executeMut.error || "执行失败", "danger");
            }
          });
        }}
      />
    </div>
  );
}
