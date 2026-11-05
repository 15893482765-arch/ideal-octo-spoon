"""排班、地推活动、样品台账（v1.2 R39）。

T13 周排班与冲突原子校验：
- ``SchedulePlanBatch`` / ``SchedulePlanEntry``：经理生成的周排班草稿与发布批次；
- ``ResourceReservation``：人员在半开区间 [start_at,end_at) 的**通用**占用源。
  冲突口径（人员时间重叠、重复商家）只由 ScheduleConflictService 定义，DB 用
  tstzrange 的 GiST 排他约束兜底并发。T13 只让 SCHEDULE 写入，T15 再接入 CAMPAIGN。
旧的 slot 粒度 ``Schedule`` 模型（无 service/router 使用）保留不动。
"""

from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    Time,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import TSTZRANGE, ExcludeConstraint
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
    """地推活动（PRD §4.7 / R39）。

    生命周期：DRAFT → PUBLISHED → IN_PROGRESS → REVIEWED → CLOSED；
    PUBLISHED/IN_PROGRESS 可 CANCELLED（必须填原因）。
    - 发布时为每个参与人写一条 CAMPAIGN ResourceReservation（冲突只由
      ScheduleConflictService 裁决）；取消时释放未来时段占用；
    - 样品只走 ActivitySampleLedger 领用/消耗/归还台账，成本计入 sample_cost_fen，
      **不进确认销量与库存**（首版不做完整 WMS）。
    """

    __tablename__ = "activities"
    __table_args__ = (
        CheckConstraint(
            "status IN ('DRAFT','PUBLISHED','IN_PROGRESS','REVIEWED','CLOSED','CANCELLED')",
            name="activities_status_check",
        ),
        CheckConstraint("budget_fen >= 0", name="activities_budget_nonneg"),
        CheckConstraint("sample_cost_fen >= 0", name="activities_sample_cost_nonneg"),
        CheckConstraint("end_at > start_at", name="activities_window_order"),
        Index("ix_activities_window", "start_at", "end_at"),
        Index("ix_activities_region_status", "region_id", "status"),
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
    # T15：行级片区范围（服务端从创建参数落库，不从前端越权推断）
    region_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("regions.id", ondelete="RESTRICT"), nullable=False
    )
    # 取消留痕（已审核事实不物理删除，只状态迁移并记原因）
    cancel_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cancelled_by_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    # 复盘留痕
    reviewed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    reviewed_by_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    # 样品成本（分）：复盘时按实耗样品结算，计入活动费用，不进确认销量与库存（R39）
    sample_cost_fen: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False, default=0, server_default=text("0"),
    )


class ActivityParticipant(Base, AuditMixin, DatasetMixin):
    """活动参与人员：发布时为每人生成一条 CAMPAIGN 人员时间占用。"""

    __tablename__ = "activity_participants"
    __table_args__ = (
        UniqueConstraint("activity_id", "user_id",
                         name="uq_activity_participants_activity_user"),
        Index("ix_activity_participants_activity", "activity_id"),
        Index("ix_activity_participants_user", "user_id"),
    )

    id: Mapped[UUID] = pk_uuid()
    activity_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("activities.id", ondelete="RESTRICT"),
        nullable=False,
    )
    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )


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


# ============================== T13 周排班 ==============================


class SchedulePlanBatch(Base, AuditMixin, DatasetMixin):
    """一次周排班的批量草稿/发布批次（PRD §4.7，schedule.publish）。

    - DRAFT：可增删条目、不生成巡访任务与人员占用；
    - PUBLISHED：同事务原子生成 VisitTask 与 ResourceReservation（全有或全无）。
    批次状态只迁移不删除；幂等：同 (week_start_date, region) 至多一个已发布批次。
    """

    __tablename__ = "schedule_plan_batches"
    __table_args__ = (
        CheckConstraint(
            "status IN ('DRAFT','PUBLISHED','CANCELLED','COMPLETED')",
            name="schedule_plan_batches_status_check",
        ),
        CheckConstraint("lower(week_range) IS NOT NULL",
                        name="schedule_plan_batches_week_lower_notnull"),
        # 同一数据范围 + 同片区 + 同业务周至多一个已发布批次（部分唯一索引见迁移）
        Index("ix_schedule_plan_batches_region_week", "region_id", "week_start_date"),
    )

    id: Mapped[UUID] = pk_uuid()
    week_start_date: Mapped[date] = mapped_column(Date, nullable=False)  # Asia/Shanghai 周一
    # 业务周半开区间 [周一 00:00, 下周一 00:00)（本地展示口径的 UTC 边界由 Service 计算）
    week_range: Mapped[Any] = mapped_column(TSTZRANGE, nullable=False)
    region_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("regions.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=ScheduleStatus.DRAFT.value,
        server_default=text("'DRAFT'"),
    )
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    published_by_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(64), nullable=True)


