"""MCP 写工具受控执行器注册表（T24 / PRD §5.3.1「executor」列、§5.4）。

定位（PRD §9.2 分层）：MCP 适配器**只调用受控 Service**，绝不直连 Repository /
SQLAlchemy 会话去写业务表。每个写工具在 `WRITE_EXECUTORS` 登记一个执行器：

    ExecuteResult = execute(session, actor, args) -> ExecuteResult

执行器内部惰性导入并调用对应受控 Service（与审批矩阵 executor 列一一对应）。
业务规则（权限/冲突/状态机/非同人/供应商授权）全部由 Service 裁决并原样上抛
DomainError；执行器不重算、不吞错。审批/整改的「非同人」因此无法绕过（Q140）：
- ApprovalService.decide 的 SELF_APPROVAL_FORBIDDEN；
- SupervisionService 整改 VERIFY/CLOSE 的提交人非同人校验；
仍是唯一闸门，MCP 层没有、也不允许有旁路。

模块导入期不拉入 ORM/DB（函数内惰性导入），保持 `import mcp` 为纯声明层。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from uuid import UUID

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from app.core.permissions import ActorContext

# 由网关注入执行器的内部参数（业务 Service 看不到）
_SESSION_ID_KEY = "__session_id"
_IDEM_KEY_KEY = "__idempotency_key"


@dataclass(frozen=True)
class ExecuteResult:
    """受控执行的统一结果（落幂等响应，供 Q139 首次结果重放）。"""

    object_type: str
    object_id: str
    status: str
    detail: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "object_type": self.object_type,
            "object_id": self.object_id,
            "status": self.status,
            **self.detail,
        }


Executor = Callable[["Session", "ActorContext", dict[str, Any]], ExecuteResult]


def _uid(value: Any) -> UUID:
    return UUID(str(value))


def _date(value: Any) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


# ============================ 草稿（A1：暂存，不进正式表）============================


def _execute_merchant_create_draft(
    session: Session, actor: ActorContext, args: dict[str, Any]
) -> ExecuteResult:
    from app.services.mcp_draft_service import McpDraftService

    draft = McpDraftService(session).save_draft(
        actor=actor,
        kind="merchant",
        payload={
            "name": args["name"],
            "contact_name": args.get("contact_name"),
            "address_text": args.get("address_text"),
        },
        session_id=str(args.get(_SESSION_ID_KEY, "")),
        idempotency_key=str(args.get(_IDEM_KEY_KEY, "")),
    )
    return ExecuteResult(
        "MCP_DRAFT", str(draft.id), draft.status,
        {"kind": "merchant", "draft_id": str(draft.id)},
    )


def _execute_merchant_update_draft(
    session: Session, actor: ActorContext, args: dict[str, Any]
) -> ExecuteResult:
    from app.services.mcp_draft_service import McpDraftService

    draft = McpDraftService(session).save_draft(
        actor=actor,
        kind="merchant",
        payload={"changes": args["changes"]},
        merchant_id=_uid(args["merchant_id"]),
        session_id=str(args.get(_SESSION_ID_KEY, "")),
        idempotency_key=str(args.get(_IDEM_KEY_KEY, "")),
    )
    return ExecuteResult(
        "MCP_DRAFT", str(draft.id), draft.status,
        {"kind": "merchant", "merchant_id": str(args["merchant_id"]),
         "draft_id": str(draft.id)},
    )


# ============================ 受控写（两阶段确认后执行）============================


def _execute_visit_submit(
    session: Session, actor: ActorContext, args: dict[str, Any]
) -> ExecuteResult:
    from app.schemas.visit import VisitSubmit
    from app.services.visit_service import VisitService

    # catalog 入参为最小集；定位/照片/销量为提交校验项，未提供时由 Service
    # 按既有规则裁决（执行器不伪造业务事实，仅做参数到 schema 的映射）。
    payload: dict[str, Any] = {
        "visit_task_id": str(args["visit_task_id"]),
        "merchant_id": str(args["merchant_id"]),
        "display_pct": int(args.get("display_pct", 0)),
        "conclusion": args.get("conclusion", "NORMAL"),
        "note": args.get("note", ""),
        "lng": float(args.get("lng", 0.0)),
        "lat": float(args.get("lat", 0.0)),
        "photo_file_ids": list(args.get("photo_file_ids", [])),
        "sales_rows": list(args.get("sales_rows", [])),
        "new_orders": list(args.get("new_orders", [])),
    }
    record, _task, abnormal = VisitService(session).submit(
        actor, VisitSubmit.model_validate(payload)
    )
    status = "ABNORMAL_PENDING" if abnormal else str(record.status)
    return ExecuteResult(
        "VISIT_RECORD", str(record.id), status,
        {"visit_task_id": str(record.visit_task_id),
         "abnormal_pending": bool(abnormal)},
    )


def _execute_replenishment_submit(
    session: Session, actor: ActorContext, args: dict[str, Any]
) -> ExecuteResult:
    from app.services.replenishment_service import ReplenishmentService

    order = ReplenishmentService(session).submit_merchant_draft(
        actor,
        merchant_id=_uid(args["merchant_id"]),
        sku_id=_uid(args["sku_id"]),
        requested_boxes=Decimal(str(args["requested_boxes"])),
    )
    return ExecuteResult(
        "REPLENISHMENT_ORDER", str(order.id), str(order.status),
        {"merchant_id": str(args["merchant_id"]), "sku_id": str(args["sku_id"])},
    )


def _execute_contract_create_application(
    session: Session, actor: ActorContext, args: dict[str, Any]
) -> ExecuteResult:
    from app.services.contract_service import ContractService

    contract = ContractService(session).create_application_from_ai(
        actor,
        merchant_id=_uid(args["merchant_id"]),
        title=args["title"],
        effective_from=_date(args["effective_from"]),
        expire_at=_date(args["expire_at"]),
        commission_rate=(
            float(args["commission_rate"])
            if args.get("commission_rate") is not None
            else None
        ),
    )
    return ExecuteResult(
        "CONTRACT", str(contract.id), str(contract.status),
        {"merchant_id": str(args["merchant_id"]),
         "contract_no": contract.contract_no},
    )


def _execute_schedule_publish(
    session: Session, actor: ActorContext, args: dict[str, Any]
) -> ExecuteResult:
    from app.services.schedule_service import ScheduleService

    entry_ids = [_uid(x) for x in args.get("entry_ids", [])] or None
    batch, conflicts = ScheduleService(session).publish_week_draft(
        actor, week_start=_date(args["week_start"]), entry_ids=entry_ids
    )
    return ExecuteResult(
        "SCHEDULE_PLAN_BATCH", str(batch.id), str(batch.status),
        {"week_start": str(args["week_start"]),
         "conflict_count": len(conflicts)},
    )


def _execute_transfer_create_request(
    session: Session, actor: ActorContext, args: dict[str, Any]
) -> ExecuteResult:
    from app.services.transfer_service import TransferService

    transfer = TransferService(session).create_request(
        actor,
        merchant_id=_uid(args["merchant_id"]),
        to_user_id=_uid(args["to_user_id"]),
        reason=args["reason"],
        effective_date=_date(args["effective_date"])
        if args.get("effective_date") else None,
    )
    return ExecuteResult(
        "MERCHANT_TRANSFER", str(transfer.id), str(transfer.status),
        {"merchant_id": str(args["merchant_id"]),
         "to_user_id": str(args["to_user_id"]),
         "from_user_id": str(transfer.from_user_id)},
    )


def _execute_campaign_publish(
    session: Session, actor: ActorContext, args: dict[str, Any]
) -> ExecuteResult:
    from app.services.activity_service import ActivityService

    activity = ActivityService(session).publish(
        actor, activity_id=_uid(args["campaign_id"])
    )
    return ExecuteResult(
        "CAMPAIGN", str(activity.id), str(activity.status),
        {"campaign_id": str(args["campaign_id"])},
    )


def _execute_supervision_submit(
    session: Session, actor: ActorContext, args: dict[str, Any]
) -> ExecuteResult:
    from app.services.supervision_service import SupervisionService

    visit = SupervisionService(session).submit_by_merchant_plan(
        actor,
        merchant_id=_uid(args["merchant_id"]),
        planned_date=_date(args["planned_date"]),
        findings=args.get("findings", ""),
    )
    return ExecuteResult(
        "SUPERVISION_VISIT", str(visit.id), str(visit.status),
        {"merchant_id": str(args["merchant_id"])},
    )


def _execute_supplier_confirm_supply(
    session: Session, actor: ActorContext, args: dict[str, Any]
) -> ExecuteResult:
    from app.services.procurement_service import ProcurementService

    confirmation = ProcurementService(session).confirm_supply_by_code(
        actor,
        procurement_code=str(args["procurement_code"]),
        confirmed_quantity_cases=Decimal(str(args["confirmed_cases"])),
        expected_delivery_date=_date(args["confirmed_delivery_date"]),
        note=args.get("note"),
    )
    return ExecuteResult(
        "SUPPLY_CONFIRMATION", str(confirmation.id), str(confirmation.status),
        {"order_id": str(confirmation.order_id),
         "line_id": str(confirmation.order_line_id),
         "procurement_code": str(args["procurement_code"])},
    )


def _execute_report_export_request(
    session: Session, actor: ActorContext, args: dict[str, Any]
) -> ExecuteResult:
    from app.services.demo_data_guard import DataScope
    from app.services.export_service import ExportService

    job = ExportService(session).request_export(
        actor,
        report_key=str(args["report_key"]),
        fmt=str(args["format"]),
        filters={"period": args.get("period")} if args.get("period") else None,
        scope=DataScope.PRODUCTION,
    )
    return ExecuteResult(
        "EXPORT_JOB", str(job.id), str(job.status),
        {"report_key": str(args["report_key"]), "format": str(args["format"])},
    )


def _execute_approval_decide(
    session: Session, actor: ActorContext, args: dict[str, Any]
) -> ExecuteResult:
    from app.services.approval_service import ApprovalService

    req = ApprovalService(session).decide(
        approval_id=_uid(args["approval_id"]),
        actor=actor,
        decision=str(args["decision"]),
        reason=args.get("reason"),
    )
    return ExecuteResult(
        "APPROVAL_REQUEST", str(req.id), str(req.status),
        {"decision": str(args["decision"]), "approver_id": str(actor.user_id)},
    )


# 工具 → 受控执行器（与 §5.3.1 executor 列严格对应；Q138 参数化校验依据）
WRITE_EXECUTORS: dict[str, Executor] = {
    "merchant.create_draft": _execute_merchant_create_draft,
    "merchant.update_draft": _execute_merchant_update_draft,
    "visit.submit": _execute_visit_submit,
    "replenishment.submit": _execute_replenishment_submit,
    "contract.create_application": _execute_contract_create_application,
    "schedule.publish": _execute_schedule_publish,
    "merchant.transfer.create_request": _execute_transfer_create_request,
    "campaign.publish": _execute_campaign_publish,
    "supervision.submit": _execute_supervision_submit,
    "supplier.confirm_supply": _execute_supplier_confirm_supply,
    "report.export_request": _execute_report_export_request,
    "approval.decide": _execute_approval_decide,
}

# 审批矩阵 executor 声明 → 执行器必须调用的受控 Service 类名（Q138/Q140 校验）
EXECUTOR_SERVICE: dict[str, str] = {
    "merchant.create_draft": "McpDraftService",
    "merchant.update_draft": "McpDraftService",
    "visit.submit": "VisitService",
    "replenishment.submit": "ReplenishmentService",
    "contract.create_application": "ContractService",
    "schedule.publish": "ScheduleService",
    "merchant.transfer.create_request": "TransferService",
    "campaign.publish": "ActivityService",
    "supervision.submit": "SupervisionService",
    "supplier.confirm_supply": "ProcurementService",
    "report.export_request": "ExportService",
    "approval.decide": "ApprovalService",
}


def get_executor(tool_name: str) -> Executor:
    if tool_name not in WRITE_EXECUTORS:
        raise KeyError(f"未登记受控执行器：{tool_name}")
    return WRITE_EXECUTORS[tool_name]
