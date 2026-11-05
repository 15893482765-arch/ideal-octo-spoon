"""合同持久化层（PRD §4.4 / R38）。只做读写，不含业务裁决。"""

from __future__ import annotations

from datetime import date
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models import (
    Contract,
    ContractVersion,
    Merchant,
    TaskItem,
)

# 行级宽范围角色：可读全部合同
WIDE_SCOPE_ROLES = {
    "SYSTEM_ADMIN",
    "REGIONAL_MANAGER",
    "SUPERVISOR",
    "FINANCE",
}


class ContractRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get_by_id(self, contract_id: UUID) -> Contract | None:
        return self.session.get(Contract, contract_id)

    def get_by_no(self, contract_no: str) -> Contract | None:
        return self.session.scalars(
            select(Contract).where(Contract.contract_no == contract_no)
        ).first()

    def get_merchant(self, merchant_id: UUID) -> Merchant | None:
        return self.session.get(Merchant, merchant_id)

    def list_for_actor(
        self,
        *,
        region_ids: list[UUID] | None,
        owner_user_id: UUID | None,
        role_code: str,
        status: str | None = None,
        keyword: str | None = None,
    ) -> list[Contract]:
        stmt = select(Contract)
        if role_code == "SALES_REP":
            # 业务员仅看本人负责的合同
            stmt = stmt.where(Contract.sales_rep_user_id == owner_user_id)
        elif region_ids is not None:
            stmt = stmt.join(
                Merchant, Merchant.id == Contract.merchant_id
            ).where(Merchant.region_id.in_(region_ids))
        if status:
            stmt = stmt.where(Contract.status == status)
        if keyword:
            like = f"%{keyword.strip()}%"
            stmt = stmt.where(
                Contract.contract_no.ilike(like) | Contract.title.ilike(like)
            )
        stmt = stmt.order_by(Contract.created_at.desc())
        return list(self.session.scalars(stmt).all())

    # ---------------- 版本 ----------------
    def list_versions(self, contract_id: UUID) -> list[ContractVersion]:
        return list(
            self.session.scalars(
                select(ContractVersion)
                .where(ContractVersion.contract_id == contract_id)
                .order_by(ContractVersion.version.asc())
            ).all()
        )

    def get_version(self, contract_id: UUID, version: int) -> ContractVersion | None:
        return self.session.scalars(
            select(ContractVersion).where(
                ContractVersion.contract_id == contract_id,
                ContractVersion.version == version,
            )
        ).first()

    def add_version(self, cv: ContractVersion) -> ContractVersion:
        self.session.add(cv)
        self.session.flush()
        return cv

    # ---------------- 续约（基于生效合同生成新合同）----------------
    def list_renewals(self, contract_id: UUID) -> list[Contract]:
        """由该合同续约产生的新合同（按创建时间升序）。"""
        return list(
            self.session.scalars(
                select(Contract)
                .where(Contract.renewed_from_contract_id == contract_id)
                .order_by(Contract.created_at.asc())
            ).all()
        )

    def has_effective_contract(self, merchant_id: UUID, *, today: date | None = None) -> bool:
        """该商家是否存在「已生效且未到期」的合同。

        口径与投柜守卫 `ContractService.assert_can_install_freezer` 完全一致
        （status=EFFECTIVE 且 expire_at 为空或未过期），供商家「合作中」状态前置校验
        复用（PRD R2：只有生效合同计入活跃商家/常规巡访口径）。
        """
        today = today or date.today()
        stmt = (
            select(Contract.id)
            .where(Contract.merchant_id == merchant_id)
            .where(Contract.status == "EFFECTIVE")
            .where(or_(Contract.expire_at.is_(None), Contract.expire_at >= today))
            .limit(1)
        )
        return self.session.scalars(stmt).first() is not None

    # ---------------- 续签（R38）----------------
    def active_contracts_expiring_within(
        self, *, today: date, deadline: date
    ) -> list[Contract]:
        """状态 EFFECTIVE 且到期日落在 [today, deadline] 的合同。"""
        return list(
            self.session.scalars(
                select(Contract)
                .where(Contract.status == "EFFECTIVE")
                .where(Contract.expire_at.is_not(None))
                .where(Contract.expire_at >= today)
                .where(Contract.expire_at <= deadline)
            ).all()
        )

    def find_renewal_task(
        self, *, contract_id: UUID, idempotency_key: str
    ) -> TaskItem | None:
        return self.session.scalars(
            select(TaskItem).where(
                TaskItem.related_object_type == "contract",
                TaskItem.related_object_id == contract_id,
                TaskItem.idempotency_key == idempotency_key,
            )
        ).first()

    def open_renewal_tasks(self, contract_id: UUID) -> list[TaskItem]:
        """合同当前未关闭的续签评估待办（各责任人一条）。"""
        return list(
            self.session.scalars(
                select(TaskItem).where(
                    TaskItem.related_object_type == "contract",
                    TaskItem.related_object_id == contract_id,
                    TaskItem.task_kind
                    == "CONTRACT_EXPIRING_RENEWAL_EVAL",
                    TaskItem.status.in_(["OPEN", "IN_PROGRESS"]),
                )
            ).all()
        )
