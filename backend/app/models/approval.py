"""审批与任务/待办。"""

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
from app.models.enums import ApprovalStatus, TaskStatus


class ApprovalRequest(Base, AuditMixin, DatasetMixin):
    __tablename__ = "approval_requests"
    __table_args__ = (
        CheckConstraint(
            "status IN ('PENDING','APPROVED','REJECTED','RETURNED','WITHDRAWN')",
            name="approval_status_check",
        ),
        # 同一业务对象同一审批动作只允许一条 PENDING 记录（R23 重复审批防护）
        Index(
            "uq_approvals_pending_target",
            "target_object_type",
            "target_object_id",
            "action",
            unique=True,
            postgresql_where=text("status = 'PENDING'"),
        ),
        Index("ix_approvals_target", "target_object_type", "target_object_id"),
        Index("ix_approvals_status", "status"),
    )

    id: Mapped[UUID] = pk_uuid()
    target_object_type: Mapped[str] = mapped_column(String(32), nullable=False)
    target_object_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    # 审批动作类型：与 ApprovalMatrix.action 一致（如 contract.create / freezer.recall）
    action: Mapped[str] = mapped_column(
        String(48), nullable=False, default="", server_default=text("''")
    )
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
    # T05 节点能力校验：发起人原始上下文（不存密码；只存元数据便于审计/复审）
    request_payload: Mapped[dict[str, Any]] = mapped_column(
        nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )


class TaskItem(Base, AuditMixin):
    """待办/任务中心（不含演示数据隔离——v1.2 合同到期等自动生成的待办）。

    T05：
    - 幂等创建：以 (related_object_type, related_object_id, task_kind) 唯一
      （R27：相同商家/类型/责任人且未关闭的任务不得重复创建）；
    - 复核任务：以 task_kind 前缀 `VERIFICATION::` 标记，禁止直接 complete，
      必须由关联单据动作关闭（Q023）。
    """

    __tablename__ = "task_items"
    __table_args__ = (
        CheckConstraint(
            "status IN ('OPEN','IN_PROGRESS','CLOSED')",
            name="task_items_status_check",
        ),
        CheckConstraint(
            "task_kind IN ('VISIT_PLANNED','CONTRACT_RENEWAL_REVIEW','FREEZER_RECALL',"
            "'FREEZER_ACCEPTANCE','FREEZER_DEPOSIT_REFUND','REPLENISHMENT_REVIEW',"
            "'PROBLEM_RECTIFICATION','SUPERVISION_RECTIFICATION','PROBLEM_RECTIFICATION_VERIFY',"
            "'CONTRACT_RENEWAL_VERIFY','CONTRACT_EXPIRING_RENEWAL_EVAL','OTHER')",
            name="task_items_task_kind_check",
        ),
        CheckConstraint(
            "completion_source IN ('DIRECT','RELATED_DOCUMENT','SYSTEM','OTHER') OR "
            "completion_source IS NULL",
            name="task_items_completion_source_check",
        ),
        Index("ix_task_items_assignee_status", "assignee_id", "status"),
        # 幂等：同 (related_object_type, related_object_id, task_kind) 仅允许一条 OPEN/IN_PROGRESS
        Index(
            "uq_task_items_idempotent",
            "related_object_type",
            "related_object_id",
            "task_kind",
            unique=True,
            postgresql_where=text("status IN ('OPEN','IN_PROGRESS')"),
        ),
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
    # T05：复核任务标记（task_kind 以 VERIFICATION:: 前缀或此列显式 True）
    requires_related_document: Mapped[bool] = mapped_column(
        nullable=False, default=False, server_default=text("false")
    )
    # T05：完成来源（DIRECT/RELATED_DOCUMENT/SYSTEM/OTHER），仅用于留痕
    completion_source: Mapped[str | None] = mapped_column(String(24), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_by_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
