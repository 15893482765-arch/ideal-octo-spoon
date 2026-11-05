"""督导跟访与整改（PRD §4.8 / R14 / R15 / 验收 A10，T16）。

- SupervisionVisit：每周跟访计划与现场记录（随访业务员、商家、照片、动作评分、
  陈列/服务/设备问题）。督导记录是**独立**事实，不写业务员 VisitTask/VisitRecord，
  因此不替代、也不计入业务员有效巡访/覆盖率（R14）。
- Rectification：跟访发现问题生成的整改单，五态状态机
  OPEN → IN_PROGRESS → SUBMITTED → VERIFIED → CLOSED；VERIFIED/CLOSED 必须由
  非整改提交人完成（职责分离）。

另保留 T03 预留的 SupervisorIssue（首版未被任何 service 写入），不做删除。
"""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import AuditMixin, Base, DatasetMixin, pk_uuid
from app.models.enums import RectificationStatus, SupervisionVisitStatus


class SupervisorIssue(Base, AuditMixin, DatasetMixin):
    __tablename__ = "supervisor_issues"
    __table_args__ = (
        CheckConstraint(
            "status IN ('OPEN','IN_PROGRESS','RESOLVED','OVERDUE')",
            name="supervisor_issues_status_check",
        ),
        Index("ix_supervisor_issues_merchant", "merchant_id"),
    )

    id: Mapped[UUID] = pk_uuid()
    merchant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("merchants.id", ondelete="RESTRICT"), nullable=False
    )
    supervisor_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    issue_type: Mapped[str] = mapped_column(String(32), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    due_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="OPEN", server_default=text("'OPEN'")
    )


class SupervisionVisit(Base, AuditMixin, DatasetMixin):
    """督导跟访（计划 + 现场记录合一）。"""

    __tablename__ = "supervision_visits"
    __table_args__ = (
        CheckConstraint(
            f"status IN ('{SupervisionVisitStatus.PLANNED.value}',"
            f"'{SupervisionVisitStatus.SUBMITTED.value}')",
            name="supervision_visits_status_check",
        ),
        CheckConstraint("action_score BETWEEN 0 AND 100", name="supervision_visits_score_range"),
        CheckConstraint("photo_count >= 0", name="supervision_visits_photo_nonneg"),
        Index("ix_supervision_visits_region_week", "region_id", "planned_date"),
        Index("ix_supervision_visits_supervisor", "supervisor_id"),
        Index("ix_supervision_visits_rep", "shadowed_user_id"),
        Index("ix_supervision_visits_merchant", "merchant_id"),
    )

    id: Mapped[UUID] = pk_uuid()
    region_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("regions.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=SupervisionVisitStatus.PLANNED.value,
        server_default=text("'PLANNED'"),
    )

    # 督导（记录人，SUPERVISOR 或经理）
    supervisor_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    # 被跟访的业务员（覆盖统计的对象；R15 每周覆盖 ≥2 名不同业务员）
    shadowed_user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    merchant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("merchants.id", ondelete="RESTRICT"), nullable=False
    )

    # 周计划
    planned_date: Mapped[date] = mapped_column(Date, nullable=False)

    # 现场记录（提交后落值）
    visited_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    photo_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    action_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    display_issue: Mapped[str | None] = mapped_column(Text, nullable=True)
    service_issue: Mapped[str | None] = mapped_column(Text, nullable=True)
    equipment_issue: Mapped[str | None] = mapped_column(Text, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    submitted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class Rectification(Base, AuditMixin, DatasetMixin):
    """整改单：跟访问题 → 责任人整改 → 提交 → 非同人验证 → 关闭。"""

    __tablename__ = "rectifications"
    __table_args__ = (
        CheckConstraint(
            "status IN ('OPEN','IN_PROGRESS','SUBMITTED','VERIFIED','CLOSED')",
            name="rectifications_status_check",
        ),
        CheckConstraint(
            "category IN ('DISPLAY','SERVICE','EQUIPMENT','OTHER')",
            name="rectifications_category_check",
        ),
        # 验证人≠提交人：允许提交前两列为空；一旦有验证人，则不得等于提交人
        CheckConstraint(
            "(verifier_id IS NULL) OR (verifier_id <> submitter_id)",
            name="rectifications_verifier_not_submitter"),
        Index("ix_rectifications_visit", "supervision_visit_id"),
        Index("ix_rectifications_owner_status", "owner_user_id", "status"),
        Index("ix_rectifications_region", "region_id"),
    )

    id: Mapped[UUID] = pk_uuid()
    region_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("regions.id", ondelete="RESTRICT"), nullable=False
    )
    supervision_visit_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("supervision_visits.id", ondelete="RESTRICT"),
        nullable=False,
    )
    merchant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("merchants.id", ondelete="RESTRICT"), nullable=False
    )
    category: Mapped[str] = mapped_column(String(16), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)

    # 整改责任人（业务员）与截止日
    owner_user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    due_date: Mapped[date] = mapped_column(Date, nullable=False)

    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=RectificationStatus.OPEN.value,
        server_default=text("'OPEN'"),
    )

    # 整改提交（责任人）
    submitter_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    submitted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    submit_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    # 验证（必须非提交人；通常为督导/经理）
    verifier_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    verify_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
