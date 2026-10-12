"""巡访任务与巡访记录（PostGIS 地理点）。"""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from geoalchemy2 import Geography
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import AuditMixin, Base, DatasetMixin, pk_uuid
from app.models.enums import VisitRecordStatus, VisitTaskStatus


class VisitTask(Base, AuditMixin, DatasetMixin):
    __tablename__ = "visit_tasks"
    __table_args__ = (
        CheckConstraint(
            "status IN ('PENDING','IN_PROGRESS','COMPLETED','OVERDUE','ABNORMAL','INVALIDATED')",
            name="visit_tasks_status_check",
        ),
        Index("ix_visit_tasks_merchant", "merchant_id"),
        Index("ix_visit_tasks_user_date", "assigned_user_id", "planned_date"),
    )

    id: Mapped[UUID] = pk_uuid()
    merchant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("merchants.id", ondelete="RESTRICT"), nullable=False
    )
    assigned_user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    planned_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=VisitTaskStatus.PENDING.value,
        server_default=text("'PENDING'"),
    )
    reason: Mapped[str | None] = mapped_column(String(255), nullable=True)


class VisitRecord(Base, AuditMixin, DatasetMixin):
    """正式巡访记录：同一任务仅一条正式记录（unique partial index）。"""

    __tablename__ = "visit_records"
    __table_args__ = (
        CheckConstraint(
            "status IN ('DRAFT','SUBMITTED','RECOGNIZED','ABNORMAL','REJECTED')",
            name="visit_records_status_check",
        ),
        CheckConstraint(
            "display_pct IS NULL OR (display_pct BETWEEN 0 AND 100)",
            name="visit_records_display_range",
        ),
        Index("ix_visit_records_merchant", "merchant_id"),
        Index("ix_visit_records_task", "visit_task_id"),
    )

    id: Mapped[UUID] = pk_uuid()
    visit_task_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("visit_tasks.id", ondelete="RESTRICT"), nullable=False
    )
    merchant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("merchants.id", ondelete="RESTRICT"), nullable=False
    )
    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    # 巡访实际位置（WGS84）
    location: Mapped[Geography | None] = mapped_column(
        Geography(geometry_type="POINT", srid=4326, spatial_index=True), nullable=True
    )
    submitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    display_pct: Mapped[int | None] = mapped_column(nullable=True)
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=VisitRecordStatus.SUBMITTED.value,
        server_default=text("'SUBMITTED'"),
    )
    is_official: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 幂等键：客户端提交时携带的 request_id，重放时由数据库唯一约束拒绝
    idempotency_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 同一任务下只允许一条正式记录（unique partial index 在迁移中创建）
    __visit_official_unique_sql__ = (
        UniqueConstraint("visit_task_id", name="uq_visit_records_official_per_task")
    )
