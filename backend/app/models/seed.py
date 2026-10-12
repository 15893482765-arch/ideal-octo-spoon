"""演示数据导入批次（T06 / PRD §10.2、§8 演示数据隔离）。

SeedBatch：
- 记录一次演示数据导入的"批次意图"，含 dataset_type/dataset_id、来源、
  行数统计、状态机与错误日志；
- 业务演示数据未到（依赖 SKU/账号）之前，本表只允许建空批次；
- 状态机（由 Service 在 flush 后落审计）：
  PENDING → RUNNING → SUCCEEDED / FAILED
  已被业务引用的批次不回滚（PRD §10.4），仅 FAILED 后可重提 → PENDING。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import AuditMixin, Base, pk_uuid

# 演示数据固定 dataset_id（与 PRD §10.3 配套工作簿保持一致；T06 引用但不导入）
DEMO_DATASET_ID_DEFAULT = "DEMO-QINSHUANG-2026Q3"
SEED_STATUS_PENDING = "PENDING"
SEED_STATUS_RUNNING = "RUNNING"
SEED_STATUS_SUCCEEDED = "SUCCEEDED"
SEED_STATUS_FAILED = "FAILED"

# 业务依赖（PRD §10.2）：先导入主数据（区域/品牌/SKU/账号），再导入引用它们
# 的业务演示表；T06 仅允许空批次，故 type 白名单限定为 MASTER 与 BUSINESS，
# 但 BUSINESS 在主数据未到位时由 SeedCheckService 拦截。
SEED_TYPE_MASTER = "MASTER"
SEED_TYPE_BUSINESS = "BUSINESS"
SEED_TYPE_EMPTY = "EMPTY"  # 仅占位/自检
SEED_TYPES: tuple[str, ...] = (SEED_TYPE_MASTER, SEED_TYPE_BUSINESS, SEED_TYPE_EMPTY)


class SeedBatch(Base, AuditMixin):
    """演示数据导入批次（T06 / PRD §10.2 / §8 / §6 R18）。"""

    __tablename__ = "seed_batches"
    __table_args__ = (
        CheckConstraint(
            "dataset_type IN ('PRODUCTION','DEMO')",
            name="seed_batches_dataset_type_check",
        ),
        CheckConstraint(
            f"seed_type IN {SEED_TYPES!r}",
            name="seed_batches_seed_type_check",
        ),
        CheckConstraint(
            f"status IN ('{SEED_STATUS_PENDING}','{SEED_STATUS_RUNNING}',"
            f"'{SEED_STATUS_SUCCEEDED}','{SEED_STATUS_FAILED}')",
            name="seed_batches_status_check",
        ),
        # 文件哈希去重：同 source_filename + file_hash 不重复创建
        Index(
            "uq_seed_batches_source_hash",
            "source_filename",
            "file_hash",
            unique=True,
        ),
        Index("ix_seed_batches_status", "status"),
    )

    id: Mapped[UUID] = pk_uuid()
    dataset_type: Mapped[str] = mapped_column(String(16), nullable=False)
    dataset_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    seed_type: Mapped[str] = mapped_column(
        String(16), nullable=False, default=SEED_TYPE_EMPTY, server_default=text("'EMPTY'")
    )
    # 来源文件名（仅展示，不解析）：T06 留空也可
    source_filename: Mapped[str] = mapped_column(
        String(255), nullable=False, default="", server_default=text("''")
    )
    # 文件 SHA256（去重用）；T06 留空表示"无源文件 / 空批次"
    file_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, default="", server_default=text("''")
    )
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=SEED_STATUS_PENDING,
        server_default=text("'PENDING'"),
    )
    rows_total: Mapped[int] = mapped_column(nullable=False, default=0, server_default=text("0"))
    rows_imported: Mapped[int] = mapped_column(
        nullable=False, default=0, server_default=text("0")
    )
    rows_rejected: Mapped[int] = mapped_column(
        nullable=False, default=0, server_default=text("0")
    )
    error_log: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 结构化负例报告：T06 用例集中三类必填
    negative_findings: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    submitted_by_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
