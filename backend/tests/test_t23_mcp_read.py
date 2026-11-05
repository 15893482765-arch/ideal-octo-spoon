"""T23 · MCP 只读查询工具（PRD §5.1、§5.3 A0 工具、§4.11）。

- Q128 业务员只读本人（merchant/contract/visit/freezer 行级）
- Q129 经理只读片区（merchant/contract/visit/freezer/补货/风险）
- Q130 厂商脱敏汇总与字段级裁剪
- Q131 财务指标同源（finance.profit_report / performance.preview 不重算、不落库）
- Q132 无权限与无数据区分
- Q133 业务记录引用链接仍受鉴权

被测：`mcp/read_handlers.py`、`mcp/envelope.py`，以及被引用的受控 Service。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from tests.test_t09_contract import _ctx, _user, _world

import mcp
from app.core.permissions import ActorContext
from app.models import (
    Contract,
    FreezerAsset,
    FreezerMovement,
    Merchant,
    MerchantAssignment,
    PayrollStatement,
    PerformanceMonthlySnapshot,
    ProcurementOrder,
    Region,
    ReplenishmentOrder,
    RiskItem,
    SalesConfirmation,
    Sku,
    UserRegionBrandAuth,
    VisitRecord,
    VisitTask,
)
from app.models.enums import RoleCode
from app.services.demo_data_guard import DataScope
from app.services.metrics_service import M_MONTHLY_NET_PROFIT, MetricsService
from app.services.performance_snapshot_service import PerformanceSnapshotService
from mcp.gateway import McpGateway
from mcp.identity import McpContext
from mcp.read_handlers import READ_HANDLERS

SHANGHAI = ZoneInfo("Asia/Shanghai")
MONTH = "2026-10"
WEEK_MONDAY = date(2026, 10, 12)


def _ts(day: date, hour: int = 9, minute: int = 0) -> datetime:
    return (
        datetime.combine(day, time.min, tzinfo=SHANGHAI)
        .replace(hour=hour, minute=minute)
        .astimezone(UTC)
    )


# ============================== 造数 ==============================


def _merchant(session: Session, w: dict, name: str, *, region: Region) -> Merchant:
    m = Merchant(
        code=f"M-{uuid4().hex[:8]}",
        name=name,
        status="ACTIVE",
        grade="A",
        region_id=region.id,
        brand_id=w["brand"].id,
        dataset_type="PRODUCTION",
        dataset_id=w["ds"],
    )
    session.add(m)
    session.flush()
    return m


def _assign(session: Session, merchant: Merchant, owner, ds: UUID) -> None:
    session.add(
        MerchantAssignment(
            merchant_id=merchant.id,
            owner_user_id=owner.id,
            assignment_type="PRIMARY",
            valid_range=func.tstzrange(_ts(date(2020, 1, 1)), None, "[)"),
            dataset_type="PRODUCTION",
            dataset_id=ds,
        )
    )


def _contract(session: Session, w: dict, merchant: Merchant, *, rep) -> Contract:
    c = Contract(
        merchant_id=merchant.id,
        contract_no=f"C-{uuid4().hex[:10]}",
        title="陈列合作",
        party_name="商家",
        status="EFFECTIVE",
        signed_at=date(2026, 3, 1),
        effective_from=date(2026, 1, 1),
        expire_at=date(2026, 12, 31),
        display_threshold_pct=50,
        current_version=1,
        sales_rep_user_id=rep.id,
        dataset_type="PRODUCTION",
        dataset_id=w["ds"],
    )
    session.add(c)
    session.flush()
    return c


def _task(session: Session, w: dict, merchant: Merchant, user, *, planned: date) -> VisitTask:
    t = VisitTask(
        merchant_id=merchant.id,
        assigned_user_id=user.id,
        planned_date=planned,
        priority=2,
        source="WEEKLY_SCHEDULE",
        status="PENDING",
        dataset_type="PRODUCTION",
        dataset_id=w["ds"],
    )
    session.add(t)
    session.flush()
    return t


def _visit_record(
    session: Session, w: dict, task: VisitTask, merchant: Merchant, user, *,
    submitted_at: datetime,
) -> VisitRecord:
    v = VisitRecord(
        visit_task_id=task.id,
        merchant_id=merchant.id,
        user_id=user.id,
        source="WEEKLY_SCHEDULE",
        status="RECOGNIZED",
        validity="VALID",
        is_official=True,
        submitted_at=submitted_at,
        checkin_at=submitted_at,
        display_pct=70,
        display_met=True,
        photo_count=1,
        conclusion="NORMAL",
        freezer_status="NORMAL",
        dataset_type="PRODUCTION",
        dataset_id=w["ds"],
    )
    session.add(v)
    session.flush()
    return v


def _sku(session: Session, w: dict) -> Sku:
    s = Sku(
        code=f"SKU-{uuid4().hex[:8]}",
        name="500ml 冰红茶",
        brand_id=w["brand"].id,
        suggested_price_fen=300,
        case_spec=Decimal("24"),
        dataset_type="PRODUCTION",
        dataset_id=w["ds"],
    )
    session.add(s)
    session.flush()
    return s


def _sale(
    session: Session, w: dict, merchant: Merchant, sku: Sku, *, amount_fen: int,
    boxes: str, confirmer,
) -> None:
    session.add(
        SalesConfirmation(
            merchant_id=merchant.id,
            sku_id=sku.id,
            biz_date=date(2026, 10, 13),
            confirmed_boxes=Decimal(boxes),
            amount_fen=amount_fen,
            confirmer_id=confirmer.id,
            dataset_type="PRODUCTION",
            dataset_id=w["ds"],
        )
    )
    session.flush()


def _replenishment(session: Session, w: dict, merchant: Merchant, sku: Sku, *, boxes: str):
    order = ReplenishmentOrder(
        code=f"R-{uuid4().hex[:8]}",
        merchant_id=merchant.id,
        sku_id=sku.id,
        requested_boxes=Decimal(boxes),
        committed_boxes=Decimal(boxes),
        fulfilled_boxes=Decimal("0"),
        request_date=date(2026, 10, 10),
        promised_delivery_date=date(2026, 10, 20),
        status="CONFIRMED",
        dataset_type="PRODUCTION",
        dataset_id=w["ds"],
    )
    session.add(order)
    session.flush()
    return order


def _setup(session: Session) -> dict:
    w = _world(session)
    ds = w["ds"]
    region2 = Region(
        code=f"reg2-{uuid4().hex[:8]}",
        name="华南",
        org_id=w["region"].org_id,
        dataset_type="PRODUCTION",
        dataset_id=ds,
    )
    session.add(region2)

    rep1 = _user(session, ds, RoleCode.SALES_REP, "t23-rep1")
    rep2 = _user(session, ds, RoleCode.SALES_REP, "t23-rep2")
    mgr = _user(session, ds, RoleCode.REGIONAL_MANAGER, "t23-mgr")
    sup = _user(session, ds, RoleCode.SUPERVISOR, "t23-sup")
    fin = _user(session, ds, RoleCode.FINANCE, "t23-fin")
    supplier = _user(session, ds, RoleCode.SUPPLIER, "t23-sup1")
    for user in (mgr, rep1, rep2, supplier):
        session.add(
            UserRegionBrandAuth(
                user_id=user.id, region_id=w["region"].id, brand_id=w["brand"].id
            )
        )
    session.flush()

    m1 = _merchant(session, w, "华东一店", region=w["region"])
    m2 = _merchant(session, w, "华东二店", region=w["region"])
    m3 = _merchant(session, w, "华南三店", region=region2)
    _assign(session, m1, rep1, ds)
    _assign(session, m2, rep2, ds)
    _assign(session, m3, rep2, ds)

    c1 = _contract(session, w, m1, rep=rep1)
    c2 = _contract(session, w, m2, rep=rep2)
    c3 = _contract(session, w, m3, rep=rep2)

    t1 = _task(session, w, m1, rep1, planned=WEEK_MONDAY)
    _task(session, w, m3, rep2, planned=WEEK_MONDAY)
    record = _visit_record(
        session, w, t1, m1, rep1, submitted_at=_ts(WEEK_MONDAY, 9, 30)
    )

    asset = FreezerAsset(
        asset_code=f"F-{uuid4().hex[:8]}",
        brand_id=w["brand"].id,
        model="SC-500",
        purchase_date=date(2026, 1, 1),
        purchase_price_fen=80000,
        deposit_fen=20000,
        status="DEPLOYED",
        current_merchant_id=m1.id,
        responsible_user_id=rep1.id,
        dataset_type="PRODUCTION",
        dataset_id=ds,
    )
    session.add(asset)
    session.flush()
    session.add(
        FreezerMovement(
            asset_id=asset.id,
            movement_type="DEPLOY",
            amount_fen=0,
            occurred_at=_ts(date(2026, 10, 5)),
            operator_id=rep1.id,
        )
    )
    session.add_all(
        [
            RiskItem(
                object_type="MERCHANT", object_id=m1.id, rule_code="RK-1",
                period="2026-W41", status="OPEN", severity="HIGH",
                first_seen_at=_ts(date(2026, 10, 6)),
                last_seen_at=_ts(date(2026, 10, 13)),
                dataset_type="PRODUCTION", dataset_id=ds,
            ),
            RiskItem(
                object_type="MERCHANT", object_id=m3.id, rule_code="RK-1",
                period="2026-W41", status="OPEN", severity="HIGH",
                first_seen_at=_ts(date(2026, 10, 6)),
                last_seen_at=_ts(date(2026, 10, 13)),
                dataset_type="PRODUCTION", dataset_id=ds,
            ),
        ]
    )
    session.flush()
    return {
        "w": w, "region2": region2, "rep1": rep1, "rep2": rep2, "mgr": mgr,
        "sup": sup, "fin": fin, "supplier": supplier,
        "m1": m1, "m2": m2, "m3": m3, "c1": c1, "c2": c2, "c3": c3,
        "t1": t1, "record": record, "asset": asset,
        "rep1_a": _ctx(rep1, RoleCode.SALES_REP.value, frozenset({w["region"].id})),
        "rep2_a": _ctx(rep2, RoleCode.SALES_REP.value, frozenset({w["region"].id})),
        "mgr_a": _ctx(mgr, RoleCode.REGIONAL_MANAGER.value, frozenset({w["region"].id})),
        "sup_a": _ctx(sup, RoleCode.SUPERVISOR.value, frozenset({w["region"].id})),
        "fin_a": _ctx(fin, RoleCode.FINANCE.value, frozenset()),
        "supplier_a": _ctx(
            supplier, RoleCode.SUPPLIER.value, frozenset({w["region"].id})
        ),
    }


def _context(actor: ActorContext, session: Session | None = None) -> McpContext:
    return McpContext.from_actor(
        actor, session=session, request_id="t23", session_id="t23-session"
    )


def _client(session: Session, actor: ActorContext) -> mcp.MockMcpClient:
    registry = mcp.bind_read_handlers(mcp.build_default_registry())
    gateway = McpGateway(registry)
    context = McpContext.from_actor(
        actor, session=session, request_id="t23", session_id="t23-session"
    )
    return mcp.MockMcpClient(gateway, context)


# ============================== Q128 ==============================


def test_q128_sales_rep_reads_only_own(session: Session) -> None:
    """Q128：业务员只读本人——行级裁剪落在受控 Service，适配器不重算。"""
    c = _setup(session)
    client = _client(session, c["rep1_a"])

    # 绑定完整：目录内 15 个只读工具全部有 Handler（含强校验；14 个业务工具 + knowledge.search）
    registry = mcp.bind_read_handlers(mcp.build_default_registry())
    declared = {s.name for s in registry.specs() if s.kind is mcp.ToolKind.READ}
    assert declared == set(READ_HANDLERS)
    assert len(READ_HANDLERS) == 15

    search = client.call("merchant.search")["result"]
    assert search["result_state"] == "OK"
    assert [i["id"] for i in search["items"]] == [str(c["m1"].id)]
    assert search["scope"]["row_scope"] == "self"
    assert search["scope"]["role"] == RoleCode.SALES_REP.value
    assert search["scope"]["data_scope"] == "PRODUCTION"
    assert search["definition"] and search["as_of"] and search["as_of_display"]
    assert search["links"] == [
        {
            "object_type": "MERCHANT",
            "object_id": str(c["m1"].id),
            "path": f"/api/merchants/{c['m1'].id}",
        }
    ]

    contracts = client.call("contract.search")["result"]
    assert [v["id"] for v in contracts["items"]] == [str(c["c1"].id)]

    due = client.call("visit.list_due", week_start=WEEK_MONDAY.isoformat())["result"]
    assert [t["id"] for t in due["items"]] == [str(c["t1"].id)]
    assert due["week_start"] == WEEK_MONDAY.isoformat()

    visit = client.call("visit.get", visit_id=str(c["record"].id))["result"]
    assert visit["record"]["id"] == str(c["record"].id)
    assert visit["record"]["validity"] == "VALID"

    freezer = client.call("freezer.search")["result"]
    assert [a["id"] for a in freezer["items"]] == [str(c["asset"].id)]

    history = client.call("freezer.get_history", asset_id=str(c["asset"].id))["result"]
    assert history["count"] == 1
    assert history["asset"]["id"] == str(c["asset"].id)

    # 业务员无权调用经理/财务工具（角色门禁在网关，不落到 Handler）
    with pytest.raises(mcp.McpRoleNotAllowedError):
        client.call("replenishment.aggregate")


# ============================== Q129 ==============================


def test_q129_manager_reads_only_own_region(session: Session) -> None:
    """Q129：经理只读本片区（华东），外片区商家/合同/任务/风险/补货均不可见。"""
    c = _setup(session)
    client = _client(session, c["mgr_a"])

    merchants = client.call("merchant.search")["result"]
    assert {i["id"] for i in merchants["items"]} == {str(c["m1"].id), str(c["m2"].id)}
    assert merchants["scope"]["row_scope"] == "region"

    contracts = client.call("contract.search")["result"]
    assert {v["id"] for v in contracts["items"]} == {str(c["c1"].id), str(c["c2"].id)}

    # 显式指定周起始：缺省按“本周”取值，会随运行日期漂移（与本用例固定夹具不一致）
    due = client.call("visit.list_due", week_start=WEEK_MONDAY.isoformat())["result"]
    assert [t["id"] for t in due["items"]] == [str(c["t1"].id)]

    freezers = client.call("freezer.search")["result"]
    assert [a["id"] for a in freezers["items"]] == [str(c["asset"].id)]

    risks = client.call("risk.list")["result"]
    assert {r["object_id"] for r in risks["items"]} == {str(c["m1"].id)}

    sku = _sku(session, c["w"])
    _replenishment(session, c["w"], c["m1"], sku, boxes="10")
    _replenishment(session, c["w"], c["m3"], sku, boxes="7")
    aggregate = client.call("replenishment.aggregate")["result"]
    assert aggregate["confirmed_order_count"] == 1
    assert Decimal(aggregate["confirmed_boxes_total"]) == Decimal("10")

    # 经理不可读外片区合同详情
    other = client.call("contract.get", contract_id=str(c["c3"].id))["result"]
    assert other["result_state"] == "NO_PERMISSION"
    assert other["reason_code"] == "CONTRACT_REGION_FORBIDDEN"

    # 督导（region 角色）同样按片区裁剪巡访（显式周起始，避免随运行日期漂移）
    sup_due = _client(session, c["sup_a"]).call(
        "visit.list_due", week_start=WEEK_MONDAY.isoformat()
    )["result"]
    assert [t["id"] for t in sup_due["items"]] == [str(c["t1"].id)]


# ============================== Q130 ==============================


def test_q130_supplier_redacted_summary(session: Session) -> None:
    """Q130：厂商 merchant.search 只返回脱敏汇总；merchant.get 无明细权限。"""
    c = _setup(session)
    client = _client(session, c["supplier_a"])

    summary = client.call("merchant.search")["result"]
    record = summary["record"]
    assert summary["result_state"] == "OK"
    assert record["merchant_count"] == 2  # 仅授权片区（华东）的两家
    assert record["region_count"] == 1
    assert record["masked"] is True
    # 汇总结果不含商家明细列表
    assert "items" not in summary
    # 不含任何商家标识 / 联系方式 / 坐标字段
    for forbidden in (
        "name", "code", "contact_phone", "latitude", "longitude",
        "primary_contact_name", "address",
    ):
        assert forbidden not in record

    detail = client.call("merchant.get", merchant_id=str(c["m1"].id))["result"]
    assert detail["result_state"] == "NO_PERMISSION"
    assert detail["reason_code"] == "MERCHANT_DETAIL_FORBIDDEN_FOR_SUPPLIER"


def test_q130_field_level_redaction_by_role(session: Session) -> None:
    """Q130（续）：字段级裁剪管道——厂商可见字段被裁剪，业务员不受影响。

    说明：厂商能真正读到明细的业务工具当前被 T03 能力矩阵挡住（见遗留 TODO），
    因此这里同时校验「适配器裁剪管道」（对厂商脱敏生效）与「接口层明确拒绝」。
    """
    from mcp.read_handlers import _mask

    c = _setup(session)
    payload = {
        "name": "华东一店",
        "contact_phone": "13800000000",
        "purchase_price_fen": 80000,
        "latitude": 31.2,
        "longitude": 121.4,
    }
    supplier_view = _mask(_context(c["supplier_a"]), dict(payload))
    assert supplier_view["purchase_price_fen"] is None
    assert supplier_view["latitude"] is None and supplier_view["longitude"] is None
    assert supplier_view["contact_phone"] is None

    rep_view = _mask(_context(c["rep1_a"]), dict(payload))
    assert rep_view["purchase_price_fen"] is None  # 业务员不见采购成本
    assert rep_view["contact_phone"] == "138****0000"  # 联系电话掩码
    assert rep_view["latitude"] == 31.2  # 巡访导航需要精确坐标

    mgr_view = _mask(_context(c["mgr_a"]), dict(payload))
    assert mgr_view["purchase_price_fen"] == 80000  # 经理可见成本（生成采购建议）

    # 厂商连冰柜台账能力都未授予 → 明确“无权限”，不静默返回空列表
    supplier_freezer = _client(session, c["supplier_a"]).call("freezer.search")["result"]
    assert supplier_freezer["result_state"] == "NO_PERMISSION"
    assert supplier_freezer["reason_code"] == "PERMISSION_DENIED"
    assert supplier_freezer["items"] == []

    # 财务当前不在商家行级范围内 → 同样明确拒绝而非返回空列表
    fin_detail = _client(session, c["fin_a"]).call(
        "merchant.get", merchant_id=str(c["m1"].id)
    )["result"]
    assert fin_detail["result_state"] == "NO_PERMISSION"
    assert fin_detail["items"] == []


# ============================== Q131 ==============================


def test_q131_finance_metric_same_source(session: Session) -> None:
    """Q131：finance.profit_report 与 MetricsService 同源；经理版脱敏但数值一致。"""
    c = _setup(session)
    w, fin_a = c["w"], c["fin_a"]
    sku = _sku(session, w)
    _sale(session, w, c["m1"], sku, amount_fen=100000, boxes="20", confirmer=c["fin"])
    session.add(
        ProcurementOrder(
            code=f"P-{uuid4().hex[:8]}",
            sku_id=sku.id,
            supplier_name="厂商",
            quantity_cases=Decimal("10"),
            unit_price_fen=1000,
            expected_date=date(2026, 10, 20),
            status="ORDERED",
            dataset_type="PRODUCTION",
            dataset_id=w["ds"],
        )
    )
    session.flush()

    report = _client(session, fin_a).call("finance.profit_report", period=MONTH)["result"]
    record = report["record"]
    expected = MetricsService(session).compute(
        fin_a, M_MONTHLY_NET_PROFIT, scope=DataScope.PRODUCTION, period=MONTH
    )
    assert record["net_profit_fen"] == str(expected.value)
    assert record["definition"] == expected.definition
    assert record["redacted"] is False
    assert report["as_of"] == expected.as_of.isoformat()

    direct = {
        r.key: (None if r.value is None else str(r.value))
        for r in MetricsService(session).all(
            fin_a, scope=DataScope.PRODUCTION, period=MONTH
        )
    }
    by_report = {
        row["key"]: (None if row["value"] is None else str(row["value"]))
        for row in record["metrics"]
    }
    assert by_report == direct

    mgr_record = _client(session, c["mgr_a"]).call(
        "finance.profit_report", period=MONTH
    )["result"]["record"]
    assert mgr_record["redacted"] is True
    assert "metrics" not in mgr_record
    assert mgr_record["net_profit_fen"] == str(expected.value)


def test_q131_performance_preview_readonly(session: Session) -> None:
    """Q131（续）：绩效预估只读——无规则时明确无记录，有规则时试算且不落快照。"""
    c = _setup(session)
    w = c["w"]
    sku = _sku(session, w)
    _sale(session, w, c["m1"], sku, amount_fen=100000, boxes="20", confirmer=c["fin"])

    # 未配置生效规则 → 明确“无记录”而非编造
    before = _client(session, c["mgr_a"]).call(
        "performance.preview", period=MONTH, subject_user_id=str(c["rep1"].id)
    )["result"]
    assert before["result_state"] == "NO_RECORDS"
    assert before["reason_code"] == "PERF_RULE_NOT_EFFECTIVE"

    # 经理配置 → 财务审核生效（职责分离）
    svc = PerformanceSnapshotService(session)
    rule = svc.create_rule_version(
        c["mgr_a"],
        params={
            "new_contract_award_fen": 10000,
            "valid_visit_award_fen": 2000,
            "sales_tiers": [[None, 1000]],
            "activity_award_fen": 5000,
        },
        effective_from=date(2026, 1, 1),
        name="T23 规则",
    )
    svc.review_rule_version(c["fin_a"], rule.id)

    preview = _client(session, c["mgr_a"]).call(
        "performance.preview", period=MONTH, subject_user_id=str(c["rep1"].id)
    )["result"]["record"]
    assert preview["preview"] is True
    assert preview["status"] == "ESTIMATED"
    # T35：绩效只算新签 + 活动；销量提成与巡访奖励恒 0，本月无新签 → 净额为 0
    assert preview["sales_commission_fen"] == 0
    assert preview["new_contract_fen"] == 0
    assert preview["visit_bonus_fen"] == 0
    assert preview["net_performance_fen"] == 0

    # 只读：预估不得落快照
    count = session.execute(
        select(func.count()).select_from(PerformanceMonthlySnapshot)
    ).scalar_one()
    assert count == 0

    # 业务员调用绩效工具：T03 能力矩阵未授予 performance 模块 → 明确「无权限」
    rep_call = _client(session, c["rep1_a"]).call(
        "performance.preview", period=MONTH, subject_user_id=str(c["rep2"].id)
    )["result"]
    assert rep_call["result_state"] == "NO_PERMISSION"
    assert rep_call["reason_code"] in ("PERMISSION_DENIED", "PERF_SCOPE_FORBIDDEN")

    # 财务（MODULE_PERFORMANCE）预估与经理读同一份试算，不落快照
    fin_preview = _client(session, c["fin_a"]).call(
        "performance.preview", period=MONTH, subject_user_id=str(c["rep1"].id)
    )["result"]["record"]
    assert fin_preview["net_performance_fen"] == preview["net_performance_fen"]
    assert session.execute(
        select(func.count()).select_from(PerformanceMonthlySnapshot)
    ).scalar_one() == 0


# ============================== Q132 ==============================


def test_q132_no_permission_vs_no_records(session: Session) -> None:
    """Q132：无权限与无记录必须区分，不能互相冒充。"""
    c = _setup(session)
    rep = _client(session, c["rep1_a"])

    denied = rep.call("merchant.get", merchant_id=str(c["m2"].id))["result"]
    assert denied["result_state"] == "NO_PERMISSION"
    assert denied["reason_code"] == "MERCHANT_NOT_ASSIGNED"
    assert "record" not in denied
    assert denied["items"] == []

    denied_c = rep.call("contract.get", contract_id=str(c["c2"].id))["result"]
    assert denied_c["result_state"] == "NO_PERMISSION"
    assert denied_c["reason_code"] == "CONTRACT_NOT_ASSIGNED"
    assert denied_c["links"] == [
        {
            "object_type": "CONTRACT",
            "object_id": str(c["c2"].id),
            "path": f"/api/contracts/{c['c2'].id}",
        }
    ]

    mgr = _client(session, c["mgr_a"])
    empty = mgr.call("merchant.search", keyword="不存在的商家")["result"]
    assert empty["result_state"] == "NO_RECORDS"
    assert empty["items"] == [] and empty["count"] == 0
    assert "reason_code" not in empty

    missing = mgr.call("merchant.get", merchant_id=str(uuid4()))["result"]
    assert missing["result_state"] == "NO_RECORDS"
    assert missing["reason_code"] == "MERCHANT_NOT_FOUND"

    assert denied["result_state"] != empty["result_state"]


# ============================== Q133 ==============================


def test_q133_links_are_reachable_and_still_authenticated(
    session: Session, client: TestClient
) -> None:
    """Q133：引用链接指向真实路由，未鉴权/失效令牌一律 401（仍受鉴权保护）。"""
    c = _setup(session)
    rep = _client(session, c["rep1_a"])

    links = []
    links += rep.call("merchant.search")["result"]["links"]
    links += rep.call("contract.search")["result"]["links"]
    links += rep.call(
        "visit.list_due", week_start=WEEK_MONDAY.isoformat()
    )["result"]["links"]
    assert links, "只读工具必须返回业务记录链接"
    for link in links:
        assert link["path"] and link["object_id"]
        assert link["path"].startswith("/api/")

    # 路由真实存在且要求鉴权：无令牌 401（路由不存在则为 404）
    for link in links:
        assert client.get(link["path"]).status_code == 401
        assert (
            client.get(
                link["path"], headers={"Authorization": "Bearer not-a-real-token"}
            ).status_code
            == 401
        )


def test_q133_links_do_not_bypass_row_scope(session: Session) -> None:
    """Q133（续）：链接只是回链，权限仍由目标接口二次校验（链接不构成授权）。"""
    c = _setup(session)
    contract_link = _client(session, c["rep2_a"]).call("contract.search")["result"][
        "links"
    ][0]
    assert contract_link["object_id"] in {str(c["c2"].id), str(c["c3"].id)}

    denied = _client(session, c["rep1_a"]).call(
        "contract.get", contract_id=contract_link["object_id"]
    )["result"]
    assert denied["result_state"] == "NO_PERMISSION"


# ============================== payroll.list ==============================


def _payroll_stmt(
    session: Session,
    w: dict,
    user,
    *,
    period: str,
    gross_fen: int = 530000,
    status: str = "PAID",
) -> PayrollStatement:
    year, month = int(period[:4]), int(period[5:])
    start = date(year, month, 1)
    end = (
        date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    ) - timedelta(days=1)
    st = PayrollStatement(
        statement_no=f"PY-{uuid4().hex[:12].upper()}",
        user_id=user.id,
        period_month=period,
        period_start=start,
        period_end=end,
        base_salary_fen=480000,
        performance_fen=50000,
        adjustment_fen=0,
        gross_pay_fen=gross_fen,
        correction_fen=0,
        status=status,
        dataset_type="PRODUCTION",
        dataset_id=w["ds"],
    )
    session.add(st)
    session.flush()
    return st


def test_payroll_list_rep_reads_only_own(session: Session) -> None:
    """非财务角色走本人自查口径：同数据集他人工资单一行都看不到。"""
    w = _world(session)
    rep = _user(session, w["ds"], RoleCode.SALES_REP, f"rep23p-{uuid4().hex[:4]}")
    other = _user(session, w["ds"], RoleCode.SALES_REP, f"rep23q-{uuid4().hex[:4]}")
    _payroll_stmt(session, w, rep, period="2026-09")
    _payroll_stmt(session, w, other, period="2026-09", gross_fen=999999)

    result = _client(session, _ctx(rep, RoleCode.SALES_REP.value, frozenset())).call(
        "payroll.list", period="2026-09"
    )["result"]

    assert result["result_state"] == "OK"
    assert result["scope"] == "self"
    assert [i["user_id"] for i in result["items"]] == [str(rep.id)]
    assert result["items"][0]["gross_pay_fen"] == 530000


def test_payroll_list_finance_reads_dataset(session: Session) -> None:
    """财务（payroll 能力）走全员口径：同月两人的工资单都可见。"""
    w = _world(session)
    fin = _user(session, w["ds"], RoleCode.FINANCE, f"fin23p-{uuid4().hex[:4]}")
    rep = _user(session, w["ds"], RoleCode.SALES_REP, f"rep23r-{uuid4().hex[:4]}")
    _payroll_stmt(session, w, fin, period="2026-09")
    _payroll_stmt(session, w, rep, period="2026-09")

    result = _client(session, _ctx(fin, RoleCode.FINANCE.value, frozenset())).call(
        "payroll.list", period="2026-09"
    )["result"]

    assert result["result_state"] == "OK"
    assert result["scope"] == "dataset"
    assert {i["user_id"] for i in result["items"]} == {str(fin.id), str(rep.id)}


def test_payroll_list_default_window_excludes_current_month(session: Session) -> None:
    """默认窗口 = 最近 6 个完整月（不含本月），与「近半年、本月不算」口径一致。"""
    w = _world(session)
    rep = _user(session, w["ds"], RoleCode.SALES_REP, f"rep23s-{uuid4().hex[:4]}")
    today = date.today()
    cursor = today.replace(day=1)
    expected: list[str] = []
    for _ in range(6):
        prev = cursor - timedelta(days=1)
        expected.append(f"{prev.year}-{prev.month:02d}")
        cursor = prev.replace(day=1)
    _payroll_stmt(session, w, rep, period=expected[0])  # 上月
    _payroll_stmt(session, w, rep, period=f"{today.year}-{today.month:02d}")  # 本月

    result = _client(session, _ctx(rep, RoleCode.SALES_REP.value, frozenset())).call(
        "payroll.list"
    )["result"]

    assert result["periods"] == expected
    assert f"{today.year}-{today.month:02d}" not in expected
    # 本月工资单即使在库也不出现在默认窗口
    assert [i["period_month"] for i in result["items"]] == [expected[0]]


def test_payroll_list_finance_filters_by_user_name(session: Session) -> None:
    """财务点名员工：user_name 精确过滤，只回该员工的工资单。"""
    w = _world(session)
    fin = _user(session, w["ds"], RoleCode.FINANCE, f"fin23n-{uuid4().hex[:4]}")
    target = _user(session, w["ds"], RoleCode.SALES_REP, "刘美玲")
    other = _user(session, w["ds"], RoleCode.SALES_REP, "张伟")
    _payroll_stmt(session, w, target, period="2026-09", gross_fen=750000)
    _payroll_stmt(session, w, other, period="2026-09", gross_fen=480000)

    result = _client(session, _ctx(fin, RoleCode.FINANCE.value, frozenset())).call(
        "payroll.list", period="2026-09", user_name="刘美玲"
    )["result"]

    assert result["result_state"] == "OK"
    assert result["scope"] == "dataset"
    assert [i["user_name"] for i in result["items"]] == ["刘美玲"]
    assert result["items"][0]["gross_pay_fen"] == 750000


def test_payroll_list_finance_filter_unknown_name_no_records(
    session: Session,
) -> None:
    """财务点名库内不存在的姓名：空结果，文案带姓名，不回退全员。"""
    w = _world(session)
    fin = _user(session, w["ds"], RoleCode.FINANCE, f"fin23o-{uuid4().hex[:4]}")
    someone = _user(session, w["ds"], RoleCode.SALES_REP, "刘美玲")
    _payroll_stmt(session, w, someone, period="2026-09")

    result = _client(session, _ctx(fin, RoleCode.FINANCE.value, frozenset())).call(
        "payroll.list", period="2026-09", user_name="不存在的人"
    )["result"]

    assert result["result_state"] == "NO_RECORDS"
    assert "不存在的人" in result["note"]


def test_payroll_list_rep_naming_other_gets_hint(session: Session) -> None:
    """非财务点名他人：不静默退回本人口径（防张冠李戴），明确提示仅可查本人。"""
    w = _world(session)
    rep = _user(session, w["ds"], RoleCode.SALES_REP, "王小花")
    other = _user(session, w["ds"], RoleCode.SALES_REP, "刘美玲")
    _payroll_stmt(session, w, rep, period="2026-09")
    _payroll_stmt(session, w, other, period="2026-09")

    result = _client(session, _ctx(rep, RoleCode.SALES_REP.value, frozenset())).call(
        "payroll.list", period="2026-09", user_name="刘美玲"
    )["result"]

    assert result["result_state"] == "NO_RECORDS"
    assert result["reason_code"] == "PAYROLL_SELF_ONLY"
    assert "本人" in result["note"]


def test_payroll_list_rep_naming_self_ok(session: Session) -> None:
    """非财务点名自己：与本人姓名一致，正常返回本人口径。"""
    w = _world(session)
    rep = _user(session, w["ds"], RoleCode.SALES_REP, "王小花")
    _payroll_stmt(session, w, rep, period="2026-09")

    result = _client(session, _ctx(rep, RoleCode.SALES_REP.value, frozenset())).call(
        "payroll.list", period="2026-09", user_name="王小花"
    )["result"]

    assert result["result_state"] == "OK"
    assert result["scope"] == "self"
    assert [i["user_id"] for i in result["items"]] == [str(rep.id)]
