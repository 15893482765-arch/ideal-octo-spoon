"""巡访 API 请求/响应 schema（PRD §4.6 / T11）。

照片"先上传后绑定"：提交时只给 file_id（FileService 已持久化）。
坐标/距离一律以服务端重算为准，schema 不接收客户端"距离"作为裁决输入。
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from app.models.enums import (
    VisitConclusion,
    VisitFreezerStatus,
    VisitSource,
)

PHOTO_KINDS = {"STOREFRONT", "FREEZER", "OTHER"}


class VisitTaskCreate(BaseModel):
    merchant_id: UUID
    assigned_user_id: UUID | None = None  # 空=服务端按 actor 注入（业务员给自己）
    planned_date: date
    window_start_at: datetime | None = None
    window_end_at: datetime | None = None
    priority: int = 2
    source: VisitSource = VisitSource.WEEKLY_SCHEDULE
    reason: str | None = Field(default=None, max_length=255)

    @field_validator("priority")
    @classmethod
    def _priority_range(cls, v: int) -> int:
        if not 1 <= v <= 3:
            raise ValueError("优先级须在 1–3 之间")
        return v


class VisitPhotoInput(BaseModel):
    file_id: UUID
    photo_kind: str = "STOREFRONT"

    @field_validator("photo_kind")
    @classmethod
    def _kind(cls, v: str) -> str:
        if v not in PHOTO_KINDS:
            raise ValueError("photo_kind 必须为 STOREFRONT/FREEZER/OTHER")
        return v


class VisitSalesLineInput(BaseModel):
    """巡访上报 SKU 销量/反馈行（PRD §4.6：按 SKU 填销量箱数或商家反馈）。

    红线：上报值在经理确认前不进正式口径（确认销量只出自 SalesConfirmation）。
    箱数 NUMERIC(12,2)，用 Decimal 接收，禁止 float。
    """

    sku_id: UUID
    reported_boxes: Decimal = Field(default=Decimal("0"), ge=0, max_digits=12, decimal_places=2)
    feedback: str | None = Field(default=None, max_length=500)
    out_of_stock: bool = False


class VisitReplenishmentLineInput(BaseModel):
    """巡访提报补货行（PRD §4.6：补货 SKU、需求箱数、期望到货日）。

    提交后同事务生成补货 DRAFT（§4.9），经理确认前不进正式补货池/采购口径。
    """

    sku_id: UUID
    requested_boxes: Decimal = Field(..., gt=0, max_digits=12, decimal_places=2)
    expected_delivery_date: date | None = None


class VisitSubmit(BaseModel):
    """业务员提交巡访打卡（visit.submit，PRD §4.6 有效巡访提交表单）。

    - lng/lat：设备定位；accuracy_meters：定位精度；device_checkin_at：设备时间（仅取证）
    - photos：已上传的照片 file_id 列表（至少一张才可能有效）
    - freezer_status/device_issue：冰柜状态与设备异常
    - display_pct：本品牌陈列比例；是否达合同阈值由服务端按生效合同固化阈值判定
    - sales_lines：按 SKU 的上报销量/反馈（确认前不入指标）
    - replenishment_lines：补货需求行（同事务生成补货草稿）
    - conclusion：当日结论
    - idempotency_key：提交幂等（依赖 T04 幂等）
    不接收客户端"距离"，距离由服务端 PostGIS 重算。
    """

    lng: float = Field(..., ge=-180, le=180)
    lat: float = Field(..., ge=-90, le=90)
    accuracy_meters: int | None = Field(default=None, ge=0)
    device_checkin_at: datetime | None = None
    checkout_at: datetime | None = None
    display_pct: int | None = Field(default=None, ge=0, le=100)
    freezer_status: VisitFreezerStatus = VisitFreezerStatus.NORMAL
    device_issue: str | None = Field(default=None, max_length=500)
    conclusion: VisitConclusion = VisitConclusion.NORMAL
    note: str | None = None
    photos: list[VisitPhotoInput] = Field(default_factory=list)
    sales_lines: list[VisitSalesLineInput] = Field(default_factory=list)
    replenishment_lines: list[VisitReplenishmentLineInput] = Field(default_factory=list)
    idempotency_key: str | None = Field(default=None, max_length=64)

    @field_validator("sales_lines")
    @classmethod
    def _unique_sku(cls, v: list[VisitSalesLineInput]):
        if len({line.sku_id for line in v}) != len(v):
            raise ValueError("同一 SKU 在销量行中只能出现一次")
        return v

    @field_validator("replenishment_lines")
    @classmethod
    def _unique_repl_sku(cls, v: list[VisitReplenishmentLineInput]):
        if len({line.sku_id for line in v}) != len(v):
            raise ValueError("同一 SKU 在补货行中只能出现一次")
        return v


class VisitSkuOption(BaseModel):
    """巡访提报补货/销量行可选 SKU。"""

    id: UUID
    code: str
    name: str


class VisitJudge(BaseModel):
    """经理对异常巡访（ABNORMAL_PENDING）的判定。"""

    recognize: bool = Field(
        ...,
        description="true=认可计有效(RECOGNIZED)；false=判异常不计(ABNORMAL)",
    )
    reason: str = Field(..., min_length=1)


class VisitRevisionCreate(BaseModel):
    supplement: str = Field(..., min_length=1)


class VisitPhotoView(BaseModel):
    id: UUID
    file_id: UUID
    photo_kind: str


class VisitRevisionView(BaseModel):
    id: UUID
    author_id: UUID
    supplement: str
    created_at: datetime


class VisitTaskView(BaseModel):
    id: UUID
    merchant_id: UUID
    assigned_user_id: UUID
    planned_date: date
    window_start_at: datetime | None
    window_end_at: datetime | None
    priority: int
    source: str
    status: str
    reason: str | None


class VisitSalesLineView(BaseModel):
    id: UUID
    sku_id: UUID
    reported_boxes: Decimal
    feedback: str | None
    out_of_stock: bool


class VisitReplenishmentLineView(BaseModel):
    order_id: UUID
    code: str
    sku_id: UUID
    requested_boxes: Decimal
    expected_delivery_date: date | None
    status: str


class VisitRecordView(BaseModel):
    id: UUID
    visit_task_id: UUID
    merchant_id: UUID
    user_id: UUID
    source: str
    checkin_at: datetime | None
    checkout_at: datetime | None
    device_checkin_at: datetime | None
    lng: float | None = None
    lat: float | None = None
    accuracy_meters: int | None
    distance_meters: float | None
    display_pct: int | None
    # T12：合同阈值快照与达标判定
    display_threshold_pct: int | None
    display_met: bool | None
    freezer_status: str | None
    device_issue: str | None
    conclusion: str | None
    photo_count: int
    note: str | None
    is_official: bool = True
    status: str
    validity: str
    judged_by_id: UUID | None
    judged_at: datetime | None
    judge_reason: str | None
    submitted_at: datetime
    photos: list[VisitPhotoView] = Field(default_factory=list)
    revisions: list[VisitRevisionView] = Field(default_factory=list)
    sales_lines: list[VisitSalesLineView] = Field(default_factory=list)
    replenishment_lines: list[VisitReplenishmentLineView] = Field(default_factory=list)
