"""A2 图表 spec 单测：形态判定、单位换算、空数据不出图、截断。"""

from __future__ import annotations

from decimal import Decimal

from app.services.assistant_chart import MAX_CHART_POINTS, build_chart
from app.text2sql.compiler import OutputColumn


def _dim(name: str, bucket: str | None = None, desc: str = "") -> OutputColumn:
    return OutputColumn(name=name, kind="dimension", unit=None, description=desc, bucket=bucket)


def _metric(name: str, unit: str = "money", desc: str = "") -> OutputColumn:
    return OutputColumn(name=name, kind="metric", unit=unit, description=desc)


def test_month_series_renders_line_and_converts_fen_to_yuan() -> None:
    columns = [_dim("sales_month", "month"), _metric("sales_amount", "money", "销售额")]
    rows = [("2026-08", Decimal("120000")), ("2026-09", Decimal("150000"))]

    chart = build_chart(columns, rows, question="最近六个月的销售额")

    assert chart is not None
    assert chart.chart_type == "line"
    assert chart.unit == "money_yuan"
    # 分 → 元，保留两位；不补点
    assert chart.series[0].data == [1200.0, 1500.0]
    assert chart.categories == ["2026-08", "2026-09"]


def test_time_series_is_sorted_by_bucket_label() -> None:
    columns = [_dim("sales_month", "month"), _metric("boxes", "box")]
    rows = [("2026-10", 10), ("2026-08", 30), ("2026-09", 20)]

    chart = build_chart(columns, rows, question="每月销量")

    assert chart is not None
    assert chart.categories == ["2026-08", "2026-09", "2026-10"]
    assert chart.series[0].data == [30.0, 20.0, 10.0]


def test_share_question_becomes_pie() -> None:
    columns = [_dim("sku_name"), _metric("boxes", "box")]
    rows = [("白桃气泡水", 30), ("苹果汁", 20), ("矿泉水", 50)]

    chart = build_chart(columns, rows, question="不同饮料的销量占比如何")

    assert chart is not None
    assert chart.chart_type == "pie"
    assert chart.unit == "box"
    assert chart.categories == ["白桃气泡水", "苹果汁", "矿泉水"]


def test_plain_comparison_becomes_bar() -> None:
    columns = [_dim("sku_name"), _metric("boxes", "box")]
    rows = [("白桃气泡水", 30), ("苹果汁", 20)]

    chart = build_chart(columns, rows, question="各产品销量对比")

    assert chart is not None
    assert chart.chart_type == "bar"


def test_single_time_point_is_not_a_line() -> None:
    columns = [_dim("sales_month", "month"), _metric("boxes", "box")]
    chart = build_chart(columns, [("2026-10", 12)], question="本月销量")

    assert chart is not None
    assert chart.chart_type == "bar"


def test_empty_rows_returns_none() -> None:
    columns = [_dim("sales_month", "month"), _metric("boxes", "box")]
    assert build_chart(columns, [], question="最近一周销量") is None


def test_all_null_values_returns_none() -> None:
    columns = [_dim("sku_name"), _metric("boxes", "box")]
    assert build_chart(columns, [("A", None), ("B", None)]) is None


def test_null_values_stay_null_without_filling() -> None:
    columns = [_dim("sales_month", "month"), _metric("boxes", "box")]
    chart = build_chart(columns, [("2026-08", None), ("2026-09", 5)], question="每月销量")

    assert chart is not None
    # 缺失点保持 None（前端断点），不补 0
    assert chart.series[0].data == [None, 5.0]


def test_multiple_dimensions_returns_none() -> None:
    columns = [
        _dim("sku_name"),
        _dim("region_name"),
        _metric("boxes", "box"),
    ]
    assert build_chart(columns, [("A", "郑州", 10)]) is None


def test_no_metric_returns_none() -> None:
    assert build_chart([_dim("sku_name")], [("A",)]) is None


def test_two_metrics_share_one_category_axis() -> None:
    columns = [
        _dim("finance_month", "month"),
        _metric("income", "money", "收入"),
        _metric("expense", "money", "支出"),
    ]
    rows = [("2026-09", Decimal("500000"), Decimal("-300000"))]

    chart = build_chart(columns, rows, question="最近一个月收支")

    assert chart is not None
    assert [s.name for s in chart.series] == ["收入", "支出"]
    assert chart.series[1].data == [-3000.0]


def test_series_name_drops_parenthetical_caveat() -> None:
    """语义层描述常带口径括号，图例只取短头，避免图例吃掉半张图。"""
    columns = [
        _dim("finance_month", "month"),
        _metric("income", "money", "收入合计（分，仅 INCOME 流水）"),
    ]

    chart = build_chart(columns, [("2026-09", 100)], question="月度收入")

    assert chart is not None
    assert chart.series[0].name == "收入合计"


def test_long_series_is_truncated_with_note() -> None:
    columns = [_dim("sku_name"), _metric("boxes", "box")]
    rows = [(f"SKU-{i:03d}", i) for i in range(MAX_CHART_POINTS + 10)]

    chart = build_chart(columns, rows, question="全部商品销量")

    assert chart is not None
    assert len(chart.categories) == MAX_CHART_POINTS
    assert chart.note is not None and str(MAX_CHART_POINTS) in chart.note


def test_question_becomes_title_and_is_truncated() -> None:
    columns = [_dim("sku_name"), _metric("boxes", "box")]
    long_question = "最近一周售出的不同饮料销量占比如何以及各自的环比变化" * 2

    chart = build_chart(columns, [("A", 1), ("B", 2)], question=long_question)

    assert chart is not None
    assert chart.title.endswith("…")
    assert len(chart.title) <= 40
