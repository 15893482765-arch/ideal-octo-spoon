"""组织/片区/品牌主数据。"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import AuditMixin, Base, DatasetMixin, ImmutableMixin, pk_uuid


class Organization(Base, ImmutableMixin, AuditMixin, DatasetMixin):
    __tablename__ = "organizations"
    __table_args__ = (
        CheckConstraint("char_length(code) > 0", name="organizations_code_nonempty"),
        Index("ix_organizations_dataset", "dataset_type", "dataset_id"),
    )

    id: Mapped[UUID] = pk_uuid()
    code: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    parent_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=True
    )

    parent: Mapped[Organization | None] = relationship(remote_side="Organization.id")


class Region(Base, ImmutableMixin, AuditMixin, DatasetMixin):
    __tablename__ = "regions"
    __table_args__ = (
        CheckConstraint("char_length(code) > 0", name="regions_code_nonempty"),
        Index("ix_regions_dataset", "dataset_type", "dataset_id"),
    )

    id: Mapped[UUID] = pk_uuid()
    code: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    org_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )


class Brand(Base, ImmutableMixin, AuditMixin, DatasetMixin):
    __tablename__ = "brands"
    __table_args__ = (
        CheckConstraint("char_length(code) > 0", name="brands_code_nonempty"),
        Index("ix_brands_dataset", "dataset_type", "dataset_id"),
    )

    id: Mapped[UUID] = pk_uuid()
    code: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    # 仅作兼容字段，dataset_type 才是事实来源（PRD §6 R18）
    is_demo: Mapped[bool] = mapped_column(
        nullable=False, default=False, server_default=text("false")
    )
