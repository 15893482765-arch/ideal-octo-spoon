/** 周排班与地图（T13 前端联调 / PRD §4.7）。
 *
 * 接入后端：
 * - `GET /api/schedules/week-view?week_start=&region_id=&user_id=`（业务员只返回自己的条目；
 *   未发布周返回「按每周模板预排」的只读条目）；
 * - `GET /api/schedules/map-list`（与周视图同源，供打点）；
 * - `GET /api/schedules/suggestions`、`/coverage-gap`；
 * - `POST /api/schedules/draft-batches` + `/{id}/publish`（逐条选业务员与周几）；
 * - `POST /api/schedules/templates`、`DELETE /templates/{id}`、`POST /templates/apply`
 *   （每周循环：下周依旧同一套排班，除非继续添加/调整）；
 * - `POST /api/schedules/entries/{id}/move`（经理把某业务员当天的商家调到其他天）。
 *
 * 冲突裁决、占用唯一来源（ResourceReservation）、时段自动分配全在
 * ScheduleService / ScheduleConflictService；页面只做展示与受控交互。
 */
import { useEffect, useMemo, useState } from "react";

import { api } from "../lib/api";
import { useAuth } from "../lib/auth";
import { currentWeekStart, label, todayInTz } from "../lib/format";
import { useApiMutation, useApiQuery } from "../lib/useApi";
import { useToast } from "../lib/useToast";
import { ErrorState, LoadingBlock, SessionNotice } from "../components/AsyncState";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { EmptyState } from "../components/EmptyState";
import { MetricCard } from "../components/MetricCard";
import { PageHeader } from "../components/PageHeader";
import { StatusBadge } from "../components/StatusBadge";
import { ToastStack } from "../components/Toast";

const WEEKDAY_NAMES = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"];

interface OptionItem {
  id: string;
  code: string;
  name: string;
}

interface OptionList {
  items: OptionItem[];
}

/** 排班人员下拉：业务员 + 其所属片区（经理可见片区内的全部在职业务员）。 */
interface ScheduleUserOption extends OptionItem {
  region_names: string[];
}

function repLabel(rep: ScheduleUserOption): string {
  return rep.region_names.length > 0
    ? `${rep.name}（${rep.region_names.join("、")}）`
    : rep.name;
}

/** 排班候选商家：启用即入列，scheduled 表示本业务周已排。 */
interface ScheduleCandidate {
  id: string;
  code: string;
  name: string;
  grade: string | null;
  region_id: string;
  region_name: string;
  current_owner_id: string | null;
  scheduled: boolean;
}

interface ScheduleEntry {
  entry_id: string;
  batch_id: string | null;
  region_id: string | null;
  merchant_id: string;
  merchant_name: string;
  lng: number | null;
  lat: number | null;
  assigned_user_id: string;
  assigned_user_name: string | null;
  planned_date: string;
  start_at: string | null;
  end_at: string | null;
  priority: number;
  projected: boolean;
}

interface WeekView {
  week_start: string;
  entries: ScheduleEntry[];
  /** 该周是否已发布排班：已发布但筛选无条目时提示"没有你的排班"，而不是拿模板预排充数。 */
  has_published?: boolean;
}

interface Suggestion {
  merchant_id: string;
  merchant_name: string;
  grade: string | null;
  assignee_id: string | null;
  overdue_days: number;
  has_pending_replenishment: boolean;
  distance_meters: number | null;
  risk_pinned: boolean;
  reasons: string[];
}

interface CoverageGap {
  total_active: number;
  covered: number;
  gap: number;
  rate: number | string;
  missing_merchant_ids: string[];
}

interface BatchView {
  id: string;
  week_start: string;
  region_id: string;
  status: string;
  published_at: string | null;
  entry_count: number;
  /** 本次真实新增条目数；0 表示该周已与模板一致（无写入） */
  created_count?: number;
}

interface TemplateItem {
  id: string;
  region_id: string;
  region_name: string | null;
  weekday: number;
  merchant_id: string;
  merchant_name: string;
  assigned_user_id: string;
  assigned_user_name: string | null;
  start_time: string;
  end_time: string;
  priority: number;
}

/** 按业务周（周一起算）平移 n 周。 */
function shiftWeek(weekStart: string, n: number): string {
  const d = new Date(`${weekStart}T00:00:00Z`);
  d.setUTCDate(d.getUTCDate() + n * 7);
  return d.toISOString().slice(0, 10);
}

/** 该业务周的 7 天（YYYY-MM-DD）。 */
function weekDays(weekStart: string): string[] {
  return Array.from({ length: 7 }, (_, i) => {
    const d = new Date(`${weekStart}T00:00:00Z`);
    d.setUTCDate(d.getUTCDate() + i);
    return d.toISOString().slice(0, 10);
  });
}

