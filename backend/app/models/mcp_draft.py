"""MCP AI 草稿暂存（T24 / PRD §5.2「支持巡访、补货、商家、合同、活动、督导草稿」）。

设计要点（PRD §5.2）：

- 草稿是 AI 辅助**待确认**产物，绝不能进入正式业务表，因此统一落在独立暂存表
  `mcp_drafts`，与 merchants / visit_records / purchase_orders / contracts /
  marketing_activities / supervision_records 等正式表物理隔离；
- 正式域指标（业绩/利润/巡访有效性/补货满足率/工资）只从正式表口径读取，
  本表不参与任何 `MetricsService` / `PerformanceEngine` / `RiskService` 计算，
  因而“草稿不进入正式指标”；
- 草稿经结构化确认（T24 确认令牌）后，才由对应受控 Service 具化为正式记录；
- 草稿归属发起人 + 会话，行级隔离；可更新/作废（逻辑删除），不提供物理删除。
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CHAR,
    CheckConstraint,
    DateTime,
    Index,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, pk_uuid

# 支持的 6 类草稿（PRD §5.2）
DRAFT_KIND_MERCHANT = "merchant"
DRAFT_KIND_VISIT = "visit"
DRAFT_KIND_REPLENISHMENT = "replenishment"
DRAFT_KIND_CONTRACT = "contract"
DRAFT_KIND_CAMPAIGN = "campaign"
DRAFT_KIND_SUPERVISION = "supervision"
DRAFT_KINDS: tuple[str, ...] = (
    DRAFT_KIND_MERCHANT,
    DRAFT_KIND_VISIT,
    DRAFT_KIND_REPLENISHMENT,
    DRAFT_KIND_CONTRACT,
    DRAFT_KIND_CAMPAIGN,
    DRAFT_KIND_SUPERVISION,
)

STATUS_OPEN = "OPEN"
STATUS_MATERIALIZED = "MATERIALIZED"
STATUS_DISCARDED = "DISCARDED"


class McpDraft(Base):
    __tablename__ = "mcp_drafts"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('merchant','visit','replenishment','contract','campaign',"
            "'supervision')",
            name="mcp_drafts_kind_check",
        ),
        CheckConstraint(
            "status IN ('OPEN','MATERIALIZED','DISCARDED')",
            name="mcp_drafts_status_check",
        ),
        # 可选的幂等键唯一（仅对非空值）：同键重复保存草稿返回同一草稿
        UniqueConstraint("idempotency_key", name="uq_mcp_drafts_idem"),
        Index("ix_mcp_drafts_owner_kind", "owner_user_id", "kind", "status"),
        Index("ix_mcp_drafts_session", "session_id"),
        Index("ix_mcp_drafts_merchant", "merchant_id"),
    )

    id: Mapped[UUID] = pk_uuid()
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    # 不加 users 外键：草稿归属用于行级隔离，独立留存，不被用户删除级联影响
    owner_user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False
    )
    session_id: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    # 草稿关联商家（巡访/补货/合同/督导等），商家草稿本身可空；不设外键强约束，
    # 因为草稿不参与正式表引用完整性，且目标商家可能尚未具化
    merchant_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True
    )
    # 草稿业务内容（经 schema 校验后的参数快照）
    payload: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=STATUS_OPEN,
        server_default=text("'OPEN'"),
    )
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # 具化后回填正式记录引用（对象类型 + 主键），形成草稿→正式可追溯链
    materialized_type: Mapped[str | None] = mapped_column(String(48), nullable=True)
    materialized_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True
    )
    materialized_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    discarded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # 预留：参数摘要，便于同内容草稿去重/比对
    params_hash: Mapped[str | None] = mapped_column(CHAR(64), nullable=True)
