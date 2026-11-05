/** AI 助手对话面板（PRD §5.1；会话持久化见《03.Agent状态管理》§4/§5）：
 *
 * - 内嵌在「AI 助手」页主区域（页面左栏陈列历史会话，本面板承载当前对话）；
 * - 只做「受控只读查询」：不提供下单/审批/改数据入口；
 * - 身份与数据范围由后端从 token 注入，前端只传问题、审计用 session_id
 *   与服务端会话 conversation_id；
 * - 选中历史会话时从服务端回填消息，续聊由服务端注入上下文（记忆）；
 * - 结果分三层展示：确定性摘要（工具返回）→ 语言层（标注为假设）→ 风险提示。
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";

import {
  askAssistant,
  listAssistantSessionMessages,
  newAssistantSessionId,
  type AssistantChatResponse,
  type AssistantRecordRef,
  type AssistantSessionMessage,
} from "../../lib/assistant";
import { cn } from "../../lib/utils";
import { AssistantChart } from "./AssistantChart";
import { RecordPanel } from "./RecordPanel";

interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  text: string;
  data?: AssistantChatResponse;
  failed?: boolean;
}

const WELCOME =
  "我是沁爽分销的业务助手。可以在你的权限范围内查询商家、合同、冰柜、待巡访任务、" +
  "补货需求汇总、绩效预估与风险清单；我不会替你下单、审批或修改业务数据。";

const PROMPTS = ["查一下我的商家", "本月补货需求汇总", "我的待巡访任务", "本月绩效预估"];

let messageSeq = 0;
function nextId(): string {
  messageSeq += 1;
  return `msg-${messageSeq}`;
}

/** 历史消息 → 气泡；assistant 的工具摘要（meta）还原为高亮/草稿卡。 */
function toChatMessages(rows: AssistantSessionMessage[]): ChatMessage[] {
  return rows.map((r) => ({
    id: r.id,
    role: r.role,
    text: r.content,
    data:
      r.role === "assistant"
        ? {
            reply: r.content,
            intent: "TOOL_RESULT",
            tool: r.meta.tool ?? null,
            tool_status: r.meta.tool_status ?? "NOT_CALLED",
            item_count: r.meta.item_count ?? 0,
            highlights: r.meta.highlights ?? [],
            draft: r.meta.draft ?? null,
            // 历史会话还原图表：与当轮回复同源，且仍遵守「先文后图」
            chart: r.meta.chart ?? null,
            records: r.meta.records ?? [],
            narrative: null,
            degraded: r.meta.degraded ?? false,
            degraded_reason: null,
            audit_id: null,
            session_id: null,
            conversation_id: null,
            suggestions: [],
          }
        : undefined,
  }));
}

export interface AssistantChatProps {
  /** 选中的历史会话；空则为新对话 */
  conversationId: string | null;
  /** 新对话首轮落库后回传服务端会话 id（父组件刷新历史列表） */
  onConversationCreated?: (id: string) => void;
}

