/** T28 恢复联网后的差异计算（Q161 恢复后确认；Q163 冲突不覆盖）。 */

import type { DraftDiff, FieldDiff, OfflineDraft } from "./types";

/** 各动作在状态机层面的"终态"——草稿基线若已推进到这些状态，禁止再提交。 */
const TERMINAL_STATUS: Record<string, readonly string[]> = {
  "visit.submit": ["SUBMITTED", "JUDGED_VALID", "JUDGED_INVALID", "CANCELLED"],
  "visit.checkin": ["SUBMITTED", "JUDGED_VALID", "JUDGED_INVALID", "CANCELLED"],
  "replenishment.fulfill": ["FULFILLED", "CANCELLED", "CLOSED"],
  "activity.execute": ["EXECUTED", "CANCELLED", "SETTLED"],
};

function isTerminal(kind: string, status: string): boolean {
  return (TERMINAL_STATUS[kind] ?? []).includes(status);
}

/**
 * 计算单条草稿相对服务端当前状态的差异。
 * @param draft 本地草稿（base = 离线开始时的服务端快照）
 * @param serverSnapshot 恢复后拉取的服务端当前快照（含 status/version/字段）
 */
export function computeDiff(
  draft: OfflineDraft,
  serverSnapshot: Record<string, unknown>,
): DraftDiff {
  const fields: FieldDiff[] = [];

  // payload 内字段逐一比对：base(离线前) vs draft(本地修改) vs server(当前)
  for (const [field, draftValue] of Object.entries(draft.payload)) {
    const baseValue = resolve(draft.base, field);
    const serverValue = serverSnapshot[field];
    const serverChanged = !stableEqual(baseValue, serverValue);
    const locallyChanged = !stableEqual(baseValue, draftValue);
    // 只列出"本地改过"的字段；没动过的字段不制造噪音。
    if (locallyChanged) {
      fields.push({ field, baseValue, draftValue, serverValue, serverChanged });
    }
  }

  const serverStatus = typeof serverSnapshot.status === "string"
    ? (serverSnapshot.status as string)
    : undefined;

  // 状态机冲突：服务端已进入终态，或版本被推进 → 不允许静默覆盖。
  const versionChanged =
    draft.base.version !== undefined &&
    serverSnapshot.version !== undefined &&
    draft.base.version !== serverSnapshot.version;
  const stateConflict =
    (serverStatus !== undefined && isTerminal(draft.kind, serverStatus)) ||
    (serverStatus !== undefined && serverStatus !== draft.base.status) ||
    versionChanged;

  return {
    draftId: draft.draftId,
    kind: draft.kind,
    targetId: draft.targetId,
    risk: draft.risk,
    fields,
    stateConflict,
    serverStatus,
  };
}

/** 差异是否需要用户显式确认：高风险动作、服务端有改动、或状态机冲突。 */
export function needsConfirmation(diff: DraftDiff): boolean {
  if (diff.stateConflict) return true;
  if (diff.risk === "high") return true;
  return diff.fields.some((f) => f.serverChanged);
}

function resolve(base: OfflineDraft["base"], field: string): unknown {
  // base 快照里可带 snapshot；简单实现先只看 status/version。
  const snapshot = (base as unknown as { snapshot?: Record<string, unknown> }).snapshot;
  if (snapshot && field in snapshot) return snapshot[field];
  return undefined;
}

/** 结构稳定比较（JSON 规范化），满足弱网场景即可，不做深引用相等。 */
export function stableEqual(a: unknown, b: unknown): boolean {
  if (a === b) return true;
  if (typeof a !== typeof b) return false;
  if (a && b && typeof a === "object") {
    return canonical(a) === canonical(b);
  }
  return false;
}

function canonical(v: unknown): string {
  if (v && typeof v === "object") {
    const obj = v as Record<string, unknown>;
    const keys = Object.keys(obj).sort();
    return `{${keys.map((k) => `${JSON.stringify(k)}:${canonical(obj[k])}`).join(",")}}`;
  }
  return JSON.stringify(v);
}
