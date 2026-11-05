/** AI 助手图表（A2）：把后端确定性生成的图表配置渲染为 ECharts。
 *
 * 约定：
 * - 只渲染服务端给出的 categories/series，不在前端补点或补 0；
 * - 图表是「文字答案的配图」，视觉上弱于正文，卡片化嵌在气泡内；
 * - 按需注册图表类型与组件，避免整包引入 echarts。
 */

import { useEffect, useMemo, useRef } from "react";
import { BarChart, LineChart, PieChart } from "echarts/charts";
import {
  GridComponent,
  LegendComponent,
  TooltipComponent,
} from "echarts/components";
import * as echarts from "echarts/core";
import type { EChartsCoreOption } from "echarts/core";
import { CanvasRenderer } from "echarts/renderers";

import type { AssistantChartData } from "../../lib/assistant";

echarts.use([
  LineChart,
  BarChart,
  PieChart,
  GridComponent,
  TooltipComponent,
  LegendComponent,
  CanvasRenderer,
]);

/** 品牌青绿主色系，与财务趋势图、状态标签保持一致。 */
const PALETTE = [
  "#0F766E",
  "#14B8A6",
  "#0EA5E9",
  "#10B981",
  "#F59E0B",
  "#6366F1",
  "#EF4444",
  "#94A3B8",
];

const UNIT_LABEL: Record<AssistantChartData["unit"], string> = {
  box: "单位：箱",
  money_yuan: "单位：元",
  count: "单位：条",
  other: "按日/按月",
};

const TYPE_LABEL: Record<AssistantChartData["chart_type"], string> = {
  line: "趋势",
  bar: "对比",
  pie: "占比",
};

function formatValue(value: number | null, unit: AssistantChartData["unit"]): string {
  if (value === null) return "无数据";
  const text = value.toLocaleString("zh-CN", {
    minimumFractionDigits: unit === "count" ? 0 : 2,
    maximumFractionDigits: 2,
  });
  if (unit === "money_yuan") return `¥${text}`;
  if (unit === "box") return `${text} 箱`;
  return text;
}

/** 折线面积：同色渐变，避免大块实色压过正文。 */
function areaGradient(color: string) {
  return {
    type: "linear" as const,
    x: 0,
    y: 0,
    x2: 0,
    y2: 1,
    colorStops: [
      { offset: 0, color: `${color}40` },
      { offset: 1, color: `${color}05` },
    ],
  };
}

