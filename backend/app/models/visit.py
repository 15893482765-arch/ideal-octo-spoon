"""巡访任务、巡访记录、打卡证据与修订（PostGIS 地理点，PRD §4.6 / §4.8 / T11）。

红线：
- 原始定位/到离店时间/照片一经提交**不可覆盖**（无更新接口），补充说明只追加到
  ``visit_revisions``；照片"先上传后绑定"（``visit_photos`` 只增不改）。
- 距离由服务端 PostGIS 重算落 ``distance_meters``，不采信前端上报距离。
- 督导跟访（source=SUPERVISION）独立成记录，**不计业务员覆盖率**。
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from geoalchemy2 import Geography
from sqlalchemy import (
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
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import AuditMixin, Base, DatasetMixin, pk_uuid
from app.models.enums import VisitRecordStatus, VisitSource, VisitTaskStatus

_VISIT_SOURCES = "('WEEKLY_SCHEDULE','AD_HOC','RECTIFICATION','SUPERVISION')"


class VisitTask(Base, AuditMixin, DatasetMixin):
    """巡访任务：商家、负责人、计划日期/时间窗、优先级、来源、状态（§4.6）。"""

    __tablename__ = "visit_tasks"
    __table_args__ = (
        CheckConstraint(
            "status IN ('PENDING','IN_PROGRESS','COMPLETED','OVERDUE','ABNORMAL','INVALIDATED')",
            name="visit_tasks_status_check",
        ),
        CheckConstraint(f"source IN {_VISIT_SOURCES}", name="visit_tasks_source_check"),
        CheckConstraint("priority BETWEEN 1 AND 3", name="visit_tasks_priority_range"),
        CheckConstraint(
            "window_end_at IS NULL OR window_start_at IS NULL "
            "OR window_end_at >= window_start_at",
            name="visit_tasks_window_order",
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
    # 计划时间窗（当日本地时刻，供移动端显示与导航）
    window_start_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    window_end_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # 1=高 2=中 3=低（默认中）
    priority: Mapped[int] = mapped_column(
        Integer, nullable=False, default=2, server_default=text("2")
    )
    source: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=VisitSource.WEEKLY_SCHEDULE.value,
        server_default=text("'WEEKLY_SCHEDULE'"),
    )
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=VisitTaskStatus.PENDING.value,
        server_default=text("'PENDING'"),
    )
    reason: Mapped[str | None] = mapped_column(String(255), nullable=True)


class VisitRecord(Base, AuditMixin, DatasetMixin):
    """正式巡访记录：同一任务仅一条正式记录（unique partial index）。

    有效与否只由 VisitValidityService 裁决：validity=VALID/ABNORMAL_PENDING/INVALID，
    经理判定后把异常记录置 RECOGNIZED（计入）或 ABNORMAL（不计）。
    """

    __tablename__ = "visit_records"
    __table_args__ = (
        CheckConstraint(
            "status IN ('DRAFT','SUBMITTED','RECOGNIZED','ABNORMAL_PENDING',"
            "'ABNORMAL','REJECTED')",
            name="visit_records_status_check",
        ),
        CheckConstraint(
            "validity IN ('VALID','ABNORMAL_PENDING','INVALID')",
            name="visit_records_validity_check",
        ),
        CheckConstraint(
            "display_pct IS NULL OR (display_pct BETWEEN 0 AND 100)",
            name="visit_records_display_range",
        ),
        CheckConstraint(
            "freezer_status IS NULL OR freezer_status IN "
            "('NORMAL','ABSENT','POWERED_OFF','DIRTY','DAMAGED','OTHER')",
            name="visit_records_freezer_status_check",
        ),
        CheckConstraint(
            "conclusion IS NULL OR conclusion IN "
            "('NORMAL','NEEDS_REPLENISHMENT','NEEDS_RECTIFICATION',"
            "'RISK_MERCHANT','PROPOSE_TERMINATION')",
            name="visit_records_conclusion_check",
        ),
        CheckConstraint(
            "display_threshold_pct IS NULL OR (display_threshold_pct BETWEEN 0 AND 100)",
            name="visit_records_threshold_range",
        ),
        CheckConstraint("accuracy_meters IS NULL OR accuracy_meters >= 0",
                        name="visit_records_accuracy_nonneg"),
        CheckConstraint("distance_meters IS NULL OR distance_meters >= 0",
                        name="visit_records_distance_nonneg"),
        CheckConstraint(
            "checkout_at IS NULL OR checkin_at IS NULL OR checkout_at >= checkin_at",
            name="visit_records_time_order",
        ),
        Index("ix_visit_records_merchant", "merchant_id"),
        Index("ix_visit_records_task", "visit_task_id"),
        Index("ix_visit_records_user_time", "user_id", "checkin_at"),
        # 一个任务至多一条正式记录（T03 起存在的 is_official 语义，PRD §4.6）
        Index(
            "uq_visit_records_official_per_task",
            "visit_task_id",
            unique=True,
            postgresql_where=text("is_official"),
        ),
        UniqueConstraint(
            "idempotency_key", name="uq_visit_records_idempotency_key"
        ),
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
    source: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text("'WEEKLY_SCHEDULE'")
    )
    # ---- T12：有效巡访提交表单（PRD §4.6：冰柜状态/陈列阈值判定/设备异常/当日结论）----
    freezer_status: Mapped[str | None] = mapped_column(
        String(16), nullable=True, server_default=text("'NORMAL'")
    )
    # 设备异常描述（非空即视为存在设备异常，联动异常/整改待办）
    device_issue: Mapped[str | None] = mapped_column(Text, nullable=True)
    conclusion: Mapped[str | None] = mapped_column(
        String(24), nullable=True, server_default=text("'NORMAL'")
    )
    # 陈列阈值快照：提交时从生效合同固化（R3），后改合同不影响历史判定
    display_threshold_pct: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 是否达到合同阈值：display_pct >= display_threshold_pct（49<50 不达标；50>=50 达标）
    display_met: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # 到店实际位置（WGS84，服务端落库后不可改）
    location: Mapped[Geography | None] = mapped_column(
        Geography(geometry_type="POINT", srid=4326, spatial_index=True), nullable=True
    )
    # 打卡到店/离店（服务端接收时刻，UTC）
    checkin_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    checkout_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # 设备自报时间（仅取证，不作为裁决依据）
    device_checkin_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # 定位精度（米，设备上报）与服务端重算距离（米）
    accuracy_meters: Mapped[int | None] = mapped_column(Integer, nullable=True)
    distance_meters: Mapped[float | None] = mapped_column(nullable=True)
    display_pct: Mapped[int | None] = mapped_column(nullable=True)
    # 是否有至少一张现场照片（冗余计数口径，权威以 visit_photos 为准）
    photo_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 是否本次巡访任务的正式记录（一任务至多一条 True，见部分唯一索引）
    is_official: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    # 提交状态 + 有效性裁决（VisitValidityService 唯一产出）
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=VisitRecordStatus.SUBMITTED.value,
        server_default=text("'SUBMITTED'"),
    )
    validity: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text("'ABNORMAL_PENDING'")
    )
    # 经理判定（异常记录）：判定人/结论/时间/原因
    judged_by_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    judged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    judge_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    submitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    idempotency_key: Mapped[str | None] = mapped_column(String(64), nullable=True)


class VisitPhoto(Base, AuditMixin, DatasetMixin):
    """巡访现场照片：先上传 FileService 换 file_id，再绑定到巡访记录（只增不改）。"""

    __tablename__ = "visit_photos"
    __table_args__ = (
        CheckConstraint(
            "photo_kind IN ('STOREFRONT','FREEZER','OTHER')",
            name="visit_photos_kind_check",
        ),
        Index("ix_visit_photos_record", "visit_record_id"),
        UniqueConstraint("visit_record_id", "file_id", name="uq_visit_photos_record_file"),
    )

    id: Mapped[UUID] = pk_uuid()
    visit_record_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("visit_records.id", ondelete="RESTRICT"),
        nullable=False,
    )
    file_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("file_objects.id", ondelete="RESTRICT"), nullable=False
    )
    photo_kind: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'STOREFRONT'")
    )


class VisitRevision(Base, AuditMixin, DatasetMixin):
    """巡访补充说明：原始证据不可覆盖，补充以修订记录**追加**（§4.6）。"""

    __tablename__ = "visit_revisions"
    __table_args__ = (
        Index("ix_visit_revisions_record", "visit_record_id"),
    )

    id: Mapped[UUID] = pk_uuid()
    visit_record_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("visit_records.id", ondelete="RESTRICT"),
        nullable=False,
    )
    author_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    supplement: Mapped[str] = mapped_column(Text, nullable=False)


class VisitSalesLine(Base, AuditMixin, DatasetMixin):
    """巡访上报 SKU 销量/反馈行（PRD §4.6"按 SKU 填写销量箱数或商家反馈"）。

    红线（R8）：这些是业务员**上报**值，经理确认前不进正式口径——
    确认销量只出自 SalesConfirmation（sales_confirmations），本表不参与指标。
    """

    __tablename__ = "visit_sales_lines"
    __table_args__ = (
        CheckConstraint(
            "reported_boxes >= 0", name="visit_sales_lines_boxes_nonneg"
        ),
        CheckConstraint(
            "reported_boxes > 0 OR feedback IS NOT NULL OR out_of_stock = TRUE",
            name="visit_sales_lines_meaningful",
        ),
        Index("ix_visit_sales_lines_record", "visit_record_id"),
        Index("ix_visit_sales_lines_sku", "sku_id"),
        UniqueConstraint(
            "visit_record_id", "sku_id", name="uq_visit_sales_lines_record_sku"
        ),
    )

    id: Mapped[UUID] = pk_uuid()
    visit_record_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("visit_records.id", ondelete="RESTRICT"),
        nullable=False,
    )
    sku_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("skus.id", ondelete="RESTRICT"), nullable=False
    )
    # 上报销量箱数：NUMERIC(12,2)（红线：箱数不得用 float）
    reported_boxes: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    # 商家反馈（可仅反馈不报销量）
    feedback: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 缺货标记（PRD §4.6 表单"缺货 SKU"）
    out_of_stock: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
