"""/api/master-data 路由：品牌与片区的只读选项（前端表单下拉用）。

T07 主数据导入只负责写入；本路由只暴露**非敏感参照数据**（品牌/片区的编码与名称），
供商家、合同、冰柜等表单选择，不返回任何成本、价格或联系人字段。

行级范围按服务端 actor 解析（PRD §2.4）：`row_scope=global` 返回全部；其余角色
只返回其授权片区 / 授权品牌。真正的业务拦截仍由各模块的能力鉴权负责（PRD §3.1）。
"""

from __future__ import annotations

# FastAPI 的 Depends() 必须出现在参数默认值中，统一豁免 B008。
# ruff: noqa: B008
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.deps import current_actor, db_session
from app.core.permissions import ActorContext
from app.models import Brand, Region

router = APIRouter(prefix="/api/master-data", tags=["master-data"])


class OptionOut(BaseModel):
    id: UUID
    code: str
    name: str


class OptionListOut(BaseModel):
    items: list[OptionOut]


def _is_global(actor: ActorContext) -> bool:
    return actor.profile.row_scope == "global"


@router.get("/brands", response_model=OptionListOut)
def list_brands(
    actor: ActorContext = Depends(current_actor),
    session: Session = Depends(db_session),
) -> OptionListOut:
    """可选品牌：全局角色返回全部；其余按 actor 的品牌授权裁剪。"""
    stmt = select(Brand).where(Brand.is_active.is_(True)).order_by(Brand.code.asc())
    if not _is_global(actor) and actor.brand_ids:
        stmt = stmt.where(Brand.id.in_(actor.brand_ids))
    rows = session.execute(stmt).scalars().all()
    return OptionListOut(
        items=[OptionOut(id=b.id, code=b.code, name=b.name) for b in rows]
    )


@router.get("/regions", response_model=OptionListOut)
def list_regions(
    actor: ActorContext = Depends(current_actor),
    session: Session = Depends(db_session),
) -> OptionListOut:
    """可选片区：全局角色返回全部；其余按 actor 的片区授权裁剪。"""
    stmt = select(Region).where(Region.is_active.is_(True)).order_by(Region.code.asc())
    if not _is_global(actor) and actor.region_ids:
        stmt = stmt.where(Region.id.in_(actor.region_ids))
    rows = session.execute(stmt).scalars().all()
    return OptionListOut(
        items=[OptionOut(id=r.id, code=r.code, name=r.name) for r in rows]
    )
