"""MCP 写工具结构化确认令牌（T24 / PRD §5.4「执行预览 + 结构化确认」）。

写工具第一次调用只产出 `execution_preview` 并由**服务端**签发一次性确认令牌；
结构化确认 UI 携带令牌二次调用，网关校验通过后才真正执行对应 Service。

令牌绑定（PRD §5.4，任一不符即拒绝）：
- `actor_id`：发起人；换人确认无效；
- `session_id`：会话；跨会话确认无效；
- `tool_name`：工具；令牌不可跨工具复用；
- `params_hash`：业务参数摘要；改参确认无效；
- `idempotency_key`：幂等键；与最终执行的幂等键严格一致；
- `expires_at`：有效期；过期需重新预览。

一次性：执行消费后置 `consumed_at`，并以 `(tool_name, token_hash)` 唯一约束
防止并发重复消费；业务回滚时消费记录一并回滚，不产生“已消费但未执行”。

令牌原文不落库：只存其 SHA-256 摘要（`token_hash`），响应里一次性下发原文。
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import CHAR, CheckConstraint, DateTime, Index, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, pk_uuid

STATUS_ISSUED = "ISSUED"
STATUS_CONSUMED = "CONSUMED"


class McpConfirmationToken(Base):
    __tablename__ = "mcp_confirmation_tokens"
    __table_args__ = (
        # 同一令牌原文全库唯一（防并发重复消费）；令牌按工具隔离签发
        UniqueConstraint(
            "tool_name", "token_hash", name="uq_mcp_confirm_tool_token"
        ),
        CheckConstraint(
            "status IN ('ISSUED','CONSUMED')",
            name="mcp_confirmation_tokens_status_check",
        ),
        Index("ix_mcp_confirm_actor", "actor_id"),
        Index("ix_mcp_confirm_expires", "expires_at"),
        Index("ix_mcp_confirm_idem", "idempotency_key"),
    )

    id: Mapped[UUID] = pk_uuid()
    # 绑定要素
    tool_name: Mapped[str] = mapped_column(String(64), nullable=False)
    # 不加 users 外键：安全审计记录独立留存，避免用户删除/跨域造数被外键阻塞
    # （对齐幂等表 idempotency_keys.actor_id 的可空 UUID 口径）。
    actor_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    session_id: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    # 业务参数摘要（SHA-256，规范化 JSON），与幂等 params_hash 同算法
    params_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    # 令牌原文摘要（不存明文）
    token_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    # 执行预览快照：供执行前二次核对，并在消费时返回一致性依据
    preview: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=STATUS_ISSUED,
        server_default=text("'ISSUED'"),
    )
    issued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    consumed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
