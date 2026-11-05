"""T21 指标 / 风险 / 报表 / 导出 API 出入参（Pydantic v2）。"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

# ============================== 指标 ==============================

class MetricView(BaseModel):
    key: str
    name: str
    value: Any | None = None
    unit: str
    definition: str
    as_of: datetime
    period: str
    filters: dict
    data_complete: bool
    note: str | None = None
    source_refs: list[str] = Field(default_factory=list)


class ProfitTrendItem(BaseModel):
    """逐月收支汇总行（分；T33 财务逐月累加视图）。"""

    period: str
    sales: int
    procurement: int
    freezer: int
    activity: int
    other_expense: int
    performance: int
    expense_total: int
    net: int
    cumulative: int


class ProfitTrendView(BaseModel):
    items: list[ProfitTrendItem] = Field(default_factory=list)


# ============================== 风险 ==============================

class RiskView(BaseModel):
    id: UUID
    rule_code: str
    object_type: str
    object_id: UUID
    period: str
    status: str
    severity: str
    first_seen_at: datetime
    last_seen_at: datetime
    closed_at: datetime | None = None
    resolution: str | None = None
    data_incomplete: bool


class RiskInsufficientView(BaseModel):
    rule_code: str
    object_type: str
    object_id: str
    reason: str
    data_incomplete: bool = True


class RiskScanResponse(BaseModel):
    items: list[RiskView] = Field(default_factory=list)
    created: list[UUID] = Field(default_factory=list)
    insufficient: list[RiskInsufficientView] = Field(default_factory=list)


class CloseRiskRequest(BaseModel):
    resolution: str = Field(..., min_length=1, max_length=500)


# ============================== 报表 ==============================

class ReportRowsView(BaseModel):
    report_key: str
    columns: list[str] = Field(default_factory=list)
    rows: list[dict] = Field(default_factory=list)
    row_count: int = 0


# ============================== 导出 ==============================

class ExportRequest(BaseModel):
    report_key: str
    format: str = Field(..., pattern="^(XLSX|CSV|PDF)$")
    filters: dict = Field(default_factory=dict)


class ExportJobView(BaseModel):
    id: UUID
    job_no: str
    report_key: str
    format: str
    status: str
    row_count: int | None = None
    object_key: str | None = None
    content_type: str | None = None
    requested_by_id: UUID
    scope: str
    expires_at: datetime | None = None
    created_at: datetime


class ExportDownloadView(BaseModel):
    job_id: str
    job_no: str
    object_key: str
    content_type: str | None = None
    row_count: int | None = None
    expires_at: str | None = None
    ttl_seconds: int
