"""商家中心 API 请求/响应模型（PRD §4.3）。ORM 不直接作为 API 返回。"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

AssignmentType = Literal["PRIMARY", "TEMPORARY"]
Grade = Literal["A", "B", "C"]
MerchantStatusLiteral = Literal[
    "LEAD", "NEGOTIATING", "ACTIVE", "PAUSED", "TERMINATED"
]


# ----------------------------- 请求 -----------------------------
class MerchantCreateRequest(BaseModel):
    code: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=128)
    region_id: UUID
    brand_id: UUID
    address: str | None = Field(default=None, max_length=255)
    contact_phone: str | None = Field(default=None, max_length=32)
    primary_contact_name: str | None = Field(default=None, max_length=64)
    business_hours: str | None = Field(default=None, max_length=128)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    # 建档即指派主负责人（可选）；归属走生效区间
    owner_user_id: UUID | None = None
    assignment_start: datetime | None = None

    @field_validator("code", "name")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()


class MerchantListQuery(BaseModel):
    """URL 筛选条件的规范化表示（Q042：条件可由查询参数往返恢复）。"""

    status: str | None = None
    grade: str | None = None
    region_id: UUID | None = None
    keyword: str | None = None
    order: str | None = None
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=100)


class AssignmentCreateRequest(BaseModel):
    owner_user_id: UUID
    assignment_type: AssignmentType
    start_at: datetime
    end_at: datetime | None = None
    reason: str | None = Field(default=None, max_length=255)


class GradeChangeRequest(BaseModel):
    new_grade: Grade
    effective_from: date
    reason: str = Field(min_length=1, max_length=255)


class MerchantStatusChangeRequest(BaseModel):
    """商家状态变更（PRD §4.3 状态机）。

    `TERMINATED` 在枚举里但业务上不可由业务员直改：终止合作必须走
    「取消合作申请 → 经理审批 → 撤柜」（PRD §6.3、R2），由 Service 拒绝并给明确错误码。
    """

    new_status: MerchantStatusLiteral
    reason: str = Field(min_length=1, max_length=255)


# ----------------------------- 响应 -----------------------------
class MerchantListItem(BaseModel):
    id: UUID
    code: str
    name: str
    status: str
    grade: str | None
    region_id: UUID
    brand_id: UUID
    address: str | None
    contact_phone: str | None
    primary_contact_name: str | None
    latitude: float | None
    longitude: float | None
    current_owner_id: UUID | None = None
    created_at: datetime


class Pagination(BaseModel):
    page: int
    page_size: int
    total: int


class MerchantListResponse(BaseModel):
    items: list[MerchantListItem]
    pagination: Pagination
    # 回显本次实际生效的筛选条件（用于前端恢复 URL 状态）
    applied_filters: dict
    duplicates: list[dict] = Field(default_factory=list)


class AssignmentView(BaseModel):
    id: UUID
    owner_user_id: UUID
    assignment_type: str
    start_at: datetime
    end_at: datetime | None
    reason: str | None


class GradeChangeView(BaseModel):
    id: UUID
    old_grade: str | None
    new_grade: str
    effective_from: date
    reason: str


class MerchantStatusChangeView(BaseModel):
    merchant_id: UUID
    old_status: str
    new_status: str
    reason: str


class MerchantDetailResponse(BaseModel):
    id: UUID
    code: str
    name: str
    status: str
    grade: str | None
    region_id: UUID
    brand_id: UUID
    address: str | None
    contact_phone: str | None
    primary_contact_name: str | None
    business_hours: str | None
    latitude: float | None
    longitude: float | None
    # 脱敏标记：contact_phone/精确坐标是否已按角色裁剪
    contact_masked: bool
    location_masked: bool
    current_owner: AssignmentView | None
    assignments: list[AssignmentView]
    grade_history: list[GradeChangeView]
    contracts: list[dict]
    freezers: list[dict]
    visits: int
    replenishments: list[dict]
    activities: list[dict]
    issues: list[dict]
    audits: list[dict]


class MerchantCreateResponse(BaseModel):
    id: UUID
    code: str
    # 非阻断重复提示：同片区店名/地址近似的存量商家
    duplicate_warnings: list[dict] = Field(default_factory=list)
