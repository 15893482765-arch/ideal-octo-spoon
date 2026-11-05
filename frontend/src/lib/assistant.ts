/** AI 助手对话客户端（PRD §5.1）。
 *
 * 约定：
 * - 只调用受控只读接口 `/api/v1/assistant/chat`，不接受任何「执行/下单/改数据」意图；
 * - 身份与数据范围由后端从 Bearer token 注入，前端不得传 role/region/brand；
 * - `session_id` 由前端生成，仅用于把同一轮对话的审计串起来，不含业务含义。
 */

import { apiFetch } from "./api";

export interface AssistantNarrative {
  headline: string;
  summary: string;
  assumptions: string[];
  confidence: "LOW" | "MEDIUM" | "HIGH";
}

/** A1 草稿引用：只指向暂存表，`target_path` 用于「去表单确认」。 */
export interface AssistantDraft {
  id: string;
  kind: string;
  status: string;
  target_path: string;
  target_label: string;
}

/** 回复命中的业务记录引用：右栏凭 object_id 拉详情，path 指向业务页面。 */
export interface AssistantRecordRef {
  object_type: string;
  object_id: string;
  path: string | null;
  label: string;
}

/** 图表系列：data 中的 null 表示该分类确实缺数据（断点），不做补点。 */
export interface AssistantChartSeries {
  name: string;
  data: (number | null)[];
}

/** A2 图表：与 reply 同源，由服务端按确定性规则生成；无数据时后端不返回。 */
export interface AssistantChartData {
  chart_type: "line" | "bar" | "pie";
  title: string;
  categories: string[];
  series: AssistantChartSeries[];
  /** money_yuan 表示服务端已把「分」换算成「元」 */
  unit: "box" | "money_yuan" | "count" | "other";
  note: string | null;
  row_count: number;
}

export interface AssistantChatResponse {
  reply: string;
  intent: string;
  tool: string | null;
  /** OK / NO_RECORDS / NO_PERMISSION / ERROR / NOT_CALLED */
  tool_status: string;
  item_count: number;
  highlights: string[];
  /** 命中记录的引用（右栏展示依据）；无命中为空数组 */
  records: AssistantRecordRef[];
  /** 非空表示已生成草稿（仅暂存，未写正式业务表） */
  draft?: AssistantDraft | null;
  /** A2 图表：仅在查询有可用数据时返回；null 表示这轮不画图 */
  chart?: AssistantChartData | null;
  narrative: AssistantNarrative | null;
  degraded: boolean;
  degraded_reason: string | null;
  audit_id: string | null;
  session_id: string | null;
  /** 服务端持久会话 id：保存后可在会话列表中继续本轮对话 */
  conversation_id: string | null;
  suggestions: string[];
}

/** 历史会话条目：title 为首条消息（《03.Agent状态管理》§4）。 */
export interface AssistantSession {
  id: string;
  title: string;
  message_count: number;
  last_message_at: string;
  created_at: string;
}

/** 一条历史消息；assistant 消息的 meta 含工具摘要（还原高亮/草稿卡）。 */
export interface AssistantSessionMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  meta: {
    tool?: string | null;
    tool_status?: string;
    item_count?: number;
    highlights?: string[];
    draft?: AssistantDraft | null;
    chart?: AssistantChartData | null;
    records?: AssistantRecordRef[];
    degraded?: boolean;
  };
  created_at: string;
}

export interface AssistantTool {
  name: string;
  title: string;
  description: string;
  kind: string;
  risk_level: string;
}

/** 生成会话标识：优先 crypto.randomUUID，隐私模式/旧内核下降级为时间戳随机串。 */
export function newAssistantSessionId(): string {
  const cryptoApi = globalThis.crypto;
  if (cryptoApi && typeof cryptoApi.randomUUID === "function") {
    return cryptoApi.randomUUID();
  }
  return `asst-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

export function askAssistant(
  message: string,
  sessionId: string,
  conversationId?: string | null,
  signal?: AbortSignal,
): Promise<AssistantChatResponse> {
  return apiFetch<AssistantChatResponse>("/api/v1/assistant/chat", {
    method: "POST",
    body: {
      message,
      session_id: sessionId,
      conversation_id: conversationId ?? null,
    },
    signal,
  });
}

/** 我的历史会话（首句作标题，最近在前）。 */
export function listAssistantSessions(
  signal?: AbortSignal,
): Promise<AssistantSession[]> {
  return apiFetch<AssistantSession[]>("/api/v1/assistant/sessions", { signal });
}

/** 会话历史消息（继续对话时回填）。 */
export function listAssistantSessionMessages(
  conversationId: string,
  signal?: AbortSignal,
): Promise<AssistantSessionMessage[]> {
  return apiFetch<AssistantSessionMessage[]>(
    `/api/v1/assistant/sessions/${encodeURIComponent(conversationId)}/messages`,
    { signal },
  );
}

export function deleteAssistantSession(conversationId: string): Promise<void> {
  return apiFetch<void>(
    `/api/v1/assistant/sessions/${encodeURIComponent(conversationId)}`,
    { method: "DELETE" },
  );
}

export function listAssistantTools(): Promise<AssistantTool[]> {
  return apiFetch<AssistantTool[]>("/api/v1/assistant/tools");
}
