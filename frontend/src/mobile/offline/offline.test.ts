/** T28 弱网草稿与重放引擎单测：Q160–Q163。 */

import { beforeEach, describe, expect, it, vi } from "vitest";

import { AttachmentStore, AttachmentQuotaExceeded } from "./attachment-store";
import { DraftCorruptedError, DraftStore } from "./draft-store";
import { createDraft } from "./draft-factory";
import { computeDiff, needsConfirmation, stableEqual } from "./diff";
import { buildIdempotencyKey, mintDraftIdentity, uuidv4 } from "./idempotency";
import { SyncCoordinator } from "./sync-coordinator";
import type { DraftStorage, FetchStateFn, OfflineDraft, SubmitFn, SubmitResult } from "./types";

function memStorage(): DraftStorage & { dump(): Record<string, string> } {
  const m = new Map<string, string>();
  return {
    getItem: (k) => (m.has(k) ? (m.get(k) as string) : null),
    setItem: (k, v) => void m.set(k, v),
    removeItem: (k) => void m.delete(k),
    dump: () => Object.fromEntries(m),
  };
}

const fixedRandom = (a: Uint8Array) => {
  for (let i = 0; i < a.length; i += 1) a[i] = (i * 17 + 3) % 256;
  return a;
};
const now = () => "2026-10-15T01:00:00.000Z";

function makeDraft(over: Partial<OfflineDraft> = {}): OfflineDraft {
  return createDraft({
    kind: "activity.execute",
    targetId: "act-1",
    base: { status: "DRAFT", version: 1, snapshot: { remark: "old", boxes: 10 } },
    payload: { remark: "new", boxes: 12 },
    now,
    random: fixedRandom,
    ...over,
  });
}

describe("Q160 离线草稿", () => {
  it("离线时仅本地保存草稿，状态是 local 而非已提交", () => {
    const store = new DraftStore(memStorage());
    const d = makeDraft();
    expect(d.status).toBe("local");
    store.upsert(d);
    expect(store.pending()).toHaveLength(1);
    // 绝不出现伪造的 submitted
    expect(store.get(d.draftId)?.status).not.toBe("submitted");
  });

  it("稳定幂等键：同一草稿重复生成结果一致，且不随时间变化", () => {
    const id1 = mintDraftIdentity("visit.submit", { random: fixedRandom, now });
    const key1a = buildIdempotencyKey("visit.submit", id1.draftId);
    const key1b = buildIdempotencyKey("visit.submit", id1.draftId);
    expect(key1a).toBe(key1b);
    expect(key1a.length).toBeLessThanOrEqual(64);
    // 不同草稿身份 → 不同键
    const otherRandom = (a: Uint8Array) => {
      for (let i = 0; i < a.length; i += 1) a[i] = (i * 7 + 1) % 256;
      return a;
    };
    const id2 = mintDraftIdentity("visit.submit", { random: otherRandom });
    expect(buildIdempotencyKey("visit.submit", id2.draftId)).not.toBe(key1a);
  });

  it("生产附件二进制只进内存、不进持久草稿存储", () => {
    const store = new DraftStore(memStorage());
    const att = new AttachmentStore();
    const meta = att.put(
      { kind: "storefront", mediaType: "image/jpeg", sizeBytes: 0 },
      new Blob(["binary"], { type: "image/jpeg" }),
    );
    const d = makeDraft({ attachments: [meta] });
    store.upsert(d);
    const raw = JSON.stringify(store.list());
    // 持久层只有 localRef 元数据，不含二进制内容
    expect(raw).toContain(meta.localRef);
    expect(raw).not.toContain("binary");
    // 内存里可取回二进制
    expect(att.getBlob(meta.localRef)).toBeInstanceOf(Blob);
  });

  it("附件超临时配额直接拒绝（不弱网积压撑爆内存）", () => {
    const att = new AttachmentStore(10);
    expect(() =>
      att.put({ kind: "display", mediaType: "image/jpeg", sizeBytes: 0 }, new Blob(["12345678901"])),
    ).toThrow(AttachmentQuotaExceeded);
  });

  it("损坏的持久草稿被隔离而不是崩溃", () => {
    const s = memStorage();
    s.setItem("qs.mobile.offline-drafts.v1", "{not-json");
    const store = new DraftStore(s);
    expect(() => store.list()).toThrow(DraftCorruptedError);
  });

  it("精确定位默认不采集（只有主动打卡才带 location）", () => {
    const d = makeDraft();
    expect(d.location).toBeNull();
    const withFix = makeDraft({ location: { lng: 116.4, lat: 39.9, accuracyMeters: 12 } });
    expect(withFix.location?.lat).toBe(39.9);
  });
});

