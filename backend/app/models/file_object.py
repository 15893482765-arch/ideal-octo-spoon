"""文件对象（T04 / PRD §8：S3 兼容对象存储 + 短期签名 URL）。

- 只存元数据与哈希；字节流经 StorageBackend 落对象存储；
- scan_status 预留病毒扫描状态（附录 A：真实扫描不在首版实现）；
- INFECTED → 隔离，下载被拒；SKIPPED 表示未启用扫描直接放行；
- 红线：原始定位/照片不可覆盖 —— FileService 不提供覆盖接口，本表只增不改。
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CHAR,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, DatasetMixin, pk_uuid


class FileObject(Base, DatasetMixin):
    __tablename__ = "file_objects"
    __table_args__ = (
        CheckConstraint(
            "biz_type IN ('CONTRACT_ATTACHMENT','PHOTO','PROOF_DOC','OTHER')",
            name="file_objects_biz_type_check",
        ),
        CheckConstraint(
            "scan_status IN ('PENDING','CLEAN','INFECTED','SKIPPED')",
            name="file_objects_scan_status_check",
        ),
        CheckConstraint("size_bytes >= 0", name="file_objects_size_nonneg"),
        Index("ix_file_objects_owner_time", "owner_user_id", "created_at"),
    )

    id: Mapped[UUID] = pk_uuid()
    # 对象存储键：{biz_type}/{uuid}/{安全化文件名}；全局唯一且永不覆盖
    object_key: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(128), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    biz_type: Mapped[str] = mapped_column(String(32), nullable=False)
    owner_user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    # 病毒扫描状态（预留）：上传后 PENDING；扫描器回调改 CLEAN/INFECTED；
    # 未启用扫描的环境由 FileService 直接置 SKIPPED
    scan_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="PENDING", server_default=text("'PENDING'")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
