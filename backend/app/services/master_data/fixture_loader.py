"""T07 补充演示夹具加载器。

导入《沁爽饮料-初始化夹具与补充演示数据.xlsx》，顺序（关联链数据域一致）：
1. 34 SKU + 15 账号（默认禁用/待激活，不发凭据）+ 5 片区 + 6 角色；
2. 20 商家（引用片区/品牌/负责人账号）；
3. 合同（引用商家/品牌）；
4. 8 冰柜（引用品牌/商家）；
5. 20 SKU 补货（引用商家/SKU）；
调拨/排班/地推/绩效/巡访 无对应落库模型（T08/T13/T19），本卡只做**引用与边界校验**，
不建表不落库；任何未知外键整批失败。

全部行固化 dataset_type=DEMO、dataset_id=DEMO-QINSHUANG-2026Q3(UUIDv5)。
单一批次全有或全无（SAVEPOINT）；写操作与审计同事务。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import UUID

from geoalchemy2.shape import from_shape
from shapely.geometry import Point
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import DomainError
from app.models import (
    Brand,
    Contract,
    FreezerAsset,
    ImportBatch,
    Merchant,
    Region,
    ReplenishmentOrder,
    Sku,
    User,
)
from app.services import demo_dataset as ds
from app.services.audit_service import AuditService
from app.services.demo_data_guard import assert_demo_disabled_in_production
from app.services.master_data.master_import_service import MasterDataImportService
from app.services.master_data.schemas import ParsedWorkbook
from app.services.master_data.workbook_parser import parse_workbook

BATCH_KIND_FIXTURE = "BUSINESS"


class FixtureLoadResult:
    def __init__(self, batch: ImportBatch) -> None:
        self.batch = batch


class DemoFixtureLoader:
    """把完整初始化夹具作为一个批次导入（主数据 + 业务边界场景）。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    def load(self, *, path: str, actor_user_id: UUID) -> ImportBatch:
        assert_demo_disabled_in_production(dataset_type=ds.DEMO_DATASET_TYPE)
        parsed = parse_workbook(path)

        existing = self._find_batch_by_hash(parsed.file_hash)
        if existing is not None:
            return existing

        # 行总数：主数据 + 商家 + 合同 + 冰柜 + 补货
        rows_total = (
            parsed.master_rows_total
            + len(parsed.merchants)
            + len(parsed.contracts)
            + len(parsed.freezers)
            + len(parsed.replenishments)
        )
        batch = ImportBatch(
            dataset_type=ds.DEMO_DATASET_TYPE,
            dataset_id=ds.DEMO_DATASET_UUID,
            source_filename=parsed.source_filename,
            file_hash=parsed.file_hash,
            seed_kind=BATCH_KIND_FIXTURE,
            rows_total=rows_total,
            rows_imported=0,
            rows_rejected=0,
            status="PENDING",
            payload_json={
                "stage": "UPLOADED",
                "ignored_columns": parsed.ignored_columns,
                "sku_count": len(parsed.skus),
                "account_count": len(parsed.accounts),
                "merchant_count": len(parsed.merchants),
                "contract_count": len(parsed.contracts),
                "freezer_count": len(parsed.freezers),
                "replenishment_count": len(parsed.replenishments),
                "referential_only": sorted({r.sheet for r in parsed.referential}),
                "account_enabled": {a.login_name: False for a in parsed.accounts},
                "supplier_portal_enabled": False,
                "role_matrix_source": "CODE_PERMISSION_MATRIX",
            },
            imported_by_id=actor_user_id,
        )
        self.session.add(batch)
        self.session.flush()
        AuditService(self.session).record(
            object_type="IMPORT_BATCH",
            object_id=batch.id,
            event_type="CREATE",
            actor_id=actor_user_id,
            after={"stage": "UPLOADED", "kind": BATCH_KIND_FIXTURE},
            reason="T07 完整初始化夹具上传",
        )

        if self._fixture_already_landed(parsed):
            batch.status = "SUCCEEDED"
            batch.rows_imported = rows_total
            batch.payload_json = {**batch.payload_json, "stage": "EXECUTED", "idempotent": True}
            self.session.flush()
            return batch

        nested = self.session.begin_nested()
        try:
            master = MasterDataImportService(self.session)
            # 先落主数据（品牌/片区/角色/34SKU/15账号），账号默认禁用、不发凭据
            credentials = master.execute(
                batch,
                actor_user_id=actor_user_id,
                parsed=parsed,
                account_decisions={a.login_name: False for a in parsed.accounts},
                issue_credentials=False,
            )
            assert credentials == []

            brand = self._require_brand()
            region_map = self._map_regions()
            user_map = self._map_users()
            sku_map = self._map_skus()
            merchant_map = self._persist_merchants(parsed, region_map, brand, user_map)
            self._persist_contracts(parsed, merchant_map, brand)
            self._persist_freezers(parsed, merchant_map, brand)
            self._persist_replenishments(parsed, merchant_map, sku_map)
            self._validate_referential(parsed, merchant_map, sku_map, user_map)
            nested.commit()
        except DomainError:
            nested.rollback()
            batch.status = "FAILED"
            batch.error_log = "夹具导入失败，已整批回滚"
            self.session.flush()
            raise
        except Exception as exc:
            nested.rollback()
            batch.status = "FAILED"
            batch.error_log = "夹具导入失败，已整批回滚"
            self.session.flush()
            raise DomainError(
                code="FIXTURE_IMPORT_FAILED",
                message="初始化夹具导入失败，已整批回滚（全有或全无）",
            ) from exc

        batch.status = "SUCCEEDED"
        batch.rows_imported = rows_total
        batch.rows_rejected = 0
        batch.payload_json = {
            **batch.payload_json,
            "stage": "EXECUTED",
            "idempotent": False,
            "credentials_issued": 0,
        }
        self.session.flush()
        AuditService(self.session).record(
            object_type="IMPORT_BATCH",
            object_id=batch.id,
            event_type="UPDATE",
            actor_id=actor_user_id,
            after={"stage": "EXECUTED", "rows_imported": rows_total, "credentials_issued": 0},
            reason="完整初始化夹具导入完成",
        )
        return batch

    # ---------------- 持久化 ----------------

    def _persist_merchants(self, parsed, region_map, brand, user_map) -> dict[str, Merchant]:
        out: dict[str, Merchant] = {}
        for m in parsed.merchants:
            existing = self.session.execute(
                select(Merchant).where(Merchant.code == m.code).limit(1)
            ).scalar_one_or_none()
            if existing is not None:
                out[m.code] = existing
                continue
            region = region_map.get(m.region_code)
            if region is None:
                raise DomainError(
                    code="FIXTURE_BAD_REFERENCE",
                    message="商家引用了未导入片区",
                    details={"merchant": m.code, "region_code": m.region_code},
                )
            location = None
            if m.longitude is not None and m.latitude is not None:
                location = from_shape(
                    Point(m.longitude, m.latitude), srid=4326
                )
            merchant = Merchant(
                code=m.code,
                name=m.name,
                status=m.status,
                grade=m.grade,
                region_id=region.id,
                brand_id=brand.id,
                location=location,
                address=m.address,
                contact_phone=m.contact_phone,
                dataset_type=ds.DEMO_DATASET_TYPE,
                dataset_id=ds.DEMO_DATASET_UUID,
            )
            self.session.add(merchant)
            self.session.flush()
            # 负责人关系在 T08+ 的归属/调拨模型承载；本卡仅校验账号存在
            if m.owner_login and m.owner_login not in user_map:
                raise DomainError(
                    code="FIXTURE_BAD_REFERENCE",
                    message="商家负责人账号不存在",
                    details={"merchant": m.code, "owner_login": m.owner_login},
                )
            out[m.code] = merchant
        return out

    def _persist_contracts(self, parsed, merchant_map, brand) -> None:
        # 旧台账状态 → T09 审批版本化状态
        _STATUS_MAP = {
            "DRAFT": "DRAFT",
            "PENDING_APPROVAL": "PENDING_APPROVAL",
            "ACTIVE": "EFFECTIVE",
            "EFFECTIVE": "EFFECTIVE",
            "EXPIRED": "TERMINATED",
            "TERMINATED": "TERMINATED",
        }
        for c in parsed.contracts:
            existing = self.session.execute(
                select(Contract).where(Contract.contract_no == c.code).limit(1)
            ).scalar_one_or_none()
            if existing is not None:
                continue
            merchant = merchant_map.get(c.merchant_code)
            if merchant is None or (c.brand_code and c.brand_code != ds.QINSHUANG_BRAND_CODE):
                raise DomainError(
                    code="FIXTURE_BAD_REFERENCE",
                    message="合同引用了未导入商家或非本品牌",
                    details={
                        "contract": c.code,
                        "merchant": c.merchant_code,
                        "brand": c.brand_code,
                    },
                )
            self.session.add(
                Contract(
                    contract_no=c.code,
                    merchant_id=merchant.id,
                    title=f"陈列合作协议 {c.code}",
                    party_name=merchant.name,
                    status=_STATUS_MAP.get(c.status, "DRAFT"),
                    signed_at=(
                        date.fromisoformat(c.signed_date) if c.signed_date else None
                    ),
                    effective_from=date.fromisoformat(c.start_date),
                    expire_at=date.fromisoformat(c.end_date),
                    display_threshold_pct=c.display_threshold_pct,
                    commission_rule_version="fixture-v1",
                    commission_rate=Decimal("0"),
                    dataset_type=ds.DEMO_DATASET_TYPE,
                    dataset_id=ds.DEMO_DATASET_UUID,
                )
            )
        self.session.flush()

    def _persist_freezers(self, parsed, merchant_map, brand) -> None:
        for f in parsed.freezers:
            existing = self.session.execute(
                select(FreezerAsset).where(FreezerAsset.asset_code == f.asset_code).limit(1)
            ).scalar_one_or_none()
            if existing is not None:
                continue
            if f.brand_code not in ("沁爽", "QINSHUANG", ds.QINSHUANG_BRAND_CODE):
                raise DomainError(
                    code="FIXTURE_BAD_REFERENCE",
                    message="冰柜品牌非沁爽，跨域引用被拒",
                    details={"asset": f.asset_code, "brand": f.brand_code},
                )
            current_merchant = (
                merchant_map.get(f.current_merchant_code) if f.current_merchant_code else None
            )
            self.session.add(
                FreezerAsset(
                    asset_code=f.asset_code,
                    brand_id=brand.id,
                    model=f.model,
                    purchase_date=date.fromisoformat(f.purchase_date),
                    purchase_price_fen=f.purchase_price_fen,
                    status=f.status,
                    current_merchant_id=(
                        current_merchant.id if current_merchant else None
                    ),
                    dataset_type=ds.DEMO_DATASET_TYPE,
                    dataset_id=ds.DEMO_DATASET_UUID,
                )
            )
        self.session.flush()

    def _persist_replenishments(self, parsed, merchant_map, sku_map) -> None:
        for r in parsed.replenishments:
            existing = self.session.execute(
                select(ReplenishmentOrder).where(ReplenishmentOrder.code == r.code).limit(1)
            ).scalar_one_or_none()
            if existing is not None:
                continue
            merchant = merchant_map.get(r.merchant_code)
            sku = sku_map.get(r.sku_code)
            if merchant is None or sku is None:
                raise DomainError(
                    code="FIXTURE_BAD_REFERENCE",
                    message="补货引用了未导入商家或 SKU",
                    details={
                        "request": r.code,
                        "merchant": r.merchant_code,
                        "sku": r.sku_code,
                    },
                )
            # SUBMITTED/REQUESTED 尚未确认，夹具确认箱数为 0；库约束要求
            # committed_boxes > 0，故未确认时以需求箱数作为提交量，语义不变。
            committed = (
                r.committed_boxes if r.committed_boxes > 0 else r.requested_boxes
            )
            self.session.add(
                ReplenishmentOrder(
                    code=r.code,
                    merchant_id=merchant.id,
                    sku_id=sku.id,
                    requested_boxes=r.requested_boxes,
                    committed_boxes=committed,
                    request_date=date.fromisoformat(r.request_date) if r.request_date else None,
                    promised_delivery_date=(
                        date.fromisoformat(r.promised_date) if r.promised_date else None
                    ),
                    actual_delivery_date=(
                        date.fromisoformat(r.actual_date) if r.actual_date else None
                    ),
                    status=r.status,
                    dataset_type=ds.DEMO_DATASET_TYPE,
                    dataset_id=ds.DEMO_DATASET_UUID,
                )
            )
        self.session.flush()

    # ---------------- 引用/边界校验（不落库的表） ----------------

    def _validate_referential(self, parsed, merchant_map, sku_map, user_map) -> None:
        for row in parsed.referential:
            refs = row.refs
            for key, val in refs.items():
                if not val:
                    continue
                if key in ("merchant_code",) and val not in merchant_map:
                    raise DomainError(
                        code="FIXTURE_BAD_REFERENCE",
                        message=f"{row.sheet} 引用了未导入商家",
                        details={"sheet": row.sheet, "row": row.row_no, key: val},
                    )
                if key in ("assignee", "原负责人", "新负责人", "参与账号", "业务员账号"):
                    for login in [p.strip() for p in val.split("|") if p.strip()]:
                        if login not in user_map:
                            raise DomainError(
                                code="FIXTURE_BAD_REFERENCE",
                                message=f"{row.sheet} 引用了未导入账号",
                                details={"sheet": row.sheet, "row": row.row_no, key: login},
                            )
                if key == "region_code" and not self._region_exists(val):
                    raise DomainError(
                        code="FIXTURE_BAD_REFERENCE",
                        message=f"{row.sheet} 引用了未导入片区",
                        details={"sheet": row.sheet, "row": row.row_no, key: val},
                    )
        # 排班/巡访/地推中的 source_no（DEMO-V/CP）本卡不落库，仅记录为边界场景，
        # 真正的冲突/有效性判定归 T13/T14 的 ScheduleConflictService/VisitValidityService。

    # ---------------- 映射/查询 ----------------

    def _require_brand(self) -> Brand:
        brand = self.session.execute(
            select(Brand).where(Brand.code == ds.QINSHUANG_BRAND_CODE).limit(1)
        ).scalar_one_or_none()
        if brand is None:
            raise DomainError(code="FIXTURE_BAD_STATE", message="品牌未建立，无法导入夹具")
        return brand

    def _map_regions(self) -> dict[str, Region]:
        rows = self.session.execute(
            select(Region).where(Region.dataset_id == ds.DEMO_DATASET_UUID)
        ).scalars().all()
        return {r.code: r for r in rows}

    def _region_exists(self, code: str) -> bool:
        return (
            self.session.execute(
                select(Region.id).where(Region.code == code).limit(1)
            ).first()
            is not None
        )

    def _map_users(self) -> dict[str, User]:
        rows = self.session.execute(
            select(User).where(User.dataset_id == ds.DEMO_DATASET_UUID)
        ).scalars().all()
        return {u.username: u for u in rows}

    def _map_skus(self) -> dict[str, Sku]:
        rows = self.session.execute(
            select(Sku).where(Sku.dataset_id == ds.DEMO_DATASET_UUID)
        ).scalars().all()
        return {s.code: s for s in rows}

    def _find_batch_by_hash(self, file_hash: str) -> ImportBatch | None:
        """幂等命中：只认**本数据集 + 已成功**的夹具批次。

        与 MasterDataImportService 同因：不限定数据集与状态时，任何同字节批次都会
        劫持重跑——失败批次之上无法重试，其它数据集的批次会污染本数据集。
        """
        if not file_hash:
            return None
        return self.session.execute(
            select(ImportBatch)
            .where(ImportBatch.file_hash == file_hash)
            .where(ImportBatch.seed_kind == BATCH_KIND_FIXTURE)
            .where(ImportBatch.dataset_id == ds.DEMO_DATASET_UUID)
            .where(ImportBatch.status == "SUCCEEDED")
            .order_by(ImportBatch.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()

    def _fixture_already_landed(self, parsed: ParsedWorkbook) -> bool:
        if not parsed.merchants:
            return False
        first = parsed.merchants[0].code
        return (
            self.session.execute(
                select(Merchant.id).where(Merchant.code == first).limit(1)
            ).first()
            is not None
        )