class ScheduleTemplate(Base, AuditMixin, DatasetMixin):
    """每周循环排班模板（用户新增需求）。

    与 ``SchedulePlanEntry`` 的区别：模板不绑定日期，只描述"排给谁 + 周几 +
    商家 + 时段"，每周自动复用；一周内同一商家只排一次（发布批次仍按商家唯一），
    但同一天可以排多家不同商家。生成任务时按目标业务周把 weekday 换算成日期。
    """

    __tablename__ = "schedule_templates"
    __table_args__ = (
        CheckConstraint("weekday BETWEEN 1 AND 7", name="schedule_templates_weekday_check"),
        CheckConstraint("end_time > start_time", name="schedule_templates_time_order"),
        CheckConstraint("priority BETWEEN 1 AND 3",
                        name="schedule_templates_priority_range"),
        # 同一商家在同一片区模板里只出现一次（发布批次按 batch+merchant 唯一）
        UniqueConstraint("region_id", "merchant_id",
                         name="uq_schedule_templates_region_merchant"),
        Index("ix_schedule_templates_region_weekday", "region_id", "weekday"),
        Index("ix_schedule_templates_user", "assigned_user_id"),
    )

    id: Mapped[UUID] = pk_uuid()
    region_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("regions.id", ondelete="RESTRICT"), nullable=False
    )
    merchant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("merchants.id", ondelete="RESTRICT"), nullable=False
    )
    # 排给谁（业务员）
    assigned_user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    # 排在周几：1=周一 … 7=周日（ISO）
    weekday: Mapped[int] = mapped_column(Integer, nullable=False)
    start_time: Mapped[time] = mapped_column(Time, nullable=False)
    end_time: Mapped[time] = mapped_column(Time, nullable=False)
    priority: Mapped[int] = mapped_column(
        Integer, nullable=False, default=2, server_default=text("2")
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )


class SchedulePlanEntry(Base, AuditMixin, DatasetMixin):
    """批次内的一条排班条目（草稿可编辑；发布后原子转成 VisitTask + 占用）。"""

    __tablename__ = "schedule_plan_entries"
    __table_args__ = (
        CheckConstraint(
            "end_at IS NULL OR start_at IS NULL OR end_at > start_at",
            name="schedule_plan_entries_window_order",
        ),
        CheckConstraint("priority BETWEEN 1 AND 3",
                        name="schedule_plan_entries_priority_range"),
        Index("ix_schedule_plan_entries_batch", "batch_id"),
        Index("ix_schedule_plan_entries_merchant", "merchant_id"),
        Index("ix_schedule_plan_entries_user", "assigned_user_id"),
        UniqueConstraint("batch_id", "merchant_id",
                         name="uq_schedule_plan_entries_batch_merchant"),
    )

    id: Mapped[UUID] = pk_uuid()
    batch_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("schedule_plan_batches.id", ondelete="RESTRICT"),
        nullable=False,
    )
    merchant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("merchants.id", ondelete="RESTRICT"), nullable=False
    )
    assigned_user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    planned_date: Mapped[date] = mapped_column(Date, nullable=False)
    start_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 1=高 2=中 3=低（建议器打分后落库）
    priority: Mapped[int] = mapped_column(
        Integer, nullable=False, default=2, server_default=text("2")
    )
    # 发布后回链生成的巡访任务与占用（草稿期为 NULL）
    visit_task_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    reservation_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)


class ResourceReservation(Base, AuditMixin, DatasetMixin):
    """人员在半开区间 [start_at, end_at) 的通用占用源（PRD §4.7 R6）。

    - resource 一律为业务员（resource_user_id）；source_type 区分来源；
    - T13 只写 SCHEDULE；T15 地推活动发布时写 CAMPAIGN，冲突服务无需改动即可避让；
    - 同一资源在时间轴上的占用区间互不重叠（GiST 排他约束，btree_gist 提供 UUID 等值）。
    冲突业务口径只由 ScheduleConflictService 定义；本约束是并发兜底，不承载业务判定。
    """

    __tablename__ = "resource_reservations"
    __table_args__ = (
        CheckConstraint(
            "source_type IN ('SCHEDULE','CAMPAIGN')",
            name="resource_reservations_source_type_check",
        ),
        CheckConstraint(
            "status IN ('ACTIVE','RELEASED')",
            name="resource_reservations_status_check",
        ),
        CheckConstraint("end_at > start_at",
                        name="resource_reservations_window_order"),
        # 并发兜底：同一 ACTIVE 占用资源的时间区间不得重叠（半开区间，&&）
        ExcludeConstraint(
            ("resource_user_id", "="),
            ("occupied_range", "&&"),
            using="gist",
            postgresql_where=text("status = 'ACTIVE'"),
            name="excl_resource_reservations_active_range",
        ),
        Index("ix_resource_reservations_resource_time",
              "resource_user_id", "start_at", "end_at"),
        Index("ix_resource_reservations_merchant", "merchant_id"),
    )

    id: Mapped[UUID] = pk_uuid()
    resource_user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    # 占用区间（冗余 start_at/end_at 便于查询/视图；区间以 occupied_range 为权威）
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    occupied_range: Mapped[Any] = mapped_column(TSTZRANGE, nullable=False)
    source_type: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="ACTIVE", server_default=text("'ACTIVE'")
    )
    merchant_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("merchants.id", ondelete="RESTRICT"),
        nullable=True,
    )
    source_object_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_object_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
