"""商家责任调拨单（T14 / PRD §4.7 商家责任转移、R9、A8）。

业务边界（PRD）：
- 调拨单记录原/新负责人、生效日（含）、状态机与原因；是“历史不回写”的分界凭证；
- 生效日（含）起：未完成巡访任务与待办转新负责人；已完成事实、已提交记录不回写；
  业绩按事实发生时点归属（VisitTask/VisitRecord 的 owner 快照保持不变）；
- 生效前允许撤回（WITHDRAWN）；生效后不可覆盖，只能新建反向调拨（reversed_by_id 回链）。

归属本身仍由 MerchantAssignment 的 tstzrange 区间表达；本表只承载调拨流程与分界。
状态机：PENDING/SCHEDULED → EFFECTIVE；未生效 → WITHDRAWN；
EFFECTIVE 只能由反向调拨在新单据上回链。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Date, DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import AuditMixin, Base, DatasetMixin, pk_uuid

# 调拨单状态机（DB 侧 String + CHECK，见迁移）
TRANSFER_PENDING = "PENDING"        # 已创建，等待到生效日/经理执行
TRANSFER_SCHEDULED = "SCHEDULED"    # 未来生效，预览后已排期
TRANSFER_EFFECTIVE = "EFFECTIVE"    # 已生效（区间切换 + 待办转移完成）
TRANSFER_WITHDRAWN = "WITHDRAWN"    # 生效前撤回
TRANSFER_SUPERSEDED = "SUPERSEDED"  # 未生效即被另一张调拨单取代

TRANSFER_STATUSES = frozenset(
    {TRANSFER_PENDING, TRANSFER_SCHEDULED, TRANSFER_EFFECTIVE,
     TRANSFER_WITHDRAWN, TRANSFER_SUPERSEDED}
)
# 生效前可撤回/可执行的状态
TRANSFER_OPEN_STATUSES = frozenset({TRANSFER_PENDING, TRANSFER_SCHEDULED})


class MerchantTransfer(AuditMixin, DatasetMixin, Base):
    __tablename__ = "merchant_transfers"

    id: Mapped[UUID] = pk_uuid()
    merchant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("merchants.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    from_user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False, index=True,
    )
    to_user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False, index=True,
    )

    effective_date: Mapped[date] = mapped_column(Date, nullable=False)
    # 生效瞬间（上海业务日 00:00 换算的 UTC）；未生效为 None
    effective_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    effective_by_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True)

    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=TRANSFER_PENDING)
    reason: Mapped[str] = mapped_column(Text, nullable=False)

    # 撤回（生效前）
    withdrawn_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    withdrawn_by_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True)
    withdraw_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # 反向调拨回链（生效后只能新建反向调拨）
    reversed_by_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("merchant_transfers.id", ondelete="RESTRICT"),
        nullable=True)
    reverses_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("merchant_transfers.id", ondelete="RESTRICT"),
        nullable=True)

    # 生效时的交接快照（待办/任务/临时授权/合同分界计数），用于审计与预览回执
    handover_json: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True)
