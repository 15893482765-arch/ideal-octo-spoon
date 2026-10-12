"""冰柜资产与流水（含 v1.2 押金 R37）。"""

from __future__ import annotations

from datetime import date
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import AuditMixin, Base, DatasetMixin, ImmutableMixin, pk_uuid
from app.models.enums import FreezerAssetStatus


class FreezerAsset(Base, ImmutableMixin, AuditMixin, DatasetMixin):
    __tablename__ = "freezer_assets"
    __table_args__ = (
        UniqueConstraint(
            "dataset_type", "dataset_id", "asset_code", name="uq_freezer_assets_dataset_code"
        ),
        CheckConstraint(
            "status IN ('IN_STOCK','IN_TRANSIT','DEPLOYED','UNDER_MAINTENANCE','SCRAPPED')",
            name="freezer_assets_status_check",
        ),
        # v1.2 R37：押金（可选），BIGINT 分，>= 0
        CheckConstraint(
            "deposit_fen IS NULL OR deposit_fen >= 0",
            name="freezer_assets_deposit_nonneg",
        ),
        CheckConstraint("purchase_price_fen >= 0", name="freezer_assets_purchase_price_nonneg"),
    )

    id: Mapped[UUID] = pk_uuid()
    asset_code: Mapped[str] = mapped_column(String(32), nullable=False)
    brand_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("brands.id", ondelete="RESTRICT"), nullable=False
    )
    model: Mapped[str] = mapped_column(String(64), nullable=False)
    purchase_date: Mapped[date] = mapped_column(Date, nullable=False)
    purchase_price_fen: Mapped[int] = mapped_column(
        nullable=False, default=0, server_default=text("0")
    )
    deposit_fen: Mapped[int | None] = mapped_column(nullable=True)  # v1.2：可选押金
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=FreezerAssetStatus.IN_STOCK.value,
        server_default=text("'IN_STOCK'"),
    )
    current_merchant_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("merchants.id", ondelete="RESTRICT"), nullable=True
    )
    responsible_user_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )


class FreezerMovement(Base, AuditMixin):
    """资产流水：投放/撤回/维修/报废/押金收缴/退还（v1.2）。"""

    __tablename__ = "freezer_movements"
    __table_args__ = (
        CheckConstraint(
            "movement_type IN ("
            "'PURCHASE','DEPLOY','RECALL','REPAIR_START','REPAIR_END','SCRAP',"
            "'DEPOSIT_COLLECTED','DEPOSIT_REFUNDED')",
            name="freezer_movements_type_check",
        ),
        Index("ix_freezer_movements_asset", "asset_id"),
        Index("ix_freezer_movements_type", "movement_type"),
    )

    id: Mapped[UUID] = pk_uuid()
    asset_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("freezer_assets.id", ondelete="RESTRICT"),
        nullable=False,
    )
    movement_type: Mapped[str] = mapped_column(String(24), nullable=False)
    merchant_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("merchants.id", ondelete="RESTRICT"), nullable=True
    )
    # 押金流水必须关联投放/验收单（v1.2 R37）
    related_movement_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("freezer_movements.id", ondelete="RESTRICT"),
        nullable=True,
    )
    amount_fen: Mapped[int | None] = mapped_column(nullable=True)
    operator_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)
