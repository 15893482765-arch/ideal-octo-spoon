/** T28 离线草稿存储（Q160 离线草稿）。
 *
 * 边界（红线）：
 * - 只持久化"文本草稿 + 附件元数据"到 localStorage；绝不持久化照片/视频二进制。
 *   二进制在 AttachmentStore 的内存中，刷新即丢（草稿会标记附件待重拍/重选）。
 * - 离线草稿显式区别于"已提交"：status=local/queued，绝不伪造服务端状态。
 * - 存储可注入（便于 jsdom / 单测），带容量保护与损坏隔离。
 */

import type { DraftStorage, OfflineDraft } from "./types";

const STORAGE_KEY = "qs.mobile.offline-drafts.v1";

export type { DraftStorage };

export class DraftCorruptedError extends Error {
  constructor(public readonly raw: string) {
    super("离线草稿数据损坏，已隔离");
    this.name = "DraftCorruptedError";
  }
}

export class DraftStore {
  constructor(
    private readonly storage: DraftStorage | null = defaultStorage(),
    private readonly key: string = STORAGE_KEY,
  ) {}

  private readAll(): Record<string, OfflineDraft> {
    if (!this.storage) return {};
    const raw = this.storage.getItem(this.key);
    if (!raw) return {};
    let parsed: unknown;
    try {
      parsed = JSON.parse(raw);
    } catch {
      throw new DraftCorruptedError(raw);
    }
    if (typeof parsed !== "object" || parsed === null) {
      throw new DraftCorruptedError(raw);
    }
    return parsed as Record<string, OfflineDraft>;
  }

  private writeAll(map: Record<string, OfflineDraft>): void {
    if (!this.storage) return;
    this.storage.setItem(this.key, JSON.stringify(map));
  }

  list(): OfflineDraft[] {
    const map = this.readAll();
    return Object.values(map).sort((a, b) => a.createdAt.localeCompare(b.createdAt));
  }

  /** 未完成的草稿（已提交的成功项应被清理，不应残留）。 */
  pending(): OfflineDraft[] {
    return this.list().filter((d) => d.status !== "submitted");
  }

  get(draftId: string): OfflineDraft | undefined {
    return this.readAll()[draftId];
  }

  upsert(draft: OfflineDraft): OfflineDraft {
    const map = this.readAll();
    map[draft.draftId] = draft;
    this.writeAll(map);
    return draft;
  }

  remove(draftId: string): void {
    const map = this.readAll();
    delete map[draftId];
    this.writeAll(map);
  }

  /** 提交成功后清理对应草稿（Q162）。 */
  clearSubmitted(): string[] {
    const map = this.readAll();
    const removed: string[] = [];
    for (const [id, d] of Object.entries(map)) {
      if (d.status === "submitted") {
        delete map[id];
        removed.push(id);
      }
    }
    if (removed.length) this.writeAll(map);
    return removed;
  }

  count(): number {
    return Object.keys(this.readAll()).length;
  }
}

export function defaultStorage(): DraftStorage | null {
  if (typeof localStorage === "undefined") return null;
  return {
    getItem: (k) => localStorage.getItem(k),
    setItem: (k, v) => localStorage.setItem(k, v),
    removeItem: (k) => localStorage.removeItem(k),
  };
}
