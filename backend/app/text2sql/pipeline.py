"""Text2SQL 端到端编排：检索 -> 规划（LLM）-> 上下文注入 -> 编译 -> 执行。

分层职责：
- ``plan()``：唯一含 LLM 调用的阶段（调用方应经 AiGateway 包装以获得
  断路器/审计/降级）；
- ``query_context()``：dataset_id 与行级范围只能在这里由服务端按 actor
  解析注入（口径红线：模型与客户端都不可指定）；
- ``execute()``：纯确定性阶段，无 LLM、只读事务。

检索器/执行器走模块级单例（embedding 模型与连接都是重资源）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.permissions import ActorContext, get_profile
from app.db.session import engine_sync
from app.models import User
from app.retrieval import ContextBuilder
from app.retrieval.context import SemanticContext
from app.retrieval.retriever import build_default_retriever
from app.semantic import get_default_catalog
from app.semantic.catalog import SemanticCatalog
from app.services.report_scope import MerchantScope, resolve_merchant_scope

from .compiler import CompiledQuery, compile_query
from .errors import Text2SqlError
from .executor import QueryResult, ReadOnlyExecutor
from .planner import LlmClient, PlanGeneration, PlanGenerator
from .scope import QueryContext, RowScope


@dataclass(frozen=True)
class PlannedQuery:
    question: str
    generation: PlanGeneration
    context: SemanticContext


@dataclass(frozen=True)
class ExecutedQuery:
    planned: PlannedQuery
    compiled: CompiledQuery
    result: QueryResult


class Text2SqlPipeline:
    def __init__(
        self,
        session: Session,
        *,
        llm: LlmClient,
        catalog: SemanticCatalog | None = None,
        retriever: Any = None,
        context_builder: ContextBuilder | None = None,
        executor: ReadOnlyExecutor | None = None,
        max_rounds: int = 2,
    ) -> None:
        self._session = session
        self._catalog = catalog or get_default_catalog()
        self._retriever = retriever or get_text2sql_retriever()
        self._builder = context_builder or ContextBuilder(
            self._catalog,
            token_budget=get_settings().retrieval_context_token_budget,
        )
        self._executor = executor or get_text2sql_executor()
        self._generator = PlanGenerator(
            llm, self._catalog, max_rounds=max_rounds
        )

    # ---- LLM 阶段（调用方负责 AiGateway 包装） ----

    def plan(
        self,
        *,
        question: str,
        history: list[dict[str, str]] | tuple[dict[str, str], ...] = (),
        today: date | None = None,
    ) -> PlannedQuery:
        """检索语义上下文并生成校验通过的查询计划。

        :raises CannotAnswerError / PlanGenerationError: 规划失败（上层回退）
        :raises LlmUnavailableError: 模型不可用（上层网关降级）
        """
        retrieval = self._retriever.search(question)
        context = self._builder.build(question, retrieval)
        generation = self._generator.generate(
            question, context, history=history, today=today
        )
        return PlannedQuery(
            question=question, generation=generation, context=context
        )

    # ---- 确定性阶段 ----

    def query_context(
        self, actor: ActorContext, *, today: date | None = None
    ) -> QueryContext:
        """按 actor 注入数据集与行级范围（模型永远接触不到这一步）。"""
        me = self._session.get(User, actor.user_id)
        if me is None:
            raise Text2SqlError("无法构建查询上下文：用户不存在")
        profile = get_profile(actor.role_code)
        scope = resolve_merchant_scope(self._session, actor)
        return QueryContext(
            dataset_id=me.dataset_id,
            scope=_row_scope_for(actor, profile.row_scope, scope),
            today=today or date.today(),
        )

    def execute(
        self,
        *,
        actor: ActorContext,
        planned: PlannedQuery,
        today: date | None = None,
    ) -> ExecutedQuery:
        compiled = compile_query(
            planned.generation.resolved,
            self.query_context(actor, today=today),
        )
        result = self._executor.execute(compiled)
        return ExecutedQuery(planned=planned, compiled=compiled, result=result)


def _row_scope_for(
    actor: ActorContext, profile_row_scope: str, scope: MerchantScope
) -> RowScope | None:
    """MerchantScope -> RowScope；brand_region 叠加品牌约束。

    注意保留空集合语义：「有范围维度但集合为空」= 永假（看不到任何行），
    而不是「不限」。
    """
    if scope.is_global:
        return None
    if profile_row_scope == "brand_region":
        return RowScope(
            region_ids=scope.region_ids,
            brand_ids=tuple(sorted(actor.brand_ids)),
        )
    return RowScope(
        region_ids=scope.region_ids,
        merchant_ids=scope.merchant_ids,
    )


# ---------------------------------------------------------------------------
# 模块级装配（重资源单例）
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def get_text2sql_retriever() -> Any:
    """默认检索器单例（Qdrant 关闭/故障时内部自动降级关键词匹配）。"""
    return build_default_retriever()


@lru_cache(maxsize=1)
def get_text2sql_executor() -> ReadOnlyExecutor:
    settings = get_settings()
    return ReadOnlyExecutor(
        engine_sync(),
        statement_timeout_ms=settings.text2sql_statement_timeout_ms,
        max_rows=settings.text2sql_max_rows,
    )


def reset_text2sql_singletons() -> None:
    """测试/配置变更后重建单例。"""
    get_text2sql_retriever.cache_clear()
    get_text2sql_executor.cache_clear()
