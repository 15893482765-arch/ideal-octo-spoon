"""商家、商家联系人、商家等级巡访频次配置（v1.2 R35）。"""

from __future__ import annotations

from datetime import date
from uuid import UUID

from geoalchemy2 import Geography
from sqlalchemy import (
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import AuditMixin, Base, DatasetMixin, ImmutableMixin, pk_uuid
from app.models.enums import MerchantStatus


class Merchant(Base, ImmutableMixin, AuditMixin, DatasetMixin):
    __tablename__ = "merchants"
    __table_args__ = (
        # 商家编号唯一（生产/演示各自独立）
        UniqueConstraint(
            "dataset_type", "dataset_id", "code", name="uq_merchants_dataset_code"
        ),
        # 状态机 CHECK（数据库层强制）
        CheckConstraint(
            "status IN ('LEAD','NEGOTIATING','ACTIVE','PAUSED','TERMINATED')",
            name="merchants_status_check",
        ),
        CheckConstraint(
            "grade IN ('A','B','C') OR grade IS NULL",
            name="merchants_grade_check",
        ),
        # PostGIS 地理点（WGS84），GiST 索引见迁移文件
    )

    id: Mapped[UUID] = pk_uuid()
    code: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=MerchantStatus.LEAD.value,
        server_default=text("'LEAD'"),
    )
    grade: Mapped[str | None] = mapped_column(String(1), nullable=True)
    region_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("regions.id", ondelete="RESTRICT"), nullable=False
    )
    brand_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("brands.id", ondelete="RESTRICT"), nullable=False
    )
    location: Mapped[Geography | None] = mapped_column(
        Geography(geometry_type="POINT", srid=4326, spatial_index=True), nullable=True
    )
    address: Mapped[str | None] = mapped_column(String(255), nullable=True)
    contact_phone: Mapped[str | None] = mapped_column(String(32), nullable=True)


class MerchantContact(Base, AuditMixin):
    __tablename__ = "merchant_contacts"
    __table_args__ = (
        Index("ix_merchant_contacts_merchant", "merchant_id"),
    )

    id: Mapped[UUID] = pk_uuid()
    merchant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("merchants.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    role: Mapped[str | None] = mapped_column(String(64), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)


class MerchantGradeFrequency(Base, AuditMixin):
    """v1.2 R35：等级 → 巡访频次（天）的配置，带生效区间。

    配置变更只影响生效日之后的排班与预警，不回溯历史。
    """

    __tablename__ = "merchant_grade_frequencies"
    __table_args__ = (
        CheckConstraint("frequency_days > 0", name="merchant_grade_freq_positive"),
        CheckConstraint(
            "end_date IS NULL OR end_date >= start_date",
            name="merchant_grade_freq_range",
        ),
        # 同一等级生效区间不重叠：数据库用 EXCLUDE 约束在迁移中创建（GIST on daterange）
    )

    id: Mapped[UUID] = pk_uuid()
    grade: Mapped[str] = mapped_column(String(1), nullable=False)
    frequency_days: Mapped[int] = mapped_column(Integer, nullable=False)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
