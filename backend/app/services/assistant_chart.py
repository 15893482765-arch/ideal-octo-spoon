"""把 Text2SQL 的确定性查询结果转成前端可直接渲染的图表配置（A2 图表）。

红线：
- 数据只来自查询结果的 rows，**不补点、不插值、不猜缺失值**；
- 没有行、没有可用数值、列形态不适合画图时返回 ``None``，
  由调用方如实回答"未查询到数据，无法生成图表"；
- 纯函数：不读库、不调模型、不写任何表，便于单测与复用。

图表形态判定（确定性规则，不看模型脸色）：
- 维度是时间分桶（day/week/month）且点数 ≥ 2 → 折线；
- 问题含"占比/比例/构成/分布/结构"，单指标、分类数 2~8、值非负 → 环形饼图；
- 其余（含只有 1 个点）→ 柱状。
"""

from __future__ import annotations

import re
from typing import Any, Literal, Sequence

from pydantic import BaseModel, Field

from app.text2sql.compiler import OutputColumn

# 最多画多少个分类：再多图就糊了，超出截断并在 note 说明
MAX_CHART_POINTS = 50
# 饼图分类上限（超过就不是"占比"而是"排行榜"）
MAX_PIE_SLICES = 8
# 触发饼图的问句信号词
_PIE_SIGNALS = ("占比", "比例", "构成", "分布", "结构", "份额")
# 单位映射：语义层 unit → 前端展示口径
_UNIT_ALIASES = {
    "money": "money_yuan",
    "box": "box",
    "count": "count",
}


class ChartSeries(BaseModel):
    name: str
    # 允许 None：某分类确实没有数据时断点，绝不补 0 或插值
    data: list[float | None]


class ChartSpec(BaseModel):
    """图表配置：只描述"画什么"，渲染细节交给前端 ECharts。"""

    chart_type: Literal["line", "bar", "pie"]
    title: str
    categories: list[str]
    series: list[ChartSeries] = Field(default_factory=list)
    unit: Literal["box", "money_yuan", "count", "other"] = "other"
    note: str | None = None
    row_count: int = 0


def build_chart(
    columns: Sequence[OutputColumn],
    rows: Sequence[Sequence[Any]],
    *,
    question: str = "",
    max_points: int = MAX_CHART_POINTS,
) -> ChartSpec | None:
    """按确定性规则生成图表配置；不具备画图条件时返回 None。"""
    dimensions = [c for c in columns if c.kind == "dimension"]
    metrics = [c for c in columns if c.kind == "metric"]
    # 多维度更适合表格；无指标则没有可画的数值
    if len(dimensions) != 1 or not 1 <= len(metrics) <= 2:
        return None
    if not rows:
        return None

    dimension = dimensions[0]
    index = {c.name: i for i, c in enumerate(columns)}
    dim_index = index[dimension.name]
    metric_indexes = [(m, index[m.name]) for m in metrics]

    points: list[tuple[str, list[float | None]]] = []
    for row in rows:
        label = _label(row[dim_index])
        values = [_to_float(row[i]) for _, i in metric_indexes]
        points.append((label, values))

    # 全空数值：没有任何可展示的数据，不出图（不臆造 0）
    if not any(v is not None for _, values in points for v in values):
        return None

    note: str | None = None
    if len(points) > max_points:
        note = f"数据点较多，仅展示前 {max_points} 条"
        points = points[:max_points]

    is_time_series = dimension.bucket in {"day", "week", "month"}
    if is_time_series:
        # 分桶输出为 'YYYY-MM' / 'YYYY-MM-DD' / 'IYYY-"W"IW'，字典序即时间序
        points.sort(key=lambda item: item[0])

    unit = _normalize_unit(metrics[0].unit)
    categories = [label for label, _ in points]
    series = [
        ChartSeries(
            name=_series_name(metric),
            data=[_scale(values[pos], metric.unit) for _, values in points],
        )
        for pos, (metric, _) in enumerate(metric_indexes)
    ]

    chart_type = _pick_type(
        is_time_series=is_time_series,
        question=question,
        series_count=len(series),
        categories=categories,
        first_series=series[0].data if series else [],
    )
    return ChartSpec(
        chart_type=chart_type,
        title=_title(question, dimension, metrics[0]),
        categories=categories,
        series=series,
        unit=unit,
        note=note,
        row_count=len(points),
    )


