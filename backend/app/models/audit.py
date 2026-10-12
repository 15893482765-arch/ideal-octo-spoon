"""审计、AI 日志、导入批次。"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import AuditMixin, Base, DatasetMixin, pk_uuid


class AuditLog(Base):
    """全局审计：写操作与审计同事务（PRD §5/§8 红线、R23、R30）。

    - T04 起 AuditService 统一写本表：actor、原因（reason）、前后值
      （before_json/after_json）、request_id 全部落列，普通用户无修改入口；
    - before/after 在写入前经 redact() 脱敏，凭据正文永不入库；
    - 本表只增不改：Service 层不提供 UPDATE/DELETE 路径。
    """

    __tablename__ = "audit_logs"
    __table_args__ = (
        CheckConstraint(
            "event_type IN ('CREATE','UPDATE','STATE_TRANSITION','DELETE_DENIED')",
            name="audit_logs_event_type_check",
        ),
        Index("ix_audit_logs_object", "object_type", "object_id"),
        Index("ix_audit_logs_actor_time", "actor_id", "occurred_at"),
    )

    id: Mapped[UUID] = pk_uuid()
    object_type: Mapped[str] = mapped_column(String(48), nullable=False)
    object_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    event_type: Mapped[str] = mapped_column(String(24), nullable=False)
    actor_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # T04：操作原因（驳回/终止/状态迁移必须填原因，R24）
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # T04：变更前后快照（JSONB，写入前已脱敏）
    before_json: Mapped[dict[str, Any] | None] = mapped_column(nullable=True)
    after_json: Mapped[dict[str, Any] | None] = mapped_column(nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(
        nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )


class AiLog(Base, DatasetMixin):
    __tablename__ = "ai_logs"
    __table_args__ = (
        Index("ix_ai_logs_user_time", "user_id", "occurred_at"),
    )

    id: Mapped[UUID] = pk_uuid()
    user_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    tool_name: Mapped[str] = mapped_column(String(48), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    request_summary: Mapped[str] = mapped_column(Text, nullable=False)
    response_summary: Mapped[str] = mapped_column(Text, nullable=False)
    refused: Mapped[bool] = mapped_column(
        nullable=False, default=False, server_default=text("false")
    )


class ImportBatch(Base, AuditMixin):
    __tablename__ = "import_batches"
    __table_args__ = (
        CheckConstraint(
            "dataset_type IN ('PRODUCTION','DEMO')",
            name="import_batches_dataset_type_check",
        ),
        CheckConstraint(
            "status IN ('PENDING','RUNNING','SUCCEEDED','FAILED','ROLLED_BACK')",
            name="import_batches_status_check",
        ),
    )

    id: Mapped[UUID] = pk_uuid()
    dataset_type: Mapped[str] = mapped_column(String(16), nullable=False)
    source_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    rows_total: Mapped[int] = mapped_column(nullable=False, default=0, server_default=text("0"))
    rows_imported: Mapped[int] = mapped_column(nullable=False, default=0, server_default=text("0"))
    rows_rejected: Mapped[int] = mapped_column(nullable=False, default=0, server_default=text("0"))
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="PENDING", server_default=text("'PENDING'")
    )
    error_log: Mapped[str | None] = mapped_column(Text, nullable=True)
    imported_by_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )


class AuthEvent(Base):
    """认证域事件审计（PRD §2.1、§9.4）。

    T03 阶段先覆盖登录/激活/改密/重置/登出/被踢；通用业务审计由 T04 接管。
    不写凭据正文；payload 仅放事件前后结构化字段。
    """

    __tablename__ = "auth_events"
    __table_args__ = (
        CheckConstraint(
            "event_type IN ('LOGIN_SUCCESS','LOGIN_FAILED','ACTIVATION_CONSUMED',"
            "'PASSWORD_CHANGED','PASSWORD_RESET_ISSUED','PASSWORD_RESET_CONSUMED',"
            "'TOKEN_REVOKED','LOGOUT','ACCOUNT_LOCKED')",
            name="auth_events_event_type_check",
        ),
        Index("ix_auth_events_user_time", "user_id", "occurred_at"),
        Index("ix_auth_events_actor_time", "actor_id", "occurred_at"),
    )

    id: Mapped[UUID] = pk_uuid()
    user_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    # 触发者：可能与 user_id 不同（系统管理员重置普通用户时 actor_id=管理员）
    actor_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(
        nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
