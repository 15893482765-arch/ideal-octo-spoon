"""合同到期续签评估待办生成（T09 / PRD §4.4、R38、RK-6、验收 A26）。

规则：
- 生效合同到期前 N 天（settings.contract_renewal_remind_days，默认 30，可配置）
  自动为责任业务员与片区经理**各生成一条**“续签评估”待办；
- 同一合同同一到期周期只生成一次（幂等）：以合同上的
  `renewal_generated_for_expire_at` 标记 + TaskService 的
  (对象,类型,责任人) 未关闭唯一约束双重保证；
- 首版只提醒，不自动续签（续签/电子签约在二期）；
- 待办完成前合同与冰柜状态不变（本服务不改合同状态，Q047/A26）。

触发方式：可由定时任务（APS）或管理员手动调用 `run_renewal_scan()`，结果幂等，
重复扫描不会重复生成。
"""

from __future__ import annotations

from datetime import date, timedelta
from uuid import UUID

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import ConflictError
from app.models import Contract
from app.repositories.contract_repository import ContractRepository
from app.services.audit_service import EVENT_CREATE, AuditService
from app.services.task_service import (
    TASK_KIND_CONTRACT_EXPIRING_RENEWAL_EVAL,
    TaskService,
)


class RenewalService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.repo = ContractRepository(session)
        self._tasks = TaskService(session)

    def generate_for_contract(
        self,
        contract: Contract,
        *,
        today: date | None = None,
        remind_days: int | None = None,
    ) -> list[UUID]:
        """为单份合同生成续签评估待办（幂等）。返回本次新建的任务 id 列表。

        幂等条件：同一合同 + 同一到期日只处理一次；已生成则直接返回空列表。
        重复触发（含并发）由 TaskService 未关闭唯一索引兜底。
        """
        today = today or date.today()
        days = remind_days if remind_days is not None else (
            get_settings().contract_renewal_remind_days
        )
        if contract.status != "EFFECTIVE" or contract.expire_at is None:
            return []
        deadline = today + timedelta(days=days)
        # 尚未进入提醒窗口：不生成
        if not (today <= contract.expire_at <= deadline):
            return []
        # 同一到期周期已生成过：幂等跳过
        if contract.renewal_generated_for_expire_at == contract.expire_at:
            return []

        # 责任人：责任业务员 + 片区经理；缺失者跳过（至少有其一）
        assignees: list[UUID] = []
        for uid in (contract.sales_rep_user_id, contract.manager_user_id):
            if uid is not None and uid not in assignees:
                assignees.append(uid)

        created: list[UUID] = []
        for uid in assignees:
            task_id = self._safe_assign(contract, uid)
            if task_id is not None:
                created.append(task_id)

        # 标记本到期周期已生成（即便责任人缺失也标记，避免每次扫描空转）
        contract.renewal_generated_for_expire_at = contract.expire_at
        AuditService(self.session).record(
            object_type="contract",
            object_id=contract.id,
            actor_id=None,
            event_type=EVENT_CREATE,
            after={
                "renewal_eval": True,
                "expire_at": contract.expire_at.isoformat(),
                "tasks": [str(t) for t in created],
            },
            reason=f"到期前 {days} 天自动生成续签评估待办（R38）",
        )
        self.session.flush()
        return created

    def run_renewal_scan(
        self,
        *,
        today: date | None = None,
        remind_days: int | None = None,
    ) -> dict:
        """扫描全部进入提醒窗口的生效合同并幂等生成待办（定时任务入口）。"""
        today = today or date.today()
        days = remind_days if remind_days is not None else (
            get_settings().contract_renewal_remind_days
        )
        deadline = today + timedelta(days=days)
        contracts = self.repo.active_contracts_expiring_within(
            today=today, deadline=deadline
        )
        generated = 0
        touched: list[UUID] = []
        for c in contracts:
            created = self.generate_for_contract(c, today=today, remind_days=days)
            generated += len(created)
            if created:
                touched.append(c.id)
        return {
            "scanned": len(contracts),
            "generated": generated,
            "skipped": len(contracts) - len(touched),
            "contract_ids": touched,
        }

    def _safe_assign(self, contract: Contract, assignee_id: UUID) -> UUID | None:
        """为单个责任人创建续签评估待办；同责任人已有未关闭则幂等跳过。"""
        try:
            task = self._tasks.assign(
                title=f"合同 {contract.contract_no} 即将到期：续签评估",
                assignee_id=assignee_id,
                task_kind=TASK_KIND_CONTRACT_EXPIRING_RENEWAL_EVAL,
                related_object_type="contract",
                related_object_id=contract.id,
                description=(
                    f"合同将于 {contract.expire_at.isoformat()} 到期，"
                    "请评估是否续签；首版仅提醒，不自动续签。"
                ),
            )
        except ConflictError:
            return None
        return task.id