function hhmm(value: string | null): string {
  if (!value) return "";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return "";
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

function timeText(value: string): string {
  return value.slice(0, 5);
}

function rateText(rate: number | string): string {
  const n = typeof rate === "number" ? rate : Number(rate);
  return Number.isNaN(n) ? String(rate) : `${(n * 100).toFixed(1)}%`;
}

type TabKey = "week" | "draft" | "templates" | "suggest" | "map";

export function Schedule() {
  const { authenticated, user } = useAuth();
  const { items: toasts, push, dismiss } = useToast();

  const [weekStart, setWeekStart] = useState(currentWeekStart());
  const [regionId, setRegionId] = useState("");
  const [tab, setTab] = useState<TabKey>("week");

  const canManage = user?.role === "REGIONAL_MANAGER" || user?.role === "SYSTEM_ADMIN";
  const isRep = user?.role === "SALES_REP";

  const regions = useApiQuery<OptionList>("/master-data/regions", undefined, {
    enabled: authenticated,
  });

  // 默认「全部可见片区」：周视图一次展示所有片区本周排班，
  // 不再默认选中第一个片区（否则只看得到该片区的一家商家）。
  const week = useApiQuery<WeekView>(
    "/schedules/week-view",
    { week_start: weekStart, region_id: regionId || undefined },
    { enabled: authenticated && Boolean(weekStart) },
  );
  const mapData = useApiQuery<WeekView>(
    "/schedules/map-list",
    { week_start: weekStart, region_id: regionId || undefined },
    { enabled: authenticated && tab === "map" },
  );
  const suggestions = useApiQuery<Suggestion[]>(
    "/schedules/suggestions",
    { region_id: regionId, week_start: weekStart },
    { enabled: authenticated && Boolean(regionId) && tab === "suggest" },
  );
  const gap = useApiQuery<CoverageGap>(
    "/schedules/coverage-gap",
    { region_id: regionId || undefined, week_start: weekStart },
    { enabled: authenticated },
  );
  const reps = useApiQuery<ScheduleUserOption[]>(
    "/schedules/options/users",
    { region_id: regionId || undefined },
    { enabled: authenticated && canManage },
  );
  // 排班候选商家：跨可见片区 + 本周是否已排（新启用商家刷新即可见）
  const candidates = useApiQuery<ScheduleCandidate[]>(
    "/schedules/merchants",
    { week_start: weekStart },
    { enabled: authenticated && canManage && (tab === "draft" || tab === "templates") },
  );
  // 不按当前选中片区过滤：一次展示经理可见片区的全部模板（带片区列）
  const templates = useApiQuery<TemplateItem[]>(
    "/schedules/templates",
    undefined,
    { enabled: authenticated && canManage && tab === "templates" },
  );

  const moveMut = useApiMutation((args: { entryId: string; plannedDate: string }) =>
    api.post<ScheduleEntry>(`/schedules/entries/${args.entryId}/move`, {
      planned_date: args.plannedDate,
    }),
  );
  const templateAddMut = useApiMutation((payload: Record<string, unknown>) =>
    api.post<TemplateItem[]>("/schedules/templates", payload),
  );
  const templateDeleteMut = useApiMutation((id: string) =>
    api.del<void>(`/schedules/templates/${id}`),
  );
  const templateApplyMut = useApiMutation(
    (payload: { region_id: string; week_start: string; replace?: boolean }) =>
      api.post<BatchView>("/schedules/templates/apply", payload),
  );

  const entries = useMemo(() => week.data?.entries ?? [], [week.data]);
  const days = useMemo(() => weekDays(weekStart), [weekStart]);
  const byDay = useMemo(() => {
    const map = new Map<string, ScheduleEntry[]>();
    for (const d of days) map.set(d, []);
    for (const e of entries) {
      const list = map.get(e.planned_date);
      if (list) list.push(e);
      else map.set(e.planned_date, [e]);
    }
    return map;
  }, [entries, days]);

  const suggestionList = suggestions.data ?? [];
  const candidateList = candidates.data ?? [];
  const repList = reps.data ?? [];
  const templateList = templates.data ?? [];
  const today = todayInTz();
  const todayCount = entries.filter((e) => e.planned_date === today).length;
  const merchantCount = new Set(entries.map((e) => e.merchant_id)).size;
  const projectedCount = entries.filter((e) => e.projected).length;
  // 本周各片区已发布批次：勾选商家时该片区走增量追加，其他片区新建草稿
  const publishedBatchByRegion = useMemo(() => {
    const map = new Map<string, string>();
    for (const e of entries) {
      if (e.projected || !e.batch_id || !e.region_id) continue;
      map.set(e.region_id, e.batch_id);
    }
    return map;
  }, [entries]);

  const moveEntry = (entry: ScheduleEntry, plannedDate: string) => {
    moveMut.runSafe({ entryId: entry.entry_id, plannedDate }).then((res) => {
      if (res.ok) {
        push(`已改期到 ${plannedDate}`, "success");
        week.refetch();
        gap.refetch();
      } else {
        push(res.error || "改期失败（目标日与活动/其他排班冲突）", "danger");
      }
    });
  };

  /** 按模板生成目标周排班。
   *
   * `targetRegionId` 为空表示"全部可见片区"逐个执行——多片区账号以前默认只作用于
   * 第一个片区，看起来就像"点了没反应"。
   * `replace=true` 时该周已发布排班会先撤销再按模板整周重排（后端对已开始执行的周拒绝）。
   */
  const applyTemplates = (
    targetWeek: string,
    targetRegionId: string,
    replace: boolean = false,
  ) => {
    const regionIds = targetRegionId
      ? [targetRegionId]
      : (regions.data?.items ?? []).map((r) => r.id);
    if (regionIds.length === 0) {
      push("请先选择要生成的片区", "danger");
      return;
    }
    void (async () => {
      let added = 0;
      let total = 0;
      let emptyRegions = 0;
      const errors: string[] = [];
      for (const rid of regionIds) {
        const res = await templateApplyMut.runSafe({
          region_id: rid,
          week_start: targetWeek,
          replace,
        });
        if (res.ok && res.data) {
          added += res.data.created_count ?? 0;
          total += res.data.entry_count ?? 0;
        } else if (!res.ok) {
          // 批量生成时，从未维护过模板的片区不算失败，单独统计
          if (res.error.includes("还没有每周排班模板")) {
            emptyRegions += 1;
          } else {
            errors.push(res.error || "生成失败");
          }
        }
      }
      const emptyNote =
        emptyRegions > 0 ? `（${emptyRegions} 个片区尚无模板，已跳过）` : "";
      if (errors.length > 0) {
        push(
          added > 0
            ? `部分片区生成成功（新增 ${added} 条），${errors[0]}`
            : errors[0],
          "danger",
        );
      } else if (added === 0) {
        push(
          `${targetWeek} 的排班已与模板一致（${total} 条），无需重复生成${emptyNote}`,
          "info",
        );
      } else {
        push(
          `已按模板生成 ${targetWeek}：新增 ${added} 条，共 ${total} 条${emptyNote}`,
          "success",
        );
      }
      week.refetch();
      gap.refetch();
      templates.refetch();
      candidates.refetch();
    })();
  };

  const [pendingApply, setPendingApply] = useState<{
    week: string;
    regionId: string;
  } | null>(null);

  /** 模板页的「生成」入口：本周已有已发布排班时先确认，避免一键覆盖手工调整。 */
  const requestApply = (targetWeek: string, targetRegionId: string) => {
    const hasPublished =
      targetWeek === weekStart &&
      (week.data?.entries ?? []).some((e) => !e.projected);
    if (hasPublished) {
      setPendingApply({ week: targetWeek, regionId: targetRegionId });
      return;
    }
    applyTemplates(targetWeek, targetRegionId, false);
  };

  if (!authenticated) {
    return (
      <div className="space-y-4">
        <PageHeader title="周排班与地图" description="周视图、地图打点与覆盖缺口" />
        <SessionNotice hint="当前是开发注入账号，请从登录页用真实账号登录后再查看业务数据。" />
      </div>
    );
  }

  const tabs: ReadonlyArray<[TabKey, string]> = canManage
    ? [
        ["week", "周视图"],
        ["draft", "排班草稿"],
        ["templates", "每周模板"],
        ["suggest", "建议与缺口"],
        ["map", "地图"],
      ]
    : [
        ["week", "我的排班"],
        ["map", "地图"],
      ];

  return (
    <div className="space-y-4">
      <PageHeader
        title={isRep ? "我的排班" : "周排班与地图"}
        description={
          isRep
            ? "经理排给你的本周拜访商家；一天多家商家按顺序分时段，不需要你手动调整"
            : "业务周从周一起算；一天可排多家商家（自动分时段），任意真实冲突整体拒绝（零写入）"
        }
      />

      <div className="flex flex-wrap items-end gap-3 rounded-md border border-slate-200 bg-white p-3">
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={() => setWeekStart(shiftWeek(weekStart, -1))}
            className="rounded-md border border-slate-300 px-2 py-1.5 text-sm text-slate-600 hover:bg-slate-50"
          >
            ‹ 上周
          </button>
          <button
            type="button"
            onClick={() => setWeekStart(currentWeekStart())}
            className="rounded-md border border-slate-300 px-2 py-1.5 text-sm text-slate-600 hover:bg-slate-50"
          >
            本周
          </button>
          <button
            type="button"
            onClick={() => setWeekStart(shiftWeek(weekStart, 1))}
            className="rounded-md border border-slate-300 px-2 py-1.5 text-sm text-slate-600 hover:bg-slate-50"
          >
            下周 ›
          </button>
        </div>
        <Field label="业务周（自动对齐周一）">
          <input
            type="date"
            value={weekStart}
            onChange={(e) => {
              const v = e.target.value;
              if (!v) return;
              const d = new Date(`${v}T00:00:00Z`);
              const dow = (d.getUTCDay() + 6) % 7;
              d.setUTCDate(d.getUTCDate() - dow);
              setWeekStart(d.toISOString().slice(0, 10));
            }}
            className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
          />
        </Field>
        <Field label="片区">
          <select
            value={regionId}
            onChange={(e) => setRegionId(e.target.value)}
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
        <div className="ml-auto flex flex-wrap gap-1 rounded-md bg-slate-100 p-1">
          {tabs.map(([key, text]) => (
            <button
              key={key}
              type="button"
              onClick={() => setTab(key)}
              className={`rounded px-3 py-1.5 text-sm ${
                tab === key ? "bg-white text-slate-800 shadow-sm" : "text-slate-600"
              }`}
            >
              {text}
            </button>
          ))}
        </div>
      </div>

      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <MetricCard label={isRep ? "我的拜访商家" : "本周排班商家"} value={merchantCount} unit="家" />
        <MetricCard label="排班条目" value={entries.length} unit="条" />
        <MetricCard label="今日需拜访" value={todayCount} unit="家" />
        <MetricCard
          label={canManage ? "覆盖缺口" : "覆盖率"}
          value={canManage ? (gap.data?.gap ?? "—") : gap.data ? rateText(gap.data.rate) : "—"}
          unit={canManage ? "家" : ""}
        />
      </div>

      {projectedCount > 0 ? (
        <p className="rounded-md border border-sky-200 bg-sky-50 px-3 py-2 text-xs text-sky-800">
          本周尚未发布，下面按「每周模板」预排了 {projectedCount} 条（只读，不生成巡访任务）。
          经理可在「每周模板」里一键生成正式排班与巡访任务。
        </p>
      ) : null}

      {entries.length === 0 && week.data?.has_published ? (
        <p className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800">
          本周排班已发布，但没有符合当前筛选的条目。
          业务员只能看到排给自己的商家；若确实没有你的排班，请联系片区经理在「排班」里补排。
        </p>
      ) : null}

      {tab === "week" ? (
        <WeekBoard
          days={days}
          byDay={byDay}
          loading={week.loading}
          error={week.error}
          status={week.status}
          onRetry={week.refetch}
          canManage={canManage}
          moving={moveMut.submitting}
          onMove={moveEntry}
        />
      ) : null}

      {tab === "draft" ? (
        <DraftPanel
          days={days}
          candidates={candidates.data ?? []}
          reps={repList}
          publishedBatchByRegion={publishedBatchByRegion}
          loading={candidates.loading || reps.loading}
          loadError={candidates.error || reps.error}
          reload={() => {
            candidates.refetch();
            reps.refetch();
          }}
          weekStart={weekStart}
          onDone={() => {
            week.refetch();
            gap.refetch();
            candidates.refetch();
            templates.refetch();
          }}
          push={push}
        />
      ) : null}

      {tab === "templates" ? (
        <TemplatePanel
          weekStart={weekStart}
          regionId={regionId}
          regions={regions.data?.items ?? []}
          candidates={candidateList}
          reps={repList}
          templates={templateList}
          loading={templates.loading || candidates.loading || reps.loading}
          loadError={templates.error || candidates.error || reps.error}
          reload={() => {
            templates.refetch();
            candidates.refetch();
            reps.refetch();
          }}
          addMut={templateAddMut}
          deleteMut={templateDeleteMut}
          applyMut={templateApplyMut}
          onApplied={requestApply}
          push={push}
        />
      ) : null}

      {tab === "suggest" ? (
        regionId ? (
          <SuggestionPanel
            items={suggestionList}
            loading={suggestions.loading}
            error={suggestions.error}
            status={suggestions.status}
            onRetry={suggestions.refetch}
            gap={gap.data}
          />
        ) : (
          <EmptyState
            title="请先选择片区"
            description="排班建议按片区计算；在顶部把片区从「全部可见片区」切到具体片区即可查看。"
          />
        )
      ) : null}

      {tab === "map" ? (
        <MapPanel
          entries={mapData.data?.entries ?? []}
          loading={mapData.loading}
          error={mapData.error}
          status={mapData.status}
          onRetry={mapData.refetch}
        />
      ) : null}

      <ConfirmDialog
        open={pendingApply !== null}
        title="本周已有排班，按模板重排会替换它们"
        description="将撤销该周已发布的排班（条目/巡访任务/资源占用）并按每周模板重新生成。若本周已有巡访任务开始执行，系统会拒绝重排并保留原排班。"
        confirmText="按模板重排本周"
        destructive
        onClose={() => setPendingApply(null)}
        onConfirm={() => {
          const target = pendingApply;
          setPendingApply(null);
          if (target) applyTemplates(target.week, target.regionId, true);
        }}
      />

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

// ------------------------------ 排班草稿 ------------------------------

function DraftPanel({
  days,
  candidates,
  reps,
  publishedBatchByRegion,
  loading,
  loadError,
  reload,
  weekStart,
  onDone,
  push,
}: {
  days: string[];
  candidates: ScheduleCandidate[];
  reps: ScheduleUserOption[];
  /** 片区 → 本周已发布批次 id：有值时该片区的勾选走“增量追加”，保留原排班 */
  publishedBatchByRegion: Map<string, string>;
  loading: boolean;
  loadError: string;
  reload: () => void;
  weekStart: string;
  onDone: () => void;
  push: (message: string, tone?: "success" | "danger" | "info") => void;
}) {
  const [picked, setPicked] = useState<Record<string, boolean>>({});
  const [assignee, setAssignee] = useState<Record<string, string>>({});
  const [day, setDay] = useState<Record<string, string>>({});
  const [batches, setBatches] = useState<BatchView[]>([]);
  const [showScheduled, setShowScheduled] = useState(false);
  const [regionFilter, setRegionFilter] = useState("");
  const [busy, setBusy] = useState(false);

  const draftMut = useApiMutation((payload: Record<string, unknown>) =>
    api.post<BatchView>("/schedules/draft-batches", payload),
  );
  const publishMut = useApiMutation((batchId: string) =>
    api.post<BatchView>(`/schedules/draft-batches/${batchId}/publish`),
  );
  const appendMut = useApiMutation(
    (payload: { batchId: string; entries: Record<string, unknown>[] }) =>
      api.post<BatchView>(`/schedules/draft-batches/${payload.batchId}/append`, {
        entries: payload.entries,
      }),
  );

  useEffect(() => {
    setPicked({});
    setAssignee({});
    setDay({});
    setBatches([]);
  }, [weekStart]);

  const pending = candidates.filter((c) => !c.scheduled);
  const scheduledCount = candidates.length - pending.length;
  const regionOptions = Array.from(
    new Map(candidates.map((c) => [c.region_id, c.region_name])).entries(),
  );
  const visible = candidates.filter(
    (c) => (showScheduled || !c.scheduled) && (!regionFilter || c.region_id === regionFilter),
  );
  const pickedIds = Object.keys(picked).filter((id) => picked[id]);

  // 下拉框显示的是“已选值 || 主负责人默认值”，提交也必须按同一口径取值；
  // 否则用户看到下拉已经有人、但 state 里没有记录（选中同一项不会触发 onChange），
  // 提交时就会误报“未选择业务员”。
  const effectiveAssignee = (c: ScheduleCandidate): string => {
    if (assignee[c.id]) return assignee[c.id];
    if (c.current_owner_id && reps.some((r) => r.id === c.current_owner_id)) {
      return c.current_owner_id;
    }
    return reps.length === 1 ? reps[0].id : "";
  };

  const submit = () => {
    const chosen = pending.filter((c) => picked[c.id]);
    if (chosen.length === 0) {
      push("请先勾选要排班的商家", "danger");
      return;
    }
    const missing = chosen.filter((c) => !effectiveAssignee(c));
    if (missing.length > 0) {
      push(`有 ${missing.length} 家未选择业务员（请在下拉里选人）`, "danger");
      return;
    }

    // 草稿批次按片区归档：勾选跨片区时自动拆成多个批次，分别创建/追加
    const groups = new Map<string, { entries: Record<string, unknown>[] }>();
    for (const c of chosen) {
      const group = groups.get(c.region_id) ?? { entries: [] };
      group.entries.push({
        merchant_id: c.id,
        assigned_user_id: effectiveAssignee(c),
        planned_date: day[c.id] || days[0],
        priority: 2,
      });
      groups.set(c.region_id, group);
    }

    setBusy(true);
    void (async () => {
      let appended = 0;
      try {
        for (const [regionKey, group] of groups) {
          const published = publishedBatchByRegion.get(regionKey);
          if (!published) continue;
          const res = await appendMut.runSafe({ batchId: published, entries: group.entries });
          if (!res.ok) {
            push(res.error || "追加失败（存在冲突，已整体回滚）", "danger");
            return;
          }
          appended += group.entries.length;
          groups.delete(regionKey);
        }

        const created: BatchView[] = [];
        for (const [regionKey, group] of groups) {
          const res = await draftMut.runSafe({
            region_id: regionKey,
            week_start: weekStart,
            entries: group.entries,
            idempotency_key:
              typeof crypto !== "undefined" && "randomUUID" in crypto
                ? crypto.randomUUID()
                : undefined,
          });
          if (!res.ok || !res.data) {
            push(res.error || "草稿生成失败", "danger");
            break;
          }
          created.push(res.data);
        }
        if (created.length > 0) {
          setBatches((prev) => [...prev, ...created]);
          push(
            `已生成 ${created.length} 个片区草稿（共 ${created.reduce((s, b) => s + b.entry_count, 0)} 条），确认后发布`,
            "success",
          );
        }
        if (appended > 0) {
          push(`已追加 ${appended} 家到本周已发布排班`, "success");
          onDone();
        }
        setPicked({});
        setAssignee({});
      } finally {
        setBusy(false);
      }
    })();
  };

  const publishAll = () => {
    const pendingBatches = batches.filter((b) => b.status !== "PUBLISHED");
    if (pendingBatches.length === 0) return;
    setBusy(true);
    void (async () => {
      try {
        for (const b of pendingBatches) {
          const res = await publishMut.runSafe(b.id);
          if (!res.ok || !res.data) {
            push(res.error || "发布失败（存在冲突，已整体回滚）", "danger");
            return;
          }
        }
        push(`已发布 ${pendingBatches.length} 个片区排班：巡访任务与人员占用已生成`, "success");
        setBatches([]);
        onDone();
      } finally {
        setBusy(false);
      }
    })();
  };

  if (loading) return <LoadingBlock text="加载商家与业务员…" />;
  if (loadError) {
    return <ErrorState message={loadError} status={null} onRetry={reload} />;
  }
  if (candidates.length === 0) {
    return (
      <EmptyState
        title="暂无可排班商家"
        description="请先在商家中心启用商家（status=ACTIVE）并维护主负责人，再回到这里刷新。"
      />
    );
  }

  return (
    <div className="space-y-3">
      <div className="rounded-md border border-slate-200 bg-white p-3">
        <div className="flex flex-wrap items-center gap-3 text-xs text-slate-500">
          <span>
            共 <b className="text-slate-700">{candidates.length}</b> 家启用商家，待排{" "}
            <b className="text-slate-700">{pending.length}</b> 家，本周已排{" "}
            <b className="text-slate-700">{scheduledCount}</b> 家。
            一天可排多家，发布时自动分时段，不会因此报冲突。
          </span>
          <div className="ml-auto flex flex-wrap items-center gap-2">
            <label className="flex items-center gap-1 text-slate-600">
              <input
                type="checkbox"
                checked={showScheduled}
                onChange={(e) => setShowScheduled(e.target.checked)}
              />
              显示本周已排
            </label>
            <select
              value={regionFilter}
              onChange={(e) => setRegionFilter(e.target.value)}
              className="rounded-md border border-slate-300 bg-white px-2 py-1"
            >
              <option value="">全部片区</option>
              {regionOptions.map(([id, name]) => (
                <option key={id} value={id}>
                  {name}
                </option>
              ))}
            </select>
            <button
              type="button"
              onClick={reload}
              className="rounded-md border border-slate-300 px-2 py-1 text-slate-600"
              title="重新查询已启用商家（新启用的商家会立即出现）"
            >
              刷新商家
            </button>
          </div>
        </div>

        <div className="mt-3 max-h-[460px] overflow-auto">
          <table className="w-full text-xs">
            <thead className="sticky top-0 bg-white">
              <tr className="text-left text-slate-500">
                <th className="w-8 py-1">选</th>
                <th className="py-1">商家</th>
                <th className="py-1">片区</th>
                <th className="py-1">排给业务员</th>
                <th className="py-1">排在周几</th>
                <th className="py-1">本周状态</th>
              </tr>
            </thead>
            <tbody>
              {visible.map((m) => {
                const defaultAssignee = effectiveAssignee(m);
                return (
                  <tr key={m.id} className="border-t border-slate-100">
                    <td className="py-1.5">
                      <input
                        type="checkbox"
                        checked={Boolean(picked[m.id])}
                        disabled={m.scheduled}
                        onChange={(e) => {
                          setPicked((prev) => ({ ...prev, [m.id]: e.target.checked }));
                          if (e.target.checked) {
                            // 勾选时把当前有效业务员固化成显式选择，避免“看着选了却没提交”
                            setAssignee((prev) => ({
                              ...prev,
                              [m.id]: prev[m.id] || effectiveAssignee(m),
                            }));
                          }
                        }}
                      />
                    </td>
                    <td className="py-1.5">
                      <div className="text-slate-800">{m.name}</div>
                      <div className="text-[11px] text-slate-400">
                        {m.code}
                        {m.grade ? ` · ${m.grade} 级` : ""}
                      </div>
                    </td>
                    <td className="py-1.5 text-slate-500">{m.region_name}</td>
                    <td className="py-1.5">
                      <select
                        value={defaultAssignee}
                        onChange={(e) =>
                          setAssignee((prev) => ({ ...prev, [m.id]: e.target.value }))
                        }
                        className="w-full max-w-[200px] rounded-md border border-slate-300 bg-white px-2 py-1"
                      >
                        <option value="">请选择业务员</option>
                        {reps.map((r) => (
                          <option key={r.id} value={r.id} title={r.region_names.join("、")}>
                            {repLabel(r)}
                          </option>
                        ))}
                      </select>
                    </td>
                    <td className="py-1.5">
                      <select
                        value={day[m.id] || days[0]}
                        onChange={(e) =>
                          setDay((prev) => ({ ...prev, [m.id]: e.target.value }))
                        }
                        className="rounded-md border border-slate-300 bg-white px-2 py-1"
                      >
                        {days.map((d, i) => (
                          <option key={d} value={d}>
                            {WEEKDAY_NAMES[i]}（{d.slice(5)}）
                          </option>
                        ))}
                      </select>
                    </td>
                    <td className="py-1.5">
                      {m.scheduled ? (
                        <StatusBadge tone="success" label="本周已排" />
                      ) : (
                        <StatusBadge tone="warning" label="待排班" />
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>

        <div className="mt-3 flex flex-wrap items-center gap-3">
          <span className="text-xs text-slate-500">已选 {pickedIds.length} 家</span>
          <button
            type="button"
            disabled={busy || pickedIds.length === 0}
            onClick={submit}
            className="rounded-md bg-[var(--color-brand)] px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50"
          >
            {busy ? "提交中…" : "提交（已发布片区追加，其他片区生成草稿）"}
          </button>
        </div>
      </div>

      {batches.length > 0 ? (
        <div className="space-y-2 rounded-md border border-slate-200 bg-white p-3">
          <div className="text-xs font-medium text-slate-600">
            待发布草稿（{batches.length} 个片区）
          </div>
          {batches.map((b) => (
            <div key={b.id} className="flex flex-wrap items-center gap-3 text-xs text-slate-600">
              <span className="font-mono">{b.id.slice(0, 8)}…</span>
              <span>
                {candidates.find((c) => c.region_id === b.region_id)?.region_name ?? "—"}
              </span>
              <span>{b.entry_count} 条</span>
              <StatusBadge
                tone={b.status === "PUBLISHED" ? "success" : "warning"}
                label={label(b.status)}
              />
            </div>
          ))}
          <button
            type="button"
            disabled={busy}
            onClick={publishAll}
            className="rounded-md bg-[var(--color-brand)] px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50"
          >
            {busy ? "发布中…" : "发布全部草稿（生成任务与占用）"}
          </button>
        </div>
      ) : null}
    </div>
  );
}

// ------------------------------ 每周模板 ------------------------------

function TemplatePanel({
  weekStart,
  regionId,
  regions,
  candidates,
  reps,
  templates,
  loading,
  loadError,
  reload,
  addMut,
  deleteMut,
  applyMut,
  onApplied,
  push,
}: {
  weekStart: string;
  regionId: string;
  regions: OptionItem[];
  candidates: ScheduleCandidate[];
  reps: ScheduleUserOption[];
  templates: TemplateItem[];
  loading: boolean;
  loadError: string;
  reload: () => void;
  addMut: ReturnType<typeof useApiMutation<Record<string, unknown>, TemplateItem[]>>;
  deleteMut: ReturnType<typeof useApiMutation<string, void>>;
  applyMut: ReturnType<
    typeof useApiMutation<
      { region_id: string; week_start: string; replace?: boolean },
      BatchView
    >
  >;
  onApplied: (targetWeek: string, targetRegionId: string) => void;
  push: (message: string, tone?: "success" | "danger" | "info") => void;
}) {
  const [merchantId, setMerchantId] = useState("");
  const [userId, setUserId] = useState("");
  const [weekday, setWeekday] = useState(1);
  const [start, setStart] = useState("09:00");
  const [end, setEnd] = useState("09:30");
  // "" = 全部可见片区。必须跟随顶部片区选择：以前只在为空时初始化一次，
  // 切换顶部片区后这里仍指向最初那个片区，同步/生成看起来"完全没反应"。
  const [targetRegion, setTargetRegion] = useState(regionId);

  useEffect(() => {
    setTargetRegion(regionId);
  }, [regionId]);

  const targetRegionIds = targetRegion
    ? [targetRegion]
    : regions.map((r) => r.id);

  const syncMut = useApiMutation((payload: {
    region_id: string;
    overwrite: boolean;
    week_start?: string;
  }) =>
    api.post<{ created: number; updated: number; skipped: number; source_entries: number }>(
      "/schedules/templates/sync",
      payload,
    ),
  );

  const sync = (overwrite: boolean) => {
    if (targetRegionIds.length === 0) {
      push("请先选择要同步的片区", "danger");
      return;
    }
    void (async () => {
      let created = 0;
      let updated = 0;
      let skipped = 0;
      const errors: string[] = [];
      for (const rid of targetRegionIds) {
        const res = await syncMut.runSafe({
          region_id: rid,
          overwrite,
          // 用「当前查看的业务周」作为源周：后端默认取"历史最新一周"，
          // 若已提前生成下周，就会用下周覆盖，模板永远变不成本周的样子。
          week_start: weekStart,
        });
        if (res.ok && res.data) {
          created += res.data.created;
          updated += res.data.updated;
          skipped += res.data.skipped;
        } else if (!res.ok) {
          errors.push(res.error || "同步失败");
        }
      }
      if (errors.length > 0) {
        push(errors[0], "danger");
      } else if (created === 0 && updated === 0) {
        push(
          `${weekStart} 这一周的已发布排班为空，或模板已与该周一致（跳过 ${skipped}）`,
          "info",
        );
      } else {
        push(`模板已按 ${weekStart} 的排班同步：新增 ${created}、更新 ${updated}`, "success");
      }
      reload();
    })();
  };

  const add = () => {
    if (!merchantId || !userId) {
      push("请选择商家与业务员", "danger");
      return;
    }
    const merchantRegion =
      candidates.find((c) => c.id === merchantId)?.region_id ?? targetRegion;
    if (!merchantRegion) {
      push("请先选择片区与商家", "danger");
      return;
    }
    addMut
      .runSafe({
        region_id: merchantRegion,
        entries: [
          {
            merchant_id: merchantId,
            assigned_user_id: userId,
            weekday,
            start_time: `${start}:00`,
            end_time: `${end}:00`,
            priority: 2,
          },
        ],
      })
      .then((res) => {
        if (res.ok) {
          push("已加入每周循环模板：下周自动沿用", "success");
          setMerchantId("");
          reload();
        } else {
          push(res.error || "新增模板失败（该商家可能已在模板中）", "danger");
        }
      });
  };

  if (loading) return <LoadingBlock text="加载每周模板…" />;
  if (loadError) return <ErrorState message={loadError} status={null} onRetry={reload} />;

  return (
    <div className="space-y-3">
      <div className="rounded-md border border-slate-200 bg-white p-3">
        <div className="text-xs text-slate-500">
          每周循环：模板里的「业务员 + 周几 + 商家」每周自动沿用，下周打开就是同样的排班；
          需要调整时再添加/删除模板。同一业务员同一天可放多家商家（生成时自动顺延时段）。
          列表展示经理可见的<strong>全部片区</strong>；新增/同步/生成按上方所选片区执行。
        </div>
        <div className="mt-3 flex flex-wrap items-end gap-3">
          <Field label="片区（新增/同步/生成）">
            <select
              value={targetRegion}
              onChange={(e) => setTargetRegion(e.target.value)}
              className="rounded-md border border-slate-300 bg-white px-2 py-1.5 text-sm"
            >
              {regions.map((r) => (
                <option key={r.id} value={r.id}>
                  {r.name}
                </option>
              ))}
            </select>
          </Field>
          <Field label="商家">
            <select
              value={merchantId}
              onChange={(e) => {
                setMerchantId(e.target.value);
                const picked = candidates.find((c) => c.id === e.target.value);
                if (picked) setTargetRegion(picked.region_id);
              }}
              className="w-[260px] rounded-md border border-slate-300 bg-white px-2 py-1.5 text-sm"
            >
              <option value="">请选择商家</option>
              {candidates.map((m) => (
                <option key={m.id} value={m.id}>
                  {m.region_name} · {m.name}（{m.code}）
                </option>
              ))}
            </select>
          </Field>
          <Field label="排给业务员">
            <select
              value={userId}
              onChange={(e) => setUserId(e.target.value)}
              className="w-[180px] rounded-md border border-slate-300 bg-white px-2 py-1.5 text-sm"
            >
              <option value="">请选择</option>
              {reps.map((r) => (
                <option key={r.id} value={r.id} title={r.region_names.join("、")}>
                  {repLabel(r)}
                </option>
              ))}
            </select>
          </Field>
          <Field label="周几">
            <select
              value={weekday}
              onChange={(e) => setWeekday(Number(e.target.value))}
              className="rounded-md border border-slate-300 bg-white px-2 py-1.5 text-sm"
            >
              {WEEKDAY_NAMES.map((name, i) => (
                <option key={name} value={i + 1}>
                  {name}
                </option>
              ))}
            </select>
          </Field>
          <Field label="开始">
            <input
              type="time"
              value={start}
              onChange={(e) => setStart(e.target.value)}
              className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
            />
          </Field>
          <Field label="结束">
            <input
              type="time"
              value={end}
              onChange={(e) => setEnd(e.target.value)}
              className="rounded-md border border-slate-300 px-2 py-1.5 text-sm"
            />
          </Field>
          <button
            type="button"
            disabled={addMut.submitting}
            onClick={add}
            className="rounded-md bg-[var(--color-brand)] px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50"
          >
            {addMut.submitting ? "添加中…" : "加入模板"}
          </button>
        </div>
      </div>

      <div className="rounded-md border border-slate-200 bg-white p-3">
        <div className="flex flex-wrap items-center gap-3">
          <div className="text-xs font-medium text-slate-600">
            每周模板（全部可见片区，共 {templates.length} 条）
          </div>
          <span className="rounded-full bg-slate-100 px-2 py-0.5 text-[11px] text-slate-600">
            生成目标：
            {targetRegion
              ? (regions.find((r) => r.id === targetRegion)?.name ?? targetRegion)
              : `全部可见片区（${regions.length} 个）`}
          </span>
          <button
            type="button"
            disabled={applyMut.submitting || templates.length === 0 || targetRegionIds.length === 0}
            onClick={() => onApplied(weekStart, targetRegion)}
            className="rounded-md border border-[var(--color-brand)] px-3 py-1.5 text-sm text-[var(--color-brand)] disabled:opacity-50"
            title="按模板生成/重排当前业务周；本周已有排班时会先确认再整周重排"
          >
            {applyMut.submitting ? "生成中…" : "按模板生成本周排班"}
          </button>
          <button
            type="button"
            disabled={applyMut.submitting || templates.length === 0 || targetRegionIds.length === 0}
            onClick={() => onApplied(shiftWeek(weekStart, 1), targetRegion)}
            className="rounded-md border border-slate-300 px-3 py-1.5 text-sm text-slate-600 disabled:opacity-50"
            title="按模板生成下周；下周已排的商家不重复写入，只补齐缺失"
          >
            生成下周排班
          </button>
          <div className="ml-auto flex items-center gap-2">
            <span className="text-[11px] text-slate-400">
              模板落后于实际排班？
            </span>
            <button
              type="button"
              disabled={syncMut.submitting || targetRegionIds.length === 0}
              onClick={() => sync(false)}
              className="rounded-md border border-slate-300 px-2.5 py-1.5 text-xs text-slate-600 disabled:opacity-50"
            >
              {syncMut.submitting ? "同步中…" : "从已发布排班补齐缺失"}
            </button>
            <button
              type="button"
              disabled={syncMut.submitting || targetRegionIds.length === 0}
              onClick={() => sync(true)}
              className="rounded-md border border-amber-300 px-2.5 py-1.5 text-xs text-amber-700 disabled:opacity-50"
              title={`用当前查看这一周（${weekStart}）的已发布排班覆盖模板的业务员/周几/时段`}
            >
              用当前周排班覆盖模板
            </button>
          </div>
        </div>

        {templates.length === 0 ? (
          <p className="mt-3 text-sm text-slate-400">
            还没有模板。发布排班后系统会自动沉淀模板；已有历史排班请点右上角
            「从已发布排班补齐缺失」，也可以在上方手动加入。
          </p>
        ) : (
          <table className="mt-3 w-full text-xs">
            <thead>
              <tr className="text-left text-slate-500">
                <th className="py-1">片区</th>
                <th className="py-1">周几</th>
                <th className="py-1">商家</th>
                <th className="py-1">业务员</th>
                <th className="py-1">时段</th>
                <th className="py-1" />
              </tr>
            </thead>
            <tbody>
              {[...templates]
                .sort(
                  (a, b) =>
                    (a.region_name ?? "").localeCompare(b.region_name ?? "")
                    || a.weekday - b.weekday
                    || a.start_time.localeCompare(b.start_time),
                )
                .map((t) => (
                  <tr key={t.id} className="border-t border-slate-100">
                    <td className="py-1.5 text-slate-500">{t.region_name ?? "—"}</td>
                    <td className="py-1.5 text-slate-700">{WEEKDAY_NAMES[t.weekday - 1]}</td>
                    <td className="py-1.5 text-slate-800">{t.merchant_name}</td>
                    <td className="py-1.5 text-slate-600">{t.assigned_user_name ?? "—"}</td>
                    <td className="py-1.5 text-slate-600">
                      {timeText(t.start_time)}–{timeText(t.end_time)}
                    </td>
                    <td className="py-1.5 text-right">
                      <button
                        type="button"
                        disabled={deleteMut.submitting}
                        onClick={() => {
                          deleteMut.runSafe(t.id).then((res) => {
                            if (res.ok) {
                              push("已删除模板", "success");
                              reload();
                            } else {
                              push(res.error || "删除失败", "danger");
                            }
                          });
                        }}
                        className="text-rose-600 hover:underline disabled:opacity-50"
                      >
                        删除
                      </button>
                    </td>
                  </tr>
                ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}

// ------------------------------ 建议与缺口 ------------------------------

function SuggestionPanel({
  items,
  loading,
  error,
  status,
  onRetry,
  gap,
}: {
  items: Suggestion[];
  loading: boolean;
  error: string;
  status: number | null;
  onRetry: () => void;
  gap: CoverageGap | null;
}) {
  if (loading) return <LoadingBlock text="计算排班建议…" />;
  if (error) return <ErrorState message={error} status={status} onRetry={onRetry} />;

  return (
    <div className="space-y-3">
      <div className="rounded-md border border-slate-200 bg-white p-3 text-xs text-slate-500">
        仅建议、不自动发布。覆盖缺口 {gap?.gap ?? "—"} 家 / 覆盖率{" "}
        {gap ? rateText(gap.rate) : "—"}；要排班请到「排班草稿」勾选商家并选择业务员与周几。
      </div>
      <div className="rounded-md border border-slate-200 bg-white p-3">
        {items.length === 0 ? (
          <p className="text-sm text-slate-400">该片区本周无缺口建议（已排或已有效巡访）。</p>
        ) : (
          <ul className="divide-y divide-slate-100">
            {items.map((s) => (
              <li key={s.merchant_id} className="flex items-start gap-3 py-2">
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="text-sm font-medium text-slate-800">{s.merchant_name}</span>
                    {s.grade ? <StatusBadge tone="neutral" label={`${s.grade} 级`} /> : null}
                    {s.risk_pinned ? <StatusBadge tone="danger" label="风险钉首" /> : null}
                    {s.overdue_days > 0 ? (
                      <StatusBadge tone="warning" label={`逾期 ${s.overdue_days} 天`} />
                    ) : null}
                    {s.has_pending_replenishment ? (
                      <StatusBadge tone="info" label="有待处理补货" />
                    ) : null}
                  </div>
                  <div className="mt-1 text-xs text-slate-500">
                    {s.reasons.length > 0 ? s.reasons.join("；") : "常规排班"}
                    {s.distance_meters !== null
                      ? ` · 距离 ${Math.round(s.distance_meters)} 米`
                      : ""}
                  </div>
                </div>
                <div className="shrink-0 text-right text-xs text-slate-600">
                  {s.assignee_id ? "主负责人已就绪" : "无主负责人（需先建负责关系）"}
                </div>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

// ------------------------------ 周视图 ------------------------------

function WeekBoard({
  days,
  byDay,
  loading,
  error,
  status,
  onRetry,
  canManage,
  moving,
  onMove,
}: {
  days: string[];
  byDay: Map<string, ScheduleEntry[]>;
  loading: boolean;
  error: string;
  status: number | null;
  onRetry: () => void;
  canManage: boolean;
  moving: boolean;
  onMove: (entry: ScheduleEntry, plannedDate: string) => void;
}) {
  if (loading) return <LoadingBlock text="加载周排班…" />;
  if (error) return <ErrorState message={error} status={status} onRetry={onRetry} />;

  const total = days.reduce((acc, d) => acc + (byDay.get(d)?.length ?? 0), 0);
  if (total === 0) {
    return (
      <EmptyState
        title="本周暂无排班"
        description="经理可在「排班草稿」里勾选商家、选择业务员与周几后生成并发布；模板会在下周自动沿用。"
      />
    );
  }

  return (
    <div className="grid grid-cols-1 gap-2 md:grid-cols-7">
      {days.map((d, i) => {
        const list = byDay.get(d) ?? [];
        const isToday = d === todayInTz();
        return (
          <div
            key={d}
            className={`rounded-md border p-2 ${
              isToday ? "border-[var(--color-brand)] bg-white" : "border-slate-200 bg-white"
            }`}
          >
            <div className="flex items-baseline justify-between">
              <span className="text-xs font-medium text-slate-700">{WEEKDAY_NAMES[i]}</span>
              <span className="text-xs text-slate-400">{d.slice(5)}</span>
            </div>
            <div className="mt-2 space-y-1">
              {list.length === 0 ? (
                <p className="text-xs text-slate-300">—</p>
              ) : (
                list.map((e) => (
                  <div
                    key={e.entry_id}
                    className="rounded border border-slate-200 bg-slate-50 p-1.5 text-xs"
                  >
                    <div className="flex items-start justify-between gap-1">
                      <span className="truncate font-medium text-slate-800" title={e.merchant_name}>
                        {e.merchant_name}
                      </span>
                      {e.projected ? <StatusBadge tone="info" label="模板预排" /> : null}
                    </div>
                    <div className="mt-0.5 text-slate-500">
                      {e.assigned_user_name ?? "未指派"}
                      {e.start_at && e.end_at ? ` · ${hhmm(e.start_at)}–${hhmm(e.end_at)}` : ""}
                    </div>
                    <div className="mt-0.5 flex items-center gap-1">
                      <span className="text-slate-400">P{e.priority}</span>
                      {e.lng === null || e.lat === null ? (
                        <span className="text-amber-600">无坐标</span>
                      ) : null}
                    </div>
                    {canManage && !e.projected ? (
                      <select
                        value={e.planned_date}
                        disabled={moving}
                        onChange={(ev) => {
                          if (ev.target.value !== e.planned_date) onMove(e, ev.target.value);
                        }}
                        className="mt-1 w-full rounded border border-slate-300 bg-white px-1 py-0.5 text-[11px] text-slate-600 disabled:opacity-50"
                        title="改期到本周其他天（避让地推时使用）"
                      >
                        {days.map((target, idx) => (
                          <option key={target} value={target}>
                            改到{WEEKDAY_NAMES[idx]}
                          </option>
                        ))}
                      </select>
                    ) : null}
                  </div>
                ))
              )}
            </div>
          </div>
        );
      })}
    </div>
  );
}

// ------------------------------ 地图 ------------------------------

function MapPanel({
  entries,
  loading,
  error,
  status,
  onRetry,
}: {
  entries: ScheduleEntry[];
  loading: boolean;
  error: string;
  status: number | null;
  onRetry: () => void;
}) {
  if (loading) return <LoadingBlock text="加载地图点位…" />;
  if (error) return <ErrorState message={error} status={status} onRetry={onRetry} />;

  const points = entries.filter((e) => e.lng !== null && e.lat !== null) as (ScheduleEntry & {
    lng: number;
    lat: number;
  })[];
  const noCoord = entries.filter((e) => e.lng === null || e.lat === null);

  if (entries.length === 0) {
    return <EmptyState title="本周无可打点条目" description="排班条目会带商家坐标显示在这里。" />;
  }

  const lngs = points.map((p) => p.lng);
  const lats = points.map((p) => p.lat);
  const minLng = Math.min(...lngs);
  const maxLng = Math.max(...lngs);
  const minLat = Math.min(...lats);
  const maxLat = Math.max(...lats);
  const spanLng = Math.max(maxLng - minLng, 0.0005);
  const spanLat = Math.max(maxLat - minLat, 0.0005);
  const px = (lng: number) => 48 + ((lng - minLng) / spanLng) * 700;
  const py = (lat: number) => 372 - ((lat - minLat) / spanLat) * 320;
  const showLabels = points.length <= 20;

  return (
    <div className="space-y-3">
      <div className="rounded-md border border-slate-200 bg-white p-3">
        <div className="flex items-center justify-between">
          <div className="text-xs font-medium text-slate-600">
            排班点位（{points.length} 个，按商家坐标绘制）
          </div>
          <div className="text-xs text-slate-400">
            无地图底图依赖：以经纬度直接投影，便于核对片区分布
          </div>
        </div>
        {points.length === 0 ? (
          <p className="mt-2 text-sm text-slate-400">本周条目均未维护商家坐标。</p>
        ) : (
          <svg viewBox="0 0 800 400" className="mt-2 w-full" role="img" aria-label="排班点位分布">
            <rect x="0" y="0" width="800" height="400" fill="#f8fafc" stroke="#e2e8f0" />
            {[0, 1, 2, 3, 4].map((i) => (
              <line
                key={`v${i}`}
                x1={48 + i * 175}
                y1="52"
                x2={48 + i * 175}
                y2="372"
                stroke="#e2e8f0"
                strokeDasharray="3 3"
              />
            ))}
            {[0, 1, 2, 3, 4].map((i) => (
              <line
                key={`h${i}`}
                x1="48"
                y1={52 + i * 80}
                x2="748"
                y2={52 + i * 80}
                stroke="#e2e8f0"
                strokeDasharray="3 3"
              />
            ))}
            <text x="48" y="42" fontSize="11" fill="#94a3b8">
              纬度 {maxLat.toFixed(4)}
            </text>
            <text x="48" y="392" fontSize="11" fill="#94a3b8">
              纬度 {minLat.toFixed(4)}
            </text>
            <text x="620" y="392" fontSize="11" fill="#94a3b8">
              经度 {minLng.toFixed(4)} → {maxLng.toFixed(4)}
            </text>
            {points.map((p) => (
              <g key={p.entry_id}>
                <circle cx={px(p.lng)} cy={py(p.lat)} r="5" fill="var(--color-brand)" opacity="0.85" />
                {showLabels ? (
                  <text x={px(p.lng) + 8} y={py(p.lat) + 4} fontSize="10" fill="#334155">
                    {p.merchant_name.length > 10
                      ? `${p.merchant_name.slice(0, 10)}…`
                      : p.merchant_name}
                  </text>
                ) : null}
              </g>
            ))}
          </svg>
        )}
      </div>

      <div className="rounded-md border border-slate-200 bg-white p-3">
        <div className="text-xs font-medium text-slate-600">点位明细</div>
        <table className="mt-2 w-full text-xs">
          <thead>
            <tr className="text-left text-slate-500">
              <th className="py-1">商家</th>
              <th className="py-1">日期</th>
              <th className="py-1">负责人</th>
              <th className="py-1">时间窗</th>
              <th className="py-1 text-right">经度</th>
              <th className="py-1 text-right">纬度</th>
            </tr>
          </thead>
          <tbody>
            {entries.map((e) => (
              <tr key={e.entry_id} className="border-t border-slate-100">
                <td className="py-1 text-slate-800">{e.merchant_name}</td>
                <td className="py-1 text-slate-600">{e.planned_date}</td>
                <td className="py-1 text-slate-600">{e.assigned_user_name ?? "—"}</td>
                <td className="py-1 text-slate-600">
                  {e.start_at && e.end_at ? `${hhmm(e.start_at)}–${hhmm(e.end_at)}` : "—"}
                </td>
                <td className="py-1 text-right font-mono text-slate-600">
                  {e.lng === null ? "—" : e.lng.toFixed(5)}
                </td>
                <td className="py-1 text-right font-mono text-slate-600">
                  {e.lat === null ? "—" : e.lat.toFixed(5)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {noCoord.length > 0 ? (
          <p className="mt-2 text-xs text-amber-700">
            有 {noCoord.length} 条未维护商家坐标，无法打点：{noCoord
              .slice(0, 5)
              .map((e) => e.merchant_name)
              .join("、")}
            {noCoord.length > 5 ? " 等" : ""}
          </p>
        ) : null}
      </div>
    </div>
  );
}
