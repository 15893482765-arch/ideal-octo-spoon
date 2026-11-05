"""功能开关解析（T22 / PRD §5.3.1 功能开关列）。

每个写工具在审批矩阵中声明 `feature_flag`；开关关闭时工具调用被拒
（`MCP_FEATURE_DISABLED`）。开关值统一来自 `app.core.config.Settings`，
不允许散落在工具代码里硬编码。
"""

from __future__ import annotations

from app.core.config import Settings, get_settings

# 审批矩阵使用的大写开关名 → Settings 字段名
FEATURE_FLAG_SETTINGS: dict[str, str] = {
    "MERCHANT_ENABLED": "merchant_enabled",
    "VISIT_ENABLED": "visit_enabled",
    "REPLENISHMENT_ENABLED": "replenishment_enabled",
    "CONTRACT_ENABLED": "contract_enabled",
    "SCHEDULE_ENABLED": "schedule_enabled",
    "MERCHANT_TRANSFER_ENABLED": "merchant_transfer_enabled",
    "CAMPAIGN_ENABLED": "campaign_enabled",
    "SUPERVISION_ENABLED": "supervision_enabled",
    "SUPPLIER_PORTAL_ENABLED": "supplier_portal_enabled",
    "EXPORT_ENABLED": "export_enabled",
    "APPROVAL_ENABLED": "approval_enabled",
}

KNOWN_FEATURE_FLAGS: frozenset[str] = frozenset(FEATURE_FLAG_SETTINGS)


def is_known_flag(flag: str) -> bool:
    return flag in FEATURE_FLAG_SETTINGS


def is_enabled(flag: str, *, settings: Settings | None = None) -> bool:
    """开关是否开启；未知开关名一律视为**关闭**（最小权限）。"""
    attr = FEATURE_FLAG_SETTINGS.get(flag)
    if attr is None:
        return False
    resolved = settings or get_settings()
    return bool(getattr(resolved, attr, False))
