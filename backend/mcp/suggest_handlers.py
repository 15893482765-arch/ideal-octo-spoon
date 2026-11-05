"""MCP A1 建议工具适配器：schedule.suggest / procurement.suggest（T25 / PRD §5.3、§5.5）。

红线（PRD §5.2、§9.1、§5.5）：
- 建议**只读**：不发布排班、不下采购单、不写任何业务表；
- 适配器只调用受控 Service，不直连 Repository/ORM/会话写表；
- 冲突/库存缺失时按 §5.5 降级：冲突只产可编辑草稿（优先级清单）、库存缺失只汇总
  确认需求并标注限制，绝不臆造库存、不把模型猜测当业务事实。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from mcp.envelope import (
    RESULT_OK,
    _base,
    date_arg,
    jsonable,
    limit_arg,
    record_link,
    uuid_arg,
)
from mcp.errors import McpRegistrationRejectedError, McpSessionUnavailableError
from mcp.identity import McpContext
from mcp.levels import ToolKind
from mcp.registry import ToolRegistry

Handler = Callable[[McpContext, dict[str, Any]], dict[str, Any]]

_DEFINITION_SCHEDULE = (
    "基于逾期、风险、活动占用、位置生成**可编辑**周巡访建议；被活动占用的商家"
    "降级为优先级清单。建议只读，不自动发布排班（需人工改期/改派后走受控发布）。"
)
_DEFINITION_PROCUREMENT = (
    "只用已确认补货需求、在途采购量生成采购建议；库存域未接入时只汇总确认需求并"
    "标注限制（inventory_status=UNAVAILABLE），不臆造可用库存。建议只读，不自动下单。"
)


def _session(context: McpContext, tool_name: str) -> Any:
    session = context.session
    if session is None:
        raise McpSessionUnavailableError(tool_name)
    return session


def _suggestion_envelope(
    context: McpContext, *, definition: str, result: dict[str, Any],
    note: str, extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """建议结果统一封装：result_state 恒 OK（建议存在性用 empty 标记，不与无权限混淆）。"""
    merged: dict[str, Any] = {
        "suggestion": jsonable(result),
        "read_only": True,
        "auto_apply": False,  # 红线：建议不自动发布/下单
    }
    if extra:
        merged.update(extra)
    payload = _base(
        context,
        definition=definition,
        result_state=RESULT_OK,
        as_of=None,
        links=[],
        note=note,
        extra=merged,
    )
    return payload


# ============================ 排班建议 ============================


def schedule_suggest(context: McpContext, args: dict[str, Any]) -> dict[str, Any]:
    from app.core.errors import DomainError, PermissionDeniedError
    from app.services.schedule_service import ScheduleService

    session = _session(context, "schedule.suggest")
    week_start = date_arg(args, "week_start")
    if week_start is None:
        raise DomainError(
            code="MCP_ARGUMENT_REQUIRED",
            message="schedule.suggest 必须提供 week_start（周一，YYYY-MM-DD）",
            details={"argument": "week_start"},
        )
    # 行级范围只来自服务端 actor：显式 region_id 必须在授权片区内，否则默认首个片区。
    region_id = _resolve_region_id(context, args.get("region_id"))
    if region_id is None:
        raise DomainError(
            code="MCP_ARGUMENT_REQUIRED",
            message="当前行级范围无法确定片区，请用有片区授权的身份调用或显式传 region_id",
            details={"argument": "region_id"},
        )

    lng = _optional_float(args.get("lng"))
    lat = _optional_float(args.get("lat"))
    limit = limit_arg(args, default=50)

    svc = ScheduleService(session)
    try:
        plan = svc.suggest_plan(
            context.actor,
            region_id=region_id,
            week_start=week_start,
            lng=lng,
            lat=lat,
            limit=limit,
        )
    except PermissionDeniedError as exc:
        return _no_permission(context, exc)
    except DomainError as exc:
        if exc.code == "SCHEDULE_WEEK_NOT_MONDAY":
            raise DomainError(
                code="MCP_ARGUMENT_INVALID",
                message="week_start 必须是 Asia/Shanghai 的周一",
                details={"argument": "week_start"},
            ) from exc
        raise

    links = [
        record_link("MERCHANT", row["merchant_id"])
        for row in plan.to_dict()["editable"] + plan.to_dict()["priority_list"]
    ]
    note = (
        "建议只读，不自动发布；被活动占用的商家已降级为优先级清单，"
        "需人工改期/改派后再走受控发布。"
        if plan.degraded
        else "建议只读，不自动发布；可编辑后走周排班受控发布。"
    )
    payload = _suggestion_envelope(
        context, definition=_DEFINITION_SCHEDULE, result=plan.to_dict(), note=note,
    )
    payload["links"] = links
    return payload


# ============================ 采购建议 ============================


def procurement_suggest(context: McpContext, args: dict[str, Any]) -> dict[str, Any]:
    from app.core.errors import DomainError, PermissionDeniedError
    from app.services.procurement_service import ProcurementService

    session = _session(context, "procurement.suggest")

    brand_id, region_id = _resolve_brand_region(context, args)
    if brand_id is None or region_id is None:
        raise DomainError(
            code="MCP_ARGUMENT_REQUIRED",
            message="procurement.suggest 需在行级范围内确定 brand_id 与 region_id",
            details={"argument": "brand_id/region_id"},
        )

    requested_from = date_arg(args, "requested_from")
    requested_to = date_arg(args, "requested_to")

    svc = ProcurementService(session)
    try:
        lines = svc.build_suggestions(
            context.actor,
            brand_id=brand_id,
            region_id=region_id,
            requested_from=requested_from,
            requested_to=requested_to,
        )
    except PermissionDeniedError as exc:
        return _no_permission(context, exc)

    # 库存取 T32 收发存台账真实余额：建议 = 确认缺口 − 有效在途 − 现存库存。
    rows = [
        {
            "sku_id": str(ln.sku_id),
            "sku_code": ln.sku_code,
            "confirmed_unmet_boxes": str(ln.confirmed_unmet_boxes),
            "open_purchase_boxes": str(ln.open_purchase_boxes),
            "available_boxes": str(ln.available_boxes),
            "suggested_purchase_boxes": str(ln.suggested_purchase_boxes),
            "inventory_available": bool(ln.inventory_available),
            "inventory_status": ln.inventory_status,
        }
        for ln in lines
    ]
    inventory_degraded = any(r["inventory_status"] == "UNAVAILABLE" for r in rows)
    result = {
        "lines": rows,
        "brand_id": str(brand_id),
        "region_id": str(region_id),
        "inventory_status": "UNAVAILABLE" if inventory_degraded else "AVAILABLE",
        "degraded": inventory_degraded,
        "degraded_reason": (
            "INVENTORY_UNAVAILABLE:库存台账不可读，仅汇总已确认需求与在途量，"
            "未扣减可用库存"
            if inventory_degraded
            else None
        ),
        "requested_from": requested_from.isoformat() if requested_from else None,
        "requested_to": requested_to.isoformat() if requested_to else None,
    }
    note = (
        "建议只读，不自动下单；库存台账不可读，仅按已确认需求与在途量给出。"
        if inventory_degraded
        else "建议只读，不自动下单；已按系统库存台账扣减现存箱数。"
    )
    return _suggestion_envelope(
        context, definition=_DEFINITION_PROCUREMENT, result=result, note=note,
    )


# ============================ 行级范围解析（只取服务端注入）============================


def _resolve_region_id(context: McpContext, raw: Any):
    actor = context.actor
    if raw not in (None, ""):
        return uuid_arg({"region_id": raw}, "region_id")
    return next(iter(actor.region_ids), None)


def _resolve_brand_region(context: McpContext, args: dict[str, Any]):
    actor = context.actor
    raw_brand = args.get("brand_id")
    raw_region = args.get("region_id")
    brand_id = uuid_arg({"brand_id": raw_brand}, "brand_id") if raw_brand else None
    region_id = uuid_arg({"region_id": raw_region}, "region_id") if raw_region else None
    # 非全局角色：缺省时取服务端注入的首个授权品牌/片区；显式传值仍由 Service 鉴权。
    if brand_id is None:
        brand_id = next(iter(actor.brand_ids), None)
    if region_id is None:
        region_id = next(iter(actor.region_ids), None)
    return brand_id, region_id


def _optional_float(raw: Any) -> float | None:
    if raw in (None, ""):
        return None
    try:
        return float(raw)
    except (TypeError, ValueError) as exc:
        from app.core.errors import DomainError

        raise DomainError(
            code="MCP_ARGUMENT_INVALID",
            message="坐标必须是数字",
            details={"argument": "lng/lat"},
        ) from exc


def _no_permission(context: McpContext, exc: Exception) -> dict[str, Any]:
    return _base(
        context,
        definition=_DEFINITION_SCHEDULE,
        result_state="NO_PERMISSION",
        as_of=None,
        links=[],
        note=getattr(exc, "message", str(exc)),
        extra={"reason_code": getattr(exc, "code", "PERMISSION_DENIED")},
    )


# ============================ 绑定 ============================

SUGGEST_HANDLERS: dict[str, Handler] = {
    "schedule.suggest": schedule_suggest,
    "procurement.suggest": procurement_suggest,
}


def bind_suggest_handlers(registry: ToolRegistry) -> ToolRegistry:
    """把 T25 A1 建议 Handler 绑定到注册表，并强校验「目录 ↔ Handler」一致。"""
    declared = {
        spec.name
        for spec in registry.specs()
        if spec.kind is ToolKind.DRAFT and spec.name in SUGGEST_HANDLERS
    }
    if declared != set(SUGGEST_HANDLERS):
        raise McpRegistrationRejectedError(
            "*",
            "建议工具 Handler 绑定与目录不一致",
            {
                "missing": sorted(set(SUGGEST_HANDLERS) - declared),
                "extra": sorted(declared - set(SUGGEST_HANDLERS)),
            },
        )
    for name, handler in SUGGEST_HANDLERS.items():
        registry.bind_handler(name, handler)
    return registry


__all__ = ["SUGGEST_HANDLERS", "bind_suggest_handlers"]
