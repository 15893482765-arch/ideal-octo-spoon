/** T28 待上传附件临时暂存（红线：不得把生产附件持久放浏览器缓存）。
 *
 * 策略：
 * - 二进制（Blob）只保存在内存 Map，绝不写入 localStorage / IndexedDB / CacheStorage；
 *   页面关闭/刷新即释放，满足"临时暂存、非持久化"。
 * - 仅维护"附件元数据"（类型/大小/状态），元数据随草稿走，草稿提交成功或丢弃即清理。
 * - 提供配额上限，防止弱网积压把内存撑爆。
 */

import type { PendingAttachment } from "./types";

export const DEFAULT_MAX_TOTAL_BYTES = 20 * 1024 * 1024; // 单设备临时暂存上限 20MB

export class AttachmentQuotaExceeded extends Error {
  constructor(public readonly limitBytes: number, public readonly askedBytes: number) {
    super(`待上传附件超过临时暂存上限 ${limitBytes} 字节（本次需要 ${askedBytes} 字节）`);
    this.name = "AttachmentQuotaExceeded";
  }
}

interface BlobEntry {
  blob: Blob;
  /** objectURL（若已创建），用于 <img>/<video> 预览。 */
  objectUrl?: string;
}

export class AttachmentStore {
  private blobs = new Map<string, BlobEntry>();
  private totalBytes = 0;

  constructor(private readonly maxTotalBytes: number = DEFAULT_MAX_TOTAL_BYTES) {}

  /** 暂存一个待上传文件，返回可持久的元数据（不含二进制）。 */
  put(
    meta: Omit<PendingAttachment, "localRef" | "status">,
    blob: Blob,
    opts: { createObjectUrl?: (b: Blob) => string } = {},
  ): PendingAttachment {
    if (this.totalBytes + blob.size > this.maxTotalBytes) {
      throw new AttachmentQuotaExceeded(this.maxTotalBytes, this.totalBytes + blob.size);
    }
    const localRef = `att-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
    const objectUrl = opts.createObjectUrl ? opts.createObjectUrl(blob) : undefined;
    this.blobs.set(localRef, { blob, objectUrl });
    this.totalBytes += blob.size;
    return {
      localRef,
      kind: meta.kind,
      mediaType: meta.mediaType,
      sizeBytes: blob.size,
      status: "pending",
    };
  }

  getBlob(localRef: string): Blob | undefined {
    return this.blobs.get(localRef)?.blob;
  }

  getObjectUrl(localRef: string): string | undefined {
    return this.blobs.get(localRef)?.objectUrl;
  }

  /** 上传成功后只回填 file_id，二进制仍保留到整单提交成功再释放。 */
  markUploaded(localRef: string, fileId: string): void {
    const entry = this.blobs.get(localRef);
    if (!entry) return;
    // 通过事件回调更新元数据由上层完成；此处无需改动二进制。
    void entry;
    void fileId;
  }

  /** 释放单个附件（提交成功或用户删除）。 */
  release(localRef: string, revokeUrl?: (url: string) => void): void {
    const entry = this.blobs.get(localRef);
    if (!entry) return;
    if (entry.objectUrl && revokeUrl) revokeUrl(entry.objectUrl);
    this.totalBytes -= entry.blob.size;
    this.blobs.delete(localRef);
  }

  /** 释放一批（整单提交成功后清理对应草稿附件，Q162 成功后清理）。 */
  releaseMany(localRefs: string[], revokeUrl?: (url: string) => void): void {
    for (const ref of localRefs) this.release(ref, revokeUrl);
  }

  /** 全部清空（登出/丢弃全部草稿）。 */
  clear(revokeUrl?: (url: string) => void): void {
    if (revokeUrl) {
      for (const entry of this.blobs.values()) {
        if (entry.objectUrl) revokeUrl(entry.objectUrl);
      }
    }
    this.blobs.clear();
    this.totalBytes = 0;
  }

  get usedBytes(): number {
    return this.totalBytes;
  }

  get count(): number {
    return this.blobs.size;
  }
}