describe("Q161 恢复后确认（差异展示）", () => {
  it("逐字段标出本地改动；服务端离线期间未改 → 非冲突", () => {
    const d = makeDraft();
    const diff = computeDiff(d, { status: "DRAFT", version: 1, remark: "old", boxes: 10 });
    const fields = Object.fromEntries(diff.fields.map((f) => [f.field, f]));
    expect(fields.remark.draftValue).toBe("new");
    expect(fields.remark.serverChanged).toBe(false);
    expect(fields.boxes.serverChanged).toBe(false);
    expect(diff.stateConflict).toBe(false);
    // 无服务端改动 + low 风险 → 无需强制确认
    expect(needsConfirmation(diff)).toBe(false);
  });

  it("服务端离线期间改过同一字段 → serverChanged，需要确认", () => {
    const d = makeDraft();
    const diff = computeDiff(d, { status: "DRAFT", version: 1, remark: "manager-edited", boxes: 10 });
    expect(diff.fields.find((f) => f.field === "remark")?.serverChanged).toBe(true);
    expect(needsConfirmation(diff)).toBe(true);
  });

  it("高风险动作即使无差异也必须用户确认，不自动提交", () => {
    const d = createDraft({
      kind: "replenishment.fulfill",
      targetId: "ro-1",
      base: { status: "CONFIRMED", version: 1, snapshot: { delivered_boxes: 5 } },
      payload: { delivered_boxes: 5 },
      now,
      random: fixedRandom,
    });
    expect(d.risk).toBe("high");
    const diff = computeDiff(d, { status: "CONFIRMED", version: 1, delivered_boxes: 5 });
    expect(diff.stateConflict).toBe(false);
    expect(needsConfirmation(diff)).toBe(true); // high
  });

  it("stableEqual 对键序不同的对象判等", () => {
    expect(stableEqual({ a: 1, b: 2 }, { b: 2, a: 1 })).toBe(true);
    expect(stableEqual({ a: 1 }, { a: 2 })).toBe(false);
  });
});

describe("Q162 重放单次写入", () => {
  let store: DraftStore;
  let att: AttachmentStore;

  beforeEach(() => {
    store = new DraftStore(memStorage());
    att = new AttachmentStore();
  });

  it("成功提交后清理对应草稿并释放附件；幂等键随请求发送", async () => {
    const meta = att.put({ kind: "storefront", mediaType: "image/jpeg", sizeBytes: 0 }, new Blob(["x"]));
    const d = makeDraft({ attachments: [meta] });
    store.upsert(d);
    const seenKeys: string[] = [];
    const submit: SubmitFn = vi.fn(async (draft) => {
      seenKeys.push(draft.idempotencyKey);
      return { ok: true, httpStatus: 201, body: { id: "new" } } as SubmitResult;
    });
    const fetchState: FetchStateFn = vi.fn(async () => ({
      status: "DRAFT",
      version: 1,
      snapshot: { remark: "old", boxes: 10 },
    }));
    const coord = new SyncCoordinator(store, att, submit, fetchState, { now });

    const out = await coord.replay(d.draftId);
    expect(out.result).toBe("submitted");
    expect(store.pending()).toHaveLength(0); // 草稿已清理
    expect(att.count).toBe(0); // 附件已释放
    expect(seenKeys).toEqual([d.idempotencyKey]);
  });

  it("同一草稿并发重放只产生一次写（单次写入）", async () => {
    const d = makeDraft();
    store.upsert(d);
    let calls = 0;
    const submit: SubmitFn = vi.fn(async () => {
      calls += 1;
      await new Promise((r) => setTimeout(r, 5));
      return { ok: true, httpStatus: 201 } as SubmitResult;
    });
    const fetchState: FetchStateFn = vi.fn(async () => ({
      status: "DRAFT", version: 1, snapshot: { remark: "old", boxes: 10 },
    }));
    const coord = new SyncCoordinator(store, att, submit, fetchState, { now });
    const [a, b] = await Promise.all([coord.replay(d.draftId), coord.replay(d.draftId)]);
    expect(calls).toBe(1);
    expect(a.result).toBe("submitted");
    expect(b.result).toBe("submitted");
  });

  it("网络失败保留草稿且幂等键不变，可同键重试", async () => {
    const d = makeDraft();
    store.upsert(d);
    const submit: SubmitFn = vi.fn(async () => {
      throw new Error("offline");
    });
    const fetchState: FetchStateFn = vi.fn(async () => ({
      status: "DRAFT", version: 1, snapshot: { remark: "old", boxes: 10 },
    }));
    const coord = new SyncCoordinator(store, att, submit, fetchState, { now });
    const out = await coord.replay(d.draftId);
    expect(out.result).toBe("failed");
    const kept = store.get(d.draftId);
    expect(kept?.status).toBe("failed");
    expect(kept?.idempotencyKey).toBe(d.idempotencyKey);
    expect(kept?.attempts).toBe(1);
  });

  it("服务端幂等重放（replayed=true）也视为成功并清理，不重复创建", async () => {
    const d = makeDraft();
    store.upsert(d);
    const submit: SubmitFn = vi.fn(async () => ({
      ok: true, httpStatus: 200, body: { replayed: true },
    }));
    const fetchState: FetchStateFn = vi.fn(async () => ({
      status: "DRAFT", version: 1, snapshot: { remark: "old", boxes: 10 },
    }));
    const coord = new SyncCoordinator(store, att, submit, fetchState, { now });
    const out = await coord.replay(d.draftId);
    expect(out.result).toBe("submitted");
    expect(out.replayed).toBe(true);
    expect(store.pending()).toHaveLength(0);
  });

  it("replayAll 只自动提交 low 且无冲突草稿，high 一律跳过等确认", async () => {
    const low = makeDraft(); // activity.execute = low
    const highRandom = (a: Uint8Array) => {
      for (let i = 0; i < a.length; i += 1) a[i] = (i * 11 + 5) % 256;
      return a;
    };
    const high = createDraft({
      kind: "visit.submit", targetId: "vt-1",
      base: { status: "PLANNED", version: 1, snapshot: { note: "x" } },
      payload: { note: "y" }, now, random: highRandom,
    });
    store.upsert(low);
    store.upsert(high);
    const submit: SubmitFn = vi.fn(async () => ({ ok: true, httpStatus: 201 }));
    const fetchState: FetchStateFn = vi.fn(async (kind) =>
      kind === "activity.execute"
        ? { status: "DRAFT", version: 1, snapshot: { remark: "old", boxes: 10 } }
        : { status: "PLANNED", version: 1, snapshot: { note: "x" } },
    );
    const coord = new SyncCoordinator(store, att, submit, fetchState, { now });
    const outcomes = await coord.replayAll();
    const byId = Object.fromEntries(outcomes.map((o) => [o.draftId, o.result]));
    expect(byId[low.draftId]).toBe("submitted");
    expect(byId[high.draftId]).toBe("skipped");
    // high 草稿仍保留
    expect(store.get(high.draftId)?.status).toBe("local");
    // 用户显式确认后 high 才提交
    const confirmed = await coord.replay(high.draftId, true);
    expect(confirmed.result).toBe("submitted");
  });
});

