"""AI 助手会话持久化服务（方法见《03.Agent状态管理》第 4/5 节）。

对照资料实现：
- **按员工和会话编号隔离（§4）**：所有读写都以 `actor.user_id` 为行级边界，
  他人会话一律 404（不暴露存在性）；
- **每轮成功后整体保存（§4 save_session）**：一轮对话（user + assistant）
  在同一事务内落库，assistant 消息的 meta 保存工具调用与结果摘要；
- **上下文压缩（§5 compact_context）**：`history_for_model` 按字符阈值从
  最新往回放消息，超出即丢弃更早历史；facts 作为需求摘要始终保留，
  由调用方决定注入方式；
- **需求状态 facts（§2）**：每轮把确定性解析出的关键信息（工具/周期/检索词）
  覆盖式合并进 `session.facts`，只更新本轮出现过的字段。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import DomainError
from app.core.permissions import ActorContext
from app.models.assistant_session import (
    ROLE_ASSISTANT,
    ROLE_USER,
    TITLE_MAX,
    AssistantMessage,
    AssistantSession,
)

# 资料 §5：CONTEXT_CHAR_LIMIT = 12000；历史+当前问题超过阈值即丢弃最早消息
CONTEXT_CHAR_LIMIT = 12_000
# 会话列表默认返回条数
_LIST_LIMIT = 50


class AssistantSessionService:
    """会话的创建/读取/追加与模型上下文重建（员工行级隔离）。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    # ============================== 会话读写 ==============================

    def get_or_create(
        self, actor: ActorContext, conversation_id: str | None, first_message: str
    ) -> AssistantSession:
        """有 conversation_id 则校验归属后复用；否则以首条消息建会话（首句作标题）。"""
        if conversation_id:
            try:
                cid = UUID(str(conversation_id))
            except ValueError as exc:
                raise DomainError("SESSION_NOT_FOUND", "会话编号格式不正确") from exc
            row = self.session.get(AssistantSession, cid)
            # 他人会话按 404 处理：不暴露会话是否存在（最小信息披露）
            if row is None or row.user_id != actor.user_id:
                raise DomainError("SESSION_NOT_FOUND", "会话不存在或无权访问")
            return row

        title = " ".join(first_message.split())[:TITLE_MAX] or "新会话"
        row = AssistantSession(user_id=actor.user_id, title=title)
        self.session.add(row)
        self.session.flush()
        return row

    def append_turn(
        self,
        session: AssistantSession,
        *,
        user_message: str,
        assistant_reply: str,
        assistant_meta: dict[str, Any] | None = None,
        facts_patch: dict[str, Any] | None = None,
    ) -> None:
        """一轮成功对话整体落库（user + assistant），并更新会话计数与 facts。"""
        next_seq = (session.message_count or 0) + 1
        self.session.add(
            AssistantMessage(
                session_id=session.id, seq=next_seq, role=ROLE_USER, content=user_message
            )
        )
        self.session.add(
            AssistantMessage(
                session_id=session.id,
                seq=next_seq + 1,
                role=ROLE_ASSISTANT,
                content=assistant_reply,
                meta=assistant_meta or {},
            )
        )
        session.message_count = next_seq + 1
        session.last_message_at = datetime.now(UTC)
        if facts_patch:
            # 只覆盖本轮解析出的字段（资料 §2：facts.update(changes)）
            merged = dict(session.facts or {})
            merged.update({k: v for k, v in facts_patch.items() if v})
            session.facts = merged
        self.session.flush()

    # ============================== 列表 / 历史 ==============================

    def list_sessions(
        self, actor: ActorContext, *, limit: int = _LIST_LIMIT
    ) -> list[AssistantSession]:
        stmt = (
            select(AssistantSession)
            .where(AssistantSession.user_id == actor.user_id)
            .order_by(AssistantSession.last_message_at.desc())
            .limit(max(1, min(limit, 200)))
        )
        return list(self.session.scalars(stmt).all())

    def get_owned(
        self, actor: ActorContext, session_id: str
    ) -> AssistantSession:
        try:
            cid = UUID(str(session_id))
        except ValueError as exc:
            raise DomainError("SESSION_NOT_FOUND", "会话不存在或无权访问") from exc
        row = self.session.get(AssistantSession, cid)
        if row is None or row.user_id != actor.user_id:
            raise DomainError("SESSION_NOT_FOUND", "会话不存在或无权访问")
        return row

    def list_messages(
        self, actor: ActorContext, session_id: str
    ) -> list[AssistantMessage]:
        owned = self.get_owned(actor, session_id)
        stmt = (
            select(AssistantMessage)
            .where(AssistantMessage.session_id == owned.id)
            .order_by(AssistantMessage.seq.asc())
        )
        return list(self.session.scalars(stmt).all())

    def delete_session(self, actor: ActorContext, session_id: str) -> None:
        owned = self.get_owned(actor, session_id)
        # 消息经外键 ondelete=CASCADE 一并清理
        self.session.delete(owned)
        self.session.flush()

    # ============================== 模型上下文（§5 compact_context） ==============================

    def history_for_model(
        self,
        session: AssistantSession,
        *,
        char_limit: int = CONTEXT_CHAR_LIMIT,
    ) -> list[dict[str, str]]:
        """重建喂模型的历史消息：按 seq 正序，超过字符阈值丢弃最早的消息。

        与资料 `compact_context()` 等价：从最新往回放，整体字符数逼近阈值即停；
        截断点只落在消息边界（本会话实现中工具结果内嵌在 assistant 消息 meta，
        不存在半截工具链）。facts 不由这里注入，调用方按需要取用。
        """
        stmt = (
            select(AssistantMessage)
            .where(AssistantMessage.session_id == session.id)
            .order_by(AssistantMessage.seq.desc())
        )
        rows = list(self.session.scalars(stmt).all())
        kept: list[dict[str, str]] = []
        used = 0
        for row in rows:
            cost = len(row.content or "") + 16  # role 等结构开销的粗略估计
            if kept and used + cost > char_limit:
                break
            kept.append({"role": row.role, "content": row.content or ""})
            used += cost
        kept.reverse()
        return kept
