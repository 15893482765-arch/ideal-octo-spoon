"""Text2SQL 异常体系。

- ``PlanValidationError`` 携带结构化 ``issues``，可直接回喂给 LLM 做
  自我修正（每个 issue 都有机器可读 code 与出错路径）；
- ``PlanCompileError`` 是校验通过但编译期发现语义层配置不足（如行级
  范围缺少映射列）；
- 执行期异常刻意不携带 SQL 文本与参数值（防泄漏进日志/回答），
  审计信息由调用方从 ``CompiledQuery`` 单独留存。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class PlanIssue:
    """一条校验问题；``code`` 供程序分支，``message`` 供人/模型阅读。"""

    code: str
    message: str
    path: str = ""


class Text2SqlError(Exception):
    """Text2SQL 链路基类异常。"""


class PlanValidationError(Text2SqlError):
    def __init__(self, issues: list[PlanIssue]) -> None:
        self.issues: list[PlanIssue] = list(issues)
        detail = "; ".join(f"[{i.code}] {i.message}" for i in self.issues)
        super().__init__(f"Query Plan 校验失败（{len(self.issues)} 项）: {detail}")


class PlanCompileError(Text2SqlError):
    pass


class QueryTimeoutError(Text2SqlError):
    pass


class ExecutionRejectedError(Text2SqlError):
    """只读防线拦截（防御性，正常编译产物不会触发）。"""


class QueryExecutionError(Text2SqlError):
    def __init__(self, message: str, *, sqlstate: str | None = None) -> None:
        self.sqlstate = sqlstate
        super().__init__(message)
