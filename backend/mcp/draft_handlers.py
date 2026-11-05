"""MCP A1 草稿工具 Handler（T24 / PRD §5.2「支持巡访、补货、商家、合同、活动、督导草稿」）。

这些工具 `write_matrix` 为空（不在 §5.3.1 审批矩阵），风险等级 A1：把口述/表单
信息**组稿**为可编辑草稿，直接落独立暂存表 `mcp_drafts`，不写任何正式业务表，
因此不进入业绩/利润/巡访有效性/补货满足率/工资等正式指标口径（Q134）。

矩阵内的 merchant.create_draft / merchant.update_draft 走两阶段确认
（见 write_flow.py），不在本文件。

同 read_handlers：函数内惰性导入 Service/ORM，保持 `import mcp` 为纯声明层。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID

from mcp.envelope import date_arg, uuid_arg
from mcp.errors import McpSessionUnavailableError

if TYPE_CHECKING:
    from mcp.identity import McpContext

Handler = Any  # Callable[[McpContext, dict[str, Any]], dict[str, Any]]


def _session(context: McpContext, tool_name: str):
    session = getattr(context, "session", None)
    if session is None:
        raise McpSessionUnavailableError(tool_name)
    return session


def _stored(
    context: McpContext,
    *,
    kind: str,
    payload: dict[str, Any],
    merchant_id: UUID | None = None,
) -> dict[str, Any]:
    from app.services.mcp_draft_service import McpDraftService

    draft = McpDraftService(_session(context, kind)).save_draft(
        actor=context.actor,
        kind=kind,
        payload=payload,
        session_id=context.session_id or "",
        merchant_id=merchant_id,
    )
    _session(context, kind).commit()
    return {
        "kind": "draft",
        "draft_kind": kind,
        "draft_id": str(draft.id),
        "status": draft.status,
        "merchant_id": str(draft.merchant_id) if draft.merchant_id else None,
        "enters_production_metrics": False,
        "note": "草稿仅暂存，不进入正式指标；确认后由对应写工具具化为正式记录",
    }


# ----------------------------- 各草稿工具 -----------------------------


def visit_compose_draft(context: McpContext, args: dict[str, Any]) -> dict[str, Any]:
    merchant_id = uuid_arg(args, "merchant_id")
    return _stored(
        context,
        kind="visit",
        merchant_id=merchant_id,
        payload={
            "merchant_id": str(merchant_id),
            "transcript": args.get("transcript", ""),
        },
    )


def replenishment_create_draft(
    context: McpContext, args: dict[str, Any]
) -> dict[str, Any]:
    merchant_id = uuid_arg(args, "merchant_id")
    sku_id = uuid_arg(args, "sku_id")
    return _stored(
        context,
        kind="replenishment",
        merchant_id=merchant_id,
        payload={
            "merchant_id": str(merchant_id),
            "sku_id": str(sku_id),
            "requested_boxes": args["requested_boxes"],
        },
    )


def campaign_create_draft(
    context: McpContext, args: dict[str, Any]
) -> dict[str, Any]:
    return _stored(
        context,
        kind="campaign",
        payload={
            "title": args["title"],
            "planned_start": str(args["planned_start"]) if args.get("planned_start") else None,
            "merchant_ids": [str(x) for x in args.get("merchant_ids", [])],
        },
    )


def supervision_create_draft(
    context: McpContext, args: dict[str, Any]
) -> dict[str, Any]:
    merchant_id = uuid_arg(args, "merchant_id")
    return _stored(
        context,
        kind="supervision",
        merchant_id=merchant_id,
        payload={
            "merchant_id": str(merchant_id),
            "planned_date": str(date_arg(args, "planned_date") or ""),
            "focus": args.get("focus", ""),
        },
    )


DRAFT_HANDLERS: dict[str, Handler] = {
    "visit.compose_draft": visit_compose_draft,
    "replenishment.create_draft": replenishment_create_draft,
    "campaign.create_draft": campaign_create_draft,
    "supervision.create_draft": supervision_create_draft,
}


def bind_draft_handlers(registry: Any) -> Any:
    """绑定 A1 组稿草稿 Handler（schedule.suggest / procurement.suggest 为纯建议，
    不落库，仍由各自只读/建议 Handler 负责，本绑定只覆盖会落暂存表的组稿工具）。"""
    from mcp.levels import ToolKind

    declared = {
        spec.name for spec in registry.specs()
        if spec.kind is ToolKind.DRAFT and spec.write_matrix is None
    }
    # 纯建议类（不组稿、不落库）允许不绑定写 Handler
    optional = {"schedule.suggest", "procurement.suggest"}
    missing = (declared - set(DRAFT_HANDLERS)) - optional
    if missing:
        from mcp.errors import McpRegistrationRejectedError

        raise McpRegistrationRejectedError(
            "*", "A1 草稿工具缺少 Handler 绑定", {"missing": sorted(missing)}
        )
    for name, handler in DRAFT_HANDLERS.items():
        registry.bind_handler(name, handler)
    return registry


__all__ = ["DRAFT_HANDLERS", "bind_draft_handlers"]