def build_payroll_chart(raw: dict[str, Any] | None) -> ChartSpec | None:
    """工资单规则路径的图表：按业务月画应发金额趋势。

    规则命中的「工资」问句不会进 Text2SQL（payroll.list 优先级更高），
    这里把同一批 items 复用成柱/折线，保证文字与图表同源。
    """
    items = (raw or {}).get("items") or []
    if not isinstance(items, list) or not items:
        return None
    rows: list[list[Any]] = []
    names: set[str] = set()
    for row in items:
        if not isinstance(row, dict):
            continue
        period = row.get("period_month")
        if not period:
            continue
        # PAID 后以更正单金额为准，与文字摘要口径一致
        amount = row.get("corrected_pay_fen")
        if amount is None:
            amount = row.get("gross_pay_fen")
        rows.append([period, amount])
        name = str(row.get("user_name") or "").strip()
        if name:
            names.add(name)
    if not rows:
        return None
    metric_desc = f"{next(iter(names))} 应发工资" if len(names) == 1 else "应发工资"
    return build_chart(
        [
            OutputColumn(
                name="period_month",
                kind="dimension",
                unit=None,
                description="业务月",
                bucket="month",
            ),
            OutputColumn(
                name="gross_pay_fen",
                kind="metric",
                unit="money",
                description=metric_desc,
            ),
        ],
        rows,
        question="",
    )


def _series_name(metric: OutputColumn) -> str:
    """图例名取描述的短头（去掉括号里的口径说明），避免图例吃掉半张图。"""
    text = (metric.description or metric.name or "").strip()
    head = re.split(r"[（(]", text, maxsplit=1)[0]
    head = head.split("，")[0].strip()
    return head[:12] or metric.name


def _label(value: Any) -> str:
    """维度值转分类标签；空值显式标注，不伪装成正常分类。"""
    if value is None:
        return "未知"
    if hasattr(value, "isoformat"):
        return str(value.isoformat())
    text = str(value).strip()
    return text or "未知"


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalize_unit(unit: str | None) -> Literal["box", "money_yuan", "count", "other"]:
    return _UNIT_ALIASES.get(unit or "", "other")  # type: ignore[return-value]


def _scale(value: float | None, unit: str | None) -> float | None:
    """单位换算：金额分转元；箱数与计数保留两位、其余原样。"""
    if value is None:
        return None
    if unit == "money":
        return round(value / 100, 2)
    if unit in {"box", "count"}:
        return round(value, 2)
    return round(value, 2)


def _pick_type(
    *,
    is_time_series: bool,
    question: str,
    series_count: int,
    categories: list[str],
    first_series: list[float | None],
) -> Literal["line", "bar", "pie"]:
    if is_time_series and len(categories) >= 2:
        return "line"
    wants_share = any(signal in question for signal in _PIE_SIGNALS)
    values = [v for v in first_series if v is not None]
    if (
        wants_share
        and series_count == 1
        and 2 <= len(categories) <= MAX_PIE_SLICES
        and values
        and all(v >= 0 for v in values)
    ):
        return "pie"
    return "bar"


def _title(question: str, dimension: OutputColumn, metric: OutputColumn) -> str:
    text = (question or "").strip()
    if text:
        return text if len(text) <= 40 else text[:38] + "…"
    dim = dimension.description or dimension.name
    met = metric.description or metric.name
    return f"{dim} · {met}"
