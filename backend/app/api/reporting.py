"""/api/reporting 路由：指标、风险、经营报表与异步导出（T21）。

Router 只鉴权/校验/装配；口径、状态机、权限与事务边界全部在 Service。
`scope` 为必填查询参数——缺省即 422 拒绝（PRD R34：不得依赖隐式默认）。
"""

from __future__ import annotations

# ruff: noqa: B008  FastAPI Depends()/Query() 需出现在默认值
from datetime import date
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.deps import current_actor, db_session
from app.core.permissions import ActorContext
from app.schemas.reporting import (
    CloseRiskRequest,
    ExportDownloadView,
    ExportJobView,
    ExportRequest,
    MetricView,
    ProfitTrendView,
    ReportRowsView,
    RiskScanResponse,
    RiskView,
)
from app.services.export_service import ExportService
from app.services.metrics_service import MetricsService
from app.services.risk_service import RiskService

router = APIRouter(prefix="/api/reporting", tags=["reporting"])


# ============================== 指标 ==============================

def _metric_view(result) -> MetricView:
    return MetricView(**result.to_dict())


@router.get("/metrics", response_model=list[MetricView])
def list_metrics(
    period: str = Query(..., description="统计周期 YYYY-MM 或 YYYY-Www"),
    scope: str = Query(..., description="必须显式 PRODUCTION"),
    session: Session = Depends(db_session),
    actor: ActorContext = Depends(current_actor),
):
    results = MetricsService(session).all(actor, scope=scope, period=period)
    return [_metric_view(r) for r in results]


@router.get("/profit-trend", response_model=ProfitTrendView)
def profit_trend(
    months: int = Query(6, ge=1, le=24),
    scope: str = Query(..., description="必须显式 PRODUCTION"),
    session: Session = Depends(db_session),
    actor: ActorContext = Depends(current_actor),
):
    """逐月收支汇总 + 累计净利润（T33）：口径与 MONTHLY_NET_PROFIT 一致。"""
    items = MetricsService(session).profit_trend(actor, scope=scope, months=months)
    return ProfitTrendView(items=items)


@router.get("/metrics/{metric_key}", response_model=MetricView)
def get_metric(
    metric_key: str,
    period: str = Query(...),
    scope: str = Query(...),
    session: Session = Depends(db_session),
    actor: ActorContext = Depends(current_actor),
):
    return _metric_view(
        MetricsService(session).compute(actor, metric_key, scope=scope, period=period)
    )


# ============================== 风险 ==============================

def _risk_view(item) -> RiskView:
    return RiskView(
        id=item.id, rule_code=item.rule_code, object_type=item.object_type,
        object_id=item.object_id, period=item.period, status=item.status,
        severity=item.severity, first_seen_at=item.first_seen_at,
        last_seen_at=item.last_seen_at, closed_at=item.closed_at,
        resolution=item.resolution, data_incomplete=item.data_incomplete,
    )


@router.post("/risks/scan", response_model=RiskScanResponse)
def scan_risks(
    as_of: date | None = Query(None),
    scope: str = Query(...),
    session: Session = Depends(db_session),
    actor: ActorContext = Depends(current_actor),
):
    result = RiskService(session).scan(actor, scope=scope, as_of=as_of)
    return RiskScanResponse(
        items=[_risk_view(i) for i in result.items],
        created=result.created,
        insufficient=[i.to_dict() for i in result.insufficient],
    )


@router.get("/risks", response_model=list[RiskView])
def list_risks(
    scope: str = Query(...),
    status: str | None = Query(None),
    rule_code: str | None = Query(None),
    session: Session = Depends(db_session),
    actor: ActorContext = Depends(current_actor),
):
    items = RiskService(session).list_risks(
        actor, scope=scope, status=status, rule_code=rule_code
    )
    return [_risk_view(i) for i in items]


@router.post("/risks/{risk_id}/acknowledge", response_model=RiskView)
def acknowledge_risk(
    risk_id: UUID,
    session: Session = Depends(db_session),
    actor: ActorContext = Depends(current_actor),
):
    return _risk_view(RiskService(session).acknowledge(actor, risk_id))


@router.post("/risks/{risk_id}/close", response_model=RiskView)
def close_risk(
    risk_id: UUID,
    body: CloseRiskRequest,
    session: Session = Depends(db_session),
    actor: ActorContext = Depends(current_actor),
):
    return _risk_view(
        RiskService(session).close(actor, risk_id, resolution=body.resolution)
    )


# ============================== 报表（聚合，避免前端多请求拼装）==============================

@router.get("/reports/{report_key}", response_model=ReportRowsView)
def get_report(
    report_key: str,
    period: str | None = Query(None),
    region_id: UUID | None = Query(None),
    brand_id: UUID | None = Query(None),
    sku_id: UUID | None = Query(None),
    sales_rep_id: UUID | None = Query(None),
    scope: str = Query(...),
    session: Session = Depends(db_session),
    actor: ActorContext = Depends(current_actor),
):
    rows = ExportService(session).report_view(
        actor, report_key=report_key, scope=scope, period=period,
        region_id=region_id, brand_id=brand_id, sku_id=sku_id,
        sales_rep_id=sales_rep_id,
    )
    return ReportRowsView(**rows)


# ============================== 导出 ==============================

def _job_view(job) -> ExportJobView:
    return ExportJobView(
        id=job.id, job_no=job.job_no, report_key=job.report_key,
        format=job.format, status=job.status, row_count=job.row_count,
        object_key=job.object_key, content_type=job.content_type,
        requested_by_id=job.requested_by_id, scope=job.scope,
        expires_at=job.expires_at, created_at=job.created_at,
    )


@router.post("/exports", response_model=ExportJobView, status_code=201)
def request_export(
    body: ExportRequest,
    scope: str = Query(...),
    session: Session = Depends(db_session),
    actor: ActorContext = Depends(current_actor),
):
    job = ExportService(session).request_export(
        actor, report_key=body.report_key, fmt=body.format,
        filters=body.filters, scope=scope,
    )
    return _job_view(job)


@router.post("/exports/{job_id}/run", response_model=ExportJobView)
def run_export(
    job_id: UUID,
    session: Session = Depends(db_session),
    actor: ActorContext = Depends(current_actor),
):
    """执行导出任务（worker/同步触发入口）；执行时重新校验 scope 与权限。"""
    ExportService(session).require_runner(actor, job_id)
    job = ExportService(session).run_job(job_id)
    return _job_view(job)


@router.get("/exports/{job_id}/download", response_model=ExportDownloadView)
def download_export(
    job_id: UUID,
    session: Session = Depends(db_session),
    actor: ActorContext = Depends(current_actor),
):
    return ExportDownloadView(**ExportService(session).get_download(actor, job_id))


@router.get("/exports", response_model=list[ExportJobView])
def list_exports(
    session: Session = Depends(db_session),
    actor: ActorContext = Depends(current_actor),
):
    return [_job_view(j) for j in ExportService(session).list_jobs(actor)]
