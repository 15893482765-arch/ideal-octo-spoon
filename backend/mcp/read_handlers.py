"""MCP 只读查询工具适配器（T23 / PRD §5.3 A0 工具、§5.1、§4.11）。

定位（PRD §9.2）：MCP 适配器**只调用受控 Service**，不直连数据库、不导入
`app.repositories` / `app.db` / ORM 会话；所有业务规则、行级范围与状态机仍由
Service 裁决。为此本模块**在函数内惰性导入** `app.services.*`，模块导入期不拉入
SQLAlchemy，保证 `import mcp` 仍是纯声明层。

覆盖 PRD §5.3 的 13 个 A0 只读工具：
`merchant.search/get`、`contract.search/get`、`freezer.search/get_history`、
`visit.list_due/get`、`replenishment.aggregate`、`performance.preview`、
`finance.profit_report`、`approval.list`、`risk.list`。

统一约束：
- 行级范围只来自服务端注入的 `McpContext.actor`（模型自带的 region/brand 已被网关剥离）；
- 返回值统一经 `mcp.envelope` 携带数据范围 / 口径 / 截至时间 / 业务记录链接；
- 「无权限」与「无记录」用 `result_state` 明确区分，不互相冒充；
- 指标只引用 `MetricsService`、风险只引用 `RiskService`，不重算；
- 金额/箱数经 `jsonable` 输出为字符串，禁止 float（红线）。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from app.services.demo_data_guard import DataScope


from mcp.envelope import (
    date_arg,
    limit_arg,
    list_envelope,
    no_permission,
    no_records,
    permission_envelope,
    record_envelope,
    record_link,
    risk_links,
    uuid_arg,
)
from mcp.errors import McpRegistrationRejectedError, McpSessionUnavailableError
from mcp.identity import McpContext
from mcp.levels import ToolKind
from mcp.registry import ToolRegistry

Handler = Callable[[McpContext, dict[str, Any]], dict[str, Any]]

SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")

_SUPPLIER = "SUPPLIER"
_SALES_REP = "SALES_REP"

# 待巡访口径（与 VisitTaskStatus 枚举一致，避免自行发明状态）
_DUE_TASK_STATUSES = frozenset({"PENDING", "IN_PROGRESS", "OVERDUE"})

_DEF = {
    "merchant.search": (
        "按当前用户行级范围返回商家摘要（业务员=本人负责商家，经理/督导=本片区，"
        "财务/管理员=全部）；厂商仅返回脱敏汇总，不含任何商家标识字段。"
    ),
    "merchant.get": (
        "读取单个商家档案摘要，须在当前用户行级范围内；联系方式与精确坐标按角色脱敏。"
    ),
    "contract.search": (
        "按行级权限返回合同及状态（业务员仅本人负责；经理/督导/财务/管理员按片区或全局）。"
    ),
    "contract.get": "读取单个合同状态、关键条款与版本历史，须在行级范围内。",
    "freezer.search": (
        "按资产编号/状态/商家查询冰柜台账；采购价等成本字段按角色字段级脱敏。"
    ),
    "freezer.get_history": (
        "读取单个冰柜的投放/撤回/验收/维修/押金流水（唯一来源 FreezerMovement，不重算）。"
    ),
    "visit.list_due": (
        "查询行级范围内的待巡访任务（业务员=本人，经理/督导=本片区），按业务周日历周过滤。"
    ),
    "visit.get": (
        "读取巡访记录详情与有效性裁决；validity 为 VisitValidityService 落库结果，不重算。"
    ),
    "replenishment.aggregate": (
        "只汇总**已确认**补货需求（未确认不进口径，R8），按 SKU 汇总承诺箱数。"
    ),
    "performance.preview": (
        "查询本人预估或授权范围内成员绩效；有快照读快照，无快照按当期生效规则"
        "只读试算（不落库、不进工资）。"
    ),
    "finance.profit_report": (
        "损益与差异解释数据，全部来自 MetricsService（不重算）；经理版为脱敏摘要。"
    ),
    "approval.list": (
        "查询本角色待办/已办审批：待办按审批矩阵角色路由，已办按实际审批人归属。"
    ),
    "risk.list": "查询权限范围内的风险清单（唯一来源 RiskService，不重算风险判定）。",
    "payroll.list": (
        "查询工资单：财务（payroll 能力）查数据集内全员；其余内部角色仅本人。"
        "默认最近 6 个完整月（不含本月），可用 period 指定单月或 months 调整窗口。"
    ),
    "knowledge.search": (
        "检索公司制度知识库（全员可见的公开制度文档），返回来源文档编号、小节原文与相似度。"
    ),
}


def _session(context: McpContext, tool_name: str) -> Any:
    session = context.session
    if session is None:
        raise McpSessionUnavailableError(tool_name)
    return session


def _production() -> DataScope:
    """正式口径标记（PRD R34）：只读工具一律只吃 PRODUCTION 数据域。"""
    from app.services.demo_data_guard import DataScope

    return DataScope.PRODUCTION


def _mask(context: McpContext, payload: dict[str, Any]) -> dict[str, Any]:
    """统一字段级裁剪（厂商敏感字段 / 手机号掩码 / 成本与工资字段）。"""
    from app.core.redaction import sanitize_response

    return sanitize_response(context.actor, payload)


def _week_bounds(week_start: date | None) -> tuple[date, date]:
    today = datetime.now(SHANGHAI_TZ).date()
    monday = week_start or (today - timedelta(days=today.weekday()))
    return monday, monday + timedelta(days=6)


# =============================== 商家 ===============================


def merchant_search(context: McpContext, args: dict[str, Any]) -> dict[str, Any]:
    from app.schemas.merchant import MerchantListQuery
    from app.services.merchant_service import MerchantService

    session = _session(context, "merchant.search")
    definition = _DEF["merchant.search"]
    actor = context.actor

    if actor.role_code == _SUPPLIER:
        summary = MerchantService(session).supplier_redacted_summary(actor)
        return record_envelope(
            context,
            definition=definition,
            record=summary,
            note="厂商仅可见脱敏汇总（按授权片区计数），不含商家明细",
            extra={"masked": True},
        )

    query = MerchantListQuery(
        status=args.get("status"),
        keyword=args.get("keyword"),
        page=1,
        page_size=limit_arg(args, default=20),
    )
    items, total, applied = MerchantService(session).list_merchants(actor, query)
    rows = [_mask(context, item.model_dump()) for item in items]
    return list_envelope(
        context,
        definition=definition,
        items=rows,
        links=[record_link("MERCHANT", item.id) for item in items],
        extra={"total": int(total), "applied_filters": applied},
    )


def merchant_get(context: McpContext, args: dict[str, Any]) -> dict[str, Any]:
    from app.core.errors import DomainError, PermissionDeniedError
    from app.services.merchant_service import MerchantService

    session = _session(context, "merchant.get")
    definition = _DEF["merchant.get"]
    merchant_id = uuid_arg(args, "merchant_id")

    if context.actor.role_code == _SUPPLIER:
        return no_permission(
            context,
            definition=definition,
            reason_code="MERCHANT_DETAIL_FORBIDDEN_FOR_SUPPLIER",
            message="厂商仅可查看脱敏汇总，无商家档案明细权限",
        )

    try:
        detail = MerchantService(session).get_merchant_detail(
            context.actor, merchant_id
        )
    except PermissionDeniedError as exc:
        return permission_envelope(context, definition=definition, exc=exc)
    except DomainError as exc:
        if exc.code == "MERCHANT_NOT_FOUND":
            return no_records(
                context,
                definition=definition,
                reason_code=exc.code,
                message=exc.message,
            )
        raise

    return record_envelope(
        context,
        definition=definition,
        record=_mask(context, detail),
        links=[record_link("MERCHANT", merchant_id)],
    )


# =============================== 合同 ===============================


def contract_search(context: McpContext, args: dict[str, Any]) -> dict[str, Any]:
    from app.services.contract_service import ContractService

    session = _session(context, "contract.search")
    definition = _DEF["contract.search"]
    views = ContractService(session).list_contracts(
        context.actor, status=args.get("status"), keyword=args.get("keyword")
    )
    merchant_id = args.get("merchant_id")
    if merchant_id:
        wanted = str(merchant_id)
        views = [v for v in views if str(v.merchant_id) == wanted]
    limit = limit_arg(args, default=20)
    views = views[:limit]
    rows = [_mask(context, v.model_dump()) for v in views]
    return list_envelope(
        context,
        definition=definition,
        items=rows,
        links=[record_link("CONTRACT", v.id) for v in views],
    )


def contract_get(context: McpContext, args: dict[str, Any]) -> dict[str, Any]:
    from app.core.errors import DomainError, PermissionDeniedError
    from app.services.contract_service import ContractService

    session = _session(context, "contract.get")
    definition = _DEF["contract.get"]
    contract_id = uuid_arg(args, "contract_id")
    try:
        view, versions, _renewals = ContractService(session).get_detail(
            context.actor, contract_id
        )
    except PermissionDeniedError as exc:
        return permission_envelope(
            context,
            definition=definition,
            exc=exc,
            links=[record_link("CONTRACT", contract_id)],
        )
    except DomainError as exc:
        if exc.code == "CONTRACT_NOT_FOUND":
            return no_records(
                context,
                definition=definition,
                reason_code=exc.code,
                message=exc.message,
            )
        raise

    record = _mask(context, view.model_dump())
    record["versions"] = [_mask(context, v.model_dump()) for v in versions]
    return record_envelope(
        context,
        definition=definition,
        record=record,
        links=[record_link("CONTRACT", contract_id)],
    )


# =============================== 冰柜 ===============================


def freezer_search(context: McpContext, args: dict[str, Any]) -> dict[str, Any]:
    from app.services.freezer_service import FreezerService

    session = _session(context, "freezer.search")
    definition = _DEF["freezer.search"]
    views = FreezerService(session).search_assets(
        context.actor,
        asset_code=args.get("asset_code"),
        status=args.get("status"),
        merchant_id=args.get("merchant_id"),
        limit=limit_arg(args, default=50),
    )
    rows = [_mask(context, v.model_dump()) for v in views]
    return list_envelope(
        context,
        definition=definition,
        items=rows,
        links=[record_link("FREEZER", v.id) for v in views],
    )


def freezer_get_history(context: McpContext, args: dict[str, Any]) -> dict[str, Any]:
    from app.core.errors import DomainError, PermissionDeniedError
    from app.core.permissions import PermissionError_
    from app.services.freezer_service import FreezerService

    session = _session(context, "freezer.get_history")
    definition = _DEF["freezer.get_history"]
    asset_id = uuid_arg(args, "asset_id")
    try:
        view, movements = FreezerService(session).get_detail(context.actor, asset_id)
    except (PermissionDeniedError, PermissionError_) as exc:
        code = getattr(exc, "code", "PERMISSION_DENIED")
        message = getattr(exc, "message", str(exc))
        return no_permission(
            context, definition=definition, reason_code=code, message=message
        )
    except DomainError as exc:
        if exc.code == "FREEZER_NOT_FOUND":
            return no_records(
                context,
                definition=definition,
                reason_code=exc.code,
                message=exc.message,
            )
        raise

    rows = [_mask(context, m.model_dump()) for m in movements]
    return list_envelope(
        context,
        definition=definition,
        items=rows,
        links=[record_link("FREEZER", asset_id)],
        extra={"asset": _mask(context, view.model_dump())},
    )


# =============================== 巡访 ===============================


def visit_list_due(context: McpContext, args: dict[str, Any]) -> dict[str, Any]:
    from app.services.visit_service import VisitService

    session = _session(context, "visit.list_due")
    definition = _DEF["visit.list_due"]
    start, end = _week_bounds(date_arg(args, "week_start"))
    keyword = str(args.get("keyword") or "").strip()
    tasks = VisitService(session).list_my_tasks(context.actor, None)
    due = [
        t
        for t in tasks
        if t.status in _DUE_TASK_STATUSES and start <= t.planned_date <= end
    ]
    # 任务行本身不含商家名：补一张 id→名 映射，既支撑 keyword 过滤也让
    # 高亮/回话直接报商家名而不是 UUID。任务列表已过行级权限，名字不越权。
    merchant_names: dict[Any, str] = {}
    if due:
        from sqlalchemy import select

        from app.models import Merchant

        merchant_ids = {t.merchant_id for t in due}
        merchant_names = dict(
            session.execute(
                select(Merchant.id, Merchant.name).where(Merchant.id.in_(merchant_ids))
            ).all()
        )
    if keyword:
        due = [
            t for t in due if keyword in merchant_names.get(t.merchant_id, "")
        ]
    rows = [
        {
            "id": t.id,
            "merchant_id": t.merchant_id,
            "merchant_name": merchant_names.get(t.merchant_id),
            "assigned_user_id": t.assigned_user_id,
            "planned_date": t.planned_date,
            "window_start_at": t.window_start_at,
            "window_end_at": t.window_end_at,
            "priority": t.priority,
            "source": t.source,
            "status": t.status,
            "reason": t.reason,
        }
        for t in due
    ]
    return list_envelope(
        context,
        definition=definition,
        items=rows,
        links=[record_link("VISIT_TASK", t.id) for t in due],
        extra={"week_start": start.isoformat(), "week_end": end.isoformat()},
    )


def visit_get(context: McpContext, args: dict[str, Any]) -> dict[str, Any]:
    from app.core.errors import DomainError, PermissionDeniedError
    from app.schemas.visit import (
        VisitPhotoView,
        VisitRecordView,
        VisitReplenishmentLineView,
        VisitRevisionView,
        VisitSalesLineView,
    )
    from app.services.visit_service import VisitService

    session = _session(context, "visit.get")
    definition = _DEF["visit.get"]
    visit_id = uuid_arg(args, "visit_id")
    try:
        record, photos, revisions, lng_lat, sales_lines, replenishments = (
            VisitService(session).get_record_detail(context.actor, visit_id)
        )
    except PermissionDeniedError as exc:
        return permission_envelope(context, definition=definition, exc=exc)
    except DomainError as exc:
        if exc.code == "VISIT_RECORD_NOT_FOUND":
            return no_records(
                context,
                definition=definition,
                reason_code=exc.code,
                message=exc.message,
            )
        raise

    lng, lat = lng_lat if lng_lat else (None, None)
    view = VisitRecordView(
        id=record.id,
        visit_task_id=record.visit_task_id,
        merchant_id=record.merchant_id,
        user_id=record.user_id,
        source=record.source,
        checkin_at=record.checkin_at,
        checkout_at=record.checkout_at,
        device_checkin_at=record.device_checkin_at,
        lng=lng,
        lat=lat,
        accuracy_meters=record.accuracy_meters,
        distance_meters=record.distance_meters,
        display_pct=record.display_pct,
        display_threshold_pct=record.display_threshold_pct,
        display_met=record.display_met,
        freezer_status=record.freezer_status,
        device_issue=record.device_issue,
        conclusion=record.conclusion,
        photo_count=record.photo_count,
        note=record.note,
        is_official=record.is_official,
        status=record.status,
        validity=record.validity,
        judged_by_id=record.judged_by_id,
        judged_at=record.judged_at,
        judge_reason=record.judge_reason,
        submitted_at=record.submitted_at,
        photos=[
            VisitPhotoView(id=p.id, file_id=p.file_id, photo_kind=p.photo_kind)
            for p in photos
        ],
        revisions=[
            VisitRevisionView(
                id=r.id, author_id=r.author_id, supplement=r.supplement,
                created_at=r.created_at,
            )
            for r in revisions
        ],
        sales_lines=[
            VisitSalesLineView(
                id=s.id, sku_id=s.sku_id, reported_boxes=s.reported_boxes,
                feedback=s.feedback, out_of_stock=s.out_of_stock,
            )
            for s in sales_lines
        ],
        replenishment_lines=[
            VisitReplenishmentLineView(
                order_id=o.id, code=o.code, sku_id=o.sku_id,
                requested_boxes=o.requested_boxes,
                expected_delivery_date=o.promised_delivery_date,
                status=o.status,
            )
            for o in replenishments
        ],
    )
    return record_envelope(
        context,
        definition=definition,
        record=view.model_dump(),
        links=[record_link("VISIT_RECORD", record.id)],
    )


# =============================== 补货 ===============================


def replenishment_aggregate(
    context: McpContext, args: dict[str, Any]
) -> dict[str, Any]:
    from app.services.metrics_service import parse_period
    from app.services.replenishment_service import ReplenishmentService

    session = _session(context, "replenishment.aggregate")
    definition = _DEF["replenishment.aggregate"]
    period = args.get("period")
    requested_from = requested_to = None
    if period:
        requested_from, requested_to = parse_period(str(period))
    merchant_id = args.get("merchant_id")
    summary = ReplenishmentService(session).summarize_confirmed(
        context.actor,
        merchant_ids=[uuid_arg(args, "merchant_id")] if merchant_id else None,
        requested_from=requested_from,
        requested_to=requested_to,
    )
    by_sku = summary.get("by_sku", {})
    rows = [
        {"sku_id": sku_id, "confirmed_boxes": boxes}
        for sku_id, boxes in sorted(by_sku.items())
    ]
    return list_envelope(
        context,
        definition=definition,
        items=rows,
        extra={
            "confirmed_order_count": summary.get("confirmed_order_count", 0),
            "confirmed_boxes_total": summary.get("confirmed_boxes_total", "0"),
            "period": period,
        },
    )


# =============================== 绩效 ===============================


def performance_preview(context: McpContext, args: dict[str, Any]) -> dict[str, Any]:
    from app.core.errors import DomainError, PermissionDeniedError
    from app.services.performance_snapshot_service import PerformanceSnapshotService

    session = _session(context, "performance.preview")
    definition = _DEF["performance.preview"]
    actor = context.actor
    period = args.get("period")
    if not period:
        return no_records(
            context,
            definition=definition,
            reason_code="PERF_PERIOD_REQUIRED",
            message="必须指定统计周期（YYYY-MM）",
        )
    subject = args.get("subject_user_id")
    if subject:
        user_id = uuid_arg(args, "subject_user_id")
        if actor.role_code == _SALES_REP and user_id != actor.user_id:
            return no_permission(
                context,
                definition=definition,
                reason_code="PERF_SCOPE_FORBIDDEN",
                message="业务员只能查看本人绩效预估",
            )
    else:
        user_id = actor.user_id

    try:
        preview = PerformanceSnapshotService(session).preview(
            actor, user_id=user_id, period_month=str(period)
        )
    except PermissionDeniedError as exc:
        return permission_envelope(context, definition=definition, exc=exc)
    except DomainError as exc:
        if exc.code in ("PERF_USER_NOT_FOUND", "PERF_RULE_NOT_EFFECTIVE"):
            return no_records(
                context,
                definition=definition,
                reason_code=exc.code,
                message=exc.message,
            )
        raise
    return record_envelope(context, definition=definition, record=preview)


# =============================== 工资 ===============================


def _recent_full_months(count: int) -> list[str]:
    """最近 N 个完整月（不含本月），倒序（「近半年工资，本月不算」口径）。"""
    today = datetime.now(SHANGHAI_TZ).date()
    cursor = today.replace(day=1)
    months: list[str] = []
    for _ in range(count):
        prev = cursor - timedelta(days=1)
        months.append(f"{prev.year}-{prev.month:02d}")
        cursor = prev.replace(day=1)
    return months


def payroll_list(context: McpContext, args: dict[str, Any]) -> dict[str, Any]:
    from app.core.permissions import MODULE_PAYROLL, permission_service
    from app.services.payroll_service import PayrollService

    session = _session(context, "payroll.list")
    definition = _DEF["payroll.list"]
    period = str(args.get("period") or "").strip()
    months_raw = args.get("months")
    months = min(max(int(months_raw), 1), 12) if months_raw is not None else 6
    periods = [period] if period else _recent_full_months(months)

    svc = PayrollService(session)
    actor = context.actor
    # 财务走全员工资发放口径；其余内部角色走本人自查口径（仓库层 user_id 截断）
    is_finance = permission_service.has_ability(actor, MODULE_PAYROLL)
    user_name = str(args.get("user_name") or "").strip()
    if user_name and not is_finance:
        # 非财务点名：只可能与本人姓名一致，否则明确提示——
        # 绝不静默退回本人口径造成张冠李戴（与绩效点名同一原则）
        from app.models import User

        me = session.get(User, actor.user_id)
        my_name = str(me.display_name or "").strip() if me else ""
        if user_name != my_name:
            return no_records(
                context,
                definition=definition,
                reason_code="PAYROLL_SELF_ONLY",
                message="工资单仅支持查询本人记录，无权查看他人工资单",
            )
    items: list[dict[str, Any]] = []
    links: list[dict[str, Any]] = []
    for p in periods:
        if is_finance:
            rows, _ = svc.list_statements(actor, period_month=p, page=1, page_size=200)
            if user_name:
                # 精确匹配库内姓名；匹配不到即该月无此人工资单
                rows = [r for r in rows if str(r.get("user_name") or "") == user_name]
        else:
            rows, _ = svc.list_own_statements(actor, period_month=p, page=1, page_size=200)
        for row in rows:
            st = row["statement"]
            items.append(
                _mask(
                    context,
                    {
                        "id": st.id,
                        "statement_no": st.statement_no,
                        "user_id": st.user_id,
                        "user_name": row["user_name"],
                        "period_month": st.period_month,
                        "base_salary_fen": st.base_salary_fen,
                        "performance_fen": st.performance_fen,
                        "adjustment_fen": st.adjustment_fen,
                        "gross_pay_fen": st.gross_pay_fen,
                        "correction_fen": st.correction_fen,
                        "corrected_pay_fen": st.corrected_pay_fen,
                        "status": st.status,
                        "paid_on": st.paid_on,
                    },
                )
            )
            links.append(record_link("PAYROLL", st.id))
    if not items:
        subject = f"{user_name} 在 " if (user_name and is_finance) else ""
        return no_records(
            context,
            definition=definition,
            reason_code="PAYROLL_NO_RECORDS",
            message=(
                f"{subject}{'、'.join(periods)} 期间没有可查的工资单"
                + ("" if is_finance else "（本人口径）")
            ),
        )
    return list_envelope(
        context,
        definition=definition,
        items=items,
        links=links,
        extra={"periods": periods, "scope": "dataset" if is_finance else "self"},
    )


# =============================== 财务 ===============================


def finance_profit_report(context: McpContext, args: dict[str, Any]) -> dict[str, Any]:
    from app.services.metrics_service import M_MONTHLY_NET_PROFIT, MetricsService

    session = _session(context, "finance.profit_report")
    definition = _DEF["finance.profit_report"]
    period = args.get("period")
    if not period:
        return no_records(
            context,
            definition=definition,
            reason_code="REPORT_PERIOD_REQUIRED",
            message="必须指定统计周期（YYYY-MM 或 YYYY-Www）",
        )
    svc = MetricsService(session)
    net = svc.compute(
        context.actor,
        M_MONTHLY_NET_PROFIT,
        scope=_production(),
        period=str(period),
    )
    record: dict[str, Any] = {
        "period": str(period),
        "net_profit_fen": None if net.value is None else str(net.value),
        "definition": net.definition,
        "data_complete": net.data_complete,
        "note": net.note,
    }
    extra: dict[str, Any] = {}
    if context.actor.role_code == "REGIONAL_MANAGER":
        # PRD §5.3：经理可见损益但为脱敏版，成本/工资明细只对财务开放
        record["redacted"] = True
        record["masked_fields"] = ["metrics", "source_refs"]
        extra["masked"] = True
    else:
        results = svc.all(context.actor, scope=_production(), period=str(period))
        record["redacted"] = False
        record["metrics"] = [r.to_dict() for r in results]
    return record_envelope(
        context,
        definition=definition,
        record=record,
        as_of=net.as_of,
        note="指标口径与数值全部来自 MetricsService，未在适配器重算",
        extra=extra,
    )


# =============================== 审批 / 风险 ===============================


def approval_list(context: McpContext, args: dict[str, Any]) -> dict[str, Any]:
    from app.services.approval_service import ApprovalService

    session = _session(context, "approval.list")
    definition = _DEF["approval.list"]
    items = ApprovalService(session).list_for_actor(
        context.actor,
        status=args.get("status"),
        target_object_type=args.get("target_object_type"),
    )
    rows = [
        {
            "id": a.id,
            "target_object_type": a.target_object_type,
            "target_object_id": a.target_object_id,
            "action": a.action,
            "status": a.status,
            "requester_id": a.requester_id,
            "approver_id": a.approver_id,
            "matrix_role_code": a.matrix_role_code,
            "reason": a.reason,
            "decided_at": a.decided_at,
            "created_at": a.created_at,
        }
        for a in items
    ]
    links = [record_link(a.target_object_type, a.target_object_id) for a in items]
    return list_envelope(
        context, definition=definition, items=rows, links=links
    )


def risk_list(context: McpContext, args: dict[str, Any]) -> dict[str, Any]:
    from app.services.risk_service import RiskService

    session = _session(context, "risk.list")
    definition = _DEF["risk.list"]
    items = RiskService(session).list_risks(
        context.actor,
        scope=_production(),
        status=args.get("status"),
        rule_code=args.get("rule_code"),
    )
    rows = [
        {
            "id": r.id,
            "rule_code": r.rule_code,
            "object_type": r.object_type,
            "object_id": r.object_id,
            "period": r.period,
            "status": r.status,
            "severity": r.severity,
            "first_seen_at": r.first_seen_at,
            "last_seen_at": r.last_seen_at,
            "closed_at": r.closed_at,
            "resolution": r.resolution,
            "data_incomplete": r.data_incomplete,
        }
        for r in items
    ]
    links: list[dict[str, Any]] = []
    for r in items:
        links.extend(risk_links(r.object_type, r.object_id))
    return list_envelope(context, definition=definition, items=rows, links=links)


# =============================== 知识库 ===============================


def knowledge_search(context: McpContext, args: dict[str, Any]) -> dict[str, Any]:
    from app.services.knowledge_search import DEFAULT_TOP_K, search_knowledge

    session = _session(context, "knowledge.search")
    definition = _DEF["knowledge.search"]
    query = str(args.get("query") or "").strip()
    top_k_raw = args.get("top_k")
    top_k = min(max(int(top_k_raw), 1), 10) if top_k_raw is not None else DEFAULT_TOP_K

    hits, mode = search_knowledge(session, query, top_k=top_k)
    if not hits:
        return no_records(
            context,
            definition=definition,
            reason_code="KNOWLEDGE_NO_MATCH",
            message="未在制度知识库中找到与问题相关的内容，可换个问法或关键词重试",
        )
    rows = [
        {
            "doc_id": hit.doc_id,
            "doc_title": hit.doc_title,
            "section_path": hit.section_path,
            "content": hit.content,
            "score": round(hit.score, 4),
        }
        for hit in hits
    ]
    note = (
        "语义检索暂不可用，已回退为关键词匹配，结果可能不够精确"
        if mode == "keyword"
        else None
    )
    extra: dict[str, Any] = {"retrieval_mode": mode}
    if any(hit.injection_blocked for hit in hits):
        # 文档正文中的越权指令已被剥离（内容侧清洗）；此处标记供审计回溯
        extra["injection_blocked"] = True
    return list_envelope(
        context,
        definition=definition,
        items=rows,
        note=note,
        extra=extra,
    )


# =============================== 绑定 ===============================

READ_HANDLERS: dict[str, Handler] = {
    "merchant.search": merchant_search,
    "merchant.get": merchant_get,
    "contract.search": contract_search,
    "contract.get": contract_get,
    "freezer.search": freezer_search,
    "freezer.get_history": freezer_get_history,
    "visit.list_due": visit_list_due,
    "visit.get": visit_get,
    "replenishment.aggregate": replenishment_aggregate,
    "performance.preview": performance_preview,
    "finance.profit_report": finance_profit_report,
    "approval.list": approval_list,
    "risk.list": risk_list,
    "payroll.list": payroll_list,
    "knowledge.search": knowledge_search,
}


def bind_read_handlers(registry: ToolRegistry) -> ToolRegistry:
    """把 T23 只读 Handler 绑定到注册表，并强校验「目录 ↔ Handler」完全一致。

    任一方向不一致（漏绑/多绑）即拒绝绑定，避免出现「元数据声明了但不可执行」
    或「可执行但不在白名单目录」的漂移。

    每个 Handler 统一包一层能力门禁兜底：`PermissionError_`（T03 能力矩阵拒绝）
    转为明确的「无权限」结果，不静默返回空列表、也不冒泡成 500。
    """
    declared = {spec.name for spec in registry.specs() if spec.kind is ToolKind.READ}
    provided = set(READ_HANDLERS)
    if declared != provided:
        raise McpRegistrationRejectedError(
            "*",
            "只读工具 Handler 绑定与目录不一致",
            {
                "missing": sorted(declared - provided),
                "extra": sorted(provided - declared),
            },
        )
    for name, handler in READ_HANDLERS.items():
        registry.bind_handler(name, _guarded(name, handler))
    return registry


def _guarded(name: str, handler: Handler) -> Handler:
    from app.core.permissions import PermissionError_

    def wrapper(context: McpContext, args: dict[str, Any]) -> dict[str, Any]:
        try:
            return handler(context, args)
        except PermissionError_ as exc:
            return no_permission(
                context,
                definition=_DEF.get(name, ""),
                reason_code="PERMISSION_DENIED",
                message=f"当前角色无权访问该工具所需数据：{exc}",
            )

    return wrapper


__all__ = ["READ_HANDLERS", "bind_read_handlers"]