function buildOption(chart: AssistantChartData): EChartsCoreOption {
  const { chart_type: type, categories, series, unit } = chart;
  const valueFormatter = (value: unknown) =>
    formatValue(typeof value === "number" ? value : null, unit);
  const axisCommon = {
    axisLine: { lineStyle: { color: "#E2E8F0" } },
    axisTick: { show: false },
    axisLabel: { color: "#94A3B8", fontSize: 10 },
  };
  const tooltip = {
    trigger: type === "pie" ? "item" : "axis",
    confine: true,
    backgroundColor: "rgba(255,255,255,0.97)",
    borderColor: "#E2E8F0",
    borderWidth: 1,
    padding: [6, 10],
    textStyle: { color: "#0F172A", fontSize: 11 },
    valueFormatter,
  };
  const legend =
    series.length > 1
      ? {
          top: 0,
          right: 0,
          itemWidth: 8,
          itemHeight: 8,
          icon: "circle",
          textStyle: { color: "#475569", fontSize: 10 },
        }
      : undefined;

  if (type === "pie") {
    const total = (series[0]?.data ?? []).reduce<number>(
      (sum, value) => sum + (typeof value === "number" ? value : 0),
      0,
    );
    return {
      color: PALETTE,
      tooltip,
      series: [
        {
          type: "pie",
          radius: ["55%", "80%"],
          center: ["50%", "52%"],
          avoidLabelOverlap: true,
          itemStyle: { borderColor: "#FFFFFF", borderWidth: 2, borderRadius: 4 },
          label: {
            color: "#475569",
            fontSize: 10,
            formatter: "{b} {d}%",
          },
          labelLine: { length: 8, length2: 8, lineStyle: { color: "#CBD5E1" } },
          data: categories.map((name, index) => ({
            name,
            value: series[0]?.data[index] ?? null,
          })),
        },
      ],
      graphic: total
        ? [
            {
              type: "text",
              left: "center",
              top: "46%",
              silent: true,
              style: {
                text: `合计\n${formatValue(total, unit)}`,
                textAlign: "center",
                fill: "#0F172A",
                fontSize: 11,
                lineHeight: 16,
              },
            },
          ]
        : undefined,
    };
  }

  const isLine = type === "line";
  return {
    color: PALETTE,
    tooltip,
    legend,
    grid: { left: 4, right: 12, top: series.length > 1 ? 24 : 12, bottom: 4, containLabel: true },
    xAxis: {
      type: "category",
      data: categories,
      boundaryGap: !isLine,
      ...axisCommon,
      axisLabel: {
        ...axisCommon.axisLabel,
        interval: categories.length > 12 ? "auto" : 0,
        hideOverlap: true,
      },
    },
    yAxis: {
      type: "value",
      ...axisCommon,
      splitLine: { lineStyle: { color: "#F1F5F9", type: "dashed" } },
    },
    series: series.map((item, index) => {

      const color = PALETTE[index % PALETTE.length];
      return {
        name: item.name,
        type: isLine ? "line" : "bar",
        smooth: isLine,
        showSymbol: false,
        symbol: "circle",
        symbolSize: 6,
        connectNulls: false,
        barMaxWidth: 26,
        itemStyle: { color, borderRadius: isLine ? 0 : [4, 4, 0, 0] },
        lineStyle: isLine ? { width: 2, color } : undefined,
        areaStyle: isLine ? { color: areaGradient(color) } : undefined,
        emphasis: { focus: "series" },
        data: item.data,
      };
    }),
  };
}

export function AssistantChart({ chart }: { chart: AssistantChartData }) {
  const holderRef = useRef<HTMLDivElement>(null);
  const instanceRef = useRef<echarts.ECharts | null>(null);
  const option = useMemo(() => buildOption(chart), [chart]);

  useEffect(() => {
    const holder = holderRef.current;
    if (!holder) return;
    const instance = echarts.init(holder, undefined, { renderer: "canvas" });
    instanceRef.current = instance;
    // 容器宽度随气泡变化：原地 resize，不重建实例
    const observer = new ResizeObserver(() => instance.resize());
    observer.observe(holder);
    return () => {
      observer.disconnect();
      instance.dispose();
      instanceRef.current = null;
    };
  }, []);

  useEffect(() => {
    instanceRef.current?.setOption(option, true);
  }, [option]);

  return (
    <figure className="mt-2 overflow-hidden rounded-lg border border-slate-200 bg-white shadow-sm">
      <figcaption className="flex items-center justify-between gap-2 border-b border-slate-100 px-3 py-2">
        <span className="truncate text-[12px] font-medium text-slate-700">
          {chart.title}
        </span>
        <span className="flex shrink-0 items-center gap-1">
          <span className="rounded-full bg-teal-50 px-2 py-0.5 text-[10px] font-medium text-teal-700">
            {TYPE_LABEL[chart.chart_type]}
          </span>
          <span className="rounded-full bg-slate-50 px-2 py-0.5 text-[10px] text-slate-500">
            {UNIT_LABEL[chart.unit]}
          </span>
        </span>
      </figcaption>
      <div
        ref={holderRef}
        role="img"
        aria-label={`${chart.title}图表`}
        className="h-[220px] w-full sm:h-[260px]"
      />
      {chart.note ? (
        <p className="px-3 pb-2 text-[10px] text-slate-400">{chart.note}</p>
      ) : null}
    </figure>
  );
}
