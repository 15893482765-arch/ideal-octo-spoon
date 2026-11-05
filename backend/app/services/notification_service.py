"""通知中心（T27 / PRD §4.2）。

通知是**派生视图**，来自三类既有来源，不复制业务数据、不建物化通知：
1. 任务待办 `task_items`：分派给本人且未完成（OPEN / IN_PROGRESS）；
2. 待审批 `approval_requests`：按审批矩阵角色路由到当前角色的 PENDING；
3. 风险预警 `risk_items`：当前行级范围内 OPEN 的 RK-1～RK-7。

已读状态独立存放于 `notification_reads`：
- 标记已读只 upsert 一条 (user_id, notification_key)，**绝不触碰**来源对象状态
  （PRD：已读不影响任务/审批/风险，Q158）；
- 通知 key 由来源类型 + 来源主键确定性拼出，稳定可复现。

行级范围只来自服务端 ActorContext（JWT/session），本服务不接受客户端传范围。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.core.errors import DomainError
from app.core.permissions import (
    MODULE_APPROVAL,
    MODULE_DASHBOARD,
    ActorContext,
    permission_service,
)
from app.models.approval import ApprovalRequest, TaskItem
from app.models.enums import ApprovalStatus, RiskStatus, TaskStatus
from app.models.notification import NotificationRead
from app.models.risk import RiskItem
from app.models.user import User

SRC_TASK = "TASK"
SRC_APPROVAL = "APPROVAL"
SRC_RISK = "RISK"

_MAX_LIST = 200


@dataclass(frozen=True)
class Notification:
    key: str
    source: str
    source_id: UUID
    title: str
    category: str
    occurred_at: datetime
    deep_link: str
    read: bool

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "source": self.source,
            "source_id": str(self.source_id),
            "title": self.title,
            "category": self.category,
            "occurred_at": self.occurred_at.isoformat(),
            "deep_link": self.deep_link,
            "read": self.read,
        }


def notification_key(source: str, source_id: UUID) -> str:
    return f"{source}:{source_id}"


class NotificationService:
    def __init__(self, session: Session) -> None:
        self.session = session

    # ============================ 查询 ============================

    def list_notifications(self, actor: ActorContext, *, limit: int = 50) -> list[Notification]:
        """聚合当前用户可见通知（行级范围由服务端 actor 决定）。"""
        user = self.session.get(User, actor.user_id)
        if user is None:
            raise DomainError(code="USER_NOT_FOUND", message="登录用户不存在")
        limit = max(1, min(int(limit), _MAX_LIST))

        read_keys = self._read_keys(actor.user_id)
        items: list[Notification] = []

        items.extend(self._task_notifications(actor, read_keys))
        items.extend(self._approval_notifications(actor, user.dataset_id, read_keys))
        items.extend(self._risk_notifications(actor, user.dataset_id, read_keys))

        # 未读优先，其次时间倒序（PRD：通知按时间倒序，未读高亮）
        items.sort(key=lambda n: (n.read, _neg_ts(n.occurred_at)))
        return items[:limit]

    def unread_count(self, actor: ActorContext) -> int:
        return sum(1 for n in self.list_notifications(actor, limit=_MAX_LIST) if not n.read)

    # ============================ 已读（不影响来源状态） ============================

    def mark_read(self, actor: ActorContext, key: str) -> None:
        """标记单条已读：仅写 notification_reads，幂等，不改动任务/审批/风险。"""
        key = (key or "").strip()
        if not key or ":" not in key or len(key) > 96:
            raise DomainError(code="NOTIFICATION_KEY_INVALID", message="通知键非法")
        source, _, raw_id = key.partition(":")
        if source not in (SRC_TASK, SRC_APPROVAL, SRC_RISK):
            raise DomainError(code="NOTIFICATION_KEY_INVALID", message="未知通知来源")
        try:
            source_uuid = UUID(raw_id)
        except ValueError as exc:
            raise DomainError(
                code="NOTIFICATION_KEY_INVALID", message="通知来源主键非法"
            ) from exc
        # 安全校验：只能标记“自己确实可见”的通知，不能给他人/越权对象插已读
        if not self._is_visible(actor, source, source_uuid):
            raise DomainError(code="NOTIFICATION_NOT_FOUND", message="通知不存在或无权访问")

        self._upsert_read(actor.user_id, key)

    def mark_all_read(self, actor: ActorContext) -> int:
        """全部已读：只对当前可见通知写已读标记；返回标记条数。"""
        visible = self.list_notifications(actor, limit=_MAX_LIST)
        count = 0
        for n in visible:
            if not n.read:
                self._upsert_read(actor.user_id, n.key)
                count += 1
        return count

    # ============================ 来源组装 ============================

    def _task_notifications(
        self, actor: ActorContext, read_keys: set[str]
    ) -> list[Notification]:
        # 任务只分派给本人（assignee_id 过滤即行级裁剪），无需额外模块门禁
        # TaskItem 以 assignee_id 本人过滤即行级隔离（任务无 dataset 列）
        rows = self.session.scalars(
            select(TaskItem)
            .where(
                TaskItem.assignee_id == actor.user_id,
                TaskItem.status.in_([TaskStatus.OPEN.value, TaskStatus.IN_PROGRESS.value]),
            )
            .order_by(TaskItem.created_at.desc())
        ).all()
        out: list[Notification] = []
        for t in rows:
            key = notification_key(SRC_TASK, t.id)
            out.append(Notification(
                key=key, source=SRC_TASK, source_id=t.id,
                title=t.title, category=_task_category(t.task_kind),
                occurred_at=t.created_at,
                deep_link=f"/tasks/{t.id}",
                read=key in read_keys,
            ))
        return out

    def _approval_notifications(
        self, actor: ActorContext, dataset_id: UUID, read_keys: set[str]
    ) -> list[Notification]:
        # 待审批：仅按审批矩阵角色路由（PENDING），无审批能力角色自然查不到
        if not permission_service.has_ability(actor, MODULE_APPROVAL):
            return []
        rows = self.session.scalars(
            select(ApprovalRequest)
            .where(
                ApprovalRequest.dataset_id == dataset_id,
                ApprovalRequest.status == ApprovalStatus.PENDING.value,
                ApprovalRequest.matrix_role_code == actor.role_code,
                # 尚未指给具体他人处理：approver_id 为空或就是本人
                (ApprovalRequest.approver_id.is_(None))
                | (ApprovalRequest.approver_id == actor.user_id),
            )
            .order_by(ApprovalRequest.created_at.desc())
        ).all()
        out: list[Notification] = []
        for a in rows:
            key = notification_key(SRC_APPROVAL, a.id)
            out.append(Notification(
                key=key, source=SRC_APPROVAL, source_id=a.id,
                title=f"待审批：{a.target_object_type}"
                      + (f"·{a.action}" if a.action else ""),
                category="APPROVAL_PENDING",
                occurred_at=a.created_at,
                deep_link=f"/approvals/{a.id}",
                read=key in read_keys,
            ))
        return out

    def _risk_notifications(
        self, actor: ActorContext, dataset_id: UUID, read_keys: set[str]
    ) -> list[Notification]:
        if not permission_service.has_ability(actor, MODULE_DASHBOARD):
            return []
        rows = self.session.scalars(
            select(RiskItem).where(
                RiskItem.dataset_type == "PRODUCTION",
                RiskItem.dataset_id == dataset_id,
                RiskItem.status == RiskStatus.OPEN.value,
            )
        ).all()
        # 行级裁剪复用 RiskService 的范围解析，避免自行重算
        from app.services.risk_service import RiskService

        visible = RiskService(self.session).filter_by_scope(actor, rows)
        out: list[Notification] = []
        for r in visible:
            key = notification_key(SRC_RISK, r.id)
            out.append(Notification(
                key=key, source=SRC_RISK, source_id=r.id,
                title=f"风险预警：{r.rule_code}",
                category=r.rule_code,
                occurred_at=r.first_seen_at,
                deep_link=f"/risks/{r.id}",
                read=key in read_keys,
            ))
        return out

    # ============================ 内部工具 ============================

    def _read_keys(self, user_id: UUID) -> set[str]:
        rows = self.session.scalars(
            select(NotificationRead.notification_key).where(
                NotificationRead.user_id == user_id
            )
        ).all()
        return set(rows)

    def _upsert_read(self, user_id: UUID, key: str) -> None:
        # 唯一约束幂等：已存在则不更新 read_at（保留首次已读时间）
        stmt = (
            pg_insert(NotificationRead)
            .values(user_id=user_id, notification_key=key)
            .on_conflict_do_nothing(
                constraint="uq_notification_read_user_key"
            )
        )
        self.session.execute(stmt)
        self.session.flush()

    def _is_visible(
        self, actor: ActorContext, source: str, source_id: UUID
    ) -> bool:
        """校验某通知确实属于当前用户可见范围（越权插已读直接拒）。"""
        if source == SRC_TASK:
            t = self.session.get(TaskItem, source_id)
            return t is not None and t.assignee_id == actor.user_id and (
                t.status in (TaskStatus.OPEN.value, TaskStatus.IN_PROGRESS.value)
            )
        if source == SRC_APPROVAL:
            a = self.session.get(ApprovalRequest, source_id)
            return (
                a is not None
                and a.status == ApprovalStatus.PENDING.value
                and a.matrix_role_code == actor.role_code
                and (a.approver_id is None or a.approver_id == actor.user_id)
            )
        if source == SRC_RISK:
            r = self.session.get(RiskItem, source_id)
            if r is None or r.status != RiskStatus.OPEN.value:
                return False
            from app.services.risk_service import RiskService

            return r in RiskService(self.session).filter_by_scope(actor, [r])
        return False


def _task_category(task_kind: str) -> str:
    if task_kind.startswith("VERIFICATION::"):
        return "TASK_VERIFICATION"
    if "RISK" in task_kind.upper():
        return "TASK_RISK"
    return "TASK_TODO"


def _neg_ts(dt: datetime) -> float:
    # 时间倒序但保持 naive/aware 可比：统一取 timestamp
    return -dt.timestamp()
