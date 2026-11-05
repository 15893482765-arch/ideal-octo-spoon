/** A2 图表组件：只渲染服务端给的数据、单位标签、卸载销毁。
 *
 * jsdom 没有 canvas，这里把 echarts 换成桩，只验证「传了什么 option」。
 */
import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { AssistantChartData } from "../../lib/assistant";

import { AssistantChart } from "./AssistantChart";

const { initMock, setOptionMock, resizeMock, disposeMock } = vi.hoisted(() => ({
  initMock: vi.fn(),
  setOptionMock: vi.fn(),
  resizeMock: vi.fn(),
  disposeMock: vi.fn(),
}));

vi.mock("echarts/core", () => ({
  use: vi.fn(),
  init: initMock.mockImplementation(() => ({
    setOption: setOptionMock,
    resize: resizeMock,
    dispose: disposeMock,
  })),
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

const LINE_CHART: AssistantChartData = {
  chart_type: "line",
  title: "最近一周的沁爽饮用水销量",
  categories: ["10-23", "10-24", "10-25"],
  series: [{ name: "确认销量", data: [10, 20, null] }],
  unit: "box",
  note: null,
  row_count: 3,
};

beforeEach(() => {
  initMock.mockClear();
  setOptionMock.mockClear();
  disposeMock.mockClear();
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe = vi.fn();
      disconnect = vi.fn();
    },
  );
});

describe("AssistantChart", () => {
  it("渲染标题、形态与单位标签", () => {
    render(<AssistantChart chart={LINE_CHART} />);

    expect(screen.getByText("最近一周的沁爽饮用水销量")).toBeInTheDocument();
    expect(screen.getByText("趋势")).toBeInTheDocument();
    expect(screen.getByText("单位：箱")).toBeInTheDocument();
    expect(screen.getByRole("img", { name: /图表/ })).toBeInTheDocument();
  });

  it("把服务端数据原样交给 echarts：缺失点保持 null，不补 0", () => {
    render(<AssistantChart chart={LINE_CHART} />);

    expect(initMock).toHaveBeenCalledTimes(1);
    const option = setOptionMock.mock.calls[0][0] as {
      xAxis: { data: string[] };
      series: { type: string; data: (number | null)[]; connectNulls: boolean }[];
    };
    expect(option.xAxis.data).toEqual(["10-23", "10-24", "10-25"]);
    expect(option.series[0].type).toBe("line");
    expect(option.series[0].data).toEqual([10, 20, null]);
    expect(option.series[0].connectNulls).toBe(false);
  });

  it("饼图按占比形态渲染", () => {
    render(
      <AssistantChart
        chart={{
          chart_type: "pie",
          title: "不同饮料销量占比",
          categories: ["白桃气泡水", "苹果汁"],
          series: [{ name: "确认销量", data: [30, 20] }],
          unit: "box",
          note: null,
          row_count: 2,
        }}
      />,
    );

    const option = setOptionMock.mock.calls[0][0] as {
      series: { type: string; data: { name: string; value: number }[] }[];
    };
    expect(option.series[0].type).toBe("pie");
    expect(option.series[0].data).toEqual([
      { name: "白桃气泡水", value: 30 },
      { name: "苹果汁", value: 20 },
    ]);
    expect(screen.getByText("占比")).toBeInTheDocument();
  });

  it("卸载时销毁实例，避免切换会话残留", () => {
    const { unmount } = render(<AssistantChart chart={LINE_CHART} />);
    unmount();
    expect(disposeMock).toHaveBeenCalledTimes(1);
  });

  it("超量截断的提示会展示", () => {
    render(<AssistantChart chart={{ ...LINE_CHART, note: "数据点较多，仅展示前 50 条" }} />);
    expect(screen.getByText("数据点较多，仅展示前 50 条")).toBeInTheDocument();
  });
});
