"""用户、角色与（区域×品牌）行级授权。"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import AuditMixin, Base, DatasetMixin, ImmutableMixin, pk_uuid


class Role(Base, ImmutableMixin, AuditMixin, DatasetMixin):
    __tablename__ = "roles"
    __table_args__ = (
        CheckConstraint("char_length(code) > 0", name="roles_code_nonempty"),
        # role_code 绑定到 PRD §2.2 的 6 类角色枚举，是代码权限矩阵的唯一锚
        CheckConstraint(
            "role_code IN ('SALES_REP','REGIONAL_MANAGER','SUPERVISOR',"
            "'FINANCE','SYSTEM_ADMIN','SUPPLIER')",
            name="roles_role_code_check",
        ),
        UniqueConstraint("dataset_type", "dataset_id", "code", name="uq_roles_dataset_code"),
        UniqueConstraint(
            "dataset_type", "dataset_id", "role_code", name="uq_roles_dataset_role_code"
        ),
    )

    id: Mapped[UUID] = pk_uuid()
    code: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    # 角色在权限矩阵中的枚举锚；与 code 不同：code 允许重复带前缀的"展示名"
    role_code: Mapped[str] = mapped_column(String(32), nullable=False)


class User(Base, ImmutableMixin, AuditMixin, DatasetMixin):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("char_length(username) >= 3", name="users_username_minlen"),
        # 仅存 Argon2 哈希；明文密码不得入表（PRD §2 红线）
        CheckConstraint("password_hash LIKE '$argon2id$%'", name="users_password_hash_format"),
        CheckConstraint(
            "status IN ('ACTIVE','INACTIVE','PENDING_ACTIVATION')",
            name="users_status_check",
        ),
        CheckConstraint("failed_login_count >= 0", name="users_failed_login_nonneg"),
        CheckConstraint(
            "password_version >= 1", name="users_password_version_positive"
        ),
        UniqueConstraint(
            "dataset_type", "dataset_id", "username", name="uq_users_dataset_username"
        ),
        Index("ix_users_status", "status"),
    )

    id: Mapped[UUID] = pk_uuid()
    username: Mapped[str] = mapped_column(String(64), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    role_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("roles.id", ondelete="RESTRICT"), nullable=False
    )
    # PRD §2.1：账号生命周期 + §2.3 厂商默认待激活
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="ACTIVE", server_default=text("'ACTIVE'")
    )
    # 首次激活 / 重置后必须改密
    must_change_password: Mapped[bool] = mapped_column(
        nullable=False, default=False, server_default=text("false")
    )
    # 登录失败计数 + 锁定到期时间（PRD §2.1 登录失败限流）
    failed_login_count: Mapped[int] = mapped_column(
        nullable=False, default=0, server_default=text("0")
    )
    locked_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    password_changed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # 角色/密码版本：版本号变化即视为该用户全部 token 失效（PRD §2.1 角色变更后旧 token 失效）
    password_version: Mapped[int] = mapped_column(
        nullable=False, default=1, server_default=text("1")
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
