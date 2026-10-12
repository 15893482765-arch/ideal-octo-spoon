"""权限矩阵与 PermissionService。

PRD §2.2/§2.3/§2.4 约束：
- 6 类角色（业务员/片区经理/督导/财务/系统管理员/厂商）的功能模块权限以代码矩阵为准；
- 行级范围：业务员只看本人商家/片区经理管本片区/厂商看授权 (brand, region)；
- 字段级脱敏：厂商不能看商家联系方式、精确坐标、工资等；系统管理员不自动获得工资发放能力。

任何 Service 在做能力判定时**必须**走本服务，不允许 if role == "..." 分支。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from app.models.enums import RoleCode

# ===== 模块能力 =====

# 模块编号与 PRD §2.2/§3.2 对齐；新增模块时必须先在这里登记
MODULE_USER_ADMIN = "user.admin"  # 建号/停用/启用/重置
MODULE_AUTH_LOGIN = "auth.login"
MODULE_DASHBOARD = "dashboard.view"
MODULE_MERCHANT = "merchant"
MODULE_CONTRACT = "contract"
MODULE_FREEZER = "freezer"
MODULE_VISIT = "visit"
MODULE_REPLENISHMENT = "replenishment"
MODULE_SALES_CONFIRM = "sales.confirm"
MODULE_SCHEDULE = "schedule"
MODULE_ACTIVITY = "activity"
MODULE_PROCUREMENT = "procurement"
MODULE_FINANCE = "finance"
MODULE_PERFORMANCE = "performance"
MODULE_PAYROLL = "payroll"  # 工资发放；系统管理员不自动获得
MODULE_PAYROLL_DISBURSE = "payroll.disburse"  # 标记"已发放"
MODULE_AUDIT = "audit"
MODULE_SETTINGS = "settings"
MODULE_SUPPLIER_CONFIRM = "supplier.confirm_supply"  # 厂商确认供货

# 敏感字段（厂商不读）— 这些是字段名维度，跨多个实体
SENSITIVE_FIELDS_SUPPLIER = frozenset(
    {
        "merchant_contact_phone",
        "merchant_owner_phone",
        "merchant_email",
        "merchant_location",  # 精确坐标
        "merchant_location_wkt",
        "merchant_address_detail",
        "payroll_base_salary_fen",
        "payroll_total_fen",
        "payroll_user_id",
        "performance_commission_fen",
        "performance_deduction_fen",
        "cost_fen",
        "gross_profit_fen",
        "phone",
        "contact_phone",
    }
)


@dataclass(frozen=True)
class RoleProfile:
    """单一角色在代码中的权限画像。"""

    code: RoleCode
    abilities: frozenset[str]
    # 字段级：默认可见字段；其余视为不读
    visible_fields: frozenset[str] = field(default_factory=frozenset)
    # 字段级：强制不可见（敏感字段红线条目）
    hidden_fields: frozenset[str] = field(default_factory=frozenset)
    # 行级范围标识：供 Service 决定 query filter
    row_scope: str = "self"  # self / region / brand_region / global_readonly / global


# ===== 6 类角色权限矩阵 =====

# 业务员：今日工作台 + 商家/合同/巡访/补货 + 自己看自己（绩效只读自己）
_PROFILE_SALES_REP = RoleProfile(
    code=RoleCode.SALES_REP,
    abilities=frozenset(
        {
            MODULE_AUTH_LOGIN,
            MODULE_DASHBOARD,
            MODULE_MERCHANT,
            MODULE_CONTRACT,
            MODULE_FREEZER,
            MODULE_VISIT,
            MODULE_REPLENISHMENT,
            MODULE_SCHEDULE,
            MODULE_ACTIVITY,
        }
    ),
    row_scope="self",
)

# 片区经理：本片区全权（不含工资发放）
_PROFILE_REGIONAL_MANAGER = RoleProfile(
    code=RoleCode.REGIONAL_MANAGER,
    abilities=frozenset(
        {
            MODULE_AUTH_LOGIN,
            MODULE_DASHBOARD,
            MODULE_MERCHANT,
            MODULE_CONTRACT,
            MODULE_FREEZER,
            MODULE_VISIT,
            MODULE_REPLENISHMENT,
            MODULE_SALES_CONFIRM,
            MODULE_SCHEDULE,
            MODULE_ACTIVITY,
            MODULE_PROCUREMENT,
            MODULE_PERFORMANCE,
        }
    ),
    row_scope="region",
)

# 督导：跟访、抽检、整改；只读
_PROFILE_SUPERVISOR = RoleProfile(
    code=RoleCode.SUPERVISOR,
    abilities=frozenset(
        {
            MODULE_AUTH_LOGIN,
            MODULE_DASHBOARD,
            MODULE_VISIT,
            MODULE_MERCHANT,
            MODULE_CONTRACT,
            MODULE_FREEZER,
            MODULE_REPLENISHMENT,
            MODULE_SCHEDULE,
            MODULE_ACTIVITY,
        }
    ),
    row_scope="region",
)

# 财务：工资发放、采购、绩效核算；不能动原始定位/照片
_PROFILE_FINANCE = RoleProfile(
    code=RoleCode.FINANCE,
    abilities=frozenset(
        {
            MODULE_AUTH_LOGIN,
            MODULE_DASHBOARD,
            MODULE_FINANCE,
            MODULE_PROCUREMENT,
            MODULE_PERFORMANCE,
            MODULE_PAYROLL,
            MODULE_PAYROLL_DISBURSE,
            MODULE_REPLENISHMENT,
        }
    ),
    row_scope="global",
)

# 系统管理员：账号/配置/审计/初始化；**不含** 工资发放
_PROFILE_SYSTEM_ADMIN = RoleProfile(
    code=RoleCode.SYSTEM_ADMIN,
    abilities=frozenset(
        {
            MODULE_AUTH_LOGIN,
            MODULE_DASHBOARD,
            MODULE_USER_ADMIN,
            MODULE_AUDIT,
            MODULE_SETTINGS,
            MODULE_MERCHANT,
            MODULE_CONTRACT,
            MODULE_FREEZER,
            MODULE_VISIT,
            MODULE_REPLENISHMENT,
            MODULE_SALES_CONFIRM,
            MODULE_SCHEDULE,
            MODULE_ACTIVITY,
            MODULE_PROCUREMENT,
            MODULE_FINANCE,
            MODULE_PERFORMANCE,
        }
    ),
    row_scope="global",
)

# 厂商：仅授权品牌/区域；只读不敏感；可确认供货
_PROFILE_SUPPLIER = RoleProfile(
    code=RoleCode.SUPPLIER,
    abilities=frozenset(
        {
            MODULE_AUTH_LOGIN,
            MODULE_DASHBOARD,
            MODULE_SUPPLIER_CONFIRM,
            MODULE_PROCUREMENT,
        }
    ),
    visible_fields=frozenset(
        {
            "id",
            "brand_code",
            "brand_name",
            "sku_code",
            "sku_name",
            "region_code",
            "region_name",
            "procurement_code",
            "supplier_confirm_quantity",
            "supplier_confirmed_delivery_date",
            "status",
        }
    ),
    hidden_fields=SENSITIVE_FIELDS_SUPPLIER,
    row_scope="brand_region",
)


_ROLE_PROFILES: dict[RoleCode, RoleProfile] = {
    RoleCode.SALES_REP: _PROFILE_SALES_REP,
    RoleCode.REGIONAL_MANAGER: _PROFILE_REGIONAL_MANAGER,
    RoleCode.SUPERVISOR: _PROFILE_SUPERVISOR,
    RoleCode.FINANCE: _PROFILE_FINANCE,
    RoleCode.SYSTEM_ADMIN: _PROFILE_SYSTEM_ADMIN,
    RoleCode.SUPPLIER: _PROFILE_SUPPLIER,
}


def get_profile(role_code: str) -> RoleProfile:
    """按 role_code 拉画像；找不到视为最严：仅登录。"""
    try:
        return _ROLE_PROFILES[RoleCode(role_code)]
    except (KeyError, ValueError):
        return RoleProfile(
            code=RoleCode.SALES_REP,  # 仅占位
            abilities=frozenset({MODULE_AUTH_LOGIN}),
            row_scope="self",
        )


@dataclass(frozen=True)
class ActorContext:
    """PermissionService 评估时的入参：服务端注入的 actor 信息。"""

    user_id: UUID
    role_code: str
    region_ids: frozenset[UUID]
    brand_ids: frozenset[UUID]
    supplier_brand_ids: frozenset[UUID] = field(default_factory=frozenset)
    supplier_region_ids: frozenset[UUID] = field(default_factory=frozenset)

    @property
    def profile(self) -> RoleProfile:
        return get_profile(self.role_code)


class PermissionError_(Exception):  # noqa: N801 — 避免与 app.core.errors.PermissionError 重名
    """权限不足。"""


class PermissionService:
    """唯一的能力权限 + 行级范围 + 字段脱敏入口。"""

    # -------- 能力 --------

    def require_ability(self, actor: ActorContext, module: str) -> None:
        if module not in actor.profile.abilities:
            raise PermissionError_(f"角色 {actor.role_code} 无权访问 {module}")

    def has_ability(self, actor: ActorContext, module: str) -> bool:
        return module in actor.profile.abilities

    # -------- 行级范围 --------

    def filter_merchant_ids(
        self, actor: ActorContext, merchant_region_ids: dict[UUID, UUID | None]
    ) -> set[UUID]:
        """根据行级范围过滤商家 ID 集合。

        merchant_region_ids: {merchant_id: region_id}；Service 负责取该商家所在的 region。
        返回 actor 有权读取的 merchant_id 集合。
        """
        scope = actor.profile.row_scope
        if scope == "global":
            return set(merchant_region_ids)
        if scope == "self":
            # 业务员：只看"主负责人=actor" 的商家；这里以"actor.user_id 与商家负责人比对"为前提
            # Service 层应只把"actor 自己负责的 merchant_id" 传入，本方法仅做全集切片
            return set(merchant_region_ids)
        if scope == "region":
            return {m for m, r in merchant_region_ids.items() if r in actor.region_ids}
        if scope == "brand_region":
            return set()  # 厂商不看商家列表（仅看汇总/采购/供货）；这里按空集防漏
        if scope == "global_readonly":
            return set(merchant_region_ids)
        return set()

    def visible_for_actor(
        self, actor: ActorContext, candidate_regions: Iterable[UUID]
    ) -> set[UUID]:
        scope = actor.profile.row_scope
        if scope in ("global", "self", "global_readonly"):
            return set(candidate_regions)
        if scope == "region":
            return {r for r in candidate_regions if r in actor.region_ids}
        if scope == "brand_region":
            return {r for r in candidate_regions if r in actor.supplier_region_ids}
        return set()

    # -------- 字段脱敏 --------

    def redact_payload(
        self, actor: ActorContext, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """对返回给前端的字典按角色画像剔除敏感字段。"""
        if not actor.profile.hidden_fields:
            return payload
        out = dict(payload)
        for key in list(out.keys()):
            if key in actor.profile.hidden_fields:
                out[key] = None
        return out


# 模块级单例：Service 与 Depends 通过 import 使用
permission_service = PermissionService()


# ===== 路由装饰器 / 依赖辅助 =====

SUPPLIER_PORTAL_GUARD = "supplier_portal_enabled"  # 配置 key


def supplier_portal_enabled() -> bool:
    """路由与 Service 共用：返回当前进程的厂商门户开关。"""
    from app.core.config import get_settings

    return bool(get_settings().supplier_portal_enabled)
