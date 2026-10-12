"""SQLAlchemy 2 声明式 Base。

只放 Base 与通用 mixin；具体实体在 app/models/。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import CHAR, DateTime, String, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """所有 ORM 模型的基类。

    命名约定在 metadata 中提供，Alembic 生成的约束名稳定可复现。
    """

    type_annotation_map = {dict[str, Any]: JSONB}


class DatasetMixin:
    """演示数据隔离（PRD §6 R18、§8）：dataset_type+dataset_id 不可变。"""

    dataset_type: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="PRODUCTION",
        server_default=text("'PRODUCTION'"),
    )
    dataset_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=False,
        default=uuid4,
        server_default=text("gen_random_uuid()"),
    )


class AuditMixin:
    """审计时间戳（PRD §5 审计留痕）。"""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
        onupdate=func.now(),
    )
    created_by_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    updated_by_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)


class ImmutableMixin:
    """主数据只停用不删除（PRD §1.5 主数据管理）：物理删除必须禁用。"""

    is_active: Mapped[bool] = mapped_column(
        nullable=False, default=True, server_default=text("true")
    )


def pk_uuid() -> Mapped[UUID]:
    """统一主键：UUID v4；Alembic 默认 gen_random_uuid()。"""
    return mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=text("gen_random_uuid()"),
    )


def char_id(length: int) -> Mapped[str]:
    """对外单号：固定长度 CHAR，CHECK 留给各实体。"""
    return mapped_column(CHAR(length), nullable=False, unique=True)
