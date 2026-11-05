/** T28 重放协调器（Q161/Q162/Q163）。
 *
 * 职责与红线：
 * - 每次提交使用草稿创建时的稳定幂等键；成功后清理对应草稿（连同附件）。
 * - 绝不静默覆盖：服务端返回 409 / 状态冲突时，草稿置 conflict 并回带服务端状态。
 * - 不自动重放高风险动作：replayAll 只处理 low 风险且无需确认者；high 必须逐条显式确认。
 * - 单次写入：submitting 占用 + per-key Promise 去重，重复点击/重放不会发出第二次写。
 * - 幂等重放安全返回：后端对同键同参回放上一次结果（replayed），视为成功且不重复创建。
 */

import { AttachmentStore } from "./attachment-store";
import { computeDiff, needsConfirmation } from "./diff";
import { DraftStore } from "./draft-store";
import type {
  DraftDiff,
  FetchStateFn,
  OfflineDraft,
  SubmitFn,
  SubmitResult,
} from "./types";

/** 视为"冲突、需用户处理、禁止覆盖"的服务端码 / HTTP 状态。 */
const CONFLICT_CODES = new Set([
  "IDEMPOTENCY_PARAMS_CONFLICT",
  "IDEMPOTENCY_IN_PROGRESS",
  "STATE_CONFLICT",
  "RESOURCE_VERSION_CONFLICT",
]);

export interface ReplayOutcome {
  draftId: string;
  result: "submitted" | "conflict" | "skipped" | "failed";
  replayed?: boolean;
  diff?: DraftDiff;
  httpStatus?: number;
  code?: string;
}

export class SyncCoordinator {
  private inFlight = new Map<string, Promise<ReplayOutcome>>();

  constructor(
    private readonly drafts: DraftStore,
    private readonly attachments: AttachmentStore,
    private readonly submit: SubmitFn,
    private readonly fetchState: FetchStateFn,
    private readonly opts: { now?: () => string; revokeUrl?: (u: string) => void } = {},
  ) {}

  private now(): string {
    return (this.opts.now ?? (() => new Date().toISOString()))();
  }

  /** 恢复联网后评估一条草稿：拉服务端状态算差异，决定是否需要用户确认。 */
  async evaluate(draftId: string): Promise<DraftDiff> {
    const draft = this.requireDraft(draftId);
    const server = await this.fetchState(draft.kind, draft.targetId);
    const diff = computeDiff(draft, { ...server.snapshot, status: server.status, version: server.version });
    return diff;
  }

  /**
   * 重放单条。
   * @param confirmed 用户是否已确认差异（高风险/有冲突时必须 true，否则拒绝发送）。
   */
  replay(draftId: string, confirmed = false): Promise<ReplayOutcome> {
    // 单次写入去重：同一草稿已有在途提交时，复用同一个 Promise，不发第二次写。
    const existing = this.inFlight.get(draftId);
    if (existing) return existing;

    const p = this.doReplay(draftId, confirmed).finally(() => {
      this.inFlight.delete(draftId);
    });
    this.inFlight.set(draftId, p);
    return p;
  }

  private async doReplay(draftId: string, confirmed: boolean): Promise<ReplayOutcome> {
    const draft = this.requireDraft(draftId);

    if (draft.status === "submitting") {
      return { draftId, result: "skipped", code: "ALREADY_SUBMITTING" };
    }

    // 先评估差异：高风险或服务端已变动时，未经用户显式确认不得提交（红线）。
    const diff = await this.evaluate(draftId);
    if (!confirmed && needsConfirmation(diff)) {
      return { draftId, result: "skipped", diff, code: "CONFIRMATION_REQUIRED" };
    }
    if (diff.stateConflict) {
      // 即使确认过，状态机终态冲突也不允许覆盖：仍需用户改走其它流程。
      const updated = this.markConflict(draft, {
        ok: false,
        httpStatus: 409,
        code: "STATE_CONFLICT",
        serverStatus: diff.serverStatus,
      });
      void updated;
      return {
        draftId,
        result: "conflict",
        diff,
        httpStatus: 409,
        code: "STATE_CONFLICT",
      };
    }

    return this.send(draft, diff);
  }

