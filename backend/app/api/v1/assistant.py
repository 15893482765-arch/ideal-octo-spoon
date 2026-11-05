"""/api/v1/assistant 路由：AI 助手对话、会话与能力清单（PRD §5.1）。

- POST /chat                     一句话提问 → 受控只读工具取数 / A1 草稿暂存 → 语言层 → 审计；
- GET  /tools                    当前角色可用的只读与 A1 草稿工具；
- GET  /sessions                 我的历史会话（首句作标题，最近在前）；
- GET  /sessions/{id}/messages   会话历史消息（继续对话时前端回填）；
- DELETE /sessions/{id}          删除我的会话（消息级联清理）。

Router 只做鉴权与 Pydantic 边界转换；意图解析、工具门禁、脱敏与审计在 Service/MCP。
会话按员工隔离：他人会话一律 404，不暴露存在性。
"""

from __future__ import annotations

# ruff: noqa: B008  FastAPI Depends() 需出现在默认值
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.deps import current_actor, db_session
from app.core.permissions import ActorContext
from app.schemas.assistant import (
    AssistantChatRequest,
    AssistantChatView,
    AssistantMessageView,
    AssistantSessionView,
    AssistantToolView,
)
from app.services.assistant_chat_service import AssistantChatService
from app.services.assistant_session_service import AssistantSessionService
from mcp import McpGateway, bind_read_handlers, build_default_registry
from mcp.draft_handlers import DRAFT_HANDLERS

router = APIRouter(prefix="/api/v1/assistant", tags=["assistant"])

# 能力清单只暴露「只读」与「A1 草稿」；A2/A3 写工具不进对话入口
_READ_KIND = "READ"
_DRAFT_KIND = "DRAFT"


@router.post("/chat", response_model=AssistantChatView)
def assistant_chat(
    payload: AssistantChatRequest,
    session: Session = Depends(db_session),
    actor: ActorContext = Depends(current_actor),
):
    data = AssistantChatService(session).chat(
        actor,
        payload.message,
        session_id=payload.session_id,
        conversation_id=payload.conversation_id,
    )
    return AssistantChatView(**data)


@router.get("/sessions", response_model=list[AssistantSessionView])
def assistant_sessions(
    session: Session = Depends(db_session),
    actor: ActorContext = Depends(current_actor),
):
    rows = AssistantSessionService(session).list_sessions(actor)
    return [
        AssistantSessionView(
            id=str(row.id),
            title=row.title,
            message_count=row.message_count,
            last_message_at=row.last_message_at,
            created_at=row.created_at,
        )
        for row in rows
    ]


@router.get("/sessions/{session_id}/messages", response_model=list[AssistantMessageView])
def assistant_session_messages(
    session_id: str,
    session: Session = Depends(db_session),
    actor: ActorContext = Depends(current_actor),
):
    rows = AssistantSessionService(session).list_messages(actor, session_id)
    return [
        AssistantMessageView(
            id=str(row.id),
            role=row.role,
            content=row.content,
            meta=row.meta or {},
            created_at=row.created_at,
        )
        for row in rows
    ]


@router.delete("/sessions/{session_id}", status_code=204)
def assistant_session_delete(
    session_id: str,
    session: Session = Depends(db_session),
    actor: ActorContext = Depends(current_actor),
):
    AssistantSessionService(session).delete_session(actor, session_id)


@router.get("/tools", response_model=list[AssistantToolView])
def assistant_tools(
    actor: ActorContext = Depends(current_actor),
):
    gateway = McpGateway(bind_read_handlers(build_default_registry()))
    views: list[AssistantToolView] = []
    for spec in gateway.list_tools():
        kind = str(spec.get("kind"))
        name = str(spec.get("name"))
        if kind == _DRAFT_KIND and name not in DRAFT_HANDLERS:
            # schedule.suggest / procurement.suggest 是纯建议（不落草稿），不作为草稿能力展示
            continue
        if kind not in {_READ_KIND, _DRAFT_KIND}:
            continue
        roles = spec.get("allowed_roles") or []
        if roles and actor.role_code not in roles:
            continue
        views.append(
            AssistantToolView(
                name=str(spec.get("name")),
                title=str(spec.get("title")),
                description=str(spec.get("description")),
                kind=str(spec.get("kind")),
                risk_level=str(spec.get("risk_level")),
            )
        )
    return views
