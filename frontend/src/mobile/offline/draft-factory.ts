/** T28 草稿工厂：统一构造离线草稿并绑定稳定幂等键。 */

import { mintDraftIdentity } from "./idempotency";
import type {
  DraftKind,
  DraftRisk,
  OfflineDraft,
  PendingAttachment,
} from "./types";

/** 各移动动作的默认风险等级。涉及钱/不可逆状态推进的列为 high（不自动重放）。 */
export const KIND_RISK: Record<DraftKind, DraftRisk> = {
  "visit.checkin": "high", // 打卡含定位证据，必须本人主动确认
  "visit.submit": "high", // 巡访提交生成不可覆盖的原始证据
  "replenishment.fulfill": "high", // 到货核销涉及库存/钱
  "activity.execute": "low", // 活动现场记录，可在无冲突时确认重放
};

export interface CreateDraftInput {
  kind: DraftKind;
  targetId: string;
  base: {
    status: string;
    version?: number | string;
    fetchedAt?: string;
    snapshot?: Record<string, unknown>;
  };
  payload?: Record<string, unknown>;
  attachments?: PendingAttachment[];
  location?: { lng: number; lat: number; accuracyMeters?: number } | null;
  note?: string;
  risk?: DraftRisk;
  datasetId?: string;
  now?: () => string;
  random?: (a: Uint8Array) => Uint8Array;
}

export function createDraft(input: CreateDraftInput): OfflineDraft {
  const now = input.now ?? (() => new Date().toISOString());
  const ts = now();
  const ident = mintDraftIdentity(input.kind, { datasetId: input.datasetId, now: input.now, random: input.random });
  return {
    draftId: ident.draftId,
    kind: input.kind,
    targetId: input.targetId,
    status: "local",
    risk: input.risk ?? KIND_RISK[input.kind],
    idempotencyKey: ident.idempotencyKey,
    base: {
      status: input.base.status,
      version: input.base.version,
      fetchedAt: input.base.fetchedAt ?? ts,
      snapshot: input.base.snapshot,
    },
    payload: input.payload ?? {},
    attachments: input.attachments ?? [],
    // 精确定位默认 null：只有用户主动打卡才传入（红线：不做后台持续定位）。
    location: input.location ?? null,
    note: input.note,
    createdAt: ts,
    updatedAt: ts,
    attempts: 0,
  };
}
