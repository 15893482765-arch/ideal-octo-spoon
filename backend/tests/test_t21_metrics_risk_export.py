"""T21 指标、风险、报表与导出（PRD §1.5 / §4.10 / §4.11 / §5.3 / §9.4）。

Q115 十项指标已知数据；Q116 口径非空；Q117 demo 隔离；Q118 取消率分母边界；
Q119 导出权限；Q120 链接过期；Q121 同源结果一致；
Q184 七类风险规则判定、幂等去重、经理待办联动、“数据不足”降级，且 RK-1 商家在
排班建议中处于优先位。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from tests.test_t09_contract import _ctx, _merchant, _user, _world

from app.core.errors import DomainError, PermissionDeniedError
from app.models import (
    Contract,
    Expense,
    FreezerAsset,
    FreezerMovement,
    MerchantGradeFrequency,
    PayrollStatement,
    ProcurementOrder,
    PurchaseOrder,
    PurchaseOrderLine,
    ReplenishmentOrder,
    Sku,
    Supplier,
    TaskItem,
    UserRegionBrandAuth,
    VisitRecord,
    VisitTask,
)
from app.models.enums import ExportStatus, RiskStatus, RoleCode
from app.services.demo_data_guard import DataScope
from app.services.export_service import ExportService
from app.services.metrics_service import (
    ALL_METRICS,
    M_ACTIVE_MERCHANTS,
    M_CONFIRMED_SALES_BOXES,
    M_CONTRACT_CANCEL_RATE,
    M_DISPLAY_COMPLIANCE_RATE,
    M_FREEZER_STOCK_DIFF,
    M_MONTHLY_NET_PROFIT,
    M_NEW_CONTRACTS,
    M_ON_TIME_VISIT_RATE,
    M_REPLENISHMENT_ON_TIME_RATE,
    M_WEEK_COVERAGE_RATE,
    MetricsService,
)
from app.services.risk_service import RiskService
from app.services.schedule_service import ScheduleService
from app.services.task_service import TASK_KIND_RISK_HANDLING

SHANGHAI = ZoneInfo("Asia/Shanghai")
MONTH = "2026-10"
WEEK = "2026-W42"
WEEK_MONDAY = date(2026, 10, 12)
AS_OF = date(2026, 10, 14)


# ================================ 造数 ================================


def _ts(day: date, hour: int = 9, minute: int = 0) -> datetime:
    return datetime.combine(day, datetime.min.time(), tzinfo=SHANGHAI).replace(
        hour=hour, minute=minute
    ).astimezone(UTC)


def _setup(session: Session, *, with_manager: bool = True) -> dict:
    w = _world(session)
    mgr = _user(session, w["ds"], RoleCode.REGIONAL_MANAGER,
                f"mgr21-{uuid4().hex[:4]}")
    fin = _user(session, w["ds"], RoleCode.FINANCE, f"fin21-{uuid4().hex[:4]}")
    rep = _user(session, w["ds"], RoleCode.SALES_REP, f"rep21-{uuid4().hex[:4]}")
    if with_manager:
        session.add(UserRegionBrandAuth(
            user_id=mgr.id, region_id=w["region"].id, brand_id=w["brand"].id))
    session.flush()
    return {
        "w": w, "mgr": mgr, "fin": fin, "rep": rep,
        "mgr_a": _ctx(mgr, RoleCode.REGIONAL_MANAGER.value,
                      frozenset({w["region"].id})),
        "fin_a": _ctx(fin, RoleCode.FINANCE.value, frozenset()),
        "rep_a": _ctx(rep, RoleCode.SALES_REP.value, frozenset()),
    }


def _sku(session: Session, w: dict) -> Sku:
    s = Sku(code=f"SKU-{uuid4().hex[:8]}", name="500ml 冰红茶",
            brand_id=w["brand"].id, suggested_price_fen=300, case_spec=Decimal("24"),
            dataset_type="PRODUCTION", dataset_id=w["ds"])
    session.add(s)
    session.flush()
    return s


def _contract(session: Session, w: dict, merchant, *, status="EFFECTIVE",
              signed_at=None, effective_from=date(2026, 1, 1),
              expire_at=date(2026, 12, 31)) -> Contract:
    c = Contract(merchant_id=merchant.id, contract_no=f"C-{uuid4().hex[:10]}",
                 title="陈列合作", party_name="商家",
                 status=status, signed_at=signed_at, effective_from=effective_from,
                 expire_at=expire_at, display_threshold_pct=50, current_version=1,
                 dataset_type="PRODUCTION", dataset_id=w["ds"])
    session.add(c)
    session.flush()
    return c


def _task(session: Session, w: dict, merchant, user, *, planned: date,
          window: tuple[datetime, datetime] | None = None) -> VisitTask:
    t = VisitTask(
        merchant_id=merchant.id, assigned_user_id=user.id, planned_date=planned,
        window_start_at=window[0] if window else None,
        window_end_at=window[1] if window else None,
        priority=2, source="WEEKLY_SCHEDULE", status="PENDING",
        dataset_type="PRODUCTION", dataset_id=w["ds"])
    session.add(t)
    session.flush()
    return t


def _visit(session: Session, w: dict, task: VisitTask, merchant, user, *,
           submitted_at: datetime, status="RECOGNIZED", display_pct=None,
           display_met=None, source="WEEKLY_SCHEDULE", is_official=True,
           validity="VALID") -> VisitRecord:
    # 注：T12 迁移把 conclusion/freezer_status 的 server_default 误写为带引号的
    # 字符串字面量（库里默认值为 "'NORMAL'"），会触发 CHECK；此处按既有测试约定
    # 显式赋值（见 遗留 TODO）。
    v = VisitRecord(
        visit_task_id=task.id, merchant_id=merchant.id, user_id=user.id,
        source=source, status=status, validity=validity, is_official=is_official,
        submitted_at=submitted_at, checkin_at=submitted_at, display_pct=display_pct,
        display_met=display_met, photo_count=0,
        conclusion="NORMAL", freezer_status="NORMAL",
        dataset_type="PRODUCTION", dataset_id=w["ds"])
    session.add(v)
    session.flush()
    return v


def _sales(session: Session, w: dict, merchant, sku, user, *, biz_date: date,
           boxes: str, amount_fen: int) -> None:
    from app.models import SalesConfirmation

    session.add(SalesConfirmation(
        merchant_id=merchant.id, sku_id=sku.id, biz_date=biz_date,
        confirmed_boxes=Decimal(boxes), amount_fen=amount_fen, confirmer_id=user.id,
        dataset_type="PRODUCTION", dataset_id=w["ds"]))
    session.flush()


def _replenishment(session: Session, w: dict, merchant, sku, *, status="CONFIRMED",
                   committed="10", fulfilled="0", request_date=date(2026, 10, 10),
                   promised=date(2026, 10, 16), actual=None) -> ReplenishmentOrder:
    o = ReplenishmentOrder(
        code=f"R-{uuid4().hex[:8]}", merchant_id=merchant.id, sku_id=sku.id,
        requested_boxes=Decimal(committed), committed_boxes=Decimal(committed),
        fulfilled_boxes=Decimal(fulfilled), request_date=request_date,
        promised_delivery_date=promised, actual_delivery_date=actual, status=status,
        dataset_type="PRODUCTION", dataset_id=w["ds"])
    session.add(o)
    session.flush()
    return o


def _metric(svc: MetricsService, actor, key: str, period: str):
    return svc.compute(actor, key, scope=DataScope.PRODUCTION, period=period)


def _voucher(session: Session, w: dict, owner) -> UUID:
    from app.models import FileObject

    f = FileObject(
        object_key=f"PROOF_DOC/{uuid4().hex}/invoice-{uuid4().hex[:6]}.jpg",
        original_filename="invoice.jpg", content_type="image/jpeg",
        size_bytes=1024, sha256=uuid4().hex * 2, biz_type="PROOF_DOC",
        owner_user_id=owner.id, scan_status="SKIPPED",
        dataset_type="PRODUCTION", dataset_id=w["ds"])
    session.add(f)
    session.flush()
    return f.id


# ================================ Q115 ================================


def test_q115_ten_metrics_known_data(session: Session) -> None:
    c = _setup(session)
    w, mgr_a, rep = c["w"], c["mgr_a"], c["rep"]
    m1 = _merchant(session, w, "一号店")
    m2 = _merchant(session, w, "二号店")
    sku = _sku(session, w)
    _contract(session, w, m1, signed_at=date(2026, 10, 5))
    _contract(session, w, m2, signed_at=date(2026, 9, 15))

    # 巡访：t1 准时、t2 迟到
    d1, d2 = WEEK_MONDAY + timedelta(days=1), WEEK_MONDAY + timedelta(days=2)
    t1 = _task(session, w, m1, rep, planned=d1, window=(_ts(d1), _ts(d1, 10)))
    t2 = _task(session, w, m2, rep, planned=d2, window=(_ts(d2), _ts(d2, 10)))
    _visit(session, w, t1, m1, rep, submitted_at=_ts(d1, 9, 30),
           display_pct=60, display_met=True)
    # m2 的陈列检查落在 W42 之外（第二周），用于分离“周覆盖率”与“陈列达标率”口径
    _visit(session, w, t2, m2, rep, submitted_at=_ts(date(2026, 10, 20), 11, 0),
           display_pct=40, display_met=False)

    # 销量：12 + 8 = 20 箱，金额 200000 分
    _sales(session, w, m1, sku, c["fin"], biz_date=date(2026, 10, 13),
           boxes="12", amount_fen=120000)
    _sales(session, w, m1, sku, c["fin"], biz_date=date(2026, 10, 14),
           boxes="8", amount_fen=80000)

    # 补货：单1 按时满足 10，单2 未满足 5
    _replenishment(session, w, m1, sku, status="FULFILLED", committed="10",
                   fulfilled="10", promised=date(2026, 10, 16),
                   actual=date(2026, 10, 15))
    _replenishment(session, w, m1, sku, status="CONFIRMED", committed="5",
                   request_date=date(2026, 10, 12), promised=date(2026, 10, 30))

    # 采购成本 20×1000 = 20000 分（T33 口径：T18 采购单当月收货才计入，
    # 旧 procurement_orders 表已废弃不再进损益）
    supplier = Supplier(code=f"S-{uuid4().hex[:8]}", name="厂商",
                        dataset_type="PRODUCTION", dataset_id=w["ds"])
    session.add(supplier)
    session.flush()
    po = PurchaseOrder(
        code=f"PO-{uuid4().hex[:8]}", supplier_id=supplier.id,
        brand_id=w["brand"].id, region_id=w["region"].id,
        expected_date=date(2026, 10, 20), status="RECEIVED",
        received_at=date(2026, 10, 20),
        dataset_type="PRODUCTION", dataset_id=w["ds"])
    session.add(po)
    session.flush()
    session.add(PurchaseOrderLine(
        order_id=po.id, sku_id=sku.id, quantity_cases=Decimal("20"),
        tax_inclusive_unit_price_fen=1000, line_amount_fen=20000,
        dataset_type="PRODUCTION", dataset_id=w["ds"]))
    # 冰柜购置 80000 分
    asset = FreezerAsset(asset_code=f"F-{uuid4().hex[:8]}", brand_id=w["brand"].id,
                         model="SC-500", purchase_date=date(2026, 1, 1),
                         purchase_price_fen=80000, status="IN_STOCK",
                         dataset_type="PRODUCTION", dataset_id=w["ds"])
    session.add(asset)
    session.flush()
    session.add(FreezerMovement(asset_id=asset.id, movement_type="PURCHASE",
                                amount_fen=80000, occurred_at=_ts(date(2026, 10, 10)),
                                operator_id=c["fin"].id))
    # 费用：活动 30000 + 其他 5000（均已审批）
    for etype, amt in (("ACTIVITY", 30000), ("OTHER", 5000)):
        session.add(Expense(
            expense_no=f"E-{uuid4().hex[:8]}", type=etype, title=etype,
            amount_fen=amt, incurred_on=date(2026, 10, 8), period_month=MONTH,
            status="APPROVED", voucher_file_id=_voucher(session, w, c["fin"]),
            approved_by_id=c["fin"].id, approved_at=_ts(date(2026, 10, 8)),
            dataset_type="PRODUCTION", dataset_id=w["ds"]))
    # 已发放绩效 20000
    session.add(PayrollStatement(
        statement_no=f"PS-{uuid4().hex[:8]}", user_id=rep.id, period_month=MONTH,
        period_start=date(2026, 10, 1), period_end=date(2026, 10, 31),
        base_salary_fen=0, performance_fen=20000, adjustment_fen=0,
        gross_pay_fen=20000, status="PAID",
        dataset_type="PRODUCTION", dataset_id=w["ds"]))
    session.flush()

    svc = MetricsService(session)
    assert _metric(svc, mgr_a, M_ACTIVE_MERCHANTS, MONTH).value == 2
    assert _metric(svc, mgr_a, M_NEW_CONTRACTS, MONTH).value == 1
    assert _metric(svc, mgr_a, M_CONTRACT_CANCEL_RATE, MONTH).value == Decimal("0.0000")
    assert _metric(svc, mgr_a, M_WEEK_COVERAGE_RATE, WEEK).value == Decimal("0.5000")
    assert _metric(svc, mgr_a, M_ON_TIME_VISIT_RATE, WEEK).value == Decimal("0.5000")
    assert _metric(svc, mgr_a, M_DISPLAY_COMPLIANCE_RATE, MONTH).value == Decimal("0.5000")
    assert _metric(svc, mgr_a, M_CONFIRMED_SALES_BOXES, MONTH).value == Decimal("20.00")
    assert _metric(svc, mgr_a, M_REPLENISHMENT_ON_TIME_RATE, MONTH).value == Decimal("0.6667")
    freezer = _metric(svc, mgr_a, M_FREEZER_STOCK_DIFF, MONTH)
    assert freezer.value is None
    assert freezer.data_complete is False
    assert _metric(svc, mgr_a, M_MONTHLY_NET_PROFIT, MONTH).value == 45000


# ================================ Q116 ================================


def test_q116_metric_definition_nonempty(session: Session) -> None:
    c = _setup(session)
    results = MetricsService(session).all(
        c["mgr_a"], scope=DataScope.PRODUCTION, period=MONTH)
    assert {r.key for r in results} == set(ALL_METRICS)
    for r in results:
        assert r.definition and r.definition.strip()
        assert r.as_of is not None
        assert r.filters
        assert r.source_refs
        assert r.period == MONTH
        assert r.name
        payload = r.to_dict()
        assert payload["definition"] and payload["as_of"]


# ================================ Q117 ================================


def test_q117_demo_isolated_and_scope_required(session: Session) -> None:
    c = _setup(session)
    w, mgr_a = c["w"], c["mgr_a"]
    pm = _merchant(session, w, "生产商家")
    _contract(session, w, pm, signed_at=date(2026, 10, 5))

    # DEMO 数据域（独立 dataset_id）：不得进入正式口径
    demo_ds = uuid4()
    from app.models import Brand, Merchant

    demo_brand = Brand(code=f"db-{uuid4().hex[:8]}", name="演示品牌",
                       dataset_type="DEMO", dataset_id=demo_ds)
    session.add(demo_brand)
    session.flush()
    demo_m = Merchant(code=f"DM-{uuid4().hex[:8]}", name="演示商家", status="ACTIVE",
                      region_id=w["region"].id, brand_id=demo_brand.id,
                      dataset_type="DEMO", dataset_id=demo_ds)
    session.add(demo_m)
    session.flush()
    session.add(Contract(
        merchant_id=demo_m.id, contract_no=f"DC-{uuid4().hex[:8]}", title="演示合同",
        party_name="演示", status="EFFECTIVE", signed_at=date(2026, 10, 5),
        effective_from=date(2026, 1, 1), expire_at=date(2026, 12, 31),
        display_threshold_pct=50, current_version=1,
        dataset_type="DEMO", dataset_id=demo_ds))
    session.flush()

    svc = MetricsService(session)
    # 仅生产商家计入
    assert _metric(svc, mgr_a, M_ACTIVE_MERCHANTS, MONTH).value == 1
    assert _metric(svc, mgr_a, M_NEW_CONTRACTS, MONTH).value == 1

    # 缺省 scope 直接拒绝
    with pytest.raises(DomainError) as exc:
        svc.compute(mgr_a, M_ACTIVE_MERCHANTS, scope=None, period=MONTH)
    assert exc.value.code == "DATA_SCOPE_REQUIRED"
    # DEMO / ALL 口径拒绝
    for bad in (DataScope.DEMO, DataScope.ALL):
        with pytest.raises(DomainError) as exc:
            svc.compute(mgr_a, M_ACTIVE_MERCHANTS, scope=bad, period=MONTH)
        assert exc.value.code == "DATA_SCOPE_NOT_PRODUCTION"


# ================================ Q118 ================================


def test_q118_cancel_rate_boundary(session: Session) -> None:
    c = _setup(session)
    w, mgr_a = c["w"], c["mgr_a"]
    svc = MetricsService(session)
    # 无任何合同：分母为 0 → 取消率 0，且给出说明
    r = _metric(svc, mgr_a, M_CONTRACT_CANCEL_RATE, MONTH)
    assert r.value == Decimal("0.0000")
    assert r.data_complete is True
    assert r.note

    # 期初有效 1 份，当期无新生效、无终止 → 分母 1，取消率 0
    m1 = _merchant(session, w, "边界店")
    _contract(session, w, m1, signed_at=date(2026, 3, 1))
    assert _metric(svc, mgr_a, M_CONTRACT_CANCEL_RATE, MONTH).value == Decimal("0.0000")

    # 当期新生效 1 且当期终止 1 → 分母 = 0(期初) + 1 = 1 → 取消率 1.0
    m2 = _merchant(session, w, "当月生灭店")
    c2 = _contract(session, w, m2, signed_at=date(2026, 10, 6))
    c2.status = "TERMINATED"
    from app.models import ContractVersion

    session.add(ContractVersion(
        contract_id=c2.id, version=2, change_type="TERMINATE", title="陈列合作",
        party_name="商家", effective_from=date(2026, 10, 1),
        expire_at=date(2026, 10, 20), created_at=_ts(date(2026, 10, 9)),
        dataset_type="PRODUCTION", dataset_id=w["ds"]))
    session.flush()
    # 期初(as of 2026-09-30)：c1 有效(1)；当期新生效：c2(1)；当期终止：c2(1)
    assert _metric(svc, mgr_a, M_CONTRACT_CANCEL_RATE, MONTH).value == Decimal("0.5000")


# ================================ Q119 ================================


def test_q119_export_permission(session: Session) -> None:
    c = _setup(session)
    svc = ExportService(session)
    # 业务员无权导出
    with pytest.raises(PermissionDeniedError) as exc:
        svc.request_export(c["rep_a"], report_key="MERCHANTS", fmt="CSV",
                           scope=DataScope.PRODUCTION)
    assert exc.value.code == "EXPORT_FORBIDDEN"
    # 缺省 scope 直接拒绝
    with pytest.raises(DomainError) as exc:
        svc.request_export(c["mgr_a"], report_key="MERCHANTS", fmt="CSV", scope=None)
    assert exc.value.code == "DATA_SCOPE_REQUIRED"
    # 经理可发起
    job = svc.request_export(c["mgr_a"], report_key="MERCHANTS", fmt="CSV",
                             scope=DataScope.PRODUCTION)
    assert job.status == ExportStatus.QUEUED.value
    assert job.scope == "PRODUCTION"

    # 另一业务员（非请求人、非全局）不可下载
    svc.run_job(job.id)
    with pytest.raises(PermissionDeniedError):
        svc.get_download(c["rep_a"], job.id)


# ================================ Q120 ================================


def test_q120_export_link_expiry(session: Session) -> None:
    c = _setup(session)
    w = c["w"]
    _merchant(session, w, "导出店")
    svc = ExportService(session)
    job = svc.request_export(c["mgr_a"], report_key="MERCHANTS", fmt="CSV",
                             scope=DataScope.PRODUCTION)
    # 未完成不可下载
    with pytest.raises(DomainError) as exc:
        svc.get_download(c["mgr_a"], job.id)
    assert exc.value.code == "EXPORT_NOT_READY"

    svc.run_job(job.id)
    assert job.status == ExportStatus.SUCCEEDED.value
    assert job.expires_at is not None
    link = svc.get_download(c["mgr_a"], job.id)
    assert link["object_key"].startswith("EXPORT/")
    assert link["ttl_seconds"] > 0

    # 人为过期后拒绝
    job.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    session.flush()
    with pytest.raises(DomainError) as exc:
        svc.get_download(c["mgr_a"], job.id)
    assert exc.value.code == "EXPORT_LINK_EXPIRED"


# ================================ Q121 ================================


def test_q121_same_source_consistency(session: Session) -> None:
    c = _setup(session)
    w, mgr_a = c["w"], c["mgr_a"]
    m1 = _merchant(session, w, "同源店")
    _contract(session, w, m1, signed_at=date(2026, 10, 5))
    svc_metrics = MetricsService(session)
    # 报表单元格为空串表示“数据不足”，与导出一致
    direct = {
        r.key: ("" if r.value is None else str(r.value))
        for r in svc_metrics.all(mgr_a, scope=DataScope.PRODUCTION, period=MONTH)
    }

    export_svc = ExportService(session)
    # 报表聚合（同步接口）与指标结果一致
    report = export_svc.report_view(
        mgr_a, report_key="METRICS", scope=DataScope.PRODUCTION, period=MONTH)
    by_export = {row["key"]: row["value"] for row in report["rows"]}
    assert by_export == direct
    # 指标与定义文案一致
    for row in report["rows"]:
        assert row["definition"]

    # 导出文件内容与指标结果一致（同源）
    job = export_svc.request_export(
        mgr_a, report_key="METRICS", fmt="CSV",
        filters={"period": MONTH}, scope=DataScope.PRODUCTION)
    export_svc.run_job(job.id)
    text = export_svc.read_object(job).decode("utf-8-sig")
    for key, value in direct.items():
        assert key in text
        if value is not None:
            assert value in text


def test_q121_report_dimension_filters(session: Session) -> None:
    """报表多维筛选：片区/品牌/SKU/业务员（PRD §4.10）。"""
    from sqlalchemy import func

    from app.models import MerchantAssignment

    c = _setup(session)
    w, mgr_a = c["w"], c["mgr_a"]
    rep2 = _user(session, w["ds"], RoleCode.SALES_REP, f"rep21b-{uuid4().hex[:4]}")
    m_a = _merchant(session, w, "业务员A店")
    m_b = _merchant(session, w, "业务员B店")
    for merchant, owner in ((m_a, c["rep"]), (m_b, rep2)):
        session.add(MerchantAssignment(
            merchant_id=merchant.id, owner_user_id=owner.id,
            assignment_type="PRIMARY",
            valid_range=func.tstzrange(_ts(date(2026, 1, 1)), None, "[)"),
            dataset_type="PRODUCTION", dataset_id=w["ds"]))
    session.flush()

    svc = ExportService(session)
    rows = svc.report_view(
        mgr_a, report_key="MERCHANTS", scope=DataScope.PRODUCTION,
        sales_rep_id=c["rep"].id)["rows"]
    assert [r["id"] for r in rows] == [str(m_a.id)]
    # 无业务员筛选 → 全部
    all_rows = svc.report_view(
        mgr_a, report_key="MERCHANTS", scope=DataScope.PRODUCTION)["rows"]
    assert {r["id"] for r in all_rows} == {str(m_a.id), str(m_b.id)}


# ================================ Q184 ================================


def test_q184_risk_rules_idempotent_todo_and_degrade(session: Session) -> None:
    c = _setup(session)
    w, mgr, mgr_a, rep, fin = c["w"], c["mgr"], c["mgr_a"], c["rep"], c["fin"]
    sku = _sku(session, w)
    wk = WEEK_MONDAY - timedelta(days=7)

    # RK-1：无有效巡访
    m_no_visit = _merchant(session, w, "从未巡访")
    # 有近期巡访（不触发 RK-1）+ 逾期补货（RK-5）+ 临期合同（RK-6）
    m_busy = _merchant(session, w, "忙碌店")
    tb = _task(session, w, m_busy, rep, planned=AS_OF - timedelta(days=1))
    _visit(session, w, tb, m_busy, rep, submitted_at=_ts(AS_OF - timedelta(days=1)),
           display_pct=80, display_met=True)
    _replenishment(session, w, m_busy, sku, status="CONFIRMED", committed="10",
                   request_date=date(2026, 10, 1), promised=date(2026, 10, 5))
    expiring = _contract(session, w, m_busy, signed_at=date(2026, 1, 5),
                         effective_from=date(2026, 1, 1),
                         expire_at=AS_OF + timedelta(days=10))

    # RK-2：连续两次定位异常
    m_abn = _merchant(session, w, "定位异常店")
    for i in (2, 1):
        t = _task(session, w, m_abn, rep, planned=AS_OF - timedelta(days=i))
        _visit(session, w, t, m_abn, rep, submitted_at=_ts(AS_OF - timedelta(days=i)),
               status="ABNORMAL_PENDING", validity="ABNORMAL_PENDING")

    # RK-3：连续两次陈列不达标
    m_disp = _merchant(session, w, "陈列不达标店")
    for i in (2, 1):
        t = _task(session, w, m_disp, rep, planned=AS_OF - timedelta(days=i))
        _visit(session, w, t, m_disp, rep, submitted_at=_ts(AS_OF - timedelta(days=i)),
               display_pct=30, display_met=False)
    # 无陈列检查（数据不足）
    m_no_disp = _merchant(session, w, "无陈列数据店")
    tnd = _task(session, w, m_no_disp, rep, planned=AS_OF - timedelta(days=1))
    _visit(session, w, tnd, m_no_disp, rep,
           submitted_at=_ts(AS_OF - timedelta(days=1)))

    # RK-4：有过确认销量但近两周为 0
    m_zero = _merchant(session, w, "两周零销量店")
    _sales(session, w, m_zero, sku, fin, biz_date=wk - timedelta(days=14), boxes="5",
           amount_fen=10000)
    # 从未有过确认销量（数据不足）
    _merchant(session, w, "未确认销量店")

    session.flush()

    risk = RiskService(session)
    first = risk.scan(mgr_a, scope=DataScope.PRODUCTION, as_of=AS_OF)
    codes = {(r.rule_code, r.object_id) for r in first.items}
    assert ("RK-1", m_no_visit.id) in codes
    assert ("RK-2", m_abn.id) in codes
    assert ("RK-3", m_disp.id) in codes
    assert ("RK-4", m_zero.id) in codes
    assert any(rc == "RK-5" for rc, _ in codes)
    assert ("RK-6", expiring.id) in codes

    # 数据不足降级：不得虚构风险
    insufficient = {(i.rule_code, i.object_id) for i in first.insufficient}
    assert ("RK-3", str(m_no_disp.id)) in insufficient
    assert any(x.rule_code == "RK-4" and "暂不判定" in x.reason
               for x in first.insufficient)

    # 新风险自动进入片区经理待办
    todos = session.execute(select(TaskItem).where(
        TaskItem.task_kind == TASK_KIND_RISK_HANDLING,
        TaskItem.assignee_id == mgr.id)).scalars().all()
    assert todos, "未生成风险处理待办"
    assert len(todos) == len(first.created)

    # 幂等：重复扫描不重复生成、不重复建待办
    before = len(first.items)
    second = risk.scan(mgr_a, scope=DataScope.PRODUCTION, as_of=AS_OF)
    assert second.created == []
    assert len(second.items) == before
    todos2 = session.execute(select(TaskItem).where(
        TaskItem.task_kind == TASK_KIND_RISK_HANDLING,
        TaskItem.assignee_id == mgr.id)).scalars().all()
    assert len(todos2) == len(todos)

    # 关闭必须填处理结果；关闭后记录保留且不重复生成
    rk1 = next(r for r in first.items if r.rule_code == "RK-1" and r.object_id == m_no_visit.id)
    with pytest.raises(DomainError) as exc:
        risk.close(mgr_a, rk1.id, resolution="   ")
    assert exc.value.code == "RISK_RESOLUTION_REQUIRED"

    risk.acknowledge(mgr_a, rk1.id)
    closed = risk.close(mgr_a, rk1.id, resolution="已安排本周巡访")
    assert closed.status == RiskStatus.CLOSED.value
    assert closed.resolution == "已安排本周巡访"
    third = risk.scan(mgr_a, scope=DataScope.PRODUCTION, as_of=AS_OF)
    assert third.created == []
    kept = session.get(type(rk1), rk1.id)
    assert kept is not None and kept.status == RiskStatus.CLOSED.value


def test_q184_rk1_pinned_in_schedule_advice(session: Session) -> None:
    c = _setup(session)
    w, mgr_a = c["w"], c["mgr_a"]
    m_risk = _merchant(session, w, "被标记店")
    m_ok = _merchant(session, w, "正常店")
    # 让 m_ok 有近期巡访，m_risk 无巡访 → 仅 m_risk 触发 RK-1
    t = _task(session, w, m_ok, c["rep"], planned=AS_OF - timedelta(days=1))
    _visit(session, w, t, m_ok, c["rep"], submitted_at=_ts(AS_OF - timedelta(days=1)))
    session.flush()

    risk = RiskService(session)
    risk.scan(mgr_a, scope=DataScope.PRODUCTION, as_of=AS_OF)

    schedule = ScheduleService(session, risk_provider=risk.risk_priority_provider)
    suggestions = schedule.suggest(
        mgr_a, region_id=w["region"].id, week_start=WEEK_MONDAY)
    assert suggestions
    assert suggestions[0].merchant.id == m_risk.id
    assert suggestions[0].risk_pinned is True


def test_q184_rk7_freezer_repair(session: Session) -> None:
    c = _setup(session)
    w, mgr_a = c["w"], c["mgr_a"]
    overdue = FreezerAsset(asset_code=f"F-{uuid4().hex[:8]}", brand_id=w["brand"].id,
                           model="SC-500", purchase_date=date(2026, 1, 1),
                           purchase_price_fen=50000, status="REPAIRING",
                           dataset_type="PRODUCTION", dataset_id=w["ds"])
    no_flow = FreezerAsset(asset_code=f"F-{uuid4().hex[:8]}", brand_id=w["brand"].id,
                           model="SC-500", purchase_date=date(2026, 1, 1),
                           purchase_price_fen=50000, status="REPAIRING",
                           dataset_type="PRODUCTION", dataset_id=w["ds"])
    session.add_all([overdue, no_flow])
    session.flush()
    session.add(FreezerMovement(
        asset_id=overdue.id, movement_type="REPAIR_START",
        occurred_at=datetime.now(UTC) - timedelta(hours=100),
        operator_id=c["fin"].id))
    session.flush()

    result = RiskService(session).scan(
        mgr_a, scope=DataScope.PRODUCTION, as_of=AS_OF)
    codes = {(r.rule_code, r.object_id) for r in result.items}
    assert ("RK-7", overdue.id) in codes
    # 无维修流水 → 数据不足，不虚构风险
    assert not any(r.object_id == no_flow.id for r in result.items)
    assert any(i.rule_code == "RK-7" and i.object_id == str(no_flow.id)
               for i in result.insufficient)


def test_q184_grade_frequency_config(session: Session) -> None:
    """RK-1 频次按等级配置生效（R35）。"""
    c = _setup(session)
    w, mgr_a, rep = c["w"], c["mgr_a"], c["rep"]
    m = _merchant(session, w, "A 级店")
    m.grade = "A"
    session.add(MerchantGradeFrequency(
        grade="A", frequency_days=30, start_date=date(2026, 1, 1)))
    t = _task(session, w, m, rep, planned=AS_OF - timedelta(days=10))
    _visit(session, w, t, m, rep, submitted_at=_ts(AS_OF - timedelta(days=10)))
    session.flush()

    result = RiskService(session).scan(mgr_a, scope=DataScope.PRODUCTION, as_of=AS_OF)
    # 10 天 < 30 天 → 不触发 RK-1
    assert not any(r.rule_code == "RK-1" and r.object_id == m.id for r in result.items)
