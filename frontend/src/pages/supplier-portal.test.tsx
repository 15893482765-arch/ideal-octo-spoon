/** 厂商门户页（/supplier）：授权主体提示 + 供货确认工作台（PRD §2.3/§3.2）。 */
import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { SupplierPortal } from "./SupplierPortal";

vi.mock("../lib/auth", () => ({
  useAuth: () => ({ authenticated: true, user: { role: "SUPPLIER" } }),
}));

function mockFetch(map: Record<string, unknown>) {
  const fetchMock = vi.fn().mockImplementation((url: string) => {
    const key = Object.keys(map).find((k) => String(url).includes(k));
    const body = key ? map[key] : {};
    return Promise.resolve(
      new Response(JSON.stringify(body), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("厂商门户", () => {
  it("展示授权主体与供货确认工作台", async () => {
    mockFetch({
      "/procurement/suppliers": {
        items: [{ id: "s1", code: "SUP-01", name: "爽快饮料厂" }],
      },
      "/master-data/brands": { items: [] },
      "/master-data/regions": { items: [] },
      "/procurement/orders": [],
    });

    render(<SupplierPortal />);

    expect(screen.getByText("厂商门户")).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByText(/爽快饮料厂/)).toBeInTheDocument(),
    );
    // SupplyTab 的厂商操作提示（Q100/Q101）
    expect(screen.getByText(/确认不等于付款/)).toBeInTheDocument();
  });

  it("未配置授权时明确提示，不静默空白", async () => {
    mockFetch({
      "/procurement/suppliers": { items: [] },
      "/master-data/brands": { items: [] },
      "/master-data/regions": { items: [] },
      "/procurement/orders": [],
    });

    render(<SupplierPortal />);

    await waitFor(() =>
      expect(screen.getByText(/尚未配置品牌×片区授权/)).toBeInTheDocument(),
    );
  });
});
