/** T28 移动端 H5：Q164 活动/巡访移动流程；Q165 相机/定位拒绝的替代提示。 */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { DraftStorage, FetchStateFn, SubmitFn } from "../../mobile/offline/types";
import { TodayTasks, type TodayTask } from "./TodayTasks";

function memStorage(): DraftStorage {
  const m = new Map<string, string>();
  return {
    getItem: (k) => (m.has(k) ? (m.get(k) as string) : null),
    setItem: (k, v) => void m.set(k, v),
    removeItem: (k) => void m.delete(k),
  };
}

const TASKS: TodayTask[] = [
  { id: "t-visit", merchantName: "好邻居便利店", kind: "visit", plannedWindow: "09:00-10:00", status: "PLANNED", version: 1 },
  { id: "t-act", merchantName: "万达广场快闪", kind: "activity", plannedWindow: "15:00-18:00", status: "DRAFT", version: 1 },
];

function makeDeps(over: { online?: boolean; submit?: SubmitFn; fetch?: FetchStateFn } = {}) {
  const fetchState: FetchStateFn =
    over.fetch ??
    (async (_kind, id) => {
      const status = id === "t-act" ? "DRAFT" : "PLANNED";
      return { status, version: 1, snapshot: { note: "" } };
    });
  return {
    tasks: TASKS,
    submit: over.submit ?? (async () => ({ ok: true, httpStatus: 201 })),
    fetchState,
    storage: memStorage(),
    isOnline: () => over.online ?? true,
  };
}

function installGeo(getCurrentPosition: Parameters<typeof vi.fn>[0]) {
  const geo = { getCurrentPosition: vi.fn(getCurrentPosition) } as unknown as Geolocation;
  Object.defineProperty(navigator, "geolocation", { configurable: true, value: geo });
  return geo;
}

describe("Q165 相机/定位拒绝的替代提示", () => {
  beforeEach(() => {
    // jsdom 不实现 HTMLInputElement.click 的文件选择；这里让点击后 change 无文件
    HTMLInputElement.prototype.click = vi.fn();
  });
  afterEach(() => vi.restoreAllMocks());

  it("定位被拒绝：展示可操作替代提示，且不把它当作已打卡", async () => {
    installGeo((_success, error) =>
      error({ code: 1, PERMISSION_DENIED: 1, POSITION_UNAVAILABLE: 2, TIMEOUT: 3, message: "denied" }),
    );
    render(<TodayTasks {...makeDeps()} />);
    fireEvent.click(screen.getByTestId("checkin-t-visit"));

    await waitFor(() => expect(screen.getByTestId("geo-hint")).toBeInTheDocument());
    expect(screen.getByTestId("geo-hint").textContent).toContain("定位权限被拒绝");
    // 未出现定位成功反馈
    expect(screen.queryByTestId("feedback")).toBeNull();
  });

  it("定位成功但仅在主动打卡时采集", async () => {
    const getPos = vi.fn((ok: PositionCallback) =>
      ok({
        coords: { longitude: 116.4, latitude: 39.9, accuracy: 10 } as GeolocationCoordinates,
        timestamp: Date.now(),
      } as GeolocationPosition),
    );
    installGeo(getPos);
    render(<TodayTasks {...makeDeps()} />);
    // 渲染阶段不采集（没有点击就不调用）
    expect(getPos).not.toHaveBeenCalled();
    fireEvent.click(screen.getByTestId("checkin-t-visit"));
    await waitFor(() => expect(screen.getByTestId("feedback")).toBeInTheDocument());
    expect(screen.getByTestId("feedback").textContent).toContain("获取定位");
  });

  it("相机取消/拒绝：展示替代提示（可重拍/相册/先存草稿）", async () => {
    installGeo((success) =>
      success({ coords: { longitude: 1, latitude: 2, accuracy: 5 }, timestamp: 1 }),
    );
    render(<TodayTasks {...makeDeps()} />);
    fireEvent.click(screen.getByTestId("visit-t-visit"));
    // 点击拍照；用户取消选择 → change 事件但无文件，视为拒绝/取消
    fireEvent.click(screen.getByTestId("take-photo"));
    fireEvent.change(screen.getByLabelText("拍照或选择照片"), { target: { files: [] } });
    await waitFor(() => expect(screen.getByTestId("photo-hint")).toBeInTheDocument());
    expect(screen.getByTestId("photo-hint").textContent).toContain("未获取到照片");
  });
});

