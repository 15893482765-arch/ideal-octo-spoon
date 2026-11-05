"""T14 商家调拨 Pydantic v2 schema（ORM 不直接对外）。

红线：行级范围（dataset）不接受客户端输入，故无 dataset 字段。
"""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


class TransferCreate(BaseModel):
    merchant_id: UUID
    from_user_id: UUID
    to_user_id: UUID
    effective_date: date
    reason: str = Field(min_length=1, max_length=500)

    @field_validator("reason")
    @classmethod
    def _reason_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("调拨原因不能为空")
        return v


class TransferWithdraw(BaseModel):
    reason: str = Field(min_length=1, max_length=500)

    @field_validator("reason")
    @classmethod
    def _reason_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("撤回原因不能为空")
        return v


class TransferReverse(BaseModel):
    original_id: UUID
    effective_date: date
    reason: str = Field(min_length=1, max_length=500)

    @field_validator("reason")
    @classmethod
    def _reason_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("调拨原因不能为空")
        return v


class TransferOut(BaseModel):
    id: UUID
    merchant_id: UUID
    from_user_id: UUID
    to_user_id: UUID
    effective_date: date
    effective_at: datetime | None
    status: str
    reason: str
    withdrawn_at: datetime | None
    withdraw_reason: str | None
    reverses_id: UUID | None
    reversed_by_id: UUID | None
    handover_json: dict | None

    model_config = {"from_attributes": True}