  private async send(draft: OfflineDraft, diff: DraftDiff): Promise<ReplayOutcome> {
    const submitting: OfflineDraft = { ...draft, status: "submitting", attempts: draft.attempts + 1 };
    this.drafts.upsert(submitting);

    let res: SubmitResult;
    try {
      res = await this.submit(submitting);
    } catch {
      // 网络异常：保留为 failed 草稿，幂等键不变，允许稍后同键重试。
      this.drafts.upsert({
        ...submitting,
        status: "failed",
        lastResult: { httpStatus: 0, code: "NETWORK_ERROR", at: this.now() },
      });
      return { draftId: draft.draftId, result: "failed", code: "NETWORK_ERROR", diff };
    }

    if (res.ok) {
      // 成功（含服务端幂等重放 replayed）：标记 submitted → 清理草稿与临时附件。
      const replayed = (res.body as { replayed?: boolean } | null)?.replayed === true;
      this.drafts.upsert({
        ...submitting,
        status: "submitted",
        lastResult: {
          httpStatus: res.httpStatus,
          code: res.code,
          serverStatus: res.serverStatus,
          serverVersion: res.serverVersion,
          at: this.now(),
        },
      });
      this.attachments.releaseMany(
        submitting.attachments.map((a) => a.localRef),
        this.opts.revokeUrl,
      );
      this.drafts.clearSubmitted();
      return { draftId: draft.draftId, result: "submitted", replayed, httpStatus: res.httpStatus, diff };
    }

    if (res.httpStatus === 409 || (res.code && CONFLICT_CODES.has(res.code))) {
      // 冲突不覆盖：保留草稿 + 服务端状态，等待用户在差异界面处理。
      this.markConflict(submitting, res);
      return { draftId: draft.draftId, result: "conflict", httpStatus: res.httpStatus, code: res.code, diff };
    }

    // 其它失败：幂等键不变，可重试。
    this.drafts.upsert({
      ...submitting,
      status: "failed",
      lastResult: {
        httpStatus: res.httpStatus,
        code: res.code,
        serverStatus: res.serverStatus,
        at: this.now(),
      },
    });
    return { draftId: draft.draftId, result: "failed", httpStatus: res.httpStatus, code: res.code, diff };
  }

  private markConflict(draft: OfflineDraft, res: SubmitResult): OfflineDraft {
    const updated: OfflineDraft = {
      ...draft,
      status: "conflict",
      lastResult: {
        httpStatus: res.httpStatus,
        code: res.code,
        serverStatus: res.serverStatus,
        serverVersion: res.serverVersion,
        at: this.now(),
      },
    };
    this.drafts.upsert(updated);
    return updated;
  }

  /**
   * 恢复联网后的自动处理：只重放 low 风险、无差异、无需确认的草稿。
   * 高风险或需确认的一律保留，返回 skipped，由 UI 引导用户逐条确认（红线）。
   */
  async replayAll(): Promise<ReplayOutcome[]> {
    const outcomes: ReplayOutcome[] = [];
    for (const draft of this.drafts.pending()) {
      if (draft.status === "submitting") {
        outcomes.push({ draftId: draft.draftId, result: "skipped", code: "ALREADY_SUBMITTING" });
        continue;
      }
      // 串行重放，避免弱网恢复瞬间并发写；自动通道不传 confirmed。
      outcomes.push(await this.replay(draft.draftId, false));
    }
    return outcomes;
  }

  private requireDraft(draftId: string): OfflineDraft {
    const draft = this.drafts.get(draftId);
    if (!draft) throw new Error(`草稿不存在: ${draftId}`);
    return draft;
  }
}