describe("Q163 服务端冲突不覆盖", () => {
  it("服务端状态已推进到终态：草稿置 conflict，不发送覆盖写", async () => {
    const store = new DraftStore(memStorage());
    const att = new AttachmentStore();
    const d = createDraft({
      kind: "replenishment.fulfill", targetId: "ro-9",
      base: { status: "CONFIRMED", version: 1, snapshot: { delivered_boxes: 5 } },
      payload: { delivered_boxes: 5 }, now, random: fixedRandom,
    });
    store.upsert(d);
    const submit = vi.fn(async () => ({ ok: true, httpStatus: 201 }));
    // 服务端已经 FULFILLED（终态）
    const fetchState: FetchStateFn = vi.fn(async () => ({
      status: "FULFILLED", version: 2, snapshot: { delivered_boxes: 5 },
    }));
    const coord = new SyncCoordinator(store, att, submit, fetchState, { now });
    const out = await coord.replay(d.draftId, true); // 即使 confirmed 也不覆盖
    expect(out.result).toBe("conflict");
    expect(submit).not.toHaveBeenCalled();
    expect(store.get(d.draftId)?.status).toBe("conflict");
    expect(store.get(d.draftId)?.lastResult?.serverStatus).toBe("FULFILLED");
  });

  it("提交时服务端返回 409 幂等参数冲突：保留草稿与服务端状态，不覆盖", async () => {
    const store = new DraftStore(memStorage());
    const att = new AttachmentStore();
    const d = makeDraft();
    store.upsert(d);
    const submit: SubmitFn = vi.fn(async () => ({
      ok: false, httpStatus: 409, code: "IDEMPOTENCY_PARAMS_CONFLICT",
      serverStatus: "DRAFT", serverVersion: 1,
    }));
    const fetchState: FetchStateFn = vi.fn(async () => ({
      status: "DRAFT", version: 1, snapshot: { remark: "old", boxes: 10 },
    }));
    const coord = new SyncCoordinator(store, att, submit, fetchState, { now });
    const out = await coord.replay(d.draftId, true);
    expect(out.result).toBe("conflict");
    expect(out.code).toBe("IDEMPOTENCY_PARAMS_CONFLICT");
    const kept = store.get(d.draftId);
    expect(kept?.status).toBe("conflict");
    expect(kept?.lastResult?.code).toBe("IDEMPOTENCY_PARAMS_CONFLICT");
  });
});

describe("uuid 兜底", () => {
  it("无 crypto 时用注入随机源也能生成 v4", () => {
    const id = uuidv4(fixedRandom);
    expect(id).toHaveLength(36);
    expect(id[14]).toBe("4");
  });
});
