"""AI 交互审计日志（T26 / PRD §5.4 第 6 条、§9.3 AiGateway、R30）。

记录内容（PRD §5.4-6）：脱敏后的问题、解析意图、允许工具、数据范围、
工具参数摘要、结果引用、用户确认引用、模型版本、耗时与错误。

红线：
- **原始问题默认不落库**：只存脱敏后的 prompt（密码/凭据/完整电话/精确坐标一律
  剔除或掩码）；如合规必须保留原文须独立加密（首版不做，见遗留 TODO）；
- 不记密码、完整电话、精确坐标（§9.4）；参数摘要同样脱敏；
- 本表只增不改：不提供 UPDATE/DELETE 路径；
- 审计与 AI 调用在同一事务（AiInteractionService 不自行 commit）；
- actor_id 不加 users 外键：与 MCP 令牌/幂等表一致，用户停用时审计仍须保留，
  且避免清理用户历史时级联影响审计。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, pk_uuid

# 五类 AI 能力（PRD §5.5 降级矩阵），用于 intent 归类与逐能力降级
INTENT_VISIT_COMPOSE = "VISIT_COMPOSE"
INTENT_SCHEDULE_SUGGEST = "SCHEDULE_SUGGEST"
INTENT_BUSINESS_INSIGHT = "BUSINESS_INSIGHT"
INTENT_REPLENISHMENT = "REPLENISHMENT_AGGREGATE"
INTENT_QA = "QA"
INTENT_TEXT2SQL = "TEXT2SQL"
INTENT_OTHER = "OTHER"

ALLOWED_INTENTS = frozenset(
    {
        INTENT_VISIT_COMPOSE,
        INTENT_SCHEDULE_SUGGEST,
        INTENT_BUSINESS_INSIGHT,
        INTENT_REPLENISHMENT,
        INTENT_QA,
        INTENT_TEXT2SQL,
        INTENT_OTHER,
    }
)

# 终态
STATUS_OK = "OK"
STATUS_DEGRADED = "DEGRADED"
STATUS_REFUSED = "REFUSED"
STATUS_ERROR = "ERROR"
ALLOWED_STATUS = frozenset({STATUS_OK, STATUS_DEGRADED, STATUS_REFUSED, STATUS_ERROR})


class AiInteractionLog(Base):
    __tablename__ = "ai_interaction_logs"
    __table_args__ = (
        CheckConstraint(
            "intent IN ('VISIT_COMPOSE','SCHEDULE_SUGGEST','BUSINESS_INSIGHT',"
            "'REPLENISHMENT_AGGREGATE','QA','OTHER','TEXT2SQL')",
            name="ai_interaction_logs_intent_check",
        ),
        CheckConstraint(
            "status IN ('OK','DEGRADED','REFUSED','ERROR')",
            name="ai_interaction_logs_status_check",
        ),
        CheckConstraint("latency_ms >= 0", name="ai_interaction_logs_latency_nonneg"),
        Index("ix_ai_interaction_user_time", "user_id", "occurred_at"),
        Index("ix_ai_interaction_intent_time", "intent", "occurred_at"),
        Index("ix_ai_interaction_request", "request_id"),
    )

    id: Mapped[UUID] = pk_uuid()

    # 受信身份（服务端注入；可空以容纳鉴权前拒绝的调用），不加 users 外键
    user_id: Mapped[Any | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    role_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    # 解析意图（服务端按工具映射，绝不采信模型自报的越权意图）
    intent: Mapped[str] = mapped_column(String(32), nullable=False)
    # 本次实际允许调用的工具（白名单 ∩ 角色后的结果），多个用逗号分隔
    allowed_tools: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # 数据范围（服务端注入的 region/brand/data_scope 摘要），JSONB
    scope_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )

    # 脱敏后的问题（原始问题默认不落库）
    prompt_redacted: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # 脱敏后的工具参数摘要（不含身份字段；敏感值已掩码）
    params_summary_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )

    # 结果/确认引用（只存引用标识，不存敏感正文）
    result_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)
    confirm_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # 模型与可观测性
    model: Mapped[str] = mapped_column(String(64), nullable=False)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # attempts=0 表示断路器打开、本次根本未调用模型
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )
    degraded: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    degraded_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 防注入：本次剥离/拒绝的越权指令（如模型在外部内容里夹带“忽略审批”）
    injection_blocked: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    blocked_directives_json: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )

    status: Mapped[str] = mapped_column(String(16), nullable=False, default=STATUS_OK)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