export function AssistantChat({ conversationId, onConversationCreated }: AssistantChatProps) {
  const [messages, setMessages] = useState<ChatMessage[]>([
    { id: "welcome", role: "assistant", text: WELCOME },
  ]);
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const [loadingHistory, setLoadingHistory] = useState(false);
  const [activeConversationId, setActiveConversationId] = useState<string | null>(
    conversationId,
  );
  const [sessionId] = useState(newAssistantSessionId);
  // 右栏「命中记录」：最近一条带记录引用的助手回复驱动；空表示不展示面板
  const [panelRecords, setPanelRecords] = useState<AssistantRecordRef[]>([]);
  const listRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const abortRef = useRef<AbortController | null>(null);

  // 打开历史会话：回填服务端保存的完整消息（资料 §4 load_session）
  useEffect(() => {
    if (!conversationId) return;
    const controller = new AbortController();
    setLoadingHistory(true);
    listAssistantSessionMessages(conversationId, controller.signal)
      .then((rows) => {
        setMessages(toChatMessages(rows));
        setActiveConversationId(conversationId);
        // 还原右栏：取历史中最后一条带记录引用的助手消息
        const lastWithRecords = [...rows]
          .reverse()
          .find((r) => r.role === "assistant" && (r.meta.records?.length ?? 0) > 0);
        setPanelRecords(lastWithRecords?.meta.records ?? []);
      })
      .catch((err) => {
        if (err instanceof Error && err.name === "AbortError") return;
        setMessages([
          {
            id: "load-failed",
            role: "assistant",
            text: "历史会话加载失败，可在左侧重新选择；会话可能已被删除。",
            failed: true,
          },
        ]);
      })
      .finally(() => setLoadingHistory(false));
    return () => controller.abort();
  }, [conversationId]);

  useEffect(() => {
    if (!loadingHistory) inputRef.current?.focus();
  }, [loadingHistory]);

  useEffect(() => {
    const el = listRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages]);

  useEffect(() => () => abortRef.current?.abort(), []);

  const send = useCallback(
    async (raw: string) => {
      const text = raw.trim();
      if (!text || sending) return;
      setInput("");
      setMessages((prev) => [...prev, { id: nextId(), role: "user", text }]);
      setSending(true);
      const controller = new AbortController();
      abortRef.current = controller;
      try {
        const data = await askAssistant(
          text,
          sessionId,
          activeConversationId,
          controller.signal,
        );
        setMessages((prev) => [
          ...prev,
          { id: nextId(), role: "assistant", text: data.reply, data },
        ]);
        // 本轮命中业务记录 → 右栏就地展示详情；未命中保持现状（用户可手动关）
        if (data.records?.length) setPanelRecords(data.records);
        // 新对话首轮：服务端已建会话，记下 id 供后续轮次续聊，并通知父组件刷新列表
        if (data.conversation_id && data.conversation_id !== activeConversationId) {
          setActiveConversationId(data.conversation_id);
          onConversationCreated?.(data.conversation_id);
        }
      } catch (err) {
        const message =
          err instanceof Error && err.name !== "AbortError"
            ? err.message
            : "请求已取消，请重试。";
        setMessages((prev) => [
          ...prev,
          { id: nextId(), role: "assistant", text: message, failed: true },
        ]);
      } finally {
        setSending(false);
        abortRef.current = null;
      }
    },
    [sending, sessionId, activeConversationId, onConversationCreated],
  );

  return (
    <div className="flex h-full flex-col gap-3 md:flex-row">
      <section
        aria-label="AI 助手对话"
        className="flex min-h-0 flex-1 flex-col rounded-md border border-slate-200 bg-white"
        data-testid="assistant-chat"
      >
      <header className="flex items-center justify-between border-b border-slate-200 px-4 py-3">
        <div>
          <h2 className="text-sm font-semibold text-slate-800">AI 助手</h2>
          <p className="mt-0.5 text-xs text-slate-500">
            {activeConversationId
              ? "历史会话 · 我会记住本会话的上下文"
              : "新对话 · 数据范围由你的账号权限决定"}
          </p>
        </div>
      </header>

      <div ref={listRef} className="flex-1 space-y-3 overflow-y-auto p-4">
        {loadingHistory ? (
          <p className="text-xs text-slate-500" role="status">
            正在载入历史会话…
          </p>
        ) : (
          messages.map((m) => <MessageBubble key={m.id} message={m} />)
        )}
        {sending ? (
          <p className="text-xs text-slate-500" role="status">
            正在按你的权限范围查询…
          </p>
        ) : null}
      </div>

      <div className="border-t border-slate-200 p-3">
        <div className="mb-2 flex flex-wrap gap-1.5">
          {PROMPTS.map((p) => (
            <button
              key={p}
              type="button"
              onClick={() => void send(p)}
              disabled={sending || loadingHistory}
              className="rounded-full border border-slate-200 bg-slate-50 px-2.5 py-1 text-xs text-slate-600 hover:bg-slate-100 disabled:opacity-50"
            >
              {p}
            </button>
          ))}
        </div>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            void send(input);
          }}
          className="flex items-end gap-2"
        >
          <label className="sr-only" htmlFor="assistant-input">
            向 AI 助手提问
          </label>
          <textarea
            id="assistant-input"
            ref={inputRef}
            data-testid="assistant-input"
            rows={2}
            maxLength={500}
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                void send(input);
              }
            }}
            placeholder="例如：查一下我负责的商家"
            className="flex-1 resize-none rounded-md border border-slate-300 bg-white px-3 py-2 text-sm placeholder:text-slate-400"
          />
          <button
            type="submit"
            data-testid="assistant-send"
            disabled={sending || loadingHistory || !input.trim()}
            className="rounded-md bg-[var(--color-brand)] px-3 py-2 text-sm font-medium text-white hover:opacity-90 disabled:opacity-40"
          >
            发送
          </button>
        </form>
        <p className="mt-2 text-[11px] leading-relaxed text-slate-400">
          AI 仅负责组织语言，业务数据来自受控只读工具；草稿只暂存、需你到表单确认，
          不会自动提交或进入指标。
        </p>
      </div>
      </section>

      {panelRecords.length ? (
        <RecordPanel
          records={panelRecords}
          onClose={() => setPanelRecords([])}
        />
      ) : null}
    </div>
  );
}

