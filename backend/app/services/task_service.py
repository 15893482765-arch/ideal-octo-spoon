"""TaskService（T05 / PRD §4.5 撤柜任务、§4.6 排班任务、§4.8 整改、§4.10 工资；
R27 幂等创建；红线 复核任务不可直接 complete）。

能力：
- 派发 / 完成 / 交接 / 批量转移；
- 幂等创建：以 (related_object_type, related_object_id, task_kind, assignee_id) 唯一
  （同业务对象/类型/责任人且未关闭的不重复创建；R27；T09 起纳入责任人，
  使续签评估可为业务员与经理各生成一条，R38/RK-6）；
- **复核任务**（task_kind 以 `VERIFICATION::` 前缀或 `requires_related_document=True`
  标记）禁止 `complete()`，必须由关联单据动作通过 `close_by_document()` 关闭
  （Q023）；
- 交接留痕：transfer 写 audit 记录（actor、原因、原/新责任人、时间，Q024）；
- 完成来源留痕：DIRECT / RELATED_DOCUMENT / SYSTEM / OTHER。
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import ConflictError, DomainError
from app.models import TaskItem
from app.models.enums import TaskStatus
from app.services.audit_service import AuditService

TASK_KIND_VISIT_PLANNED = "VISIT_PLANNED"
TASK_KIND_CONTRACT_RENEWAL_REVIEW = "CONTRACT_RENEWAL_REVIEW"
TASK_KIND_FREEZER_RECALL = "FREEZER_RECALL"
TASK_KIND_FREEZER_ACCEPTANCE = "FREEZER_ACCEPTANCE"
TASK_KIND_FREEZER_DEPOSIT_REFUND = "FREEZER_DEPOSIT_REFUND"
TASK_KIND_REPLENISHMENT_REVIEW = "REPLENISHMENT_REVIEW"
TASK_KIND_PROBLEM_RECTIFICATION = "PROBLEM_RECTIFICATION"
TASK_KIND_SUPERVISION_RECTIFICATION = "SUPERVISION_RECTIFICATION"
TASK_KIND_PROBLEM_RECTIFICATION_VERIFY = "PROBLEM_RECTIFICATION_VERIFY"
TASK_KIND_CONTRACT_RENEWAL_VERIFY = "CONTRACT_RENEWAL_VERIFY"
TASK_KIND_CONTRACT_EXPIRING_RENEWAL_EVAL = "CONTRACT_EXPIRING_RENEWAL_EVAL"
# T15：地推活动复盘时样品未归还/缺失的提醒待办
TASK_KIND_CAMPAIGN_SAMPLE_RETURN = "CAMPAIGN_SAMPLE_RETURN"
# T21 / PRD §4.11：新生成的未处理风险自动进入片区经理待办
TASK_KIND_RISK_HANDLING = "RISK_HANDLING"
TASK_KIND_OTHER = "OTHER"

# 复核任务：以 `VERIFICATION::` 前缀标记；create() 自动设 requires_related_document=True
VERIFICATION_PREFIX = "VERIFICATION::"

# 完成来源
SOURCE_DIRECT = "DIRECT"
SOURCE_RELATED_DOCUMENT = "RELATED_DOCUMENT"
SOURCE_SYSTEM = "SYSTEM"
SOURCE_OTHER = "OTHER"

_ALLOWED_TASK_KINDS = frozenset(
    {
        TASK_KIND_VISIT_PLANNED,
        TASK_KIND_CONTRACT_RENEWAL_REVIEW,
        TASK_KIND_FREEZER_RECALL,
        TASK_KIND_FREEZER_ACCEPTANCE,
        TASK_KIND_FREEZER_DEPOSIT_REFUND,
        TASK_KIND_REPLENISHMENT_REVIEW,
        TASK_KIND_PROBLEM_RECTIFICATION,
        TASK_KIND_SUPERVISION_RECTIFICATION,
        TASK_KIND_PROBLEM_RECTIFICATION_VERIFY,
        TASK_KIND_CONTRACT_RENEWAL_VERIFY,
        TASK_KIND_CONTRACT_EXPIRING_RENEWAL_EVAL,
        TASK_KIND_CAMPAIGN_SAMPLE_RETURN,
        TASK_KIND_RISK_HANDLING,
        TASK_KIND_OTHER,
    }
)


def _is_verification_kind(task_kind: str) -> bool:
    return task_kind.startswith(VERIFICATION_PREFIX) or task_kind in {
        TASK_KIND_PROBLEM_RECTIFICATION_VERIFY,
        TASK_KIND_CONTRACT_RENEWAL_VERIFY,
    }


class TaskService:
    def __init__(self, session: Session) -> None:
        self.session = session

    # -------- 派发 / 幂等创建 --------

    def assign(
        self,
        *,
        title: str,
        assignee_id: UUID,
        task_kind: str,
        related_object_type: str | None = None,
        related_object_id: UUID | None = None,
        description: str | None = None,
    ) -> TaskItem:
        """派发任务；同 (object_type, object_id, task_kind) + 未关闭不重复创建。

        - R27：同业务对象/类型/责任人且未关闭的任务不得重复创建；
        - 同一 (object_type, object_id, task_kind) 已有 OPEN/IN_PROGRESS → 409；
        - 复核任务以 task_kind 前缀或 `requires_related_document=True` 标记。
        """
        if task_kind not in _ALLOWED_TASK_KINDS:
            raise DomainError(
                code="TASK_KIND_INVALID",
                message="任务类型不在白名单",
                details={"task_kind": task_kind},
            )
        if (related_object_type is None) != (related_object_id is None):
            raise DomainError(
                code="TASK_RELATION_INCOMPLETE",
                message="related_object_type/related_object_id 必须同时给出或同时为空",
            )
        requires_doc = _is_verification_kind(task_kind)

        # 先查重（先查再插避免 IntegrityError 触发 session.rollback 吞掉事务）
        # T09：幂等维度含责任人，同对象同类型不同责任人可各持一条（R38 续签待办）
        existing = self._find_idempotent(
            related_object_type, related_object_id, task_kind, assignee_id
        )
        if existing is not None:
            raise ConflictError(
                code="TASK_IDEMPOTENT_DUPLICATE",
                message="该业务对象的同类型任务已存在且未关闭",
                details={
                    "task_id": str(existing.id),
                    "task_kind": task_kind,
                    "status": existing.status,
                },
            )

        item = TaskItem(
            title=title,
            description=description,
            assignee_id=assignee_id,
            task_kind=task_kind,
            related_object_type=related_object_type,
            related_object_id=related_object_id,
            status=TaskStatus.OPEN.value,
            requires_related_document=requires_doc,
        )
        self.session.add(item)
        try:
            self.session.flush()
        except IntegrityError as exc:
            # 并发竞态：唯一索引兜底拒绝；不会回滚调用方事务
            raise ConflictError(
                code="TASK_IDEMPOTENT_DUPLICATE",
                message="该业务对象的同类型任务已存在且未关闭",
                details={"task_kind": task_kind},
            ) from exc
        return item

    def assign_idempotent(
        self,
        *,
        title: str,
        assignee_id: UUID,
        task_kind: str,
        related_object_type: str | None = None,
        related_object_id: UUID | None = None,
        description: str | None = None,
    ) -> tuple[TaskItem, bool]:
        """幂等派发（R27）：已存在未关闭同类任务则原样返回（created=False）。

        与 `assign` 的区别：不抛 409，而是返回既有任务。供 T21 风险→经理待办等
        自动派发场景使用（同一风险重复扫描不重复建待办）。
        """
        existing = self._find_idempotent(
            related_object_type, related_object_id, task_kind, assignee_id
        )
        if existing is not None:
            return existing, False
        try:
            item = self.assign(
                title=title,
                assignee_id=assignee_id,
                task_kind=task_kind,
                related_object_type=related_object_type,
                related_object_id=related_object_id,
                description=description,
            )
        except ConflictError:
            again = self._find_idempotent(
                related_object_type, related_object_id, task_kind, assignee_id
            )
            if again is None:
                raise
            return again, False
        return item, True

    def _find_idempotent(
        self,
        object_type: str | None,
        object_id: UUID | None,
        task_kind: str,
        assignee_id: UUID | None = None,
    ) -> TaskItem | None:
        stmt = select(TaskItem).where(
            TaskItem.task_kind == task_kind,
            TaskItem.status.in_([TaskStatus.OPEN.value, TaskStatus.IN_PROGRESS.value]),
        )
        if assignee_id is not None:
            stmt = stmt.where(TaskItem.assignee_id == assignee_id)
        if object_type is None and object_id is None:
            stmt = stmt.where(TaskItem.related_object_type.is_(None))
            stmt = stmt.where(TaskItem.related_object_id.is_(None))
        else:
            stmt = stmt.where(TaskItem.related_object_type == object_type)
            stmt = stmt.where(TaskItem.related_object_id == object_id)
        return self.session.execute(stmt).scalars().first()

    # -------- 完成 --------

    def complete(
        self,
        *,
        task_id: UUID,
        actor_id: UUID,
        reason: str | None = None,
    ) -> TaskItem:
        """完成任务。**复核任务禁止直接 complete**（Q023）。

        - 复核任务：必须使用 `close_by_document()`，由关联单据动作关闭；
        - 已 CLOSED：直接拒（幂等防御）；
        - 同事务审计：before/after 写入 audit_logs。
        """
        item = self.session.get(TaskItem, task_id)
        if item is None:
            raise DomainError(code="TASK_NOT_FOUND", message="任务不存在")
        if item.status == TaskStatus.CLOSED.value:
            raise ConflictError(
                code="TASK_ALREADY_CLOSED",
                message="任务已关闭",
                details={"task_id": str(task_id)},
            )
        if item.requires_related_document:
            raise DomainError(
                code="TASK_VERIFICATION_REQUIRES_DOCUMENT",
                message="复核任务不得直接完成；请通过关联单据动作关闭",
                details={
                    "task_id": str(task_id),
                    "task_kind": item.task_kind,
                    "related_object_type": item.related_object_type,
                    "related_object_id": str(item.related_object_id)
                    if item.related_object_id
                    else None,
                },
            )

        before = {"status": item.status, "assignee_id": str(item.assignee_id)}
        item.status = TaskStatus.CLOSED.value
        item.completion_source = SOURCE_DIRECT
        item.closed_at = datetime.now(tz=UTC)
        item.closed_by_id = actor_id
        self.session.flush()
        AuditService(self.session).record(
            object_type="TASK",
            object_id=item.id,
            event_type="STATE_TRANSITION",
            actor_id=actor_id,
            before=before,
            after={
                "status": item.status,
                "assignee_id": str(item.assignee_id),
                "completion_source": item.completion_source,
            },
            reason=reason or "任务直接完成",
        )
        return item

    # -------- 复核任务：关联单据动作关闭（Q023）--------

    def close_by_document(
        self,
        *,
        task_id: UUID,
        actor_id: UUID,
        document_object_type: str,
        document_object_id: UUID,
        outcome: str,
        reason: str | None = None,
    ) -> TaskItem:
        """复核任务关闭入口：由关联单据动作触发（Q023）。

        - 不强制 task_kind 在复核集合内（业务上配置了 requires_related_document
          即视为复核任务）；非复核任务可调用但会记录 source=RELATED_DOCUMENT；
        - outcome：业务结果（如 VERIFIED / CLOSED / APPROVED），写审计。
        """
        item = self.session.get(TaskItem, task_id)
        if item is None:
            raise DomainError(code="TASK_NOT_FOUND", message="任务不存在")
        if item.status == TaskStatus.CLOSED.value:
            raise ConflictError(
                code="TASK_ALREADY_CLOSED",
                message="任务已关闭",
                details={"task_id": str(task_id)},
            )
        # 关联单据与任务的业务对象必须一致（避免误关）
        if (
            item.related_object_type is not None
            and item.related_object_type != document_object_type
        ) or (
            item.related_object_id is not None
            and item.related_object_id != document_object_id
        ):
            raise DomainError(
                code="TASK_DOCUMENT_MISMATCH",
                message="关闭任务时传入的单据与任务关联对象不一致",
                details={
                    "expected_type": item.related_object_type,
                    "expected_id": str(item.related_object_id)
                    if item.related_object_id
                    else None,
                    "got_type": document_object_type,
                    "got_id": str(document_object_id),
                },
            )

        before = {"status": item.status, "assignee_id": str(item.assignee_id)}
        item.status = TaskStatus.CLOSED.value
        item.completion_source = SOURCE_RELATED_DOCUMENT
        item.closed_at = datetime.now(tz=UTC)
        item.closed_by_id = actor_id
        self.session.flush()
        AuditService(self.session).record(
            object_type="TASK",
            object_id=item.id,
            event_type="STATE_TRANSITION",
            actor_id=actor_id,
            before=before,
            after={
                "status": item.status,
                "assignee_id": str(item.assignee_id),
                "completion_source": item.completion_source,
                "document_object_type": document_object_type,
                "document_object_id": str(document_object_id),
                "outcome": outcome,
            },
            reason=reason or f"关联单据 {document_object_type} 关闭复核任务",
        )
        return item

    # -------- 交接留痕（Q024）--------

    def transfer(
        self,
        *,
        task_id: UUID,
        new_assignee_id: UUID,
        actor_id: UUID,
        reason: str,
    ) -> TaskItem:
        """任务交接：必须填原因；同事务写审计（before/after 含原/新责任人）。"""
        if not reason or not reason.strip():
            raise DomainError(code="TASK_TRANSFER_REASON_REQUIRED", message="交接必须填写原因")
        item = self.session.get(TaskItem, task_id)
        if item is None:
            raise DomainError(code="TASK_NOT_FOUND", message="任务不存在")
        if item.status == TaskStatus.CLOSED.value:
            raise ConflictError(
                code="TASK_ALREADY_CLOSED",
                message="已关闭任务不可交接",
            )
        if item.assignee_id == new_assignee_id:
            raise DomainError(
                code="TASK_TRANSFER_SAME_ASSIGNEE",
                message="新责任人必须与当前不同",
            )

        before = {"status": item.status, "assignee_id": str(item.assignee_id)}
        item.assignee_id = new_assignee_id
        if item.status == TaskStatus.OPEN.value:
            item.status = TaskStatus.IN_PROGRESS.value
        self.session.flush()
        AuditService(self.session).record(
            object_type="TASK",
            object_id=item.id,
            event_type="UPDATE",
            actor_id=actor_id,
            before=before,
            after={
                "status": item.status,
                "assignee_id": str(new_assignee_id),
            },
            reason=reason,
        )
        return item

    def batch_transfer(
        self,
        *,
        task_ids: list[UUID],
        new_assignee_id: UUID,
        actor_id: UUID,
        reason: str,
    ) -> list[TaskItem]:
        """批量交接：同事务内完成；任一失败全部回滚。"""
        if not task_ids:
            return []
        items: list[TaskItem] = []
        for tid in task_ids:
            items.append(
                self.transfer(
                    task_id=tid,
                    new_assignee_id=new_assignee_id,
                    actor_id=actor_id,
                    reason=reason,
                )
            )
        return items

    # -------- 状态迁移 --------

    def mark_in_progress(
        self, *, task_id: UUID, actor_id: UUID, reason: str | None = None
    ) -> TaskItem:
        item = self.session.get(TaskItem, task_id)
        if item is None:
            raise DomainError(code="TASK_NOT_FOUND", message="任务不存在")
        if item.status == TaskStatus.CLOSED.value:
            raise ConflictError(code="TASK_ALREADY_CLOSED", message="已关闭任务不可变更")
        if item.status == TaskStatus.IN_PROGRESS.value:
            return item
        before = {"status": item.status}
        item.status = TaskStatus.IN_PROGRESS.value
        self.session.flush()
        AuditService(self.session).record(
            object_type="TASK",
            object_id=item.id,
            event_type="STATE_TRANSITION",
            actor_id=actor_id,
            before=before,
            after={"status": item.status},
            reason=reason or "任务进入处理中",
        )
        return item


# 工具方法：列出 task kind（供 UI/审计使用）
def all_task_kinds() -> frozenset[str]:
    return _ALLOWED_TASK_KINDS
