/** T28 移动端弱网草稿与重放 —— 核心类型（PRD §4.2 / §4.6 / §4.7 / §9.1）。 */

/** 草稿持久层抽象（默认 localStorage；测试/隐私模式可注入内存实现）。 */
export interface DraftStorage {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
}

/** 业务员移动端可离线暂存的提交动作类型。 */
export type DraftKind =
  | "visit.submit" // 巡访打卡提交（Q079–Q083 一致性）
  | "replenishment.fulfill" // 补货履约/到货核销（Q055–Q067）
  | "activity.execute" // 地推活动现场执行（Q164）
  | "visit.checkin"; // 到店打卡（含定位，仅主动触发）

/**
 * 风险等级：决定恢复联网后是否允许自动重放。
 * - low：可在用户确认后重放（普通表单提交）
 * - high：禁止自动重放，必须用户逐条显式确认（红线：不得自动重放高风险动作）
 */
export type DraftRisk = "low" | "high";

/** 草稿生命周期状态（离线草稿绝不是"已提交"）。 */
export type DraftStatus =
  | "local" // 仅本地，尚未尝试提交
  | "queued" // 已进入待重放队列，等待用户确认/联网
  | "submitting" // 正在提交（单次占用，防重复）
  | "submitted" // 服务端确认成功，等待清理
  | "conflict" // 服务端返回冲突，需用户处理，禁止静默覆盖
  | "failed"; // 可重试失败（非冲突）

/** 待上传附件的暂存描述。生产附件只做临时暂存，不长期持久化到浏览器缓存。 */
export interface PendingAttachment {
  /** 本地临时引用（objectURL 或临时文件句柄标识），非服务端 file_id。 */
  localRef: string;
  /** 用途：门头/陈列/冰柜/补货凭证等。 */
  kind: string;
  /** 媒体类型，如 image/jpeg。 */
  mediaType: string;
  /** 字节数；用于临时暂存配额与提示。 */
  sizeBytes: number;
  /** 上传成功后回填的服务端 file_id。 */
  fileId?: string;
  status: "pending" | "uploading" | "uploaded" | "failed";
}

/**
 * 一份离线草稿。
 * 关键字段：
 * - idempotencyKey：创建草稿时即生成的稳定键，重放/重试都不变（Q162）。
 * - base：用户开始编辑时拉取的服务端基线（用于恢复后差异展示，Q161）。
 * - payload：本次提交内容。
 */
export interface OfflineDraft {
  /** 本地草稿主键（同时用于幂等键派生的业务实体标识）。 */
  draftId: string;
  kind: DraftKind;
  /** 目标资源（如巡访任务 id / 补货单 id / 活动 id）。 */
  targetId: string;
  status: DraftStatus;
  risk: DraftRisk;
  idempotencyKey: string;
  /** 编辑所基于的服务端版本/状态（冲突检测用）。 */
  base: {
    status: string;
    version?: number | string;
    fetchedAt: string;
    /** 离线开始时拉取的服务端字段快照（用于恢复后逐字段差异，Q161）。 */
    snapshot?: Record<string, unknown>;
  };
  payload: Record<string, unknown>;
  attachments: PendingAttachment[];
  /** 精确定位仅在用户主动打卡时采集，否则为 null（红线：不做后台持续定位）。 */
  location: { lng: number; lat: number; accuracyMeters?: number } | null;
  note?: string;
  createdAt: string;
  updatedAt: string;
  /** 最近一次提交尝试结果（用于差异/冲突展示）。 */
  lastResult?: {
    httpStatus?: number;
    code?: string;
    serverStatus?: string;
    serverVersion?: number | string;
    at: string;
  };
  attempts: number;
}

/** 恢复联网后，单条草稿与服务端当前状态的字段级差异。 */
export interface FieldDiff {
  field: string;
  baseValue: unknown;
  draftValue: unknown;
  serverValue: unknown;
  /** 服务端已在离线期间被改动 → true（潜在冲突，需要用户确认）。 */
  serverChanged: boolean;
}

export interface DraftDiff {
  draftId: string;
  kind: DraftKind;
  targetId: string;
  risk: DraftRisk;
  fields: FieldDiff[];
  /** 是否存在服务端状态机层面的冲突（如单据已关闭/已提交）。 */
  stateConflict: boolean;
  serverStatus?: string;
}

/** 提交器抽象：由实际 API 层提供，弱网引擎不直接耦合 fetch。 */
export interface SubmitResult {
  ok: boolean;
  httpStatus: number;
  code?: string;
  /** 服务端当前状态/版本（冲突时回填，供 Q163 展示）。 */
  serverStatus?: string;
  serverVersion?: number | string;
  body?: unknown;
}

export type SubmitFn = (draft: OfflineDraft) => Promise<SubmitResult>;

/** 服务端状态读取器：恢复后拉取目标资源当前状态以计算差异。 */
export type FetchStateFn = (
  kind: DraftKind,
  targetId: string,
) => Promise<{ status: string; version?: number | string; snapshot: Record<string, unknown> }>;
