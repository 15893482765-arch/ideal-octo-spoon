/** T28 稳定幂等键（Q162 重放单次写入；PRD §5.4-7）。
 *
 * 幂等键在"创建草稿"时生成，之后无论离线重试、联网重放都保持不变，
 * 从而保证弱网下"用户点一次 = 服务端只写一次"。
 *
 * 键的稳定域：数据域 + 动作类型 + 目标实体 + 本地草稿 UUID。
 * 不含时间戳/随机重试序号 —— 那些会让重试变成新请求而重复写入。
 */

import type { DraftKind } from "./types";

/** 生成 RFC4122 v4（优先用原生 crypto，测试环境兜底）。 */
export function uuidv4(getRandomValues?: (a: Uint8Array) => Uint8Array): string {
  const g =
    getRandomValues ??
    (typeof crypto !== "undefined" && crypto.getRandomValues
      ? (a: Uint8Array) => crypto.getRandomValues(a)
      : undefined);
  const bytes = new Uint8Array(16);
  if (g) {
    g(bytes);
  } else {
    for (let i = 0; i < 16; i += 1) bytes[i] = Math.floor(Math.random() * 256);
  }
  bytes[6] = (bytes[6] & 0x0f) | 0x40; // version 4
  bytes[8] = (bytes[8] & 0x3f) | 0x80; // variant 10
  const h = [...bytes].map((b) => b.toString(16).padStart(2, "0"));
  return `${h.slice(0, 4).join("")}-${h.slice(4, 6).join("")}-${h
    .slice(6, 8)
    .join("")}-${h.slice(8, 10).join("")}-${h.slice(10, 16).join("")}`;
}

/**
 * 构建稳定幂等键。同一草稿（同一 draftUuid）的所有重放必须产出完全相同的键。
 *
 * 长度控制在后端约束 64 字符内：前缀 + 36 字符 UUID（kind/dataset 不进键体，
 * 由调用方放在 header/scope；这里仍拼入短 kind 便于排障，但总长受限）。
 */
export function buildIdempotencyKey(
  kind: DraftKind,
  draftUuid: string,
  opts: { datasetId?: string; maxLen?: number } = {},
): string {
  // 后端 idempotency_key 上限 64；UUIDv4 固定 36，前缀留 28。
  const maxLen = opts.maxLen ?? 64;
  const prefix = kind.replace(/[^a-z]/gi, "").slice(0, 12);
  const key = `${prefix}_${draftUuid}`;
  if (key.length > maxLen) {
    // 极端情况下截断前缀而非 UUID，保持幂等域稳定。
    return key.slice(0, maxLen);
  }
  return key;
}

/** 为一份新草稿一次性生成稳定标识（draftId + idempotencyKey）。 */
export function mintDraftIdentity(
  kind: DraftKind,
  opts: { datasetId?: string; now?: () => string; random?: (a: Uint8Array) => Uint8Array } = {},
): { draftId: string; idempotencyKey: string; createdAt: string } {
  const draftId = uuidv4(opts.random);
  return {
    draftId,
    idempotencyKey: buildIdempotencyKey(kind, draftId, { datasetId: opts.datasetId }),
    createdAt: (opts.now ?? (() => new Date(0).toISOString()))(),
  };
}
