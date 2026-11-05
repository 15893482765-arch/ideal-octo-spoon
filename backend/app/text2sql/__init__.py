"""Text2SQL 确定性内核：Query Plan -> 校验 -> 编译 -> 只读执行。

链路约定：LLM 只产出 ``QueryPlan``（结构化 JSON，不写 SQL）；
``validate_plan`` 按语义层硬校验（错误可回喂模型修正）；
``compile_query`` 确定性生成参数化 SQL；``ReadOnlyExecutor``
在只读事务内执行。数据集隔离与行级范围由 ``QueryContext`` 注入，
模型与客户端均不可指定。
"""

from .compiler import CompiledQuery, OutputColumn, compile_query
from .errors import (
    ExecutionRejectedError,
    PlanCompileError,
    PlanIssue,
    PlanValidationError,
    QueryExecutionError,
    QueryTimeoutError,
    Text2SqlError,
)
from .executor import QueryResult, ReadOnlyExecutor
from .pipeline import (
    ExecutedQuery,
    PlannedQuery,
    Text2SqlPipeline,
    get_text2sql_executor,
    get_text2sql_retriever,
)
from .planner import (
    CannotAnswerError,
    PlanGeneration,
    PlanGenerationError,
    PlanGenerator,
)
from .plan import (
    DimensionRef,
    Filter,
    FilterOp,
    MetricRef,
    OrderBy,
    QueryPlan,
    RelativeRange,
    TimeRange,
    plan_json_schema,
)
from .scope import QueryContext, RowScope
from .validator import ResolvedPlan, validate_plan

__all__ = [
    "CannotAnswerError",
    "CompiledQuery",
    "DimensionRef",
    "ExecutedQuery",
    "ExecutionRejectedError",
    "Filter",
    "FilterOp",
    "MetricRef",
    "OrderBy",
    "OutputColumn",
    "PlanCompileError",
    "PlanGeneration",
    "PlanGenerationError",
    "PlanGenerator",
    "PlanIssue",
    "PlanValidationError",
    "PlannedQuery",
    "QueryContext",
    "QueryExecutionError",
    "QueryPlan",
    "QueryResult",
    "QueryTimeoutError",
    "ReadOnlyExecutor",
    "RelativeRange",
    "ResolvedPlan",
    "RowScope",
    "Text2SqlError",
    "Text2SqlPipeline",
    "TimeRange",
    "compile_query",
    "get_text2sql_executor",
    "get_text2sql_retriever",
    "plan_json_schema",
    "validate_plan",
]
