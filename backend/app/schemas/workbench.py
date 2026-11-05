"""T27 角色工作台 / 通知 / 全局搜索 API 出入参（Pydantic v2）。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

# ============================== 工作台 ==============================

class MetricCard(BaseModel):
    key: str
    name: str
    value: Any | None = None
    unit: str
    definition: str
    as_of: datetime
    period: str
    data_complete: bool
    note: str | None = None
    source_refs: list[str] = Field(default_factory=list)


class TodoItem(BaseModel):
    key: str
    source: str
    source_id: str
    title: str
    category: str
    occurred_at: datetime
    deep_link: str
    read: bool


class RiskCard(BaseModel):
    id: str
    rule_code: str
    object_type: str
    object_id: str
    status: str
    severity: str
    first_seen_at: datetime
    priority: int
    deep_link: str


class QuickAction(BaseModel):
    label: str
    path: str


class WorkbenchHome(BaseModel):
    role: str
    period: str
    as_of: str | None = None
    metric_cards: list[MetricCard] = Field(default_factory=list)
    todos: list[TodoItem] = Field(default_factory=list)
    unread_count: int = 0
    risk_list: list[RiskCard] = Field(default_factory=list)
    quick_actions: list[QuickAction] = Field(default_factory=list)
    deep_links: dict[str, str] = Field(default_factory=dict)


# ============================== 通知 ==============================

class NotificationView(BaseModel):
    key: str
    source: str
    source_id: str
    title: str
    category: str
    occurred_at: datetime
    deep_link: str
    read: bool


class MarkReadRequest(BaseModel):
    key: str = Field(..., min_length=1, max_length=96)


class UnreadCountView(BaseModel):
    unread_count: int


class MarkAllReadView(BaseModel):
    marked: int


# ============================== 搜索 ==============================

class SearchHitView(BaseModel):
    object_type: str
    object_id: str
    title: str
    subtitle: str
    status: str
    module: str
    deep_link: str