describe("Q164 活动与巡访移动流程", () => {
  beforeEach(() => {
    HTMLInputElement.prototype.click = vi.fn();
  });
  afterEach(() => vi.restoreAllMocks());

  it("列出今日巡访与活动任务，提供打卡/巡访/补货/活动触控入口", () => {
    render(<TodayTasks {...makeDeps()} />);
    expect(screen.getByText("好邻居便利店")).toBeInTheDocument();
    expect(screen.getByText("万达广场快闪")).toBeInTheDocument();
    expect(screen.getByTestId("checkin-t-visit")).toBeInTheDocument();
    expect(screen.getByTestId("visit-t-visit")).toBeInTheDocument();
    expect(screen.getByTestId("repl-t-visit")).toBeInTheDocument();
    expect(screen.getByTestId("act-t-act")).toBeInTheDocument();
  });

  it("离线时点击提交只保存本地草稿并显示弱网横幅，不调用服务端", async () => {
    const submit = vi.fn(async () => ({ ok: true, httpStatus: 201 }));
    render(<TodayTasks {...makeDeps({ online: false, submit })} />);

    expect(screen.getByTestId("offline-banner")).toBeInTheDocument();
    fireEvent.click(screen.getByTestId("visit-t-visit"));
    fireEvent.change(screen.getByLabelText("现场备注"), { target: { value: "陈列 80%" } });
    fireEvent.click(screen.getByTestId("save-draft"));

    await waitFor(() => expect(screen.getByText(/不是已提交/)).toBeInTheDocument());
    expect(submit).not.toHaveBeenCalled();
    // 草稿箱出现一条 local 草稿
    const status = await screen.findByText("local");
    expect(status).toBeInTheDocument();
  });

  it("在线保存草稿→查看差异→确认提交成功后草稿清理", async () => {
    const submit = vi.fn(async (draft) => ({
      ok: true,
      httpStatus: 201,
      body: { replayed: false, key: draft.idempotencyKey },
    }));
    render(<TodayTasks {...makeDeps({ submit })} />);

    fireEvent.click(screen.getByTestId("visit-t-visit"));
    fireEvent.change(screen.getByLabelText("现场备注"), { target: { value: "补货 5 箱" } });
    // visit.submit 是高风险：在线主按钮"提交（带幂等键）"仍先落草稿，走确认流程
    fireEvent.click(screen.getByTestId("save-draft"));
    const draftNode = await screen.findByTestId(/^draft-card-/);
    const draftId = draftNode.getAttribute("data-testid")?.replace("draft-card-", "") as string;

    // 查看差异（无服务端改动，高风险仍需确认）
    fireEvent.click(screen.getByTestId(`review-${draftId}`));
    await waitFor(() => expect(screen.getByTestId("diff-panel")).toBeInTheDocument());

    // 确认提交 → 成功 → 草稿被清理
    fireEvent.click(screen.getByTestId("diff-confirm"));
    await waitFor(() => expect(submit).toHaveBeenCalledTimes(1));
    expect(screen.queryByTestId(`draft-card-${draftId}`)).toBeNull();
  });

  it("服务端已推进状态：差异面板显示状态冲突，禁止覆盖提交", async () => {
    const fetchState: FetchStateFn = async () => ({
      status: "SUBMITTED",
      version: 3,
      snapshot: { note: "" },
    });
    const submit = vi.fn(async () => ({ ok: true, httpStatus: 201 }));
    render(<TodayTasks {...makeDeps({ fetch: fetchState, submit })} />);

    fireEvent.click(screen.getByTestId("visit-t-visit"));
    fireEvent.click(screen.getByTestId("save-draft"));
    const draftNode = await screen.findByTestId(/^draft-card-/);
    const draftId = draftNode.getAttribute("data-testid")?.replace("draft-card-", "") as string;
    fireEvent.click(screen.getByTestId(`review-${draftId}`));

    await waitFor(() => expect(screen.getByTestId("state-conflict")).toBeInTheDocument());
    expect(screen.getByTestId("state-conflict").textContent).toContain("SUBMITTED");
    const confirmBtn = screen.getByTestId("diff-confirm") as HTMLButtonElement;
    expect(confirmBtn.disabled).toBe(true);
    expect(submit).not.toHaveBeenCalled();
  });
});
