"""T14 商家调拨与业绩分界（PRD §4.7 商家责任转移、R9、A8；§4.3 归属字段）。

职责：
- 调拨预览：原/新负责人、生效日、未完成任务、待办、临时授权、合同与绩效分界；
- 生效（apply）：生效日（含，上海业务日 00:00）起切换 MerchantAssignment 主归属区间，
  未完成巡访任务与 TaskItem 待办批量转新负责人（复用 T05 TaskService.batch_transfer）；
  已完成事实、已提交记录一律不回写（业绩按事实发生时点归属，owner 快照不变）；
- 生效前可撤回；生效后不可覆盖，只能新建反向调拨。

红线：
- 行级范围（dataset_id）一律由服务端实体派生，不接受客户端/模型指定；
- 与审计同事务、不自行 commit（事务归 Router）；提交类操作幂等。
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.core.errors import (
    ConflictError,
    DomainError,
    PermissionDeniedError,
)
from app.core.permissions import MODULE_MERCHANT, permission_service
from app.models import Merchant, MerchantTransfer, User
from app.models.enums import UserStatus
from app.models.merchant_transfer import (
    TRANSFER_EFFECTIVE,
    TRANSFER_PENDING,
    TRANSFER_WITHDRAWN,
)
from app.repositories.transfer_repository import TransferRepository
from app.services.audit_service import (
    EVENT_CREATE,
    AuditService,
)
from app.services.task_service import TaskService

SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
logger = logging.getLogger(__name__)


def shanghai_day_start_utc(day: date) -> datetime:
    """上海业务日 00:00 → UTC（含生效日）。"""
    return datetime.combine(day, time(0, 0), tzinfo=SHANGHAI_TZ).astimezone(UTC)


class TransferService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.repo = TransferRepository(session)
        self.audit = AuditService(session)

    # ----------------------------- 预览 -----------------------------
    def preview(
        self, actor, *, merchant_id: UUID, from_user_id: UUID,
        to_user_id: UUID, effective_date: date,
    ) -> dict[str, Any]:
        """调拨预览：纯读，不写库。"""
        merchant, from_user, to_user = self._load_actors(
            merchant_id, from_user_id, to_user_id)
        effective_at = shanghai_day_start_utc(effective_date)

        open_visits = self.repo.open_visit_tasks(merchant_id, from_user_id)
        open_tasks = self.repo.open_tasks(merchant_id, from_user_id)
        temps = self.repo.active_temporary_assignments(merchant_id, effective_at)
        contracts = self.repo.contracts_boundary(merchant_id, merchant.dataset_id)

        return {
            "merchant_id": merchant_id,
            "merchant_name": merchant.name,
            "from_user": {"id": from_user.id, "display_name": from_user.display_name},
            "to_user": {"id": to_user.id, "display_name": to_user.display_name},
            "effective_date": effective_date.isoformat(),
            "effective_at_utc": effective_at.isoformat(),
            "open_visit_tasks": [
                {"id": t.id, "status": t.status,
                 "planned_date": t.planned_date.isoformat() if t.planned_date else None}
                for t in open_visits
            ],
            "open_todo_count": len(open_tasks),
            "open_visit_count": len(open_visits),
            "temporary_grants": [
                {"user_id": a.owner_user_id,
                 "expires_at": _upper_utc(a.valid_range)}
                for a in temps
            ],
            "contracts": [
                {"id": c.id, "code": c.code, "status": c.status}
                for c in contracts
            ],
            "performance_boundary": (
                "生效日（含）起的新事实归新负责人；生效前已完成事实不回写，归原负责人"
            ),
        }

    # ----------------------------- 表单选项 -----------------------------
    def options(self, actor, *, merchant_id: UUID) -> dict[str, Any]:
        """调拨表单选项（只读）：当前主负责人 + 本片区可承接的在职业务员。"""
        permission_service.require_ability(actor, MODULE_MERCHANT)
        merchant = self.repo.get_merchant(merchant_id)
        if merchant is None:
            raise DomainError(code="MERCHANT_NOT_FOUND", message="商家不存在")
        self._assert_region(actor, merchant)

        assignment = self.repo.primary_assignment(
            merchant_id, merchant.dataset_id, datetime.now(UTC)
        )
        from_user = (
            self.repo.get_user(assignment.owner_user_id) if assignment else None
        )
        candidates = self.repo.list_transfer_candidates(
            merchant.dataset_id, merchant.region_id
        )
        return {
            "merchant_id": merchant_id,
            "from_user": (
                {"id": from_user.id, "display_name": from_user.display_name}
                if from_user is not None
                else None
            ),
            "items": [
                {"id": u.id, "display_name": u.display_name} for u in candidates
            ],
        }

    # ----------------------------- 创建调拨单 -----------------------------
    def create_transfer(
        self, actor, *, merchant_id: UUID, from_user_id: UUID, to_user_id: UUID,
        effective_date: date, reason: str,
    ) -> MerchantTransfer:
        permission_service.require_ability(actor, MODULE_MERCHANT)
        if not reason or not reason.strip():
            raise DomainError(
                code="TRANSFER_REASON_REQUIRED", message="调拨必须填写原因")
        merchant, _from, to_user = self._load_actors(
            merchant_id, from_user_id, to_user_id)
        self._assert_region(actor, merchant)

        if to_user.status != UserStatus.ACTIVE.value:
            raise DomainError(
                code="TRANSFER_TARGET_INACTIVE", message="新负责人账号未激活，无法承接")

        # 同一商家同时只允许一张未完成调拨单（DB 部分唯一索引兜底并发）
        existing = self.repo.find_open_transfer(merchant_id, merchant.dataset_id)
        if existing is not None:
            raise ConflictError(
                code="TRANSFER_ALREADY_OPEN",
                message="该商家已有进行中的调拨单，请先撤回或完成后再操作")

        # 校验“现在”的主负责人确为 from_user（归属区间在 apply 时才切换；
        # 反向调拨时当前主负责人正是上一张单的新负责人 B）
        current = self.repo.current_primary_interval(
            merchant_id, from_user_id, merchant.dataset_id, datetime.now(UTC))
        if current is None:
            raise DomainError(
                code="TRANSFER_FROM_NOT_OWNER",
                message="原负责人当前不是该商家的主负责人，无法发起调拨")

        transfer = MerchantTransfer(
            merchant_id=merchant_id,
            from_user_id=from_user_id,
            to_user_id=to_user_id,
            effective_date=effective_date,
            effective_at=shanghai_day_start_utc(effective_date),
            status=TRANSFER_PENDING,
            reason=reason.strip(),
            # 数据域随商家实体派生（红线：行级范围不由客户端/常量指定）；
            # 之前硬编码 PRODUCTION 会让 DEMO 商家的调拨单与其归属区间跨域。
            dataset_type=merchant.dataset_type,
            dataset_id=merchant.dataset_id,
            created_by_id=actor.user_id,
            updated_by_id=actor.user_id,
        )
        self.session.add(transfer)
        self.session.flush()
        self.audit.record(
            object_type="MERCHANT_TRANSFER", object_id=transfer.id,
            event_type=EVENT_CREATE, actor_id=actor.user_id,
            after={"from": str(from_user_id), "to": str(to_user_id),
                   "effective_date": effective_date.isoformat(),
                   "status": transfer.status},
        )
        return transfer

    def create_request(
        self,
        actor,
        *,
        merchant_id: UUID,
        to_user_id: UUID,
        reason: str,
        effective_date: date | None = None,
    ) -> MerchantTransfer:
        """MCP 受控入口：发起调拨**申请**（不生效、不改归属）（T24/PRD §5.5）。

        原负责人由服务端按商家当前主归属区间解析，模型不能伪造 from_user；
        默认次日生效。仅落 PENDING 申请，真正归属切换必须经既有审批/生效流程，
        AI 无法“静默调拨”。
        """
        merchant = self.repo.get_merchant(merchant_id)
        if merchant is None:
            raise DomainError(
                code="MERCHANT_NOT_FOUND", message="商家不存在",
                details={"merchant_id": str(merchant_id)})
        assignment = self.repo.primary_assignment(
            merchant_id, merchant.dataset_id, datetime.now(UTC)
        )
        if assignment is None:
            raise ConflictError(
                code="TRANSFER_NO_CURRENT_OWNER",
                message="该商家当前无主负责人，无法发起调拨",
            )
        effective = effective_date or (date.today() + timedelta(days=1))
        return self.create_transfer(
            actor,
            merchant_id=merchant_id,
            from_user_id=assignment.owner_user_id,
            to_user_id=to_user_id,
            effective_date=effective,
            reason=reason,
        )

    # ----------------------------- 生效 -----------------------------
    def apply(self, actor, *, transfer_id: UUID) -> MerchantTransfer:
        """执行调拨生效：区间切换 + 未完成任务/待办批量转移。幂等。"""
        permission_service.require_ability(actor, MODULE_MERCHANT)
        transfer = self._get(transfer_id)
        merchant = self.repo.get_merchant(transfer.merchant_id)
        self._assert_region(actor, merchant)

        if transfer.status == TRANSFER_EFFECTIVE:
            return transfer  # 幂等：重复生效不重复转移
        if transfer.status == TRANSFER_WITHDRAWN:
            raise ConflictError(
                code="TRANSFER_WITHDRAWN", message="调拨已撤回，无法生效")
        if transfer.status not in ("PENDING", "SCHEDULED"):
            raise ConflictError(
                code="TRANSFER_NOT_APPLICABLE", message="当前调拨单状态不允许生效")

        effective_at = transfer.effective_at or shanghai_day_start_utc(
            transfer.effective_date)
        before_status = transfer.status

        # 1) 归属区间分界：在生效时点命中的旧负责人主区间截断为 [..., effective_at)
        old_interval = self.repo.current_primary_interval(
            transfer.merchant_id, transfer.from_user_id, transfer.dataset_id,
            effective_at)
        if old_interval is not None:
            self.repo.close_primary_interval(old_interval, effective_at)
        # 新负责人主归属 [effective_at, ∞)
        self.repo.insert_primary_interval(
            merchant_id=transfer.merchant_id, owner_user_id=transfer.to_user_id,
            dataset_id=transfer.dataset_id, start=effective_at,
            dataset_type=transfer.dataset_type)

        # 2) 未完成巡访任务批量转新负责人（历史 COMPLETED / 已提交记录不动）
        open_visits = self.repo.open_visit_tasks(
            transfer.merchant_id, transfer.from_user_id)
        for task in open_visits:
            task.assigned_user_id = transfer.to_user_id

        # 2.1) 今后的排班一并改派（条目/占用/每周模板）：
        # 之前只转任务不改排班，导致原负责人"排班还在、巡访计划里已没有"。
        # 改派失败不应阻断调拨本身（业务事实优先），结果落到交接快照里可追溯。
        schedule_reassigned = self._reassign_schedule(
            transfer, effective_at=effective_at, actor_id=actor.user_id)

        # 3) 待办调用 T05 批量转移（未关闭的商家相关待办）
        open_todos = self.repo.open_tasks(transfer.merchant_id, transfer.from_user_id)
        moved_items = TaskService(self.session).batch_transfer(
            task_ids=[t.id for t in open_todos],
            new_assignee_id=transfer.to_user_id,
            actor_id=actor.user_id,
            reason=f"商家调拨生效 {transfer_id}",
        )

        # 4) 临时授权交接：生效时点仍有效的临时授权保留（PRD：交接临时授权）；
        #    已到期的自然不再命中。统计供快照。
        temps = self.repo.active_temporary_assignments(
            transfer.merchant_id, effective_at)

        transfer.status = TRANSFER_EFFECTIVE
        transfer.effective_at = effective_at
        transfer.effective_by_id = actor.user_id
        transfer.updated_by_id = actor.user_id
        transfer.handover_json = {
            "open_visit_task_ids": [str(t.id) for t in open_visits],
            "open_todo_count": len(moved_items),
            "temporary_grant_user_ids": [str(a.owner_user_id) for a in temps],
            "schedule_reassigned": schedule_reassigned,
        }
        self.session.flush()

        self.audit.record_state_transition(
            object_type="MERCHANT_TRANSFER", object_id=transfer.id,
            actor_id=actor.user_id,
            before={"status": before_status},
            after={"status": TRANSFER_EFFECTIVE,
                   "open_visits_moved": len(open_visits),
                   "open_todos_moved": len(moved_items)},
            reason=transfer.reason,
        )
        return transfer

    def _reassign_schedule(
        self, transfer: MerchantTransfer, *, effective_at, actor_id
    ) -> dict:
        """调拨生效时改派今后排班（SAVEPOINT 隔离，失败不污染调拨事务）。"""
        from app.services.schedule_service import ScheduleService

        savepoint = self.session.begin_nested()
        try:
            result = ScheduleService(self.session).reassign_merchant_schedule(
                merchant_id=transfer.merchant_id,
                from_user_id=transfer.from_user_id,
                to_user_id=transfer.to_user_id,
                effective_at=effective_at,
                actor_id=actor_id,
            )
            savepoint.commit()
            return result
        except Exception as exc:  # noqa: BLE001 - 排班是附属事实，不阻断调拨
            savepoint.rollback()
            logger.warning(
                "transfer %s schedule reassign failed: %s", transfer.id, type(exc).__name__
            )
            return {
                "entries": 0,
                "reservations": 0,
                "templates": 0,
                "skipped": [],
                "error": type(exc).__name__,
            }

    # ----------------------------- 撤回（生效前） -----------------------------
    def withdraw(self, actor, *, transfer_id: UUID, reason: str) -> MerchantTransfer:
        permission_service.require_ability(actor, MODULE_MERCHANT)
        if not reason or not reason.strip():
            raise DomainError(
                code="TRANSFER_WITHDRAW_REASON_REQUIRED", message="撤回必须填写原因")
        transfer = self._get(transfer_id)
        merchant = self.repo.get_merchant(transfer.merchant_id)
        self._assert_region(actor, merchant)

        if transfer.status == TRANSFER_EFFECTIVE:
            # R9：生效后不可撤回/覆盖，只能新建反向调拨
            raise ConflictError(
                code="TRANSFER_ALREADY_EFFECTIVE",
                message="调拨已生效，不能撤回或覆盖；请新建反向调拨")
        if transfer.status == TRANSFER_WITHDRAWN:
            return transfer  # 幂等
        if transfer.status not in ("PENDING", "SCHEDULED"):
            raise ConflictError(
                code="TRANSFER_NOT_WITHDRAWABLE", message="当前调拨单不可撤回")

        before = transfer.status
        transfer.status = TRANSFER_WITHDRAWN
        transfer.withdrawn_at = datetime.now(UTC)
        transfer.withdrawn_by_id = actor.user_id
        transfer.withdraw_reason = reason.strip()
        transfer.updated_by_id = actor.user_id
        self.session.flush()
        self.audit.record_state_transition(
            object_type="MERCHANT_TRANSFER", object_id=transfer.id,
            actor_id=actor.user_id,
            before={"status": before},
            after={"status": TRANSFER_WITHDRAWN},
            reason=reason.strip(),
        )
        return transfer

    # ----------------------------- 反向调拨（生效后） -----------------------------
    def create_reverse(
        self, actor, *, original_id: UUID, effective_date: date, reason: str
    ) -> MerchantTransfer:
        """生效后唯一允许的回退方式：新建一张反向调拨单（新/原负责人对调）。"""
        permission_service.require_ability(actor, MODULE_MERCHANT)
        original = self._get(original_id)
        if original.status != TRANSFER_EFFECTIVE:
            raise ConflictError(
                code="TRANSFER_REVERSE_ONLY_AFTER_EFFECTIVE",
                message="只有已生效的调拨单才能创建反向调拨；未生效请直接撤回")
        reverse = self.create_transfer(
            actor,
            merchant_id=original.merchant_id,
            from_user_id=original.to_user_id,
            to_user_id=original.from_user_id,
            effective_date=effective_date,
            reason=reason,
        )
        reverse.reverses_id = original.id
        original.reversed_by_id = reverse.id
        self.session.flush()
        return reverse

    # ----------------------------- 查询 -----------------------------
    def owner_at(
        self, *, merchant_id: UUID, dataset_id: UUID, at: datetime
    ) -> UUID | None:
        """归属解析：某一时点的主负责人（区间命中）。供绩效分界与测试断言。"""
        a = self.repo.primary_assignment(merchant_id, dataset_id, at)
        return a.owner_user_id if a else None

    # ----------------------------- 私有 -----------------------------
    def _get(self, transfer_id: UUID) -> MerchantTransfer:
        t = self.repo.get_transfer(transfer_id)
        if t is None:
            raise DomainError(
                code="TRANSFER_NOT_FOUND", message="调拨单不存在")
        return t

    def _load_actors(
        self, merchant_id: UUID, from_user_id: UUID, to_user_id: UUID
    ) -> tuple[Merchant, User, User]:
        if from_user_id == to_user_id:
            raise DomainError(
                code="TRANSFER_SAME_USER", message="原负责人与新负责人不能相同")
        merchant = self.repo.get_merchant(merchant_id)
        if merchant is None:
            raise DomainError(
                code="MERCHANT_NOT_FOUND", message="商家不存在")
        from_user = self.repo.get_user(from_user_id)
        to_user = self.repo.get_user(to_user_id)
        if from_user is None or to_user is None:
            raise DomainError(
                code="TRANSFER_USER_NOT_FOUND", message="原/新负责人不存在")
        return merchant, from_user, to_user

    @staticmethod
    def _assert_region(actor, merchant: Merchant) -> None:
        if merchant.region_id not in actor.region_ids:
            raise PermissionDeniedError(
                code="REGION_SCOPE_DENIED",
                message="无权操作该片区的商家",
                details={"merchant_id": str(merchant.id)},
            )


def _upper_utc(range_value: Any) -> str | None:
    """tstzrange 上界（到期时刻）转 ISO 字符串；开放区间为 None。"""
    if range_value is None:
        return None
    upper = getattr(range_value, "upper", None)
    if upper is None:
        return None
    if upper.tzinfo is None:
        upper = upper.replace(tzinfo=UTC)
    return upper.astimezone(UTC).isoformat()
