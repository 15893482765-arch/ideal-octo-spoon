"""T25 排班、采购与经营建议（PRD §4.7、§4.9、§5.2、§5.3、§5.5、§9.1）。

覆盖：
- Q141 排班建议避开活动：主负责人本周被 CAMPAIGN 占用的商家不进「可编辑建议」；
- Q142 冲突只产草稿：活动占用降级为优先级清单，建议只读、不写任何排班/任务/占用；
- Q143 库存接入：采购建议读 T32 收发存台账真实余额并扣减建议量，
        台账不可读才降级标注 UNAVAILABLE；
- Q144 采购只含确认需求：未确认（DRAFT/SUBMITTED）补货不进口径，在途量被扣减；
- Q145 指标同源：经营解释的事实证据全部来自 MetricsService（同输入同值），LLM 不产出数值；
- Q146 mock 可复现：同 (kind,facts,seed) 多次/跨实例输出一致；结构化通过 Pydantic 校验；
- Q147 非结构化输出降级：Provider 无法产出结构化结果时只返回确定性证据，不虚构解释。

红线：建议不自动发布/下单；不把模型猜测当业务事实；LLM 不重算金额与指标。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from decimal import Decimal
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session
from tests.test_t09_contract import _merchant, _user, _world
from tests.test_t11_visit import _assign_primary
from tests.test_t18_procurement import (
    _confirmed_replenishment,
    _internal_actor,
    _sku,
    _supplier_world,
    _unconfirmed_replenishment,
)

from app.models import (
    PurchaseOrder,
    PurchaseOrderLine,
    ResourceReservation,
    SchedulePlanBatch,
    SchedulePlanEntry,
    VisitTask,
)
from app.models.enums import PurchaseOrderStatus, RoleCode
from app.schemas.ai import Narrative
from app.services.ai_provider import MockAIProvider
from app.services.business_insight_service import BusinessInsightService
from app.services.demo_data_guard import DataScope
from app.services.metrics_service import MetricsService
from app.services.procurement_service import ProcurementService
from app.services.schedule_service import ScheduleService
from mcp.identity import McpContext

TZ = ZoneInfo("Asia/Shanghai")
MONDAY = date(2026, 12, 14)


# ----------------------------- 造数辅助 -----------------------------


def _manager(session, w, tag="t25mgr"):
    return _internal_actor(session, w, RoleCode.REGIONAL_MANAGER, tag)


def _rep_with_shops(session, w, n, *, tag="t25rep"):
    rep = _user(session, w["ds"], RoleCode.SALES_REP, tag)
    shops = []
    for i in range(n):
        m = _merchant(session, w, name=f"T25店-{i}-{uuid4().hex[:5]}")
        m.grade = "B"
        session.flush()
        _assign_primary(session, m, rep)
        shops.append(m)
    session.flush()
    return rep, shops


def _campaign_reservation(session, w, user, *, day: date, hour=9, hours=3):
    """在指定业务日写入一段 ACTIVE 的 CAMPAIGN 人员占用。"""
    start = datetime.combine(day, time(hour, 0), tzinfo=TZ).astimezone(UTC)
    end = datetime.combine(day, time(hour + hours, 0), tzinfo=TZ).astimezone(UTC)
    r = ResourceReservation(
        resource_user_id=user.id,
        start_at=start,
        end_at=end,
        occupied_range=func.tstzrange(start, end, "[)"),
        source_type="CAMPAIGN",
        status="ACTIVE",
        merchant_id=None,
        source_object_type="CAMPAIGN",
        source_object_id=uuid4(),
        dataset_type="PRODUCTION",
        dataset_id=w["ds"],
    )
    session.add(r)
    session.flush()
    return r


def _mcp_ctx(session, actor):
    return McpContext.from_actor(
        actor, session=session, request_id="t25-req", session_id="t25-sess"
    )


# ============================ Q141 排班避开活动 ============================


def test_q141_schedule_suggest_avoids_campaign_occupancy(session: Session) -> None:
    w = _world(session)
    _mgr, actor = _manager(session, w)
    rep, shops = _rep_with_shops(session, w, 2)

    # 该业务员周一被一场地推活动占用 → 两家其负责商家当天都不能直接排入
    _campaign_reservation(session, w, rep, day=MONDAY)

    plan = ScheduleService(session).suggest_plan(
        actor, region_id=w["region"].id, week_start=MONDAY
    )
    editable_ids = {s.merchant.id for s in plan.editable}
    blocked_ids = {s.merchant.id for s in plan.priority_list}

    assert editable_ids == set(), "活动占用负责人的商家不得进入可编辑建议"
    assert blocked_ids == {s.id for s in shops}
    assert all(s.activity_blocked for s in plan.priority_list)
    assert all(
        any("活动" in reason for reason in s.reasons)
        for s in plan.priority_list
    )
    assert plan.degraded is True
    assert plan.degraded_reason and plan.degraded_reason.startswith("ACTIVITY_CONFLICT")


def test_q141_no_campaign_then_editable(session: Session) -> None:
    """对照组：无活动占用时商家正常进入可编辑建议。"""
    w = _world(session)
    _mgr, actor = _manager(session, w)
    _rep, shops = _rep_with_shops(session, w, 1)

    plan = ScheduleService(session).suggest_plan(
        actor, region_id=w["region"].id, week_start=MONDAY
    )
    assert {s.merchant.id for s in plan.editable} == {shops[0].id}
    assert plan.priority_list == []
    assert plan.degraded is False


# ====================== Q142 冲突只产草稿（建议只读） ======================


def test_q142_conflict_only_yields_editable_draft_and_writes_nothing(
    session: Session,
) -> None:
    from mcp.suggest_handlers import schedule_suggest

    w = _world(session)
    _mgr, actor = _manager(session, w)
    rep, shops = _rep_with_shops(session, w, 2)
    _campaign_reservation(session, w, rep, day=MONDAY)

    before_batches = session.scalar(
        select(func.count()).select_from(SchedulePlanBatch)
        .where(SchedulePlanBatch.dataset_id == w["ds"])
    )
    before_entries = session.scalar(
        select(func.count()).select_from(SchedulePlanEntry)
        .where(SchedulePlanEntry.dataset_id == w["ds"])
    )
    before_tasks = session.scalar(
        select(func.count()).select_from(VisitTask)
        .where(VisitTask.dataset_id == w["ds"])
    )
    before_resv = session.scalar(
        select(func.count()).select_from(ResourceReservation)
        .where(ResourceReservation.dataset_id == w["ds"])
    )

    ctx = _mcp_ctx(session, actor)
    payload = schedule_suggest(
        ctx, {"week_start": MONDAY.isoformat(), "region_id": str(w["region"].id)}
    )

    # 结果分流：活动占用商家在 priority_list（可编辑草稿性质），editable 为空
    assert payload["auto_apply"] is False
    assert payload["read_only"] is True
    assert payload["suggestion"]["editable"] == []
    assert len(payload["suggestion"]["priority_list"]) == 2

    # 建议全程不写任何排班/任务/占用（唯一新增的占用是夹具里的 CAMPAIGN）
    session.expire_all()
    assert session.scalar(
        select(func.count()).select_from(SchedulePlanBatch)
        .where(SchedulePlanBatch.dataset_id == w["ds"])
    ) == before_batches
    assert session.scalar(
        select(func.count()).select_from(SchedulePlanEntry)
        .where(SchedulePlanEntry.dataset_id == w["ds"])
    ) == before_entries
    assert session.scalar(
        select(func.count()).select_from(VisitTask)
        .where(VisitTask.dataset_id == w["ds"])
    ) == before_tasks
    assert session.scalar(
        select(func.count()).select_from(ResourceReservation)
        .where(ResourceReservation.dataset_id == w["ds"])
    ) == before_resv  # 建议不得新增 SCHEDULE 占用


# ============================ Q143 库存台账接入 ============================


def _stock_entry(session: Session, w, sku, boxes: str, actor) -> None:
    """造一条采购收货入库流水（T32 收发存台账）。"""
    from app.models import BeverageStockEntry

    session.add(
        BeverageStockEntry(
            sku_id=sku.id,
            change_cases=Decimal(boxes),
            source_type="PURCHASE_RECEIVE",
            source_id=uuid4(),
            biz_date=MONDAY,
            note="测试入库",
            registered_by=actor.user_id,
            dataset_type="PRODUCTION",
            dataset_id=w["ds"],
        )
    )
    session.flush()


def test_procurement_suggestion_uses_inventory_ledger(session: Session) -> None:
    """库存取真实台账：无流水即 0，有入库则扣减建议量。"""
    w = _world(session)
    sku = _sku(session, w, "143")
    _confirmed_replenishment(session, w, sku, "10")
    _mgr, actor = _manager(session, w, "t25-mgr-143")
    svc = ProcurementService(session)

    lines = svc.build_suggestions(
        actor, brand_id=w["brand"].id, region_id=w["region"].id
    )
    assert len(lines) == 1
    line = lines[0]
    # 台账已接入：没有流水时现存量就是真实的 0，不再是"平台未接入"
    assert line.inventory_status == "AVAILABLE"
    assert line.inventory_available is True
    assert line.available_boxes == Decimal("0")
    assert line.suggested_purchase_boxes == Decimal("10")

    # 入库 4 箱 → 建议采购 = 10 − 4
    _stock_entry(session, w, sku, "4", actor)
    line = svc.build_suggestions(
        actor, brand_id=w["brand"].id, region_id=w["region"].id
    )[0]
    assert line.available_boxes == Decimal("4")
    assert line.suggested_purchase_boxes == Decimal("6")


def test_procurement_suggestion_diagnostics_reports_inventory(
    session: Session,
) -> None:
    """诊断口径同步带出系统库存，避免"有库存还被建议采购"无法解释。"""
    w = _world(session)
    sku = _sku(session, w, "143diag")
    _confirmed_replenishment(session, w, sku, "10")
    _mgr, actor = _manager(session, w, "t25-mgr-143diag")
    _stock_entry(session, w, sku, "3", actor)

    diag = ProcurementService(session).suggestion_diagnostics(
        actor, brand_id=w["brand"].id, region_id=w["region"].id
    )
    assert diag["available_inventory_boxes"] == Decimal("3")
    assert diag["suggested_purchase_boxes"] == Decimal("7")


def test_supplier_suggestion_does_not_expose_internal_stock(session: Session) -> None:
    """R18 延伸：厂商看采购建议时不下发内部库存余额，也不按库存扣减。"""
    from app.core.errors import PermissionDeniedError

    w = _world(session)
    sku = _sku(session, w, "stockpriv")
    _confirmed_replenishment(session, w, sku, "8")
    _supplier, _su, supplier_actor = _supplier_world(session, w, "t25-stockpriv")
    _stock_entry(session, w, sku, "5", supplier_actor)

    try:
        lines = ProcurementService(session).build_suggestions(
            supplier_actor, brand_id=w["brand"].id, region_id=w["region"].id
        )
    except PermissionDeniedError:
        return  # 直接拒绝访问同样满足"不泄露内部库存"
    for line in lines:
        assert line.inventory_available is False
        assert line.inventory_status == "UNAVAILABLE"
        assert line.available_boxes == Decimal("0")
        assert line.suggested_purchase_boxes == Decimal("8")


def test_q143_procurement_mcp_handler_is_read_only(session: Session) -> None:
    """Q143 适配层：MCP procurement.suggest 只读不下单，并带出库存余额。"""
    from mcp.suggest_handlers import procurement_suggest

    w = _world(session)
    sku = _sku(session, w, "143mcp")
    _confirmed_replenishment(session, w, sku, "6")
    _mgr, actor = _manager(session, w, "t25-mgr-143mcp")

    before = session.scalar(select(func.count()).select_from(PurchaseOrder))
    payload = procurement_suggest(_mcp_ctx(session, actor), {})
    after = session.scalar(select(func.count()).select_from(PurchaseOrder))

    assert payload["read_only"] is True
    assert payload["auto_apply"] is False
    assert payload["suggestion"]["inventory_status"] == "AVAILABLE"
    assert payload["suggestion"]["degraded"] is False
    assert payload["suggestion"]["degraded_reason"] is None
    assert payload["suggestion"]["lines"][0]["available_boxes"] == "0"
    assert len(payload["suggestion"]["lines"]) == 1
    assert after == before, "采购建议不得创建采购单"


# ======================== Q144 采购只含确认需求（含在途） ========================


def test_q144_procurement_only_confirmed_demand_and_deducts_in_transit(
    session: Session,
) -> None:
    w = _world(session)
    sku = _sku(session, w, "144")
    # 已确认未满足 12 箱
    _confirmed_replenishment(session, w, sku, "12")
    # 未确认 50 箱，绝不进口径
    _unconfirmed_replenishment(session, w, sku, "50")

    # 已有 4 箱在途采购（CONFIRMED），应从建议量扣减
    supplier, _su, _sa = _supplier_world(session, w, "t144sup")
    po = PurchaseOrder(
        code=f"PO-{uuid4().hex[:12]}",
        supplier_id=supplier.id,
        brand_id=w["brand"].id,
        region_id=w["region"].id,
        status=PurchaseOrderStatus.CONFIRMED.value,
        total_amount_fen=0,
        expected_date=date(2026, 12, 25),
        dataset_type="PRODUCTION",
        dataset_id=w["ds"],
    )
    session.add(po)
    session.flush()
    session.add(PurchaseOrderLine(
        order_id=po.id, sku_id=sku.id,
        quantity_cases=Decimal("4"), tax_inclusive_unit_price_fen=100,
        line_amount_fen=400,
        dataset_type="PRODUCTION", dataset_id=w["ds"],
    ))
    session.flush()

    _mgr, actor = _manager(session, w, "t25-mgr-144")
    lines = ProcurementService(session).build_suggestions(
        actor, brand_id=w["brand"].id, region_id=w["region"].id
    )
    assert len(lines) == 1, "未确认补货不得产生建议行"
    line = lines[0]
    assert line.confirmed_unmet_boxes == Decimal("12")
    assert line.open_purchase_boxes == Decimal("4")
    assert line.suggested_purchase_boxes == Decimal("8")  # 12 确认需求 − 4 在途


# ============================ Q145 指标同源 ============================


def test_q145_business_insight_evidence_metrics_service_same_source(
    session: Session,
) -> None:
    """经营解释证据值必须与直接调用 MetricsService 完全一致，LLM 不产出数值。"""
    w = _world(session)
    _mgr, actor = _manager(session, w, "t25-mgr-145")
    period = "2026-12"

    svc = BusinessInsightService(session)
    insight = svc.explain(actor, period=period, provider=MockAIProvider())

    # 证据覆盖全部指标，且每一项的值/口径/来源与 MetricsService 直算一致
    svc_metrics = MetricsService(session)
    by_key = {e.key: e for e in insight.evidence}
    for key in [
        "ACTIVE_MERCHANTS", "NEW_CONTRACTS", "CONTRACT_CANCEL_RATE",
        "WEEK_COVERAGE_RATE", "ON_TIME_VISIT_RATE", "DISPLAY_COMPLIANCE_RATE",
        "CONFIRMED_SALES_BOXES", "REPLENISHMENT_ON_TIME_RATE",
        "FREEZER_STOCK_DIFF", "MONTHLY_NET_PROFIT",
    ]:
        assert key in by_key
        direct = svc_metrics.compute(
            actor, key, scope=DataScope.PRODUCTION, period=period
        )
        ev = by_key[key]
        assert ev.current == direct.value
        assert ev.definition == direct.definition
        assert tuple(ev.source_refs) == tuple(direct.source_refs)

    # 库存域未接入 → 该指标证据缺失标注，不臆造数值
    stock = by_key["FREEZER_STOCK_DIFF"]
    assert stock.current is None and stock.delta is None
    assert stock.data_complete is False

    # LLM 语言层：存在结构化叙述，但不含任何事实数值（事实只在 evidence）
    assert insight.narrative is not None
    narrative_dump = insight.narrative.model_dump_json()
    for token in ("0", "1", "2", "3", "4", "5", "6", "7", "8", "9"):
        assert token not in narrative_dump, "LLM 叙述不得包含任何数值，数值只能来自证据"
    # 原因一律是假设
    assert insight.narrative.assumptions
    assert insight.to_dict()["narrative_is_assumption_only"] is True
    assert insight.metrics_source == "MetricsService"


# ============================ Q146 mock 可复现 ============================


def test_q146_mock_provider_reproducible_and_validated() -> None:
    facts = {"period": "2026-12", "direction": "UP", "data_complete": True,
             "metric_count": 10}
    p1, p2, p3 = MockAIProvider(seed=7), MockAIProvider(seed=7), MockAIProvider(seed=999)

    a = p1.narrate(kind="business_insight", facts=facts)
    b = p2.narrate(kind="business_insight", facts=facts)

    # 同输入同种子跨实例完全一致（可复现）
    assert a.model_dump() == b.model_dump()
    # 结构化输出通过 Pydantic 校验（类型即保证），字段受控
    assert isinstance(a, Narrative)
    assert a.confidence in ("LOW", "MEDIUM", "HIGH")
    assert a.headline and a.summary

    # 同输入不同种子允许不同模板（证明选择由确定性种子驱动，而非随机/时钟）
    c = p3.narrate(kind="business_insight", facts=facts)
    assert isinstance(c, Narrative)
    # 两次相同调用仍一致（无隐藏随机/时间漂移）
    assert c.model_dump() == p3.narrate(
        kind="business_insight", facts=facts
    ).model_dump()

    # 数据不完整时 mock 强制低置信
    incomplete = dict(facts, data_complete=False)
    lo = MockAIProvider(seed=7).narrate(kind="business_insight", facts=incomplete)
    assert lo.confidence == "LOW"


# ============================ Q147 非结构化输出降级 ============================


def test_q147_unstructured_output_degrades_to_evidence_only(
    session: Session,
) -> None:
    w = _world(session)
    _mgr, actor = _manager(session, w, "t25-mgr-147")

    insight = BusinessInsightService(session).explain(
        actor, period="2026-12", provider=MockAIProvider(), force_invalid=True
    )
    # 降级：不采纳任何模型文本，但确定性证据仍完整返回
    assert insight.degraded is True
    assert insight.narrative is None
    assert insight.degraded_reason and insight.degraded_reason.startswith(
        "AI_NARRATIVE_DEGRADED"
    )
    assert len(insight.evidence) == 10
    payload = insight.to_dict()
    assert payload["narrative"] is None
    assert all(e["definition"] for e in payload["evidence"])
