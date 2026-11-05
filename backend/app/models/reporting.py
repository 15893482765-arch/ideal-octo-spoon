"""T21 经营报表导出任务（PRD §4.10 经营报表；§5.3 report.export_request；§9.4 导出）。

红线：
- 导出必须显式 `DataScope.PRODUCTION`（PRD R34）；DEMO 混入直接拒绝（R33）；
- 导出文件按当前用户行/字段权限生成，记录导出人、条件、时间与文件有效期（R28）；
- 短期下载链接（到期即失效），所有导出写审计；
- 任务只增不改：失败/成功都保留记录（不物理删除）。
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
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import AuditMixin, Base, DatasetMixin, pk_uuid


class ExportJob(Base, AuditMixin, DatasetMixin):
    """异步导出任务头（Excel/CSV/PDF）。"""

    __tablename__ = "export_jobs"
    __table_args__ = (
        CheckConstraint(
            "format IN ('XLSX','CSV','PDF')", name="export_jobs_format_check"
        ),
        CheckConstraint(
            "status IN ('QUEUED','RUNNING','SUCCEEDED','FAILED')",
            name="export_jobs_status_check",
        ),
        CheckConstraint(
            "scope = 'PRODUCTION'", name="export_jobs_scope_production_only"
        ),
        CheckConstraint(
            "row_count IS NULL OR row_count >= 0", name="export_jobs_row_count_nonneg"
        ),
        Index("ix_export_jobs_requester", "requested_by_id", "created_at"),
        Index("ix_export_jobs_status", "status"),
    )

    id: Mapped[UUID] = pk_uuid()
    job_no: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    report_key: Mapped[str] = mapped_column(String(32), nullable=False)
    format: Mapped[str] = mapped_column(String(8), nullable=False)
    # 报表筛选条件（时间/片区/品牌/SKU/业务员等）
    filters_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    # 生成文件实际使用的列（按用户字段权限过滤后的快照）
    columns_json: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    # 显式数据域：CHECK 强制 PRODUCTION（R34）
    scope: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'PRODUCTION'")
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'QUEUED'")
    )
    requested_by_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    requested_role_code: Mapped[str] = mapped_column(String(32), nullable=False)
    # 生成时用户的行级范围快照（self/region/brand_region/global）——异步任务据此复核
    row_scope: Mapped[str] = mapped_column(String(24), nullable=False)
    # 产出对象存储键（短期链接据此签发）
    object_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    content_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    row_count: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text(), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # 链接有效期（到期即失效，Q120）
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
