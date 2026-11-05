"""AI 助手会话持久化（PRD §5.1 扩展；方法见《03.Agent状态管理》第 4/5 节）。

设计（对照资料）：
- **按员工和会话编号隔离**：`AssistantSession.user_id` 归属员工，任何人只能
  读写自己的会话；`user_id` 不加 users 外键（与审计表同理：用户停用后会话
  记录仍保留，且避免级联清理）；
- **每轮成功后整体保存**：`AssistantMessage` 按 `seq` 记录完整消息
  （user/assistant），assistant 消息的 `meta` JSONB 保存工具调用摘要
  （工具名、状态、条目数、高亮、草稿引用），等价于资料中 messages 里的
  工具调用与结果，供「继续对话」时重建上下文；
- **需求状态 facts**：`AssistantSession.facts` JSONB 保存每轮确定性解析出的
  关键信息（最近工具、周期、检索词），上下文超长时作为压缩摘要的头部；
- **上下文压缩**：读取历史喂模型时按字符阈值保留最近若干轮（见
  `assistant_session_service.history_for_model`），对应资料 `compact_context()`。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
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

# 会话标题取首条用户消息（资料：「历史会话第一句信息陈列在旁边」）
TITLE_MAX = 60

ROLE_USER = "user"
ROLE_ASSISTANT = "assistant"
ALLOWED_ROLES = frozenset({ROLE_USER, ROLE_ASSISTANT})


class AssistantSession(Base):
    __tablename__ = "assistant_sessions"
    __table_args__ = (
        CheckConstraint("message_count >= 0", name="assistant_sessions_count_nonneg"),
        Index("ix_assistant_sessions_user_recent", "user_id", "last_message_at"),
    )

    id: Mapped[UUID] = pk_uuid()

    # 归属员工（行级隔离唯一依据；不加外键，理由见模块 docstring）
    user_id: Mapped[Any] = mapped_column(PG_UUID(as_uuid=True), nullable=False)

    # 首条用户消息截断（列表页陈列用）
    title: Mapped[str] = mapped_column(String(120), nullable=False, default="")

    # 需求状态（资料第 2/5 节 facts）：确定性解析出的最近工具/周期/检索词
    facts: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )

    message_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    last_message_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class AssistantMessage(Base):
    __tablename__ = "assistant_messages"
    __table_args__ = (
        CheckConstraint(
            "role IN ('user','assistant')", name="assistant_messages_role_check"
        ),
        Index("ix_assistant_messages_session_seq", "session_id", "seq", unique=True),
    )

    id: Mapped[UUID] = pk_uuid()

    # 会话删除时消息一并清理（会话是消息的唯一宿主）
    session_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("assistant_sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    # 会话内序号（单调递增，上下文按此排序重建）
    seq: Mapped[int] = mapped_column(Integer, nullable=False)

    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")

    # assistant 消息的工具摘要：tool / tool_status / item_count / highlights / draft /
    # degraded —— 等价于资料 messages 里的工具调用与结果记录
    meta: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
