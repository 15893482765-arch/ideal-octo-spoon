"""用户、角色与（区域×品牌）行级授权。"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import AuditMixin, Base, DatasetMixin, ImmutableMixin, pk_uuid


class Role(Base, ImmutableMixin, AuditMixin, DatasetMixin):
    __tablename__ = "roles"
    __table_args__ = (
        CheckConstraint("char_length(code) > 0", name="roles_code_nonempty"),
        UniqueConstraint("dataset_type", "dataset_id", "code", name="uq_roles_dataset_code"),
    )

    id: Mapped[UUID] = pk_uuid()
    code: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)


class User(Base, ImmutableMixin, AuditMixin, DatasetMixin):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("char_length(username) >= 3", name="users_username_minlen"),
        # 仅存 Argon2 哈希；明文密码不得入表（PRD §2 红线）
        CheckConstraint("password_hash LIKE '$argon2id$%'", name="users_password_hash_format"),
        UniqueConstraint(
            "dataset_type", "dataset_id", "username", name="uq_users_dataset_username"
        ),
    )

    id: Mapped[UUID] = pk_uuid()
    username: Mapped[str] = mapped_column(String(64), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    role_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("roles.id", ondelete="RESTRICT"), nullable=False
    )
    is_locked: Mapped[bool] = mapped_column(
        nullable=False, default=False, server_default=text("false")
    )


class UserRegionBrandAuth(Base, AuditMixin):
    """行级授权：用户 × 区域 × 品牌（PRD §2 授权矩阵）。

    行级数据范围只能由此处派发；禁止客户端或模型指定 actor/role/region/brand。
    """

    __tablename__ = "user_region_brand_auths"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "region_id", "brand_id", name="uq_user_region_brand"
        ),
        Index("ix_user_region_brand_user", "user_id"),
    )

    id: Mapped[UUID] = pk_uuid()
    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    region_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("regions.id", ondelete="RESTRICT"), nullable=False
    )
    brand_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("brands.id", ondelete="RESTRICT"), nullable=False
    )
