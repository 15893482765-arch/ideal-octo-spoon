"""AI 助手对话的请求/响应模型（PRD §5.1 / §5.4）。

边界：响应中的业务数据全部来自受控只读工具；`narrative` 只是语言层，
`assumptions` 一律为假设，不得作为业务事实使用。

会话持久化（《03.Agent状态管理》§4/§5）：
- `conversation_id` 定位服务端持久会话；不传则新建（首条消息作标题）；
- 会话按员工隔离，历史消息注入模型上下文形成记忆。
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

_MAX_MESSAGE = 500
_MAX_HIGHLIGHTS = 5


class AssistantChatRequest(BaseModel):
    """一条用户提问；`session_id` 由前端生成，仅用于串联审计。"""

    message: Annotated[str, Field(min_length=1, max_length=_MAX_MESSAGE)]
    session_id: Annotated[str, Field(max_length=64)] | None = None
    # 服务端持久会话：继续历史对话时传入；空则新建会话
    conversation_id: str | None = None


class AssistantNarrativeView(BaseModel):
    """语言层输出（不含任何业务数值）。"""

    headline: str
    summary: str
    assumptions: list[str] = Field(default_factory=list)
    confidence: Literal["LOW", "MEDIUM", "HIGH"] = "LOW"


class AssistantDraftView(BaseModel):
    """A1 草稿引用：只指向 `mcp_drafts` 暂存记录，不含正式业务表主键。"""

    id: str
    kind: str
    status: str
    # 前端「去表单确认」跳转路径与页面名
    target_path: str
    target_label: str


class AssistantRecordRefView(BaseModel):
    """命中记录的引用：前端右栏凭 object_id 拉详情，path 指向业务页面。"""

    object_type: str
    object_id: str
    path: str | None = None
    label: str = ""


class AssistantChartSeriesView(BaseModel):
    name: str
    # None 表示该分类确实缺数据，前端断点处理，不做补点
    data: list[float | None] = Field(default_factory=list)


class AssistantChartView(BaseModel):
    """查询结果的可视化配置（A2 图表）：数据与 reply 同源，由服务端确定性生成。"""

    chart_type: Literal["line", "bar", "pie"]
    title: str
    categories: list[str] = Field(default_factory=list)
    series: list[AssistantChartSeriesView] = Field(default_factory=list)
    # money_yuan 表示已由服务端把「分」换算成「元」
    unit: Literal["box", "money_yuan", "count", "other"] = "other"
    note: str | None = None
    row_count: int = 0


class AssistantChatView(BaseModel):
    """一次对话的确定性结果 + 语言层 + 审计引用。"""

    reply: str
    # 审计意图：QA / REPLENISHMENT_AGGREGATE / VISIT_COMPOSE
    intent: str
    tool: str | None = None
    # OK / NO_RECORDS / NO_PERMISSION / ERROR / NOT_CALLED / NEED_INPUT / DRAFT_CREATED
    tool_status: str
    item_count: int = 0
    highlights: list[str] = Field(default_factory=list)
    # 命中记录引用（右栏展示依据）；无命中为空
    records: list[AssistantRecordRefView] = Field(default_factory=list)
    # A1 草稿：非空即「仅暂存、待确认」，前端展示去表单确认入口
    draft: AssistantDraftView | None = None
    # A2 图表：仅在查询有可用数据时出现，数据与 reply 同源
    chart: AssistantChartView | None = None
    narrative: AssistantNarrativeView | None = None
    degraded: bool = False
    degraded_reason: str | None = None
    audit_id: str | None = None
    session_id: str | None = None
    # 服务端持久会话 id：前端保存后可在会话列表中继续本轮对话
    conversation_id: str | None = None
    suggestions: list[str] = Field(default_factory=list)


class AssistantToolView(BaseModel):
    """当前角色可用的只读/建议工具（前端能力清单）。"""

    name: str
    title: str
    description: str
    kind: str
    risk_level: str


class AssistantSessionView(BaseModel):
    """会话列表条目：首句作标题，陈列在最近使用的一侧（资料 §4）。"""

    id: str
    title: str
    message_count: int
    last_message_at: datetime
    created_at: datetime


class AssistantMessageView(BaseModel):
    """一条历史消息；assistant 消息的 meta 含工具调用摘要（供前端还原高亮/草稿卡）。"""

    id: str
    role: str
    content: str
    meta: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
