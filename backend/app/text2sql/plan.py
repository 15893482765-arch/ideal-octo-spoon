"""Query Plan：LLM 与确定性编译器之间的唯一契约。

模型只允许输出本文件定义的 JSON 结构——不允许写 SQL。所有名字
（实体/指标/维度/列）必须来自语义层，由 ``validator`` 逐一核对；
所有取值经编译器参数化，用户输入永远不进 SQL 文本。
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# 单次查询行数硬上限（执行器另有 max_rows 兜底）
MAX_PLAN_LIMIT = 1000

_MONTH_PATTERN = r"^\d{4}-(0[1-9]|1[0-2])$"


class FilterOp(StrEnum):
    EQ = "eq"
    NE = "ne"
    GT = "gt"
    GE = "ge"
    LT = "lt"
    LE = "le"
    IN = "in"
    LIKE = "like"
    BETWEEN = "between"
    IS_NULL = "is_null"
    NOT_NULL = "not_null"


class RelativeRange(StrEnum):
    TODAY = "today"
    YESTERDAY = "yesterday"
    LAST_7D = "last_7d"
    LAST_30D = "last_30d"
    THIS_MONTH = "this_month"
    LAST_MONTH = "last_month"


class MetricRef(BaseModel):
    """引用一个语义层指标（name 必须是 catalog 中的指标名）。"""

    model_config = ConfigDict(extra="forbid")

    name: str
    alias: str | None = Field(default=None, max_length=64)


class DimensionRef(BaseModel):
    """引用一个语义层维度（分组列）。"""

    model_config = ConfigDict(extra="forbid")

    name: str
    alias: str | None = Field(default=None, max_length=64)


class Filter(BaseModel):
    """结构化过滤条件。

    - ``entity`` 缺省为主实体；列必须在对应实体的语义层列定义中；
    - ``in``/``between`` 用 ``values``（between 恰好 2 个）；
    - ``is_null``/``not_null`` 不带值；其余 op 用 ``value``。
    """

    model_config = ConfigDict(extra="forbid")

    column: str
    op: FilterOp
    entity: str | None = None
    value: Any | None = None
    values: list[Any] | None = None


class TimeRange(BaseModel):
    """时间过滤。

    - absolute：闭区间语义（end 当天计入，编译器换算为左闭右开）；
    - month：业务月 "YYYY-MM"（月度实体直接等值，日期实体换算该月起止）；
    - relative：以执行当日为基准的常用相对窗口。
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["absolute", "month", "relative"]
    start: date | None = None
    end: date | None = None
    month: str | None = Field(default=None, pattern=_MONTH_PATTERN)
    relative: RelativeRange | None = None


class OrderBy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(description="指标/维度的 name 或 alias")
    direction: Literal["asc", "desc"] = "desc"


class QueryPlan(BaseModel):
    """一个分析问题对应的结构化查询计划。

    ``entity`` 是主实体（事实表）；指标/维度/过滤可引用经语义层
    relations 可达的其他实体，join 路径由校验器推导（模型不需要、
    也不允许描述 join）。
    """

    model_config = ConfigDict(extra="forbid")

    question: str = Field(default="", description="原始问题（仅审计用途，不进 SQL）")
    entity: str
    metrics: list[MetricRef] = Field(default_factory=list)
    dimensions: list[DimensionRef] = Field(default_factory=list)
    filters: list[Filter] = Field(default_factory=list)
    time_range: TimeRange | None = None
    order_by: list[OrderBy] = Field(default_factory=list)
    limit: int | None = Field(default=None, ge=1, le=MAX_PLAN_LIMIT)


def plan_json_schema() -> dict[str, Any]:
    """导出 JSON Schema，供 prompt/结构化输出约束使用。"""
    return QueryPlan.model_json_schema()
