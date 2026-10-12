"""审批与任务/待办。"""

from __future__ import annotations

from datetime import datetime
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
from app.models.enums import ApprovalStatus, TaskStatus


class ApprovalRequest(Base, AuditMixin, DatasetMixin):
    __tablename__ = "approval_requests"
    __table_args__ = (
        CheckConstraint(
            "status IN ('PENDING','APPROVED','REJECTED','WITHDRAWN')",
            name="approval_status_check",
        ),
        Index("ix_approvals_target", "target_object_type", "target_object_id"),
        Index("ix_approvals_status", "status"),
    )

    id: Mapped[UUID] = pk_uuid()
    target_object_type: Mapped[str] = mapped_column(String(32), nullable=False)
    target_object_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    requester_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    approver_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=ApprovalStatus.PENDING.value,
        server_default=text("'PENDING'"),
    )
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    matrix_role_code: Mapped[str] = mapped_column(String(32), nullable=False)


class TaskItem(Base, AuditMixin):
    """待办/任务中心（不含演示数据隔离——v1.2 合同到期等自动生成的待办）。"""

    __tablename__ = "task_items"
    __table_args__ = (
        CheckConstraint(
            "status IN ('OPEN','IN_PROGRESS','CLOSED')",
            name="task_items_status_check",
        ),
        Index("ix_task_items_assignee_status", "assignee_id", "status"),
    )

    id: Mapped[UUID] = pk_uuid()
    title: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    assignee_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    related_object_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    related_object_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    # 幂等键：v1.2 自动生成待办（如到期续签评估）以 (object_type, object_id, task_kind) 唯一
    task_kind: Mapped[str] = mapped_column(String(48), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=TaskStatus.OPEN.value, server_default=text("'OPEN'")
    )
