"""T21 报表/指标行级范围解析（PRD §2.4 角色即数据范围；§4.11 权限范围内风险）。

行级范围只能由服务端按 actor 的角色画像解析（禁止客户端指定）。解析结果是一个
`MerchantScope`：或为全局（is_global），或限定 `region_ids`，或限定具体
`merchant_ids`（业务员"本人负责商家"）。
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.permissions import ActorContext, get_profile
from app.models.merchant_assignment import MerchantAssignment


@dataclass(frozen=True)
class MerchantScope:
    """行级范围：region_ids / merchant_ids 为 None 表示不加该维度限制。"""

    region_ids: tuple[UUID, ...] | None = None
    merchant_ids: tuple[UUID, ...] | None = None
    is_global: bool = False

    @property
    def region_list(self) -> list[UUID] | None:
        return list(self.region_ids) if self.region_ids is not None else None

    @property
    def merchant_list(self) -> list[UUID] | None:
        return list(self.merchant_ids) if self.merchant_ids is not None else None


def resolve_merchant_scope(session: Session, actor: ActorContext) -> MerchantScope:
    """按 actor 角色画像解析行级范围。"""
    profile = get_profile(actor.role_code)
    if profile.row_scope in ("global", "global_readonly"):
        return MerchantScope(is_global=True)
    if profile.row_scope == "self":
        rows = session.execute(
            select(MerchantAssignment.merchant_id).where(
                MerchantAssignment.owner_user_id == actor.user_id,
                MerchantAssignment.valid_range.op("@>")(func.now()),
            )
        ).scalars().all()
        return MerchantScope(merchant_ids=tuple(set(rows)))
    # region / brand_region：限定授权片区
    return MerchantScope(region_ids=tuple(sorted(actor.region_ids)))
