/** AI 助手页（PRD §5.1；会话持久化见《03.Agent状态管理》§4）：
 *
 * 布局：页面内左栏为「会话侧边栏」——新对话按钮 + 历史会话列表
 * （首条消息作标题，最近在前）；右侧主区域为对话面板。
 * 点击历史会话 → 右侧面板回填历史并带着上下文继续对话；
 * 「新对话 / 生成草稿」→ 右侧切换为空白对话；首轮落库后左栏列表自动刷新。
 */
import { useCallback, useEffect, useState } from "react";

import { AssistantChat } from "../components/assistant/AssistantChat";
import {
  deleteAssistantSession,
  listAssistantSessions,
  type AssistantSession,
} from "../lib/assistant";
import { cn } from "../lib/utils";

function formatTime(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getMonth() + 1}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

export function AssistantPage() {
  const [sessions, setSessions] = useState<AssistantSession[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [activeId, setActiveId] = useState<string | null>(null);
  // 每次点「新对话」递增，强制对话面板重置为空白会话
  const [newChatSeq, setNewChatSeq] = useState(0);
  // 删除二次确认：先点 × 进入确认态，再点确认才真删（防误触）
  const [confirmDeleteId, setConfirmDeleteId] = useState<string | null>(null);
  const [deletingId, setDeletingId] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setSessions(await listAssistantSessions());
    } catch (err) {
      setError(err instanceof Error ? err.message : "会话列表加载失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const openNew = () => {
    setActiveId(null);
    setNewChatSeq((n) => n + 1);
  };
  const openSession = (id: string) => setActiveId(id);

  const removeSession = async (id: string) => {
    setDeletingId(id);
    try {
      await deleteAssistantSession(id);
      // 删掉的正是当前打开的会话 → 主区域切回空白新对话
      if (activeId === id) {
        setActiveId(null);
        setNewChatSeq((n) => n + 1);
      }
      setConfirmDeleteId(null);
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "删除会话失败");
    } finally {
      setDeletingId(null);
    }
  };

  return (
    <div
      className="flex h-[calc(100vh-5.5rem)] flex-col gap-3 md:flex-row"
      data-testid="assistant-page"
    >
      {/* 会话侧边栏：历史会话（首句作标题） */}
      <aside
        aria-label="历史会话"
        className="flex max-h-48 w-full shrink-0 flex-col rounded-md border border-slate-200 bg-white md:max-h-none md:w-64"
      >
        <div className="border-b border-slate-200 p-2.5">
          <button
            type="button"
            onClick={openNew}
            data-testid="assistant-new-chat"
            className="w-full rounded-md bg-[var(--color-brand)] px-3 py-2 text-sm font-medium text-white hover:opacity-90"
          >
            新对话 / 生成草稿
          </button>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto p-2">
          {loading ? (
            <p className="px-2 py-1 text-xs text-slate-400" role="status">
              正在载入…
            </p>
          ) : error ? (
            <p className="rounded-md border border-rose-200 bg-rose-50 px-2.5 py-2 text-xs text-rose-700">
              {error}
            </p>
          ) : sessions.length === 0 ? (
            <p className="px-2 py-3 text-xs leading-relaxed text-slate-400">
              还没有会话。点击上方「新对话 / 生成草稿」开始提问，会话会自动保存到这里。
            </p>
          ) : (
            <ul className="space-y-1">
              {sessions.map((s) => (
                <li
                  key={s.id}
                  className="group relative"
                  onMouseLeave={() =>
                    setConfirmDeleteId((cur) => (cur === s.id ? null : cur))
                  }
                >
                  <button
                    type="button"
                    onClick={() => openSession(s.id)}
                    data-testid="assistant-session-item"
                    aria-current={activeId === s.id ? "true" : undefined}
                    className={cn(
                      "w-full rounded-md px-2.5 py-2 pr-8 text-left hover:bg-slate-50",
                      activeId === s.id &&
                        "border border-[var(--color-brand)] bg-blue-50/50",
                    )}
                  >
                    <span className="block truncate text-sm text-slate-700">
                      {s.title}
                    </span>
                    <span className="mt-0.5 block text-[11px] text-slate-400">
                      {s.message_count} 条消息 · {formatTime(s.last_message_at)}
                    </span>
                  </button>
                  <button
                    type="button"
                    aria-label={`删除会话 ${s.title}`}
                    data-testid="assistant-session-delete"
                    disabled={deletingId === s.id}
                    onClick={(e) => {
                      e.stopPropagation();
                      if (confirmDeleteId === s.id) {
                        void removeSession(s.id);
                      } else {
                        setConfirmDeleteId(s.id);
                      }
                    }}
                    className={cn(
                      "absolute right-1.5 top-1.5 rounded px-1 text-xs leading-5",
                      confirmDeleteId === s.id
                        ? "bg-rose-600 font-medium text-white hover:bg-rose-700"
                        : "text-slate-300 hover:bg-rose-50 hover:text-rose-600 md:opacity-0 md:group-hover:opacity-100",
                      deletingId === s.id && "cursor-wait opacity-60",
                    )}
                  >
                    {deletingId === s.id
                      ? "…"
                      : confirmDeleteId === s.id
                        ? "确认删除"
                        : "×"}
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
      </aside>

      {/* 主区域：当前对话 */}
      <div className="min-h-0 flex-1">
        <AssistantChat
          key={activeId ?? `new-${newChatSeq}`}
          conversationId={activeId}
          onConversationCreated={(id) => {
            setActiveId(id);
            void refresh();
          }}
        />
      </div>
    </div>
  );
}
