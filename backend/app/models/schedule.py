"""排班、地推活动、样品台账（v1.2 R39）。"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import AuditMixin, Base, DatasetMixin, pk_uuid
from app.models.enums import ActivityStatus, ScheduleStatus


class Schedule(Base, AuditMixin, DatasetMixin):
    """周排班（PRD §3.6）。"""

    __tablename__ = "schedules"
    __table_args__ = (
        CheckConstraint(
            "status IN ('DRAFT','PUBLISHED','CANCELLED','COMPLETED')",
            name="schedules_status_check",
        ),
        # 同一用户在同一时段只可有一条已发布排班
        UniqueConstraint(
            "user_id", "week_start_date", "slot_index", name="uq_schedules_user_slot"
        ),
        Index("ix_schedules_week", "week_start_date"),
    )

    id: Mapped[UUID] = pk_uuid()
    week_start_date: Mapped[date] = mapped_column(Date, nullable=False)
    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    merchant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("merchants.id", ondelete="RESTRICT"), nullable=False
    )
    slot_index: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=ScheduleStatus.DRAFT.value,
        server_default=text("'DRAFT'"),
    )
    priority_score: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )


class Activity(Base, AuditMixin, DatasetMixin):
    __tablename__ = "activities"
    __table_args__ = (
        CheckConstraint(
            "status IN ('DRAFT','PUBLISHED','IN_PROGRESS','REVIEWED','CLOSED','CANCELLED')",
            name="activities_status_check",
        ),
        CheckConstraint("budget_fen >= 0", name="activities_budget_nonneg"),
        Index("ix_activities_window", "start_at", "end_at"),
    )

    id: Mapped[UUID] = pk_uuid()
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    location: Mapped[str] = mapped_column(String(255), nullable=False)
    owner_user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    budget_fen: Mapped[int] = mapped_column(
        nullable=False, default=0, server_default=text("0")
    )
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=ActivityStatus.DRAFT.value,
        server_default=text("'DRAFT'"),
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)


class ActivitySampleLedger(Base, AuditMixin, DatasetMixin):
    """v1.2 R39：地推携带商品按样品领用/消耗/归还记录。

    样品不计入确认销量与库存（与 SalesConfirmation 严格隔离）。
    """

    __tablename__ = "activity_sample_ledgers"
    __table_args__ = (
        CheckConstraint(
            "stage IN ('TAKEN_OUT','CONSUMED','RETURNED','MISSING')",
            name="activity_samples_stage_check",
        ),
        CheckConstraint("boxes >= 0", name="activity_samples_boxes_nonneg"),
        Index("ix_activity_samples_activity", "activity_id"),
    )

    id: Mapped[UUID] = pk_uuid()
    activity_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("activities.id", ondelete="RESTRICT"), nullable=False
    )
    sku_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("skus.id", ondelete="RESTRICT"), nullable=False
    )
    stage: Mapped[str] = mapped_column(String(16), nullable=False)
    boxes: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    operator_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)
