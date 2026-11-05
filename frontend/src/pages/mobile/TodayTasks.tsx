/** T28 业务员移动端今日任务 H5（PRD §4.2 / §4.6 / §4.7）。
 *
 * 触控优先单列布局；包含：今日任务、打卡（仅主动定位）、拍照、巡访/补货/活动入口、
 * 弱网横幅、离线草稿箱（恢复后差异确认，高风险不自动提交）。
 */

import { useMemo, useRef, useState } from "react";

import { captureLocationOnce, capturePhoto, type DeviceResult, type GeoFix } from "../../mobile/device";
import { createDraft } from "../../mobile/offline/draft-factory";
import { computeDiff, needsConfirmation } from "../../mobile/offline/diff";
import type {
  DraftDiff,
  DraftKind,
  DraftStorage,
  FetchStateFn,
  SubmitFn,
} from "../../mobile/offline/types";
import { useOfflineQueue } from "../../mobile/use-offline-queue";

export interface TodayTask {
  id: string;
  merchantName: string;
  kind: "visit" | "replenishment" | "activity";
  plannedWindow: string;
  status: string;
  version?: number;
}

const KIND_LABEL: Record<DraftKind, string> = {
  "visit.checkin": "到店打卡",
  "visit.submit": "巡访提交",
  "replenishment.fulfill": "补货核销",
  "activity.execute": "活动执行",
};

export interface TodayTasksProps {
  tasks: TodayTask[];
  submit: SubmitFn;
  fetchState: FetchStateFn;
  storage?: DraftStorage;
  isOnline?: () => boolean;
  now?: () => string;
}

