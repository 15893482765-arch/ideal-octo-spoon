"""T31: ai_interaction_logs.intent 增加 TEXT2SQL

Revision ID: c1a8f5e3b207
Revises: b4e7a2c9d1f8
Create Date: 2026-10-22 10:00:00.000000

Text2SQL 接入助手后，分析统计类查询（LLM 产 QueryPlan、确定性编译、
只读执行）以 TEXT2SQL 意图写入审计。仅放宽 intent CHECK 约束，无表结构变更。
"""

from collections.abc import Sequence

from alembic import op

revision: str = "c1a8f5e3b207"
down_revision: str | Sequence[str] | None = "b4e7a2c9d1f8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CONSTRAINT = "ai_interaction_logs_intent_check"
_TABLE = "ai_interaction_logs"

_INTENTS_OLD = (
    "intent IN ('VISIT_COMPOSE','SCHEDULE_SUGGEST','BUSINESS_INSIGHT',"
    "'REPLENISHMENT_AGGREGATE','QA','OTHER')"
)
_INTENTS_NEW = (
    "intent IN ('VISIT_COMPOSE','SCHEDULE_SUGGEST','BUSINESS_INSIGHT',"
    "'REPLENISHMENT_AGGREGATE','QA','OTHER','TEXT2SQL')"
)


def upgrade() -> None:
    op.drop_constraint(_CONSTRAINT, _TABLE, type_="check")
    op.create_check_constraint(_CONSTRAINT, _TABLE, _INTENTS_NEW)


def downgrade() -> None:
    op.drop_constraint(_CONSTRAINT, _TABLE, type_="check")
    op.create_check_constraint(_CONSTRAINT, _TABLE, _INTENTS_OLD)
