"""周排班 API 入参/出参（Pydantic v2）。ORM 模型不直接作为 API 返回。"""

from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


class ScheduleEntryInput(BaseModel):
    """一条排班条目（草稿创建）。时间窗可整体留空（发布时按默认时段生成占用）。"""

    merchant_id: UUID
    assigned_user_id: UUID
    planned_date: date
    start_at: datetime | None = None
    end_at: datetime | None = None
    priority: int = Field(default=2, ge=1, le=3)


class AppendEntriesRequest(BaseModel):
    """已发布周的增量追加：只新增条目，不改动已有条目与已生成任务。"""

    entries: list[ScheduleEntryInput] = Field(default_factory=list, min_length=1)

    @field_validator("entries")
    @classmethod
    def _unique_merchant(cls, v: list[ScheduleEntryInput]):
        if len({e.merchant_id for e in v}) != len(v):
            raise ValueError("同一批次中商家只能出现一次")
        return v


class DraftBatchCreate(BaseModel):
    region_id: UUID
    week_start: date  # 必须为 Asia/Shanghai 周一
    entries: list[ScheduleEntryInput] = Field(default_factory=list, min_length=1)
    idempotency_key: str | None = Field(default=None, max_length=64)

    @field_validator("entries")
    @classmethod
    def _unique_merchant(cls, v: list[ScheduleEntryInput]):
        if len({e.merchant_id for e in v}) != len(v):
            raise ValueError("同一批次中商家只能出现一次")
        return v


class TemplateEntryInput(BaseModel):
    """每周循环排班的一条模板：排给谁 + 商家 + 周几 + 时段（不绑定日期）。"""

    merchant_id: UUID
    assigned_user_id: UUID
    weekday: int = Field(..., ge=1, le=7, description="1=周一 … 7=周日")
    start_time: time
    end_time: time
    priority: int = Field(default=2, ge=1, le=3)


class TemplateBulkCreate(BaseModel):
    """按天批量添加：同一天可以放多家不同商家。"""

    region_id: UUID
    entries: list[TemplateEntryInput] = Field(default_factory=list, min_length=1)

    @field_validator("entries")
    @classmethod
    def _unique_merchant(cls, v: list[TemplateEntryInput]):
        if len({e.merchant_id for e in v}) != len(v):
            raise ValueError("同一商家在一周模板中只能出现一次")
        for e in v:
            if e.end_time <= e.start_time:
                raise ValueError("结束时间必须晚于开始时间")
        return v


class TemplateView(BaseModel):
    id: UUID
    region_id: UUID
    # 「全部可见片区」视图下用于区分模板所属片区
    region_name: str | None = None
    weekday: int
    merchant_id: UUID
    merchant_name: str
    assigned_user_id: UUID
    assigned_user_name: str | None = None
    start_time: time
    end_time: time
    priority: int


class ScheduleUserOption(BaseModel):
    """排班人员下拉：业务员 + 其所属片区（跨片区时用于提示，避免误排）。"""

    id: UUID
    code: str
    name: str
    region_names: list[str] = Field(default_factory=list)


class ScheduleMerchantItem(BaseModel):
    """排班候选商家：启用即入列，并标记本业务周是否已排。"""

    id: UUID
    code: str
    name: str
    grade: str | None = None
    region_id: UUID
    region_name: str
    current_owner_id: UUID | None = None
    scheduled: bool = False


class TemplateApplyRequest(BaseModel):
    region_id: UUID
    week_start: date  # 必须为 Asia/Shanghai 周一
    # True：该周已发布排班时撤销后按模板整周重排（默认只在已发布周增量补齐缺失商家）
    replace: bool = False


class TemplateSyncRequest(BaseModel):
    region_id: UUID
    # True：用已发布排班覆盖已有模板的业务员/周几/时段
    overwrite: bool = False
    # 指定源业务周（页面「用本周排班覆盖模板」）；为空则取历史上最新一周
    week_start: date | None = None


class TemplateSyncResult(BaseModel):
    created: int
    updated: int
    skipped: int
    source_entries: int


class BatchView(BaseModel):
    id: UUID
    week_start: date
    region_id: UUID
    status: str
    published_at: datetime | None
    entry_count: int
    # 本次操作真实新增的条目数（0 表示该周已是最新，无写入）
    created_count: int = 0


class ScheduleEntryView(BaseModel):
    entry_id: UUID
    # 所属批次：已发布周用于“继续追加”（前端据此调用 append 接口）
    batch_id: UUID | None = None
    # 所属片区：前端按片区把勾选商家分成多个草稿批次
    region_id: UUID | None = None
    merchant_id: UUID
    merchant_name: str
    lng: float | None = None
    lat: float | None = None
    assigned_user_id: UUID
    assigned_user_name: str | None = None
    planned_date: date
    start_at: datetime | None
    end_at: datetime | None
    priority: int
    # True：未发布周按每周模板预排的只读条目（entry_id 实为模板 id，不能改期）
    projected: bool = False


class ScheduleEntryMove(BaseModel):
    """经理改期：把一条已发布排班挪到同一业务周的另一天。

    ``start_at/end_at`` 留空时沿用原时间段的时分；若目标日该时段已被占用，
    自动顺延到当天第一个 30 分钟空档。
    """

    planned_date: date
    start_at: datetime | None = None
    end_at: datetime | None = None

    @field_validator("end_at")
    @classmethod
    def _window_order(cls, v: datetime | None, info):
        start = info.data.get("start_at")
        if v is not None and start is not None and v <= start:
            raise ValueError("结束时间必须晚于开始时间")
        return v


class WeekView(BaseModel):
    week_start: date
    entries: list[ScheduleEntryView] = Field(default_factory=list)
    # 该周在可见范围内是否已发布排班：已发布但当前筛选无条目时前端要给出明确提示，
    # 避免把「模板预排」误当排班（模板预排不落巡访任务）。
    has_published: bool = False


class SuggestionView(BaseModel):
    merchant_id: UUID
    merchant_name: str
    grade: str | None
    assignee_id: UUID | None
    overdue_days: int
    has_pending_replenishment: bool
    distance_meters: float | None
    risk_pinned: bool
    reasons: list[str] = Field(default_factory=list)


class CoverageGapView(BaseModel):
    total_active: int
    covered: int
    gap: int
    rate: Decimal
    missing_merchant_ids: list[UUID] = Field(default_factory=list)
