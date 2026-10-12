"""产品分类与 SKU。"""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKey, Index, Numeric, String, text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import AuditMixin, Base, DatasetMixin, ImmutableMixin, pk_uuid


class ProductCategory(Base, ImmutableMixin, AuditMixin, DatasetMixin):
    __tablename__ = "product_categories"
    __table_args__ = (
        CheckConstraint("char_length(code) > 0", name="product_categories_code_nonempty"),
        Index("ix_product_categories_dataset", "dataset_type", "dataset_id"),
    )

    id: Mapped[UUID] = pk_uuid()
    code: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    parent_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("product_categories.id", ondelete="RESTRICT"),
        nullable=True,
    )

    parent: Mapped[ProductCategory | None] = relationship(remote_side="ProductCategory.id")


class Sku(Base, ImmutableMixin, AuditMixin, DatasetMixin):
    __tablename__ = "skus"
    __table_args__ = (
        CheckConstraint("char_length(code) > 0", name="skus_code_nonempty"),
        # 建议零售价：分（PRD §8 金额规则）
        CheckConstraint("suggested_price_fen >= 0", name="skus_suggested_price_nonneg"),
        # 箱数规格：NUMERIC(12,2) >= 0
        CheckConstraint("case_spec >= 0", name="skus_case_spec_nonneg"),
        Index("ix_skus_dataset", "dataset_type", "dataset_id"),
    )

    id: Mapped[UUID] = pk_uuid()
    code: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    brand_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("brands.id", ondelete="RESTRICT"), nullable=False
    )
    category_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("product_categories.id", ondelete="RESTRICT"),
        nullable=True,
    )
    capacity_ml: Mapped[int | None] = mapped_column(nullable=True)
    suggested_price_fen: Mapped[int] = mapped_column(
        nullable=False, default=0, server_default=text("0")
    )
    case_spec: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, default=Decimal("0"), server_default=text("0")
    )
