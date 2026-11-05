"""地推活动 API 入参/出参（Pydantic v2）。ORM 模型不直接作为 API 返回。"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


class SampleLine(BaseModel):
    """一条样品行（草稿领用 / 复盘结果共用）。"""

    sku_id: UUID
    boxes: Decimal = Field(ge=0)
    note: str | None = Field(default=None, max_length=255)


class ActivityCreate(BaseModel):
    region_id: UUID
    name: str = Field(min_length=1, max_length=128)
    start_at: datetime
    end_at: datetime
    location: str = Field(min_length=1, max_length=255)
    owner_user_id: UUID
    participant_user_ids: list[UUID] = Field(default_factory=list, min_length=1)
    budget_fen: int = Field(default=0, ge=0)
    notes: str | None = None
    samples: list[SampleLine] = Field(default_factory=list)

    @field_validator("participant_user_ids")
    @classmethod
    def _unique_participants(cls, v: list[UUID]):
        if len(set(v)) != len(v):
            raise ValueError("同一活动参与人不能重复")
        return v


class ActivityCancelRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=2000)


class SampleResultLine(BaseModel):
    """复盘样品结果行。"""

    sku_id: UUID
    stage: str = Field(description="CONSUMED / RETURNED / MISSING")
    boxes: Decimal = Field(gt=0)
    note: str | None = Field(default=None, max_length=255)

    @field_validator("stage")
    @classmethod
    def _stage_allowed(cls, v: str) -> str:
        if v not in ("CONSUMED", "RETURNED", "MISSING"):
            raise ValueError("复盘阶段必须是 CONSUMED/RETURNED/MISSING")
        return v


class ActivityReviewRequest(BaseModel):
    sample_results: list[SampleResultLine] = Field(default_factory=list)


class ParticipantView(BaseModel):
    user_id: UUID


class SampleLedgerView(BaseModel):
    sku_id: UUID
    stage: str
    boxes: Decimal
    operator_id: UUID
    note: str | None = None


class ActivityView(BaseModel):
    id: UUID
    name: str
    status: str
    region_id: UUID
    start_at: datetime
    end_at: datetime
    location: str
    owner_user_id: UUID
    budget_fen: int
    sample_cost_fen: int
    notes: str | None = None
    cancel_reason: str | None = None
    cancelled_at: datetime | None = None
    reviewed_at: datetime | None = None
    participant_user_ids: list[UUID] = Field(default_factory=list)
    active_reservation_ids: list[UUID] = Field(default_factory=list)
    sample_ledger: list[SampleLedgerView] = Field(default_factory=list)


# ----------------------------- 列表 / 选项（T15 地推活动页面） -----------------------------


class OptionView(BaseModel):
    id: UUID
    code: str
    name: str


class OptionListView(BaseModel):
    items: list[OptionView] = Field(default_factory=list)


class ActivityListItem(BaseModel):
    """活动列表行：补齐片区名、负责人名与占位统计，前端逐行展示不需要再回查。"""

    id: UUID
    name: str
    status: str
    region_id: UUID
    region_name: str
    owner_user_id: UUID
    owner_name: str | None = None
    start_at: datetime
    end_at: datetime
    location: str
    budget_fen: int
    sample_cost_fen: int
    # 参与人数与已生效（ACTIVE）人员占用条数：发布后一一对应，草稿为 0 占用
    participant_count: int
    active_reservation_count: int
    notes: str | None = None
    cancel_reason: str | None = None
    reviewed_at: datetime | None = None


class Pagination(BaseModel):
    """分页元信息（与 schemas/merchant.Pagination 同形，各业务域自持一份）。"""

    page: int
    page_size: int
    total: int


class ActivityListResponse(BaseModel):
    items: list[ActivityListItem]
    pagination: Pagination
    # 回显本次实际生效的筛选条件（前端据此恢复 URL 状态）
    applied_filters: dict
