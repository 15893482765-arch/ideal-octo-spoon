"""冰柜资产与流水（T10 PRD §4.5 全生命周期；含 v1.2 押金 R37）。"""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
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
from app.models.enums import FreezerAssetStatus

# 受控状态值（与迁移 CHECK 保持一致；含 T02 旧台账兼容值）
_FREEZER_STATUS_VALUES = (
    "IN_STOCK",
    "RESERVED",
    "DEPLOYED",
    "PENDING_PICKUP",
    "PENDING_ACCEPTANCE",
    "REPAIRING",
    "SCRAPPED",
    "IN_TRANSIT",
    "UNDER_MAINTENANCE",
)

# 撤回申请状态（业务员发起 → 片区经理审批）
_RECALL_STATUS_VALUES = ("PENDING", "APPROVED", "REJECTED")

# 受控流水类型（含 T02 旧 RECALL 兼容值）
_MOVEMENT_TYPE_VALUES = (
    "PURCHASE",
    "DEPLOYMENT_ORDER",
    "DEPLOY",
    "RECALL",
    "RECALL_REQUEST",
    "RECALL_REJECT",
    "RECALL_RESCHEDULE",
    "PICKUP",
    "ACCEPT",
    "REPAIR_START",
    "REPAIR_END",
    "SCRAP",
    "DEPOSIT_COLLECTED",
    "DEPOSIT_REFUNDED",
)


class FreezerAsset(Base, ImmutableMixin, AuditMixin, DatasetMixin):
    __tablename__ = "freezer_assets"
    __table_args__ = (
        UniqueConstraint(
            "dataset_type", "dataset_id", "asset_code", name="uq_freezer_assets_dataset_code"
        ),
        CheckConstraint(
            "status IN ("
            + ",".join(f"'{s}'" for s in _FREEZER_STATUS_VALUES)
            + ")",
            name="freezer_assets_status_check",
        ),
        # v1.2 R37：押金（可选），BIGINT 分，>= 0
        CheckConstraint(
            "deposit_fen IS NULL OR deposit_fen >= 0",
            name="freezer_assets_deposit_nonneg",
        ),
        CheckConstraint("purchase_price_fen >= 0", name="freezer_assets_purchase_price_nonneg"),
        CheckConstraint("repair_count >= 0", name="freezer_assets_repair_count_nonneg"),
        # R12：仅物理在店状态（已投放/待撤回现场未回收）必须挂唯一一个商家；
        # 现场回收进入待验收即解绑（商家归属历史保留在 freezer_movements）。
        # “同一冰柜不能同时绑两个商家”由单行单一 current_merchant_id + 投放行锁保证。
        CheckConstraint(
            "status IN ('DEPLOYED','PENDING_PICKUP') = (current_merchant_id IS NOT NULL)",
            name="freezer_assets_merchant_binding_check",
        ),
        # 撤回申请状态受控（NULL=未申请）
        CheckConstraint(
            "recall_status IS NULL OR recall_status IN ("
            + ",".join(f"'{s}'" for s in _RECALL_STATUS_VALUES)
            + ")",
            name="freezer_assets_recall_status_check",
        ),
    )

    id: Mapped[UUID] = pk_uuid()
    asset_code: Mapped[str] = mapped_column(String(32), nullable=False)
    brand_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("brands.id", ondelete="RESTRICT"), nullable=False
    )
    model: Mapped[str] = mapped_column(String(64), nullable=False)
    purchase_date: Mapped[date] = mapped_column(Date, nullable=False)
    purchase_price_fen: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0")
    )
    deposit_fen: Mapped[int | None] = mapped_column(BigInteger, nullable=True)  # v1.2：可选押金
    status: Mapped[str] = mapped_column(
        String(24),
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
    # 台账（PRD §4.5）：最近投放/撤回时间、维修次数
    last_deployed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_recalled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    repair_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    # ---- 撤回（撤柜）申请：业务员发起 → 片区经理审批 → 转待撤回 ----
    # PENDING=待审批 / APPROVED=已同意（资产已转 PENDING_PICKUP）/ REJECTED=已驳回
    recall_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    recall_planned_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    recall_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    recall_requested_by: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    recall_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    recall_decided_by: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    recall_decided_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    recall_decision_note: Mapped[str | None] = mapped_column(String(255), nullable=True)


class FreezerMovement(Base, AuditMixin):
    """资产流水：购置/投放/撤回/验收/维修/报废/押金收缴/退还（v1.2）。

    每次状态迁移写一条；押金流水必须经 related_movement_id 关联投放单/验收单（R37）。
    一条流水同时充当“投放单/撤柜回收单/验收单”（外部展示以 doc_no 单号为准）。
    """

    __tablename__ = "freezer_movements"
    __table_args__ = (
        CheckConstraint(
            "movement_type IN ("
            + ",".join(f"'{t}'" for t in _MOVEMENT_TYPE_VALUES)
            + ")",
            name="freezer_movements_type_check",
        ),
        CheckConstraint(
            "amount_fen IS NULL OR amount_fen >= 0",
            name="freezer_movements_amount_nonneg",
        ),
        # 押金流水必须关联对应投放单/验收单（R37）
        CheckConstraint(
            "movement_type NOT IN ('DEPOSIT_COLLECTED','DEPOSIT_REFUNDED') "
            "OR related_movement_id IS NOT NULL",
            name="freezer_movements_deposit_related_check",
        ),
        Index("ix_freezer_movements_asset", "asset_id"),
        Index("ix_freezer_movements_type", "movement_type"),
        Index("ix_freezer_movements_contract", "contract_id"),
        UniqueConstraint("doc_no", name="uq_freezer_movements_doc_no"),
    )

    id: Mapped[UUID] = pk_uuid()
    asset_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("freezer_assets.id", ondelete="RESTRICT"),
        nullable=False,
    )
    movement_type: Mapped[str] = mapped_column(String(24), nullable=False)
    # 对外单号（投放单/回收单/验收单/押金单），全局唯一
    doc_no: Mapped[str | None] = mapped_column(String(40), nullable=True)
    merchant_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("merchants.id", ondelete="RESTRICT"), nullable=True
    )
    contract_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("contracts.id", ondelete="RESTRICT"), nullable=True
    )
    # 押金流水必须关联投放/验收单（v1.2 R37）；维修结束关联维修开始
    related_movement_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("freezer_movements.id", ondelete="RESTRICT"),
        nullable=True,
    )
    amount_fen: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # 状态迁移留痕（投放单创建时 from/to 可能相同）
    from_status: Mapped[str | None] = mapped_column(String(24), nullable=True)
    to_status: Mapped[str | None] = mapped_column(String(24), nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    # 现场/凭证照片附件（走 FileService）
    attachment_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("file_objects.id", ondelete="RESTRICT"), nullable=True
    )
    operator_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)
