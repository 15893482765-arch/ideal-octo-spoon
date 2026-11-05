"""/api/admin/users 路由：系统管理员的账号管理。

PRD §2.1 + §2.4：
- 只有拥有 user.admin 能力的角色（=SYSTEM_ADMIN）能进入；
- 管理员不能停用/降权自己；
- 建号 / 重新激活 返回的 `one_time_credential` 是 Activation token，只展示一次，
  后续接口无法再次获取。
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Body, Depends, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.deps import db_session, require_ability
from app.core.errors import DomainError
from app.core.permissions import MODULE_USER_ADMIN, ActorContext
from app.models import Brand, Region, Role, Supplier, SupplierAuthorization, User, UserRegionBrandAuth
from app.models.enums import UserStatus
from app.services import UserAdminService

router = APIRouter(prefix="/api/admin/users", tags=["admin/users"])


# ============== Schemas ==============


class CreateUserIn(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    display_name: str = Field(min_length=1, max_length=128)
    phone: str | None = Field(default=None, max_length=32)
    role_id: UUID
    # 厂商账号：关联供应商（role=SUPPLIER 时使用）
    supplier_id: UUID | None = Field(default=None)
    # 行级授权：品牌 + 片区列表（SUPPLIER 必填，其他角色可选）
    brand_ids: list[UUID] = Field(default_factory=list)
    region_ids: list[UUID] = Field(default_factory=list)


class CreateUserOut(BaseModel):
    user_id: str
    username: str
    role_code: str
    # 一次性凭据：明文 Activation token，只在创建/重新激活时返回
    one_time_credential: str


class DeactivateIn(BaseModel):
    reason: str = Field(min_length=1, max_length=200)


class UserOut(BaseModel):
    id: str
    username: str
    display_name: str
    role_code: str
    status: str
    must_change_password: bool
    is_locked: bool
    # PRD §2.4：厂商账号展示授权范围
    supplier_ids: list[str] = Field(default_factory=list)
    brand_codes: list[str] = Field(default_factory=list)
    region_codes: list[str] = Field(default_factory=list)


class RoleOut(BaseModel):
    id: str
    code: str
    name: str
    role_code: str


class SupplierOptionOut(BaseModel):
    id: str
    code: str
    name: str
    active: bool


# ============== 端点 ==============


@router.get("/roles", response_model=list[RoleOut])
def list_roles(
    actor: ActorContext = Depends(require_ability(MODULE_USER_ADMIN)),  # noqa: B008
    session: Session = Depends(db_session),  # noqa: B008
) -> list[RoleOut]:
    """角色下拉源：建号与变更角色只能从受控角色集合里选（PRD §2.2 六类角色）。"""
    roles = session.execute(select(Role).order_by(Role.role_code)).scalars().all()
    return [
        RoleOut(id=str(r.id), code=r.code, name=r.name, role_code=r.role_code)
        for r in roles
    ]


@router.post("", response_model=CreateUserOut, status_code=status.HTTP_201_CREATED)
def create_user(
    payload: CreateUserIn,
    actor: ActorContext = Depends(require_ability(MODULE_USER_ADMIN)),  # noqa: B008
    session: Session = Depends(db_session),  # noqa: B008
) -> CreateUserOut:
    role = session.get(Role, payload.role_id)
    if role is None:
        raise DomainError(code="ROLE_NOT_FOUND", message="角色不存在")
    is_supplier_role = role.role_code == "SUPPLIER"

    # 厂商账号必须有供应商主体与最小授权范围，否则建出来看不到任何采购单
    if is_supplier_role:
        if payload.supplier_id is None:
            raise DomainError(
                code="SUPPLIER_REQUIRED", message="厂商账号必须选择所属供应商"
            )
        if not payload.brand_ids or not payload.region_ids:
            raise DomainError(
                code="SUPPLIER_SCOPE_REQUIRED",
                message="厂商账号必须至少选择一个品牌和一个片区，否则无法看到采购单",
            )

    created = UserAdminService(session).create_account(
        actor_user_id=actor.user_id,
        username=payload.username,
        display_name=payload.display_name,
        phone=payload.phone,
        role_id=payload.role_id,
    )
    svc = UserAdminService(session)
    if is_supplier_role:
        svc.assign_supplier_auth(
            user_id=created.user_id,
            supplier_id=payload.supplier_id,  # type: ignore[arg-type]
            brand_ids=payload.brand_ids,
            region_ids=payload.region_ids,
        )
    elif payload.brand_ids or payload.region_ids:
        # 内部账号：未选的一侧按「全部启用项」展开（区域经理只需选片区）
        svc.assign_region_brand_auth(
            user_id=created.user_id,
            brand_ids=payload.brand_ids,
            region_ids=payload.region_ids,
        )
    return CreateUserOut(
        user_id=str(created.user_id),
        username=created.username,
        role_code=created.role_code,
        one_time_credential=created.one_time_credential,
    )


@router.post("/{user_id}/deactivate", status_code=204)
def deactivate(
    user_id: UUID,
    payload: DeactivateIn,
    actor: ActorContext = Depends(require_ability(MODULE_USER_ADMIN)),  # noqa: B008
    session: Session = Depends(db_session),  # noqa: B008
) -> None:
    UserAdminService(session).deactivate(
        actor_user_id=actor.user_id, target_user_id=user_id, reason=payload.reason
    )


@router.post("/{user_id}/reactivate", response_model=CreateUserOut)
def reactivate(
    user_id: UUID,
    actor: ActorContext = Depends(require_ability(MODULE_USER_ADMIN)),  # noqa: B008
    session: Session = Depends(db_session),  # noqa: B008
) -> CreateUserOut:
    created = UserAdminService(session).activate_account(
        actor_user_id=actor.user_id, target_user_id=user_id
    )
    return CreateUserOut(
        user_id=str(created.user_id),
        username=created.username,
        role_code=created.role_code,
        one_time_credential=created.one_time_credential,
    )


@router.post("/{user_id}/change-role", status_code=204)
def change_role(
    user_id: UUID,
    payload: dict = Body(...),  # noqa: B008 — FastAPI body
    actor: ActorContext = Depends(require_ability(MODULE_USER_ADMIN)),  # noqa: B008
    session: Session = Depends(db_session),  # noqa: B008
) -> None:
    UserAdminService(session).change_role(
        actor_user_id=actor.user_id,
        target_user_id=user_id,
        new_role_id=UUID(str(payload["role_id"])),
    )


@router.get("/suppliers", response_model=list[SupplierOptionOut])
def list_supplier_options(
    actor: ActorContext = Depends(require_ability(MODULE_USER_ADMIN)),  # noqa: B008
    session: Session = Depends(db_session),  # noqa: B008
) -> list[SupplierOptionOut]:
    """厂商账号创建时的供应商下拉：仅返回正式数据集的在职供应商。"""
    from app.services.demo_dataset import PRODUCTION_DATASET_TYPE

    rows = session.execute(
        select(Supplier)
        .where(
            Supplier.dataset_type == PRODUCTION_DATASET_TYPE,
            Supplier.active.is_(True),
        )
        .order_by(Supplier.code.asc())
    ).scalars().all()
    return [SupplierOptionOut(id=str(s.id), code=s.code, name=s.name, active=s.active) for s in rows]


@router.get("", response_model=list[UserOut])
def list_users(
    actor: ActorContext = Depends(require_ability(MODULE_USER_ADMIN)),  # noqa: B008
    session: Session = Depends(db_session),  # noqa: B008
) -> list[UserOut]:
    users = session.execute(select(User).order_by(User.created_at)).scalars().all()
    out: list[UserOut] = []
    role_cache: dict[UUID, str] = {}
    for u in users:
        if u.role_id not in role_cache:
            r = session.get(Role, u.role_id)
            role_cache[u.role_id] = r.role_code if r else ""
        # 读取该用户的供应商授权 + 行级授权
        supplier_ids: list[str] = []
        brand_codes: list[str] = []
        region_codes: list[str] = []
        if role_cache[u.role_id] == "SUPPLIER":
            auths = session.execute(
                select(SupplierAuthorization, Brand.code, Region.code)
                .where(SupplierAuthorization.user_id == u.id)
                .join(Brand, Brand.id == SupplierAuthorization.brand_id)
                .join(Region, Region.id == SupplierAuthorization.region_id)
            ).all()
            for a, brand_code, region_code in auths:
                supplier_ids.append(str(a.supplier_id))
                brand_codes.append(brand_code)
                region_codes.append(region_code)
        else:
            # 内部用户：读取 UserRegionBrandAuth
            auths = session.execute(
                select(UserRegionBrandAuth, Brand.code, Region.code)
                .where(UserRegionBrandAuth.user_id == u.id)
                .join(Brand, Brand.id == UserRegionBrandAuth.brand_id)
                .join(Region, Region.id == UserRegionBrandAuth.region_id)
            ).all()
            for a, brand_code, region_code in auths:
                brand_codes.append(brand_code)
                region_codes.append(region_code)
        out.append(
            UserOut(
                id=str(u.id),
                username=u.username,
                display_name=u.display_name,
                role_code=role_cache[u.role_id],
                status=u.status,
                must_change_password=u.must_change_password,
                is_locked=u.is_active is False or u.status == UserStatus.INACTIVE.value,
                supplier_ids=supplier_ids,
                brand_codes=brand_codes,
                region_codes=region_codes,
            )
        )
    return out