function MessageBubble({ message }: { message: ChatMessage }) {
  const mine = message.role === "user";
  const data = message.data;
  return (
    <div className={cn("flex", mine ? "justify-end" : "justify-start")}>
      <div
        className={cn(
          "max-w-[85%] rounded-lg px-3 py-2 text-sm",
          mine
            ? "bg-[var(--color-brand)] text-white"
            : message.failed
              ? "border border-rose-200 bg-rose-50 text-rose-700"
              : "border border-slate-200 bg-slate-50 text-slate-700",
        )}
      >
        <p className="whitespace-pre-wrap leading-relaxed">{message.text}</p>

        {/* A2 图表：先文字结论、后配图，数据与 reply 同源；无数据时后端不返回 */}
        {data?.chart ? <AssistantChart chart={data.chart} /> : null}

        {data?.highlights.length ? (
          <ul className="mt-2 space-y-1 border-t border-slate-200 pt-2 text-xs text-slate-600">
            {data.highlights.map((h) => (
              <li key={h} className="truncate">
                · {h}
              </li>
            ))}
          </ul>
        ) : null}

        {data?.draft ? (
          <div className="mt-2 rounded-md border border-emerald-200 bg-emerald-50 px-2.5 py-2">
            <p className="text-[11px] text-emerald-700">
              草稿 {data.draft.id.slice(0, 8)} · 仅暂存，未写入正式业务表，也不进入指标
            </p>
            <Link
              to={data.draft.target_path}
              data-testid="assistant-draft-link"
              className="mt-1 inline-block rounded-md bg-emerald-600 px-2.5 py-1 text-xs font-medium text-white hover:bg-emerald-700"
            >
              去{data.draft.target_label}确认
            </Link>
          </div>
        ) : null}

        {data?.tool_status === "NEED_INPUT" ? (
          <p className="mt-2 text-[11px] text-amber-600">
            还缺关键信息，补充后我再生成草稿；本次未创建任何草稿。
          </p>
        ) : null}

        {data?.tool_status === "NO_PERMISSION" ? (
          <p className="mt-2 text-xs text-amber-600">
            已按最小权限拒绝，未返回任何业务数据。
          </p>
        ) : null}

        {data?.degraded ? (
          <p className="mt-2 text-[11px] text-slate-400">
            语言层已降级（{data.degraded_reason ?? "模型不可用"}），查询结果仍为确定性返回。
          </p>
        ) : null}

        {data?.narrative ? (
          <p className="mt-2 text-[11px] text-slate-400">
            {data.narrative.headline}
            {data.narrative.confidence === "LOW" ? "（置信度低）" : ""}
          </p>
        ) : null}
      </div>
    </div>
  );
}
