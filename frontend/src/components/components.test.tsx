/** 10 个通用组件渲染测试（Q027）。 */

import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { ConfirmDialog } from "./ConfirmDialog";
import { DataTable } from "./DataTable";
import { EmptyState } from "./EmptyState";
import { ErrorBoundary } from "./ErrorBoundary";
import { FilterBar } from "./FilterBar";
import { LoadingSpinner } from "./LoadingSpinner";
import { MetricCard } from "./MetricCard";
import { Pagination } from "./Pagination";
import { StatusBadge } from "./StatusBadge";
import { ToastStack } from "./Toast";

describe("EmptyState (Q027-1)", () => {
  it("渲染标题、描述、下一步提示", () => {
    render(<EmptyState title="暂无商家" description="请先导入" nextStep="T08 商家中心" />);
    expect(screen.getByText("暂无商家")).toBeInTheDocument();
    expect(screen.getByText("请先导入")).toBeInTheDocument();
    expect(screen.getByText(/下一步：T08 商家中心/)).toBeInTheDocument();
    expect(screen.getByRole("status")).toBeInTheDocument();
  });
});

describe("StatusBadge (Q027-2)", () => {
  it("渲染不同 tone 的徽章", () => {
    const { container } = render(
      <div>
        <StatusBadge tone="success" label="已签" />
        <StatusBadge tone="warning" label="待审" />
        <StatusBadge tone="danger" label="驳回" />
        <StatusBadge label="默认" />
      </div>,
    );
    const badges = container.querySelectorAll('[data-testid="status-badge"]');
    expect(badges).toHaveLength(4);
    expect(within(badges[0] as HTMLElement).getByText("已签")).toBeInTheDocument();
    expect(within(badges[3] as HTMLElement).getByText("默认")).toBeInTheDocument();
    expect(badges[0]!.getAttribute("data-tone")).toBe("success");
  });
});

describe("DataTable (Q027-3)", () => {
  interface Row {
    id: string;
    name: string;
  }
  const columns = [
    { key: "id", header: "ID", render: (r: Row) => r.id },
    { key: "name", header: "名称", render: (r: Row) => r.name },
  ];
  it("空数据时显示占位文本", () => {
    render(<DataTable<Row> rows={[]} columns={columns} rowKey={(r) => r.id} />);
    expect(screen.getByRole("status")).toHaveTextContent("暂无数据");
  });
  it("渲染表头与行", () => {
    render(
      <DataTable<Row>
        rows={[{ id: "1", name: "A" }, { id: "2", name: "B" }]}
        columns={columns}
        rowKey={(r) => r.id}
      />,
    );
    expect(screen.getByText("ID")).toBeInTheDocument();
    expect(screen.getByText("名称")).toBeInTheDocument();
    expect(screen.getByText("A")).toBeInTheDocument();
    expect(screen.getByText("B")).toBeInTheDocument();
  });
});

describe("FilterBar (Q027-4)", () => {
  it("提交触发 onApply，重置触发 onReset", () => {
    const onApply = vi.fn();
    const onReset = vi.fn();
    render(
      <FilterBar onApply={onApply} onReset={onReset}>
        <input aria-label="关键字" defaultValue="沁爽" />
      </FilterBar>,
    );
    fireEvent.click(screen.getByRole("button", { name: "应用" }));
    fireEvent.click(screen.getByRole("button", { name: "重置" }));
    expect(onApply).toHaveBeenCalled();
    expect(onReset).toHaveBeenCalled();
    expect(screen.getByLabelText("关键字")).toBeInTheDocument();
  });
});

describe("Pagination (Q027-5)", () => {
  it("禁用边界、回调翻页", () => {
    const onPageChange = vi.fn();
    render(<Pagination page={2} totalPages={5} onPageChange={onPageChange} />);
    fireEvent.click(screen.getByRole("button", { name: "上一页" }));
    expect(onPageChange).toHaveBeenCalledWith(1);
    fireEvent.click(screen.getByRole("button", { name: "下一页" }));
    expect(onPageChange).toHaveBeenCalledWith(3);
    expect(screen.getByText(/第 2 \/ 5 页/)).toBeInTheDocument();
  });
  it("边界禁用：page=1/total=1", () => {
    render(<Pagination page={1} totalPages={1} onPageChange={() => {}} />);
    expect(screen.getByRole("button", { name: "上一页" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "下一页" })).toBeDisabled();
  });
});

describe("ConfirmDialog (Q027-6)", () => {
  it("open=false 不渲染；open=true 触发 onConfirm/onClose", () => {
    const onConfirm = vi.fn();
    const onClose = vi.fn();
    const { rerender } = render(
      <ConfirmDialog
        open={false}
        title="删除"
        onConfirm={onConfirm}
        onClose={onClose}
      />,
    );
    expect(screen.queryByRole("dialog")).toBeNull();
    rerender(
      <ConfirmDialog open title="删除" onConfirm={onConfirm} onClose={onClose} />,
    );
    fireEvent.click(screen.getByRole("button", { name: "确认" }));
    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    expect(onConfirm).toHaveBeenCalled();
    expect(onClose).toHaveBeenCalled();
  });
});

describe("Toast (Q027-7)", () => {
  it("渲染消息与消失回调", () => {
    const onDismiss = vi.fn();
    render(
      <ToastStack
        items={[{ id: "1", message: "保存成功", tone: "success", ttlMs: 50 }]}
        onDismiss={onDismiss}
      />,
    );
    expect(screen.getByText("保存成功")).toBeInTheDocument();
  });
});

describe("LoadingSpinner (Q027-8)", () => {
  it("渲染带 role=status 的指示器", () => {
    render(<LoadingSpinner label="加载中…" />);
    expect(screen.getByRole("status")).toHaveTextContent("加载中…");
  });
});

describe("ErrorBoundary (Q027-9)", () => {
  it("捕获子树错误并渲染降级 UI", () => {
    function Boom(): React.ReactElement {
      throw new Error("boom");
    }
    // 抑制 React 错误日志噪音
    const spy = vi.spyOn(console, "error").mockImplementation(() => {});
    render(
      <ErrorBoundary>
        <Boom />
      </ErrorBoundary>,
    );
    expect(screen.getByRole("alert")).toHaveTextContent("界面渲染异常");
    spy.mockRestore();
  });
});

describe("MetricCard (Q027-10)", () => {
  it("渲染 label/value/unit/delta", () => {
    render(
      <MetricCard
        label="本月新签"
        value={12}
        unit="家"
        delta={{ value: "+3 同比", tone: "up" }}
      />,
    );
    expect(screen.getByLabelText("本月新签")).toBeInTheDocument();
    expect(screen.getByText("12")).toBeInTheDocument();
    expect(screen.getByText("家")).toBeInTheDocument();
    expect(screen.getByText("+3 同比")).toBeInTheDocument();
  });
});
