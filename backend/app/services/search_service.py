"""全局搜索（T27 / PRD §4.2 全局搜索框）。

红线（PRD §9.1）：搜索**只返回当前用户有权查看的对象**——行级范围只来自服务端
ActorContext（JWT/session 注入的 region/brand/dataset），绝不接受客户端传入范围；
越权关键字（命中其他片区/品牌/数据域）结果为空（Q155）。

搜索实体（首版）：商家（名称/编号/主联系人）。返回结构只含安全字段 + 深链 +
模块标签；精确坐标、电话等敏感字段不返回（厂商还受字段级可见性约束）。

DEMO 数据不进搜索（dataset_type=PRODUCTION）。
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlencode
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.core.errors import DomainError
from app.core.permissions import (
    MODULE_MERCHANT,
    ActorContext,
    permission_service,
)
from app.models.enums import MerchantStatus
from app.models.merchant import Merchant
from app.models.user import User
from app.repositories.reporting_repository import ReportingRepository
from app.services.report_scope import resolve_merchant_scope

_MIN_QUERY = 2
_MAX_QUERY = 64
_MAX_RESULTS = 20


@dataclass(frozen=True)
class SearchHit:
    object_type: str
    object_id: UUID
    title: str
    subtitle: str
    status: str
    module: str
    deep_link: str

    def to_dict(self) -> dict:
        return {
            "object_type": self.object_type,
            "object_id": str(self.object_id),
            "title": self.title,
            "subtitle": self.subtitle,
            "status": self.status,
            "module": self.module,
            "deep_link": self.deep_link,
        }


def build_deep_link(path: str, *, q: str | None = None, **filters: object) -> str:
    """组装保留筛选条件的深链（Q157：从聚合页跳列表页可恢复筛选）。

    空值过滤；布尔/数值转字符串；路径以 / 开头。
    """
    params: dict[str, str] = {}
    if q:
        params["q"] = q
    for key, value in filters.items():
        if value is None or value == "":
            continue
        params[key] = str(value)
    return f"{path}?{urlencode(params)}" if params else path


class GlobalSearchService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.repo = ReportingRepository(session)

    def search_merchants(
        self, actor: ActorContext, query: str, *, limit: int = _MAX_RESULTS
    ) -> list[SearchHit]:
        q = (query or "").strip()
        if len(q) < _MIN_QUERY:
            return []
        q = q[:_MAX_QUERY]
        # 无商家模块权限：直接空（厂商无 merchant 能力，搜不到任何商家）
        if not permission_service.has_ability(actor, MODULE_MERCHANT):
            return []

        user = self.session.get(User, actor.user_id)
        if user is None:
            raise DomainError(code="USER_NOT_FOUND", message="登录用户不存在")

        like = f"%{q}%"
        rows = list(self.session.scalars(
            select(Merchant)
            .where(
                Merchant.dataset_type == "PRODUCTION",
                Merchant.dataset_id == user.dataset_id,
                Merchant.status != MerchantStatus.TERMINATED.value,
                or_(
                    Merchant.name.ilike(like),
                    Merchant.code.ilike(like),
                    Merchant.primary_contact_name.ilike(like),
                ),
            )
            .order_by(Merchant.name.asc())
            .limit(200)
        ).all())

        # 行级裁剪：region/brand/self 范围由服务端 actor 决定（越权为空）
        mscope = resolve_merchant_scope(self.session, actor)
        if not mscope.is_global:
            allowed = self.repo.scope_merchant_ids(user.dataset_id, mscope)
            rows = [m for m in rows if m.id in allowed]

        hits: list[SearchHit] = []
        for m in rows[: max(1, min(int(limit), _MAX_RESULTS))]:
            hits.append(SearchHit(
                object_type="MERCHANT",
                object_id=m.id,
                title=m.name,
                subtitle=f"{m.code or ''} {m.grade or ''}".strip(),
                status=m.status,
                module="merchant",
                # 深链保留搜索关键字（Q157 深链筛选恢复）
                deep_link=build_deep_link("/merchants", q=q, merchant_id=m.id),
            ))
        return hits
