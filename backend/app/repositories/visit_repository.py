"""巡访持久化层（PRD §4.6 / T11）。

只做查询/落库，不做鉴权与有效性裁决；距离由 PostGIS 在 SQL 内计算，
Repository 不采信前端上报距离。
"""

from __future__ import annotations

from datetime import date
from uuid import UUID

from geoalchemy2 import WKTElement
from geoalchemy2 import functions as gfunc
from geoalchemy2.types import Geometry
from sqlalchemy import String, cast, func, literal, select
from sqlalchemy.orm import Session

from app.models import (
    Contract,
    FileObject,
    Merchant,
    ReplenishmentOrder,
    Role,
    Sku,
    User,
    UserRegionBrandAuth,
    VisitPhoto,
    VisitRecord,
    VisitRevision,
    VisitSalesLine,
    VisitTask,
)


def point_geography(lng: float, lat: float) -> WKTElement:
    """WGS84 点（经度, 纬度）→ EWKT，供写入 geography 列。"""
    return WKTElement(f"POINT({lng} {lat})", srid=4326)


def _geography_literal(lng: float, lat: float):
    """把经纬度转成带类型的 geography 表达式（ST_Distance 走 geography 重载，米）。"""
    return func.ST_GeogFromText(literal(f"SRID=4326;POINT({lng} {lat})", String))


class VisitRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    # ---------- 任务 ----------
    def get_task(self, task_id: UUID, for_update: bool = False) -> VisitTask | None:
        stmt = select(VisitTask).where(VisitTask.id == task_id)
        if for_update:
            stmt = stmt.with_for_update()
        return self.session.scalars(stmt).first()

    def add_task(self, task: VisitTask) -> VisitTask:
        self.session.add(task)
        self.session.flush()
        return task

    def list_tasks_for_actor(
        self,
        *,
        user_id: UUID,
        on_date: date | None,
        region_ids: frozenset[UUID] | None,
    ) -> list[VisitTask]:
        """责任人工作台：业务员看自己的任务，经理/督导看本片区任务。"""
        stmt = select(VisitTask).join(Merchant, Merchant.id == VisitTask.merchant_id)
        if region_ids is not None:
            stmt = stmt.where(Merchant.region_id.in_(region_ids))
        else:
            stmt = stmt.where(VisitTask.assigned_user_id == user_id)
        if on_date is not None:
            stmt = stmt.where(VisitTask.planned_date == on_date)
        stmt = stmt.order_by(VisitTask.planned_date.asc(), VisitTask.priority.asc())
        return list(self.session.scalars(stmt).all())

    def official_record_for_task(self, task_id: UUID) -> VisitRecord | None:
        """该任务下已有的正式记录（is_official=True，至多一条）。"""
        stmt = select(VisitRecord).where(
            VisitRecord.visit_task_id == task_id,
            VisitRecord.is_official.is_(True),
        )
        return self.session.scalars(stmt).first()

    def get_record(self, record_id: UUID, for_update: bool = False) -> VisitRecord | None:
        stmt = select(VisitRecord).where(VisitRecord.id == record_id)
        if for_update:
            stmt = stmt.with_for_update()
        return self.session.scalars(stmt).first()

    def get_merchant(self, merchant_id: UUID) -> Merchant | None:
        return self.session.get(Merchant, merchant_id)

    def regional_manager(
        self, region_id: UUID, dataset_id: UUID, brand_id: UUID | None = None
    ) -> User | None:
        """解析该片区在职片区经理（补货 DRAFT 复核待办指给经理，而非上报业务员）。

        范围由服务端行级授权表 user_region_brand_auths 决定（业务员不能确认自己的
        补货，PRD 安全红线）；同一品牌可进一步收窄，未命中则回退片区任一经理。
        """
        base = (
            select(User)
            .join(Role, Role.id == User.role_id)
            .join(UserRegionBrandAuth, UserRegionBrandAuth.user_id == User.id)
            .where(Role.role_code == "REGIONAL_MANAGER")
            .where(User.status == "ACTIVE")
            .where(User.dataset_id == dataset_id)
            .where(UserRegionBrandAuth.region_id == region_id)
        )
        if brand_id is not None:
            scoped = self.session.scalars(
                base.where(UserRegionBrandAuth.brand_id == brand_id)
                .order_by(User.created_at.asc())
            ).first()
            if scoped is not None:
                return scoped
        return self.session.scalars(
            base.order_by(User.created_at.asc())
        ).first()

    # ---------- 距离（服务端权威口径） ----------
    def distance_to_merchant_meters(
        self, merchant: Merchant, lng: float, lat: float
    ) -> float | None:
        """PostGIS ST_Distance(geography) 米；商家无坐标时返回 None（服务端权威口径）。"""
        row = self.session.execute(
            select(
                gfunc.ST_Distance(Merchant.location, _geography_literal(lng, lat))
            ).where(Merchant.id == merchant.id)
        ).first()
        if row is None or row[0] is None:
            return None
        return float(row[0])

    def merchant_lng_lat(self, merchant: Merchant) -> tuple[float, float] | None:
        return self._lng_lat(Merchant.location, Merchant.id, merchant.id)

    def record_lng_lat(self, record: VisitRecord) -> tuple[float, float] | None:
        """巡访打卡点经纬度（服务端落库坐标，供地图回溯）。"""
        return self._lng_lat(VisitRecord.location, VisitRecord.id, record.id)

    def _lng_lat(self, column, id_column, row_id: UUID):
        geom = cast(column, Geometry(geometry_type="POINT", srid=4326))
        row = self.session.execute(
            select(func.ST_X(geom), func.ST_Y(geom)).where(
                id_column == row_id, column.isnot(None)
            )
        ).first()
        if row is None or row[0] is None:
            return None
        return float(row[0]), float(row[1])

    # ---------- 记录 / 照片 / 修订 ----------
    def add_record(self, record: VisitRecord) -> VisitRecord:
        self.session.add(record)
        self.session.flush()
        return record

    def add_photo(self, photo: VisitPhoto) -> VisitPhoto:
        self.session.add(photo)
        self.session.flush()
        return photo

    def get_file(self, file_id: UUID) -> FileObject | None:
        return self.session.get(FileObject, file_id)

    def count_photos(self, record_id: UUID) -> int:
        return int(
            self.session.scalar(
                select(func.count())
                .select_from(VisitPhoto)
                .where(VisitPhoto.visit_record_id == record_id)
            )
        )

    def list_photos(self, record_id: UUID) -> list[VisitPhoto]:
        return list(
            self.session.scalars(
                select(VisitPhoto)
                .where(VisitPhoto.visit_record_id == record_id)
                .order_by(VisitPhoto.created_at.asc())
            ).all()
        )

    def add_revision(self, revision: VisitRevision) -> VisitRevision:
        self.session.add(revision)
        self.session.flush()
        return revision

    def list_revisions(self, record_id: UUID) -> list[VisitRevision]:
        return list(
            self.session.scalars(
                select(VisitRevision)
                .where(VisitRevision.visit_record_id == record_id)
                .order_by(VisitRevision.created_at.asc())
            ).all()
        )

    # ---------- T12：合同阈值 / 销量行 / 补货草稿 ----------

    def effective_contract(self, merchant_id: UUID, on_date: date) -> Contract | None:
        """商家在 on_date 当天生效（EFFECTIVE 且覆盖该日）的合同；取最新一条。

        陈列阈值在建合同时固化（R3），提交巡访即从此快照取值，后改合同不影响历史判定。
        """
        stmt = (
            select(Contract)
            .where(Contract.merchant_id == merchant_id)
            .where(Contract.status == "EFFECTIVE")
            .where(
                (Contract.effective_from.is_(None)) | (Contract.effective_from <= on_date)
            )
            .where((Contract.expire_at.is_(None)) | (Contract.expire_at >= on_date))
            .order_by(Contract.current_version.desc(), Contract.signed_at.desc().nullslast())
        )
        return self.session.scalars(stmt).first()

    def list_skus_for_brand(self, brand_id: UUID, dataset_id: UUID) -> list[Sku]:
        """巡访提报用的 SKU 下拉（同品牌 + 同数据范围，按编码排序）。"""
        stmt = (
            select(Sku)
            .where(Sku.brand_id == brand_id)
            .where(Sku.dataset_id == dataset_id)
            .order_by(Sku.code.asc())
        )
        return list(self.session.scalars(stmt).all())

    def get_skus(self, sku_ids: set[UUID]) -> dict[UUID, Sku]:
        if not sku_ids:
            return {}
        rows = self.session.scalars(select(Sku).where(Sku.id.in_(sku_ids))).all()
        return {s.id: s for s in rows}

    def add_sales_line(self, line: VisitSalesLine) -> VisitSalesLine:
        self.session.add(line)
        self.session.flush()
        return line

    def list_sales_lines(self, record_id: UUID) -> list[VisitSalesLine]:
        return list(
            self.session.scalars(
                select(VisitSalesLine)
                .where(VisitSalesLine.visit_record_id == record_id)
                .order_by(VisitSalesLine.created_at.asc())
            ).all()
        )

    def add_replenishment(self, order: ReplenishmentOrder) -> ReplenishmentOrder:
        self.session.add(order)
        self.session.flush()
        return order

    def list_replenishments_for_record(
        self, record_id: UUID
    ) -> list[ReplenishmentOrder]:
        return list(
            self.session.scalars(
                select(ReplenishmentOrder)
                .where(ReplenishmentOrder.source_visit_record_id == record_id)
                .order_by(ReplenishmentOrder.created_at.asc())
            ).all()
        )
