/** AI 助手页（会话持久化）：页面内会话侧边栏陈列历史会话（首句作标题），
 * 右侧主区域为对话面板；点击历史会话回填并可继续对话；
 * 「新对话 / 生成草稿」切回空白对话。
 */
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AssistantPage } from "./Assistant";

const SESSIONS = [
  {
    id: "conv-1",
    title: "查一下我的商家",
    message_count: 4,
    last_message_at: "2026-10-20T02:00:00Z",
    created_at: "2026-10-20T01:00:00Z",
  },
  {
    id: "conv-2",
    title: "帮我给华东便利店整理巡访草稿",
    message_count: 2,
    last_message_at: "2026-10-19T08:00:00Z",
    created_at: "2026-10-19T08:00:00Z",
  },
];

function okJson(payload: unknown): Response {
  return new Response(JSON.stringify(payload), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

function renderPage() {
  return render(
    <MemoryRouter>
      <AssistantPage />
    </MemoryRouter>,
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("AI 助手页（会话侧边栏 + 对话主区域）", () => {
  it("侧边栏陈列历史会话：首句作标题，展示消息数与时间；主区域默认新对话", async () => {
    const fetchMock = vi.fn((url: string) => {
      if (url === "/api/v1/assistant/sessions") {
        return Promise.resolve(okJson(SESSIONS));
      }
      return Promise.reject(new Error(`unexpected ${url}`));
    });
    vi.stubGlobal("fetch", fetchMock);

    renderPage();

    const sidebar = screen.getByRole("complementary", { name: "历史会话" });
    expect(await within(sidebar).findByText("查一下我的商家")).toBeInTheDocument();
    expect(within(sidebar).getByText("帮我给华东便利店整理巡访草稿")).toBeInTheDocument();
    expect(within(sidebar).getByText(/4 条消息/)).toBeInTheDocument();
    expect(within(sidebar).getAllByTestId("assistant-session-item")).toHaveLength(2);
    // 主区域：空白对话（欢迎语）
    const chat = screen.getByTestId("assistant-chat");
    expect(within(chat).getByText(/业务助手/)).toBeInTheDocument();
  });

  it("无会话时侧边栏展示引导文案", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(okJson([])));

    renderPage();

    expect(await screen.findByText(/还没有会话/)).toBeInTheDocument();
    expect(screen.getByTestId("assistant-new-chat")).toBeInTheDocument();
  });

  it("点击侧边栏历史会话：主区域回填该会话消息", async () => {
    const HISTORY = [
      {
        id: "m1",
        role: "user",
        content: "查一下我的商家",
        meta: {},
        created_at: "2026-10-20T01:00:00Z",
      },
      {
        id: "m2",
        role: "assistant",
        content: "查到 2 条记录。",
        meta: { tool: "merchant.search", tool_status: "OK" },
        created_at: "2026-10-20T01:00:01Z",
      },
    ];
    const fetchMock = vi.fn((url: string) => {
      if (url === "/api/v1/assistant/sessions") {
        return Promise.resolve(okJson(SESSIONS));
      }
      if (url === "/api/v1/assistant/sessions/conv-1/messages") {
        return Promise.resolve(okJson(HISTORY));
      }
      return Promise.reject(new Error(`unexpected ${url}`));
    });
    vi.stubGlobal("fetch", fetchMock);

    renderPage();

    const item = (await screen.findAllByTestId("assistant-session-item"))[0];
    fireEvent.click(item);

    const chat = screen.getByTestId("assistant-chat");
    expect(await within(chat).findByText("查到 2 条记录。")).toBeInTheDocument();
    expect(within(chat).getByText(/历史会话 · 我会记住本会话的上下文/)).toBeInTheDocument();
    expect(item).toHaveAttribute("aria-current", "true");
    await waitFor(() => {
      expect(
        fetchMock.mock.calls.some(
          ([url]) => url === "/api/v1/assistant/sessions/conv-1/messages",
        ),
      ).toBe(true);
    });
  });

  it("会话项叉号二次确认后删除：列表移除，删当前会话切回空白对话", async () => {
    const deleted: string[] = [];
    const fetchMock = vi.fn((url: string, init?: RequestInit) => {
      if (url === "/api/v1/assistant/sessions") {
        return Promise.resolve(
          okJson(SESSIONS.filter((s) => !deleted.includes(s.id))),
        );
      }
      if (url === "/api/v1/assistant/sessions/conv-1/messages") {
        return Promise.resolve(okJson([]));
      }
      if (url.startsWith("/api/v1/assistant/sessions/") && init?.method === "DELETE") {
        deleted.push(url.split("/").pop() ?? "");
        return Promise.resolve(new Response(null, { status: 204 }));
      }
      return Promise.reject(new Error(`unexpected ${url} ${init?.method}`));
    });
    vi.stubGlobal("fetch", fetchMock);

    renderPage();

    // 先打开 conv-1，让它成为当前会话
    fireEvent.click((await screen.findAllByTestId("assistant-session-item"))[0]);
    await screen.findByText(/历史会话 · 我会记住本会话的上下文/);

    // 第一次点 ×：进入确认态，不发 DELETE
    fireEvent.click((await screen.findAllByTestId("assistant-session-delete"))[0]);
    expect(screen.getAllByTestId("assistant-session-delete")[0]).toHaveTextContent(
      "确认删除",
    );
    expect(deleted).toHaveLength(0);

    // 第二次点：确认删除
    fireEvent.click(screen.getAllByTestId("assistant-session-delete")[0]);

    await waitFor(() => expect(deleted).toEqual(["conv-1"]));
    await waitFor(() =>
      expect(screen.getAllByTestId("assistant-session-item")).toHaveLength(1),
    );
    expect(screen.getByText("帮我给华东便利店整理巡访草稿")).toBeInTheDocument();
    // 主区域切回空白新对话
    const chat = screen.getByTestId("assistant-chat");
    expect(await within(chat).findByText(/业务助手/)).toBeInTheDocument();
  });

  it("打开历史会话后点「新对话 / 生成草稿」：主区域切回空白对话", async () => {
    const fetchMock = vi.fn((url: string) => {
      if (url === "/api/v1/assistant/sessions") {
        return Promise.resolve(okJson(SESSIONS));
      }
      if (url === "/api/v1/assistant/sessions/conv-1/messages") {
        return Promise.resolve(okJson([]));
      }
      return Promise.reject(new Error(`unexpected ${url}`));
    });
    vi.stubGlobal("fetch", fetchMock);

    renderPage();

    const item = (await screen.findAllByTestId("assistant-session-item"))[0];
    fireEvent.click(item);
    await screen.findByText(/历史会话 · 我会记住本会话的上下文/);

    fireEvent.click(screen.getByTestId("assistant-new-chat"));

    const chat = screen.getByTestId("assistant-chat");
    expect(await within(chat).findByText(/业务助手/)).toBeInTheDocument();
    expect(within(chat).getByText(/新对话 · 数据范围由你的账号权限决定/)).toBeInTheDocument();
  });

  it("回复命中合同记录：右栏展示该合同详情", async () => {
    const CHAT_REPLY = {
      reply: "查到 1 条「查询合同」记录。",
      intent: "TOOL_RESULT",
      tool: "contract.search",
      tool_status: "OK",
      item_count: 1,
      highlights: ["汇佳便利店年度合同（EFFECTIVE）"],
      records: [
        {
          object_type: "CONTRACT",
          object_id: "c-1",
          path: "/contracts/c-1",
          label: "汇佳便利店年度合同（EFFECTIVE）",
        },
      ],
      draft: null,
      narrative: null,
      degraded: true,
      degraded_reason: "模型不可用",
      audit_id: "a-1",
      session_id: "s-1",
      conversation_id: "conv-new",
      suggestions: [],
    };
    const CONTRACT_DETAIL = {
      contract: {
        contract_no: "HT-2026-001",
        title: "汇佳便利店年度合同",
        party_name: "汇佳便利店",
        status: "EFFECTIVE",
        version: 2,
        signed_at: "2026-01-10",
        effective_from: "2026-02-01",
        expire_at: "2027-01-31",
        commission_rate: 3.5,
        display_subsidy_amount: 120000,
        display_threshold_pct: 50,
        commission_rule_version: "comm-v1",
        store_manager_name: "王店长",
      },
      versions: [{ version: 1 }, { version: 2 }],
      renewals: [],
    };
    // 首轮落库后父组件刷新会话 id → 对话面板按新会话重挂载并从服务端回填
    const SAVED_TURN = [
      {
        id: "m1",
        role: "user",
        content: "我想看一下汇佳便利店的合同",
        meta: {},
        created_at: "2026-10-21T01:00:00Z",
      },
      {
        id: "m2",
        role: "assistant",
        content: CHAT_REPLY.reply,
        meta: {
          tool: "contract.search",
          tool_status: "OK",
          records: CHAT_REPLY.records,
        },
        created_at: "2026-10-21T01:00:01Z",
      },
    ];
    const fetchMock = vi.fn((url: string, init?: RequestInit) => {
      if (url === "/api/v1/assistant/sessions") {
        return Promise.resolve(okJson([]));
      }
      if (url === "/api/v1/assistant/chat" && init?.method === "POST") {
        return Promise.resolve(okJson(CHAT_REPLY));
      }
      if (url === "/api/v1/assistant/sessions/conv-new/messages") {
        return Promise.resolve(okJson(SAVED_TURN));
      }
      if (url === "/api/contracts/c-1") {
        return Promise.resolve(okJson(CONTRACT_DETAIL));
      }
      return Promise.reject(new Error(`unexpected ${url} ${init?.method}`));
    });
    vi.stubGlobal("fetch", fetchMock);

    renderPage();

    fireEvent.change(screen.getByTestId("assistant-input"), {
      target: { value: "我想看一下汇佳便利店的合同" },
    });
    fireEvent.click(screen.getByTestId("assistant-send"));

    // 右栏：拉取并展示合同详情
    const panel = await screen.findByTestId("assistant-record-panel");
    const card = await within(panel).findByTestId("assistant-contract-card");
    expect(within(card).getByText("汇佳便利店年度合同")).toBeInTheDocument();
    expect(within(card).getByText(/HT-2026-001/)).toBeInTheDocument();
    expect(within(card).getByText("王店长")).toBeInTheDocument();
    expect(within(card).getByTestId("assistant-record-open")).toHaveAttribute(
      "href",
      "/contracts/c-1",
    );

    // 关闭面板
    fireEvent.click(within(panel).getByRole("button", { name: "关闭命中记录面板" }));
    expect(screen.queryByTestId("assistant-record-panel")).not.toBeInTheDocument();
  });

  it("历史会话回填：最后一条带记录引用的助手消息还原右栏", async () => {
    const HISTORY = [
      {
        id: "m1",
        role: "user",
        content: "我想看一下汇佳便利店的合同",
        meta: {},
        created_at: "2026-10-20T01:00:00Z",
      },
      {
        id: "m2",
        role: "assistant",
        content: "查到 1 条「查询合同」记录。",
        meta: {
          tool: "contract.search",
          tool_status: "OK",
          records: [
            {
              object_type: "CONTRACT",
              object_id: "c-1",
              path: "/contracts/c-1",
              label: "汇佳便利店年度合同（EFFECTIVE）",
            },
          ],
        },
        created_at: "2026-10-20T01:00:01Z",
      },
    ];
    const fetchMock = vi.fn((url: string) => {
      if (url === "/api/v1/assistant/sessions") {
        return Promise.resolve(okJson(SESSIONS));
      }
      if (url === "/api/v1/assistant/sessions/conv-1/messages") {
        return Promise.resolve(okJson(HISTORY));
      }
      if (url === "/api/contracts/c-1") {
        return Promise.resolve(
          okJson({
            contract: {
              contract_no: "HT-2026-001",
              title: "汇佳便利店年度合同",
              party_name: "汇佳便利店",
              status: "EFFECTIVE",
              version: 2,
              signed_at: null,
              effective_from: null,
              expire_at: null,
              commission_rate: null,
              display_subsidy_amount: null,
              display_threshold_pct: null,
              commission_rule_version: null,
              store_manager_name: null,
            },
            versions: [],
            renewals: [],
          }),
        );
      }
      return Promise.reject(new Error(`unexpected ${url}`));
    });
    vi.stubGlobal("fetch", fetchMock);

    renderPage();

    fireEvent.click((await screen.findAllByTestId("assistant-session-item"))[0]);

    const panel = await screen.findByTestId("assistant-record-panel");
    expect(await within(panel).findByText(/HT-2026-001/)).toBeInTheDocument();
  });
});
