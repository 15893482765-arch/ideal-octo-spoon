"""补货需求、经理确认与履约（PRD §4.9 / R16 / A11，T17）。

状态链（T17 起以 PRD 为准）：
    DRAFT → SUBMITTED → CONFIRMED → PARTIALLY_FULFILLED / FULFILLED → CLOSED / CANCELLED

数量口径（全部 NUMERIC(12,2) 箱数，Decimal，禁 float）：
- requested_boxes：业务员提报需求（已审核事实不覆盖）；
- committed_boxes：经理**确认数量**，可调整，必须记录调整原因；
- fulfilled_boxes：历次送达履约累计满足量；恒有 0 ≤ fulfilled ≤ committed；
- 部分满足（fulfilled < committed）为 PARTIALLY_FULFILLED，未满足量可继续履约；
  fulfilled == committed 为 FULFILLED；履约事件按 idempotency_key 幂等（A17/Q092）。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Numeric,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import AuditMixin, Base, DatasetMixin, pk_uuid
from app.models.enums import ReplenishmentStatus

# T17 有效流程状态（历史台账词 REQUESTED/IN_TRANSIT/DELIVERED/OVERDUE 仅供旧数据兼容）
_ACTIVE_STATES = (
    "DRAFT", "SUBMITTED", "CONFIRMED",
    "PARTIALLY_FULFILLED", "FULFILLED", "CLOSED", "CANCELLED",
    "REQUESTED", "IN_TRANSIT", "DELIVERED", "OVERDUE",
)
_STATUS_IN = ", ".join(f"'{s}'" for s in _ACTIVE_STATES)


class ReplenishmentOrder(Base, AuditMixin, DatasetMixin):
    __tablename__ = "replenishment_orders"
    __table_args__ = (
        UniqueConstraint(
            "dataset_type", "dataset_id", "code", name="uq_replenishment_orders_dataset_code"
        ),
        CheckConstraint(f"status IN ({_STATUS_IN})",
                        name="replenishment_orders_status_check"),
        # T12/T17：DRAFT/SUBMITTED 未经经理确认，committed_boxes=0；
        # 进入已确认及以后状态必须 >0
        CheckConstraint(
            "status NOT IN ('CONFIRMED','PARTIALLY_FULFILLED','FULFILLED','CLOSED',"
            "'IN_TRANSIT','DELIVERED') OR committed_boxes > 0",
            name="replenishment_orders_boxes_positive",
        ),
        CheckConstraint(
            "promised_delivery_date IS NULL OR promised_delivery_date >= request_date",
            name="replenishment_orders_date_order",
        ),
        # T17：满足量不得超过确认量；确认量不得超过提报量上限？——PRD 允许经理调整
        # （可增可减），故不限制 committed 与 requested 的大小，仅约束数量非负与
        # fulfilled ≤ committed（R16/A11：超量拒绝）。
        CheckConstraint("committed_boxes >= 0", name="replenishment_orders_committed_nonneg"),
        CheckConstraint(
            "fulfilled_boxes >= 0 AND fulfilled_boxes <= committed_boxes",
            name="replenishment_orders_fulfilled_within_committed",
        ),
        # 取消必须留原因（R：已审核事实只状态迁移并留原因 / Q093）
        CheckConstraint(
            "status <> 'CANCELLED' OR cancel_reason IS NOT NULL",
            name="replenishment_orders_cancel_reason_required",
        ),
        Index("ix_replenishment_orders_merchant", "merchant_id"),
        Index("ix_replenishment_orders_status_promised", "status", "promised_delivery_date"),
        Index("ix_replenishment_orders_source_visit", "source_visit_record_id"),
    )

    id: Mapped[UUID] = pk_uuid()
    code: Mapped[str] = mapped_column(String(32), nullable=False)
    merchant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("merchants.id", ondelete="RESTRICT"), nullable=False
    )
    sku_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("skus.id", ondelete="RESTRICT"), nullable=False
    )
    requested_boxes: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    committed_boxes: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    # T17：累计已满足箱数（历次送达凭证之和）
    fulfilled_boxes: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, server_default=text("0")
    )
    request_date: Mapped[date] = mapped_column(Date, nullable=False)
    promised_delivery_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    actual_delivery_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=ReplenishmentStatus.REQUESTED.value,
        server_default=text("'REQUESTED'"),
    )
    # T12：由巡访提报生成的草稿回链到巡访记录（经理确认后进入正式补货池）
    source_visit_record_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("visit_records.id", ondelete="RESTRICT"),
        nullable=True,
    )

    # ---- T17：经理确认与取消留痕（已审核事实不覆盖，只迁移+留原因） ----
    confirmer_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    confirmed_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    # 经理调整确认数量时必填原因
    adjust_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    cancel_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    cancelled_by: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )


class ReplenishmentConfirm(Base, AuditMixin):
    __tablename__ = "replenishment_confirms"
    __table_args__ = (
        UniqueConstraint("order_id", name="uq_replenishment_confirm_order"),
        Index("ix_replenishment_confirms_confirmer", "confirmer_id"),
    )

    id: Mapped[UUID] = pk_uuid()
    order_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("replenishment_orders.id", ondelete="RESTRICT"),
        nullable=False,
    )
    confirmer_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    confirmed_boxes: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)


class ReplenishmentFulfillment(Base, AuditMixin, DatasetMixin):
    """补货送达履约事件（PRD §4.9 首期边界：凭送达凭证手工登记）。

    每次登记一笔（数量、送达日、凭证 file_id），订单 fulfilled_boxes 由各事件
    delivered_boxes 累计；同一 idempotency_key 重复回调只返回原事件、不重复累计
    （A17/Q092）。
    """

    __tablename__ = "replenishment_fulfillments"
    __table_args__ = (
        UniqueConstraint(
            "dataset_type", "dataset_id", "idempotency_key",
            name="uq_repl_fulfillment_idempotency",
        ),
        CheckConstraint("delivered_boxes > 0",
                        name="repl_fulfillment_boxes_positive"),
        Index("ix_repl_fulfillment_order", "order_id"),
    )

    id: Mapped[UUID] = pk_uuid()
    order_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("replenishment_orders.id", ondelete="RESTRICT"),
        nullable=False,
    )
    delivered_boxes: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    delivered_on: Mapped[date] = mapped_column(Date, nullable=False)
    # 送达凭证（送货单/签收照片）对象存储 file_id
    proof_file_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("file_objects.id", ondelete="RESTRICT"),
        nullable=True,
    )
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False)
    note: Mapped[str | None] = mapped_column(String(500), nullable=True)
    registered_by: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
