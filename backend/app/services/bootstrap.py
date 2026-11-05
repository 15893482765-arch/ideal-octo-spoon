"""系统引导：仅当数据库内不存在系统管理员，且配置了 INITIAL_ADMIN_* 时执行一次。

设计原则（PRD §2.1）：
- 不开放注册；首任系统管理员只能由运维在部署时通过环境变量注入；
- 密码仍是 Argon2id 哈希存储；本服务只在内存中接收明文并立即哈希；
- 执行后 operator 应立即从环境变量中移除 INITIAL_ADMIN_PASSWORD，避免凭据长期存在。
"""

from __future__ import annotations

from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import DomainError
from app.core.security import hash_password
from app.models import Role, User
from app.models.enums import RoleCode, UserStatus

# 引导管理员的固定数据域：全平台仅此一个，用于让
# uq_users_dataset_username 成为「多副本并发引导」的仲裁者（见 bootstrap_initial_admin）。
BOOTSTRAP_DATASET_ID: UUID = uuid5(NAMESPACE_URL, "qinshuang/platform/bootstrap-admin")


def ensure_system_admin_role(
    session: Session, *, dataset_type: str = "PRODUCTION", dataset_id: UUID | None = None
) -> Role:
    """确保系统管理员角色存在；不存在则在 (dataset_type, dataset_id) 内创建。

    不带 dataset_id 时使用新的 dataset_id，调用方应自己维护稳定 dataset 以复用。
    """
    import uuid as _uuid

    if dataset_id is None:
        dataset_id = _uuid.uuid4()
    role = session.execute(
        select(Role).where(
            Role.role_code == RoleCode.SYSTEM_ADMIN.value,
            Role.dataset_type == dataset_type,
            Role.dataset_id == dataset_id,
        )
    ).scalar_one_or_none()
    if role is not None:
        return role
    role = Role(
        code="SYSTEM_ADMIN",
        name="系统管理员",
        role_code=RoleCode.SYSTEM_ADMIN.value,
        dataset_type=dataset_type,
        dataset_id=dataset_id,
    )
    session.add(role)
    session.commit()
    session.refresh(role)
    return role


def bootstrap_initial_admin(session: Session, settings: Settings) -> dict[str, Any] | None:
    """若配置齐全且库内无系统管理员，创建首位管理员。

    返回 {"username": ..., "user_id": str}；否则返回 None。
    """
    if not settings.initial_admin_bootstrap:
        return None
    if not (
        settings.initial_admin_username
        and settings.initial_admin_password
        and settings.initial_admin_display_name
    ):
        return None

    # 是否已有系统管理员账号（status=ACTIVE 且 role=system admin）
    existing = (
        session.execute(
            select(User)
            .join(Role, Role.id == User.role_id)
            .where(Role.role_code == RoleCode.SYSTEM_ADMIN.value)
            .where(User.status == UserStatus.ACTIVE.value)
        )
        .scalars()
        .first()
    )
    if existing is not None:
        return None

    # 首任管理员使用固定的独立 dataset_id（而非每次随机）：
    # 多 worker / 多副本同时启动时，各自都会通过上面的“不存在”检查，
    # 此时由 uq_users_dataset_username 唯一约束裁决——只有第一个插入成功，
    # 其余捕获唯一冲突并视为“引导已完成”，绝不产生重复管理员账号。
    dsid = BOOTSTRAP_DATASET_ID
    try:
        role = ensure_system_admin_role(session, dataset_id=dsid)
        user = User(
            username=settings.initial_admin_username.strip().lower(),
            display_name=settings.initial_admin_display_name.strip(),
            phone=None,
            role_id=role.id,
            status=UserStatus.ACTIVE.value,
            must_change_password=True,
            password_hash=hash_password(settings.initial_admin_password.get_secret_value()),
            dataset_type="PRODUCTION",
            dataset_id=dsid,
        )
        session.add(user)
        session.commit()
    except IntegrityError:
        # 另一个副本已抢先完成引导：幂等成功（不是错误）
        session.rollback()
        return None
    except Exception as exc:
        session.rollback()
        raise DomainError(code="BOOTSTRAP_FAILED", message="首次系统管理员创建失败") from exc
    session.refresh(user)
    return {"username": user.username, "user_id": str(user.id)}
