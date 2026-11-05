"""只读执行器：编译产物的唯一执行通道。

防线（纵深防御，尽管 SQL 已由编译器保证形态）：
1. 每个查询在 ``SET TRANSACTION READ ONLY`` 事务中执行——数据库级
   拒绝任何写操作，即便上游防线全部失守；
2. ``statement_timeout`` 事务级生效（SET LOCAL 语义，连接归还后
   无残留）；
3. 行数双保险：编译器内嵌 LIMIT + 执行器 fetchmany 截断；
4. 异常消息不携带 SQL/参数（防值泄漏进日志），超时单独分类。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

import psycopg2
from sqlalchemy.engine import Engine

from .compiler import CompiledQuery
from .errors import (
    ExecutionRejectedError,
    QueryExecutionError,
    QueryTimeoutError,
)

DEFAULT_STATEMENT_TIMEOUT_MS = 15000
DEFAULT_MAX_ROWS = 1000


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[dict[str, Any]]  # 值已做 JSON 友好转换
    row_count: int
    elapsed_ms: float
    truncated: bool  # 触及执行器 max_rows 兜底（正常应靠 SQL LIMIT）
    sql: str  # 审计留存
    primary_entity: str


class ReadOnlyExecutor:
    def __init__(
        self,
        engine: Engine,
        *,
        statement_timeout_ms: int = DEFAULT_STATEMENT_TIMEOUT_MS,
        max_rows: int = DEFAULT_MAX_ROWS,
    ) -> None:
        if statement_timeout_ms <= 0:
            raise ValueError("statement_timeout_ms 必须为正")
        if max_rows <= 0:
            raise ValueError("max_rows 必须为正")
        self._engine = engine
        self._timeout_ms = statement_timeout_ms
        self._max_rows = max_rows

    def execute(self, compiled: CompiledQuery) -> QueryResult:
        started = time.monotonic()
        connection = self._engine.raw_connection()
        try:
            cursor = connection.cursor()
            # psycopg2 隐式 BEGIN 后的事务首批语句：只读 + 超时（事务级，无残留）
            cursor.execute("SET TRANSACTION READ ONLY")
            cursor.execute(
                "SELECT set_config('statement_timeout', %s, true)",
                (str(self._timeout_ms),),
            )
            cursor.execute(compiled.sql, compiled.params)
            column_names = [desc[0] for desc in cursor.description or []]
            fetched = cursor.fetchmany(self._max_rows + 1)
            truncated = len(fetched) > self._max_rows
            rows = [
                dict(zip(column_names, (_jsonable(value) for value in row), strict=True))
                for row in fetched[: self._max_rows]
            ]
            elapsed = (time.monotonic() - started) * 1000
            return QueryResult(
                columns=column_names,
                rows=rows,
                row_count=len(rows),
                elapsed_ms=round(elapsed, 1),
                truncated=truncated,
                sql=compiled.sql,
                primary_entity=compiled.primary_entity,
            )
        except psycopg2.errors.QueryCanceled as exc:
            raise QueryTimeoutError(
                f"查询超过 statement_timeout（{self._timeout_ms}ms）被取消"
            ) from exc
        except psycopg2.errors.ReadOnlySqlTransaction as exc:
            raise ExecutionRejectedError(
                "只读事务拒绝了写操作（防线触发，请上报该查询）"
            ) from exc
        except psycopg2.Error as exc:
            sqlstate = getattr(exc.diag, "sqlstate", None) if exc.diag else None
            raise QueryExecutionError(
                f"查询执行失败（sqlstate={sqlstate or 'unknown'}）",
                sqlstate=sqlstate,
            ) from exc
        finally:
            connection.close()

    def explain_estimate(self, compiled: CompiledQuery) -> int | None:
        """EXPLAIN 估算行数（可选预检；估算偏差大，只作参考不强制）。"""
        connection = self._engine.raw_connection()
        try:
            cursor = connection.cursor()
            cursor.execute("SET TRANSACTION READ ONLY")
            cursor.execute(
                "SELECT set_config('statement_timeout', %s, true)",
                (str(self._timeout_ms),),
            )
            cursor.execute(
                "EXPLAIN (FORMAT JSON) " + compiled.sql, compiled.params
            )
            payload = cursor.fetchone()[0]
            plan = payload[0]["Plan"] if payload else None
            return int(plan["Plan Rows"]) if plan and "Plan Rows" in plan else None
        except psycopg2.Error:
            return None
        finally:
            connection.close()


def _jsonable(value: Any) -> Any:
    """结果值 JSON 友好化：Decimal→float、日期→ISO、UUID→str。"""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return str(value)
