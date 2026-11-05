"""督导跟访/整改 API 入参出参（Pydantic v2）。ORM 不直接作为返回。"""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


class VisitPlanRequest(BaseModel):
    region_id: UUID
    shadowed_user_id: UUID
    merchant_id: UUID
    planned_date: date


class IssueInput(BaseModel):
    category: str
    description: str = Field(min_length=1, max_length=2000)
    owner_user_id: UUID
    due_date: date

    @field_validator("category")
    @classmethod
    def _category_allowed(cls, v: str) -> str:
        if v not in ("DISPLAY", "SERVICE", "EQUIPMENT", "OTHER"):
            raise ValueError("问题分类必须是 DISPLAY/SERVICE/EQUIPMENT/OTHER")
        return v


class VisitSubmitRequest(BaseModel):
    action_score: int = Field(ge=0, le=100)
    photo_count: int = Field(ge=0)
    visited_at: datetime
    display_issue: str | None = Field(default=None, max_length=4000)
    service_issue: str | None = Field(default=None, max_length=4000)
    equipment_issue: str | None = Field(default=None, max_length=4000)
    note: str | None = Field(default=None, max_length=4000)
    issues: list[IssueInput] = Field(default_factory=list)


class RectificationSubmitRequest(BaseModel):
    note: str = Field(min_length=1, max_length=2000)


class RectificationVerifyRequest(BaseModel):
    passed: bool
    note: str | None = Field(default=None, max_length=2000)


class RectificationCloseRequest(BaseModel):
    note: str | None = Field(default=None, max_length=2000)


class RectificationView(BaseModel):
    id: UUID
    supervision_visit_id: UUID
    merchant_id: UUID
    region_id: UUID
    category: str
    description: str
    owner_user_id: UUID
    due_date: date
    status: str
    submitter_id: UUID | None = None
    verifier_id: UUID | None = None


class SupervisionVisitView(BaseModel):
    id: UUID
    region_id: UUID
    status: str
    supervisor_id: UUID
    shadowed_user_id: UUID
    merchant_id: UUID
    planned_date: date
    visited_at: datetime | None = None
    photo_count: int
    action_score: int | None = None
    display_issue: str | None = None
    service_issue: str | None = None
    equipment_issue: str | None = None
    note: str | None = None
    rectifications: list[RectificationView] = Field(default_factory=list)


class WeeklyCoverageView(BaseModel):
    region_id: UUID
    supervisor_id: UUID
    week_start: date
    visit_count: int
    distinct_rep_count: int
    compliant: bool
    warnings: list[str]


# ----------------------------- 列表 / 选项（T16 督导页面） -----------------------------


class OptionView(BaseModel):
    id: UUID
    code: str
    name: str


class OptionListView(BaseModel):
    items: list[OptionView] = Field(default_factory=list)


class SupervisionVisitListItem(BaseModel):
    """跟访列表行：补齐片区/督导/被跟访业务员/商家名称与整改计数。"""

    id: UUID
    region_id: UUID
    region_name: str
    status: str
    supervisor_id: UUID
    supervisor_name: str | None = None
    shadowed_user_id: UUID
    shadowed_user_name: str | None = None
    merchant_id: UUID
    merchant_name: str
    planned_date: date
    visited_at: datetime | None = None
    photo_count: int
    action_score: int | None = None
    note: str | None = None
    # 本跟访已生成的整改单总数 / 仍未闭环数（OPEN+IN_PROGRESS+SUBMITTED）
    rectification_count: int
    pending_rectification_count: int


class RectificationListItem(BaseModel):
    """整改列表行：负责人视角与督导/经理视角共用同一行结构。"""

    id: UUID
    region_id: UUID
    region_name: str
    supervision_visit_id: UUID
    merchant_id: UUID
    merchant_name: str
    category: str
    description: str
    owner_user_id: UUID
    owner_name: str | None = None
    due_date: date
    status: str
    submitter_id: UUID | None = None
    verifier_id: UUID | None = None
    # 逾期：已过截止日且未闭环（VERIFIED/CLOSED 不计）
    overdue: bool


class Pagination(BaseModel):
    """分页元信息（与 schemas/merchant.Pagination 同形，各业务域自持一份）。"""

    page: int
    page_size: int
    total: int


class SupervisionVisitListResponse(BaseModel):
    items: list[SupervisionVisitListItem]
    pagination: Pagination
    applied_filters: dict


class RectificationListResponse(BaseModel):
    items: list[RectificationListItem]
    pagination: Pagination
    applied_filters: dict
