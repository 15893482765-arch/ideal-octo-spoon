/** 巡访与打卡（T11 前端联调 / PRD §4.6 / §4.8）。
 *
 * 接入后端：
 * - `GET /api/visits/tasks?on_date=`（责任人工作台：业务员看自己、经理/督导看本片区）；
 * - `GET /api/visits/tasks/{id}`（任务 + 商家导航点）；
 * - `POST /api/visits/photos`（先上传后绑定，拿 file_id）；
 * - `POST /api/visits/tasks/{id}/submit`（打卡提交）；
 * - `GET /api/visits/records/{id}`、`POST .../judge`、`POST .../revisions`。
 *
 * 红线由服务端守：距离用 PostGIS 重算（前端上报不作数）、仅任务责任人可提交、
 * 一任务至多一条正式记录、原始证据不可覆盖（补充只追加修订）、有效性由
 * VisitValidityService 唯一裁决。页面只做展示与受控交互。
 */
import { useMemo, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import { api } from "../lib/api";
import { useAuth } from "../lib/auth";
import { formatDate, formatDateTime, label, statusTone, todayInTz } from "../lib/format";
import { locateOnce, type LocateFailure } from "../lib/geolocation";
import { useApiMutation, useApiQuery } from "../lib/useApi";
import { useToast } from "../lib/useToast";
import { ErrorState, LoadingBlock, SessionNotice } from "../components/AsyncState";
import { DataTable, type DataTableColumn } from "../components/DataTable";
import { EmptyState } from "../components/EmptyState";
import { FilterBar } from "../components/FilterBar";
import { MetricCard } from "../components/MetricCard";
import { PageHeader } from "../components/PageHeader";
import { StatusBadge } from "../components/StatusBadge";
import { ToastStack } from "../components/Toast";

/** 受控枚举（与后端 VisitSource / VisitFreezerStatus / VisitConclusion 对齐）。 */
const SOURCE_OPTIONS = ["WEEKLY_SCHEDULE", "AD_HOC", "RECTIFICATION", "SUPERVISION"] as const;
const FREEZER_OPTIONS = ["NORMAL", "ABSENT", "POWERED_OFF", "DIRTY", "DAMAGED", "OTHER"] as const;
const CONCLUSION_OPTIONS = [
  "NORMAL",
  "NEEDS_REPLENISHMENT",
  "NEEDS_RECTIFICATION",
  "RISK_MERCHANT",
  "PROPOSE_TERMINATION",
] as const;
const PHOTO_KIND_OPTIONS = ["STOREFRONT", "FREEZER", "OTHER"] as const;

const JUDGE_ROLES = new Set(["REGIONAL_MANAGER", "SYSTEM_ADMIN"]);
const SUPERVISION_ROLES = new Set(["SUPERVISOR", "REGIONAL_MANAGER"]);

interface VisitTaskRow {
  id: string;
  merchant_id: string;
  assigned_user_id: string;
  planned_date: string;
  window_start_at: string | null;
  window_end_at: string | null;
  priority: number;
  source: string;
  status: string;
  reason: string | null;
}

interface MerchantPoint {
  id: string;
  code: string;
  name: string;
  lng: number | null;
  lat: number | null;
}

interface VisitTaskDetail {
  task: VisitTaskRow;
  merchant: MerchantPoint;
}

/** 补货/销量行可选 SKU（同品牌、同数据范围）。 */
interface SkuOption {
  id: string;
  code: string;
  name: string;
}

interface ReplenishmentRow {
  sku_id: string;
  boxes: string;
  expected: string;
}

interface VisitPhoto {
  id: string;
  file_id: string;
  photo_kind: string;
}

interface VisitRevision {
  id: string;
  author_id: string;
  supplement: string;
  created_at: string;
}

interface VisitSalesLine {
  id: string;
  sku_id: string;
  reported_boxes: string;
  feedback: string | null;
  out_of_stock: boolean;
}

interface VisitReplenishmentLine {
  order_id: string;
  code: string;
  sku_id: string;
  requested_boxes: string;
  expected_delivery_date: string | null;
  status: string;
}

interface VisitRecord {
  id: string;
  visit_task_id: string;
  merchant_id: string;
  user_id: string;
  source: string;
  checkin_at: string | null;
  checkout_at: string | null;
  device_checkin_at: string | null;
  lng: number | null;
  lat: number | null;
  accuracy_meters: number | null;
  distance_meters: number | null;
  display_pct: number | null;
  display_threshold_pct: number | null;
  display_met: boolean | null;
  freezer_status: string | null;
  device_issue: string | null;
  conclusion: string | null;
  photo_count: number;
  note: string | null;
  is_official: boolean;
  status: string;
  validity: string;
  judged_by_id: string | null;
  judged_at: string | null;
  judge_reason: string | null;
  submitted_at: string;
  photos: VisitPhoto[];
  revisions: VisitRevision[];
  sales_lines: VisitSalesLine[];
  replenishment_lines: VisitReplenishmentLine[];
}

interface MerchantOption {
  id: string;
  code: string;
  name: string;
}

function hhmm(value: string | null): string {
  if (!value) return "—";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return "—";
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

function meters(value: number | null): string {
  return value === null || value === undefined ? "—" : `${Math.round(value)} m`;
}

/** 业务日一律按 Asia/Shanghai：用设备本地时区会让「今天」错位一天（凌晨/跨时区）。 */
function todayIso(): string {
  return todayInTz();
}

export function Visits() {
  const { authenticated, user, hasAbility } = useAuth();
  const { items: toasts, push, dismiss } = useToast();
  const navigate = useNavigate();

  const [dateInput, setDateInput] = useState(todayIso());
  const [appliedDate, setAppliedDate] = useState(todayIso());
  const [activeTaskId, setActiveTaskId] = useState<string | null>(null);

  const canVisit = hasAbility("visit");

  const tasks = useApiQuery<VisitTaskRow[]>(
    "/visits/tasks",
    { on_date: appliedDate || undefined },
    { enabled: authenticated && canVisit },
  );
  const merchants = useApiQuery<{ items: MerchantOption[] }>(
    "/merchants",
    { page: 1, page_size: 200 },
    { enabled: authenticated && canVisit },
  );

  const merchantMap = useMemo(() => {
    const map = new Map<string, MerchantOption>();
    for (const m of merchants.data?.items ?? []) map.set(m.id, m);
    return map;
  }, [merchants.data]);

  const rows = tasks.data ?? [];
  const completed = rows.filter((t) => t.status === "COMPLETED").length;

  const columns: DataTableColumn<VisitTaskRow>[] = [
    {
      key: "merchant",
      header: "商家",
      render: (t) => {
        const m = merchantMap.get(t.merchant_id);
        return (
          <button
            type="button"
            onClick={() => setActiveTaskId(t.id === activeTaskId ? null : t.id)}
            className="text-left text-xs font-medium text-[var(--color-brand)] hover:underline"
          >
            {m ? `${m.code} · ${m.name}` : `${t.merchant_id.slice(0, 8)}…`}
          </button>
        );
      },
    },
    {
      key: "planned_date",
      header: "计划日期",
      render: (t) => <span className="text-xs text-slate-600">{formatDate(t.planned_date)}</span>,
    },
    {
      key: "window",
      header: "时间窗",
      render: (t) => (
        <span className="text-xs text-slate-600">
          {hhmm(t.window_start_at)}–{hhmm(t.window_end_at)}
        </span>
      ),
    },
    {
      key: "source",
      header: "来源",
      render: (t) => <StatusBadge tone="neutral" label={label(t.source)} />,
    },
    { key: "priority", header: "优先级", align: "right", render: (t) => String(t.priority) },
    {
      key: "status",
      header: "状态",
      render: (t) => <StatusBadge tone={statusTone(t.status)} label={label(t.status)} />,
    },
    { key: "reason", header: "事由", render: (t) => t.reason ?? "—" },
  ];

  if (!authenticated) {
    return (
      <div className="space-y-4">
        <PageHeader title="巡访与打卡" description="巡访任务、定位、照片与有效性" />
        <SessionNotice hint="当前是开发注入账号，请从登录页用真实账号登录后再查看业务数据。" />
      </div>
    );
  }

  if (!canVisit) {
    return (
      <div className="space-y-4">
        <PageHeader title="巡访与打卡" />
        <EmptyState title="当前角色无巡访权限" description="巡访模块仅对业务员/督导/经理/管理员开放。" />
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <PageHeader
        title="巡访与打卡"
        description="任务 → 到店打卡 → 有效性裁决 → 经理判定；距离由服务端重算，照片只增不改"
      />

      <div className="grid grid-cols-1 gap-3 md:grid-cols-3">
        <MetricCard label="当期任务" value={rows.length} unit="条" />
        <MetricCard label="已完成打卡" value={completed} unit="条" />
        <MetricCard label="待打卡" value={Math.max(0, rows.length - completed)} unit="条" />
      </div>

      <FilterBar
        onApply={() => {
          setAppliedDate(dateInput);
          setActiveTaskId(null);
        }}
        onReset={() => {
          const t = todayIso();
          setDateInput(t);
          setAppliedDate(t);
          setActiveTaskId(null);
        }}
      >
        <Field label="计划日期">
          <input
            type="date"
            value={dateInput}
            onChange={(e) => setDateInput(e.target.value)}
            className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
          />
        </Field>
        <p className="text-xs text-slate-500">
          业务员只看自己的任务，经理/督导看本片区任务（行级范围由服务端注入）。
        </p>
      </FilterBar>

      {tasks.loading ? <LoadingBlock text="加载巡访任务…" /> : null}
      {tasks.error ? (
        <ErrorState message={tasks.error} status={tasks.status} onRetry={tasks.refetch} />
      ) : null}

      {!tasks.loading && !tasks.error ? (
        <>
          <DataTable
            rows={rows}
            columns={columns}
            rowKey={(t) => t.id}
            emptyText="该日期没有巡访任务（可用下方「新建任务」排班）"
          />
          {activeTaskId ? (
            <TaskPanel
              taskId={activeTaskId}
              currentUserId={user?.id ?? ""}
              onSubmitted={(recordId) => {
                push("打卡已提交", "success");
                tasks.refetch();
                navigate(`/visits/${recordId}`);
              }}
              onError={(message) => push(message, "danger")}
            />
          ) : null}
          <CreateTaskForm
            merchants={merchants.data?.items ?? []}
            canSupervise={SUPERVISION_ROLES.has(user?.role ?? "")}
            defaultDate={appliedDate || todayIso()}
            onCreated={() => {
              push("巡访任务已创建", "success");
              tasks.refetch();
            }}
          />
        </>
      ) : null}

      <ToastStack items={toasts} onDismiss={dismiss} />
    </div>
  );
}

/** 巡访记录详情路由（`/visits/:id`）：原始证据不可覆盖，补充只追加修订。 */
export function VisitRecordPage() {
  const { id } = useParams<{ id: string }>();
  const { authenticated, user } = useAuth();
  const { items: toasts, push, dismiss } = useToast();

  const record = useApiQuery<VisitRecord>(`/visits/records/${id}`, undefined, {
    enabled: authenticated && Boolean(id),
  });

  const judgeMut = useApiMutation((payload: { recognize: boolean; reason: string }) =>
    api.post(`/visits/records/${id}/judge`, payload),
  );
  const revisionMut = useApiMutation((supplement: string) =>
    api.post(`/visits/records/${id}/revisions`, { supplement }),
  );

  const [reason, setReason] = useState("");
  const [supplement, setSupplement] = useState("");
  const [recognize, setRecognize] = useState(true);

  if (!id) {
    return (
      <div className="space-y-4">
        <PageHeader title="巡访详情" />
        <EmptyState title="缺少记录 ID" description="请从巡访任务列表进入详情。" />
      </div>
    );
  }

  if (!authenticated) {
    return <SessionNotice hint="巡访详情需要真实登录后的后端会话。" />;
  }

  const data = record.data ?? null;
  const canJudge = JUDGE_ROLES.has(user?.role ?? "");

  return (
    <div className="space-y-4">
      <PageHeader
        title="巡访详情"
        description="原始定位/到离店时间/照片一经提交不可覆盖，补充说明只追加"
        actions={
          <Link
            to="/visits"
            className="rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm text-slate-600 hover:bg-slate-50"
          >
            返回巡访列表
          </Link>
        }
      />

      {record.loading ? <LoadingBlock text="加载巡访记录…" /> : null}
      {record.error ? (
        <ErrorState message={record.error} status={record.status} onRetry={record.refetch} />
      ) : null}

      {data && !record.loading && !record.error ? (
        <section className="space-y-4 rounded-md border border-slate-200 bg-white p-4">
          <div className="grid grid-cols-2 gap-3 text-sm md:grid-cols-4">
            <Info
              label="有效性"
              value={<StatusBadge tone={statusTone(data.validity)} label={label(data.validity)} />}
            />
            <Info
              label="记录状态"
              value={<StatusBadge tone={statusTone(data.status)} label={label(data.status)} />}
            />
            <Info label="到店时间" value={formatDateTime(data.checkin_at)} />
            <Info label="离店时间" value={formatDateTime(data.checkout_at)} />
            <Info label="设备时间" value={formatDateTime(data.device_checkin_at)} />
            <Info label="服务端距离" value={meters(data.distance_meters)} />
            <Info label="定位精度" value={data.accuracy_meters === null ? "—" : `${data.accuracy_meters} m`} />
            <Info
              label="定位坐标"
              value={
                data.lng === null || data.lat === null
                  ? "—"
                  : `${data.lng.toFixed(5)}, ${data.lat.toFixed(5)}`
              }
            />
            <Info
              label="本品牌陈列"
              value={
                data.display_pct === null
                  ? "—"
                  : `${data.display_pct}%（阈值 ${data.display_threshold_pct ?? "—"}%）`
              }
            />
            <Info
              label="陈列是否达标"
              value={data.display_met === null ? "—" : data.display_met ? "达标" : "未达标"}
            />
            <Info label="冰柜状态" value={label(data.freezer_status)} />
            <Info label="当日结论" value={label(data.conclusion)} />
            <Info label="照片数" value={String(data.photo_count)} />
            <Info label="来源" value={label(data.source)} />
            <Info label="是否正式记录" value={data.is_official ? "是" : "否"} />
            <Info label="提交时间" value={formatDateTime(data.submitted_at)} />
            <Info label="设备异常" value={data.device_issue ?? "—"} />
            <Info label="备注" value={data.note ?? "—"} />
          </div>

          <div>
            <div className="text-xs font-medium text-slate-600">现场照片（file_id 绑定，只增不改）</div>
            <div className="mt-2">
              <DataTable
                rows={data.photos}
                columns={[
                  { key: "kind", header: "照片类型", render: (p) => label(p.photo_kind) },
                  {
                    key: "file_id",
                    header: "文件 ID",
                    render: (p) => <span className="font-mono text-xs">{p.file_id}</span>,
                  },
                ]}
                rowKey={(p) => p.id}
                emptyText="无照片（缺照片的打卡会被判为异常待判定）"
              />
            </div>
          </div>

          <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
            <div>
              <div className="text-xs font-medium text-slate-600">上报销量行（确认前不进指标）</div>
              <div className="mt-2">
                <DataTable
                  rows={data.sales_lines}
                  columns={[
                    { key: "box", header: "上报箱数", align: "right", render: (s) => s.reported_boxes },
                    { key: "oos", header: "缺货", render: (s) => (s.out_of_stock ? "是" : "否") },
                    { key: "feedback", header: "商家反馈", render: (s) => s.feedback ?? "—" },
                  ]}
                  rowKey={(s) => s.id}
                  emptyText="无上报销量"
                />
              </div>
            </div>
            <div>
              <div className="text-xs font-medium text-slate-600">提报补货（同事务生成草稿）</div>
              <div className="mt-2">
                <DataTable
                  rows={data.replenishment_lines}
                  columns={[
                    {
                      key: "code",
                      header: "补货单",
                      render: (r) => <span className="font-mono text-xs">{r.code}</span>,
                    },
                    { key: "boxes", header: "需求箱数", align: "right", render: (r) => r.requested_boxes },
                    {
                      key: "eta",
                      header: "期望到货",
                      render: (r) => (
                        <span className="text-xs text-slate-600">
                          {formatDate(r.expected_delivery_date)}
                        </span>
                      ),
                    },
                    {
                      key: "status",
                      header: "状态",
                      render: (r) => <StatusBadge tone={statusTone(r.status)} label={label(r.status)} />,
                    },
                  ]}
                  rowKey={(r) => r.order_id}
                  emptyText="无补货提报"
                />
              </div>
            </div>
          </div>

          <div>
            <div className="text-xs font-medium text-slate-600">
              补充说明（{data.revisions.length} 条，追加不覆盖原始证据）
            </div>
            <div className="mt-2 space-y-2">
              {data.revisions.length === 0 ? (
                <p className="text-xs text-slate-400">暂无补充说明</p>
              ) : (
                data.revisions.map((rev) => (
                  <div key={rev.id} className="rounded-md border border-slate-200 bg-slate-50 p-2">
                    <div className="text-xs text-slate-500">{formatDateTime(rev.created_at)}</div>
                    <div className="mt-1 text-sm text-slate-800">{rev.supplement}</div>
                  </div>
                ))
              )}
              <form
                onSubmit={(e) => {
                  e.preventDefault();
                  revisionMut.run(supplement.trim()).then((res) => {
                    if (res !== null) {
                      push("补充说明已追加", "success");
                      setSupplement("");
                      record.refetch();
                    } else {
                      push(revisionMut.error || "追加失败", "danger");
                    }
                  });
                }}
                className="space-y-2"
              >
                <Field label="补充说明 *">
                  <input
                    required
                    value={supplement}
                    onChange={(e) => setSupplement(e.target.value)}
                    className="w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm"
                  />
                </Field>
                <button
                  type="submit"
                  disabled={revisionMut.submitting}
                  className="rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm text-slate-700 hover:bg-slate-100 disabled:opacity-50"
                >
                  {revisionMut.submitting ? "提交中…" : "追加补充说明"}
                </button>
              </form>
            </div>
          </div>

          {data.status === "ABNORMAL_PENDING" ? (
            canJudge ? (
              <form
                onSubmit={(e) => {
                  e.preventDefault();
                  judgeMut.run({ recognize, reason: reason.trim() }).then((res) => {
                    if (res !== null) {
                      push(recognize ? "已认可为有效巡访" : "已判定异常不计", "success");
                      setReason("");
                      record.refetch();
                    } else {
                      push(judgeMut.error || "判定失败", "danger");
                    }
                  });
                }}
                className="space-y-2 rounded-md border border-amber-200 bg-amber-50 p-3"
              >
                <div className="text-xs font-medium text-amber-900">
                  经理判定：定位超 200 米或缺照片等异常巡访，需认可计有效或判异常不计
                </div>
                <Field label="判定原因 *">
                  <input
                    required
                    value={reason}
                    onChange={(e) => setReason(e.target.value)}
                    className="w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm"
                  />
                </Field>
                <div className="flex gap-2">
                  <button
                    type="submit"
                    onClick={() => setRecognize(true)}
                    disabled={judgeMut.submitting}
                    className="rounded-md bg-[var(--color-brand)] px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50"
                  >
                    认可计有效
                  </button>
                  <button
                    type="submit"
                    onClick={() => setRecognize(false)}
                    disabled={judgeMut.submitting}
                    className="rounded-md border border-rose-300 bg-white px-3 py-1.5 text-sm text-rose-700 hover:bg-rose-50 disabled:opacity-50"
                  >
                    判异常不计
                  </button>
                </div>
              </form>
            ) : (
              <p className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-900">
                该巡访为「异常待判定」，等待片区经理认可或判异常（仅片区经理/系统管理员可判定）。
              </p>
            )
          ) : null}

          {data.status === "ABNORMAL" || data.status === "REJECTED" ? (
            <p className="rounded-md border border-rose-200 bg-rose-50 px-3 py-2 text-xs text-rose-700">
              该巡访已判定为不计有效（有效性：{label(data.validity)}），原始证据与流水保留供追溯。
            </p>
          ) : null}
        </section>
      ) : null}

      {!data && !record.loading && !record.error ? (
        <EmptyState title="未找到巡访记录" description="可能已超出你的行级可见范围。" />
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

function Info({ label: text, value }: { label: string; value: React.ReactNode }) {
  return (
    <div>
      <div className="text-xs text-slate-500">{text}</div>
      <div className="mt-0.5 text-slate-800">{value}</div>
    </div>
  );
}

/** 任务详情 + 打卡提交（仅任务责任人可提交，服务端二次校验）。 */
function TaskPanel({
  taskId,
  currentUserId,
  onSubmitted,
  onError,
}: {
  taskId: string;
  currentUserId: string;
  onSubmitted: (recordId: string) => void;
  onError: (message: string) => void;
}) {
  const detail = useApiQuery<VisitTaskDetail>(`/visits/tasks/${taskId}`, undefined, {
    enabled: Boolean(taskId),
  });
  const skus = useApiQuery<SkuOption[]>(
    "/visits/skus",
    { merchant_id: detail.data?.task.merchant_id },
    { enabled: Boolean(detail.data?.task.merchant_id) },
  );

  const uploadMut = useApiMutation((file: File) =>
    api.upload<{ file_id: string }>("/visits/photos", file),
  );
  const submitMut = useApiMutation((payload: Record<string, unknown>) =>
    api.post<VisitRecord>(`/visits/tasks/${taskId}/submit`, payload),
  );

  const [lng, setLng] = useState("");
  const [lat, setLat] = useState("");
  const [accuracy, setAccuracy] = useState("");
  // 定位采集状态：失败时保留根因与建议，避免只弹一句“定位失败”
  const [locating, setLocating] = useState(false);
  const [geoIssue, setGeoIssue] = useState<LocateFailure | null>(null);
  const [displayPct, setDisplayPct] = useState("");
  const [freezerStatus, setFreezerStatus] = useState<string>("NORMAL");
  const [conclusion, setConclusion] = useState<string>("NORMAL");
  const [note, setNote] = useState("");
  const [deviceIssue, setDeviceIssue] = useState("");
  const [photos, setPhotos] = useState<{ file_id: string; photo_kind: string }[]>([]);
  // 补货需求行：结论=需补货时至少一行，提交后同事务生成补货草稿（DRAFT）
  const [replRows, setReplRows] = useState<ReplenishmentRow[]>([]);
  const skuList = skus.data ?? [];
  // 幂等键在面板挂载时固定：重复点击不会产生第二条正式记录
  const [idempotencyKey] = useState(() =>
    typeof crypto !== "undefined" && "randomUUID" in crypto
      ? crypto.randomUUID()
      : `visit-${Date.now()}`,
  );

  const task = detail.data?.task ?? null;
  const merchant = detail.data?.merchant ?? null;
  const isAssignee = Boolean(task) && task?.assigned_user_id === currentUserId;

  async function captureLocation() {
    setLocating(true);
    setGeoIssue(null);
    const result = await locateOnce();
    setLocating(false);
    if (result.ok) {
      setLng(String(result.data.lng));
      setLat(String(result.data.lat));
      setAccuracy(String(result.data.accuracy));
      return;
    }
    // 精确定位失败：给出根因与处置建议（HTTPS/权限/信号/超时各不相同）
    setGeoIssue(result.failure);
    onError(`${result.failure.message}：${result.failure.hint}`);
  }

  async function pickPhoto(file: File, photoKind: string) {
    const res = await uploadMut.run(file);
    if (res && typeof res === "object" && "file_id" in res) {
      setPhotos((prev) => [...prev, { file_id: String(res.file_id), photo_kind: photoKind }]);
    } else {
      onError(uploadMut.error || "照片上传失败");
    }
  }

  return (
    <section className="space-y-3 rounded-md border border-slate-200 bg-white p-4">
      <h2 className="text-sm font-semibold text-slate-700">任务详情与到店打卡</h2>

      {detail.loading ? <LoadingBlock text="加载任务详情…" /> : null}
      {detail.error ? (
        <ErrorState message={detail.error} status={detail.status} onRetry={detail.refetch} />
      ) : null}

      {task && !detail.loading && !detail.error ? (
        <>
          <div className="grid grid-cols-2 gap-3 text-sm md:grid-cols-4">
            <Info label="商家" value={merchant ? `${merchant.code} · ${merchant.name}` : "—"} />
            <Info
              label="导航点"
              value={
                merchant?.lng === null || merchant?.lat === null || !merchant
                  ? "未维护坐标"
                  : `${merchant.lng.toFixed(5)}, ${merchant.lat.toFixed(5)}`
              }
            />
            <Info label="计划日期" value={formatDate(task.planned_date)} />
            <Info label="时间窗" value={`${hhmm(task.window_start_at)}–${hhmm(task.window_end_at)}`} />
            <Info label="来源" value={label(task.source)} />
            <Info label="优先级" value={String(task.priority)} />
            <Info
              label="状态"
              value={<StatusBadge tone={statusTone(task.status)} label={label(task.status)} />}
            />
            <Info label="事由" value={task.reason ?? "—"} />
          </div>

          {task.status === "COMPLETED" ? (
            <p className="rounded-md border border-emerald-200 bg-emerald-50 px-3 py-2 text-xs text-emerald-800">
              该任务已提交正式打卡记录（一个任务至多一条，不可重复提交）。可在记录详情中查看
              原始证据与补充说明。
            </p>
          ) : !isAssignee ? (
            <p className="rounded-md border border-slate-200 bg-slate-50 px-3 py-2 text-xs text-slate-600">
              只有该任务的负责人可以提交打卡（当前账号不是责任人）。
            </p>
          ) : (
            <form
              onSubmit={(e) => {
                e.preventDefault();
                const lngNum = Number(lng);
                const latNum = Number(lat);
                if (Number.isNaN(lngNum) || Number.isNaN(latNum)) {
                  onError("请填写或获取定位经纬度");
                  return;
                }
                // 补货行：填了 SKU 就必须填 >0 的箱数；结论=需补货时至少一行
                const badRow = replRows.some(
                  (r) => r.sku_id !== "" && !(Number(r.boxes) > 0),
                );
                if (badRow) {
                  onError("补货需求的需求箱数必须大于 0");
                  return;
                }
                const replenishmentLines = replRows
                  .filter((r) => r.sku_id !== "" && Number(r.boxes) > 0)
                  .map((r) => ({
                    sku_id: r.sku_id,
                    requested_boxes: Number(r.boxes),
                    expected_delivery_date: r.expected || undefined,
                  }));
                if (
                  conclusion === "NEEDS_REPLENISHMENT"
                  && replenishmentLines.length === 0
                ) {
                  onError("当日结论为「需补货」：请在补货需求里选择 SKU 并填写需求箱数");
                  return;
                }
                submitMut
                  .run({
                    lng: lngNum,
                    lat: latNum,
                    accuracy_meters: accuracy === "" ? undefined : Number(accuracy),
                    display_pct: displayPct === "" ? undefined : Number(displayPct),
                    freezer_status: freezerStatus,
                    conclusion,
                    note: note || undefined,
                    device_issue: deviceIssue || undefined,
                    photos,
                    replenishment_lines: replenishmentLines,
                    idempotency_key: idempotencyKey,
                  })
                  .then((res) => {
                    if (res && typeof res === "object" && "id" in res) {
                      onSubmitted(String(res.id));
                    } else {
                      onError(submitMut.error || "打卡提交失败");
                    }
                  });
              }}
              className="space-y-3 rounded-md border border-slate-200 bg-slate-50 p-3"
            >
              <div className="flex flex-wrap items-end gap-3">
                <Field label="经度 *">
                  <input
                    required
                    value={lng}
                    onChange={(e) => setLng(e.target.value)}
                    className="w-36 rounded-md border border-slate-300 px-2 py-1.5 text-sm"
                  />
                </Field>
                <Field label="纬度 *">
                  <input
                    required
                    value={lat}
                    onChange={(e) => setLat(e.target.value)}
                    className="w-36 rounded-md border border-slate-300 px-2 py-1.5 text-sm"
                  />
                </Field>
                <Field label="定位精度（米）">
                  <input
                    value={accuracy}
                    onChange={(e) => setAccuracy(e.target.value)}
                    className="w-28 rounded-md border border-slate-300 px-2 py-1.5 text-sm"
                  />
                </Field>
                <button
                  type="button"
                  onClick={captureLocation}
                  disabled={locating}
                  className="rounded-md border border-[var(--color-brand)] bg-white px-3 py-1.5 text-sm text-[var(--color-brand)] hover:bg-teal-50 disabled:opacity-50"
                >
                  {locating ? "定位中…" : "获取当前定位"}
                </button>
              </div>

              {geoIssue ? (
                <div className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-900">
                  <div className="font-medium">{geoIssue.message}</div>
                  <div className="mt-0.5">{geoIssue.hint}</div>
                  <div className="mt-1 text-amber-700">
                    也可以直接手填经纬度（到店门口在手机地图里长按复制坐标）后提交，服务端会按 200 米阈值判定。
                  </div>
                </div>
              ) : null}

              <p className="text-xs text-slate-500">
                距离以服务端 PostGIS 重算（阈值 200 米），前端上报的距离不作数。手机浏览器必须通过
                HTTPS 访问才允许定位（局域网 IP 的 http 地址会被浏览器直接拒绝）。
              </p>

              <div className="grid grid-cols-1 gap-3 md:grid-cols-3">
                <Field label="本品牌陈列比例（%）">
                  <input
                    type="number"
                    min="0"
                    max="100"
                    value={displayPct}
                    onChange={(e) => setDisplayPct(e.target.value)}
                    className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
                  />
                </Field>
                <Field label="冰柜状态">
                  <select
                    value={freezerStatus}
                    onChange={(e) => setFreezerStatus(e.target.value)}
                    className="rounded-md border border-slate-300 bg-white px-2 py-1.5 text-sm"
                  >
                    {FREEZER_OPTIONS.map((s) => (
                      <option key={s} value={s}>
                        {label(s)}
                      </option>
                    ))}
                  </select>
                </Field>
                <Field label="当日结论">
                  <select
                    value={conclusion}
                    onChange={(e) => {
                      setConclusion(e.target.value);
                      // 选“需补货”时自动给出第一行补货需求，避免只选结论却没填数量
                      if (e.target.value === "NEEDS_REPLENISHMENT") {
                        setReplRows((prev) =>
                          prev.length > 0
                            ? prev
                            : [{ sku_id: "", boxes: "", expected: "" }],
                        );
                      }
                    }}
                    className="rounded-md border border-slate-300 bg-white px-2 py-1.5 text-sm"
                  >
                    {CONCLUSION_OPTIONS.map((c) => (
                      <option key={c} value={c}>
                        {label(c)}
                      </option>
                    ))}
                  </select>
                </Field>
                <Field label="设备异常">
                  <input
                    value={deviceIssue}
                    onChange={(e) => setDeviceIssue(e.target.value)}
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

              <div className="space-y-2 rounded-md border border-slate-200 bg-white p-3">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div className="text-xs font-medium text-slate-600">
                    补货需求（当日结论为「需补货」时必填）
                  </div>
                  <button
                    type="button"
                    disabled={skuList.length === 0}
                    onClick={() =>
                      setReplRows((prev) => [
                        ...prev,
                        { sku_id: "", boxes: "", expected: "" },
                      ])
                    }
                    className="rounded-md border border-slate-300 px-2 py-1 text-xs text-slate-600 disabled:opacity-50"
                  >
                    + 添加 SKU
                  </button>
                </div>
                <p className="text-xs text-slate-500">
                  填写「SKU + 需求箱数 + 期望到货日」；提交后同事务自动生成
                  <b> 补货草稿（DRAFT）</b>，片区经理在「补货管理」确认后才进入正式补货池/采购。
                </p>
                {skuList.length === 0 ? (
                  <p className="text-xs text-amber-700">
                    该品牌暂无 SKU 主数据，无法提报补货；请联系管理员导入 SKU 后再试。
                  </p>
                ) : null}
                {replRows.length === 0 ? (
                  <p className="text-xs text-slate-400">
                    暂无补货需求行（结论选「需补货」时会自动出现一行）
                  </p>
                ) : (
                  <ul className="space-y-2">
                    {replRows.map((r, idx) => (
                      <li key={idx} className="flex flex-wrap items-end gap-2">
                        <Field label="SKU">
                          <select
                            value={r.sku_id}
                            onChange={(e) =>
                              setReplRows((prev) =>
                                prev.map((row, i) =>
                                  i === idx ? { ...row, sku_id: e.target.value } : row,
                                ),
                              )
                            }
                            className="w-56 rounded-md border border-slate-300 bg-white px-2 py-1.5 text-sm"
                          >
                            <option value="">请选择 SKU</option>
                            {skuList.map((s) => (
                              <option key={s.id} value={s.id}>
                                {s.code} · {s.name}
                              </option>
                            ))}
                          </select>
                        </Field>
                        <Field label="需求箱数 *">
                          <input
                            value={r.boxes}
                            onChange={(e) =>
                              setReplRows((prev) =>
                                prev.map((row, i) =>
                                  i === idx ? { ...row, boxes: e.target.value } : row,
                                ),
                              )
                            }
                            placeholder="如 12.5"
                            className="w-24 rounded-md border border-slate-300 px-2 py-1.5 text-sm"
                          />
                        </Field>
                        <Field label="期望到货日">
                          <input
                            type="date"
                            value={r.expected}
                            onChange={(e) =>
                              setReplRows((prev) =>
                                prev.map((row, i) =>
                                  i === idx ? { ...row, expected: e.target.value } : row,
                                ),
                              )
                            }
                            className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
                          />
                        </Field>
                        <button
                          type="button"
                          onClick={() =>
                            setReplRows((prev) => prev.filter((_row, i) => i !== idx))
                          }
                          className="pb-2 text-xs text-rose-600 hover:underline"
                        >
                          移除
                        </button>
                      </li>
                    ))}
                  </ul>
                )}
              </div>

              <div className="space-y-2">
                <div className="text-xs font-medium text-slate-600">
                  现场照片（先上传后绑定，至少一张才可能判为有效）
                </div>
                {photos.length > 0 ? (
                  <ul className="space-y-1">
                    {photos.map((p) => (
                      <li
                        key={p.file_id}
                        className="flex items-center gap-2 rounded-md border border-slate-200 bg-white px-2 py-1 text-xs"
                      >
                        <StatusBadge tone="neutral" label={label(p.photo_kind)} />
                        <span className="font-mono text-slate-500">{p.file_id}</span>
                        <button
                          type="button"
                          onClick={() =>
                            setPhotos((prev) => prev.filter((x) => x.file_id !== p.file_id))
                          }
                          className="ml-auto text-rose-600 hover:underline"
                        >
                          移除
                        </button>
                      </li>
                    ))}
                  </ul>
                ) : (
                  <p className="text-xs text-slate-400">暂无照片</p>
                )}
                <div className="flex flex-wrap items-center gap-2">
                  <select
                    id={`photo-kind-${taskId}`}
                    defaultValue="STOREFRONT"
                    className="rounded-md border border-slate-300 bg-white px-2 py-1.5 text-sm"
                  >
                    {PHOTO_KIND_OPTIONS.map((k) => (
                      <option key={k} value={k}>
                        {label(k)}
                      </option>
                    ))}
                  </select>
                  <input
                    type="file"
                    accept="image/jpeg,image/png,image/webp"
                    onChange={(e) => {
                      const file = e.target.files?.[0];
                      const kind =
                        (document.getElementById(`photo-kind-${taskId}`) as HTMLSelectElement)
                          ?.value ?? "STOREFRONT";
                      if (file) {
                        void pickPhoto(file, kind);
                        e.target.value = "";
                      }
                    }}
                    className="text-xs"
                  />
                  {uploadMut.submitting ? (
                    <span className="text-xs text-slate-500">上传中…</span>
                  ) : null}
                </div>
                {uploadMut.error ? (
                  <div className="text-xs text-rose-600">{uploadMut.error}</div>
                ) : null}
              </div>

              <button
                type="submit"
                disabled={submitMut.submitting}
                className="rounded-md bg-[var(--color-brand)] px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50"
              >
                {submitMut.submitting ? "提交中…" : "提交打卡"}
              </button>
              <p className="text-xs text-slate-400">
                提交后原始证据不可修改；如需说明请在记录详情中追加补充说明。
              </p>
            </form>
          )}
        </>
      ) : null}
    </section>
  );
}

function CreateTaskForm({
  merchants,
  canSupervise,
  defaultDate,
  onCreated,
}: {
  merchants: MerchantOption[];
  canSupervise: boolean;
  defaultDate: string;
  onCreated: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [merchantId, setMerchantId] = useState("");
  const [plannedDate, setPlannedDate] = useState(defaultDate);
  const [windowStart, setWindowStart] = useState("");
  const [windowEnd, setWindowEnd] = useState("");
  const [priority, setPriority] = useState("2");
  const [source, setSource] = useState("WEEKLY_SCHEDULE");
  const [reason, setReason] = useState("");

  const createMut = useApiMutation((payload: Record<string, unknown>) =>
    api.post("/visits/tasks", payload),
  );

  const sources = canSupervise ? SOURCE_OPTIONS : SOURCE_OPTIONS.filter((s) => s !== "SUPERVISION");

  return (
    <div className="space-y-2 rounded-md border border-slate-200 bg-white p-3">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="rounded-md bg-[var(--color-brand)] px-3 py-1.5 text-sm font-medium text-white hover:opacity-90"
      >
        {open ? "收起新建" : "新建任务"}
      </button>

      {open ? (
        <form
          onSubmit={(e) => {
            e.preventDefault();
            createMut
              .run({
                merchant_id: merchantId || merchants[0]?.id || "",
                planned_date: plannedDate,
                window_start_at: windowStart || undefined,
                window_end_at: windowEnd || undefined,
                priority: Number(priority),
                source,
                reason: reason || undefined,
              })
              .then((res) => {
                if (res !== null) {
                  setOpen(false);
                  setReason("");
                  onCreated();
                }
              });
          }}
          className="space-y-3"
        >
          {merchants.length === 0 ? (
            <div className="rounded-md border border-amber-200 bg-amber-50 p-2 text-xs text-amber-800">
              尚未取到商家选项（`GET /api/merchants`）。请先在商家中心建立商家。
            </div>
          ) : null}
          <div className="grid grid-cols-1 gap-3 md:grid-cols-3">
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
            <Field label="计划日期 *">
              <input
                required
                type="date"
                value={plannedDate}
                onChange={(e) => setPlannedDate(e.target.value)}
                className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
              />
            </Field>
            <Field label="来源 *">
              <select
                value={source}
                onChange={(e) => setSource(e.target.value)}
                className="rounded-md border border-slate-300 bg-white px-2 py-1.5 text-sm"
              >
                {sources.map((s) => (
                  <option key={s} value={s}>
                    {label(s)}
                  </option>
                ))}
              </select>
            </Field>
            <Field label="时间窗开始">
              <input
                type="datetime-local"
                value={windowStart}
                onChange={(e) => setWindowStart(e.target.value)}
                className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
              />
            </Field>
            <Field label="时间窗结束">
              <input
                type="datetime-local"
                value={windowEnd}
                onChange={(e) => setWindowEnd(e.target.value)}
                className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
              />
            </Field>
            <Field label="优先级（1 最高）">
              <select
                value={priority}
                onChange={(e) => setPriority(e.target.value)}
                className="rounded-md border border-slate-300 bg-white px-2 py-1.5 text-sm"
              >
                <option value="1">1</option>
                <option value="2">2</option>
                <option value="3">3</option>
              </select>
            </Field>
            <Field label="事由">
              <input
                value={reason}
                onChange={(e) => setReason(e.target.value)}
                className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
              />
            </Field>
          </div>
          <p className="text-xs text-slate-500">
            业务员只能给自己排班；「督导」来源仅督导/经理/管理员可安排（服务端校验）。
          </p>
          {createMut.error ? <div className="text-xs text-rose-600">{createMut.error}</div> : null}
          <div className="flex gap-2">
            <button
              type="submit"
              disabled={createMut.submitting || merchants.length === 0}
              className="rounded-md bg-[var(--color-brand)] px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50"
            >
              {createMut.submitting ? "提交中…" : "创建任务"}
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
