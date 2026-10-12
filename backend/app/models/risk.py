"""v1.2 R36：风险表（RiskItem）。幂等生成、关闭留痕。

唯一约束：(object_type, object_id, rule_code, period) 同一周期不重复生成。

（T04 起 IdempotencyKey 迁至 app/models/idempotency.py 并扩展为通用幂等内核。）
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Index,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import AuditMixin, Base, DatasetMixin, pk_uuid
from app.models.enums import RiskStatus


class RiskItem(Base, AuditMixin, DatasetMixin):
    __tablename__ = "risk_items"
    __table_args__ = (
        CheckConstraint(
            "rule_code IN ('RK-1','RK-2','RK-3','RK-4','RK-5','RK-6','RK-7')",
            name="risk_items_rule_code_check",
        ),
        CheckConstraint(
            "object_type IN ('MERCHANT','CONTRACT','FREEZER','REPLENISHMENT')",
            name="risk_items_object_type_check",
        ),
        CheckConstraint(
            "status IN ('OPEN','ACKNOWLEDGED','CLOSED')",
            name="risk_items_status_check",
        ),
        # v1.2 R36：同对象同规则同周期不重复生成
        UniqueConstraint(
            "object_type", "object_id", "rule_code", "period",
            name="uq_risk_items_object_rule_period",
        ),
        Index("ix_risk_items_status", "status"),
        Index("ix_risk_items_first_seen", "first_seen_at"),
    )

    id: Mapped[UUID] = pk_uuid()
    object_type: Mapped[str] = mapped_column(String(16), nullable=False)
    object_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    rule_code: Mapped[str] = mapped_column(String(8), nullable=False)
    # 周期：业务周 ISO 编号（如 2026-W42）或日期（如 2026-10-12）；由 RiskService 决定
    period: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=RiskStatus.OPEN.value,
        server_default=text("'OPEN'"),
    )
    severity: Mapped[str] = mapped_column(
        String(8), nullable=False, default="NORMAL", server_default=text("'NORMAL'")
    )
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_by_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        # 注：users 表存在时为合法外键；为支持先建表后建关联 users，可选保留 nullable
        nullable=True,
    )
    resolution: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 事实缺失降级标记
    data_incomplete: Mapped[bool] = mapped_column(
        nullable=False, default=False, server_default=text("false")
    )
