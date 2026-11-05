"""冰柜持久化层（PRD §4.5 / R11 / R12 / R37）。只做读写，不含业务裁决。"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models import FreezerAsset, FreezerMovement, Merchant, Role, User

# 物理在店 / 在库 / 维修 等口径集中在此，Service 不自行重算
STATUS_IN_STOCK = "IN_STOCK"
STATUS_RESERVED = "RESERVED"
STATUS_DEPLOYED = "DEPLOYED"
STATUS_PENDING_PICKUP = "PENDING_PICKUP"
STATUS_PENDING_ACCEPTANCE = "PENDING_ACCEPTANCE"
STATUS_REPAIRING = "REPAIRING"
STATUS_SCRAPPED = "SCRAPPED"

# 仍绑定商家的物理在店状态
MERCHANT_BOUND_STATUSES = (STATUS_DEPLOYED, STATUS_PENDING_PICKUP)

# 押金流水类型
MOV_DEPOSIT_COLLECTED = "DEPOSIT_COLLECTED"
MOV_DEPOSIT_REFUNDED = "DEPOSIT_REFUNDED"


class FreezerRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    # ---------------- 资产 ----------------
    def get(self, asset_id: UUID, *, for_update: bool = False) -> FreezerAsset | None:
        stmt = select(FreezerAsset).where(FreezerAsset.id == asset_id)
        if for_update:
            stmt = stmt.with_for_update()
        return self.session.scalars(stmt).first()

    def get_by_code(self, asset_code: str) -> FreezerAsset | None:
        return self.session.scalars(
            select(FreezerAsset).where(FreezerAsset.asset_code == asset_code)
        ).first()

    def add_asset(self, asset: FreezerAsset) -> FreezerAsset:
        self.session.add(asset)
        self.session.flush()
        return asset

    def count_in_stock(self, *, dataset_type: str | None = None) -> int:
        """可用库存：仅 IN_STOCK（R11：待验收不算在库）。"""
        stmt = select(func.count()).select_from(FreezerAsset).where(
            FreezerAsset.status == STATUS_IN_STOCK,
            FreezerAsset.is_active.is_(True),
        )
        if dataset_type is not None:
            stmt = stmt.where(FreezerAsset.dataset_type == dataset_type)
        return int(self.session.execute(stmt).scalar_one())

    def search(
        self,
        *,
        asset_code: str | None = None,
        status: str | None = None,
        merchant_id: UUID | None = None,
        merchant_ids: list[UUID] | None = None,
        region_ids: list[UUID] | None = None,
        include_unassigned: bool = False,
        limit: int = 50,
    ) -> list[FreezerAsset]:
        """冰柜台账只读查询（T23 只读工具用）。

        行级裁剪参数由 Service 依据服务端 actor 计算：
        - `merchant_ids`：业务员本人负责商家（资产须绑在其中之一）；
        - `region_ids`：片区角色/厂商授权片区（join 当前商家取 region）；
        - `include_unassigned`：片区角色的**可投放池**——尚未绑定商家（在库/维修中/
          待验收）的资产不属于任何片区，若仍按 region 内连接裁剪，财务购置入库的
          冰柜对片区经理/督导永远不可见，也就无法创建投放单。
        """
        stmt = select(FreezerAsset)
        if asset_code:
            stmt = stmt.where(FreezerAsset.asset_code.ilike(f"%{asset_code.strip()}%"))
        if status:
            stmt = stmt.where(FreezerAsset.status == status.upper())
        if merchant_id is not None:
            stmt = stmt.where(FreezerAsset.current_merchant_id == merchant_id)
        if merchant_ids is not None:
            if not merchant_ids:
                return []
            stmt = stmt.where(FreezerAsset.current_merchant_id.in_(merchant_ids))
        if region_ids is not None:
            if not region_ids:
                return []
            if include_unassigned:
                # 外连接：未绑定商家的资产（可投放池）不应被片区裁剪排除
                stmt = stmt.outerjoin(
                    Merchant, Merchant.id == FreezerAsset.current_merchant_id
                ).where(
                    or_(
                        FreezerAsset.current_merchant_id.is_(None),
                        Merchant.region_id.in_(region_ids),
                    )
                )
            else:
                stmt = stmt.join(
                    Merchant, Merchant.id == FreezerAsset.current_merchant_id
                ).where(Merchant.region_id.in_(region_ids))
        stmt = stmt.order_by(FreezerAsset.asset_code.asc()).limit(limit)
        return list(self.session.scalars(stmt).all())

    def deployed_for_merchant(self, merchant_id: UUID) -> list[FreezerAsset]:
        """商家当前物理在店的冰柜（已投放 / 待撤回现场未回收）。"""
        return list(
            self.session.scalars(
                select(FreezerAsset)
                .where(FreezerAsset.current_merchant_id == merchant_id)
                .where(FreezerAsset.status.in_(MERCHANT_BOUND_STATUSES))
            ).all()
        )

    # ---------------- 流水 ----------------
    def add_movement(self, movement: FreezerMovement) -> FreezerMovement:
        self.session.add(movement)
        self.session.flush()
        return movement

    def get_movement(self, movement_id: UUID) -> FreezerMovement | None:
        return self.session.get(FreezerMovement, movement_id)

    def list_movements(self, asset_id: UUID) -> list[FreezerMovement]:
        return list(
            self.session.scalars(
                select(FreezerMovement)
                .where(FreezerMovement.asset_id == asset_id)
                .order_by(FreezerMovement.occurred_at.asc(), FreezerMovement.created_at.asc())
            ).all()
        )

    def latest_open_deposit(self, asset_id: UUID) -> FreezerMovement | None:
        """最近一笔押金收缴，且整笔尚未退还（供退款定位，R37）。

        一笔收缴只允许一次退款动作（全额或部分）；已存在关联的 DEPOSIT_REFUNDED
        即视为处理完毕，重复退款直接返回 None（幂等拒绝）。
        """
        collected = list(
            self.session.scalars(
                select(FreezerMovement)
                .where(FreezerMovement.asset_id == asset_id)
                .where(FreezerMovement.movement_type == MOV_DEPOSIT_COLLECTED)
                .order_by(FreezerMovement.occurred_at.desc(), FreezerMovement.created_at.desc())
            ).all()
        )
        for coll in collected:
            refunded = self.session.execute(
                select(func.count())
                .select_from(FreezerMovement)
                .where(
                    FreezerMovement.movement_type == MOV_DEPOSIT_REFUNDED,
                    FreezerMovement.related_movement_id == coll.id,
                )
            ).scalar_one()
            if int(refunded) == 0:
                return coll
        return None

    # ---------------- 责任人解析（服务端注入，不接受客户端指定）----------------
    def first_active_user_with_role(
        self, role_code: str, *, dataset_id: UUID | None = None
    ) -> User | None:
        """系统侧待办无明确责任人时，解析一名该角色的在职用户兜底。

        必须按数据域过滤：否则生产资产的待办可能被派给 DEMO 数据域用户，
        违反数据域隔离红线（PRD R34）。``dataset_id`` 为空时退化为全局查询，
        仅供无数据域上下文的系统级调用。
        """
        stmt = (
            select(User)
            .join(Role, Role.id == User.role_id)
            .where(Role.role_code == role_code, User.status == "ACTIVE")
        )
        if dataset_id is not None:
            stmt = stmt.where(User.dataset_id == dataset_id)
        return self.session.scalars(
            stmt.order_by(User.created_at.asc())
        ).first()
