/** AI 助手对话面板（PRD §5.1；会话持久化见《03.Agent状态管理》§4）：
 * 新对话欢迎语、发送渲染后端回复、请求体不含角色/范围字段、草稿入口；
 * 选中历史会话时回填服务端消息并携带 conversation_id 续聊。
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AssistantChat } from "./AssistantChat";

// 图表在 jsdom 里没有 canvas：只验证「气泡里有没有图表、文字是否在前」
vi.mock("echarts/core", () => ({
  use: vi.fn(),
  init: vi.fn(() => ({ setOption: vi.fn(), resize: vi.fn(), dispose: vi.fn() })),
}));
vi.mock("echarts/charts", () => ({
  LineChart: {},
  BarChart: {},
  PieChart: {},
}));
vi.mock("echarts/components", () => ({
  GridComponent: {},
  TooltipComponent: {},
  LegendComponent: {},
}));
vi.mock("echarts/renderers", () => ({ CanvasRenderer: {} }));

beforeEach(() => {
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe = vi.fn();
      disconnect = vi.fn();
    },
  );
});

const REPLY = {
  reply: "已在你的可见范围内查到 2 条「商家」记录，以下为工具直接返回的摘要，可继续追问具体对象。",
  intent: "QA",
  tool: "merchant.search",
  tool_status: "OK",
  item_count: 2,
  highlights: ["华东便利店（ACTIVE）", "城西便利店（ACTIVE）"],
  narrative: {
    headline: "已在你可见的数据范围内完成查询（2 条）",
    summary: "以下内容来自受控只读工具的实际返回。",
    assumptions: ["答复中的业务数据来自受控只读工具的真实返回（非模型推断）"],
    confidence: "MEDIUM",
  },
  degraded: false,
  degraded_reason: null,
  audit_id: "audit-1",
  session_id: "session-1",
  conversation_id: "conv-new-1",
  suggestions: ["查一下我的商家"],
};

const DRAFT_REPLY = {
  ...REPLY,
  reply:
    "已生成「巡访草稿」（编号 1a2b3c4d）。草稿只暂存、未写入正式业务表，也不进入任何指标；请前往「巡访管理」页面确认后提交。",
  intent: "VISIT_COMPOSE",
  tool: "visit.compose_draft",
  tool_status: "DRAFT_CREATED",
  item_count: 1,
  highlights: ["草稿编号 1a2b3c4d"],
  draft: {
    id: "1a2b3c4d-0000-4000-8000-000000000000",
    kind: "visit",
    status: "OPEN",
    target_path: "/visits",
    target_label: "巡访管理",
  },
};

function okJson(payload: unknown): Response {
  return new Response(JSON.stringify(payload), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

function renderChat(props: Partial<Parameters<typeof AssistantChat>[0]> = {}) {
  return render(
    <MemoryRouter>
      <AssistantChat conversationId={null} {...props} />
    </MemoryRouter>,
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("AI 助手对话面板", () => {
  it("新对话：欢迎语与权限说明", () => {
    renderChat();
    expect(screen.getByTestId("assistant-chat")).toBeInTheDocument();
    expect(screen.getByText(/新对话 · 数据范围由你的账号权限决定/)).toBeInTheDocument();
    expect(screen.getByText(/业务助手/)).toBeInTheDocument();
  });

  it("发送问题后渲染后端回复与证据条目；请求体不含角色/范围字段", async () => {
    const fetchMock = vi.fn().mockResolvedValue(okJson(REPLY));
    vi.stubGlobal("fetch", fetchMock);
    const onCreated = vi.fn();

    renderChat({ onConversationCreated: onCreated });
    fireEvent.change(screen.getByTestId("assistant-input"), {
      target: { value: "查一下我的商家" },
    });
    fireEvent.click(screen.getByTestId("assistant-send"));

    expect(await screen.findByText(/查到 2 条「商家」记录/)).toBeInTheDocument();
    expect(screen.getByText(/华东便利店/)).toBeInTheDocument();

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/v1/assistant/chat");
    const body = JSON.parse(String(init.body)) as Record<string, unknown>;
    expect(body.message).toBe("查一下我的商家");
    expect(typeof body.session_id).toBe("string");
    expect(body.conversation_id).toBeNull();
    // 红线：身份与数据范围只能由后端从 token 注入
    expect(body).not.toHaveProperty("role");
    expect(body).not.toHaveProperty("region_id");
    expect(body).not.toHaveProperty("actor_id");
    // 首轮落库后通知父组件刷新会话列表
    await waitFor(() => expect(onCreated).toHaveBeenCalledWith("conv-new-1"));
    // 会话建立后头部切换为「历史会话」
    expect(await screen.findByText(/历史会话 · 我会记住本会话的上下文/)).toBeInTheDocument();
  });

  it("草稿结果展示「去表单确认」入口并标明仅暂存", async () => {
    const fetchMock = vi.fn().mockResolvedValue(okJson(DRAFT_REPLY));
    vi.stubGlobal("fetch", fetchMock);

    renderChat();
    fireEvent.change(screen.getByTestId("assistant-input"), {
      target: { value: "帮我给华东便利店整理巡访草稿" },
    });
    fireEvent.click(screen.getByTestId("assistant-send"));

    const link = await screen.findByTestId("assistant-draft-link");
    expect(link).toHaveAttribute("href", "/visits");
    expect(link).toHaveTextContent("去巡访管理确认");
    expect(screen.getByText(/仅暂存，未写入正式业务表/)).toBeInTheDocument();
  });

  it("查询结果：先给文字答案，再渲染图表", async () => {
    const CHART_REPLY = {
      ...REPLY,
      reply: "最近一周共售出 120 箱，日均约 17 箱。",
      tool: "text2sql.query",
      item_count: 7,
      chart: {
        chart_type: "line",
        title: "最近一周的沁爽饮用水销量",
        categories: ["10-23", "10-24"],
        series: [{ name: "确认销量", data: [10, 20] }],
        unit: "box",
        note: null,
        row_count: 2,
      },
    };
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(okJson(CHART_REPLY)));

    renderChat();
    fireEvent.change(screen.getByTestId("assistant-input"), {
      target: { value: "最近一周的沁爽饮用水销量如何" },
    });
    fireEvent.click(screen.getByTestId("assistant-send"));

    const title = await screen.findByText("最近一周的沁爽饮用水销量");
    expect(screen.getByText("单位：箱")).toBeInTheDocument();
    // 同一气泡内：文字结论在图表标题之前
    const bubbleText = title.closest("figure")?.parentElement?.textContent ?? "";
    const answerAt = bubbleText.indexOf("最近一周共售出 120 箱");
    const chartAt = bubbleText.indexOf("最近一周的沁爽饮用水销量");
    expect(answerAt).toBeGreaterThanOrEqual(0);
    expect(answerAt).toBeLessThan(chartAt);
  });

  it("历史会话回看：图表同样还原", async () => {
    const HISTORY = [
      {
        id: "m1",
        role: "user",
        content: "最近一周的沁爽饮用水销量如何",
        meta: {},
        created_at: "2026-10-20T01:00:00Z",
      },
      {
        id: "m2",
        role: "assistant",
        content: "最近一周共售出 120 箱。",
        meta: {
          tool: "text2sql.query",
          tool_status: "OK",
          item_count: 2,
          chart: {
            chart_type: "bar",
            title: "近 7 天各饮料销量",
            categories: ["白桃气泡水", "苹果汁"],
            series: [{ name: "确认销量", data: [30, 20] }],
            unit: "box",
            note: null,
            row_count: 2,
          },
        },
        created_at: "2026-10-20T01:00:01Z",
      },
    ];
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(okJson(HISTORY)));

    renderChat({ conversationId: "conv-7" });

    expect(await screen.findByText("近 7 天各饮料销量")).toBeInTheDocument();
    expect(screen.getByText("对比")).toBeInTheDocument();
  });

  it("选中历史会话：回填服务端消息；续聊携带 conversation_id", async () => {
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
        content: "查到 2 条「商家」记录。",
        meta: {
          tool: "merchant.search",
          tool_status: "OK",
          item_count: 2,
          highlights: ["华东便利店（ACTIVE）"],
          degraded: false,
        },
        created_at: "2026-10-20T01:00:01Z",
      },
    ];
    const fetchMock = vi.fn((url: string, init?: RequestInit) => {
      if (url === "/api/v1/assistant/sessions/conv-9/messages") {
        return Promise.resolve(okJson(HISTORY));
      }
      if (url === "/api/v1/assistant/chat") {
        return Promise.resolve(okJson(REPLY));
      }
      return Promise.reject(new Error(`unexpected ${init?.method ?? "GET"} ${url}`));
    });
    vi.stubGlobal("fetch", fetchMock);

    renderChat({ conversationId: "conv-9" });

    // 历史回填（首条问题与回答、高亮都还原）
    expect(await screen.findByText("查一下我的商家")).toBeInTheDocument();
    expect(screen.getByText("查到 2 条「商家」记录。")).toBeInTheDocument();
    expect(screen.getByText(/华东便利店/)).toBeInTheDocument();
    expect(screen.getByText(/历史会话 · 我会记住本会话的上下文/)).toBeInTheDocument();

    // 续聊：请求体携带 conversation_id
    fireEvent.change(screen.getByTestId("assistant-input"), {
      target: { value: "继续说第二家" },
    });
    fireEvent.click(screen.getByTestId("assistant-send"));

    await waitFor(() => {
      const chatCall = fetchMock.mock.calls.find(
        ([url]) => url === "/api/v1/assistant/chat",
      );
      expect(chatCall).toBeTruthy();
      const body = JSON.parse(String(chatCall?.[1]?.body)) as Record<string, unknown>;
      expect(body.conversation_id).toBe("conv-9");
      expect(body.message).toBe("继续说第二家");
    });
  });
});
