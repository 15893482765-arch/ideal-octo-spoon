"""响应 Schema 字段裁剪（T04 / PRD §2.4、§4.3、§8）。

规则（字段类 × 角色）：
- 手机号：SUPPLIER 完全隐藏；FINANCE/SALES_REP/REGIONAL_MANAGER/SUPERVISOR 掩码
  （138****1234）；SYSTEM_ADMIN 明文（账号管理需要）。掩码 vs 明文的取舍
  在 T08 商家中心落地时按业务确认，当前默认从严。
- 精确坐标：SUPPLIER 隐藏；其余内部角色可见（巡访导航需要）。
- 工资字段：仅 FINANCE / SYSTEM_ADMIN 可见（业务员"看自己"由 T19 专用接口放行，
  通用列表一律裁剪）。
- 成本/毛利字段：FINANCE / SYSTEM_ADMIN / REGIONAL_MANAGER 可见
  （经理需要生成采购建议）；SALES_REP / SUPERVISOR / SUPPLIER 隐藏。

使用方式：所有对外响应在返回前必须经过 `sanitize_response(actor, payload)`；
禁止在 Router 内手写 if role 裁剪逻辑。
"""

from __future__ import annotations

import re
from typing import Any
from uuid import UUID

from app.core.permissions import ActorContext, permission_service
from app.models.enums import RoleCode

# ===== 字段类（命中其一即按该类策略处理）=====

PHONE_FIELDS = frozenset(
    {"phone", "contact_phone", "merchant_contact_phone", "merchant_owner_phone", "owner_phone"}
)
LOCATION_FIELDS = frozenset(
    {"location", "location_wkt", "latitude", "longitude", "merchant_location",
     "merchant_location_wkt", "address_detail", "merchant_address_detail"}
)
PAYROLL_FIELDS = frozenset(
    {"payroll_base_salary_fen", "payroll_total_fen", "payroll_deduction_fen", "salary_fen"}
)
COST_FIELDS = frozenset(
    {"cost_fen", "purchase_price_fen", "gross_profit_fen", "unit_cost_fen", "tax_unit_price_fen"}
)

# 可见工资/成本字段的角色
_PAYROLL_VISIBLE = frozenset({RoleCode.FINANCE.value, RoleCode.SYSTEM_ADMIN.value})
_COST_VISIBLE = frozenset(
    {RoleCode.FINANCE.value, RoleCode.SYSTEM_ADMIN.value, RoleCode.REGIONAL_MANAGER.value}
)

_PHONE_RE = re.compile(r"^(?:\+?86)?(1\d{2})(\d{4})(\d{4})$")


def mask_phone(value: str) -> str:
    """中国大陆手机号掩码：138****1234；非法格式原样返回（不猜）。"""
    m = _PHONE_RE.match(value.strip())
    if m is None:
        return value
    return f"{m.group(1)}****{m.group(3)}"


def _field_class(key: str) -> str | None:
    k = key.lower()
    if k in PHONE_FIELDS:
        return "phone"
    if k in LOCATION_FIELDS:
        return "location"
    if k in PAYROLL_FIELDS:
        return "payroll"
    if k in COST_FIELDS:
        return "cost"
    return None


def sanitize_response(actor: ActorContext, payload: dict[str, Any]) -> dict[str, Any]:
    """按角色画像裁剪响应字段。

    处理顺序：
    1. T03 `permission_service.redact_payload`（厂商 hidden_fields → None）；
    2. 本模块字段类策略：隐藏（None）/ 掩码 / 明文；
    3. 嵌套一层 dict（如 {"merchant": {...}}）递归处理。
    """
    redacted = permission_service.redact_payload(actor, payload)
    return _apply_policy(actor, redacted)


def _apply_policy(actor: ActorContext, payload: dict[str, Any]) -> dict[str, Any]:
    role = actor.role_code
    out: dict[str, Any] = {}
    for key, value in payload.items():
        cls = _field_class(key)
        if cls is None:
            out[key] = _apply_policy(actor, value) if isinstance(value, dict) else value
            continue
        if cls == "phone":
            if role == RoleCode.SUPPLIER.value:
                out[key] = None
            elif role == RoleCode.SYSTEM_ADMIN.value:
                out[key] = value
            else:
                out[key] = mask_phone(value) if isinstance(value, str) else value
        elif cls == "location":
            out[key] = None if role == RoleCode.SUPPLIER.value else value
        elif cls == "payroll":
            out[key] = value if role in _PAYROLL_VISIBLE else None
        elif cls == "cost":
            out[key] = value if role in _COST_VISIBLE else None
    return out


def visible_regions_for(actor: ActorContext, candidate: frozenset[UUID]) -> set[UUID]:
    """行级范围快捷入口（转发 PermissionService，避免旁路）。"""
    return permission_service.visible_for_actor(actor, candidate)