export function TodayTasks({ tasks, submit, fetchState, storage, isOnline, now }: TodayTasksProps) {
  const queue = useOfflineQueue({ submit, fetchState, storage, isOnline, now });
  const fileRef = useRef<HTMLInputElement | null>(null);
  const [fix, setFix] = useState<GeoFix | null>(null);
  const [geoHint, setGeoHint] = useState<string | null>(null);
  const [photoHint, setPhotoHint] = useState<string | null>(null);
  const [activeTask, setActiveTask] = useState<TodayTask | null>(null);
  const [note, setNote] = useState("");
  const [diff, setDiff] = useState<DraftDiff | null>(null);
  const [feedback, setFeedback] = useState<string | null>(null);

  const visibleDrafts = queue.drafts;
  const activeDraft = useMemo(
    () => (diff ? queue.drafts.find((d) => d.draftId === diff.draftId) : undefined),
    [diff, queue.drafts],
  );

  /** 主动打卡：仅在点击时取一次定位，不做后台持续定位。 */
  const onCheckin = async (task: TodayTask) => {
    setGeoHint(null);
    const res = await captureLocationOnce();
    if (res.state !== "granted" || !res.data) {
      setFix(null);
      setGeoHint(res.fallbackHint ?? "定位不可用，可先保存草稿稍后重试。");
      return;
    }
    setFix(res.data);
    setActiveTask(task);
    setDiff(null);
    setFeedback(`已在 ${res.data.capturedAt} 获取定位（精度 ${Math.round(res.data.accuracyMeters)}m），提交后才计入打卡。`);
  };

  /** 主动拍照/选图；拒绝或取消时展示替代路径。 */
  const onPhoto = async () => {
    setPhotoHint(null);
    const res: DeviceResult<{ file: File }> = await capturePhoto(fileRef.current, "storefront");
    if (res.state !== "granted" || !res.data) {
      setPhotoHint(res.fallbackHint ?? "未获取到照片，可重试或从相册选择。");
      return;
    }
    const meta = queue.attachments.put(
      { kind: "storefront", mediaType: res.data.file.type, sizeBytes: res.data.file.size },
      res.data.file,
    );
    setPhotoHint(`照片已临时保存（${meta.localRef}），提交成功后自动清理。`);
  };

  /** 弱网：仅保存本地草稿（不是已提交）。 */
  const onSaveOffline = (task: TodayTask, kind: DraftKind) => {
    const draft = createDraft({
      kind,
      targetId: task.id,
      base: { status: task.status, version: task.version, fetchedAt: (now ?? (() => new Date().toISOString()))() },
      payload: { note },
      location: fix ? { lng: fix.lng, lat: fix.lat, accuracyMeters: fix.accuracyMeters } : null,
      note,
      now,
    });
    queue.saveDraft(draft);
    setFeedback("已保存到本机草稿（离线草稿，不是已提交），联网确认后才会上传。");
  };

  const onReview = async (draftId: string) => {
    const d = queue.drafts.find((x) => x.draftId === draftId);
    if (!d) return;
    const server = await fetchState(d.kind, d.targetId);
    const computed = computeDiff(d, { ...server.snapshot, status: server.status, version: server.version });
    setDiff(computed);
  };

  return (
    <div data-testid="today-tasks" className="mx-auto flex max-w-md flex-col gap-4 p-4">
      <header className="flex items-center justify-between">
        <h1 className="text-lg font-semibold">今日任务</h1>
        <span data-testid="net-state" className={queue.online ? "text-green-600" : "text-amber-600"}>
          {queue.online ? "在线" : "弱网/离线"}
        </span>
      </header>

      {!queue.online && (
        <div data-testid="offline-banner" className="rounded bg-amber-50 p-2 text-sm text-amber-800">
          当前网络不可用：提交将仅保存为本机草稿，恢复联网后需你确认再上传。
        </div>
      )}
      {queue.conflictCount > 0 && (
        <div data-testid="conflict-banner" className="rounded bg-red-50 p-2 text-sm text-red-700">
          {queue.conflictCount} 条草稿与服务端状态冲突，未被覆盖，请逐条处理。
        </div>
      )}

      {feedback && (
        <div data-testid="feedback" className="rounded bg-slate-50 p-2 text-sm">{feedback}</div>
      )}

      <section aria-label="任务列表" className="flex flex-col gap-2">
        {tasks.map((t) => (
          <article key={t.id} data-testid={`task-${t.id}`} className="rounded border p-3">
            <div className="flex items-center justify-between">
              <strong>{t.merchantName}</strong>
              <span className="text-xs text-slate-500">{t.plannedWindow}</span>
            </div>
            <div className="mt-2 flex flex-wrap gap-2">
              <button type="button" onClick={() => onCheckin(t)} data-testid={`checkin-${t.id}`}>
                到店打卡
              </button>
              <button type="button" onClick={() => setActiveTask(t)} data-testid={`visit-${t.id}`}>
                巡访
              </button>
              <button type="button" onClick={() => setActiveTask(t)} data-testid={`repl-${t.id}`}>
                补货
              </button>
              <button type="button" onClick={() => setActiveTask(t)} data-testid={`act-${t.id}`}>
                活动
              </button>
            </div>
          </article>
        ))}
      </section>

      {/* 相机/定位拒绝的替代提示 */}
      {geoHint && (
        <div role="alert" data-testid="geo-hint" className="rounded bg-orange-50 p-2 text-sm text-orange-800">
          {geoHint}
        </div>
      )}
      {photoHint && (
        <div role="alert" data-testid="photo-hint" className="rounded bg-orange-50 p-2 text-sm text-orange-800">
          {photoHint}
        </div>
      )}

      {/* 当前任务操作面板 */}
      {activeTask && (
        <section data-testid="task-panel" className="rounded border p-3">
          <h2 className="font-medium">{activeTask.merchantName} - 现场记录</h2>
          <input ref={fileRef} type="file" hidden aria-label="拍照或选择照片" />
          <div className="mt-2 flex gap-2">
            <button type="button" onClick={onPhoto} data-testid="take-photo">拍照</button>
            <span className="text-xs text-slate-500">定位：{fix ? `已获取 ${fix.lat.toFixed(5)},${fix.lng.toFixed(5)}` : "未获取（仅打卡时采集）"}</span>
          </div>
          <textarea
            aria-label="现场备注"
            className="mt-2 w-full border p-2"
            value={note}
            onChange={(e) => setNote(e.target.value)}
            placeholder="陈列/库存/活动情况…"
          />
          <div className="mt-2 flex gap-2">
            <button
              type="button"
              data-testid="save-draft"
              onClick={() => onSaveOffline(activeTask, "visit.submit")}
            >
              {queue.online ? "提交（带幂等键）" : "保存草稿"}
            </button>
            {queue.online && (
              <button
                type="button"
                data-testid="save-draft-anyway"
                onClick={() => onSaveOffline(activeTask, "visit.submit")}
              >
                仅存草稿
              </button>
            )}
          </div>
        </section>
      )}

      {/* 离线草稿箱：恢复后展示差异，用户确认再提交 */}
      <section aria-label="离线草稿箱" className="flex flex-col gap-2">
        <h2 className="font-medium">
          待同步草稿（{visibleDrafts.length}）
        </h2>
        {visibleDrafts.map((d) => (
          <div key={d.draftId} data-testid={`draft-card-${d.draftId}`} className="rounded border p-2 text-sm">
            <div className="flex items-center justify-between">
              <span>{KIND_LABEL[d.kind]}</span>
              <span data-testid={`draft-status-${d.draftId}`}>{d.status}</span>
            </div>
            {d.risk === "high" && <span className="text-xs text-red-600">高风险：需你确认后才提交</span>}
            <div className="mt-1 flex gap-2">
              <button type="button" onClick={() => onReview(d.draftId)} data-testid={`review-${d.draftId}`}>
                查看差异
              </button>
              <button
                type="button"
                onClick={() => queue.confirmAndReplay(d.draftId)}
                data-testid={`confirm-${d.draftId}`}
              >
                确认提交
              </button>
              <button type="button" onClick={() => queue.removeDraft(d.draftId)} data-testid={`discard-${d.draftId}`}>
                丢弃
              </button>
            </div>
          </div>
        ))}
      </section>

      {/* 差异确认弹层 */}
      {diff && (
        <div data-testid="diff-panel" className="rounded border-2 p-3" role="dialog" aria-label="恢复后差异确认">
          <h3 className="font-semibold">恢复联网 · 差异确认</h3>
          {diff.stateConflict && (
            <p data-testid="state-conflict" className="text-sm text-red-700">
              服务端状态已变为 {diff.serverStatus}，该提交不能覆盖，请改走对应流程。
            </p>
          )}
          {diff.fields.length === 0 && !diff.stateConflict && <p className="text-sm text-slate-600">无字段差异。</p>}
          <ul>
            {diff.fields.map((f) => (
              <li key={f.field} data-testid={`diff-field-${f.field}`}>
                {f.field}: 本机「{String(f.draftValue)}」
                {f.serverChanged ? (
                  <span className="text-red-600"> / 服务端已改为「{String(f.serverValue)}」</span>
                ) : (
                  <span className="text-slate-500"> / 服务端未变</span>
                )}
              </li>
            ))}
          </ul>
          {needsConfirmation(diff) && !diff.stateConflict && (
            <p className="text-xs text-amber-700">该动作需你显式确认后才会提交（不会自动重放）。</p>
          )}
          <div className="mt-2 flex gap-2">
            <button
              type="button"
              disabled={diff.stateConflict}
              onClick={() => {
                if (activeDraft) queue.confirmAndReplay(diff.draftId);
                setDiff(null);
              }}
              data-testid="diff-confirm"
            >
              确认覆盖提交
            </button>
            <button type="button" onClick={() => setDiff(null)} data-testid="diff-cancel">取消</button>
          </div>
        </div>
      )}
    </div>
  );
}
