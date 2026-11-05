"""MCP AI 草稿暂存应用服务（T24 / PRD §5.2）。

只负责 6 类草稿在独立暂存表的保存/更新/查询/作废/具化标记；**不**把草稿写入
任何正式业务表，也不参与正式指标口径。草稿具化为正式记录由各受控 Service
（商家/巡访/补货/合同/活动/督导）在结构化确认后完成，本服务仅回填可追溯引用。

归属隔离：草稿只能由其发起人在本会话读取/修改/作废。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID

from app.core.errors import ConflictError, DomainError, PermissionDeniedError
from app.core.permissions import ActorContext
from app.models.mcp_draft import (
    DRAFT_KINDS,
    STATUS_DISCARDED,
    STATUS_MATERIALIZED,
    STATUS_OPEN,
    McpDraft,
)

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


class McpDraftService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def save_draft(
        self,
        *,
        actor: ActorContext,
        kind: str,
        payload: dict,
        session_id: str = "",
        merchant_id: UUID | None = None,
        draft_id: UUID | None = None,
        idempotency_key: str = "",
    ) -> McpDraft:
        """创建或更新一条 OPEN 草稿（create_draft / update_draft 共用）。"""
        if kind not in DRAFT_KINDS:
            raise DomainError(
                "DRAFT_KIND_UNSUPPORTED",
                f"不支持的草稿类型：{kind}",
                {"allowed": list(DRAFT_KINDS)},
            )
        if not isinstance(payload, dict) or not payload:
            raise DomainError("DRAFT_PAYLOAD_EMPTY", "草稿内容不能为空")

        if draft_id is not None:
            draft = self._get_owned(draft_id, actor, session_id, writable=True)
            draft.payload = payload
            if merchant_id is not None:
                draft.merchant_id = merchant_id
            draft.params_hash = self._hash(payload)
            draft.updated_at = datetime.now(tz=UTC)
            self.session.flush()
            return draft

        # 幂等键：同键重复保存返回同一草稿（窗口内）
        if idempotency_key:
            from sqlalchemy import select

            existing = self.session.execute(
                select(McpDraft).where(
                    McpDraft.idempotency_key == idempotency_key,
                    McpDraft.owner_user_id == actor.user_id,
                )
            ).scalar_one_or_none()
            if existing is not None and existing.status == STATUS_OPEN:
                existing.payload = payload
                existing.params_hash = self._hash(payload)
                existing.updated_at = datetime.now(tz=UTC)
                self.session.flush()
                return existing

        draft = McpDraft(
            kind=kind,
            owner_user_id=actor.user_id,
            session_id=session_id,
            merchant_id=merchant_id,
            payload=payload,
            status=STATUS_OPEN,
            idempotency_key=idempotency_key or None,
            params_hash=self._hash(payload),
        )
        self.session.add(draft)
        self.session.flush()
        return draft

    def get_draft(
        self,
        *,
        actor: ActorContext,
        draft_id: UUID,
        session_id: str = "",
        writable: bool = False,
    ) -> McpDraft:
        return self._get_owned(draft_id, actor, session_id, writable=writable)

    def list_drafts(
        self,
        *,
        actor: ActorContext,
        kind: str = "",
        status: str = "",
        session_id: str = "",
        limit: int = 100,
    ) -> list[McpDraft]:
        from sqlalchemy import select

        if kind and kind not in DRAFT_KINDS:
            raise DomainError("DRAFT_KIND_UNSUPPORTED", f"不支持的草稿类型：{kind}")
        stmt = select(McpDraft).where(McpDraft.owner_user_id == actor.user_id)
        if kind:
            stmt = stmt.where(McpDraft.kind == kind)
        if status:
            stmt = stmt.where(McpDraft.status == status)
        if session_id:
            stmt = stmt.where(McpDraft.session_id == session_id)
        stmt = stmt.order_by(McpDraft.updated_at.desc()).limit(min(max(limit, 1), 500))
        return list(self.session.execute(stmt).scalars().all())

    def discard_draft(
        self, *, actor: ActorContext, draft_id: UUID, session_id: str = ""
    ) -> McpDraft:
        """逻辑作废（不物理删除）；已具化草稿不可作废。"""
        draft = self._get_owned(draft_id, actor, session_id, writable=True)
        if draft.status == STATUS_MATERIALIZED:
            raise DomainError("DRAFT_ALREADY_MATERIALIZED", "草稿已转为正式记录，不能作废")
        draft.status = STATUS_DISCARDED
        draft.discarded_at = datetime.now(tz=UTC)
        draft.updated_at = datetime.now(tz=UTC)
        self.session.flush()
        return draft

    def mark_materialized(
        self,
        *,
        draft_id: UUID,
        materialized_type: str,
        materialized_id: UUID,
    ) -> McpDraft:
        """具化成功后回填正式记录引用（由受控流程在同事务调用，不做归属再校验）。"""
        from sqlalchemy import select

        draft = self.session.execute(
            select(McpDraft).where(McpDraft.id == draft_id)
        ).scalar_one_or_none()
        if draft is None:
            raise DomainError("DRAFT_NOT_FOUND", "草稿不存在")
        draft.status = STATUS_MATERIALIZED
        draft.materialized_type = materialized_type
        draft.materialized_id = materialized_id
        draft.materialized_at = datetime.now(tz=UTC)
        draft.updated_at = datetime.now(tz=UTC)
        self.session.flush()
        return draft

    # ----------------------------- 内部 -----------------------------

    def _get_owned(
        self,
        draft_id: UUID,
        actor: ActorContext,
        session_id: str,
        *,
        writable: bool,
    ) -> McpDraft:
        from sqlalchemy import select

        draft = self.session.execute(
            select(McpDraft).where(McpDraft.id == draft_id)
        ).scalar_one_or_none()
        if draft is None:
            raise DomainError("DRAFT_NOT_FOUND", "草稿不存在")
        if draft.owner_user_id != actor.user_id:
            raise PermissionDeniedError("DRAFT_FORBIDDEN", "不能访问他人的草稿")
        if session_id and draft.session_id and draft.session_id != session_id:
            raise PermissionDeniedError(
                "DRAFT_SESSION_FORBIDDEN", "草稿属于其他会话，不能访问"
            )
        if writable and draft.status == STATUS_MATERIALIZED:
            raise DomainError("DRAFT_ALREADY_MATERIALIZED", "草稿已转为正式记录，不能修改")
        if writable and draft.status == STATUS_DISCARDED:
            raise ConflictError("DRAFT_DISCARDED", "草稿已作废，不能修改")
        return draft

    @staticmethod
    def _hash(payload: dict) -> str:
        import hashlib
        import json

        canonical = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, default=str
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
