/** 纸质合同区块：A4 预览 / 打印 / 草稿期手工修订（PRD §4.4 扩展）。 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ContractBodyCard } from "./Contracts";

const CONTRACT = {
  id: "c-1",
  merchant_id: "m-1",
  contract_no: "HT-001",
  title: "冰柜陈列合作协议",
  party_name: "悦购便利店",
  status: "DRAFT",
  version: 1,
  template_version: null,
  signed_at: null,
  effective_from: "2026-10-16",
  expire_at: "2027-04-16",
  commission_rate: 3.5,
  display_subsidy_amount: null,
  display_threshold_pct: 50,
  commission_rule_version: "comm-v2026-01",
  store_manager_name: "王店长",
  body_text:
    "冰柜陈列合作协议\n\n合同编号：HT-001\n\n乙方（合作门店）：悦购便利店\n门店店长：王店长\n\n签署日期：2026-10-19",
  sales_rep_user_id: null,
  manager_user_id: null,
  attachment_id: null,
  has_attachment: false,
  renewal_generated_for_expire_at: null,
  renewed_from_contract_id: null,
  created_at: null,
};

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("纸质合同区块", () => {
  it("在线预览渲染正文：商家店名/店长姓名/日期已填入", () => {
    render(<ContractBodyCard contract={CONTRACT} editable onSaved={() => {}} />);

    expect(screen.getByText(/悦购便利店/)).toBeInTheDocument();
    expect(screen.getByText(/王店长/)).toBeInTheDocument();
    expect(screen.getByText(/2026-10-19/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /打印/ })).toBeInTheDocument();
  });

  it("修订正文保存时提交 body_text，并回传合同必填字段", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify(CONTRACT), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const onSaved = vi.fn();

    render(<ContractBodyCard contract={CONTRACT} editable onSaved={onSaved} />);
    fireEvent.click(screen.getByRole("button", { name: "修订正文" }));
    fireEvent.change(screen.getByRole("textbox"), {
      target: { value: "手工修订：用电由乙方承担。" },
    });
    fireEvent.click(screen.getByRole("button", { name: "保存正文" }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/contracts/c-1");
    expect(String(init.method).toUpperCase()).toBe("PUT");
    const body = JSON.parse(String(init.body)) as Record<string, unknown>;
    expect(body.body_text).toBe("手工修订：用电由乙方承担。");
    expect(body.contract_no).toBe("HT-001");
    expect(body.merchant_id).toBe("m-1");
    expect(body.title).toBe("冰柜陈列合作协议");
    await waitFor(() => expect(onSaved).toHaveBeenCalledTimes(1));
  });

  it("打印按钮打开 A4 打印窗口并写入合同正文", () => {
    const write = vi.fn();
    const win = {
      document: { write, close: vi.fn() },
      focus: vi.fn(),
      print: vi.fn(),
    };
    const openSpy = vi
      .spyOn(window, "open")
      .mockReturnValue(win as unknown as Window);

    render(<ContractBodyCard contract={CONTRACT} editable={false} onSaved={() => {}} />);
    fireEvent.click(screen.getByRole("button", { name: /打印/ }));

    expect(openSpy).toHaveBeenCalled();
    const html = String(write.mock.calls[0]?.[0] ?? "");
    expect(html).toContain("size: A4");
    expect(html).toContain("悦购便利店");
    expect(win.print).toHaveBeenCalledTimes(1);
  });

  it("非草稿状态不显示修订入口", () => {
    render(
      <ContractBodyCard contract={CONTRACT} editable={false} onSaved={() => {}} />,
    );
    expect(screen.queryByRole("button", { name: "修订正文" })).not.toBeInTheDocument();
  });
});
